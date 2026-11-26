"""Vercel serverless function: GET /api/data -> dashboard JSON, read live from Gmail.

Environment variables (Vercel -> Project -> Settings -> Environment Variables):
  GMAIL_USER, GMAIL_APP_PASSWORD   the inbox the Moodle Robot mails its reports to
  DASHBOARD_KEY                    shared secret; without it the function refuses to serve
  MOODLE_DASH_NAME                 optional first name for the greeting
  SHOW_STUDENT_NAMES=1             optional; by default other students' names/posts are removed
"""
import hmac
import json
import os
import pathlib
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dashboard"))
import sync  # noqa: E402

CACHE_DIR = pathlib.Path("/tmp/moodle-robot-cache")
TTL_SECONDS = int(os.environ.get("DASHBOARD_TTL", "600"))
_lock = threading.Lock()
_memo = {"at": 0.0, "data": None}


def load_data() -> dict:
    with _lock:
        if _memo["data"] is not None and time.time() - _memo["at"] < TTL_SECONDS:
            return _memo["data"]
        data = sync.build_from_gmail(
            CACHE_DIR,
            days=int(os.environ.get("DASHBOARD_DAYS", "7")),
            name=os.environ.get("MOODLE_DASH_NAME") or None,
            tz=os.environ.get("DASHBOARD_TZ", "Europe/Vienna"),
            robot_query=os.environ.get("ROBOT_QUERY", sync.DEFAULT_ROBOT_QUERY),
            inbox_query=os.environ.get("INBOX_QUERY", sync.DEFAULT_INBOX_QUERY),
        )
        if os.environ.get("SHOW_STUDENT_NAMES") != "1":
            sync.redact_students(data)
        _memo.update(at=time.time(), data=data)
        return data


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        key = os.environ.get("DASHBOARD_KEY", "")
        if not key:
            return self._send(503, {"error": "not-configured",
                                    "message": "Set DASHBOARD_KEY (and the Gmail variables) in Vercel, then redeploy."})
        supplied = self.headers.get("x-dashboard-key") or parse_qs(urlparse(self.path).query).get("key", [""])[0]
        if not hmac.compare_digest(supplied.encode(), key.encode()):
            return self._send(401, {"error": "unauthorized", "message": "Wrong or missing access key."})
        try:
            data = load_data()
        except SystemExit as exc:  # missing Gmail settings, no reports found
            return self._send(500, {"error": "sync-failed", "message": str(exc)})
        except Exception as exc:  # IMAP login refused, network, etc.
            return self._send(502, {"error": "sync-failed", "message": f"{type(exc).__name__}: {exc}"})
        return self._send(200, data)

    def _send(self, status: int, body: dict):
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        # Personal data: never let a shared cache keep it.
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Robots-Tag", "noindex")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
