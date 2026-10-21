"""
Keep-alive HTTP server (Flask) - Render / Docker ke liye.
Default port 8000 (config.PORT / ENV PORT).
"""

import threading
from flask import Flask, Response, jsonify

from config import PORT

app = Flask(__name__)


@app.route("/", methods=["GET"])
def health():
    return Response("Audio Editor Bot is running ✅", status=200)


@app.route("/health", methods=["GET"])
def health_json():
    return jsonify(status="ok")


def _run():
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


def keep_alive():
    t = threading.Thread(target=_run, daemon=True)
    t.start()
