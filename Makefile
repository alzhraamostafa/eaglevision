# ============================================================
# EagleVision — Makefile
# ============================================================
.PHONY: help up down logs build clean test-video validate-kafka \
        shell-cv shell-db psql reset-db status

# Default target
help:
	@echo ""
	@echo "  EagleVision — Equipment Utilization Pipeline"
	@echo ""
	@echo "  make up             Build and start all services"
	@echo "  make down           Stop all services (keep data)"
	@echo "  make down-clean     Stop all services + wipe volumes"
	@echo "  make build          Rebuild images without cache"
	@echo "  make logs           Tail logs from all services"
	@echo "  make logs-cv        Tail CV service logs only"
	@echo "  make logs-consumer  Tail consumer logs only"
	@echo "  make status         Show running containers"
	@echo ""
	@echo "  make test-video     Generate a synthetic test video"
	@echo "  make validate-kafka Consume 5 Kafka messages and print them"
	@echo "  make psql           Open psql shell in TimescaleDB"
	@echo "  make shell-cv       Bash shell inside cv_service container"
	@echo "  make reset-db       Drop and recreate the database schema"
	@echo ""

# ── Lifecycle ────────────────────────────────────────────────
up:
	@cp -n .env.example .env 2>/dev/null || true
	docker compose up --build -d
	@echo ""
	@echo "  Grafana  →  http://localhost:3000  (admin / admin)"
	@echo "  Kafka UI →  http://localhost:8080"
	@echo "  Video    →  http://localhost:5000/stream"
	@echo ""

down:
	docker compose down

down-clean:
	docker compose down -v
	@echo "All volumes wiped."

build:
	docker compose build --no-cache

# ── Logs ─────────────────────────────────────────────────────
logs:
	docker compose logs -f --tail=50

logs-cv:
	docker compose logs -f --tail=50 cv_service

logs-consumer:
	docker compose logs -f --tail=50 kafka_consumer

status:
	docker compose ps

# ── Dev helpers ───────────────────────────────────────────────
test-video:
	@echo "Generating synthetic test video in sample_data/video.mp4 ..."
	pip install opencv-python-headless numpy --quiet
	python scripts/generate_test_video.py

validate-kafka:
	@echo "Consuming 5 messages from equipment-events ..."
	docker compose exec kafka \
		kafka-console-consumer \
		--bootstrap-server localhost:29092 \
		--topic equipment-events \
		--max-messages 5 \
		--from-beginning \
		| python3 -m json.tool

psql:
	@export $$(grep -v '^#' .env | xargs) && \
	docker compose exec timescaledb \
		psql -U $${DB_USER:-eaglevision} -d $${DB_NAME:-eaglevision}

shell-cv:
	docker compose exec cv_service bash

# ── Database ─────────────────────────────────────────────────
reset-db:
	@echo "WARNING: This will drop all equipment_events data."
	@read -p "Are you sure? [y/N] " confirm; \
		[ "$$confirm" = "y" ] || exit 1
	docker compose exec timescaledb \
		psql -U eaglevision -d eaglevision \
		-c "DROP TABLE IF EXISTS equipment_events CASCADE;" \
		-c "DROP MATERIALIZED VIEW IF EXISTS utilization_1min CASCADE;" \
		-c "DROP MATERIALIZED VIEW IF EXISTS activity_distribution_5min CASCADE;"
	docker compose restart kafka_consumer
	@echo "Schema reset. Consumer will recreate tables on next start."

# ── Convenience query shortcuts ───────────────────────────────
.PHONY: query-latest query-utilization

query-latest:
	docker compose exec timescaledb psql -U eaglevision -d eaglevision -c \
		"SELECT time, equipment_id, current_state, current_activity, \
		        ROUND(utilization_pct::numeric,1) AS util_pct \
		 FROM equipment_events ORDER BY time DESC LIMIT 20;"

query-utilization:
	@export $$(grep -v '^#' .env | xargs) && \
	docker compose exec timescaledb psql -U $${DB_USER:-eaglevision} -d $${DB_NAME:-eaglevision} -c \
		"SELECT equipment_id, \
		        ROUND(MAX(total_active_sec)::numeric, 1) AS active_sec, \
		        ROUND(MAX(total_idle_sec)::numeric,  1) AS idle_sec, \
		        ROUND(MAX(utilization_pct)::numeric,  1) AS util_pct \
		 FROM equipment_events \
		 GROUP BY equipment_id \
		 ORDER BY util_pct DESC;"
