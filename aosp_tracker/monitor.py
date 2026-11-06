"""Daemon Monitoring Core for AOSP Builds and Source Sync.

Continuously tracks soong_ui builds and repo syncs across all system users,
manages message lifecycles on Telegram, and performs artifact discovery.
Uses HTML parse_mode throughout to eliminate parsing errors with underscores.
"""

import os
import sys
import json
import time
import glob
import logging
import asyncio
import threading
import socket
import urllib.request
import urllib.error
from typing import Dict, Any, Optional, List, Tuple
import psutil

from .proto_parser import parse_build_progress, BuildProgress
from .rom_detector import parse_soong_log, detect_rom_from_tree, get_rom_avatar
from .user_detector import resolve_user_profile, load_global_config, find_username_by_chat_id
from .ogpreview import build_ogpreview_url
from .artifacts import discover_artifacts, format_file_size
from .telegram_client import TelegramClient
from .sync_tracker import RepoSyncSession
from .kernel_tracker import KernelBuildSession
from .api_server import StatusServer
from .formatter import (
    get_system_stats,
    format_build_in_progress,
    format_build_completed,
    format_build_failed,
    format_sync_in_progress,
    format_sync_completed,
    make_build_completed_keyboard,
    format_rom_list,
    make_rom_list_keyboard,
    format_gofile_upload_progress,
    format_gofile_upload_completed,
    make_gofile_open_keyboard,
    make_gofile_uploading_keyboard,
    format_kernel_build_in_progress,
    format_kernel_build_completed,
    format_kernel_build_failed,
    make_kernel_completed_keyboard,
    format_server_status_block,
    format_multi_server_status,
)
from .rom_scanner import scan_all_roms, get_rom_by_id
from .gofile import upload_artifacts_batch

logger = logging.getLogger("aosp_tracker.monitor")


def resolve_android_dir(path: Optional[str]) -> Optional[str]:
    """Resolve the true root of an Android tree by climbing parent directories if given a subfolder."""
    if not path or not os.path.isdir(path):
        return path
    cur = os.path.abspath(path)
    while cur and cur != os.path.dirname(cur):
        if (
            os.path.isfile(os.path.join(cur, "out", "soong.log"))
            or os.path.isfile(os.path.join(cur, "out", "build_progress.pb"))
            or os.path.isdir(os.path.join(cur, "build", "make"))
            or os.path.isdir(os.path.join(cur, "build", "soong"))
            or os.path.isdir(os.path.join(cur, ".repo"))
        ):
            return cur
        cur = os.path.dirname(cur)
    return os.path.abspath(path)


class BuildSession:
    def __init__(self, pid: int, username: str, build_dir: str, start_time: float):
        self.pid = pid
        self.username = username
        self.build_dir = resolve_android_dir(build_dir) or build_dir

        out_candidate = None
        try:
            p = psutil.Process(pid)
            env = p.environ()
            top = env.get("TOP")
            if top and os.path.isdir(top):
                self.build_dir = os.path.abspath(top)
            od = env.get("OUT_DIR")
            if od:
                out_candidate = od if os.path.isabs(od) else os.path.join(self.build_dir, od)
        except Exception:
            pass

        self.out_dir = out_candidate or os.path.join(self.build_dir, "out")
        self.start_time = start_time

        self.progress_pb_path = os.path.join(self.out_dir, "build_progress.pb")
        self.soong_log_path = os.path.join(self.out_dir, "soong.log")
        self.error_log_path = os.path.join(self.out_dir, "error.log")

        # Profiles and metadata
        self.user_profile = resolve_user_profile(username)
        self.rom_info = parse_soong_log(self.soong_log_path, self.build_dir)

        self.rom_title = self.rom_info.get("rom", "AOSP")
        self.device = self.rom_info.get("device", "generic")
        self.variant = self.rom_info.get("variant", "user")
        self.build_type = self.rom_info.get("type", "release")
        self.release = self.rom_info.get("release", "unknown")

        # Avatar resolution
        user_avatar = self.user_profile.get("avatar")
        self.avatar_url = user_avatar or get_rom_avatar(self.rom_title)

        # Telegram tracking
        self.message_id: Optional[int] = None
        self.chat_id: Optional[str] = self.user_profile.get("chat_id")
        self.last_edit_time: float = 0.0
        self.last_pct: float = -1.0
        self.last_progress: Optional[BuildProgress] = None

        # Rolling history for smooth ETA calculation: [(timestamp, finished_actions)]
        self.action_history: List[Tuple[float, int]] = []

    def refresh_rom_info(self):
        """Re-read soong.log if metadata was incomplete during initial startup."""
        fresh = parse_soong_log(self.soong_log_path, self.build_dir)
        if fresh.get("device") and fresh.get("device") not in ["generic", "_DIR"]:
            self.device = fresh.get("device")
        if fresh.get("rom") and fresh.get("rom") not in ["AOSP", "generic"]:
            self.rom_title = fresh.get("rom")
            if not self.user_profile.get("avatar"):
                self.avatar_url = get_rom_avatar(self.rom_title)
        if fresh.get("variant"):
            self.variant = fresh.get("variant")
        if fresh.get("type"):
            self.build_type = fresh.get("type")
        if fresh.get("release") and fresh.get("release") != "unknown":
            self.release = fresh.get("release")
        if fresh.get("out_dir"):
            new_out = os.path.join(self.build_dir, fresh["out_dir"]) if not os.path.isabs(fresh["out_dir"]) else fresh["out_dir"]
            if new_out != self.out_dir:
                self.out_dir = new_out
                self.progress_pb_path = os.path.join(self.out_dir, "build_progress.pb")
                self.soong_log_path = os.path.join(self.out_dir, "soong.log")
                self.error_log_path = os.path.join(self.out_dir, "error.log")
        self.rom_info = fresh

    def calculate_eta(self, finished: int, total: int) -> Optional[float]:
        """Calculate ETA using a 60-second rolling speed window."""
        now = time.time()
        self.action_history.append((now, finished))

        # Keep history within the last 60 seconds
        self.action_history = [(t, a) for t, a in self.action_history if (now - t) <= 60.0]

        if len(self.action_history) < 2:
            return None

        oldest_t, oldest_a = self.action_history[0]
        dt = now - oldest_t
        da = finished - oldest_a

        if dt > 5.0 and da > 0:
            rate = da / dt
            remaining = total - finished
            if remaining > 0:
                return remaining / rate
        return None

    def get_ogpreview_url(self) -> str:
        """Construct serverhive ogpreview URL."""
        return build_ogpreview_url(
            rom_title=self.rom_title,
            avatar_url=self.avatar_url,
            maintainer_name=self.user_profile.get("name", self.username),
            device=self.device,
            emoji=self.user_profile.get("emoji", "❄️"),
            theme=self.user_profile.get("theme", "midnightBloom"),
        )


STATE_FILE = "/var/run/aosp_tracker_state.json"
FALLBACK_STATE_FILE = "/tmp/aosp_tracker_state.json"


class AospMonitor:
    def __init__(self):
        self.global_config = load_global_config()
        self.bot_token = self.global_config.get("bot_token")
        self.telegram = TelegramClient(self.bot_token) if self.bot_token else None
        self.default_chat_id = self.global_config.get("default_chat_id", "-1004371923194")
        self.group_chat_id = str(self.global_config.get("group_chat_id", "-1004371923194"))
        configured_admins = self.global_config.get("admin_ids", ["6992001967", "6340997509"])
        self.admin_ids = {str(a).strip() for a in configured_admins}
        self.admin_ids.update({"6992001967", "6340997509"})
        configured_hidden = self.global_config.get("hidden_users", ["xlmost"])
        self.hidden_users = {str(u).lower().strip() for u in configured_hidden}
        self.hidden_users.add("xlmost")
        self.hidden_user_target_chat_id = str(self.global_config.get("hidden_user_target_chat_id", "6340997509"))
        self.server_name = self.global_config.get("server_name")
        self.listen_for_commands = bool(self.global_config.get("listen_for_commands", True))
        self.secret_key = str(self.global_config.get("secret_key", "")).strip()
        self.peer_servers = self.global_config.get("peer_servers", [])
        self.poll_interval = float(self.global_config.get("poll_interval", 5.0))
        self.update_interval = float(self.global_config.get("update_interval", 10.0))

        # Multi-server / Worker configuration
        self.server_name = self.global_config.get("server_name")
        self.listen_for_commands = bool(self.global_config.get("listen_for_commands", True))
        self.enable_api = bool(self.global_config.get("enable_api", True))
        self.api_host = str(self.global_config.get("api_host", "0.0.0.0"))
        self.api_port = int(self.global_config.get("api_port", 8088))
        self.secret_key = str(self.global_config.get("secret_key", "")).strip()
        self.peer_servers = self.global_config.get("peer_servers", [])
        self.status_server: Optional[StatusServer] = None

        # Active sessions
        self.active_builds: Dict[int, BuildSession] = {}
        self.active_syncs: Dict[int, RepoSyncSession] = {}
        self.active_kernels: Dict[int, KernelBuildSession] = {}
        self.active_uploads: Dict[str, Dict[str, Any]] = {}
        self.cached_multi_roms: List[Dict[str, Any]] = []
        self.loop: Optional[asyncio.AbstractEventLoop] = None

        # Telegram polling offset
        self.update_offset: Optional[int] = None
        self.bot_username: Optional[str] = None
        if self.telegram:
            try:
                me = self.telegram.get_me()
                if me and me.get("ok"):
                    self.bot_username = me.get("result", {}).get("username")
            except Exception:
                pass

        # Restore state from previous run
        self.load_state()

        # Register bot commands in Telegram UI
        if self.telegram:
            try:
                self.telegram.set_my_commands([
                    {"command": "roms", "description": "Search and download ROMs & Kernels on server"},
                    {"command": "kernels", "description": "Search and download Kernels on server via GoFile"},
                    {"command": "download", "description": "Upload ROM and Kernel files to GoFile"},
                    {"command": "status", "description": "Check active build, kernel, and sync status"},
                    {"command": "help", "description": "Bot instructions and help"},
                ])
            except Exception:
                pass

    def save_state(self):
        """Persist active build/sync/kernel sessions so restarts don't lose Telegram message IDs."""
        state = {
            "builds": {},
            "syncs": {},
            "kernels": {},
        }
        for pid, s in self.active_builds.items():
            state["builds"][str(pid)] = {
                "pid": s.pid,
                "username": s.username,
                "build_dir": s.build_dir,
                "start_time": s.start_time,
                "message_id": s.message_id,
                "chat_id": s.chat_id,
                "rom_title": s.rom_title,
                "device": s.device,
                "last_pct": s.last_pct,
                "last_edit_time": s.last_edit_time,
            }
        for pid, s in self.active_syncs.items():
            state["syncs"][str(pid)] = {
                "pid": s.pid,
                "username": s.username,
                "repo_dir": s.repo_dir,
                "start_time": s.start_time,
                "message_id": s.message_id,
                "chat_id": s.chat_id,
            }
        for pid, s in self.active_kernels.items():
            state["kernels"][str(pid)] = {
                "pid": s.pid,
                "username": s.username,
                "kernel_dir": s.kernel_dir,
                "out_dir": s.out_dir,
                "start_time": s.start_time,
                "message_id": s.message_id,
                "chat_id": s.chat_id,
                "kernel_title": s.kernel_title,
                "device": s.device,
                "last_pct": s.last_pct,
                "last_edit_time": s.last_edit_time,
            }
        target = STATE_FILE
        try:
            with open(target, "w") as f:
                json.dump(state, f)
        except Exception:
            try:
                with open(FALLBACK_STATE_FILE, "w") as f:
                    json.dump(state, f)
            except Exception as e:
                logger.debug("Failed to save state: %s", e)

    def load_state(self):
        """Restore active sessions from previous daemon run if process is still alive."""
        path = STATE_FILE if os.path.isfile(STATE_FILE) else FALLBACK_STATE_FILE
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r") as f:
                state = json.load(f)
            for pid_str, data in state.get("builds", {}).items():
                pid = int(pid_str)
                if psutil.pid_exists(pid):
                    try:
                        p = psutil.Process(pid)
                        cmd = p.cmdline()
                        if any("soong_ui" in a for a in cmd):
                            bdir = resolve_android_dir(data.get("build_dir")) or data.get("build_dir")
                            session = BuildSession(pid, data["username"], bdir, data["start_time"])
                            session.message_id = data.get("message_id")
                            session.chat_id = data.get("chat_id")
                            if session.rom_title in ["AOSP", "generic"] and data.get("rom_title") and data.get("rom_title") not in ["AOSP", "generic"]:
                                session.rom_title = data["rom_title"]
                            if session.device in ["generic", "_DIR"] and data.get("device") and data.get("device") not in ["generic", "_DIR"]:
                                session.device = data["device"]
                            session.last_pct = data.get("last_pct", -1.0)
                            session.last_edit_time = data.get("last_edit_time", 0.0)
                            self.active_builds[pid] = session
                            logger.info("Restored build session for PID %d (msg_id: %s, rom: %s, dev: %s)", pid, session.message_id, session.rom_title, session.device)
                    except Exception:
                        pass
            for pid_str, data in state.get("syncs", {}).items():
                pid = int(pid_str)
                if psutil.pid_exists(pid):
                    try:
                        p = psutil.Process(pid)
                        cmd = p.cmdline()
                        if any("sync" in a for a in cmd):
                            rdir = os.path.realpath(data["repo_dir"])
                            # Exactly one session per repo directory
                            if any(os.path.realpath(s.repo_dir) == rdir for s in self.active_syncs.values()):
                                continue
                            session = RepoSyncSession(pid, data["username"], data["repo_dir"], data["start_time"])
                            session.message_id = data.get("message_id")
                            session.chat_id = data.get("chat_id")
                            self.active_syncs[pid] = session
                            logger.info("Restored sync session for PID %d (msg_id: %s)", pid, session.message_id)
                    except Exception:
                        pass
            active_aosp_dirs = [os.path.realpath(s.build_dir) for s in self.active_builds.values() if s.build_dir]
            for pid_str, data in state.get("kernels", {}).items():
                pid = int(pid_str)
                if psutil.pid_exists(pid):
                    try:
                        p = psutil.Process(pid)
                        cmd = p.cmdline()
                        if any("make" in a.lower() for a in cmd):
                            kdir = os.path.realpath(data.get("kernel_dir", ""))
                            odir = os.path.realpath(data["out_dir"])
                            if any(os.path.commonpath([kdir, adir]) == adir for adir in active_aosp_dirs if adir):
                                continue
                            if any(os.path.realpath(s.out_dir) == odir for s in self.active_kernels.values()):
                                continue
                            session = KernelBuildSession(pid, data["username"], data["kernel_dir"], data["out_dir"], data["start_time"])
                            session.message_id = data.get("message_id")
                            session.chat_id = data.get("chat_id")
                            session.kernel_title = data.get("kernel_title", session.kernel_title)
                            session.device = data.get("device", session.device)
                            session.last_pct = data.get("last_pct", -1.0)
                            session.last_edit_time = data.get("last_edit_time", 0.0)
                            self.active_kernels[pid] = session
                            logger.info("Restored kernel build session for PID %d (msg_id: %s)", pid, session.message_id)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning("Failed to load previous tracker state: %s", e)

    def reload_config(self):
        """Reload configuration from disk."""
        self.global_config = load_global_config()
        new_token = self.global_config.get("bot_token")
        if new_token != self.bot_token:
            self.bot_token = new_token
            self.telegram = TelegramClient(new_token) if new_token else None
        self.default_chat_id = self.global_config.get("default_chat_id", "-1004371923194")
        self.group_chat_id = str(self.global_config.get("group_chat_id", "-1004371923194"))
        configured_admins = self.global_config.get("admin_ids", ["6992001967", "6340997509"])
        self.admin_ids = {str(a).strip() for a in configured_admins}
        self.admin_ids.update({"6992001967", "6340997509"})
        configured_hidden = self.global_config.get("hidden_users", ["xlmost"])
        self.hidden_users = {str(u).lower().strip() for u in configured_hidden}
        self.hidden_users.add("xlmost")
        self.hidden_user_target_chat_id = str(self.global_config.get("hidden_user_target_chat_id", "6340997509"))
        self.server_name = self.global_config.get("server_name")
        self.listen_for_commands = bool(self.global_config.get("listen_for_commands", True))
        self.secret_key = str(self.global_config.get("secret_key", "")).strip()
        self.peer_servers = self.global_config.get("peer_servers", [])

    def is_hidden_rom_or_user(
        self,
        username: Optional[str] = None,
        rom_title: Optional[str] = None,
        directory: Optional[str] = None,
    ) -> bool:
        """Check if session or ROM belongs to a hidden user (e.g. xlmost) or hidden rom name."""
        if username and str(username).lower().strip() in self.hidden_users:
            return True
        if rom_title:
            t = str(rom_title).lower().strip()
            if any(hu in t for hu in self.hidden_users):
                return True
        if directory:
            d = str(directory).lower()
            if any(f"/home/{hu}" in d or f"/{hu}/" in d or d.endswith(f"/{hu}") for hu in self.hidden_users):
                return True
        return False

    def get_target_chat_id(
        self,
        username: Optional[str] = None,
        rom_title: Optional[str] = None,
        directory: Optional[str] = None,
        session_chat_id: Optional[str] = None,
        user_profile_chat_id: Optional[str] = None,
    ) -> str:
        """Determine target Telegram chat ID, strictly isolating hidden users to their private target ID."""
        if self.is_hidden_rom_or_user(username, rom_title, directory):
            return self.hidden_user_target_chat_id

        # For normal users, route to group or default chat
        return self.group_chat_id or session_chat_id or user_profile_chat_id or self.default_chat_id

    def find_build_processes(self) -> List[Tuple[int, str, str, float]]:
        """Find actual running AOSP builds, strictly filtering out dumpvar/lunch probes."""
        results = []
        for p in psutil.process_iter(["pid", "name", "cmdline", "username", "create_time"]):
            try:
                cmd = p.info.get("cmdline")
                if not cmd:
                    continue

                # Must be soong_ui
                is_soong = any("soong_ui" in arg for arg in cmd)
                if not is_soong:
                    continue

                # STRICT: Ignore dumpvars / lunch / envsetup commands
                if any(
                    arg in ["--dumpvar-mode", "--dumpvars-mode"] or arg.startswith("--dumpvar")
                    for arg in cmd
                ):
                    continue

                pid = p.info["pid"]
                username = p.info.get("username") or "unknown"
                create_time = p.info.get("create_time") or time.time()

                # Extract and resolve true Android build root
                build_dir = None
                try:
                    proc_obj = psutil.Process(pid)
                    env = proc_obj.environ()
                    top = env.get("TOP")
                    if top and os.path.isdir(top):
                        build_dir = os.path.abspath(top)
                except Exception:
                    pass

                if not build_dir:
                    try:
                        cwd = p.cwd()
                        if cwd and os.path.isdir(cwd):
                            build_dir = resolve_android_dir(cwd)
                    except Exception:
                        pass

                if not build_dir:
                    for arg in cmd:
                        if arg.startswith("--dir="):
                            candidate = arg.split("=", 1)[1]
                            build_dir = resolve_android_dir(candidate)
                            break

                if not build_dir and cmd and cmd[0]:
                    bin_path = os.path.abspath(cmd[0])
                    if "/out/soong_ui" in bin_path:
                        cand = bin_path.split("/out/soong_ui")[0]
                        if os.path.isdir(cand):
                            build_dir = cand
                    elif "/build/soong/bin/soong_ui" in bin_path:
                        cand = bin_path.split("/build/soong/bin/soong_ui")[0]
                        if os.path.isdir(cand):
                            build_dir = cand

                if build_dir:
                    build_dir = resolve_android_dir(build_dir)

                if build_dir and os.path.isdir(build_dir):
                    results.append((pid, username, build_dir, create_time))
            except Exception:
                continue
        return results

    def find_sync_processes(self) -> List[Tuple[int, str, str, float]]:
        """Find only genuine, active repo sync root processes across the system, strictly filtering worker subprocesses."""
        candidates = []
        now = time.time()

        for p in psutil.process_iter(["pid", "ppid", "name", "cmdline", "username", "create_time"]):
            try:
                cmd = p.info.get("cmdline")
                if not cmd or len(cmd) < 2:
                    continue

                # Exclude internal build tools (e.g. zipsync, timesyncd, rsync)
                prog = os.path.basename(cmd[0]).lower()
                if "zipsync" in prog or "timesync" in prog:
                    continue

                is_sync = False
                repo_dir = None

                # 1. /usr/local/bin/repo sync ...
                if prog == "repo" or cmd[0].endswith("/repo"):
                    if any(arg == "sync" for arg in cmd[1:4]):
                        is_sync = True

                # 2. python3 .../.repo/repo/main.py ... sync ...
                elif prog.startswith("python"):
                    has_repo_main = any(
                        arg.endswith("/.repo/repo/main.py") or arg.endswith("/repo/main.py")
                        for arg in cmd
                    )
                    if has_repo_main and any(arg == "sync" for arg in cmd):
                        is_sync = True
                        for arg in cmd:
                            if arg.startswith("--repo-dir="):
                                repo_dir = arg.split("=", 1)[1]
                                break

                if is_sync:
                    pid = p.info["pid"]
                    ppid = p.info.get("ppid") or 0
                    create_time = p.info.get("create_time") or now

                    # Anti-spam: Ignore ephemeral helper checks that exit in under 5 seconds
                    if (now - create_time) < 5.0 and pid not in self.active_syncs:
                        continue

                    username = p.info.get("username") or "unknown"

                    if not repo_dir:
                        try:
                            cwd = p.cwd()
                            candidate = os.path.join(cwd, ".repo")
                            if os.path.isdir(candidate):
                                repo_dir = candidate
                        except Exception:
                            pass

                    if repo_dir and os.path.isdir(repo_dir):
                        candidates.append({
                            "pid": pid,
                            "ppid": ppid,
                            "username": username,
                            "repo_dir": os.path.realpath(repo_dir),
                            "create_time": create_time,
                        })
            except Exception:
                continue

        if not candidates:
            return []

        # STRICT FILTER 1: Exclude child worker processes whose PPID is another repo process
        candidate_pids = {c["pid"] for c in candidates}
        root_candidates = [c for c in candidates if c["ppid"] not in candidate_pids]

        # STRICT FILTER 2: Exactly ONE process per repo directory (oldest creation time / root leader)
        dir_to_proc: Dict[str, Dict[str, Any]] = {}
        for c in sorted(root_candidates, key=lambda x: x["create_time"]):
            rdir = c["repo_dir"]
            if rdir not in dir_to_proc:
                dir_to_proc[rdir] = c

        results = [
            (c["pid"], c["username"], c["repo_dir"], c["create_time"])
            for c in dir_to_proc.values()
        ]
        return results

    def find_kernel_build_processes(self) -> List[Tuple[int, str, str, str, float]]:
        """Find active Linux / Android kernel make compilation processes across the system."""
        now = time.time()
        candidate_makes = []

        active_aosp_pids = set(self.active_builds.keys())
        active_aosp_dirs = [os.path.realpath(b.build_dir) for b in self.active_builds.values() if b.build_dir]
        active_aosp_outs = [os.path.realpath(b.out_dir) for b in self.active_builds.values() if b.out_dir]

        for p in psutil.process_iter(["pid", "ppid", "name", "cmdline", "username", "create_time"]):
            try:
                cmd = p.info.get("cmdline")
                if not cmd or len(cmd) < 1:
                    continue

                prog = os.path.basename(cmd[0]).lower()
                is_make = "make" in prog or any(arg.endswith("build.sh") for arg in cmd)
                if not is_make:
                    continue

                # STRICT: Ignore configuration-only probes or cleanup targets
                if any(
                    arg.endswith("defconfig")
                    or arg.endswith("menuconfig")
                    or arg.endswith("nconfig")
                    or arg.endswith("gconfig")
                    or arg.endswith("savedefconfig")
                    or arg in ["clean", "mrproper", "distclean", "help", "--version"]
                    for arg in cmd
                ):
                    continue

                pid = p.info["pid"]
                ppid = p.info.get("ppid") or 0
                username = p.info.get("username") or "unknown"
                create_time = p.info.get("create_time") or now

                # 1. Filter out processes that have an ancestor in active AOSP builds or AOSP build tools
                is_aosp_child = False
                try:
                    for parent in p.parents():
                        if parent.pid in active_aosp_pids:
                            is_aosp_child = True
                            break
                        pname = parent.name().lower()
                        if any(ign in pname for ign in ("soong_ui", "ninja", "ckati", "kati", "siso", "nsjail")):
                            is_aosp_child = True
                            break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

                if is_aosp_child:
                    continue

                # Exclude ephemeral checks that exit in under 4 seconds
                if (now - create_time) < 4.0 and pid not in self.active_kernels:
                    continue

                # Determine kernel source dir & out dir
                kernel_dir = None
                out_dir = None

                for arg in cmd:
                    if arg.startswith("O="):
                        out_dir = arg.split("=", 1)[1]
                    elif arg.startswith("-C"):
                        kernel_dir = arg[2:] if len(arg) > 2 else cmd[cmd.index(arg) + 1]

                if not kernel_dir:
                    try:
                        kernel_dir = p.cwd()
                    except Exception:
                        pass

                if not kernel_dir or not os.path.isdir(kernel_dir):
                    continue

                kernel_dir = os.path.realpath(kernel_dir)

                # Check if kernel_dir is actually a Linux kernel source tree
                is_kernel = os.path.isfile(os.path.join(kernel_dir, "Kconfig")) and os.path.isfile(os.path.join(kernel_dir, "Makefile"))
                if not is_kernel:
                    parent_kdir = os.path.dirname(kernel_dir)
                    if os.path.isfile(os.path.join(parent_kdir, "Kconfig")) and os.path.isfile(os.path.join(parent_kdir, "Makefile")):
                        kernel_dir = parent_kdir
                        is_kernel = True

                if not is_kernel:
                    continue

                if not out_dir:
                    candidate_out = os.path.join(kernel_dir, "out")
                    out_dir = candidate_out if os.path.isdir(candidate_out) else kernel_dir
                elif not os.path.isabs(out_dir):
                    out_dir = os.path.realpath(os.path.join(kernel_dir, out_dir))
                else:
                    out_dir = os.path.realpath(out_dir)

                # 2. Filter out if kernel_dir or out_dir is inside any active AOSP build directory or out directory
                is_inside_active_aosp = False
                for adir in active_aosp_dirs:
                    if (os.path.commonpath([kernel_dir, adir]) == adir and kernel_dir != adir) or \
                       (out_dir and os.path.commonpath([out_dir, adir]) == adir and out_dir != adir):
                        is_inside_active_aosp = True
                        break
                if is_inside_active_aosp:
                    continue

                for aout in active_aosp_outs:
                    if os.path.commonpath([kernel_dir, aout]) == aout or \
                       (out_dir and os.path.commonpath([out_dir, aout]) == aout):
                        is_inside_active_aosp = True
                        break
                if is_inside_active_aosp:
                    continue

                # 3. Check if kernel_dir is nested inside any AOSP repository tree (root with build/envsetup.sh)
                is_nested_in_aosp = False
                curr = kernel_dir
                for _ in range(6):
                    parent = os.path.dirname(curr)
                    if not parent or parent == curr or parent in ["/", "/home", f"/home/{username}", "/root"]:
                        break
                    if os.path.isfile(os.path.join(parent, "build", "envsetup.sh")) or (
                        os.path.isdir(os.path.join(parent, "build", "soong")) and os.path.isdir(os.path.join(parent, "build", "make"))
                    ):
                        is_nested_in_aosp = True
                        break
                    curr = parent

                if is_nested_in_aosp:
                    continue

                candidate_makes.append({
                    "pid": pid,
                    "ppid": ppid,
                    "username": username,
                    "kernel_dir": kernel_dir,
                    "out_dir": out_dir,
                    "create_time": create_time,
                })
            except Exception:
                continue

        if not candidate_makes:
            return []

        # Filter out sub-makes whose parent process is also in candidate_makes
        make_pids = {m["pid"] for m in candidate_makes}
        root_makes = [m for m in candidate_makes if m["ppid"] not in make_pids]

        # Exactly ONE build session per out_dir
        dir_to_proc: Dict[str, Dict[str, Any]] = {}
        for m in sorted(root_makes, key=lambda x: x["create_time"]):
            odir = m["out_dir"]
            if odir not in dir_to_proc:
                dir_to_proc[odir] = m

        return [
            (m["pid"], m["username"], m["kernel_dir"], m["out_dir"], m["create_time"])
            for m in dir_to_proc.values()
        ]

    def process_build_step(self, session: BuildSession):
        """Process a single polling tick for an active build."""
        now = time.time()
        elapsed = now - session.start_time

        # Anti-spam: Do not send initial message if session has run < 3 seconds
        if session.message_id is None and elapsed < 3.0:
            return

        session.refresh_rom_info()

        # If progress_pb_path doesn't exist, self-heal by searching build_dir for out*/build_progress.pb
        if not os.path.isfile(session.progress_pb_path) and os.path.isdir(session.build_dir):
            cand_pbs = glob.glob(os.path.join(session.build_dir, "out*/build_progress.pb"))
            if cand_pbs:
                cand_pbs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                session.progress_pb_path = cand_pbs[0]
                session.out_dir = os.path.dirname(cand_pbs[0])
                session.soong_log_path = os.path.join(session.out_dir, "soong.log")
                session.error_log_path = os.path.join(session.out_dir, "error.log")

        # Parse build_progress.pb
        progress = parse_build_progress(session.progress_pb_path)
        if progress and progress.total_actions > 0:
            session.last_progress = progress
            pct = (progress.finished_actions / progress.total_actions * 100.0)
            eta = session.calculate_eta(progress.finished_actions, progress.total_actions)
            current_actions = progress.current_actions
            finished_actions = progress.finished_actions
            total_actions = progress.total_actions
        else:
            pct = 0.0
            eta = None
            current_actions = 1
            finished_actions = 0
            total_actions = 0

        if self.is_hidden_rom_or_user(session.username, session.rom_title, session.build_dir):
            if session.chat_id != self.hidden_user_target_chat_id:
                session.chat_id = self.hidden_user_target_chat_id
        target_chat_id = self.get_target_chat_id(
            username=session.username,
            rom_title=session.rom_title,
            directory=session.build_dir,
            session_chat_id=session.chat_id,
            user_profile_chat_id=session.user_profile.get("chat_id"),
        )
        if not target_chat_id or not self.telegram:
            return

        preview_url = session.get_ogpreview_url()
        sys_stats = get_system_stats(session.build_dir)
        text = format_build_in_progress(
            preview_url=preview_url,
            rom_title=session.rom_title,
            device=session.device,
            user_name=session.user_profile.get("name", session.username),
            user_emoji=session.user_profile.get("emoji", "❄️"),
            current_actions=current_actions,
            finished_actions=finished_actions,
            total_actions=total_actions,
            elapsed_seconds=elapsed,
            eta_seconds=eta,
            variant=session.variant,
            build_type=session.build_type,
            release=session.release,
            system_stats=sys_stats,
            server_name=self.server_name,
        )

        if session.message_id is None:
            # Send initial message in HTML mode
            msg_id = self.telegram.send_message(
                chat_id=target_chat_id,
                text=text,
                parse_mode="HTML",
            )
            if msg_id:
                session.message_id = msg_id
                session.chat_id = target_chat_id
                session.last_edit_time = now
                session.last_pct = pct
                self.save_state()
                logger.info(
                    "Sent build progress message for PID %d [%s on %s] (msg_id: %d)",
                    session.pid,
                    session.rom_title,
                    session.device,
                    msg_id,
                )
        else:
            time_diff = now - session.last_edit_time
            pct_diff = abs(pct - session.last_pct)
            if time_diff >= self.update_interval:
                if pct_diff >= 0.2 or pct >= 99.9 or time_diff >= 25.0:
                    edited = self.telegram.edit_message(
                        chat_id=session.chat_id,
                        message_id=session.message_id,
                        text=text,
                        parse_mode="HTML",
                        min_interval_seconds=self.update_interval,
                    )
                    if edited:
                        session.last_edit_time = now
                        session.last_pct = pct
                        self.save_state()
                        logger.info(
                            "Updated build progress for PID %d: %.2f%% (%d/%d actions)",
                            session.pid,
                            pct,
                            finished_actions,
                            total_actions,
                        )

    def handle_build_finished(self, session: BuildSession):
        """Handle completion or failure when build process terminates."""
        now = time.time()
        duration = now - session.start_time

        # Ensure build_dir and out_dir are accurately resolved to Android tree root
        session.build_dir = resolve_android_dir(session.build_dir) or session.build_dir
        if not os.path.isabs(session.out_dir) or not os.path.isdir(session.out_dir):
            session.out_dir = os.path.join(session.build_dir, "out")
            session.progress_pb_path = os.path.join(session.out_dir, "build_progress.pb")
            session.soong_log_path = os.path.join(session.out_dir, "soong.log")
            session.error_log_path = os.path.join(session.out_dir, "error.log")

        # Check if soong_log exists; if not, check for other out*/soong.log in build_dir
        if not os.path.isfile(session.soong_log_path) and os.path.isdir(session.build_dir):
            cand_soongs = glob.glob(os.path.join(session.build_dir, "out*/soong.log"))
            if cand_soongs:
                cand_soongs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                session.soong_log_path = cand_soongs[0]
                session.out_dir = os.path.dirname(cand_soongs[0])

        session.refresh_rom_info()

        # Check for build artifacts in out/target/product/<device>
        product_dir = os.path.join(session.out_dir, "target", "product", session.device)
        artifacts = discover_artifacts(product_dir, min_mtime=session.start_time)

        # Resilient Device & Artifact Auto-Discovery:
        # If product_dir produced 0 artifacts or device is generic, inspect out/target/product/*
        if not artifacts:
            # Check explicit OUT path from soong log if present
            explicit_out = session.rom_info.get("out")
            if explicit_out and os.path.isdir(explicit_out):
                cand_arts = discover_artifacts(explicit_out, min_mtime=session.start_time)
                if cand_arts:
                    product_dir = explicit_out
                    session.device = os.path.basename(explicit_out.rstrip("/\\"))
                    artifacts = cand_arts
                    logger.info("Discovered artifacts using explicit OUT path: %s (device: %s)", explicit_out, session.device)

            if not artifacts:
                product_base = os.path.join(session.out_dir, "target", "product")
                if os.path.isdir(product_base):
                    candidates = []
                    for d in os.listdir(product_base):
                        if d in ["generic", "obj", "common", "install", "symbols", "fake_packages", "temp", "tmp"]:
                            continue
                        if d.startswith("."):
                            continue
                        cand_path = os.path.join(product_base, d)
                        if os.path.isdir(cand_path):
                            candidates.append((d, cand_path, os.path.getmtime(cand_path)))

                    # Sort most recently modified device directory first
                    candidates.sort(key=lambda x: x[2], reverse=True)

                    # First pass: try with session.start_time
                    for cand_dev, cand_path, _ in candidates:
                        cand_artifacts = discover_artifacts(cand_path, min_mtime=session.start_time)
                        if cand_artifacts:
                            session.device = cand_dev
                            product_dir = cand_path
                            artifacts = cand_artifacts
                            logger.info("Auto-discovered device '%s' with %d artifacts at %s", cand_dev, len(artifacts), cand_path)
                            break

                    # Fallback pass: if still no artifacts, scan candidates without mtime filter
                    if not artifacts and candidates:
                        for cand_dev, cand_path, _ in candidates:
                            cand_artifacts = discover_artifacts(cand_path, min_mtime=None)
                            if cand_artifacts:
                                session.device = cand_dev
                                product_dir = cand_path
                                artifacts = cand_artifacts
                                logger.info("Auto-discovered device '%s' (fallback mtime) with %d artifacts at %s", cand_dev, len(artifacts), cand_path)
                                break

            # Deep search: inspect all out*/target/product/* inside build_dir
            if not artifacts and os.path.isdir(session.build_dir):
                cand_product_dirs = glob.glob(os.path.join(session.build_dir, "out*/target/product/*"))
                for cdir in sorted(cand_product_dirs, key=lambda p: os.path.getmtime(p), reverse=True):
                    if not os.path.isdir(cdir):
                        continue
                    bname = os.path.basename(cdir)
                    if bname in ["generic", "obj", "common", "install", "symbols", "fake_packages", "temp", "tmp"]:
                        continue
                    c_arts = discover_artifacts(cdir, min_mtime=session.start_time)
                    if not c_arts:
                        c_arts = discover_artifacts(cdir, min_mtime=None)
                    if c_arts:
                        session.device = bname
                        product_dir = cdir
                        artifacts = c_arts
                        logger.info("Auto-discovered product dir %s with %d artifacts (device: %s)", cdir, len(artifacts), bname)
                        break

        # Anti-spam: If process lived less than 30 seconds, has no artifacts, and sent no progress message, ignore it
        if duration < 30.0 and session.message_id is None and not artifacts:
            logger.info("Ignoring short-lived build PID %d (lived %.1fs, no artifacts)", session.pid, duration)
            return

        if self.is_hidden_rom_or_user(session.username, session.rom_title, session.build_dir):
            if session.chat_id != self.hidden_user_target_chat_id:
                session.chat_id = self.hidden_user_target_chat_id
        target_chat_id = self.get_target_chat_id(
            username=session.username,
            rom_title=session.rom_title,
            directory=session.build_dir,
            session_chat_id=session.chat_id,
            user_profile_chat_id=session.user_profile.get("chat_id"),
        )
        if not target_chat_id or not self.telegram:
            return

        # Check for error log
        has_error = False
        error_summary = ""
        error_file_to_send = None

        if (
            os.path.isfile(session.error_log_path)
            and os.path.getsize(session.error_log_path) > 0
            and os.path.getmtime(session.error_log_path) >= (session.start_time - 5)
        ):
            error_file_to_send = session.error_log_path
            try:
                with open(session.error_log_path, "r", errors="ignore") as f:
                    lines = f.readlines()
                    error_summary = "".join(lines[-25:]).strip()
                    has_error = True
            except Exception:
                pass
        elif not artifacts and os.path.isfile(session.soong_log_path):
            try:
                with open(session.soong_log_path, "r", errors="ignore") as f:
                    lines = f.readlines()
                    tail = lines[-60:]
                    if any("failed" in l.lower() or "error:" in l.lower() or "exit code" in l.lower() for l in tail):
                        error_summary = "".join(tail[-25:]).strip()
                        temp_log = f"/tmp/{session.device}_error.log"
                        with open(temp_log, "w", errors="ignore") as tf:
                            tf.writelines(lines[-300:])
                        error_file_to_send = temp_log
                        has_error = True
            except Exception:
                pass

        preview_url = session.get_ogpreview_url()
        total = session.last_progress.total_actions if session.last_progress else 0
        finished = session.last_progress.finished_actions if session.last_progress else 0

        # Succeeded if artifacts exist and no fatal error (a produced ROM ZIP implies successful build)
        has_rom_zip = any(a.get("type") == "ZIP" for a in artifacts)
        is_success = bool(artifacts) and (has_rom_zip or not has_error)

        if is_success:
            if finished == 0 and total == 0:
                finished = total = 231337
            elif total > 0:
                finished = total

            text = format_build_completed(
                preview_url=preview_url,
                rom_title=session.rom_title,
                device=session.device,
                user_name=session.user_profile.get("name", session.username),
                user_emoji=session.user_profile.get("emoji", "❄️"),
                start_time_epoch=session.start_time,
                duration_seconds=duration,
                finished_actions=finished,
                total_actions=total,
                variant=session.variant,
                build_type=session.build_type,
                release=session.release,
                artifacts=artifacts,
                system_stats=get_system_stats(product_dir),
                server_name=self.server_name,
            )
            logger.info("Build PID %d succeeded! Found %d artifacts.", session.pid, len(artifacts))

            reply_markup = make_build_completed_keyboard(session.device, self.server_name)

            if session.message_id:
                self.telegram.edit_message(
                    chat_id=session.chat_id,
                    message_id=session.message_id,
                    text=text,
                    parse_mode="HTML",
                    min_interval_seconds=0.0,
                    reply_markup=reply_markup,
                )
            else:
                self.telegram.send_message(
                    chat_id=target_chat_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
        else:
            if not error_summary:
                error_summary = "Build terminated unexpectedly without producing final artifacts."
            text = format_build_failed(
                preview_url=preview_url,
                rom_title=session.rom_title,
                device=session.device,
                user_name=session.user_profile.get("name", session.username),
                user_emoji=session.user_profile.get("emoji", "❄️"),
                start_time_epoch=session.start_time,
                duration_seconds=duration,
                finished_actions=finished,
                total_actions=total,
                variant=session.variant,
                build_type=session.build_type,
                release=session.release,
                error_summary=error_summary,
                server_name=self.server_name,
            )
            logger.warning("Build PID %d failed: %s", session.pid, error_summary[:100])

            status_msg_id = session.message_id
            if session.message_id:
                self.telegram.edit_message(
                    chat_id=session.chat_id,
                    message_id=session.message_id,
                    text=text,
                    parse_mode="HTML",
                    min_interval_seconds=0.0,
                )
            else:
                status_msg_id = self.telegram.send_message(
                    chat_id=target_chat_id,
                    text=text,
                    parse_mode="HTML",
                )

            # Send error.log document as attachment
            if error_file_to_send and os.path.isfile(error_file_to_send):
                self.telegram.send_document(
                    chat_id=target_chat_id,
                    file_path=error_file_to_send,
                    caption=f"⚠️ error.log for {session.rom_title} on {session.device}",
                    reply_to_message_id=status_msg_id,
                    custom_filename=f"{session.rom_title}_{session.device}_error.log",
                )

    def process_sync_step(self, session: RepoSyncSession):
        """Process a polling tick for an active repo sync."""
        now = time.time()
        elapsed = now - session.start_time

        # Anti-spam: Only start notifying if sync has run for at least 3 seconds
        if elapsed < 3.0:
            return

        synced = session.count_synced_projects()
        eta = session.calculate_eta(elapsed, synced)

        user_prof = resolve_user_profile(session.username)
        if self.is_hidden_rom_or_user(session.username, session.rom_title, session.repo_dir):
            if session.chat_id != self.hidden_user_target_chat_id:
                session.chat_id = self.hidden_user_target_chat_id
        target_chat_id = self.get_target_chat_id(
            username=session.username,
            rom_title=session.rom_title,
            directory=session.repo_dir,
            session_chat_id=session.chat_id,
            user_profile_chat_id=user_prof.get("chat_id"),
        )
        if not target_chat_id or not self.telegram:
            return

        preview_url = build_ogpreview_url(
            rom_title=session.rom_title,
            avatar_url=session.avatar_url,
            maintainer_name=user_prof.get("name", session.username),
            device=session.device,
            branch=session.branch,
            emoji=user_prof.get("emoji", "❄️"),
            theme=user_prof.get("theme", "midnightBloom"),
        )

        text = format_sync_in_progress(
            preview_url=preview_url,
            rom_title=session.rom_title,
            user_name=user_prof.get("name", session.username),
            user_emoji=user_prof.get("emoji", "❄️"),
            synced_projects=synced,
            total_projects=session.total_projects,
            elapsed_seconds=elapsed,
            eta_seconds=eta,
            branch=session.branch,
            remote=session.remote_url,
            system_stats=get_system_stats(session.repo_dir),
            server_name=self.server_name,
        )

        if session.message_id is None:
            msg_id = self.telegram.send_message(
                chat_id=target_chat_id,
                text=text,
                parse_mode="HTML",
            )
            if msg_id:
                session.message_id = msg_id
                session.chat_id = target_chat_id
                session.last_update_time = now
                self.save_state()
                logger.info("Sent repo sync progress message for PID %d (msg_id: %d)", session.pid, msg_id)
        else:
            if (now - session.last_update_time) >= self.update_interval:
                edited = self.telegram.edit_message(
                    chat_id=session.chat_id,
                    message_id=session.message_id,
                    text=text,
                    parse_mode="HTML",
                    min_interval_seconds=3.0,
                )
                if edited:
                    session.last_update_time = now

    def handle_sync_finished(self, session: RepoSyncSession):
        """Handle repo sync completion. ONLY sends message if in-progress was active."""
        if session.message_id is None:
            return

        now = time.time()
        duration = now - session.start_time
        user_prof = resolve_user_profile(session.username)
        if self.is_hidden_rom_or_user(session.username, session.rom_title, session.repo_dir):
            if session.chat_id != self.hidden_user_target_chat_id:
                session.chat_id = self.hidden_user_target_chat_id
        target_chat_id = self.get_target_chat_id(
            username=session.username,
            rom_title=session.rom_title,
            directory=session.repo_dir,
            session_chat_id=session.chat_id,
            user_profile_chat_id=user_prof.get("chat_id"),
        )
        if not target_chat_id or not self.telegram:
            return

        preview_url = build_ogpreview_url(
            rom_title=session.rom_title,
            avatar_url=session.avatar_url,
            maintainer_name=user_prof.get("name", session.username),
            device=session.device,
            branch=session.branch,
            emoji=user_prof.get("emoji", "❄️"),
            theme=user_prof.get("theme", "midnightBloom"),
        )

        text = format_sync_completed(
            preview_url=preview_url,
            rom_title=session.rom_title,
            user_name=user_prof.get("name", session.username),
            user_emoji=user_prof.get("emoji", "❄️"),
            start_time_epoch=session.start_time,
            duration_seconds=duration,
            total_projects=session.total_projects,
            branch=session.branch,
            remote=session.remote_url,
            server_name=self.server_name,
        )

        self.telegram.edit_message(
            chat_id=session.chat_id,
            message_id=session.message_id,
            text=text,
            parse_mode="HTML",
            min_interval_seconds=0.0,
        )
        logger.info("Completed repo sync PID %d.", session.pid)

    def process_kernel_step(self, session: KernelBuildSession):
        """Process a single polling tick for an active kernel compilation."""
        now = time.time()
        session.refresh_metadata()

        pct, compiled_objects, total_objects = session.calculate_progress()
        elapsed = now - session.start_time
        eta = session.calculate_eta(compiled_objects, total_objects)
        active_tasks = session.count_active_compiler_tasks()

        user_prof = session.user_profile
        if self.is_hidden_rom_or_user(session.username, session.kernel_title, session.kernel_dir):
            if session.chat_id != self.hidden_user_target_chat_id:
                session.chat_id = self.hidden_user_target_chat_id
        target_chat_id = self.get_target_chat_id(
            username=session.username,
            rom_title=session.kernel_title,
            directory=session.kernel_dir,
            session_chat_id=session.chat_id,
            user_profile_chat_id=user_prof.get("chat_id"),
        )
        if not target_chat_id or not self.telegram:
            return

        preview_url = session.get_ogpreview_url()
        text = format_kernel_build_in_progress(
            preview_url=preview_url,
            kernel_title=session.kernel_title,
            device=session.device_display,
            user_name=user_prof.get("name", session.username),
            user_emoji=user_prof.get("emoji", "🐱"),
            active_tasks=active_tasks,
            compiled_objects=compiled_objects,
            total_objects=total_objects,
            pct=pct,
            elapsed_seconds=elapsed,
            eta_seconds=eta,
            kernel_version=session.kernel_version,
            localversion=session.localversion,
            compiler_str=session.compiler_str,
            system_stats=get_system_stats(session.kernel_dir),
            server_name=self.server_name,
        )

        if session.message_id is None:
            # Anti-spam: Do not send initial message if session has run < 3 seconds
            if elapsed < 3.0:
                return
            msg_id = self.telegram.send_message(
                chat_id=target_chat_id,
                text=text,
                parse_mode="HTML",
            )
            if msg_id:
                session.message_id = msg_id
                session.chat_id = target_chat_id
                session.last_edit_time = now
                session.last_pct = pct
                self.save_state()
                logger.info("Sent kernel build progress message for PID %d [%s on %s] (msg_id: %d)", session.pid, session.kernel_title, session.device_display, msg_id)
        else:
            time_since_edit = now - session.last_edit_time
            pct_diff = abs(pct - session.last_pct)
            should_edit = (time_since_edit >= self.update_interval) or (pct_diff >= 1.0 and time_since_edit >= 3.0)

            if should_edit:
                edited = self.telegram.edit_message(
                    chat_id=session.chat_id,
                    message_id=session.message_id,
                    text=text,
                    parse_mode="HTML",
                    min_interval_seconds=3.0,
                )
                if edited:
                    session.last_edit_time = now
                    session.last_pct = pct

    async def handle_kernel_finished(self, session: KernelBuildSession):
        """Handle kernel build completion: auto-uploads release zip/boot images to GoFile and updates Telegram."""
        now = time.time()
        duration = now - session.start_time
        success = session.is_successful()

        user_prof = session.user_profile
        if self.is_hidden_rom_or_user(session.username, session.kernel_title, session.kernel_dir):
            if session.chat_id != self.hidden_user_target_chat_id:
                session.chat_id = self.hidden_user_target_chat_id
        target_chat_id = self.get_target_chat_id(
            username=session.username,
            rom_title=session.kernel_title,
            directory=session.kernel_dir,
            session_chat_id=session.chat_id,
            user_profile_chat_id=user_prof.get("chat_id"),
        )
        if not target_chat_id or not self.telegram:
            return

        preview_url = session.get_ogpreview_url()

        if success:
            artifacts = session.discover_artifacts()
            files_to_upload = [a["path"] for a in artifacts if os.path.isfile(a.get("path", ""))]

            download_url = None
            if files_to_upload:
                try:
                    logger.info("Auto-uploading %d kernel release artifacts to GoFile...", len(files_to_upload))
                    res = await asyncio.to_thread(
                        upload_artifacts_batch,
                        file_paths=files_to_upload,
                        token=self.global_config.get("gofile_token"),
                    )
                    if res.get("success") and res.get("download_page"):
                        download_url = res["download_page"]
                        logger.info("Uploaded kernel artifacts to GoFile: %s", download_url)
                except Exception as e:
                    logger.warning("Error auto-uploading kernel artifacts: %s", e)

            completed_text = format_kernel_build_completed(
                preview_url=preview_url,
                kernel_title=session.kernel_title,
                device=session.device_display,
                user_name=user_prof.get("name", session.username),
                user_emoji=user_prof.get("emoji", "🐱"),
                start_time_epoch=session.start_time,
                duration_seconds=duration,
                total_objects=session.total_objects,
                kernel_version=session.kernel_version,
                localversion=session.localversion,
                artifacts=artifacts,
                download_url=download_url,
                system_stats=get_system_stats(session.kernel_dir),
                server_name=self.server_name,
            )
            kb = make_kernel_completed_keyboard(download_url)
            if session.message_id:
                self.telegram.edit_message(
                    chat_id=session.chat_id,
                    message_id=session.message_id,
                    text=completed_text,
                    parse_mode="HTML",
                    min_interval_seconds=0.0,
                    reply_markup=kb,
                )
            else:
                self.telegram.send_message(
                    chat_id=target_chat_id,
                    text=completed_text,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            logger.info("Completed kernel build PID %d [%s on %s].", session.pid, session.kernel_title, session.device_display)
        else:
            if session.message_id is not None:
                # Anti-spam: If duration was very short (< 30s) and 0 objects compiled, delete ephemeral message rather than reporting failure
                if duration < 30.0 and session.last_compiled_count == 0:
                    logger.info("Deleting ephemeral kernel progress message (PID %d, lived %.1fs, 0 objects)", session.pid, duration)
                    try:
                        self.telegram.delete_message(chat_id=session.chat_id, message_id=session.message_id)
                    except Exception:
                        pass
                    return

                error_file = None
                error_summary = "Kernel build stopped or failed before producing kernel image."
                home = session.user_profile.get("home") or f"/home/{session.username}"
                candidate_logs = [
                    os.path.join(session.out_dir, "error.log"),
                    os.path.join(home, "genkz.log"),
                    os.path.join(session.kernel_dir, "error.log"),
                ]
                for clog in candidate_logs:
                    if os.path.isfile(clog) and os.path.getmtime(clog) >= (session.start_time - 10):
                        try:
                            with open(clog, "r", errors="ignore") as f:
                                llines = f.readlines()
                                if any("error:" in l.lower() or "failed" in l.lower() for l in llines[-80:]):
                                    error_summary = "".join(llines[-25:]).strip()
                                    error_file = clog
                                    break
                        except Exception:
                            pass

                failed_text = format_kernel_build_failed(
                    preview_url=preview_url,
                    kernel_title=session.kernel_title,
                    device=session.device_display,
                    user_name=user_prof.get("name", session.username),
                    user_emoji=user_prof.get("emoji", "🐱"),
                    start_time_epoch=session.start_time,
                    duration_seconds=duration,
                    compiled_objects=session.last_compiled_count,
                    total_objects=session.total_objects,
                    kernel_version=session.kernel_version,
                    error_summary=error_summary,
                    server_name=self.server_name,
                )
                self.telegram.edit_message(
                    chat_id=session.chat_id,
                    message_id=session.message_id,
                    text=failed_text,
                    parse_mode="HTML",
                    min_interval_seconds=0.0,
                )
                if error_file:
                    self.telegram.send_document(
                        chat_id=session.chat_id,
                        file_path=error_file,
                        caption=f"⚠️ error.log for {session.kernel_title} on {session.device_display}",
                        reply_to_message_id=session.message_id,
                        custom_filename=f"{session.kernel_title}_{session.device}_error.log",
                    )
                logger.warning("Kernel build PID %d failed.", session.pid)

    async def start_gofile_upload(self, chat_id: str, rom: Dict[str, Any]):
        """Upload all files of a ROM to GoFile with real-time Telegram progress updates and cancel support."""
        rom_name = rom.get("name", "Custom ROM")
        device = rom.get("device", "")
        files_to_upload = rom.get("files_to_upload", [])

        if not files_to_upload:
            self.telegram.send_message(chat_id, f"❌ No files found to upload for {rom_name}.")
            return

        total_files = len(files_to_upload)
        total_bytes = sum(os.path.getsize(p) for p in files_to_upload if os.path.isfile(p))
        upload_id = f"up_{int(time.time() * 1000) % 10000000}"
        cancel_event = threading.Event()
        active_procs: List[Any] = []
        self.active_uploads[upload_id] = {
            "cancel_event": cancel_event,
            "active_procs": active_procs,
            "chat_id": chat_id,
            "rom_name": rom_name,
        }
        uploading_kb = make_gofile_uploading_keyboard(upload_id)

        # Send initial progress message
        first_file = os.path.basename(files_to_upload[0])
        progress_text = format_gofile_upload_progress(
            rom_name=rom_name,
            device=device,
            pct=0.0,
            transferred_bytes=0,
            total_bytes=total_bytes,
            speed_mb_s=0.0,
            eta_seconds=None,
            completed_files=0,
            total_files=total_files,
            current_filename=first_file,
        )
        status_msg_id = self.telegram.send_message(
            chat_id=chat_id,
            text=progress_text,
            parse_mode="HTML",
            reply_markup=uploading_kb,
        )

        last_update_time = [time.time()]

        def on_progress(pinfo: Dict[str, Any]):
            if cancel_event.is_set():
                return
            now = time.time()
            # Throttle Telegram message edits to at least 2.5 seconds
            if (now - last_update_time[0]) >= 2.5 or pinfo.get("is_final"):
                last_update_time[0] = now
                text = format_gofile_upload_progress(
                    rom_name=rom_name,
                    device=device,
                    pct=pinfo.get("pct", 0.0),
                    transferred_bytes=pinfo.get("transferred_bytes", 0),
                    total_bytes=pinfo.get("total_bytes", total_bytes),
                    speed_mb_s=pinfo.get("speed_mb_s", 0.0),
                    eta_seconds=pinfo.get("eta_seconds"),
                    completed_files=pinfo.get("completed_files", 0),
                    total_files=pinfo.get("total_files", total_files),
                    current_filename=pinfo.get("current_filename"),
                )
                if status_msg_id and not cancel_event.is_set():
                    self.telegram.edit_message(
                        chat_id=chat_id,
                        message_id=status_msg_id,
                        text=text,
                        parse_mode="HTML",
                        min_interval_seconds=2.0,
                        reply_markup=uploading_kb,
                    )

        try:
            # Run multi-file streaming upload in background worker thread
            res = await asyncio.to_thread(
                upload_artifacts_batch,
                file_paths=files_to_upload,
                token=self.global_config.get("gofile_token"),
                progress_callback=on_progress,
                cancel_event=cancel_event,
                active_procs=active_procs,
            )

            if cancel_event.is_set() or res.get("cancelled"):
                cancel_text = (
                    f"🚫 <b>Upload Cancelled</b> for <b>{rom_name}</b>"
                    + (f" on <b>{device}</b>" if device else "")
                    + "\n\n<i>The upload to GoFile was stopped. No incomplete files published.</i>"
                )
                cancel_kb = {
                    "inline_keyboard": [
                        [{"text": "📂 Browse ROMs", "callback_data": "rescan_roms"}]
                    ]
                }
                if status_msg_id:
                    self.telegram.edit_message(
                        chat_id=chat_id,
                        message_id=status_msg_id,
                        text=cancel_text,
                        parse_mode="HTML",
                        min_interval_seconds=0.0,
                        reply_markup=cancel_kb,
                    )
                logger.info("GoFile upload %s cancelled by user request.", upload_id)
                return

            if res.get("success") and res.get("download_page"):
                completed_text = format_gofile_upload_completed(
                    rom_name=rom_name,
                    device=device,
                    download_url=res["download_page"],
                    uploaded_files=res["uploaded_files"],
                    failed_files=res.get("failed_files"),
                    duration_seconds=res.get("duration_seconds"),
                    avg_speed_mb_s=res.get("avg_speed_mb_s"),
                )
                open_kb = make_gofile_open_keyboard(res["download_page"])
                if status_msg_id:
                    self.telegram.edit_message(
                        chat_id=chat_id,
                        message_id=status_msg_id,
                        text=completed_text,
                        parse_mode="HTML",
                        min_interval_seconds=0.0,
                        reply_markup=open_kb,
                    )
                else:
                    self.telegram.send_message(
                        chat_id=chat_id,
                        text=completed_text,
                        parse_mode="HTML",
                        reply_markup=open_kb,
                    )
                logger.info("Uploaded %d files for %s to GoFile: %s", len(res["uploaded_files"]), rom_name, res["download_page"])

                # If upload was triggered from private admin DM, also broadcast completion and download links to the group (unless hidden)
                group_id = str(self.group_chat_id or self.default_chat_id)
                is_hidden = self.is_hidden_rom_or_user(
                    username=rom.get("username"),
                    rom_title=rom.get("name"),
                    directory=rom.get("directory"),
                )
                if not is_hidden and chat_id != group_id and group_id:
                    try:
                        self.telegram.send_message(
                            chat_id=group_id,
                            text=completed_text,
                            parse_mode="HTML",
                            reply_markup=open_kb,
                        )
                    except Exception as e:
                        logger.warning("Failed to broadcast GoFile release to group %s: %s", group_id, e)
            else:
                err_msg = f"❌ <b>GoFile Upload Failed</b> for {rom_name}.\n\nPlease check server network connectivity."
                if status_msg_id:
                    self.telegram.edit_message(
                        chat_id=chat_id,
                        message_id=status_msg_id,
                        text=err_msg,
                        parse_mode="HTML",
                        min_interval_seconds=0.0,
                    )
                else:
                    self.telegram.send_message(chat_id=chat_id, text=err_msg, parse_mode="HTML")
        finally:
            self.active_uploads.pop(upload_id, None)

    def get_local_status(self, is_allowed_hidden: bool = True) -> Dict[str, Any]:
        """Collect local server resources and active sessions."""
        if is_allowed_hidden:
            builds = self.active_builds
            kernels = self.active_kernels
            syncs = self.active_syncs
        else:
            builds = {
                pid: s for pid, s in self.active_builds.items()
                if not self.is_hidden_rom_or_user(s.username, s.rom_title, getattr(s, "build_dir", None))
            }
            kernels = {
                pid: s for pid, s in self.active_kernels.items()
                if not self.is_hidden_rom_or_user(s.username, s.kernel_title, getattr(s, "kernel_dir", None))
            }
            syncs = {
                pid: s for pid, s in self.active_syncs.items()
                if not self.is_hidden_rom_or_user(s.username, s.rom_title, getattr(s, "repo_dir", None))
            }

        return {
            "server_name": self.server_name or socket.gethostname(),
            "online": True,
            "system_stats": get_system_stats(),
            "builds": [
                {
                    "pid": pid,
                    "rom_title": s.rom_title,
                    "device": s.device,
                    "username": s.username,
                    "pct": s.last_pct,
                    "is_hidden": self.is_hidden_rom_or_user(s.username, s.rom_title, getattr(s, "build_dir", None)),
                }
                for pid, s in builds.items()
            ],
            "kernels": [
                {
                    "pid": pid,
                    "kernel_title": s.kernel_title,
                    "device": s.device_display,
                    "username": s.username,
                    "pct": s.last_pct,
                    "is_hidden": self.is_hidden_rom_or_user(s.username, s.kernel_title, getattr(s, "kernel_dir", None)),
                }
                for pid, s in kernels.items()
            ],
            "syncs": [
                {
                    "pid": pid,
                    "rom_title": s.rom_title,
                    "branch": s.branch,
                    "username": s.username,
                    "synced_projects": s.count_synced_projects() if hasattr(s, "count_synced_projects") else 0,
                    "total_projects": s.total_projects,
                    "is_hidden": self.is_hidden_rom_or_user(s.username, s.rom_title, getattr(s, "repo_dir", None)),
                }
                for pid, s in syncs.items()
            ],
        }

    async def fetch_peer_status(self, peer: Dict[str, Any], is_allowed_hidden: bool = True) -> Dict[str, Any]:
        """Fetch status JSON from a peer server asynchronously."""
        def _fetch():
            url = peer.get("url", "")
            name = peer.get("name") or url
            secret = peer.get("secret_key") or peer.get("secret") or self.secret_key
            req = urllib.request.Request(url)
            if secret:
                req.add_header("X-AOSP-Secret", secret)
            try:
                with urllib.request.urlopen(req, timeout=3.5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    data["online"] = True
                    if not data.get("server_name"):
                        data["server_name"] = name
                    return data
            except Exception as e:
                logger.warning("Peer %s (%s) unreachable: %s", name, url, e)
                return {
                    "server_name": name,
                    "online": False,
                    "error": str(e),
                    "builds": [],
                    "kernels": [],
                    "syncs": [],
                }

        result = await asyncio.to_thread(_fetch)
        if not is_allowed_hidden and result.get("online"):
            result["builds"] = [b for b in result.get("builds", []) if not b.get("is_hidden")]
            result["kernels"] = [k for k in result.get("kernels", []) if not k.get("is_hidden")]
            result["syncs"] = [s for s in result.get("syncs", []) if not s.get("is_hidden")]
        return result

    def get_local_roms(self) -> List[Dict[str, Any]]:
        """Return list of local ROMs formatted for multi-server aggregation."""
        roms = scan_all_roms(force_refresh=True)
        s_name = self.server_name or socket.gethostname()
        s_slug = re.sub(r'[^a-zA-Z0-9_-]', '_', s_name.lower())
        results = []
        for r in roms:
            r_copy = dict(r)
            r_copy["server_name"] = s_name
            if not str(r_copy.get("id", "")).startswith(f"{s_slug}_"):
                r_copy["id"] = f"{s_slug}_{r_copy.get('id', 'rom')}"
            results.append(r_copy)
        return results

    async def get_all_multi_server_roms(self) -> List[Dict[str, Any]]:
        """Scan local ROMs and fetch ROMs from all configured peer servers."""
        local_roms = await asyncio.to_thread(self.get_local_roms)
        if not self.peer_servers:
            self.cached_multi_roms = local_roms
            return local_roms

        async def fetch_peer_roms(peer: Dict[str, Any]) -> List[Dict[str, Any]]:
            peer_url = peer.get("url", "").rstrip("/")
            if peer_url.endswith("/status"):
                peer_url = peer_url[:-7]
            secret = peer.get("secret_key") or peer.get("secret") or self.secret_key
            try:
                req = urllib.request.Request(
                    f"{peer_url}/api/roms",
                    headers={"X-AOSP-Secret": secret} if secret else {},
                )
                resp = await asyncio.to_thread(urllib.request.urlopen, req, timeout=12.0)
                data = json.loads(resp.read().decode("utf-8"))
                peer_roms = data.get("roms", [])
                p_name = peer.get("name") or "Peer Server"
                p_slug = re.sub(r'[^a-zA-Z0-9_-]', '_', p_name.lower())
                for pr in peer_roms:
                    pr["server_name"] = p_name
                    if not str(pr.get("id", "")).startswith(f"{p_slug}_"):
                        pr["id"] = f"{p_slug}_{pr.get('id', 'rom')}"
                return peer_roms
            except Exception as e:
                logger.debug("Failed to fetch ROMs from peer %s: %s", peer.get("name"), e)
                return []

        peer_rom_lists = await asyncio.gather(*[fetch_peer_roms(p) for p in self.peer_servers])
        all_roms = list(local_roms)
        for r_list in peer_rom_lists:
            all_roms.extend(r_list)
        self.cached_multi_roms = all_roms
        return all_roms

    def handle_remote_upload_request(self, device: str, chat_id: str, rom_id: Optional[str] = None) -> Dict[str, Any]:
        """Triggered via HTTP API from peer server to start a GoFile upload of local artifacts."""
        try:
            roms = scan_all_roms(force_refresh=True)
            target_rom = None
            if rom_id:
                clean_id = rom_id.split("_", 2)[-1] if rom_id.count("_") >= 2 else rom_id
                for r in roms:
                    r_id = str(r.get("id", ""))
                    if r_id == rom_id or r_id == clean_id or rom_id.endswith(f"_{r_id}"):
                        target_rom = r
                        break
            if not target_rom and device:
                d_low = device.lower().strip()
                for r in roms:
                    if r.get("device", "").lower() == d_low and r.get("type") == "build":
                        target_rom = r
                        break
                if not target_rom:
                    for r in roms:
                        if r.get("device", "").lower() == d_low:
                            target_rom = r
                            break
                if not target_rom:
                    for r in roms:
                        if d_low in r.get("main_zip_name", "").lower() or d_low in r.get("directory", "").lower():
                            target_rom = r
                            break

            if target_rom:
                logger.info("Executing remote-requested GoFile upload for %s (%s) to chat %s", target_rom.get("name"), device, chat_id)
                if self.loop and self.loop.is_running():
                    asyncio.run_coroutine_threadsafe(self.start_gofile_upload(chat_id, target_rom), self.loop)
                else:
                    asyncio.create_task(self.start_gofile_upload(chat_id, target_rom))
                return {"success": True, "message": f"Upload started for {device} on {self.server_name}"}

            logger.warning("Remote upload requested for device '%s' but no artifacts found on %s", device, self.server_name)
            return {"success": False, "message": f"No artifacts found for {device} on {self.server_name}"}
        except Exception as e:
            logger.exception("Error in handle_remote_upload_request: %s", e)
            return {"success": False, "message": f"Error scanning artifacts on {self.server_name}: {e}"}

    def handle_remote_cancel_request(self, upload_id: str) -> Dict[str, Any]:
        """Triggered via HTTP API to cancel an active upload running on this node."""
        upload_entry = self.active_uploads.get(upload_id)
        if upload_entry:
            upload_entry["cancel_event"].set()
            for proc in list(upload_entry.get("active_procs", [])):
                try:
                    proc.kill()
                except Exception:
                    pass
            return {"success": True, "message": "Upload cancelled"}
        return {"success": False, "message": "Upload not found"}

    async def handle_telegram_message(self, msg: Dict[str, Any]):
        """Handle incoming chat messages and slash commands."""
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id"))
        chat_type = chat.get("type", "private")
        from_user = msg.get("from", {})
        from_id = str(from_user.get("id", ""))
        text = msg.get("text", "").strip()
        user_name = from_user.get("first_name", "Developer")

        # In groups or DMs, only process slash commands
        if not text.startswith("/"):
            return

        tokens = text.split()
        cmd_parts = tokens[0].split("@")
        cmd = cmd_parts[0].lower()
        if len(cmd_parts) > 1 and self.bot_username:
            if cmd_parts[1].lower() != self.bot_username.lower():
                return

        logger.info("Telegram command %s from %s (%s, chat: %s, type: %s)", cmd, user_name, from_id, chat_id, chat_type)

        # 1. Access Control: Private DMs restricted strictly to authorized admin IDs (6992001967, 6340997509)
        if chat_type == "private" and from_id not in self.admin_ids:
            logger.warning("Access denied: User %s (%s) tried to access bot in DM.", from_id, user_name)
            self.telegram.send_message(
                chat_id=chat_id,
                text="⛔ <b>Access Denied</b>\n\nYou are not authorized to use this bot in private chat.",
                parse_mode="HTML",
            )
            return

        # 2. Access Control: Direct upload command (/download or /upload) restricted to admins
        if cmd in ["/download", "/upload"]:
            if from_id not in self.admin_ids:
                logger.warning("Access restricted: Non-admin %s tried %s in chat %s", from_id, cmd, chat_id)
                self.telegram.send_message(
                    chat_id=chat_id,
                    text="⚠️ <b>Access Restricted</b>\n\nOnly bot administrators can initiate GoFile uploads to prevent server overload.",
                    parse_mode="HTML",
                )
                return

        if cmd in ["/start", "/roms", "/download", "/upload", "/search"]:
            all_roms = await self.get_all_multi_server_roms()
            is_private_admin_dl = (cmd in ["/download", "/upload"] and chat_id == self.hidden_user_target_chat_id)
            if not is_private_admin_dl:
                all_roms = [
                    r for r in all_roms
                    if not self.is_hidden_rom_or_user(r.get("username"), r.get("name"), r.get("directory"))
                ]
            list_text = format_rom_list(all_roms)
            kb = make_rom_list_keyboard(all_roms)
            self.telegram.send_message(chat_id, list_text, parse_mode="HTML", reply_markup=kb)

        elif cmd == "/kernels":
            all_roms = await self.get_all_multi_server_roms()
            all_roms = [
                r for r in all_roms
                if not self.is_hidden_rom_or_user(r.get("username"), r.get("name"), r.get("directory"))
            ]
            filtered_roms = [r for r in all_roms if r.get("type") == "kernel"]
            list_text = format_rom_list(filtered_roms)
            kb = make_rom_list_keyboard(filtered_roms)
            self.telegram.send_message(chat_id, list_text, parse_mode="HTML", reply_markup=kb)

        elif cmd == "/status":
            is_allowed_hidden = (chat_id == self.hidden_user_target_chat_id or from_id == self.hidden_user_target_chat_id)
            local_summary = self.get_local_status(is_allowed_hidden=is_allowed_hidden)

            if self.peer_servers:
                peer_results = await asyncio.gather(*[
                    self.fetch_peer_status(peer, is_allowed_hidden=is_allowed_hidden)
                    for peer in self.peer_servers
                ])
                all_servers = [local_summary] + list(peer_results)
            else:
                all_servers = [local_summary]

            status_text = format_multi_server_status(all_servers)
            self.telegram.send_message(chat_id, status_text, parse_mode="HTML")

        elif cmd in ["/restart", "/reload"]:
            if from_id not in self.admin_ids:
                return
            self.telegram.send_message(chat_id, "🔄 <b>Restarting AOSP Tracker service...</b>", parse_mode="HTML")
            self.save_state()
            os._exit(0)

        elif cmd == "/help":
            help_text = (
                "🤖 <b>AOSP Tracker & Downloader Bot</b>\n\n"
                "<b>Commands:</b>\n"
                "• /roms — Search for all ROMs and Kernels on server & download via GoFile\n"
                "• /kernels — Search for all Kernel releases on server & download via GoFile\n"
                "• /download — Browse available ROMs & Kernels and upload files to GoFile (Admin only)\n"
                "• /status — Check live ROM builds, kernel builds, and source syncs\n"
                "• /help — Show bot commands and information\n\n"
                "💡 <i>Whenever an AOSP ROM or Kernel build finishes, GoFile download links are automatically generated!</i>"
            )
            self.telegram.send_message(chat_id, help_text, parse_mode="HTML")

    async def handle_telegram_callback(self, cb: Dict[str, Any]):
        """Handle incoming inline button clicks."""
        cb_id = cb["id"]
        data = cb.get("data", "")
        message = cb.get("message", {})
        chat = message.get("chat", {})
        chat_id = str(chat.get("id"))
        chat_type = chat.get("type", "private")
        from_user = cb.get("from", {})
        from_id = str(from_user.get("id", ""))
        from_name = from_user.get("first_name", "User")
        msg_id = message.get("message_id")

        logger.info("Callback query from user %s (%s) in chat %s: %s", from_name, from_id, chat_id, data)

        # 1. Access Control: Private DMs restricted strictly to authorized admin IDs
        if chat_type == "private" and from_id not in self.admin_ids:
            self.telegram.answer_callback_query(
                cb_id,
                text="⛔ Access Denied: You are not authorized to use this bot.",
                show_alert=True,
            )
            return

        # 2. Access Control: Group members CANNOT trigger GoFile uploads or cancel them
        if data.startswith(("dl:", "upload_build:", "cancel_upload:")):
            if from_id not in self.admin_ids:
                logger.warning("Blocked upload action '%s' from non-admin user %s (%s) in chat %s", data, from_id, from_name, chat_id)
                self.telegram.answer_callback_query(
                    cb_id,
                    text="⚠️ Only bot administrators can upload ROMs to GoFile to prevent overloading the server.",
                    show_alert=True,
                )
                return

        if data == "rescan_roms":
            self.telegram.answer_callback_query(cb_id, text="Scanning server for ROMs...")
            roms = await self.get_all_multi_server_roms()
            if chat_id != self.hidden_user_target_chat_id:
                roms = [
                    r for r in roms
                    if not self.is_hidden_rom_or_user(r.get("username"), r.get("name"), r.get("directory"))
                ]
            list_text = format_rom_list(roms)
            kb = make_rom_list_keyboard(roms)
            self.telegram.edit_message(
                chat_id=chat_id,
                message_id=msg_id,
                text=list_text,
                parse_mode="HTML",
                min_interval_seconds=0.0,
                reply_markup=kb,
            )

        elif data.startswith("dl:"):
            rom_id = data[3:]
            rom = None
            for r in self.cached_multi_roms:
                if r.get("id") == rom_id:
                    rom = r
                    break
            if not rom:
                rom = get_rom_by_id(rom_id)
            if not rom:
                all_roms = await self.get_all_multi_server_roms()
                for r in all_roms:
                    if r.get("id") == rom_id:
                        rom = r
                        break

            if rom:
                if self.is_hidden_rom_or_user(rom.get("username"), rom.get("name"), rom.get("directory")):
                    if chat_id != self.hidden_user_target_chat_id:
                        self.telegram.answer_callback_query(cb_id, text="⛔ ROM not found or unavailable.", show_alert=True)
                        return

                target_server = rom.get("server_name")
                # If target server is a peer server, delegate the upload to the peer
                if target_server and target_server != self.server_name and self.peer_servers:
                    self.telegram.answer_callback_query(cb_id, text=f"Uploading {rom['name']} via {target_server}...")
                    for peer in self.peer_servers:
                        p_name = peer.get("name") or peer.get("url")
                        if p_name.lower() == target_server.lower() or peer.get("url", "").lower() == target_server.lower():
                            peer_url = peer.get("url", "").rstrip("/")
                            if peer_url.endswith("/status"):
                                peer_url = peer_url[:-7]
                            secret = peer.get("secret_key") or peer.get("secret") or self.secret_key
                            try:
                                req_data = json.dumps({"rom_id": rom_id, "chat_id": chat_id, "device": rom.get("device")}).encode("utf-8")
                                req = urllib.request.Request(
                                    f"{peer_url}/api/upload",
                                    data=req_data,
                                    headers={"Content-Type": "application/json", "X-AOSP-Secret": secret} if secret else {"Content-Type": "application/json"},
                                    method="POST",
                                )
                                await asyncio.to_thread(urllib.request.urlopen, req, timeout=10.0)
                                return
                            except Exception as e:
                                logger.warning("Failed to delegate dl upload to %s: %s", target_server, e)
                                break

                # Local upload
                self.telegram.answer_callback_query(cb_id, text=f"Uploading {rom['name']} to GoFile...")
                asyncio.create_task(self.start_gofile_upload(chat_id, rom))
            else:
                self.telegram.answer_callback_query(cb_id, text="ROM not found. Please click Rescan Server.", show_alert=True)

        elif data.startswith("upload_build:"):
            raw_target = data.split(":", 1)[1]
            target_peer = None
            if "@" in raw_target:
                device, target_peer = raw_target.split("@", 1)
            else:
                device = raw_target

            device_clean = device.strip()
            d_low = device_clean.lower()

            # 1. Search locally on this server first
            roms = await asyncio.to_thread(scan_all_roms, True)
            target_rom = None
            for r in roms:
                if r.get("device", "").lower() == d_low and r.get("type") == "build":
                    target_rom = r
                    break
            if not target_rom:
                for r in roms:
                    if r.get("device", "").lower() == d_low:
                        target_rom = r
                        break
            if not target_rom:
                for r in roms:
                    if d_low in r.get("main_zip_name", "").lower() or d_low in r.get("directory", "").lower():
                        target_rom = r
                        break

            if target_rom:
                if self.is_hidden_rom_or_user(target_rom.get("username"), target_rom.get("name"), target_rom.get("directory")):
                    if chat_id != self.hidden_user_target_chat_id:
                        self.telegram.answer_callback_query(cb_id, text=f"⛔ Artifacts not found for {device_clean}.", show_alert=True)
                        return
                self.telegram.answer_callback_query(cb_id, text=f"Uploading files for {device_clean} to GoFile...")
                asyncio.create_task(self.start_gofile_upload(chat_id, target_rom))
                return

            # 2. Not found locally! Delegate to peer workers
            if self.peer_servers:
                self.telegram.answer_callback_query(cb_id, text=f"Searching artifacts for {device_clean} on worker servers...")
                delegated = False
                for peer in self.peer_servers:
                    peer_url = peer.get("url", "").rstrip("/")
                    if peer_url.endswith("/status"):
                        peer_url = peer_url[:-7]
                    peer_name = peer.get("name", "Peer Server")
                    if target_peer and peer_name.lower() != target_peer.lower():
                        continue

                    secret = peer.get("secret_key") or peer.get("secret") or self.secret_key
                    try:
                        req_data = json.dumps({"device": device_clean, "chat_id": chat_id}).encode("utf-8")
                        req = urllib.request.Request(
                            f"{peer_url}/api/upload",
                            data=req_data,
                            headers={
                                "Content-Type": "application/json",
                                "X-AOSP-Secret": secret,
                            } if secret else {"Content-Type": "application/json"},
                            method="POST",
                        )
                        resp = await asyncio.to_thread(urllib.request.urlopen, req, timeout=10.0)
                        resp_data = json.loads(resp.read().decode("utf-8"))
                        if resp_data.get("success"):
                            delegated = True
                            logger.info("Successfully delegated upload for %s to %s (%s)", device_clean, peer_name, peer_url)
                            break
                    except Exception as e:
                        logger.warning("Peer %s upload delegation error: %s", peer_name, e)

                if delegated:
                    return

            self.telegram.answer_callback_query(cb_id, text=f"No artifacts found for {device_clean} on any server.", show_alert=True)

        elif data.startswith("cancel_upload:"):
            upload_id = data.split(":", 1)[1]
            upload_entry = self.active_uploads.get(upload_id)
            if upload_entry:
                upload_entry["cancel_event"].set()
                for proc in list(upload_entry.get("active_procs", [])):
                    try:
                        proc.kill()
                    except Exception:
                        pass
                self.telegram.answer_callback_query(cb_id, text="Cancelling upload...")
                logger.info("Signaled cancel for upload %s", upload_id)
            else:
                # Forward cancel to peer workers
                if self.peer_servers:
                    for peer in self.peer_servers:
                        peer_url = peer.get("url", "").rstrip("/")
                        if peer_url.endswith("/status"):
                            peer_url = peer_url[:-7]
                        secret = peer.get("secret_key") or peer.get("secret") or self.secret_key
                        try:
                            req_data = json.dumps({"upload_id": upload_id}).encode("utf-8")
                            req = urllib.request.Request(
                                f"{peer_url}/api/cancel_upload",
                                data=req_data,
                                headers={
                                    "Content-Type": "application/json",
                                    "X-AOSP-Secret": secret,
                                } if secret else {"Content-Type": "application/json"},
                                method="POST",
                            )
                            await asyncio.to_thread(urllib.request.urlopen, req, timeout=3.0)
                        except Exception:
                            pass
                self.telegram.answer_callback_query(cb_id, text="Upload cancel requested.")

    async def poll_telegram_updates(self):
        """Long poll Telegram getUpdates to handle incoming commands and inline button callbacks."""
        logger.info("Telegram updates polling loop started.")
        while True:
            try:
                if not self.telegram:
                    await asyncio.sleep(5)
                    continue

                updates = await asyncio.to_thread(self.telegram.get_updates, self.update_offset, 20)
                for update in updates:
                    self.update_offset = update["update_id"] + 1

                    if "message" in update:
                        asyncio.create_task(self.handle_telegram_message(update["message"]))
                    elif "callback_query" in update:
                        asyncio.create_task(self.handle_telegram_callback(update["callback_query"]))

            except Exception as e:
                logger.debug("Exception in Telegram updates polling: %s", e)
                await asyncio.sleep(2)

    async def monitor_loop(self):
        """Continuous polling loop for AOSP build and sync processes."""
        while True:
            try:
                self.reload_config()

                # 1. Check for running builds
                current_build_procs = self.find_build_processes()
                active_build_pids = set()
                active_build_dirs = {sess.build_dir for sess in self.active_builds.values()}

                for pid, username, build_dir, create_time in current_build_procs:
                    active_build_pids.add(pid)
                    if pid not in self.active_builds:
                        if build_dir in active_build_dirs:
                            # A build session for this workspace is already active
                            continue
                        logger.info("Detected new AOSP build: PID %d (user: %s, dir: %s)", pid, username, build_dir)
                        self.active_builds[pid] = BuildSession(pid, username, build_dir, create_time)
                        active_build_dirs.add(build_dir)
                        self.save_state()
                    else:
                        sess = self.active_builds[pid]
                        if sess.build_dir != build_dir or not os.path.exists(sess.progress_pb_path):
                            sess.build_dir = build_dir
                            sess.out_dir = os.path.join(build_dir, "out")
                            sess.progress_pb_path = os.path.join(sess.out_dir, "build_progress.pb")
                            sess.soong_log_path = os.path.join(sess.out_dir, "soong.log")
                            sess.error_log_path = os.path.join(sess.out_dir, "error.log")
                            sess.refresh_rom_info()
                            self.save_state()

                    self.process_build_step(self.active_builds[pid])

                # Check for finished builds
                finished_build_pids = [pid for pid in self.active_builds if pid not in active_build_pids]
                for pid in finished_build_pids:
                    session = self.active_builds.pop(pid)
                    self.save_state()
                    logger.info("AOSP build PID %d finished.", pid)
                    self.handle_build_finished(session)

                # 2. Check for running syncs
                current_sync_procs = self.find_sync_processes()
                active_sync_pids = set()
                active_sync_dirs = {os.path.realpath(sess.repo_dir) for sess in self.active_syncs.values()}

                for pid, username, repo_dir, create_time in current_sync_procs:
                    active_sync_pids.add(pid)
                    real_repo_dir = os.path.realpath(repo_dir)
                    if pid not in self.active_syncs:
                        if real_repo_dir in active_sync_dirs:
                            # A sync session for this repository tree is already active
                            continue
                        logger.info("Detected repo sync: PID %d (user: %s, dir: %s)", pid, username, repo_dir)
                        self.active_syncs[pid] = RepoSyncSession(pid, username, repo_dir, create_time)
                        active_sync_dirs.add(real_repo_dir)
                        self.save_state()

                    self.process_sync_step(self.active_syncs[pid])

                # Check for finished syncs
                finished_sync_pids = [pid for pid in self.active_syncs if pid not in active_sync_pids]
                for pid in finished_sync_pids:
                    session = self.active_syncs.pop(pid)
                    self.save_state()
                    logger.info("repo sync PID %d finished.", pid)
                    self.handle_sync_finished(session)

                # 3. Check for running kernel builds
                current_kernel_procs = self.find_kernel_build_processes()
                active_kernel_pids = set()
                active_kernel_dirs = {os.path.realpath(sess.out_dir) for sess in self.active_kernels.values()}

                for pid, username, kdir, out_dir, create_time in current_kernel_procs:
                    active_kernel_pids.add(pid)
                    real_out = os.path.realpath(out_dir)
                    if pid not in self.active_kernels:
                        if real_out in active_kernel_dirs:
                            # A kernel build session for this out_dir is already active
                            continue
                        logger.info("Detected new kernel build: PID %d (user: %s, dir: %s, out: %s)", pid, username, kdir, out_dir)
                        self.active_kernels[pid] = KernelBuildSession(pid, username, kdir, out_dir, create_time)
                        active_kernel_dirs.add(real_out)
                        self.save_state()

                    self.process_kernel_step(self.active_kernels[pid])

                # Check for finished kernel builds
                finished_kernel_pids = [pid for pid in self.active_kernels if pid not in active_kernel_pids]
                for pid in finished_kernel_pids:
                    session = self.active_kernels.pop(pid)
                    self.save_state()
                    logger.info("Kernel build PID %d finished.", pid)
                    asyncio.create_task(self.handle_kernel_finished(session))

            except Exception as e:
                logger.error("Exception in monitor loop: %s", e, exc_info=True)

            await asyncio.sleep(self.poll_interval)

    async def run(self):
        """Main async daemon runner with concurrent monitoring, Status API, and Telegram polling."""
        self.loop = asyncio.get_running_loop()
        node_name = self.server_name or socket.gethostname()
        logger.info("AOSP Tracker Daemon started on node %s.", node_name)

        if self.enable_api:
            self.status_server = StatusServer(
                host=self.api_host,
                port=self.api_port,
                get_status_callback=lambda: self.get_local_status(is_allowed_hidden=True),
                get_roms_callback=self.get_local_roms,
                start_upload_callback=self.handle_remote_upload_request,
                cancel_upload_callback=self.handle_remote_cancel_request,
                secret_key=self.secret_key,
            )
            self.status_server.start()

        tasks = [self.monitor_loop()]
        if self.listen_for_commands:
            tasks.append(self.poll_telegram_updates())
        else:
            logger.info("Worker mode: Telegram update polling disabled (node will push notifications only).")

        try:
            await asyncio.gather(*tasks)
        finally:
            if self.status_server:
                self.status_server.stop()
