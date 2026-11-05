"""GoFile API Integration for Uploading ROM Files and Build Artifacts.

Provides high-speed streaming uploads via curl, parallel multi-threading,
cancellation support, and live progress reporting (speed in MB/s, % progress,
transferred bytes, and ETA) for Telegram bots.
"""

import os
import json
import time
import socket
import logging
import threading
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional, Callable, Tuple

from .artifacts import format_file_size

logger = logging.getLogger("aosp_tracker.gofile")


def get_proc_file_offset(pid: int, file_path: str) -> Optional[int]:
    """Read the current byte read offset of an open file from /proc/<pid>/fdinfo."""
    real_path = os.path.realpath(file_path)
    fd_dir = f"/proc/{pid}/fd"
    if not os.path.isdir(fd_dir):
        return None
    try:
        for fd in os.listdir(fd_dir):
            try:
                link = os.path.realpath(f"{fd_dir}/{fd}")
                if link == real_path:
                    info_path = f"/proc/{pid}/fdinfo/{fd}"
                    if os.path.exists(info_path):
                        with open(info_path, "r") as f:
                            for line in f:
                                if line.startswith("pos:"):
                                    return int(line.split()[1])
            except Exception:
                continue
    except Exception:
        pass
    return None


class UploadTracker:
    """Tracks multi-process file upload progress, calculates aggregate MB/s and ETA."""

    def __init__(self, file_paths: List[str]):
        self.file_paths = file_paths
        self.file_sizes = {p: os.path.getsize(p) for p in file_paths if os.path.isfile(p)}
        self.total_bytes = sum(self.file_sizes.values())
        self.total_files = len(self.file_paths)

        self.completed_bytes = 0
        self.completed_files = 0
        self.lock = threading.Lock()
        self.active_procs: Dict[int, Dict[str, Any]] = {}

        self.start_time = time.time()
        self.speed_history: List[Tuple[float, int]] = [(self.start_time, 0)]
        self.speed_mb_s = 0.0

    def register_proc(self, proc: subprocess.Popen, file_path: str):
        with self.lock:
            self.active_procs[proc.pid] = {
                "proc": proc,
                "file_path": file_path,
                "file_size": self.file_sizes.get(file_path, 0),
                "last_pos": 0,
            }

    def unregister_proc(self, proc: subprocess.Popen, success: bool, file_path: str):
        with self.lock:
            self.active_procs.pop(proc.pid, None)
            if success:
                fsize = self.file_sizes.get(file_path, 0)
                self.completed_bytes += fsize
                self.completed_files += 1

    def snapshot(self) -> Dict[str, Any]:
        now = time.time()
        in_flight_bytes = 0
        active_filenames = []

        with self.lock:
            for pid, info in list(self.active_procs.items()):
                proc = info["proc"]
                fpath = info["file_path"]
                fsize = info["file_size"]
                if proc.poll() is None:
                    pos = get_proc_file_offset(pid, fpath)
                    if pos is not None and pos > info["last_pos"]:
                        info["last_pos"] = min(pos, fsize)
                elif proc.returncode == 0:
                    info["last_pos"] = fsize
                in_flight_bytes += info["last_pos"]
                active_filenames.append(os.path.basename(fpath))

            transferred = min(self.total_bytes, self.completed_bytes + in_flight_bytes)
            comp_files = self.completed_files

        # Sliding window for smooth speed in MB/s
        self.speed_history.append((now, transferred))
        self.speed_history = [(t, b) for t, b in self.speed_history if (now - t) <= 4.0]

        if len(self.speed_history) >= 2:
            dt = self.speed_history[-1][0] - self.speed_history[0][0]
            db = self.speed_history[-1][1] - self.speed_history[0][1]
            if dt > 0.4:
                self.speed_mb_s = (db / dt) / (1024.0 * 1024.0)
        else:
            overall_dt = now - self.start_time
            if overall_dt > 0.4:
                self.speed_mb_s = (transferred / overall_dt) / (1024.0 * 1024.0)

        pct = (transferred / max(1, self.total_bytes)) * 100.0
        remaining_bytes = max(0, self.total_bytes - transferred)
        speed_bps = self.speed_mb_s * 1024.0 * 1024.0
        eta_seconds = (remaining_bytes / speed_bps) if speed_bps > 50000 else None

        return {
            "transferred_bytes": transferred,
            "total_bytes": self.total_bytes,
            "pct": round(pct, 2),
            "speed_mb_s": round(self.speed_mb_s, 2),
            "eta_seconds": int(eta_seconds) if eta_seconds is not None else None,
            "completed_files": comp_files,
            "total_files": self.total_files,
            "current_filename": active_filenames[0] if active_filenames else None,
            "active_filenames": active_filenames,
        }


FALLBACK_GOFILE_SERVERS = [
    "store-eu-par-10",
    "store-eu-par-13",
    "store-eu-par-11",
    "store-eu-par-4",
    "store-eu-par-9",
    "store-eu-par-2",
    "store-eu-par-5",
    "store-eu-par-8",
    "store-eu-par-1",
    "store-eu-par-12",
    "store-eu-par-3",
    "store10",
    "store4",
    "store6",
    "store1",
]

_CACHED_SERVERS: List[str] = []
_CACHED_SERVERS_TIME: float = 0.0
_CACHED_GUEST_TOKEN: Optional[str] = None
_CACHED_GUEST_TOKEN_TIME: float = 0.0


def get_guest_token() -> Optional[str]:
    """Obtain or reuse a GoFile guest account token for higher throughput and folder management."""
    global _CACHED_GUEST_TOKEN, _CACHED_GUEST_TOKEN_TIME
    now = time.time()
    if _CACHED_GUEST_TOKEN and (now - _CACHED_GUEST_TOKEN_TIME) < 3600.0:
        return _CACHED_GUEST_TOKEN
    try:
        req = urllib.request.Request(
            "https://api.gofile.io/accounts",
            data=b"",
            headers={"User-Agent": "curl/7.81.0", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") == "ok":
                tok = data.get("data", {}).get("token")
                if tok:
                    _CACHED_GUEST_TOKEN = tok
                    _CACHED_GUEST_TOKEN_TIME = now
                    return tok
    except Exception as e:
        logger.debug("Failed to acquire GoFile guest token: %s", e)
    return None


def get_ranked_gofile_servers(force_refresh: bool = False) -> List[str]:
    """Dynamically discover and benchmark GoFile upload servers, returning them sorted by lowest latency."""
    global _CACHED_SERVERS, _CACHED_SERVERS_TIME
    now = time.time()
    if not force_refresh and _CACHED_SERVERS and (now - _CACHED_SERVERS_TIME) < 300.0:
        return _CACHED_SERVERS

    candidate_names = set(FALLBACK_GOFILE_SERVERS)
    try:
        req = urllib.request.Request(
            "https://api.gofile.io/servers",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") == "ok":
                for s in data.get("data", {}).get("servers", []):
                    name = s.get("name")
                    if name:
                        candidate_names.add(name)
    except Exception as e:
        logger.debug("GoFile server API fetch error: %s", e)

    # Filter out 'upload' or non-store endpoints which proxy through Cloudflare
    candidate_names = {s for s in candidate_names if s and s != "upload" and "upload" not in s.lower()}

    def probe(name: str) -> Tuple[str, float]:
        host = f"{name}.gofile.io"
        try:
            t0 = time.time()
            s = socket.create_connection((host, 443), timeout=1.2)
            dt = (time.time() - t0) * 1000.0
            s.close()
            return (name, dt)
        except Exception:
            return (name, 99999.0)

    try:
        with ThreadPoolExecutor(max_workers=min(16, len(candidate_names))) as pool:
            ranked = list(pool.map(probe, candidate_names))
        ranked.sort(key=lambda x: x[1])
        valid_servers = [name for name, lat in ranked if lat < 90000.0]
        if valid_servers:
            _CACHED_SERVERS = valid_servers
            _CACHED_SERVERS_TIME = now
            return valid_servers
    except Exception as e:
        logger.debug("Error probing server latencies: %s", e)

    _CACHED_SERVERS = FALLBACK_GOFILE_SERVERS
    _CACHED_SERVERS_TIME = now
    return FALLBACK_GOFILE_SERVERS


def get_active_gofile_server(force_refresh: bool = False) -> str:
    """Return the fastest active GoFile upload server."""
    servers = get_ranked_gofile_servers(force_refresh)
    return servers[0] if servers else "store-eu-par-10"


def upload_single_file(
    file_path: str,
    folder_id: Optional[str] = None,
    token: Optional[str] = None,
    server: Optional[str] = None,
    timeout: int = 1800,
    cancel_event: Optional[threading.Event] = None,
    active_procs: Optional[List[subprocess.Popen]] = None,
    tracker: Optional[UploadTracker] = None,
) -> Optional[Dict[str, Any]]:
    """Upload a single file to GoFile with high-speed curl transfer and instant cancellation."""
    if cancel_event and cancel_event.is_set():
        return None
    if not os.path.isfile(file_path):
        logger.error("File not found: %s", file_path)
        return None

    filename = os.path.basename(file_path)
    selected_server = server or get_active_gofile_server()
    if not selected_server or selected_server == "upload":
        selected_server = get_active_gofile_server()
    url = f"https://{selected_server}.gofile.io/uploadfile"

    cmd = [
        "curl", "-s", "--tcp-nodelay", "--connect-timeout", "15",
        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "-H", "Accept: application/json",
        "--keepalive-time", "10",
        "--max-time", str(timeout),
        "-X", "POST", url,
        "-F", f"file=@{file_path}",
    ]
    if folder_id:
        cmd.extend(["-F", f"folderId={folder_id}"])
    if token:
        cmd.extend(["-F", f"token={token}", "-H", f"Authorization: Bearer {token}"])

    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if active_procs is not None:
            active_procs.append(proc)
        if tracker is not None:
            tracker.register_proc(proc, file_path)

        start_t = time.time()
        while proc.poll() is None:
            if cancel_event and cancel_event.is_set():
                try:
                    proc.kill()
                except Exception:
                    pass
                return None
            if (time.time() - start_t) > (timeout + 15):
                try:
                    proc.kill()
                except Exception:
                    pass
                return None
            time.sleep(0.3)

        stdout, stderr = proc.communicate()
        if active_procs is not None and proc in active_procs:
            try:
                active_procs.remove(proc)
            except Exception:
                pass

        if cancel_event and cancel_event.is_set():
            if tracker is not None:
                tracker.unregister_proc(proc, False, file_path)
            return None

        success = False
        res_data = None
        if proc.returncode == 0 and stdout and stdout.strip().startswith("{"):
            try:
                res_json = json.loads(stdout)
                if res_json.get("status") == "ok":
                    success = True
                    res_data = res_json.get("data", {})
                else:
                    logger.warning("GoFile returned non-ok from %s: %s", selected_server, res_json)
            except Exception as e:
                logger.warning("Failed to parse GoFile JSON response from %s: %s", selected_server, e)
        else:
            err_preview = (stdout.strip()[:200] if stdout else "") or (stderr.strip()[:200] if stderr else "")
            logger.warning("GoFile upload error for %s on %s (rc=%d): %s", filename, selected_server, proc.returncode, err_preview)

        if tracker is not None:
            tracker.unregister_proc(proc, success, file_path)

        return res_data
    except Exception as e:
        logger.error("Exception in curl upload for %s on %s: %s", filename, selected_server, e)
        if tracker is not None and proc is not None:
            tracker.unregister_proc(proc, False, file_path)

    return None


def upload_artifacts_batch(
    file_paths: List[str],
    token: Optional[str] = None,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    max_workers: int = 4,
    cancel_event: Optional[threading.Event] = None,
    active_procs: Optional[List[subprocess.Popen]] = None,
) -> Dict[str, Any]:
    """Upload multiple files into the same GoFile destination folder using parallel streams."""
    results: Dict[str, Any] = {
        "success": False,
        "cancelled": False,
        "download_page": None,
        "folder_id": None,
        "uploaded_files": [],
        "failed_files": [],
        "duration_seconds": 0.0,
        "avg_speed_mb_s": 0.0,
    }

    if not file_paths:
        return results

    if cancel_event and cancel_event.is_set():
        results["cancelled"] = True
        return results

    valid_paths = [p for p in file_paths if os.path.isfile(p)]
    total_files = len(valid_paths)
    if total_files == 0:
        return results

    # Sort files by size: upload smallest file first to establish folder in < 1 second
    sorted_paths = sorted(valid_paths, key=lambda p: os.path.getsize(p))

    start_time = time.time()
    tracker = UploadTracker(sorted_paths)
    stop_ticker = threading.Event()

    def ticker_worker():
        while not stop_ticker.is_set():
            if cancel_event and cancel_event.is_set():
                break
            snap = tracker.snapshot()
            if progress_callback:
                try:
                    progress_callback(snap)
                except Exception:
                    pass
            time.sleep(1.0)

    ticker_thread = threading.Thread(target=ticker_worker, daemon=True)
    ticker_thread.start()

    try:
        first_path = sorted_paths[0]
        first_name = os.path.basename(first_path)
        first_size = os.path.getsize(first_path)

        # Acquire guest token if no token is configured
        working_token = token or get_guest_token()

        # Try active server with automatic retry across fallbacks if initial server rejects
        candidate_servers = get_ranked_gofile_servers()
        if not candidate_servers:
            candidate_servers = [get_active_gofile_server()]

        first_res = None
        working_server = candidate_servers[0]
        for srv in candidate_servers:
            if cancel_event and cancel_event.is_set():
                results["cancelled"] = True
                return results

            first_res = upload_single_file(
                file_path=first_path,
                token=working_token,
                server=srv,
                timeout=180,
                cancel_event=cancel_event,
                active_procs=active_procs,
                tracker=tracker,
            )
            if first_res and first_res.get("parentFolder"):
                working_server = srv
                break
            logger.warning("Upload of %s to server %s failed, trying fallback...", first_name, srv)

        if not first_res or not first_res.get("parentFolder"):
            results["failed_files"].append(first_path)
            logger.error("Failed to initialize GoFile destination folder with %s across all servers", first_name)
            return results

        current_folder_id = first_res.get("parentFolder")
        current_token = working_token or first_res.get("guestToken")
        download_page = first_res.get("downloadPage")

        results["uploaded_files"].append({
            "name": first_name,
            "size_formatted": format_file_size(first_size),
            "size_bytes": first_size,
            "download_page": download_page,
            "id": first_res.get("id"),
        })

        remaining_paths = sorted_paths[1:]

        if remaining_paths and not (cancel_event and cancel_event.is_set()):
            def worker(fpath: str):
                if cancel_event and cancel_event.is_set():
                    return (fpath, None)
                fsize = os.path.getsize(fpath)
                timeout = max(300, int(fsize / (150 * 1024)))
                # Try working server first
                res = upload_single_file(
                    file_path=fpath,
                    folder_id=current_folder_id,
                    token=current_token,
                    server=working_server,
                    timeout=timeout,
                    cancel_event=cancel_event,
                    active_procs=active_procs,
                    tracker=tracker,
                )
                if res and res.get("id"):
                    return (fpath, res)

                # Retry with up to 3 alternative candidate servers if working_server failed or dropped
                retries_left = 3
                for fallback_srv in candidate_servers:
                    if fallback_srv == working_server:
                        continue
                    if retries_left <= 0 or (cancel_event and cancel_event.is_set()):
                        break
                    retries_left -= 1
                    logger.info("Retrying upload of %s to fallback server %s...", os.path.basename(fpath), fallback_srv)
                    res = upload_single_file(
                        file_path=fpath,
                        folder_id=current_folder_id,
                        token=current_token,
                        server=fallback_srv,
                        timeout=timeout,
                        cancel_event=cancel_event,
                        active_procs=active_procs,
                        tracker=tracker,
                    )
                    if res and res.get("id"):
                        return (fpath, res)

                return (fpath, None)

            # Upload remaining files concurrently in thread pool
            with ThreadPoolExecutor(max_workers=min(max_workers, len(remaining_paths))) as pool:
                futures = [pool.submit(worker, p) for p in remaining_paths]
                for fut in as_completed(futures):
                    if cancel_event and cancel_event.is_set():
                        results["cancelled"] = True
                        break
                    fpath, res = fut.result()
                    fname = os.path.basename(fpath)
                    fsize = os.path.getsize(fpath)

                    if res:
                        results["uploaded_files"].append({
                            "name": fname,
                            "size_formatted": format_file_size(fsize),
                            "size_bytes": fsize,
                            "download_page": download_page,
                            "id": res.get("id"),
                        })
                        logger.info("Uploaded to GoFile: %s", fname)
                    elif not (cancel_event and cancel_event.is_set()):
                        results["failed_files"].append(fpath)
                        logger.warning("Failed parallel upload to GoFile: %s", fname)

        if cancel_event and cancel_event.is_set():
            results["cancelled"] = True
            return results

        if results["uploaded_files"]:
            results["success"] = True
            results["folder_id"] = current_folder_id
            results["download_page"] = download_page

        return results

    finally:
        stop_ticker.set()
        ticker_thread.join(timeout=1.0)
        total_duration = time.time() - start_time
        results["duration_seconds"] = total_duration
        uploaded_bytes = sum(f.get("size_bytes", 0) for f in results["uploaded_files"])
        if total_duration > 0 and uploaded_bytes > 0:
            results["avg_speed_mb_s"] = round((uploaded_bytes / total_duration) / (1024.0 * 1024.0), 2)
        else:
            results["avg_speed_mb_s"] = 0.0

        if progress_callback and not (cancel_event and cancel_event.is_set()):
            try:
                final_snap = tracker.snapshot()
                final_snap["is_final"] = True
                progress_callback(final_snap)
            except Exception:
                pass
