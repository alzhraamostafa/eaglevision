-- ============================================================
-- EagleVision - TimescaleDB Schema
-- ============================================================

-- Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- ── Main events table ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS equipment_events (
    time                TIMESTAMPTZ     NOT NULL,
    frame_id            INTEGER         NOT NULL,
    equipment_id        TEXT            NOT NULL,
    equipment_class     TEXT            NOT NULL,

    -- Utilization state
    current_state       TEXT            NOT NULL,   -- ACTIVE | INACTIVE
    current_activity    TEXT            NOT NULL,   -- DIGGING | SWINGING | DUMPING | WAITING
    motion_source       TEXT            NOT NULL,   -- full_body | arm_only | tracks_only | none

    -- Bounding box (normalized 0-1)
    bbox_x1             FLOAT,
    bbox_y1             FLOAT,
    bbox_x2             FLOAT,
    bbox_y2             FLOAT,
    confidence          FLOAT,

    -- Time analytics (cumulative seconds at this frame)
    total_tracked_sec   FLOAT           NOT NULL DEFAULT 0,
    total_active_sec    FLOAT           NOT NULL DEFAULT 0,
    total_idle_sec      FLOAT           NOT NULL DEFAULT 0,
    utilization_pct     FLOAT           NOT NULL DEFAULT 0,

    -- Motion scores
    full_body_score     FLOAT           DEFAULT 0,
    arm_region_score    FLOAT           DEFAULT 0,
    tracks_region_score FLOAT           DEFAULT 0
);

-- Convert to hypertable (TimescaleDB time-series optimization)
SELECT create_hypertable('equipment_events', 'time', if_not_exists => TRUE);

-- ── Indexes ──────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_events_equipment_id
    ON equipment_events (equipment_id, time DESC);

CREATE INDEX IF NOT EXISTS idx_events_state
    ON equipment_events (current_state, time DESC);

-- ── Continuous aggregate: per-minute utilization summary ─────
CREATE MATERIALIZED VIEW IF NOT EXISTS utilization_1min
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 minute', time)   AS bucket,
    equipment_id,
    equipment_class,
    AVG(utilization_pct)            AS avg_utilization_pct,
    MAX(total_active_sec)           AS max_active_sec,
    MAX(total_idle_sec)             AS max_idle_sec,
    COUNT(*) FILTER (WHERE current_state = 'ACTIVE')   AS active_frames,
    COUNT(*) FILTER (WHERE current_state = 'INACTIVE') AS inactive_frames,
    COUNT(*)                        AS total_frames
FROM equipment_events
GROUP BY bucket, equipment_id, equipment_class
WITH NO DATA;

-- Refresh policy: keep last 24 hours up-to-date
SELECT add_continuous_aggregate_policy('utilization_1min',
    start_offset => INTERVAL '1 hour',
    end_offset   => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists => TRUE
);

-- ── Continuous aggregate: activity distribution ───────────────
CREATE MATERIALIZED VIEW IF NOT EXISTS activity_distribution_5min
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('5 minutes', time)  AS bucket,
    equipment_id,
    current_activity,
    COUNT(*)                        AS frame_count
FROM equipment_events
GROUP BY bucket, equipment_id, current_activity
WITH NO DATA;

SELECT add_continuous_aggregate_policy('activity_distribution_5min',
    start_offset => INTERVAL '1 hour',
    end_offset   => INTERVAL '5 minutes',
    schedule_interval => INTERVAL '5 minutes',
    if_not_exists => TRUE
);

-- ── Retention policy: keep raw data for 7 days ───────────────
SELECT add_retention_policy('equipment_events',
    INTERVAL '7 days',
    if_not_exists => TRUE
);
