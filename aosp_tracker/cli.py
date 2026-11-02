"""Command Line Interface for AOSP Tracker.

Provides commands to view status, manage user profile, test Telegram setup,
and control the system daemon.
"""

import os
import sys
import json
import asyncio
import logging
import argparse
from typing import Optional

from .monitor import AospMonitor
from .user_detector import (
    resolve_user_profile,
    load_global_config,
    get_user_home,
    GLOBAL_CONFIG_PATH,
)
from .telegram_client import TelegramClient
from .ogpreview import build_ogpreview_url
from .formatter import format_build_in_progress


def cmd_status(args):
    """Show currently detected active builds and repo syncs."""
    monitor = AospMonitor()
    builds = monitor.find_build_processes()
    syncs = monitor.find_sync_processes()

    print("========================================")
    print("        AOSP Tracker Status             ")
    print("========================================")

    if builds:
        print(f"\n⚙️  Active AOSP Builds ({len(builds)}):")
        for pid, user, bdir, ctime in builds:
            prof = resolve_user_profile(user)
            print(f"  • PID: {pid} | User: {user} ({prof.get('name')})")
            print(f"    Tree: {bdir}")
    else:
        print("\n⚙️  No active AOSP builds found.")

    if syncs:
        print(f"\n🔄  Active Source Syncs ({len(syncs)}):")
        for pid, user, rdir, ctime in syncs:
            prof = resolve_user_profile(user)
            print(f"  • PID: {pid} | User: {user} ({prof.get('name')})")
            print(f"    Repo: {rdir}")
    else:
        print("🔄  No active source syncs found.")

    print("\nGlobal Config:")
    cfg = load_global_config()
    print(f"  Bot Token: {'Configured' if cfg.get('bot_token') else 'Missing'}")
    print(f"  Default Chat ID: {cfg.get('default_chat_id', 'Not set')}")
    print("========================================")


def cmd_test(args):
    """Send a test message to Telegram to verify setup."""
    username = os.environ.get("USER") or "ubuntu"
    profile = resolve_user_profile(username)
    global_cfg = load_global_config()

    bot_token = global_cfg.get("bot_token")
    if not bot_token:
        print("Error: No bot_token found in /etc/aosp_bot/config.json")
        sys.exit(1)

    target_chat_id = args.chat_id or profile.get("chat_id") or global_cfg.get("default_chat_id")
    if not target_chat_id:
        print("Error: No chat_id specified. Use --chat-id <id> or configure ~/.config/aosp_bot.json")
        sys.exit(1)

    tg = TelegramClient(bot_token)

    if args.failed:
        from .formatter import format_build_failed
        preview_url = build_ogpreview_url(
            rom_title="RisingOS",
            avatar_url="https://avatars.githubusercontent.com/u/197353021?v=4",
            maintainer_name=profile.get("name", "shivaop"),
            device="peridot",
            emoji=profile.get("emoji", "🔥"),
            theme="midnightBloom",
        )
        sample_error = (
            "FAILED: out/soong/build.ninja\n"
            "error: ninja: build stopped: subcommand failed.\n"
            "[ 82% 164010/197048] //frameworks/base:framework-minus-apex clang++ compilation error:\n"
            "fatal error: 'android/hardware/camera/device/3.4/ICameraDevice.h' file not found"
        )
        text = format_build_failed(
            preview_url=preview_url,
            rom_title="RisingOS",
            device="peridot",
            user_name=profile.get("name", "shivaop"),
            user_emoji=profile.get("emoji", "🔥"),
            start_time_epoch=os.path.getmtime("/home/ubuntu/luna/out/error.3.log") if os.path.exists("/home/ubuntu/luna/out/error.3.log") else 0,
            duration_seconds=12 * 60 + 34,
            finished_actions=164010,
            total_actions=197048,
            variant="userdebug",
            build_type="release",
            release="cp2a",
            error_summary=sample_error,
        )
        print(f"Sending test build-failed notification to chat {target_chat_id}...")
        msg_id = tg.send_message(chat_id=target_chat_id, text=text, parse_mode="Markdown")
        if msg_id:
            print(f"✅ Success! Sent test failed message ID: {msg_id}")
            # Also attach error.log
            sample_file = "/tmp/sample_error.log"
            with open(sample_file, "w") as sf:
                sf.write(sample_error + "\n\nFull stack trace:\n" + ("=" * 40) + "\nBuild log attached.")
            tg.send_document(
                chat_id=target_chat_id,
                file_path=sample_file,
                caption="⚠️ error.log for RisingOS on peridot",
                reply_to_message_id=msg_id,
                custom_filename="RisingOS_peridot_error.log",
            )
            print("✅ Success! Sent error.log attachment to Telegram.")
        return
    preview_url = build_ogpreview_url(
        rom_title="VoltageOS",
        avatar_url="https://avatars.githubusercontent.com/u/81792437?v=4",
        maintainer_name=profile.get("name", "shivaop"),
        device="porsche",
        emoji=profile.get("emoji", "🔥"),
        theme="midnightBloom",
    )

    text = format_build_in_progress(
        preview_url=preview_url,
        rom_title="VoltageOS",
        device="porsche",
        user_name=profile.get("name", "shivaop"),
        user_emoji=profile.get("emoji", "🔥"),
        current_actions=19,
        finished_actions=220965,
        total_actions=221489,
        elapsed_seconds=18 * 60 + 12,
        eta_seconds=31 * 60 + 4,
        variant="user",
        build_type="release",
        release="cp2a",
    )

    print(f"Sending test notification to chat {target_chat_id}...")
    msg_id = tg.send_message(chat_id=target_chat_id, text=text, parse_mode="Markdown")
    if msg_id:
        print(f"✅ Success! Sent test message ID: {msg_id}")
    else:
        print("❌ Failed to send message. Check bot token, chat ID, and network.")


def cmd_profile(args):
    """View or configure current user's profile."""
    username = os.environ.get("USER") or "ubuntu"
    home = get_user_home(username) or os.path.expanduser("~")
    cfg_dir = os.path.join(home, ".config")
    cfg_file = os.path.join(cfg_dir, "aosp_bot.json")

    current_data = {}
    if os.path.isfile(cfg_file):
        try:
            with open(cfg_file, "r") as f:
                current_data = json.load(f)
        except Exception:
            pass

    if args.name:
        current_data["name"] = args.name
    if args.emoji:
        current_data["emoji"] = args.emoji
    if args.chat_id:
        current_data["chat_id"] = args.chat_id
    if args.avatar:
        current_data["avatar"] = args.avatar
    if args.theme:
        current_data["theme"] = args.theme

    if any([args.name, args.emoji, args.chat_id, args.avatar, args.theme]):
        os.makedirs(cfg_dir, exist_ok=True)
        with open(cfg_file, "w") as f:
            json.dump(current_data, f, indent=2)
        print(f"✅ Saved user profile to {cfg_file}")

    resolved = resolve_user_profile(username)
    print("\nCurrent Effective Profile:")
    print(f"  Linux User: {resolved['username']}")
    print(f"  Maintainer Name: {resolved['name']}")
    print(f"  Emoji: {resolved['emoji']}")
    print(f"  Chat ID: {resolved['chat_id']}")
    print(f"  Avatar: {resolved['avatar'] or '(Auto from ROM)'}")
    print(f"  Theme: {resolved['theme']}")
    print(f"  Config File: {cfg_file}")


def cmd_roms(args):
    """Scan and list all ROMs present on the server."""
    from .rom_scanner import scan_all_roms
    roms = scan_all_roms(force_refresh=True)
    print("========================================")
    print("       ROMs Present on Server           ")
    print("========================================")
    if not roms:
        print("No ROMs found.")
        return
    for r in roms:
        tag = f"({r['device']})" if r.get("device") and r["device"] != "unknown" else ""
        print(f"\n[{r['id']}] {r['name']} {tag}")
        print(f"  Package: {r['main_zip_name']} ({r['main_zip_size']})")
        print(f"  Total Size: {r['total_size_formatted']} ({r['file_count']} files)")
        print(f"  Directory: {r['directory']}")
    print("\n========================================")


def cmd_upload(args):
    """Upload a ROM's artifacts directly to GoFile."""
    from .rom_scanner import scan_all_roms, get_rom_by_id
    from .gofile import upload_artifacts_batch
    roms = scan_all_roms()
    target = get_rom_by_id(args.rom_id)
    if not target:
        for r in roms:
            if args.rom_id.lower() in r["name"].lower() or args.rom_id.lower() in r["device"].lower():
                target = r
                break
    if not target:
        print(f"Error: ROM '{args.rom_id}' not found. Use 'aosp-tracker roms' to list.")
        sys.exit(1)

    print(f"Uploading {target['name']} ({target['device']}) to GoFile...")
    def on_prog(snap, *args):
        if isinstance(snap, dict):
            pct = snap.get("pct", 0.0)
            speed = snap.get("speed_mb_s", 0.0)
            trans = format_file_size(snap.get("transferred_bytes", 0))
            tot = format_file_size(snap.get("total_bytes", 0))
            cur = snap.get("current_filename") or ""
            print(f"\r  [{pct:5.1f}%] {trans} / {tot} | {speed:5.1f} MB/s | {cur[:35]}", end="", flush=True)
        else:
            idx, total, fname, sz = snap, args[0], args[1], args[2]
            print(f"  [{idx}/{total}] Uploading {fname} ({sz})...")

    res = upload_artifacts_batch(target["files_to_upload"], progress_callback=on_prog)
    print()
    if res.get("success"):
        dur = f" in {res.get('duration_seconds', 0):.1f}s" if res.get('duration_seconds') else ""
        spd = f" (avg {res.get('avg_speed_mb_s', 0):.1f} MB/s)" if res.get('avg_speed_mb_s') else ""
        print(f"\n✅ Upload Successful{dur}{spd}!")
        print(f"📂 GoFile Folder: {res['download_page']}")
        for f in res["uploaded_files"]:
            print(f"  • {f['name']} ({f['size_formatted']})")
    else:
        print("\n❌ Upload failed.")


def cmd_daemon(args):
    """Run daemon in foreground."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    monitor = AospMonitor()
    try:
        asyncio.run(monitor.run())
    except KeyboardInterrupt:
        print("\nDaemon stopped by user.")


def main():
    parser = argparse.ArgumentParser(description="AOSP Telegram Build and Sync Tracker")
    subparsers = parser.add_subparsers(dest="command")

    # status
    p_status = subparsers.add_parser("status", help="Show active builds and syncs")
    p_status.set_defaults(func=cmd_status)

    # roms
    p_roms = subparsers.add_parser("roms", help="Scan and list all ROMs present on the server")
    p_roms.set_defaults(func=cmd_roms)

    # upload
    p_upload = subparsers.add_parser("upload", help="Upload ROM files to GoFile")
    p_upload.add_argument("rom_id", help="ROM ID (e.g. rom_0) or keyword (e.g. rising, peridot)")
    p_upload.set_defaults(func=cmd_upload)

    # test
    p_test = subparsers.add_parser("test", help="Send a test message to Telegram")
    p_test.add_argument("--chat-id", help="Telegram chat ID for test message")
    p_test.add_argument("--failed", action="store_true", help="Send test build failure notification with error.log")
    p_test.set_defaults(func=cmd_test)

    # profile
    p_prof = subparsers.add_parser("profile", help="View or edit maintainer profile")
    p_prof.add_argument("--name", help="Maintainer display name")
    p_prof.add_argument("--emoji", help="Maintainer emoji suffix (e.g. ❄️)")
    p_prof.add_argument("--chat-id", help="Telegram chat/channel ID")
    p_prof.add_argument("--avatar", help="Custom avatar URL")
    p_prof.add_argument("--theme", help="ogpreview theme (default: midnightBloom)")
    p_prof.set_defaults(func=cmd_profile)

    # daemon
    p_daemon = subparsers.add_parser("daemon", help="Run monitor daemon in foreground")
    p_daemon.set_defaults(func=cmd_daemon)

    args = parser.parse_args()
    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
