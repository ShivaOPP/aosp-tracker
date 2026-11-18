"""Stream a selected release to its file host and return a shareable page."""

import base64
import json
import os
import re
import subprocess
import time
from urllib.parse import quote, urlsplit

from .artifacts import format_file_size
from .gofile import UploadTracker, upload_artifacts_batch

HOSTS = {"gofile": "GoFile", "pixeldrain": "Pixeldrain", "fuckingfast": "FuckingFast.net"}


def package_path(rom):
    name = rom.get("main_zip_name")
    return next((p for p in rom.get("files_to_upload", []) if os.path.basename(p) == name), None)


def select_artifacts(rom, mode):
    paths = rom.get("files_to_upload", [])
    if mode == "pkg":
        main = package_path(rom)
        paths = [main] if main else []
    elif mode == "zip":
        paths = [p for p in paths if p.lower().endswith(".zip")]
    elif mode == "boot":
        paths = [p for p in paths if p.lower().endswith((".img", ".bin"))]
    return dict(rom, files_to_upload=paths)


def pixeldrain_key(config):
    return os.environ.get("PIXELDRAIN_API_KEY") or config.get("pixeldrain_api_key")


def share_url(provider, body):
    if provider == "pixeldrain":
        data = json.loads(body)
        file_id = data.get("id") if isinstance(data, dict) else None
        if isinstance(file_id, str) and data.get("success") and re.fullmatch(r"[A-Za-z0-9_-]+", file_id):
            return "https://pixeldrain.com/u/" + file_id
    else:
        # The PUT endpoint returns a public link. Accept a URL or a JSON envelope.
        try:
            data = json.loads(body)
        except ValueError:
            data = body.strip()
        if isinstance(data, dict):
            data = data.get("url") or data.get("link") or data.get("downloadUrl")
        if isinstance(data, str):
            url = urlsplit(data)
            if url.scheme == "https" and url.netloc == "fuckingfast.net" and len(url.path) > 1 and not re.search(r'[\s<>"\x00-\x1f]', data):
                return data
    raise ValueError("The host did not return a valid download link.")


def upload_release(rom, provider, config, progress_callback=None, cancel_event=None, active_procs=None):
    if provider == "gofile":
        return upload_artifacts_batch(rom["files_to_upload"], token=config.get("gofile_token"),
                                      progress_callback=progress_callback, cancel_event=cancel_event,
                                      active_procs=active_procs)
    if provider not in HOSTS:
        return {"success": False, "error": "Unknown upload host."}
    path = package_path(rom)
    if not path or not os.path.isfile(path):
        return {"success": False, "error": "The selected release package is missing. Refresh the library."}
    if cancel_event and cancel_event.is_set():
        return {"cancelled": True}
    filename = os.path.basename(path)
    name = quote(filename, safe="")
    headers = b""
    if provider == "pixeldrain":
        key = pixeldrain_key(config)
        if not key:
            return {"success": False, "error": "Set PIXELDRAIN_API_KEY on the uploading node."}
        headers = b"Authorization: Basic " + base64.b64encode((":" + key).encode()) + b"\n"
        url = "https://pixeldrain.com/api/file/" + name
    else:
        url = "https://w.fuckingfast.net/" + name

    cmd = ["curl", "--disable", "--silent", "--show-error", "--fail", "--globoff",
           "--connect-timeout", "15", "--speed-limit", "1024", "--speed-time", "120",
           "--header", "@-", "--upload-file", path, "--url", url]
    tracker = UploadTracker([path])
    start = time.monotonic()
    try:
        with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
            tracker.register_proc(proc, path)
            if active_procs is not None:
                active_procs.append(proc)
            try:
                while True:
                    if cancel_event and cancel_event.is_set():
                        proc.kill()
                    try:
                        body, stderr = proc.communicate(headers, timeout=1)
                        break
                    except subprocess.TimeoutExpired:
                        headers = None
                        if progress_callback:
                            progress_callback(tracker.snapshot())
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                if active_procs is not None:
                    active_procs.remove(proc)
            if cancel_event and cancel_event.is_set():
                return {"cancelled": True}
            if proc.returncode:
                return {"success": False, "error": stderr.decode(errors="replace").strip()[:200] or "Transfer failed."}
        link = share_url(provider, body.decode().strip())
        size = tracker.total_bytes
        elapsed = max(time.monotonic() - start, 0.001)
        return {"success": True, "download_page": link, "duration_seconds": elapsed,
                "avg_speed_mb_s": size / (1024 ** 2) / elapsed,
                "uploaded_files": [{"name": filename, "size_bytes": size, "size_formatted": format_file_size(size)}]}
    except (ValueError, OSError) as exc:
        return {"success": False, "error": str(exc)}
