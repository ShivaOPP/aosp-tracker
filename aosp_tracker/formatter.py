"""Message Formatter for Telegram Notifications (HTML Format).

Formats live build progress, completion with artifact tables, and source sync
status matching custom ROM developer community templates.
Uses HTML parse_mode to eliminate entity parsing bugs with underscores in filenames.
"""

import hashlib
import html
import os
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
import psutil

from .artifacts import format_file_size

try:
    psutil.cpu_percent(interval=None)
except Exception:
    pass


def get_system_stats(path: Optional[str] = None) -> Dict[str, Any]:
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

    # Determine optimal disk path to monitor (e.g. /mnt/data, /data, or largest mount)
    candidates = []
    if path and path != "/":
        candidates.append(path)

    # 1. Configured disk_path / storage_path
    try:
        from .user_detector import load_global_config
        cfg = load_global_config()
        if cfg.get("disk_path"):
            candidates.append(cfg["disk_path"])
        if cfg.get("storage_path"):
            candidates.append(cfg["storage_path"])
    except Exception:
        pass

    # 2. Common dedicated build storage paths (prioritize /mnt/data and /data)
    for cand in ["/mnt/data", "/data"]:
        if os.path.exists(cand):
            candidates.append(cand)

    # 3. Discovered real mountpoints: pick largest overall partition
    try:
        root_usage = psutil.disk_usage("/")
        real_parts = [("/", root_usage.total)]
        for p in psutil.disk_partitions(all=False):
            if p.fstype in ["squashfs", "iso9660", "vfat", "tmpfs"] or p.mountpoint.startswith(("/snap", "/boot", "/run", "/var")):
                continue
            try:
                u = psutil.disk_usage(p.mountpoint)
                if p.mountpoint != "/" and u.total > 10 * (1024 ** 3):
                    real_parts.append((p.mountpoint, u.total))
            except Exception:
                pass
        real_parts.sort(key=lambda x: x[1], reverse=True)
        for mp, _ in real_parts:
            if mp not in candidates:
                candidates.append(mp)
    except Exception:
        candidates.append("/")

    disk_used_gb, disk_total_gb, disk_pct = 0.0, 0.0, 0.0
    selected_target = "/"
    for target in candidates:
        if not target or not os.path.exists(target):
            continue
        try:
            disk = psutil.disk_usage(target)
            disk_used_gb = disk.used / (1024 ** 3)
            disk_total_gb = disk.total / (1024 ** 3)
            disk_pct = disk.percent
            selected_target = target
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
        "disk_label": selected_target if selected_target != "/" else "",
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
    """Create a compact visual progress meter."""
    filled = min(length, max(0, int(round((percentage / 100.0) * length))))
    return "▰" * filled + "▱" * (length - filled)


def _preview_anchor(preview_url: str) -> str:
    escaped = html.escape(preview_url, quote=True)
    return f'<a href="{escaped}">&#8203;</a>'


def _stat_line(label: str, used: float, total: float, unit: str, percent: float, meter_length: int = 8) -> str:
    return (f"{label}  <code>{make_progress_bar(percent, meter_length)}</code>  "
            f"<b>{percent:.0f}%</b> · {used:.1f}/{total:.1f} {unit}")


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

    hidden_preview = _preview_anchor(preview_url)

    server_line = f"🖥️ Server: <b>{html.escape(server_name)}</b>\n" if server_name else ""

    sys_block = ""
    if system_stats:
        disk_label = system_stats.get("disk_label") or "Storage"
        sys_block = (
            "<b>NODE</b>\n"
            + _stat_line("CPU", system_stats["cpu_pct"], 100, "%", system_stats["cpu_pct"]) + "\n"
            + _stat_line("RAM", system_stats["ram_used_gb"], system_stats["ram_total_gb"], "GB", system_stats["ram_pct"]) + "\n"
            + _stat_line(html.escape(disk_label), system_stats["disk_used_gb"], system_stats["disk_total_gb"], "GB", system_stats["disk_pct"]) + "\n\n"
        )

    heading = f"<b>{html.escape(rom_title)}  /  {html.escape(device)}</b>"
    identity = f"{html.escape(user_name)} {user_emoji}"
    server_line = f" · {html.escape(server_name)}" if server_name else ""
    return (
        f"{hidden_preview}🛠️  <b>BUILD RUNNING</b>\n{heading}\n"
        f"{identity}{server_line}\n\n"
        f"<code>{bar}</code>  <b>{pct:.1f}%</b>\n"
        f"<b>{actions_str}</b> actions · {current_actions} active\n"
        f"◷ {elapsed_str} elapsed  ·  ETA {eta_str}\n\n"
        f"{sys_block}<b>BUILD PROFILE</b>\n"
        f"{html.escape(variant)} · {html.escape(build_type)} · {html.escape(release)}"
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

    duration_str = format_duration(duration_seconds)
    lines = [
        f"{_preview_anchor(preview_url)}✅  <b>BUILD COMPLETE</b>",
        f"<b>{html.escape(rom_title)}  /  {html.escape(device)}</b>",
        f"{html.escape(user_name)} {user_emoji}" + (f" · {html.escape(server_name)}" if server_name else ""),
        "",
        f"◷ {duration_str}  ·  {finished_actions:,}/{total_actions:,} actions",
    ]
    if system_stats:
        disk_label = html.escape(system_stats.get("disk_label") or "Storage")
        lines.extend(["", "<b>NODE STORAGE</b>",
                      _stat_line(disk_label, system_stats["disk_used_gb"], system_stats["disk_total_gb"], "GB", system_stats["disk_pct"])])
    lines.extend(["", "<b>BUILD PROFILE</b>",
                  f"{html.escape(variant)} · {html.escape(build_type)} · {html.escape(release)}"])
    if artifacts:
        lines.extend(["", f"<b>ARTIFACTS  /  {len(artifacts)}</b>"])
        for art in artifacts:
            name = html.escape(art.get("name", "release"))
            details = [html.escape(art.get("type", "FILE")), html.escape(art.get("size_formatted", ""))]
            line = f"• <code>{name}</code> · {' · '.join(details)}"
            if art.get("sha256"):
                line += f"\n  <code>SHA-256  {html.escape(art['sha256'])}</code>"
            lines.append(line)
    else:
        lines.extend(["", "Build completed. No flashable artifacts were detected."])
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
    server_line = f" · {html.escape(server_name)}" if server_name else ""
    return (
        f"{_preview_anchor(preview_url)}⛔  <b>BUILD STOPPED</b>\n"
        f"<b>{html.escape(rom_title)}  /  {html.escape(device)}</b>\n"
        f"{html.escape(user_name)} {user_emoji}{server_line}\n\n"
        f"◷ {format_duration(duration_seconds)}  ·  {finished_actions:,}/{total_actions:,} actions\n"
        f"<b>BUILD PROFILE</b>\n{html.escape(variant)} · {html.escape(build_type)} · {html.escape(release)}\n\n"
        f"<b>ERROR SUMMARY</b>\n<pre>{html.escape(error_summary[:1200])}</pre>"
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
    hidden_preview = _preview_anchor(preview_url)
    server_line = f"🖥️ Server: <b>{html.escape(server_name)}</b>\n" if server_name else ""

    sys_block = ""
    if system_stats:
        sys_block = ("<b>NODE</b>\n"
                     + _stat_line("CPU", system_stats["cpu_pct"], 100, "%", system_stats["cpu_pct"]) + "\n"
                     + _stat_line("RAM", system_stats["ram_used_gb"], system_stats["ram_total_gb"], "GB", system_stats["ram_pct"]) + "\n\n")

    identity = f"{html.escape(user_name)} {user_emoji}"
    if server_name:
        identity += f" · {html.escape(server_name)}"
    return (
        f"{hidden_preview}🔄  <b>SOURCE SYNC</b>\n"
        f"<b>{html.escape(rom_title)}</b> · {identity}\n\n"
        f"<code>{bar}</code>  <b>{pct:.1f}%</b>\n"
        f"{synced_projects:,}/{total_projects:,} projects\n"
        f"◷ {elapsed_str} elapsed  ·  ETA {eta_str}\n\n{sys_block}"
        f"<b>MANIFEST</b>\n{html.escape(branch)}\n{html.escape(remote)}"
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
    return (
        f"{_preview_anchor(preview_url)}✅  <b>SOURCE SYNC COMPLETE</b>\n"
        f"<b>{html.escape(rom_title)}</b> · {html.escape(user_name)} {user_emoji}\n"
        f"{html.escape(server_name) + ' · ' if server_name else ''}{format_duration(duration_seconds)}\n\n"
        f"<b>MANIFEST</b>\n{html.escape(branch)}\n{html.escape(remote)}\n\n"
        f"{total_projects:,} projects updated"
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
                    "text": "↑ Choose upload host",
                    "callback_data": cb_data,
                }
            ]
        ]
    }


def group_roms(roms: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group a list of ROM/Kernel builds by Name and Server for interactive device selection."""
    groups_dict = {}
    for r in roms:
        r_name = r.get("name", "Custom ROM")
        r_server = r.get("server_name", "Local Server")
        r_type = r.get("type", "build")
        key = (r_name.lower(), r_server.lower(), r_type)
        if key not in groups_dict:
            groups_dict[key] = {
                "name": r_name,
                "server_name": r_server,
                "type": r_type,
                "items": [],
                "devices": [],
            }
        groups_dict[key]["items"].append(r)
        dev = r.get("device", "")
        if dev and dev != "unknown" and dev not in groups_dict[key]["devices"]:
            groups_dict[key]["devices"].append(dev)

    groups_list = []
    for key, g in groups_dict.items():
        total_files = sum(item.get("file_count", 1) for item in g["items"])
        total_bytes = sum(item.get("total_size_bytes", 0) for item in g["items"])
        g["group_id"] = hashlib.blake2s("\0".join(key).encode(), digest_size=8).hexdigest()
        g["total_files"] = total_files
        g["total_bytes"] = total_bytes
        g["total_size_formatted"] = format_file_size(total_bytes) if total_bytes > 0 else ""
        groups_list.append(g)

    return groups_list


def format_rom_catalog(groups: List[Dict[str, Any]], filter_type: Optional[str] = None) -> str:
    """Format a compact release-library overview."""
    title = "KERNELS" if filter_type == "kernel" else "RELEASE LIBRARY"
    if not groups:
        return f"◈  <b>{title}</b>\n\nNo matching releases yet. Try /search or refresh after a build."
    files = sum(g["total_files"] for g in groups)
    nodes = len({g["server_name"] for g in groups})
    lines = [f"◈  <b>{title}</b>",
             f"{len(groups)} releases  ·  {files} files  ·  {nodes} nodes",
             "", "<i>Select a release to inspect its package and mirrors.</i>"]
    if len(groups) > 25:
        lines.append("Use /search followed by a ROM or device name to narrow the list.")
    return "\n".join(lines)


def make_rom_catalog_keyboard(groups: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Construct inline keyboard for ROM and Kernel selection with device variant counts."""
    buttons = []
    for g in groups[:25]:
        icon = "⚡" if g["type"] == "kernel" else "◈"
        dev_count = len(g["devices"])
        device = f" · {g['devices'][0]}" if dev_count == 1 else f" · {dev_count} devices" if dev_count > 1 else ""
        name = g["name"]
        if len(name) > 32:
            name = name[:29] + "…"
        btn_text = f"{icon} {name}{device}"
        buttons.append([{"text": btn_text, "callback_data": f"sel_grp:{g['group_id']}"}])

    buttons.append([{"text": "↻ Refresh", "callback_data": "rescan_roms"}])
    return {"inline_keyboard": buttons}


def format_device_selection(group: Dict[str, Any]) -> str:
    """Format device selection prompt when a ROM with multiple devices is clicked."""
    icon = "⚡" if group.get("type") == "kernel" else "📱"
    srv = f" [{html.escape(group['server_name'])}]" if group.get("server_name") else ""
    lines = [
        f"{icon}  <b>{html.escape(group['name'])}</b>{srv}",
        "Choose a device package to preview its files and download options.",
    ]
    return "\n".join(lines)


def make_device_selection_keyboard(group: Dict[str, Any]) -> Dict[str, Any]:
    """Construct inline keyboard for device variants of a specific ROM."""
    buttons = []
    items = group.get("items", [])

    # Check if there are duplicate device names among items
    device_counts = {}
    for it in items:
        d = it.get("device") or "unknown"
        device_counts[d] = device_counts.get(d, 0) + 1

    for item in items[:25]:
        icon = "⚡ " if item.get("type") == "kernel" else "📱 "
        dev = item.get("device") or "unknown"
        size = item.get("main_zip_size") or item.get("total_size_formatted", "")

        if device_counts.get(dev, 0) > 1:
            pkg_name = item.get("main_zip_name", "")
            short_pkg = (pkg_name[:16] + "...") if len(pkg_name) > 19 else pkg_name
            btn_text = f"{icon}{dev} • {short_pkg} ({size})" if size else f"{icon}{dev} • {short_pkg}"
        else:
            size_str = f" ({size})" if size else ""
            btn_text = f"{icon}{dev}{size_str}"

        buttons.append([{"text": btn_text, "callback_data": f"sel_rom:{item['id']}"}])

    buttons.append([{"text": "🔙 Back to All ROMs", "callback_data": "rescan_roms"}])
    return {"inline_keyboard": buttons}


def format_rom_device_detail(rom: Dict[str, Any]) -> str:
    """Format single device build details and GoFile upload options."""
    icon = "⚡" if rom.get("type") == "kernel" else "📱"
    server = f" · {html.escape(rom['server_name'])}" if rom.get("server_name") else ""
    size = html.escape(str(rom.get("total_size_formatted", "")))
    count = rom.get("file_count", 0)
    lines = [
        f"{icon}  <b>{html.escape(rom['name'])}</b>",
        f"{html.escape(rom.get('device', 'unknown'))}{server}",
        "",
        "<b>PACKAGE</b>",
        f"<code>{html.escape(rom.get('main_zip_name', 'No ZIP package'))}</code>",
        f"{size}  ·  {count} artifacts",
    ]
    if rom.get("directory"):
        lines.append(f"<code>{html.escape(rom['directory'])}</code>")
    lines.extend(["", "Choose a mirror or artifact set below."])
    return "\n".join(lines)


def make_rom_device_keyboard(rom: Dict[str, Any], back_callback: str = "rescan_roms") -> Dict[str, Any]:
    """Construct inline keyboard for uploading specific artifacts of a ROM."""
    rom_id = rom["id"]
    file_count = rom.get("file_count", 1)
    has_zip = str(rom.get("main_zip_name", "")).lower().endswith(".zip")

    upload_files = [p.lower() for p in rom.get("files_to_upload", [])]
    has_boot = any(p.endswith((".img", ".bin")) for p in upload_files)

    hosts = rom.get("package_hosts", [])
    host_labels = (
        ("pixeldrain", "Pixeldrain"), ("fuckingfast", "FuckingFast.net"),
        ("storage_to", "Storage.to"),
        ("sendit", "SendIt.sh"), ("temp_sh", "Temp.sh"),
        ("x0at", "X0.at"), ("qurl", "QURL.sh"),
        ("whalebone", "Whalebone"), ("uguu", "Uguu"), ("tmpfiles", "TmpFiles.org"),
    )
    mirrors = [{"text": label, "callback_data": f"dl:{rom_id}:pkg:{provider}"}
               for provider, label in host_labels if provider in hosts]
    buttons = [mirrors[index:index + 2] for index in range(0, len(mirrors), 2)]
    buttons.append([{"text": f"↑  GoFile  ·  All files ({file_count})", "callback_data": f"dl:{rom_id}:all"}])
    if has_zip:
        buttons.append([{"text": "GoFile  ·  ZIP only", "callback_data": f"dl:{rom_id}:zip"}])
    if has_boot and file_count > 1:
        buttons.append([{"text": "GoFile  ·  Boot images", "callback_data": f"dl:{rom_id}:boot"}])

    buttons.append([{"text": "‹  Back", "callback_data": back_callback}])
    return {"inline_keyboard": buttons}


def format_rom_list(roms: List[Dict[str, Any]]) -> str:
    """Format list of scanned ROMs and Kernels on the server (catalog view)."""
    groups = group_roms(roms)
    return format_rom_catalog(groups)


def make_rom_list_keyboard(roms: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Construct inline keyboard for ROM and Kernel selection (catalog keyboard)."""
    groups = group_roms(roms)
    return make_rom_catalog_keyboard(groups)


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
    host: str = "GoFile",
) -> str:
    """Format live progress message while uploading to GoFile with real-time speed and progress bar."""
    dev_str = f" on {html.escape(device)}" if device and device != "unknown" else ""
    pct_clamped = max(0.0, min(100.0, pct))
    bar = make_progress_bar(pct_clamped)

    trans_str = format_file_size(transferred_bytes)
    total_str = format_file_size(total_bytes)
    lines = [
        f"◉  <b>{html.escape(host).upper()}  /  UPLOAD</b>",
        f"<b>{html.escape(rom_name)}</b>{dev_str}",
        "",
        f"<code>{bar}</code>  <b>{pct_clamped:.1f}%</b>",
        f"{trans_str} / {total_str}  ·  {completed_files}/{total_files} files",
    ]
    if speed_mb_s > 0:
        rate = f"{speed_mb_s:.1f} MB/s"
        if eta_seconds is not None:
            rate += f" · {format_duration(eta_seconds)} left"
        lines.append(rate)
    if current_filename:
        lines.append(f"\n<code>{html.escape(current_filename)}</code>")
    return "\n".join(lines)


def format_gofile_upload_completed(
    rom_name: str,
    device: str,
    download_url: str,
    uploaded_files: List[Dict[str, Any]],
    failed_files: Optional[List[str]] = None,
    duration_seconds: Optional[float] = None,
    avg_speed_mb_s: Optional[float] = None,
    host: str = "GoFile",
) -> str:
    """Format final GoFile upload completion message with links, duration, and speed stats."""
    dev_str = f" on {html.escape(device)}" if device and device != "unknown" else ""
    total_bytes = sum(f.get("size_bytes", 0) for f in uploaded_files)
    total_str = format_file_size(total_bytes) if total_bytes > 0 else ""

    lines = [
        f"◈  <b>UPLOAD COMPLETE  /  {html.escape(host).upper()}</b>",
        f"<b>{html.escape(rom_name)}</b>{dev_str}",
        f'↗  <a href="{html.escape(download_url, quote=True)}">Open release</a>',
    ]

    if duration_seconds is not None and duration_seconds > 0:
        dur_str = format_duration(duration_seconds)
        lines.append(f"⏳ <b>Duration:</b> {dur_str}")

    if avg_speed_mb_s is not None and avg_speed_mb_s > 0:
        lines.append(f"⚡ <b>Average Speed:</b> {avg_speed_mb_s:.1f} MB/s")

    size_tag = f", {total_str}" if total_str else ""
    lines.append(f"\n<b>FILES  /  {len(uploaded_files)}{size_tag}</b>")

    for idx, f in enumerate(uploaded_files, 1):
        icon = "📁" if f["name"].endswith(".zip") else ("💿" if f["name"].endswith(".img") else "📄")
        lines.append(f"{idx}. {icon} <code>{html.escape(f['name'])}</code> ({html.escape(f['size_formatted'])})")

    if failed_files:
        lines.append(f"\n⚠️ <i>Failed to upload {len(failed_files)} file(s).</i>")

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


def make_gofile_open_keyboard(download_url: str, host: str = "GoFile") -> Dict[str, Any]:
    """Button opening the GoFile folder in browser."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"↗ Open {host}",
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
    hidden_preview = _preview_anchor(preview_url)
    server_line = f"🖥️ Server: <b>{html.escape(server_name)}</b>\n" if server_name else ""

    sys_block = ""
    if system_stats:
        d_lbl = f" ({system_stats['disk_label']})" if system_stats.get("disk_label") else ""
        sys_block = (
            f"💻 System Resources:\n"
            f"  • CPU: {system_stats['cpu_pct']:.1f}%\n"
            f"  • RAM: {system_stats['ram_used_gb']:.1f} / {system_stats['ram_total_gb']:.1f} GB ({system_stats['ram_pct']:.1f}%)\n"
            f"  • NVMe{d_lbl}: {system_stats['disk_used_gb']:.1f} / {system_stats['disk_total_gb']:.1f} GB ({system_stats['disk_pct']:.1f}%)\n\n"
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
    hidden_preview = _preview_anchor(preview_url)
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
        d_lbl = f" ({system_stats['disk_label']})" if system_stats.get("disk_label") else ""
        lines.append(f"💾 NVMe{d_lbl} Usage: {system_stats['disk_used_gb']:.1f} / {system_stats['disk_total_gb']:.1f} GB ({system_stats['disk_pct']:.1f}%)")

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
    hidden_preview = _preview_anchor(preview_url)
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
                "text": "↗ Open kernel release",
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

    lines = [f"🟢  <b>{html.escape(server_name)}</b>  /  <i>ONLINE</i>"]
    if system_stats:
        d_lbl = f" ({system_stats['disk_label']})" if system_stats.get("disk_label") else ""
        lines.extend([
            f"<b>NODE  /  RESOURCES</b>",
            _stat_line("CPU", system_stats.get("cpu_pct", 0.0), 100, "%", system_stats.get("cpu_pct", 0.0)),
            _stat_line("RAM", system_stats.get("ram_used_gb", 0.0), system_stats.get("ram_total_gb", 0.0), "GB", system_stats.get("ram_pct", 0.0)),
            _stat_line(f"NVMe{html.escape(d_lbl)}", system_stats.get("disk_used_gb", 0.0), system_stats.get("disk_total_gb", 0.0), "GB", system_stats.get("disk_pct", 0.0)),
            "",
        ])

    active_build_count = len(builds)
    if active_build_count > 0:
        lines.append(f"⚙️ <b>Active ROM Builds ({active_build_count}):</b>")
        for b in builds:
            pct_val = b.get("pct", -1.0)
            pct_str = f"{pct_val:.1f}%" if pct_val >= 0 else "compiling"
            lines.append(f"  • {html.escape(str(b.get('rom_title', 'ROM')))} ({html.escape(str(b.get('device', 'device')))}) — {pct_str} [PID {b.get('pid')}]")


    active_kernel_count = len(kernels)
    if active_kernel_count > 0:
        lines.append(f"\n⚡ <b>Active Kernel Builds ({active_kernel_count}):</b>")
        for k in kernels:
            pct_val = k.get("pct", -1.0)
            pct_str = f"{pct_val:.1f}%" if pct_val >= 0 else "compiling"
            lines.append(f"  • {html.escape(str(k.get('kernel_title', 'Kernel')))} ({html.escape(str(k.get('device', 'device')))}) — {pct_str} [PID {k.get('pid')}]")


    active_sync_count = len(syncs)
    if active_sync_count > 0:
        lines.append(f"\n🔄 <b>Active Source Syncs ({active_sync_count}):</b>")
        for s in syncs:
            tot = s.get("total_projects", 0)
            synced = s.get("synced_projects", 0)
            pct = (synced / tot * 100.0) if tot > 0 else 0.0
            lines.append(f"  • {html.escape(str(s.get('rom_title', 'ROM')))} ({html.escape(str(s.get('branch', 'default')))}) — {pct:.1f}% ({synced}/{tot} projects) [PID {s.get('pid')}]")
    if not (builds or kernels or syncs):
        lines.append("No active builds or syncs.")

    return "\n".join(lines)


def format_multi_server_status(servers: List[Dict[str, Any]]) -> str:
    """Format combined status across multiple servers."""
    header = f"◈  <b>BUILD FLEET</b>  ·  {sum(s.get('online', True) for s in servers)}/{len(servers)} online\n"
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
        if not s.get("online", True) and s.get("last_seen"):
            last_seen = datetime.fromtimestamp(s["last_seen"], timezone.utc)
            block += f"\nLast response · {last_seen:%d %b %H:%M UTC}"
        blocks.append(block)

    body = "\n\n━━━━━━━━━━━━━━━━━━━━\n\n".join(blocks)
    footer = "\n\n<i>Browse releases with /roms · /kernels · /search</i>"
    return f"{header}\n{body}{footer}"

