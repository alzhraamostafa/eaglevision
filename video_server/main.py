"""
EagleVision - Video Server
==========================
Serves the latest annotated frame as:
  GET /frame        → single JPEG (for Grafana Image Renderer)
  GET /stream       → MJPEG stream  (for browser / Grafana iframe panel)
  GET /health       → {"status": "ok"}

The CV service writes /frames/latest.jpg continuously.
This server reads and re-serves it, adding CORS headers so
Grafana can embed it in an HTML panel.
"""

import os
import time
import logging
from pathlib import Path

from flask import Flask, Response, jsonify
from flask_cors import CORS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [VideoServer] %(levelname)s  %(message)s",
)
log = logging.getLogger(__name__)

FRAME_DIR  = os.getenv("FRAME_DIR", "/frames")
FRAME_PATH = Path(FRAME_DIR) / "latest.jpg"
STREAM_FPS = float(os.getenv("STREAM_FPS", "15"))

app = Flask(__name__)
CORS(app)   # Allow Grafana (different port) to load the stream


def _read_frame() -> bytes | None:
    """Return raw JPEG bytes of the latest annotated frame, or None."""
    try:
        if FRAME_PATH.exists():
            return FRAME_PATH.read_bytes()
    except OSError:
        pass
    return None


def _placeholder_jpeg() -> bytes:
    """Return a minimal 1x1 grey JPEG when no frame is available yet."""
    # Tiny valid JPEG (grey 1×1 pixel) — avoids external dependencies
    return (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
        b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
        b"\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\x1e"
        b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
        b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00"
        b"\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b"
        b"\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05"
        b"\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06"
        b"\x13Qa\x07\"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br"
        b"\x82\t\n\x16\x17\x18\x19\x1a%&'()*456789:CDEFGHIJSTUVWXYZ"
        b"cdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94"
        b"\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa"
        b"\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7"
        b"\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3"
        b"\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8"
        b"\xf9\xfa\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xfb\xd4P\x00\x00"
        b"\x00\x1f\xff\xd9"
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok", "frame_exists": FRAME_PATH.exists()})


@app.route("/frame")
def single_frame():
    """Return the latest JPEG frame."""
    data = _read_frame() or _placeholder_jpeg()
    return Response(data, mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.route("/stream")
def mjpeg_stream():
    """MJPEG stream — embed via <img src="http://localhost:5000/stream">."""
    interval = 1.0 / STREAM_FPS

    def generate():
        while True:
            data = _read_frame() or _placeholder_jpeg()
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n"
                + data +
                b"\r\n"
            )
            time.sleep(interval)

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store"},
    )


if __name__ == "__main__":
    log.info("EagleVision video server starting on :5000")
    log.info("Frame path: %s", FRAME_PATH)
    app.run(host="0.0.0.0", port=5000, threaded=True)
