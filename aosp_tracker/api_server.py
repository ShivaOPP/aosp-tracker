"""Lightweight HTTP Status and Task Delegation API Server for Multi-Server AOSP Tracking.

Provides secure JSON endpoints for querying system resources, listing local ROMs,
and delegating GoFile uploads across worker nodes.
"""

import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Callable, Dict, Any, Optional, List

logger = logging.getLogger("aosp_tracker.api")


class StatusRequestHandler(BaseHTTPRequestHandler):
    get_status_callback: Optional[Callable[[], Dict[str, Any]]] = None
    get_roms_callback: Optional[Callable[[], List[Dict[str, Any]]]] = None
    start_upload_callback: Optional[Callable[[str, str, Optional[str]], Dict[str, Any]]] = None
    cancel_upload_callback: Optional[Callable[[str], Dict[str, Any]]] = None
    expected_secret: Optional[str] = None

    def log_message(self, format, *args):
        # Suppress routine HTTP logs unless debug is enabled
        logger.debug("%s - - [%s] %s", self.client_address[0], self.log_date_time_string(), format % args)

    def send_json_response(self, data: Any, status_code: int = 200):
        try:
            payload = json.dumps(data, indent=2).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception:
            pass

    def check_auth(self) -> bool:
        if self.expected_secret:
            req_secret = self.headers.get("X-AOSP-Secret")
            if not req_secret and "?secret=" in self.path:
                req_secret = self.path.split("?secret=", 1)[1].split("&", 1)[0]
            if req_secret != self.expected_secret:
                self.send_json_response(
                    {"error": "Unauthorized", "message": "Invalid or missing X-AOSP-Secret header."},
                    401,
                )
                return False
        return True

    def do_GET(self):
        if not self.check_auth():
            return

        path_clean = self.path.split("?")[0].rstrip("/")
        if path_clean in ["/status", "/api/status", ""]:
            if self.get_status_callback:
                try:
                    data = self.get_status_callback()
                    self.send_json_response(data, 200)
                except Exception as e:
                    logger.exception("Error serializing status JSON: %s", e)
                    self.send_json_response({"error": "Internal server error"}, 500)
            else:
                self.send_response(503)
                self.end_headers()

        elif path_clean in ["/roms", "/api/roms"]:
            if self.get_roms_callback:
                try:
                    data = self.get_roms_callback()
                    self.send_json_response({"roms": data, "count": len(data)}, 200)
                except Exception as e:
                    logger.exception("Error fetching ROMs JSON: %s", e)
                    self.send_json_response({"error": "Internal server error"}, 500)
            else:
                self.send_response(503)
                self.end_headers()

        elif path_clean in ["/health", "/ping"]:
            self.send_json_response({"status": "ok"}, 200)

        else:
            self.send_json_response({"error": "Not found"}, 404)

    def do_POST(self):
        if not self.check_auth():
            return

        path_clean = self.path.split("?")[0].rstrip("/")
        try:
            content_len = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(content_len) if content_len > 0 else b"{}"
            body_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except Exception as e:
            self.send_json_response({"error": "Bad Request", "message": str(e)}, 400)
            return

        if path_clean in ["/api/upload", "/upload"]:
            if self.start_upload_callback:
                try:
                    device = body_json.get("device", "")
                    chat_id = str(body_json.get("chat_id", ""))
                    rom_id = body_json.get("rom_id")
                    mode = body_json.get("mode", "all")
                    try:
                        res = self.start_upload_callback(device, chat_id, rom_id, mode)
                    except TypeError:
                        res = self.start_upload_callback(device, chat_id, rom_id)
                    code = 200 if res.get("success") else 404
                    self.send_json_response(res, code)
                except Exception as e:
                    logger.exception("Error in start_upload_callback: %s", e)
                    self.send_json_response({"error": "Internal server error", "message": str(e)}, 500)
            else:
                self.send_json_response({"error": "Upload callback not configured"}, 503)

        elif path_clean in ["/api/cancel_upload", "/cancel_upload"]:
            if self.cancel_upload_callback:
                try:
                    upload_id = body_json.get("upload_id", "")
                    res = self.cancel_upload_callback(upload_id)
                    self.send_json_response(res, 200)
                except Exception as e:
                    logger.exception("Error in cancel_upload_callback: %s", e)
                    self.send_json_response({"error": "Internal server error"}, 500)
            else:
                self.send_json_response({"error": "Cancel callback not configured"}, 503)

        elif path_clean in ["/api/restart", "/restart"]:
            self.send_json_response({"success": True, "message": "Worker restarting"}, 200)
            def _delayed_exit():
                import time, os
                time.sleep(0.8)
                os._exit(0)
            threading.Thread(target=_delayed_exit, daemon=True).start()

        else:
            self.send_json_response({"error": "Not found"}, 404)


class StatusServer:
    """Threaded HTTP Status and Delegation API server."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8088,
        get_status_callback: Optional[Callable[[], Dict[str, Any]]] = None,
        get_roms_callback: Optional[Callable[[], List[Dict[str, Any]]]] = None,
        start_upload_callback: Optional[Callable[[str, str, Optional[str]], Dict[str, Any]]] = None,
        cancel_upload_callback: Optional[Callable[[str], Dict[str, Any]]] = None,
        secret_key: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.secret_key = secret_key
        self.get_status_callback = get_status_callback
        self.get_roms_callback = get_roms_callback
        self.start_upload_callback = start_upload_callback
        self.cancel_upload_callback = cancel_upload_callback
        self.httpd: Optional[HTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def start(self):
        """Start the HTTP API server in a background daemon thread."""
        get_status = self.get_status_callback
        get_roms = self.get_roms_callback
        start_upload = self.start_upload_callback
        cancel_upload = self.cancel_upload_callback
        secret = self.secret_key

        class CustomHandler(StatusRequestHandler):
            get_status_callback = staticmethod(get_status) if get_status else None
            get_roms_callback = staticmethod(get_roms) if get_roms else None
            start_upload_callback = staticmethod(start_upload) if start_upload else None
            cancel_upload_callback = staticmethod(cancel_upload) if cancel_upload else None
            expected_secret = secret

        try:
            self.httpd = HTTPServer((self.host, self.port), CustomHandler)
            self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
            self.thread.start()
            logger.info("HTTP Status & Task API listening on http://%s:%d", self.host, self.port)
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
