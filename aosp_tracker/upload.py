"""Stream a selected release to its file host and return a shareable page."""

import base64
import concurrent.futures
import http.client
import json
import os
import re
import ssl
import subprocess
import threading
import time
import uuid
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from .artifacts import format_file_size
from .gofile import UploadTracker, upload_artifacts_batch

HOSTS = {
    "gofile": "GoFile",
    "pixeldrain": "Pixeldrain",
    "fuckingfast": "FuckingFast.net",
    "storage_to": "Storage.to",
    "sendit": "SendIt.sh",
    "temp_sh": "Temp.sh",
    "x0at": "X0.at",
    "qurl": "QURL.sh",
    "whalebone": "Transfer (Whalebone)",
    "uguu": "Uguu.se",
    "tmpfiles": "TmpFiles.org",
}

GIB = 1024 ** 3
MIB = 1024 ** 2
MAX_FILE_BYTES = {
    "storage_to": 25_000_000_000,
    "sendit": 10_000_000_000,
    "temp_sh": 4_000_000_000,
    "x0at": GIB,
    "qurl": 500 * MIB,
    "whalebone": 250 * MIB,
    "uguu": 128 * MIB,
    "tmpfiles": 100 * MIB,
}


def package_hosts(config, size_bytes, include_pixeldrain=True):
    """Return usable package hosts for this node and file size."""
    result = ["fuckingfast", "pixeldrain"]
    result.extend(provider for provider, limit in MAX_FILE_BYTES.items()
                  if size_bytes <= limit)
    return result


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


def save_pixeldrain_key(key, env_path="/etc/aosp_bot/upload.env"):
    """Persist a Pixeldrain key in a root-readable systemd environment file."""
    key = str(key).strip()
    if not key or len(key) > 512 or any(char.isspace() for char in key):
        raise ValueError("Invalid Pixeldrain API key format.")
    os.makedirs(os.path.dirname(env_path), mode=0o700, exist_ok=True)
    try:
        with open(env_path, "r", encoding="utf-8") as source:
            lines = source.read().splitlines()
    except FileNotFoundError:
        lines = []
    lines = [line for line in lines if not line.startswith("PIXELDRAIN_API_KEY=")]
    lines.append("PIXELDRAIN_API_KEY=" + key)
    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as destination:
        destination.write("\n".join(lines) + "\n")
    os.chmod(env_path, 0o600)


def share_url(provider, body):
    if provider == "pixeldrain":
        data = json.loads(body)
        file_id = data.get("id") if isinstance(data, dict) else None
        if isinstance(file_id, str) and data.get("success") and re.fullmatch(r"[A-Za-z0-9_-]+", file_id):
            return "https://pixeldrain.com/u/" + file_id
    elif provider == "fuckingfast":
        # Current PUT responses contain file metadata. Older responses provide a URL.
        try:
            data = json.loads(body)
        except ValueError:
            data = body.strip()
        if isinstance(data, dict):
            metadata = data.get("data")
            if data.get("code") in (200, 201) and isinstance(metadata, dict):
                file_id = metadata.get("id")
                if isinstance(file_id, str) and re.fullmatch(r"[A-Za-z0-9_-]+", file_id):
                    return "https://fuckingfast.net/" + file_id
            data = data.get("url") or data.get("link") or data.get("downloadUrl")
        if isinstance(data, str):
            url = urlsplit(data)
            if url.scheme == "https" and url.netloc == "fuckingfast.net" and len(url.path) > 1 and not re.search(r'[\s<>"\x00-\x1f]', data):
                return data
    else:
        try:
            data = json.loads(body)
        except ValueError:
            data = body
        candidates = []

        def collect(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key.lower() in {"url", "link", "downloadurl", "download_url", "fileurl"} and isinstance(item, str):
                        candidates.append(item)
                    else:
                        collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)
            elif isinstance(value, str):
                candidates.extend(re.findall(r"https?://[^\s<>\"']+", value))

        collect(data)
        allowed = {
            "storage_to": ("storage.to",),
            "sendit": ("sendit.sh",),
            "temp_sh": ("temp.sh",),
            "x0at": ("x0.at",),
            "qurl": ("qurl.sh",),
            "whalebone": ("transfer.whalebone.io",),
            "uguu": ("uguu.se", "a.uguu.se"),
            "tmpfiles": ("tmpfiles.org",),
        }.get(provider, ())
        for candidate in candidates:
            url = urlsplit(candidate.rstrip(".,)"))
            if (url.scheme == "https" and url.hostname and
                    any(url.hostname == host or url.hostname.endswith("." + host) for host in allowed) and
                    len(url.path) > 1 and not re.search(r'[\s<>"\x00-\x1f]', candidate)):
                return candidate.rstrip(".,)")
    raise ValueError("The host did not return a valid download link.")


def _curl_upload(path, provider, progress_callback=None, cancel_event=None, active_procs=None):
    filename = os.path.basename(path)
    encoded_name = quote(filename, safe="")
    fields = []
    extra_headers = []
    if provider == "sendit":
        url, form_field = f"https://sendit.sh/{encoded_name}", None
        extra_headers.append("Accept: application/json")
    elif provider == "temp_sh":
        url, form_field = "https://temp.sh/upload", "file"
    elif provider == "x0at":
        url, form_field = "https://x0.at/", "file"
    elif provider == "qurl":
        url, form_field = f"https://qurl.sh/{encoded_name}", None
        extra_headers.append("Accept: application/json")
    elif provider == "whalebone":
        url, form_field = f"https://transfer.whalebone.io/{encoded_name}", None
    elif provider == "uguu":
        url, form_field = "https://uguu.se/upload", "files[]"
    elif provider == "tmpfiles":
        url, form_field = "https://tmpfiles.org/api/v1/upload", "file"
    else:
        return {"success": False, "error": "This provider is not implemented."}

    cmd = ["curl", "--disable", "--silent", "--show-error", "--globoff",
           "--connect-timeout", "15", "--speed-limit", "1024", "--speed-time", "120",
           "--write-out", "\n%{http_code}"]
    for header in extra_headers:
        cmd.extend(["--header", header])
    if form_field:
        for key, value in fields:
            cmd.extend(["--form", f"{key}={value}"])
        cmd.extend(["--form", f"{form_field}=@{path}"])
    else:
        cmd.extend(["--upload-file", path])
    cmd.extend(["--url", url])

    tracker = UploadTracker([path])
    start = time.monotonic()
    try:
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
            tracker.register_proc(proc, path)
            if active_procs is not None:
                active_procs.append(proc)
            try:
                while True:
                    if cancel_event and cancel_event.is_set():
                        proc.kill()
                    try:
                        output, stderr = proc.communicate(timeout=1)
                        break
                    except subprocess.TimeoutExpired:
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
                return {"success": False, "error": stderr.decode(errors="replace").strip()[:240] or "Transfer failed."}
        response = output.decode(errors="replace").rstrip()
        response_body, _, status = response.rpartition("\n")
        if not status.isdigit() or int(status) < 200 or int(status) >= 300:
            return {"success": False, "error": (response_body or f"Upload service returned HTTP {status or 'error'}")[:240]}
        link = share_url(provider, response_body.strip())
        size = tracker.total_bytes
        if provider == "tmpfiles":
            parsed = urlsplit(link)
            link = parsed._replace(path=parsed.path.replace("/", "/dl/", 1)).geturl()
        elapsed = max(time.monotonic() - start, 0.001)
        return {"success": True, "download_page": link, "duration_seconds": elapsed,
                "avg_speed_mb_s": size / MIB / elapsed,
                "uploaded_files": [{"name": filename, "size_bytes": size, "size_formatted": format_file_size(size)}]}
    except (ValueError, OSError) as exc:
        return {"success": False, "error": str(exc)}


def _storage_visitor_token(config):
    token = os.environ.get("STORAGE_TO_VISITOR_TOKEN") or config.get("storage_to_visitor_token")
    if token:
        return token
    token_path = os.path.expanduser("~/.config/aosp_tracker/storage_to_visitor_token")
    os.makedirs(os.path.dirname(token_path), mode=0o700, exist_ok=True)
    try:
        with open(token_path, "r", encoding="ascii") as token_file:
            return token_file.read().strip()
    except OSError:
        token = uuid.uuid4().hex
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as token_file:
            token_file.write(token)
        return token


def _storage_api_post(endpoint, payload, visitor_token, owner_token=None):
    headers = {"Content-Type": "application/json", "X-Visitor-Token": visitor_token}
    if owner_token:
        headers["Authorization"] = "Owner " + owner_token
    request = Request("https://storage.to/api/" + endpoint, data=json.dumps(payload).encode(),
                      headers=headers, method="POST")
    with urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict) or result.get("success") is False:
        raise ValueError(result.get("error", "Storage.to API request failed.") if isinstance(result, dict) else "Invalid Storage.to response.")
    return result


def _storage_put_part(url, path, offset, length, cancel_event, progress_state, progress_callback):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Storage.to returned an invalid upload URL.")
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=180,
                                             context=ssl.create_default_context())
    target = parsed.path + (("?" + parsed.query) if parsed.query else "")
    try:
        connection.putrequest("PUT", target)
        connection.putheader("Host", parsed.netloc)
        connection.putheader("Content-Length", str(length))
        connection.endheaders()
        sent = 0
        with open(path, "rb", buffering=0) as source:
            source.seek(offset)
            while sent < length:
                if cancel_event and cancel_event.is_set():
                    raise InterruptedError("Upload cancelled.")
                chunk = source.read(min(1024 * 1024, length - sent))
                if not chunk:
                    raise IOError("The source file ended before the upload part was complete.")
                connection.send(chunk)
                sent += len(chunk)
                with progress_state["lock"]:
                    progress_state["bytes"] += len(chunk)
                    transferred = progress_state["bytes"]
                if progress_callback:
                    elapsed = max(time.monotonic() - progress_state["start"], 0.001)
                    speed = transferred / MIB / elapsed
                    left = max(0, progress_state["total"] - transferred)
                    progress_callback({"transferred_bytes": transferred, "total_bytes": progress_state["total"],
                                       "pct": round(transferred * 100 / max(1, progress_state["total"]), 2),
                                       "speed_mb_s": round(speed, 2), "eta_seconds": int(left / (speed * MIB)) if speed > 0 else None,
                                       "completed_files": 0, "total_files": 1, "current_filename": os.path.basename(path)})
        response = connection.getresponse()
        body = response.read(4096)
        if response.status < 200 or response.status >= 300:
            raise ValueError(f"Storage.to part upload returned HTTP {response.status}: {body.decode(errors='replace')[:160]}")
        etag = response.getheader("ETag")
        if not etag:
            raise ValueError("Storage.to did not return an ETag for an uploaded part.")
        return etag
    finally:
        connection.close()


def _upload_storage_to(path, config, progress_callback=None, cancel_event=None):
    filename = os.path.basename(path)
    size = os.path.getsize(path)
    visitor = _storage_visitor_token(config)
    content_type = "application/zip" if filename.lower().endswith(".zip") else "application/octet-stream"
    upload = _storage_api_post("upload/init", {"filename": filename, "content_type": content_type, "size": size}, visitor)
    started = time.monotonic()
    try:
        if upload.get("type") == "single":
            upload_url = upload.get("upload_url")
            if not isinstance(upload_url, str):
                raise ValueError("Storage.to did not return an upload URL.")
            headers = upload.get("headers", {})
            cmd = ["curl", "--disable", "--silent", "--show-error", "--globoff", "--connect-timeout", "15",
                   "--speed-limit", "1024", "--speed-time", "120", "--write-out", "\n%{http_code}"]
            for name, values in headers.items():
                for value in values if isinstance(values, list) else [values]:
                    cmd.extend(["--header", f"{name}: {value}"])
            cmd.extend(["--upload-file", path, "--url", upload_url])
            tracker = UploadTracker([path])
            with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
                tracker.register_proc(proc, path)
                try:
                    while True:
                        if cancel_event and cancel_event.is_set():
                            proc.kill()
                        try:
                            output, stderr = proc.communicate(timeout=1)
                            break
                        except subprocess.TimeoutExpired:
                            if progress_callback:
                                progress_callback(tracker.snapshot())
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()
                if cancel_event and cancel_event.is_set():
                    return {"cancelled": True}
                if proc.returncode:
                    raise ValueError(stderr.decode(errors="replace").strip()[:240] or "Storage.to transfer failed.")
            _, _, status = output.decode(errors="replace").rstrip().rpartition("\n")
            if status not in ("200", "201", "204"):
                raise ValueError(f"Storage.to transfer returned HTTP {status or 'error'}.")
        elif upload.get("type") == "multipart":
            part_size = int(upload.get("part_size", 0))
            part_count = int(upload.get("total_parts", 0))
            upload_id = upload.get("upload_id")
            owner_token = upload.get("owner_token")
            if part_size <= 0 or part_count <= 0 or not upload_id or not owner_token:
                raise ValueError("Storage.to returned incomplete multipart upload details.")
            urls = {int(number): value for number, value in (upload.get("initial_urls") or {}).items()}
            missing = [number for number in range(1, part_count + 1) if number not in urls]
            for start in range(0, len(missing), 100):
                extra = _storage_api_post("upload/parts", {"upload_id": upload_id, "part_numbers": missing[start:start + 100]}, visitor, owner_token)
                for item in extra.get("part_urls", []):
                    urls[int(item["partNumber"])] = item["url"]
            if len(urls) < part_count:
                raise ValueError("Storage.to did not provide upload URLs for every part.")
            progress_state = {"lock": threading.Lock(), "bytes": 0, "total": size, "start": started}
            def send_part(number):
                offset = (number - 1) * part_size
                length = min(part_size, size - offset)
                etag = _storage_put_part(urls[number], path, offset, length, cancel_event, progress_state, progress_callback)
                return {"partNumber": number, "etag": etag}
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                parts = sorted(pool.map(send_part, range(1, part_count + 1)), key=lambda item: item["partNumber"])
            if cancel_event and cancel_event.is_set():
                return {"cancelled": True}
            _storage_api_post("upload/complete-multipart", {"upload_id": upload_id, "parts": parts}, visitor, owner_token)
        else:
            raise ValueError("Storage.to returned an unknown upload type.")

        confirmed = _storage_api_post("upload/confirm", {"filename": filename, "size": size,
                                   "content_type": content_type, "r2_key": upload["r2_key"]}, visitor)
        file_data = confirmed.get("file", {})
        link = file_data.get("url")
        if not isinstance(link, str) or urlsplit(link).hostname not in ("storage.to", "www.storage.to"):
            raise ValueError("Storage.to did not return a valid share URL.")
        elapsed = max(time.monotonic() - started, 0.001)
        return {"success": True, "download_page": link, "duration_seconds": elapsed,
                "avg_speed_mb_s": size / MIB / elapsed,
                "uploaded_files": [{"name": filename, "size_bytes": size, "size_formatted": format_file_size(size)}]}
    except (ValueError, OSError, KeyError, TypeError, http.client.HTTPException, InterruptedError) as exc:
        if upload.get("type") == "multipart" and upload.get("upload_id"):
            try:
                _storage_api_post("upload/abort", {"upload_id": upload["upload_id"]}, visitor, upload.get("owner_token"))
            except Exception:
                pass
        return {"success": False, "error": str(exc)[:240]}


def upload_release(rom, provider, config, progress_callback=None, cancel_event=None, active_procs=None):
    if provider == "gofile":
        return upload_artifacts_batch(rom["files_to_upload"], token=config.get("gofile_token"),
                                      progress_callback=progress_callback, cancel_event=cancel_event,
                                      active_procs=active_procs)
    if provider in {"pixeldrain", "fuckingfast"}:
        path = package_path(rom)
        if not path or not os.path.isfile(path):
            return {"success": False, "error": "The selected release package is missing. Refresh the library."}
        if provider == "pixeldrain" and not pixeldrain_key(config):
            return {"success": False, "error": "Set PIXELDRAIN_API_KEY on the uploading node."}
        if cancel_event and cancel_event.is_set():
            return {"cancelled": True}
        filename = os.path.basename(path)
        name = quote(filename, safe="")
        if provider == "pixeldrain":
            key = pixeldrain_key(config)
            headers = b"Authorization: Basic " + base64.b64encode((":" + key).encode()) + b"\n"
            url = "https://pixeldrain.com/api/file/" + name
        else:
            headers = b""
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
                            headers = None
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
                    "avg_speed_mb_s": size / MIB / elapsed,
                    "uploaded_files": [{"name": filename, "size_bytes": size, "size_formatted": format_file_size(size)}]}
        except (ValueError, OSError) as exc:
            return {"success": False, "error": str(exc)}
    if provider in {"sendit", "temp_sh", "x0at", "qurl", "whalebone", "uguu", "tmpfiles"}:
        path = package_path(rom)
        if not path or not os.path.isfile(path):
            return {"success": False, "error": "The selected release package is missing. Refresh the library."}
        limit = MAX_FILE_BYTES.get(provider)
        if limit and os.path.getsize(path) > limit:
            return {"success": False, "error": f"This host's {format_file_size(limit)} per-file limit is smaller than the ROM package."}
        return _curl_upload(path, provider, progress_callback, cancel_event, active_procs)
    if provider == "storage_to":
        path = package_path(rom)
        if not path or not os.path.isfile(path):
            return {"success": False, "error": "The selected release package is missing. Refresh the library."}
        if os.path.getsize(path) > MAX_FILE_BYTES[provider]:
            return {"success": False, "error": "Storage.to's anonymous per-file limit is 25 GB."}
        return _upload_storage_to(path, config, progress_callback, cancel_event)
    return {"success": False, "error": "Unknown upload host."}
