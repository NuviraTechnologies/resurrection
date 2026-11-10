#!/usr/bin/env python3
"""Resurrection — AI subscription recovery agent (PayPal AI Hackathon).

Run:  python3 app.py            (PORT env optional, default 8080)
Mode: SIM when PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are unset,
      LIVE (PayPal sandbox REST) when they are set. See README.md.
Zero dependencies — Python standard library only.

Hosting: when PORT is set (e.g. Render) the server binds 0.0.0.0:$PORT;
for local runs without PORT it binds 127.0.0.1 only. HOST overrides both.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import recovery
from paypal_client import PayPalError

STATIC = Path(__file__).parent / "static"

ACTIONS = {
    "/api/seed": recovery.seed,
    "/api/diagnose": recovery.run_diagnosis,
    "/api/invoice": recovery.send_recovery_invoice,
    "/api/pay": recovery.member_pays,
    "/api/recover": recovery.recover,
}

# Recovery steps mutate one shared state machine. Serialize them: a
# double-click (or two tabs) gets a friendly "already running" response
# instead of racing a second capture.
_action_lock = threading.Lock()


def friendly_paypal_message(body):
    """Pull a short, human-readable message out of a PayPal error body.
    Never returns the raw body — no internals reach the browser."""
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return ""
    if isinstance(data, dict):
        return (data.get("message") or data.get("error_description")
                or data.get("name") or "")
    return ""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            body = (STATIC / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/health":
            self._json({"status": "ok",
                        "mode": "SIM" if recovery.SIM_MODE else "LIVE"})
        elif path == "/api/state":
            self._json(recovery.state())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlsplit(self.path).path
        fn = ACTIONS.get(path)
        if not fn:
            self._json({"error": "not found"}, 404)
            return
        # The API takes no request bodies; drain whatever arrived so the
        # connection stays clean, and never let a bad header kill us.
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if 0 < length <= 1_000_000:
            self.rfile.read(length)
        if not _action_lock.acquire(blocking=False):
            self._json({"error": "A recovery step is already running — "
                                 "give it a second and watch the event log.",
                        **recovery.state()}, 409)
            return
        try:
            fn()
            self._json(recovery.state())
        except recovery.StageError as e:
            self._json({"error": str(e), **recovery.state()}, 400)
        except PayPalError as e:
            if e.status:
                msg = friendly_paypal_message(e.body)
                text = f"PayPal API returned {e.status}"
                if msg:
                    text += f": {msg}"
            else:  # status 0 = network-level failure, no response at all
                text = ("Could not reach the PayPal sandbox. Check the "
                        "network connection and that PAYPAL_CLIENT_ID / "
                        "PAYPAL_CLIENT_SECRET are the sandbox app's "
                        "credentials, then try again.")
            self._json({"error": text, **recovery.state()}, 502)
        except Exception:
            # Never dump internals or stack traces to the browser.
            self._json({"error": "Something went wrong inside Resurrection "
                                 "on that step. The event log shows how far "
                                 "the recovery got; you can safely retry.",
                        **recovery.state()}, 500)
        finally:
            _action_lock.release()


if __name__ == "__main__":
    try:
        port = int(os.environ.get("PORT", "8080"))
    except ValueError:
        raise SystemExit("PORT must be a number, e.g. PORT=8080")
    host = os.environ.get("HOST") or (
        "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    mode = "SIM" if recovery.SIM_MODE else "LIVE (PayPal sandbox)"
    print(f"Resurrection running at http://{host}:{port}  [{mode} mode]")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
