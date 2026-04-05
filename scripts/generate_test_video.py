"""
generate_test_video.py
======================
Generates a synthetic construction-site video for testing the pipeline
when real excavator footage is not available.

Simulates:
  - A stationary excavator (tracks fixed, arm moving) — tests arm_only detection
  - A moving dump truck — tests full_body detection
  - Idle periods — tests INACTIVE classification

Usage:
    pip install opencv-python-headless numpy
    python scripts/generate_test_video.py
    # Output: sample_data/video.mp4
"""

import cv2
import numpy as np
import math
import os

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "sample_data", "video.mp4")
os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)

W, H   = 1280, 720
FPS    = 25
DURATION_SEC = 60   # 1-minute synthetic video

fourcc = cv2.VideoWriter_fourcc(*"mp4v")
writer = cv2.VideoWriter(OUT_PATH, fourcc, FPS, (W, H))

total_frames = FPS * DURATION_SEC

def draw_excavator(frame, cx, cy, arm_angle_deg, color=(80, 120, 60)):
    """Draw a simplified excavator silhouette."""
    # Chassis
    cv2.rectangle(frame, (cx - 80, cy - 20), (cx + 80, cy + 20), color, -1)
    # Cab
    cv2.rectangle(frame, (cx + 10, cy - 50), (cx + 60, cy - 20), color, -1)
    # Tracks
    cv2.rectangle(frame, (cx - 85, cy + 20), (cx + 85, cy + 40), (40, 40, 40), -1)

    # Boom (fixed lower segment)
    boom_end_x = cx - 60
    boom_end_y = cy - 30
    cv2.line(frame, (cx - 20, cy - 20), (boom_end_x, boom_end_y), (100, 80, 50), 6)

    # Arm (articulated — rotates)
    arm_rad = math.radians(arm_angle_deg)
    arm_len = 80
    arm_end_x = int(boom_end_x + arm_len * math.cos(arm_rad))
    arm_end_y = int(boom_end_y + arm_len * math.sin(arm_rad))
    cv2.line(frame, (boom_end_x, boom_end_y), (arm_end_x, arm_end_y), (120, 100, 60), 5)

    # Bucket
    bucket_pts = np.array([
        [arm_end_x - 12, arm_end_y],
        [arm_end_x + 5,  arm_end_y - 15],
        [arm_end_x + 18, arm_end_y + 5],
        [arm_end_x + 5,  arm_end_y + 18],
    ], dtype=np.int32)
    cv2.fillPoly(frame, [bucket_pts], (90, 70, 40))


def draw_truck(frame, tx, ty, color=(50, 80, 160)):
    """Draw a simplified dump truck silhouette."""
    cv2.rectangle(frame, (tx, ty), (tx + 140, ty + 50),  color, -1)       # body
    cv2.rectangle(frame, (tx + 90, ty - 30), (tx + 140, ty), color, -1)   # cab
    cv2.circle(frame, (tx + 25,  ty + 55), 18, (30, 30, 30), -1)           # wheel L
    cv2.circle(frame, (tx + 115, ty + 55), 18, (30, 30, 30), -1)           # wheel R


print(f"Generating {DURATION_SEC}s test video → {OUT_PATH}")

for f in range(total_frames):
    t = f / FPS

    # Sky + ground
    frame = np.zeros((H, W, 3), dtype=np.uint8)
    frame[:] = (180, 160, 130)                       # dusty site background
    cv2.rectangle(frame, (0, H - 100), (W, H), (100, 90, 70), -1)  # ground

    # ── Excavator (stationary chassis, moving arm) ──────────
    # Arm oscillates: digging (45°→135°) then swinging (135°→45°)
    cycle = t % 8.0
    if cycle < 4.0:
        arm_angle = 45 + (cycle / 4.0) * 90      # digging sweep
    else:
        arm_angle = 135 - ((cycle - 4.0) / 4.0) * 90   # return sweep

    draw_excavator(frame, cx=400, cy=H - 180, arm_angle_deg=arm_angle)

    # ── Dump truck (moves laterally, pauses to receive load) ─
    # 0-15 s: approaches from right
    # 15-30 s: stationary under excavator boom
    # 30-45 s: drives away left
    # 45-60 s: idle far left
    if t < 15:
        tx = int(W - 300 - (t / 15.0) * 300)
    elif t < 30:
        tx = W - 600
    elif t < 45:
        tx = int(W - 600 - ((t - 30) / 15.0) * 400)
    else:
        tx = W - 1000
    draw_truck(frame, tx=max(20, tx), ty=H - 190)

    # ── Labels (for visual reference) ────────────────────────
    cv2.putText(frame, f"Frame {f:05d}  t={t:.1f}s", (20, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (220, 220, 220), 1)

    phase = "DIGGING" if cycle < 4 else "SWINGING"
    cv2.putText(frame, f"Excavator arm: {phase}  angle={arm_angle:.0f}deg",
                (20, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 255, 150), 1)

    writer.write(frame)

    if f % (FPS * 5) == 0:
        print(f"  {t:.0f}s / {DURATION_SEC}s")

writer.release()
print(f"Done. Video saved to: {OUT_PATH}")
