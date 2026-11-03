"""Message Formatter for Telegram Notifications (HTML Format).

Formats live build progress, completion with artifact tables, and source sync
status matching custom ROM developer community templates.
Uses HTML parse_mode to eliminate entity parsing bugs with underscores in filenames.
"""

import html
import os
from datetime import datetime
from typing import List, Dict, Any, Optional
import psutil

from .artifacts import format_file_size

try:
    psutil.cpu_percent(interval=None)
except Exception:
    pass


def get_system_stats(path: str = "/") -> Dict[str, Any]:
    """Get current system CPU, RAM, and NVMe disk usage."""
    try:
        cpu = psutil.cpu_percent(interval=None)
    except Exception:
        cpu = 0.0

    try:
        mem = psutil.virtual_memory()
        ram_used_gb = mem.used / (1024 ** 3)
        ram_total_gb = mem.total / (1024 ** 3)
        ram_pct = mem.percent
    except Exception:
        ram_used_gb, ram_total_gb, ram_pct = 0.0, 0.0, 0.0

    disk_used_gb, disk_total_gb, disk_pct = 0.0, 0.0, 0.0
    for target in [path, "/"]:
        if not target:
            continue
        try:
            disk = psutil.disk_usage(target)
            disk_used_gb = disk.used / (1024 ** 3)
            disk_total_gb = disk.total / (1024 ** 3)
            disk_pct = disk.percent
            break
        except Exception:
            pass

    return {
        "cpu_pct": cpu,
        "ram_used_gb": ram_used_gb,
        "ram_total_gb": ram_total_gb,
        "ram_pct": ram_pct,
        "disk_used_gb": disk_used_gb,
        "disk_total_gb": disk_total_gb,
        "disk_pct": disk_pct,
    }


def format_duration(seconds: float) -> str:
    """Format seconds into 'X minutes, Y seconds' or 'X hours, Y minutes, Z seconds'."""
    sec = int(max(0, seconds))
    hours, remainder = divmod(sec, 3600)
    minutes, s = divmod(remainder, 60)

    parts = []
    if hours > 0:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    parts.append(f"{s} second{'s' if s != 1 else ''}")

    return ", ".join(parts)


def format_started_timestamp(start_time_epoch: float) -> str:
    """Format timestamp into: 'YYYY-MM-DD hh:mm:ss AM/PM UTC+HH:MM'."""
    dt = datetime.fromtimestamp(start_time_epoch).astimezone()
    tz_offset = dt.strftime("%z")
    tz_formatted = f"UTC{tz_offset[:3]}:{tz_offset[3:]}" if tz_offset else "UTC"
    return dt.strftime(f"%Y-%m-%d %I:%M:%S %p {tz_formatted}")


def make_progress_bar(percentage: float, length: int = 10) -> str:
    """Construct a 10-block progress bar using █ and ░."""
    filled = min(length, max(0, int(round((percentage / 100.0) * length))))
    return "█" * filled + "░" * (length - filled)


def format_build_in_progress(
    preview_url: str,
    rom_title: str,
    device: str,
    user_name: str,
    user_emoji: str,
    current_actions: int,
    finished_actions: int,
    total_actions: int,
    elapsed_seconds: float,
    eta_seconds: Optional[float],
    variant: str,
    build_type: str,
    release: str,
    system_stats: Optional[Dict[str, Any]] = None,
    server_name: Optional[str] = None,
) -> str:
    """Format the Build In Progress Telegram message in HTML."""
    if system_stats is None:
        system_stats = get_system_stats()

    pct = (finished_actions / total_actions * 100.0) if total_actions > 0 else 0.0
    bar = make_progress_bar(pct)
    elapsed_str = format_duration(elapsed_seconds)
    eta_str = format_duration(eta_seconds) if eta_seconds is not None else "Calculating..."
    actions_str = f"{finished_actions}/{total_actions}" if total_actions > 0 else "Initializing build environment..."

    hidden_preview = f'<a href="{preview_url}">&#8203;</a>'

    server_line = f"🖥️ Server: <b>{html.escape(server_name)}</b>\n" if server_name else ""

    sys_block = ""
    if system_stats:
        sys_block = (
            f"💻 System Resources:\n"
            f"  • CPU: {system_stats['cpu_pct']:.1f}%\n"
            f"  • RAM: {system_stats['ram_used_gb']:.1f} / {system_stats['ram_total_gb']:.1f} GB ({system_stats['ram_pct']:.1f}%)\n"
            f"  • NVMe: {system_stats['disk_used_gb']:.1f} / {system_stats['disk_total_gb']:.1f} GB ({system_stats['disk_pct']:.1f}%)\n\n"
        )

    return (
        f"{hidden_preview}⚙️ Build In Progress ({current_actions} tasks running) for {html.escape(rom_title)} on {html.escape(device)}\n"
        f"👤 User: {html.escape(user_name)}             {user_emoji}\n"
        f"{server_line}"
        f"📊 Progress: [{bar}] {pct:.2f}%\n"
        f"   Actions: {actions_str}\n"
        f"⏳ Elapsed: {elapsed_str}\n"
        f"⏱️ ETA: {eta_str}\n\n"
        f"{sys_block}"
        f"🔧 Configuration:\n"
        f"  Variant: {html.escape(variant)}\n"
        f"  Type: {html.escape(build_type)}\n"
        f"  Release: {html.escape(release)}\n\n"
        f"Optimizing for maximum developer satisfaction. Please hold. 📈"
    )


def format_build_completed(
    preview_url: str,
    rom_title: str,
    device: str,
    user_name: str,
    user_emoji: str,
    start_time_epoch: float,
    duration_seconds: float,
    finished_actions: int,
    total_actions: int,
    variant: str,
    build_type: str,
    release: str,
    artifacts: List[Dict[str, Any]],
    system_stats: Optional[Dict[str, Any]] = None,
    server_name: Optional[str] = None,
) -> str:
    """Format the Build Completed Telegram message in HTML."""
    if system_stats is None:
        system_stats = get_system_stats()

    started_str = format_started_timestamp(start_time_epoch)
    duration_str = format_duration(duration_seconds)
    hidden_preview = f'<a href="{preview_url}">&#8203;</a>'

    lines = [
        f"{hidden_preview}✅ Build Completed for {html.escape(rom_title)} on {html.escape(device)}",
        f"User: {html.escape(user_name)}             {user_emoji}",
    ]
    if server_name:
        lines.append(f"🖥️ Server: <b>{html.escape(server_name)}</b>")
    lines.extend([
        f"Started: {started_str}",
        f"Duration: {duration_str}",
        f"📊 Build Stats: {finished_actions}/{total_actions} actions",
    ])

    if system_stats:
        lines.append(f"💾 NVMe Usage: {system_stats['disk_used_gb']:.1f} / {system_stats['disk_total_gb']:.1f} GB ({system_stats['disk_pct']:.1f}%)")

    lines.extend([
        f"🔧 Configuration:",
        f"  • Variant: {html.escape(variant)}",
        f"  • Type: {html.escape(build_type)}",
        f"  • Release: {html.escape(release)}",
        "",
        "🎉 Build Artifact(s) Uploaded:",
    ])

    for i, art in enumerate(artifacts, 1):
        lines.append(f"{i}. File: {html.escape(art['name'])}")
        lines.append(f"     Type: {html.escape(art['type'])} | Size: {html.escape(art['size_formatted'])}")
        lines.append(f"     SHA256: {html.escape(art['sha256'])}")

    return "\n".join(lines)


def format_build_failed(
    preview_url: str,
    rom_title: str,
    device: str,
    user_name: str,
    user_emoji: str,
    start_time_epoch: float,
    duration_seconds: float,
    finished_actions: int,
    total_actions: int,
    variant: str,
    build_type: str,
    release: str,
    error_summary: str,
    server_name: Optional[str] = None,
) -> str:
    """Format Build Failed Telegram message in HTML."""
    started_str = format_started_timestamp(start_time_epoch)
    duration_str = format_duration(duration_seconds)
    hidden_preview = f'<a href="{preview_url}">&#8203;</a>'
    server_line = f"🖥️ Server: <b>{html.escape(server_name)}</b>\n" if server_name else ""

    return (
        f"{hidden_preview}❌ Build Failed for {html.escape(rom_title)} on {html.escape(device)}\n"
        f"User: {html.escape(user_name)}             {user_emoji}\n"
        f"{server_line}"
        f"Started: {started_str}\n"
        f"Duration: {duration_str}\n"
        f"📊 Actions: {finished_actions}/{total_actions} actions\n"
        f"🔧 Configuration:\n"
        f"  • Variant: {html.escape(variant)}\n"
        f"  • Type: {html.escape(build_type)}\n"
        f"  • Release: {html.escape(release)}\n\n"
        f"⚠️ Error Log:\n"
        f"<pre>{html.escape(error_summary[:1200])}</pre>"
    )


def format_sync_in_progress(
    preview_url: str,
    rom_title: str,
    user_name: str,
    user_emoji: str,
    synced_projects: int,
    total_projects: int,
    elapsed_seconds: float,
    eta_seconds: Optional[float],
    branch: str,
    remote: str,
    system_stats: Optional[Dict[str, Any]] = None,
    server_name: Optional[str] = None,
) -> str:
    """Format Source Sync In Progress Telegram message in HTML."""
    if system_stats is None:
        system_stats = get_system_stats()

    pct = (synced_projects / total_projects * 100.0) if total_projects > 0 else 0.0
    bar = make_progress_bar(pct)
    elapsed_str = format_duration(elapsed_seconds)
    eta_str = format_duration(eta_seconds) if eta_seconds is not None else "Calculating..."
    hidden_preview = f'<a href="{preview_url}">&#8203;</a>'
    server_line = f"🖥️ Server: <b>{html.escape(server_name)}</b>\n" if server_name else ""

    sys_block = ""
    if system_stats:
        sys_block = (
            f"💻 System Resources:\n"
            f"  • CPU: {system_stats['cpu_pct']:.1f}%\n"
            f"  • RAM: {system_stats['ram_used_gb']:.1f} / {system_stats['ram_total_gb']:.1f} GB ({system_stats['ram_pct']:.1f}%)\n"
            f"  • NVMe: {system_stats['disk_used_gb']:.1f} / {system_stats['disk_total_gb']:.1f} GB ({system_stats['disk_pct']:.1f}%)\n\n"
        )

    return (
        f"{hidden_preview}🔄 Source Sync In Progress for {html.escape(rom_title)}\n"
        f"👤 User: {html.escape(user_name)}             {user_emoji}\n"
        f"{server_line}"
        f"📊 Progress: [{bar}] {pct:.2f}%\n"
        f"   Projects: {synced_projects}/{total_projects}\n"
        f"⏳ Elapsed: {elapsed_str}\n"
        f"⏱️ ETA: {eta_str}\n\n"
        f"{sys_block}"
        f"🌿 Branch: {html.escape(branch)}\n"
        f"📦 Remote: {html.escape(remote)}\n\n"
        f"Syncing manifest and source trees. Please hold. 📡"
    )


def format_sync_completed(
    preview_url: str,
    rom_title: str,
    user_name: str,
    user_emoji: str,
    start_time_epoch: float,
    duration_seconds: float,
    total_projects: int,
    branch: str,
    remote: str,
    server_name: Optional[str] = None,
) -> str:
    """Format Source Sync Completed Telegram message in HTML."""
    started_str = format_started_timestamp(start_time_epoch)
    duration_str = format_duration(duration_seconds)
    hidden_preview = f'<a href="{preview_url}">&#8203;</a>'
    server_line = f"🖥️ Server: <b>{html.escape(server_name)}</b>\n" if server_name else ""

    return (
        f"{hidden_preview}✅ Source Sync Completed for {html.escape(rom_title)}\n"
        f"User: {html.escape(user_name)}             {user_emoji}\n"
        f"{server_line}"
        f"Started: {started_str}\n"
        f"Duration: {duration_str}\n"
        f"📊 Sync Stats: {total_projects}/{total_projects} projects synced\n"
        f"🌿 Branch: {html.escape(branch)}\n"
        f"📦 Remote: {html.escape(remote)}\n\n"
        f"All repositories are up to date! 🚀"
    )


def make_build_completed_keyboard(rom_id_or_device: str, server_name: Optional[str] = None) -> Dict[str, Any]:
    """Inline button attached to build completed messages for 1-click GoFile upload."""
    cb_data = f"upload_build:{rom_id_or_device}"
    if server_name:
        cand = f"upload_build:{rom_id_or_device}@{server_name}"
        if len(cand.encode("utf-8")) <= 60:
            cb_data = cand
    return {
        "inline_keyboard": [
            [
                {
                    "text": "⬇️ Download Files (GoFile)",
                    "callback_data": cb_data,
                }
            ]
        ]
    }


def format_rom_list(roms: List[Dict[str, Any]]) -> str:
    """Format list of scanned ROMs and Kernels on the server."""
    if not roms:
        return (
            "🔍 <b>No ROMs or Kernels Found on Server</b>\n\n"
            "No compiled builds, downloaded ROM packages, or kernel releases were detected.\n"
            "Run a build or place packages on the server to make them downloadable."
        )

    lines = [
        "📂 <b>ROMs & Kernels Present on Server</b>\n",
        "Select a build or kernel below to upload all files to GoFile and get download links:\n",
    ]

    for idx, r in enumerate(roms, 1):
        if r.get("type") == "kernel":
            icon = "⚡"
        elif r["type"] == "build":
            icon = "📱"
        else:
            icon = "📦"
        tag = f"({r['device']})" if r.get("device") and r["device"] != "unknown" else ""
        lines.append(f"{idx}. {icon} <b>{html.escape(r['name'])}</b> {html.escape(tag)}")
        lines.append(f"   • Package: <code>{html.escape(r['main_zip_name'])}</code>")
        if r["type"] in ["build", "kernel"]:
            lines.append(f"   • Artifacts: {r['file_count']} files ({r['total_size_formatted']} total)")
        else:
            lines.append(f"   • Size: {r['main_zip_size']}")
        lines.append("")

    lines.append("<i>Click a button below to upload and download:</i>")
    return "\n".join(lines)


def make_rom_list_keyboard(roms: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Construct inline keyboard for ROM and Kernel selection."""
    buttons = []
    for r in roms[:10]:
        dev_tag = f" ({r['device']})" if r.get("device") and r["device"] != "unknown" else ""
        prefix = "⚡ " if r.get("type") == "kernel" else "⬇️ "
        btn_text = f"{prefix}{r['name']}{dev_tag}"
        buttons.append([{"text": btn_text, "callback_data": f"dl:{r['id']}"}])

    buttons.append([{"text": "🔄 Rescan Server", "callback_data": "rescan_roms"}])
    return {"inline_keyboard": buttons}


def format_gofile_upload_progress(
    rom_name: str,
    device: str,
    pct: float,
    transferred_bytes: int,
    total_bytes: int,
    speed_mb_s: float,
    eta_seconds: Optional[int],
    completed_files: int,
    total_files: int,
    current_filename: Optional[str] = None,
) -> str:
    """Format live progress message while uploading to GoFile with real-time speed and progress bar."""
    dev_str = f" on {html.escape(device)}" if device and device != "unknown" else ""
    pct_clamped = max(0.0, min(100.0, pct))
    bar = make_progress_bar(pct_clamped)

    trans_str = format_file_size(transferred_bytes)
    total_str = format_file_size(total_bytes)
    speed_str = f"{speed_mb_s:.1f} MB/s" if speed_mb_s > 0 else "Calculating..."

    if eta_seconds is not None and eta_seconds > 0:
        eta_str = format_duration(float(eta_seconds))
    elif speed_mb_s > 0:
        eta_str = "Calculating..."
    else:
        eta_str = "Calculating..."

    lines = [
        f"☁️ <b>Uploading {html.escape(rom_name)}{dev_str} to GoFile...</b>\n",
        f"📊 <b>Progress:</b> [{bar}] {pct_clamped:.1f}%",
        f"   <b>Transferred:</b> {trans_str} / {total_str} ({completed_files}/{total_files} files)",
        f"⚡ <b>Speed:</b> {speed_str}",
        f"⏱️ <b>ETA:</b> {eta_str}",
    ]

    if current_filename:
        lines.append(f"\n⏳ <b>Current File:</b> <code>{html.escape(current_filename)}</code>")

    lines.append("\n<i>Optimizing streaming upload to GoFile servers... 🚀</i>")
    return "\n".join(lines)


def format_gofile_upload_completed(
    rom_name: str,
    device: str,
    download_url: str,
    uploaded_files: List[Dict[str, Any]],
    failed_files: Optional[List[str]] = None,
    duration_seconds: Optional[float] = None,
    avg_speed_mb_s: Optional[float] = None,
) -> str:
    """Format final GoFile upload completion message with links, duration, and speed stats."""
    dev_str = f" on {html.escape(device)}" if device and device != "unknown" else ""
    total_bytes = sum(f.get("size_bytes", 0) for f in uploaded_files)
    total_str = format_file_size(total_bytes) if total_bytes > 0 else ""

    lines = [
        f"🎉 <b>GoFile Upload Completed!</b>\n",
        f"📱 <b>ROM:</b> {html.escape(rom_name)}{dev_str}",
        f"🔗 <b>Download Folder:</b> <a href=\"{download_url}\">{download_url}</a>",
    ]

    if duration_seconds is not None and duration_seconds > 0:
        dur_str = format_duration(duration_seconds)
        lines.append(f"⏳ <b>Duration:</b> {dur_str}")

    if avg_speed_mb_s is not None and avg_speed_mb_s > 0:
        lines.append(f"⚡ <b>Average Speed:</b> {avg_speed_mb_s:.1f} MB/s")

    size_tag = f", {total_str}" if total_str else ""
    lines.append(f"\n📦 <b>Uploaded Artifacts ({len(uploaded_files)} files{size_tag}):</b>")

    for idx, f in enumerate(uploaded_files, 1):
        icon = "📁" if f["name"].endswith(".zip") else ("💿" if f["name"].endswith(".img") else "📄")
        lines.append(f"{idx}. {icon} <code>{html.escape(f['name'])}</code> ({html.escape(f['size_formatted'])})")

    if failed_files:
        lines.append(f"\n⚠️ <i>Failed to upload {len(failed_files)} file(s).</i>")

    lines.append("\n✨ <i>Click the folder link above to view and download all files.</i>")
    return "\n".join(lines)


def make_gofile_uploading_keyboard(upload_id: str) -> Dict[str, Any]:
    """Cancel button displayed while GoFile upload is running."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "❌ Cancel Upload",
                    "callback_data": f"cancel_upload:{upload_id}",
                }
            ]
        ]
    }


def make_gofile_open_keyboard(download_url: str) -> Dict[str, Any]:
    """Button opening the GoFile folder in browser."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🌐 Open GoFile Download Folder",
                    "url": download_url,
                }
            ],
            [
                {
                    "text": "📂 Browse Other ROMs & Kernels",
                    "callback_data": "rescan_roms",
                }
            ]
        ]
    }


def format_kernel_build_in_progress(
    preview_url: str,
    kernel_title: str,
    device: str,
    user_name: str,
    user_emoji: str,
    active_tasks: int,
    compiled_objects: int,
    total_objects: int,
    pct: float,
    elapsed_seconds: float,
    eta_seconds: Optional[float],
    kernel_version: str,
    localversion: str,
    compiler_str: str = "Clang / LLVM",
    system_stats: Optional[Dict[str, Any]] = None,
    server_name: Optional[str] = None,
) -> str:
    """Format Kernel Build In Progress message in HTML."""
    if system_stats is None:
        system_stats = get_system_stats()

    bar = make_progress_bar(pct)
    elapsed_str = format_duration(elapsed_seconds)
    eta_str = format_duration(eta_seconds) if eta_seconds is not None else "Calculating..."
    hidden_preview = f'<a href="{preview_url}">&#8203;</a>'
    server_line = f"🖥️ Server: <b>{html.escape(server_name)}</b>\n" if server_name else ""

    sys_block = ""
    if system_stats:
        sys_block = (
            f"💻 System Resources:\n"
            f"  • CPU: {system_stats['cpu_pct']:.1f}%\n"
            f"  • RAM: {system_stats['ram_used_gb']:.1f} / {system_stats['ram_total_gb']:.1f} GB ({system_stats['ram_pct']:.1f}%)\n"
            f"  • NVMe: {system_stats['disk_used_gb']:.1f} / {system_stats['disk_total_gb']:.1f} GB ({system_stats['disk_pct']:.1f}%)\n\n"
        )

    dev_label = html.escape(device)
    config_lines = [
        f"  • Kernel: {html.escape(kernel_version)}",
    ]
    if localversion:
        config_lines.append(f"  • Version: {html.escape(localversion)}")
    config_lines.append(f"  • Target: {dev_label}")
    config_lines.append(f"  • Compiler: {html.escape(compiler_str)}")
    config_block = "\n".join(config_lines)

    return (
        f"{hidden_preview}⚙️ <b>Kernel Build In Progress ({active_tasks} tasks running) for {html.escape(kernel_title)} on {dev_label}</b>\n"
        f"👤 User: {html.escape(user_name)}             {user_emoji}\n"
        f"{server_line}"
        f"📊 Progress: [{bar}] {pct:.2f}%\n"
        f"   Objects: {compiled_objects}/{total_objects} compiled\n"
        f"⏳ Elapsed: {elapsed_str}\n"
        f"⏱️ ETA: {eta_str}\n\n"
        f"{sys_block}"
        f"🔧 Configuration:\n"
        f"{config_block}\n\n"
        f"Optimizing for maximum developer satisfaction. Please hold. 📈"
    )


def format_kernel_build_completed(
    preview_url: str,
    kernel_title: str,
    device: str,
    user_name: str,
    user_emoji: str,
    start_time_epoch: float,
    duration_seconds: float,
    total_objects: int,
    kernel_version: str,
    localversion: str,
    artifacts: List[Dict[str, Any]],
    download_url: Optional[str] = None,
    system_stats: Optional[Dict[str, Any]] = None,
    server_name: Optional[str] = None,
) -> str:
    """Format Kernel Build Completed message in HTML."""
    if system_stats is None:
        system_stats = get_system_stats()

    started_str = format_started_timestamp(start_time_epoch)
    duration_str = format_duration(duration_seconds)
    hidden_preview = f'<a href="{preview_url}">&#8203;</a>'
    dev_label = html.escape(device)

    lines = [
        f"{hidden_preview}✅ <b>Kernel Build Completed for {html.escape(kernel_title)} on {dev_label}</b>",
        f"User: {html.escape(user_name)}             {user_emoji}",
    ]
    if server_name:
        lines.append(f"🖥️ Server: <b>{html.escape(server_name)}</b>")
    lines.extend([
        f"Started: {started_str}",
        f"Duration: {duration_str}",
        f"📊 Build Stats: {total_objects}/{total_objects} objects compiled",
    ])

    if system_stats:
        lines.append(f"💾 NVMe Usage: {system_stats['disk_used_gb']:.1f} / {system_stats['disk_total_gb']:.1f} GB ({system_stats['disk_pct']:.1f}%)")

    lines.extend([
        f"🔧 Configuration:",
        f"  • Kernel: {html.escape(kernel_version)}",
    ])
    if localversion:
        lines.append(f"  • Version: {html.escape(localversion)}")
    lines.append(f"  • Device: {dev_label}")

    if artifacts:
        lines.append("\n🎉 Build Artifact(s) Uploaded:")
        for i, art in enumerate(artifacts, 1):
            lines.append(f"{i}. File: {html.escape(art['name'])}")
            lines.append(f"     Type: {html.escape(art['type'])} | Size: {html.escape(art['size_formatted'])}")
            if art.get("sha256"):
                lines.append(f"     SHA256: {html.escape(art['sha256'])}")

    if download_url:
        lines.append(f"\n🔗 <b>GoFile Download Folder:</b> <a href=\"{download_url}\">{download_url}</a>")

    return "\n".join(lines)


def format_kernel_build_failed(
    preview_url: str,
    kernel_title: str,
    device: str,
    user_name: str,
    user_emoji: str,
    start_time_epoch: float,
    duration_seconds: float,
    compiled_objects: int,
    total_objects: int,
    kernel_version: str,
    error_summary: str,
    server_name: Optional[str] = None,
) -> str:
    """Format Kernel Build Failed message in HTML."""
    started_str = format_started_timestamp(start_time_epoch)
    duration_str = format_duration(duration_seconds)
    hidden_preview = f'<a href="{preview_url}">&#8203;</a>'
    dev_label = html.escape(device)
    server_line = f"🖥️ Server: <b>{html.escape(server_name)}</b>\n" if server_name else ""

    return (
        f"{hidden_preview}❌ <b>Kernel Build Failed for {html.escape(kernel_title)} on {dev_label}</b>\n"
        f"User: {html.escape(user_name)}             {user_emoji}\n"
        f"{server_line}"
        f"Started: {started_str}\n"
        f"Duration: {duration_str}\n"
        f"📊 Build Stats: {compiled_objects}/{total_objects} objects compiled\n"
        f"🔧 Configuration:\n"
        f"  • Kernel: {html.escape(kernel_version)}\n"
        f"  • Device: {dev_label}\n\n"
        f"⚠️ Error Summary:\n"
        f"<code>{html.escape(error_summary[:400])}</code>\n\n"
        f"Please check compile logs for build diagnostics. 🛠️"
    )


def make_kernel_completed_keyboard(download_url: Optional[str] = None) -> Dict[str, Any]:
    """Action buttons attached to kernel build completion message."""
    buttons = []
    if download_url:
        buttons.append([
            {
                "text": "🌐 Open GoFile Download Folder",
                "url": download_url,
            }
        ])
    buttons.append([
        {
            "text": "📂 Browse All ROMs & Kernels",
            "callback_data": "rescan_roms",
        }
    ])
    return {"inline_keyboard": buttons}


def format_server_status_block(
    server_name: str,
    system_stats: Optional[Dict[str, Any]],
    builds: List[Dict[str, Any]],
    kernels: List[Dict[str, Any]],
    syncs: List[Dict[str, Any]],
    online: bool = True,
    error: Optional[str] = None,
) -> str:
    """Format single server status card block."""
    if not online:
        err_msg = error or "Connection timed out"
        return (
            f"🖥️ <b>{html.escape(server_name)}</b> 🔴 <i>(Offline / Unreachable)</i>\n"
            f"  ⚠️ <i>{html.escape(err_msg)}</i>"
        )

    lines = [f"🖥️ <b>{html.escape(server_name)}</b> 🟢"]
    if system_stats:
        lines.extend([
            f"💻 <b>System Resources:</b>",
            f"  • CPU: {system_stats.get('cpu_pct', 0.0):.1f}%",
            f"  • RAM: {system_stats.get('ram_used_gb', 0.0):.1f} / {system_stats.get('ram_total_gb', 0.0):.1f} GB ({system_stats.get('ram_pct', 0.0):.1f}%)",
            f"  • NVMe: {system_stats.get('disk_used_gb', 0.0):.1f} / {system_stats.get('disk_total_gb', 0.0):.1f} GB ({system_stats.get('disk_pct', 0.0):.1f}%)\n",
        ])

    active_build_count = len(builds)
    if active_build_count > 0:
        lines.append(f"⚙️ <b>Active ROM Builds ({active_build_count}):</b>")
        for b in builds:
            pct_val = b.get("pct", -1.0)
            pct_str = f"{pct_val:.1f}%" if pct_val >= 0 else "compiling"
            lines.append(f"  • {html.escape(str(b.get('rom_title', 'ROM')))} ({html.escape(str(b.get('device', 'device')))}) — {pct_str} [PID {b.get('pid')}]")
    else:
        lines.append("⚙️ <b>Active ROM Builds:</b> None running")

    active_kernel_count = len(kernels)
    if active_kernel_count > 0:
        lines.append(f"\n⚡ <b>Active Kernel Builds ({active_kernel_count}):</b>")
        for k in kernels:
            pct_val = k.get("pct", -1.0)
            pct_str = f"{pct_val:.1f}%" if pct_val >= 0 else "compiling"
            lines.append(f"  • {html.escape(str(k.get('kernel_title', 'Kernel')))} ({html.escape(str(k.get('device', 'device')))}) — {pct_str} [PID {k.get('pid')}]")
    else:
        lines.append("⚡ <b>Active Kernel Builds:</b> None running")

    active_sync_count = len(syncs)
    if active_sync_count > 0:
        lines.append(f"\n🔄 <b>Active Source Syncs ({active_sync_count}):</b>")
        for s in syncs:
            tot = s.get("total_projects", 0)
            synced = s.get("synced_projects", 0)
            pct = (synced / tot * 100.0) if tot > 0 else 0.0
            lines.append(f"  • {html.escape(str(s.get('rom_title', 'ROM')))} ({html.escape(str(s.get('branch', 'default')))}) — {pct:.1f}% ({synced}/{tot} projects) [PID {s.get('pid')}]")
    else:
        lines.append("🔄 <b>Active Source Syncs:</b> None running")

    return "\n".join(lines)


def format_multi_server_status(servers: List[Dict[str, Any]]) -> str:
    """Format combined status across multiple servers."""
    header = f"📊 <b>AOSP Tracker Server Status ({len(servers)} Nodes)</b>\n"
    blocks = []
    for s in servers:
        block = format_server_status_block(
            server_name=s.get("server_name", "Unknown Server"),
            system_stats=s.get("system_stats"),
            builds=s.get("builds", []),
            kernels=s.get("kernels", []),
            syncs=s.get("syncs", []),
            online=s.get("online", True),
            error=s.get("error"),
        )
        blocks.append(block)

    body = "\n\n────────────────────\n\n".join(blocks)
    footer = "\n\n💡 <i>Send /roms or /kernels to browse releases.</i>"
    return f"{header}\n{body}{footer}"

