"""
EagleVision - Kafka Consumer & TimescaleDB Writer
==================================================
Consumes equipment-events topic and persists each event
as a time-series row in TimescaleDB.

Design decisions:
  - Uses psycopg2 with a connection pool (thread-safe)
  - Batches DB inserts every 50 messages or 2 seconds (whichever first)
    to reduce write amplification while keeping latency low
  - Idempotent upsert: ON CONFLICT DO NOTHING (frame_id + equipment_id)
  - Commits Kafka offsets only after successful DB flush
"""

import os
import json
import time
import logging
from datetime import datetime, timezone
from typing import Optional

import psycopg2
from psycopg2 import pool as pg_pool
from confluent_kafka import Consumer, KafkaException, KafkaError

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Consumer] %(levelname)s  %(message)s",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KAFKA_TOPIC     = os.getenv("KAFKA_TOPIC", "equipment-events")
KAFKA_GROUP     = os.getenv("KAFKA_GROUP_ID", "eaglevision-consumer-group")

DB_HOST     = os.getenv("DB_HOST", "localhost")
DB_PORT     = int(os.getenv("DB_PORT", "5432"))
DB_NAME     = os.getenv("DB_NAME", "eaglevision")
DB_USER     = os.getenv("DB_USER", "eaglevision")
DB_PASSWORD = os.getenv("DB_PASSWORD", "eaglevision123")

BATCH_SIZE     = int(os.getenv("BATCH_SIZE", "50"))
FLUSH_INTERVAL = float(os.getenv("FLUSH_INTERVAL_SEC", "2.0"))

# ── Insert SQL ────────────────────────────────────────────────────────────────
INSERT_SQL = """
INSERT INTO equipment_events (
    time,
    frame_id,
    equipment_id,
    equipment_class,
    current_state,
    current_activity,
    motion_source,
    bbox_x1, bbox_y1, bbox_x2, bbox_y2,
    confidence,
    total_tracked_sec,
    total_active_sec,
    total_idle_sec,
    utilization_pct,
    full_body_score,
    arm_region_score,
    tracks_region_score
) VALUES (
    %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s,
    %s, %s, %s, %s,
    %s, %s, %s
)
ON CONFLICT DO NOTHING;
"""


def parse_event(raw: bytes) -> Optional[tuple]:
    """Parse a raw Kafka message bytes into a DB row tuple."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("Bad JSON: %s", exc)
        return None

    try:
        bbox = msg.get("bbox", {})
        util = msg.get("utilization", {})
        ta   = msg.get("time_analytics", {})
        ms   = msg.get("motion_scores", {})

        # Use wall-clock time (consumer side) for the hypertable time column
        now = datetime.now(timezone.utc)

        return (
            now,
            msg["frame_id"],
            msg["equipment_id"],
            msg["equipment_class"],
            util["current_state"],
            util["current_activity"],
            util["motion_source"],
            bbox.get("x1"), bbox.get("y1"), bbox.get("x2"), bbox.get("y2"),
            msg.get("confidence"),
            ta.get("total_tracked_seconds", 0),
            ta.get("total_active_seconds",  0),
            ta.get("total_idle_seconds",    0),
            ta.get("utilization_percent",   0),
            ms.get("full_body",     0),
            ms.get("arm_region",    0),
            ms.get("tracks_region", 0),
        )
    except KeyError as exc:
        log.warning("Missing field in event: %s | msg=%s", exc, msg)
        return None


def flush_batch(conn_pool, batch: list[tuple]) -> bool:
    """Write a batch of rows to TimescaleDB. Returns True on success."""
    if not batch:
        return True
    conn = None
    try:
        conn = conn_pool.getconn()
        with conn.cursor() as cur:
            cur.executemany(INSERT_SQL, batch)
        conn.commit()
        log.info("Flushed %d rows to TimescaleDB", len(batch))
        return True
    except Exception as exc:
        log.error("DB flush error: %s", exc)
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            conn_pool.putconn(conn)


def wait_for_db(conn_pool, retries=20, delay=3.0):
    """Block until DB is reachable."""
    for attempt in range(retries):
        conn = None
        try:
            conn = conn_pool.getconn()
            conn_pool.putconn(conn)
            log.info("TimescaleDB connection OK")
            return
        except Exception as exc:
            log.warning("DB not ready (%d/%d): %s", attempt + 1, retries, exc)
            time.sleep(delay)
    raise RuntimeError("Could not connect to TimescaleDB after retries")


def main():
    log.info("Starting EagleVision Kafka Consumer")
    log.info("  Broker: %s  Topic: %s  Group: %s", KAFKA_BOOTSTRAP, KAFKA_TOPIC, KAFKA_GROUP)
    log.info("  DB: %s:%d/%s", DB_HOST, DB_PORT, DB_NAME)

    # ── DB connection pool ────────────────────────────────────
    conn_pool = pg_pool.ThreadedConnectionPool(
        minconn=1, maxconn=4,
        host=DB_HOST, port=DB_PORT,
        dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD,
    )
    wait_for_db(conn_pool)

    # ── Kafka consumer ────────────────────────────────────────
    consumer = Consumer({
        "bootstrap.servers":  KAFKA_BOOTSTRAP,
        "group.id":           KAFKA_GROUP,
        "auto.offset.reset":  "latest",
        "enable.auto.commit": False,   # manual commit after DB flush
    })
    consumer.subscribe([KAFKA_TOPIC])
    log.info("Subscribed to topic: %s", KAFKA_TOPIC)

    batch: list[tuple] = []
    last_flush = time.monotonic()

    try:
        while True:
            msg = consumer.poll(timeout=0.5)

            if msg is None:
                pass  # timeout — check flush interval below
            elif msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    log.debug("End of partition")
                elif msg.error().code() == KafkaError.UNKNOWN_TOPIC_OR_PART:
                    log.warning("Topic not ready yet, waiting...")
                    time.sleep(3)
                else:
                    raise KafkaException(msg.error())
            else:
                row = parse_event(msg.value())
                if row:
                    batch.append(row)
                    log.debug(
                        "Queued  %s → %s / %s",
                        row[2], row[4], row[5],   # eid, state, activity
                    )

            # ── Flush if batch is full or interval elapsed ────
            elapsed = time.monotonic() - last_flush
            if len(batch) >= BATCH_SIZE or (batch and elapsed >= FLUSH_INTERVAL):
                if flush_batch(conn_pool, batch):
                    consumer.commit(asynchronous=False)
                    batch.clear()
                    last_flush = time.monotonic()
                else:
                    log.warning("Flush failed — will retry on next cycle")
                    time.sleep(1.0)

    except KeyboardInterrupt:
        log.info("Shutting down consumer")
    finally:
        # Flush remaining
        flush_batch(conn_pool, batch)
        consumer.close()
        conn_pool.closeall()


if __name__ == "__main__":
    main()
