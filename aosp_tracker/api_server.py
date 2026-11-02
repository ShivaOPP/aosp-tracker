"""Lightweight HTTP Status API Server for Multi-Server AOSP Tracking.

Provides a secure JSON endpoint for primary/peer servers to query system
resources, active ROM builds, active kernels, and source syncs.
"""

import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Callable, Dict, Any, Optional

logger = logging.getLogger("aosp_tracker.api")


class StatusRequestHandler(BaseHTTPRequestHandler):
    get_status_callback: Optional[Callable[[], Dict[str, Any]]] = None
    expected_secret: Optional[str] = None

    def log_message(self, format, *args):
        # Suppress routine HTTP logs unless debug is enabled
        logger.debug("%s - - [%s] %s", self.client_address[0], self.log_date_time_string(), format % args)

    def do_GET(self):
        # Check authentication if expected_secret is configured
        if self.expected_secret:
            req_secret = self.headers.get("X-AOSP-Secret")
            if not req_secret and "?secret=" in self.path:
                req_secret = self.path.split("?secret=", 1)[1].split("&", 1)[0]

            if req_secret != self.expected_secret:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "Unauthorized", "message": "Invalid or missing X-AOSP-Secret header."}')
                return

        path_clean = self.path.split("?")[0].rstrip("/")
        if path_clean in ["/status", "/api/status", ""]:
            if self.get_status_callback:
                try:
                    data = self.get_status_callback()
                    payload = json.dumps(data, indent=2).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                except Exception as e:
                    logger.exception("Error serializing status JSON: %s", e)
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error": "Internal server error"}')
                    return
            else:
                self.send_response(503)
                self.end_headers()
                return

        elif path_clean in ["/health", "/ping"]:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
            return

        else:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "Not found"}')


class StatusServer:
    """Threaded HTTP Status API server."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8088,
        get_status_callback: Optional[Callable[[], Dict[str, Any]]] = None,
        secret_key: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.secret_key = secret_key
        self.get_status_callback = get_status_callback
        self.httpd: Optional[HTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def start(self):
        """Start the HTTP API server in a background daemon thread."""
        callback = self.get_status_callback
        secret = self.secret_key

        class CustomHandler(StatusRequestHandler):
            get_status_callback = staticmethod(callback)
            expected_secret = secret

        try:
            self.httpd = HTTPServer((self.host, self.port), CustomHandler)
            self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
            self.thread.start()
            logger.info("HTTP Status API listening on http://%s:%d/status", self.host, self.port)
        except Exception as e:
            logger.warning("Could not start HTTP Status API on %s:%d: %s", self.host, self.port, e)

    def stop(self):
        """Shut down the HTTP API server cleanly."""
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None
