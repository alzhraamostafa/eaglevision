"""
tests/test_payload.py
=====================
Validates that CV service Kafka payloads match the specification schema.
Runs without Docker — just import and call the functions directly.

Usage:
    pip install pytest
    pytest tests/test_payload.py -v
"""

import json
import sys
import os

# Allow running from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── Reference payload from the spec ─────────────────────────────────────────
SPEC_PAYLOAD = {
    "frame_id": 450,
    "equipment_id": "EX-001",
    "equipment_class": "excavator",
    "timestamp": "00:00:15.000",
    "utilization": {
        "current_state": "ACTIVE",
        "current_activity": "DIGGING",
        "motion_source": "arm_only",
    },
    "time_analytics": {
        "total_tracked_seconds": 15.0,
        "total_active_seconds": 12.5,
        "total_idle_seconds": 2.5,
        "utilization_percent": 83.3,
    },
}


# ── Schema validation helpers ────────────────────────────────────────────────
VALID_STATES      = {"ACTIVE", "INACTIVE"}
VALID_ACTIVITIES  = {"DIGGING", "SWINGING", "DUMPING", "WAITING"}
VALID_MOTION_SRC  = {"arm_only", "tracks_only", "full_body", "none"}


def validate_payload(payload: dict) -> list[str]:
    """
    Validate a payload dict against the spec schema.
    Returns a list of error strings (empty = valid).
    """
    errors = []

    # Top-level required fields
    for field in ("frame_id", "equipment_id", "equipment_class", "timestamp",
                  "utilization", "time_analytics"):
        if field not in payload:
            errors.append(f"Missing top-level field: '{field}'")

    # frame_id must be a non-negative integer
    if "frame_id" in payload:
        if not isinstance(payload["frame_id"], int) or payload["frame_id"] < 0:
            errors.append("frame_id must be a non-negative integer")

    # utilization block
    util = payload.get("utilization", {})
    for field in ("current_state", "current_activity", "motion_source"):
        if field not in util:
            errors.append(f"utilization missing field: '{field}'")

    if util.get("current_state") not in VALID_STATES:
        errors.append(
            f"utilization.current_state must be one of {VALID_STATES}, "
            f"got: {util.get('current_state')!r}"
        )

    if util.get("current_activity") not in VALID_ACTIVITIES:
        errors.append(
            f"utilization.current_activity must be one of {VALID_ACTIVITIES}, "
            f"got: {util.get('current_activity')!r}"
        )

    if util.get("motion_source") not in VALID_MOTION_SRC:
        errors.append(
            f"utilization.motion_source must be one of {VALID_MOTION_SRC}, "
            f"got: {util.get('motion_source')!r}"
        )

    # time_analytics block
    ta = payload.get("time_analytics", {})
    for field in ("total_tracked_seconds", "total_active_seconds",
                  "total_idle_seconds", "utilization_percent"):
        if field not in ta:
            errors.append(f"time_analytics missing field: '{field}'")
        elif not isinstance(ta[field], (int, float)):
            errors.append(f"time_analytics.{field} must be numeric")
        elif ta[field] < 0:
            errors.append(f"time_analytics.{field} must be >= 0")

    # utilization_percent range
    util_pct = ta.get("utilization_percent", 0)
    if not (0 <= util_pct <= 100):
        errors.append(
            f"time_analytics.utilization_percent must be in [0, 100], got {util_pct}"
        )

    # Time consistency: active + idle should approximately equal tracked
    tracked = ta.get("total_tracked_seconds", 0)
    active  = ta.get("total_active_seconds",  0)
    idle    = ta.get("total_idle_seconds",     0)
    if abs((active + idle) - tracked) > 1.0:   # 1s tolerance for rounding
        errors.append(
            f"time_analytics inconsistency: active({active}) + idle({idle}) "
            f"!= tracked({tracked})"
        )

    return errors


# ── pytest tests ─────────────────────────────────────────────────────────────
def test_spec_payload_is_valid():
    """The reference payload from the spec must pass validation."""
    errors = validate_payload(SPEC_PAYLOAD)
    assert errors == [], f"Spec payload failed validation: {errors}"


def test_active_digging_payload():
    payload = {
        "frame_id": 100,
        "equipment_id": "EX-001",
        "equipment_class": "excavator",
        "timestamp": "00:00:04.000",
        "utilization": {
            "current_state":    "ACTIVE",
            "current_activity": "DIGGING",
            "motion_source":    "arm_only",
        },
        "time_analytics": {
            "total_tracked_seconds": 4.0,
            "total_active_seconds":  4.0,
            "total_idle_seconds":    0.0,
            "utilization_percent":   100.0,
        },
    }
    assert validate_payload(payload) == []


def test_inactive_waiting_payload():
    payload = {
        "frame_id": 200,
        "equipment_id": "DT-002",
        "equipment_class": "truck",
        "timestamp": "00:00:08.000",
        "utilization": {
            "current_state":    "INACTIVE",
            "current_activity": "WAITING",
            "motion_source":    "none",
        },
        "time_analytics": {
            "total_tracked_seconds": 8.0,
            "total_active_seconds":  3.0,
            "total_idle_seconds":    5.0,
            "utilization_percent":   37.5,
        },
    }
    assert validate_payload(payload) == []


def test_invalid_state_rejected():
    payload = dict(SPEC_PAYLOAD)
    payload["utilization"] = dict(SPEC_PAYLOAD["utilization"])
    payload["utilization"]["current_state"] = "RUNNING"  # invalid
    errors = validate_payload(payload)
    assert any("current_state" in e for e in errors)


def test_invalid_activity_rejected():
    payload = dict(SPEC_PAYLOAD)
    payload["utilization"] = dict(SPEC_PAYLOAD["utilization"])
    payload["utilization"]["current_activity"] = "SLEEPING"  # invalid
    errors = validate_payload(payload)
    assert any("current_activity" in e for e in errors)


def test_utilization_percent_over_100_rejected():
    payload = dict(SPEC_PAYLOAD)
    payload["time_analytics"] = dict(SPEC_PAYLOAD["time_analytics"])
    payload["time_analytics"]["utilization_percent"] = 110.0  # invalid
    errors = validate_payload(payload)
    assert any("utilization_percent" in e for e in errors)


def test_missing_field_rejected():
    payload = dict(SPEC_PAYLOAD)
    del payload["equipment_id"]
    errors = validate_payload(payload)
    assert any("equipment_id" in e for e in errors)


def test_time_consistency_check():
    payload = dict(SPEC_PAYLOAD)
    payload["time_analytics"] = {
        "total_tracked_seconds": 10.0,
        "total_active_seconds":  9.0,
        "total_idle_seconds":    5.0,   # 9+5=14 != 10 → inconsistent
        "utilization_percent":   90.0,
    }
    errors = validate_payload(payload)
    assert any("inconsistency" in e for e in errors)


def test_json_serialization_round_trip():
    """Payload must survive JSON encode → decode without data loss."""
    raw   = json.dumps(SPEC_PAYLOAD)
    back  = json.loads(raw)
    assert back == SPEC_PAYLOAD


# ── CLI runner ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Validating spec payload ...")
    errors = validate_payload(SPEC_PAYLOAD)
    if errors:
        print("FAIL:")
        for e in errors:
            print(f"  ✗  {e}")
        sys.exit(1)
    else:
        print("  ✓  Spec payload is valid")
        print("\nRun all tests with:  pytest tests/test_payload.py -v")
