"""
tests/test_integration.py
=========================
Smoke tests that verify the live Docker Compose stack is healthy.

Prerequisites:
    docker compose up -d
    pip install pytest requests psycopg2-binary confluent-kafka

Usage:
    pytest tests/test_integration.py -v --timeout=60
"""

import json
import time
import pytest
import requests
import psycopg2
from confluent_kafka import Consumer, KafkaError


# ── Connection settings (match docker-compose defaults) ──────────────────────
VIDEO_URL  = "http://localhost:5000"
GRAFANA_URL = "http://localhost:3000"
DB_DSN = "host=localhost port=5432 dbname=eaglevision user=eaglevision password=eaglevision123"
KAFKA_BOOTSTRAP = "localhost:9092"
KAFKA_TOPIC     = "equipment-events"


# ── Video server tests ────────────────────────────────────────────────────────
class TestVideoServer:
    def test_health_endpoint(self):
        resp = requests.get(f"{VIDEO_URL}/health", timeout=5)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"

    def test_frame_endpoint_returns_jpeg(self):
        resp = requests.get(f"{VIDEO_URL}/frame", timeout=5)
        assert resp.status_code == 200
        assert resp.headers["Content-Type"] == "image/jpeg"
        assert len(resp.content) > 100   # not empty

    def test_stream_endpoint_returns_mjpeg(self):
        # Open stream and read just the first boundary
        resp = requests.get(f"{VIDEO_URL}/stream", stream=True, timeout=5)
        assert resp.status_code == 200
        assert "multipart/x-mixed-replace" in resp.headers["Content-Type"]
        # Read first 500 bytes — should contain the MIME boundary
        chunk = next(resp.iter_content(500))
        resp.close()
        assert b"--frame" in chunk


# ── Grafana tests ─────────────────────────────────────────────────────────────
class TestGrafana:
    def test_grafana_api_health(self):
        resp = requests.get(
            f"{GRAFANA_URL}/api/health",
            auth=("admin", "admin"),
            timeout=10,
        )
        assert resp.status_code == 200
        assert resp.json().get("database") == "ok"

    def test_datasource_provisioned(self):
        resp = requests.get(
            f"{GRAFANA_URL}/api/datasources",
            auth=("admin", "admin"),
            timeout=10,
        )
        assert resp.status_code == 200
        ds_names = [ds["name"] for ds in resp.json()]
        assert "TimescaleDB" in ds_names

    def test_dashboard_provisioned(self):
        resp = requests.get(
            f"{GRAFANA_URL}/api/search?type=dash-db",
            auth=("admin", "admin"),
            timeout=10,
        )
        assert resp.status_code == 200
        titles = [d["title"] for d in resp.json()]
        assert any("EagleVision" in t for t in titles)


# ── Database tests ────────────────────────────────────────────────────────────
class TestDatabase:
    @pytest.fixture(scope="class")
    def conn(self):
        conn = psycopg2.connect(DB_DSN)
        yield conn
        conn.close()

    def test_equipment_events_table_exists(self, conn):
        with conn.cursor() as cur:
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'equipment_events'
                )
            """)
            assert cur.fetchone()[0] is True

    def test_hypertable_registered(self, conn):
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM timescaledb_information.hypertables
                WHERE hypertable_name = 'equipment_events'
            """)
            assert cur.fetchone()[0] == 1

    def test_continuous_aggregates_exist(self, conn):
        with conn.cursor() as cur:
            cur.execute("""
                SELECT view_name FROM timescaledb_information.continuous_aggregates
            """)
            views = {row[0] for row in cur.fetchall()}
        assert "utilization_1min" in views


# ── Kafka payload format test (live consumer) ─────────────────────────────────
class TestKafkaPayload:
    def test_payload_format_matches_spec(self):
        """
        Wait up to 20 seconds for at least one message on the topic,
        then validate its schema against the spec.
        """
        consumer = Consumer({
            "bootstrap.servers": KAFKA_BOOTSTRAP,
            "group.id":          "pytest-validator",
            "auto.offset.reset": "latest",
        })
        consumer.subscribe([KAFKA_TOPIC])

        msg = None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            m = consumer.poll(1.0)
            if m and not m.error():
                msg = m
                break

        consumer.close()

        if msg is None:
            pytest.skip(
                "No Kafka messages received in 20 s "
                "(is cv_service running and processing video?)"
            )

        payload = json.loads(msg.value())

        # Required top-level fields
        for field in ("frame_id", "equipment_id", "equipment_class",
                      "timestamp", "utilization", "time_analytics"):
            assert field in payload, f"Missing field: {field}"

        util = payload["utilization"]
        assert util["current_state"]    in {"ACTIVE", "INACTIVE"}
        assert util["current_activity"] in {"DIGGING", "SWINGING", "DUMPING", "WAITING"}
        assert util["motion_source"]    in {"arm_only", "tracks_only", "full_body", "none"}

        ta = payload["time_analytics"]
        assert ta["utilization_percent"] >= 0
        assert ta["utilization_percent"] <= 100
        assert ta["total_active_seconds"] + ta["total_idle_seconds"] <= \
               ta["total_tracked_seconds"] + 1.0   # 1 s tolerance
