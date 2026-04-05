"""
EagleVision - CV Microservice (with Re-ID)
"""

import os
import cv2
import json
import pickle
import logging
import numpy as np
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from confluent_kafka import Producer
from ultralytics import YOLO

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [CV] %(levelname)s  %(message)s",
)
log = logging.getLogger(__name__)

KAFKA_BOOTSTRAP   = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KAFKA_TOPIC       = os.getenv("KAFKA_TOPIC", "equipment-events")
VIDEO_SOURCE      = os.getenv("VIDEO_PATH") or os.getenv("VIDEO_SOURCE", "0")
FRAME_SKIP        = int(os.getenv("FRAME_SKIP", "2"))
MOTION_THR        = float(os.getenv("MOTION_THRESHOLD", "500"))
ARM_MOTION_THR    = float(os.getenv("ARM_MOTION_THRESHOLD", "200"))
FRAME_OUT_DIR     = os.getenv("FRAME_OUT_DIR", "/frames")
YOLO_MODEL        = os.getenv("YOLO_MODEL", "yolov8m.pt")
REID_THRESHOLD    = float(os.getenv("REID_THRESHOLD", "0.82"))
REID_GALLERY_PATH = os.getenv("REID_GALLERY_PATH", "/frames/reid_gallery.pkl")
REID_MAX_ABSENT   = int(os.getenv("REID_MAX_ABSENT_FRAMES", "150"))

TARGET_CLASSES = {0: "person", 7: "truck", 8: "bus"}


def _delivery_report(err, msg):
    if err:
        log.error("Kafka delivery failed: %s", err)


def split_regions(x1, y1, x2, y2):
    h = y2 - y1
    arm_y2 = int(y1 + h * 0.45)
    return (x1, y1, x2, arm_y2), (x1, arm_y2, x2, y2)


def flow_score_in_roi(flow, x1, y1, x2, y2):
    h, w = flow.shape[:2]
    x1c, y1c = max(0, x1), max(0, y1)
    x2c, y2c = min(w, x2), min(h, y2)
    if x2c <= x1c or y2c <= y1c:
        return 0.0
    roi = flow[y1c:y2c, x1c:x2c]
    return float(np.sqrt(roi[..., 0]**2 + roi[..., 1]**2).sum())


class ReIDEmbedder:
    """
    Zero-RAM embedder using colour histograms + gradient features.
    No neural network — runs in <1ms per crop on CPU.
    """
    def __init__(self):
        log.info("Re-ID embedder ready (histogram+gradient, zero RAM overhead)")

    def embed(self, crop: np.ndarray) -> np.ndarray:
        if crop.size == 0:
            return np.zeros(384, dtype=np.float32)
        img   = cv2.resize(crop, (64, 128))
        hsv   = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        h_hist = cv2.calcHist([hsv], [0], None, [128], [0, 180]).flatten()
        s_hist = cv2.calcHist([hsv], [1], None, [128], [0, 256]).flatten()
        v_hist = cv2.calcHist([hsv], [2], None, [128], [0, 256]).flatten()
        gray  = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
        gx    = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
        gy    = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
        mag   = np.sqrt(gx**2 + gy**2)
        g_hist = np.histogram(mag, bins=128, range=(0, 255))[0].astype(np.float32)
        feat  = np.concatenate([h_hist, s_hist, v_hist, g_hist]).astype(np.float32)
        norm  = np.linalg.norm(feat)
        return feat / (norm + 1e-6)


class ReIDGallery:
    def __init__(self, gallery_path, embedder):
        self.path        = Path(gallery_path)
        self.embedder    = embedder
        self.gallery     = {}
        self.active_map  = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                with open(self.path, "rb") as f:
                    data = pickle.load(f)
                self.gallery    = data.get("gallery", {})
                self.active_map = data.get("active_map", {})
                log.info("Re-ID gallery loaded: %d known identities", len(self.gallery))
            except Exception as exc:
                log.warning("Could not load Re-ID gallery: %s", exc)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "wb") as f:
            pickle.dump({"gallery": self.gallery, "active_map": self.active_map}, f)

    def _cosine_sim(self, a, b):
        return float(np.dot(a, b))

    def resolve(self, bytetrack_id, crop, class_name, frame_id):
        if bytetrack_id in self.active_map:
            sid = self.active_map[bytetrack_id]
            if sid in self.gallery:
                self._update_embedding(sid, crop, frame_id)
            return sid, False

        embedding = self.embedder.embed(crop)
        best_sid, best_score = self._find_best_match(embedding, class_name)

        if best_score >= REID_THRESHOLD:
            stable_id = best_sid
            is_reid   = True
            log.info("Re-ID match: track %d -> %s (score=%.3f)",
                     bytetrack_id, stable_id, best_score)
        else:
            prefix    = "EX" if "truck" in class_name or "bus" in class_name else "DT"
            stable_id = f"{prefix}-{bytetrack_id:03d}"
            is_reid   = False
            log.info("New equipment: %s", stable_id)

        self.active_map[bytetrack_id] = stable_id
        self._update_embedding(stable_id, crop, frame_id)
        return stable_id, is_reid

    def _find_best_match(self, embedding, class_name):
        prefix     = "EX" if "truck" in class_name or "bus" in class_name else "DT"
        best_sid   = None
        best_score = -1.0
        for sid, entry in self.gallery.items():
            if not sid.startswith(prefix):
                continue
            if sid in self.active_map.values():
                continue
            if entry.get("absent", 0) < 5:
                continue
            score = self._cosine_sim(embedding, entry["embedding"])
            if score > best_score:
                best_score = score
                best_sid   = sid
        return best_sid, best_score

    def _update_embedding(self, stable_id, crop, frame_id):
        new_emb = self.embedder.embed(crop)
        if stable_id not in self.gallery:
            self.gallery[stable_id] = {"embedding": new_emb, "last_seen": frame_id, "absent": 0}
        else:
            old = self.gallery[stable_id]["embedding"]
            self.gallery[stable_id]["embedding"] = 0.9 * old + 0.1 * new_emb
            self.gallery[stable_id]["last_seen"] = frame_id
            self.gallery[stable_id]["absent"]    = 0

    def tick_absent(self, active_bytetrack_ids, frame_id):
        active_stable = {self.active_map[bid] for bid in active_bytetrack_ids
                         if bid in self.active_map}
        for sid in list(self.gallery.keys()):
            if sid not in active_stable:
                self.gallery[sid]["absent"] = self.gallery[sid].get("absent", 0) + 1
                stale = [bid for bid, s in self.active_map.items() if s == sid]
                for bid in stale:
                    del self.active_map[bid]
            if self.gallery[sid].get("absent", 0) > REID_MAX_ABSENT * 10:
                del self.gallery[sid]


class ActivityClassifier:
    HISTORY = 8

    def __init__(self):
        self._arm    = deque(maxlen=self.HISTORY)
        self._tracks = deque(maxlen=self.HISTORY)
        self._cx     = deque(maxlen=self.HISTORY)

    def update(self, arm, tracks, cx):
        self._arm.append(arm)
        self._tracks.append(tracks)
        self._cx.append(cx)
        avg_arm    = np.mean(self._arm)
        avg_tracks = np.mean(self._tracks)
        cx_std     = np.std(self._cx) if len(self._cx) > 2 else 0.0
        if avg_arm < ARM_MOTION_THR and avg_tracks < ARM_MOTION_THR:
            return "WAITING"
        if avg_arm > ARM_MOTION_THR and cx_std > 4.0:
            return "SWINGING"
        if avg_arm > ARM_MOTION_THR and avg_tracks < ARM_MOTION_THR * 0.5:
            return "DIGGING"
        if avg_tracks > ARM_MOTION_THR:
            return "DUMPING"
        return "DIGGING"


class EquipmentTracker:
    def __init__(self, equipment_id, equipment_class, fps,
                 restore_active=0.0, restore_idle=0.0):
        self.equipment_id    = equipment_id
        self.equipment_class = equipment_class
        self._fps            = fps
        self.total_active_sec  = restore_active
        self.total_idle_sec    = restore_idle
        self.total_tracked_sec = restore_active + restore_idle
        self.classifier        = ActivityClassifier()

    @property
    def utilization_pct(self):
        if self.total_tracked_sec < 0.001:
            return 0.0
        return round(100.0 * self.total_active_sec / self.total_tracked_sec, 1)

    def update(self, arm, tracks, full, cx, frame_skip):
        dt       = frame_skip / self._fps
        activity = self.classifier.update(arm, tracks, cx)
        is_active = activity != "WAITING"
        self.total_tracked_sec += dt
        if is_active:
            self.total_active_sec += dt
        else:
            self.total_idle_sec += dt
        src = "arm_only" if arm > ARM_MOTION_THR and tracks < ARM_MOTION_THR * 0.5 \
              else "tracks_only" if tracks > ARM_MOTION_THR and arm < ARM_MOTION_THR * 0.5 \
              else "full_body" if full > MOTION_THR else "none"
        return {
            "current_state":    "ACTIVE" if is_active else "INACTIVE",
            "current_activity": activity,
            "motion_source":    src,
        }


STATE_COLOR = {"ACTIVE": (0, 220, 80), "INACTIVE": (60, 60, 220)}

def draw_overlay(frame, detections):
    out = frame.copy()
    for det in detections:
        x1, y1, x2, y2 = det["bbox"]
        color  = STATE_COLOR.get(det["state"], (200, 200, 200))
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
        arm_roi, _ = split_regions(x1, y1, x2, y2)
        for dx in range(x1, x2, 8):
            cv2.line(out, (dx, arm_roi[3]), (dx+4, arm_roi[3]), (200,200,0), 1)
        reid_tag = " [Re-ID]" if det.get("reidentified") else ""
        label  = f"{det['equipment_id']}{reid_tag} | {det['state']} | {det['activity']}"
        util_l = f"Util:{det['utilization_pct']:.1f}%  Idle:{det.get('idle_sec',0):.0f}s"
        (tw, _), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
        cv2.rectangle(out, (x1, y1-36), (x1+max(tw,100)+6, y1), color, -1)
        cv2.putText(out, label,  (x1+3, y1-22), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (10,10,10), 1)
        cv2.putText(out, util_l, (x1+3, y1-6),  cv2.FONT_HERSHEY_SIMPLEX, 0.40, (10,10,10), 1)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d  %H:%M:%S UTC")
    cv2.putText(out, ts, (10, out.shape[0]-10), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (200,200,200), 1)
    return out


def main():
    log.info("Starting EagleVision CV service (with Re-ID)")
    log.info("  Video source    : %s", VIDEO_SOURCE)
    log.info("  Kafka broker    : %s -> %s", KAFKA_BOOTSTRAP, KAFKA_TOPIC)
    log.info("  Re-ID threshold : %.2f", REID_THRESHOLD)

    producer = Producer({
        "bootstrap.servers": KAFKA_BOOTSTRAP,
        "client.id": "eaglevision-cv",
        "acks": "1",
        "linger.ms": 50,
    })

    log.info("Loading YOLOv8 model: %s", YOLO_MODEL)
    model    = YOLO(YOLO_MODEL)
    embedder = ReIDEmbedder()
    gallery  = ReIDGallery(REID_GALLERY_PATH, embedder)

    src = int(VIDEO_SOURCE) if VIDEO_SOURCE.isdigit() else VIDEO_SOURCE
    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        log.error("Cannot open video source: %s", VIDEO_SOURCE)
        return

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    log.info("Video FPS: %.1f", fps)
    os.makedirs(FRAME_OUT_DIR, exist_ok=True)

    prev_gray    = None
    frame_id     = 0
    trackers     = {}
    skip_counter = 0
    last_save    = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            log.info("End of video stream.")
            break

        frame_id     += 1
        skip_counter += 1
        if skip_counter < FRAME_SKIP:
            continue
        skip_counter = 0

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if prev_gray is not None:
            flow = cv2.calcOpticalFlowFarneback(
                prev_gray, gray, None,
                pyr_scale=0.5, levels=3, winsize=15,
                iterations=3, poly_n=5, poly_sigma=1.2, flags=0)
        else:
            flow = np.zeros((*gray.shape, 2), dtype=np.float32)
        prev_gray = gray

        results = model.track(
            frame, persist=True, tracker="bytetrack.yaml",
            classes=list(TARGET_CLASSES.keys()), verbose=False)

        detections_for_draw  = []
        timestamp_str        = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        active_bytetrack_ids = set()

        for result in results:
            if result.boxes is None:
                continue
            for box in result.boxes:
                if box.id is None:
                    continue

                bytetrack_id = int(box.id.item())
                cls_id       = int(box.cls.item())
                conf         = float(box.conf.item())
                cls_name     = TARGET_CLASSES.get(cls_id, "unknown")
                x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                active_bytetrack_ids.add(bytetrack_id)

                crop      = frame[max(0,y1):y2, max(0,x1):x2]
                stable_id, is_reid = gallery.resolve(bytetrack_id, crop, cls_name, frame_id)

                full_score   = flow_score_in_roi(flow, x1, y1, x2, y2)
                arm_roi, tracks_roi = split_regions(x1, y1, x2, y2)
                arm_score    = flow_score_in_roi(flow, *arm_roi)
                tracks_score = flow_score_in_roi(flow, *tracks_roi)

                if stable_id not in trackers:
                    trackers[stable_id] = EquipmentTracker(stable_id, cls_name, fps)

                tracker   = trackers[stable_id]
                analytics = tracker.update(
                    arm_score, tracks_score, full_score, (x1+x2)/2, FRAME_SKIP)

                payload = {
                    "frame_id":        frame_id,
                    "equipment_id":    stable_id,
                    "equipment_class": cls_name,
                    "timestamp":       timestamp_str,
                    "confidence":      round(conf, 3),
                    "reidentified":    is_reid,
                    "bbox":            {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
                    "utilization": {
                        "current_state":    analytics["current_state"],
                        "current_activity": analytics["current_activity"],
                        "motion_source":    analytics["motion_source"],
                    },
                    "time_analytics": {
                        "total_tracked_seconds": round(tracker.total_tracked_sec, 2),
                        "total_active_seconds":  round(tracker.total_active_sec, 2),
                        "total_idle_seconds":    round(tracker.total_idle_sec, 2),
                        "utilization_percent":   tracker.utilization_pct,
                    },
                    "motion_scores": {
                        "full_body":     round(full_score, 1),
                        "arm_region":    round(arm_score, 1),
                        "tracks_region": round(tracks_score, 1),
                    },
                }

                producer.produce(KAFKA_TOPIC, key=stable_id,
                                 value=json.dumps(payload),
                                 callback=_delivery_report)

                detections_for_draw.append({
                    "equipment_id":    stable_id,
                    "bbox":            (x1, y1, x2, y2),
                    "state":           analytics["current_state"],
                    "activity":        analytics["current_activity"],
                    "utilization_pct": tracker.utilization_pct,
                    "idle_sec":        tracker.total_idle_sec,
                    "reidentified":    is_reid,
                })

                log.debug("[%d] %s%s -> %s / %s  util=%.1f%%  idle=%.1fs",
                          frame_id, stable_id, " (Re-ID)" if is_reid else "",
                          analytics["current_state"], analytics["current_activity"],
                          tracker.utilization_pct, tracker.total_idle_sec)

        gallery.tick_absent(active_bytetrack_ids, frame_id)
        if frame_id - last_save >= 500:
            gallery.save()
            last_save = frame_id

        producer.poll(0)
        annotated = draw_overlay(frame, detections_for_draw)
        cv2.imwrite(os.path.join(FRAME_OUT_DIR, "latest.jpg"), annotated,
                    [cv2.IMWRITE_JPEG_QUALITY, 80])

    cap.release()
    producer.flush(timeout=10)
    gallery.save()
    log.info("CV service finished. Total frames: %d", frame_id)


if __name__ == "__main__":
    main()
