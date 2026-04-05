# EagleVision — Equipment Utilization & Activity Classification

Real-time computer vision pipeline for construction equipment monitoring.  
**Stack:** YOLOv8 → Apache Kafka → TimescaleDB → Grafana, fully containerised with Docker Compose.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  Video Source  (MP4 file / RTSP stream)                         │
└──────────────────────┬──────────────────────────────────────────┘
                       │
         ┌─────────────▼──────────────┐
         │     CV Microservice        │  cv_service/
         │  YOLOv8 detection          │
         │  ByteTrack multi-tracking  │
         │  Dense optical flow        │
         │  Region-based motion       │
         │  Activity classifier       │
         └──────┬──────────────┬──────┘
                │              │
        Kafka   │         annotated
        topic   │          frames
                │              │
   ┌────────────▼────┐   ┌─────▼──────────┐
   │  Apache Kafka   │   │  Video Server  │  video_server/
   │  equipment-     │   │  Flask MJPEG   │  :5000/stream
   │  events         │   └────────────────┘
   └────────┬────────┘
            │
   ┌────────▼──────────┐
   │  Kafka Consumer   │  kafka_consumer/
   │  Batch DB writer  │
   └────────┬──────────┘
            │
   ┌────────▼──────────┐
   │  TimescaleDB      │  :5432
   │  Hypertable       │
   │  Cont. aggregates │
   └────────┬──────────┘
            │
   ┌────────▼──────────┐
   │  Grafana          │  :3000
   │  Auto-provisioned │
   │  dashboard        │
   └───────────────────┘
```

### Services

| Service | Port | Description |
|---|---|---|
| `cv_service` | — | YOLOv8 inference + Kafka producer |
| `kafka` | 9092 | Message broker |
| `kafka-ui` | 8080 | Kafka topic browser |
| `timescaledb` | 5432 | Time-series database |
| `kafka_consumer` | — | DB writer |
| `video_server` | 5000 | MJPEG frame server |
| `grafana` | 3000 | Dashboard (admin/admin) |

---

## Quick Start

### Prerequisites

- Docker ≥ 24 and Docker Compose v2
- A video file of construction equipment

### 1. Clone and prepare

```bash
git clone <your-repo-url> eaglevision
cd eaglevision

# Place your video file here:
cp /path/to/your/excavator_video.mp4 sample_data/video.mp4
```

### 2. Spin up the stack

```bash
docker compose up --build -d
```

The first run downloads the YOLOv8 weights (~52 MB) inside the cv_service build.

### 3. Watch the logs

```bash
# CV service (detections + Kafka publishes)
docker compose logs -f cv_service

# Consumer (DB writes)
docker compose logs -f kafka_consumer
```

### 4. Open the dashboard

| URL | What you see |
|---|---|
| http://localhost:3000 | Grafana dashboard (admin / admin) |
| http://localhost:5000/stream | Raw MJPEG stream |
| http://localhost:8080 | Kafka UI — browse messages live |

---

## Environment Variables

### cv_service

| Variable | Default | Description |
|---|---|---|
| `VIDEO_SOURCE` | `/data/video.mp4` | Path or RTSP URL |
| `FRAME_SKIP` | `2` | Process every Nth frame (reduces CPU load) |
| `MOTION_THRESHOLD` | `500` | Full-body optical flow sum to flag ACTIVE |
| `ARM_MOTION_THRESHOLD` | `200` | Arm-region flow sum to flag arm activity |
| `YOLO_MODEL` | `yolov8m.pt` | Model size: n / s / m / l / x |

### kafka_consumer

| Variable | Default | Description |
|---|---|---|
| `BATCH_SIZE` | `50` | Max rows per DB insert batch |
| `FLUSH_INTERVAL_SEC` | `2.0` | Max seconds between flushes |

---

## Technical Design Decisions

### Articulated Motion — the core challenge

A standard approach (compare consecutive full-frame bounding boxes) fails for excavators:
the machine sits still while only the boom/arm/bucket moves. This would be classified as
INACTIVE even while actively digging.

**Solution — Region-Based Optical Flow:**

Each detected bounding box is split vertically into two regions:
- **Top 45 %** — boom, arm, and bucket assembly
- **Bottom 55 %** — undercarriage, tracks, and chassis

Dense Farneback optical flow is computed on consecutive greyscale frames, then the
flow magnitude is summed independently inside each region. This gives three signals:

```
full_body_score   = Σ |flow| over entire bbox
arm_score         = Σ |flow| over top-45% region
tracks_score      = Σ |flow| over bottom-55% region
```

Decision logic:
- `arm_score > ARM_THR` and `tracks_score < 0.5 × ARM_THR` → `motion_source = arm_only` → **ACTIVE**
- `tracks_score > ARM_THR` → `motion_source = tracks_only` → **ACTIVE** (repositioning)
- Both high → `motion_source = full_body` → **ACTIVE**
- Both low → `motion_source = none` → **INACTIVE**

The 45/55 split is tunable via code; a segmentation model (SAM, Mask R-CNN) could
provide pixel-accurate part boundaries at the cost of 3-5× higher inference time.

### Activity Classification

A sliding-window rule-based classifier over the last 8 frames:

| Condition | Activity |
|---|---|
| `avg_arm < threshold` and `avg_tracks < threshold` | WAITING |
| `avg_arm > threshold` and `std(bbox_centre_x) > 4px` | SWINGING (rotation proxy) |
| `avg_arm > threshold` and `avg_tracks < 0.5 × threshold` | DIGGING |
| `avg_tracks > threshold` | DUMPING (repositioning toward truck) |

**Trade-off:** Rule-based is interpretable and zero-shot. A production upgrade would be
a lightweight LSTM trained on labelled pose sequences from the target site — expected
accuracy jump from ~75 % to ~90 %+ on domain-specific footage.

### Kafka Payload

Matches the specification exactly:

```json
{
  "frame_id": 450,
  "equipment_id": "EX-001",
  "equipment_class": "truck",
  "timestamp": "00:00:15.000",
  "utilization": {
    "current_state": "ACTIVE",
    "current_activity": "DIGGING",
    "motion_source": "arm_only"
  },
  "time_analytics": {
    "total_tracked_seconds": 15.0,
    "total_active_seconds": 12.5,
    "total_idle_seconds": 2.5,
    "utilization_percent": 83.3
  }
}
```

### TimescaleDB vs plain PostgreSQL

TimescaleDB adds:
- Automatic time-based partitioning (`create_hypertable`) — keeps query performance flat as data grows
- Continuous aggregates (`utilization_1min`) — pre-computed 1-minute rollups for fast Grafana queries
- Native Grafana datasource plugin — SQL queries use `$__timeFilter(time)` macro

### Consumer batching

Raw Kafka → DB at frame rate would generate hundreds of tiny single-row inserts per second.
The consumer batches up to 50 rows and flushes every 2 seconds (whichever triggers first),
reducing DB round-trips by ~50× while keeping Grafana latency under 5 seconds.

---

## Using a Custom / Fine-Tuned Model

Replace `yolov8m.pt` with your own weights:

```bash
# In docker-compose.yml, cv_service section:
environment:
  YOLO_MODEL: /models/my_excavator_model.pt
volumes:
  - ./my_models:/models
```

Your model should output classes that can be mapped in `cv_service/main.py`
inside `TARGET_CLASSES`.

---

## Stopping the stack

```bash
docker compose down           # keep volumes (data survives)
docker compose down -v        # also wipe TimescaleDB + Grafana data
```

## Dataset

This project was tested with the **Mendeley earthmoving equipment dataset**:
> Roberts, D., & Golparvar-Fard, M. (2019). Data for: End-to-end visual detection,
> tracking and activity analysis of interacting earthmoving equipment.
> Mendeley Data. https://doi.org/10.17632/fyw6ps2d2j.1

Download the dataset and place a video in `sample_data/construction_video.mp4`
then update `VIDEO_PATH` in your `.env` file.

## Demo

[Watch the demo video](https://github.com/alzhraamostafa/eaglevision/raw/main/project_demo.mp4)

