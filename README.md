# AOSP Build and Source Sync Telegram Tracker

A multi-user background daemon that detects and tracks **Android Open Source Project (AOSP)** source sync (`repo sync`) and ROM compilation progress (`soong_ui`, `ninja`, `siso`, `m bacon`, `brunch`) across all users on a build server, sending real-time updates and completion summaries to Telegram with dynamic [serverhive](https://serverhive.in) OpenGraph preview cards.

---

## Features

- **Automated Process Detection**: Continuously monitors builds and source sync across all system users (e.g. `ubuntu`, `mrjellyxd`, and any future users created on the system) without requiring wrappers or changes to command workflows.
- **Dynamic ServerHive Banner Cards**: Generates preview cards via `https://ogpreview.serverhive.in/api/generate` with custom themes, ROM avatars, device tags, and maintainer information.
- **Live Progress Reporting**:
  - Parses `out/build_progress.pb` in real time using a pure-Python varint protobuf decoder.
  - Reports current running actions, finished vs total actions, percentage, 10-block progress bar (`[█████████░] 99.76%`), elapsed time, and rolling-window ETA.
  - Updates the existing Telegram message in-place every 10 seconds.
- **Artifact Discovery & SHA256 Checksums**:
  - Upon build completion, automatically discovers generated flashable artifacts (`.zip`, `vendor_boot.img`, `vbmeta.img`, `dtbo.img`, `system.img`, `boot.img`, `super_empty.img`, `recovery.img`, OTA `.json`).
  - Formats file sizes (`GB`, `MB`, `KB`, `B`) and computes SHA256 checksums.
- **Multi-User & Future-User Ready**:
  - Global config (`/etc/aosp_bot/config.json`) sets the default bot token and chat destination.
  - Each developer can optionally customize their maintainer name, emoji, avatar, and dedicated chat/channel ID via `~/.config/aosp_bot.json` or `aosp-tracker profile`.
  - Any future user created via `useradd` works automatically with zero setup (inheriting Git config user name and global chat settings).

---

## Message Format Previews

### ⚙️ Build In Progress
```
⚙️ Build In Progress (19 tasks running) for VoltageOS on porsche
👤 User: Rei 陈丽             ❄️
📊 Progress: [█████████░] 99.76%
   Actions: 220965/221489
⏳ Elapsed: 18 minutes, 12 seconds
⏱️ ETA: 31 minutes, 4 seconds

🔧 Configuration:
  Variant: user
  Type: release
  Release: cp2a

Optimizing for maximum developer satisfaction. Please hold. 📈
```

### ✅ Build Completed
```
✅ Build Completed for Mist-OS on porsche
User: Rei 陈丽             ❄️
Started: 2026-10-19 02:30:22 PM UTC+05:30
Duration: 26 minutes, 41 seconds
📊 Build Stats: 231337/231337 actions
🔧 Configuration:
  • Variant: user
  • Type: release
  • Release: cp2a

🎉 Build Artifact(s) Uploaded:
1. File: MistOS-5.1-Beta-17.0-GAPPS-20261019-0902-porsche-UNOFFICIAL.zip
     Type: ZIP | Size: 3.39 GB
     SHA256: 00843716108429a26f79440fe70d76077385f18ec187223f533a9fce6b2ae794
2. File: vendor_boot.img
     Type: IMG | Size: 192.00 MB
     SHA256: b0201cdd1d851afdc39517e020ee8b2ecfc8cd8763ef5675b743309eca39740d
3. File: vbmeta.img
     Type: IMG | Size: 64.00 KB
     SHA256: 225087c2bfd137d7c0b96c5da90c4bc9fe39ab97c57b24dc765d1fb6ab695e10
...
```

---

## Installation & Setup

Run the setup script with sudo:
```bash
cd /home/ubuntu/Downloads/aosp
sudo ./setup.sh
```

This installs:
1. System commands `/usr/local/bin/aosp-tracker` and `/usr/local/bin/aosp-bot`.
2. Global configuration in `/etc/aosp_bot/config.json`.
3. Systemd background daemon `aosp-tracker.service`.

---

## Configuration

### Global Config: `/etc/aosp_bot/config.json`
```json
{
  "bot_token": "YOUR_TELEGRAM_BOT_TOKEN",
  "default_chat_id": "-100XXXXXXXXXX",
  "default_theme": "midnightBloom",
  "poll_interval": 5.0,
  "update_interval": 10.0,
  "users": {
    "ubuntu": {
      "name": "Rei 陈丽",
      "emoji": "❄️",
      "avatar": "https://avatars.githubusercontent.com/u/81792437?v=4",
      "chat_id": "6340997509"
    }
  }
}
```

### Per-User Customization: `~/.config/aosp_bot.json`
Any user on the system can configure their own settings:
```bash
aosp-tracker profile --name "Rei 陈丽" --emoji "❄️" --chat-id "6340997509"
```

### Multi-Server Setup (Primary + Worker Nodes)

Track multiple build servers concurrently with a single Telegram Bot:

* **Primary Server (Server 1)** listens for Telegram slash commands (`/status`, `/roms`, etc.) and polls peer nodes.
* **Worker Server (Server 2)** monitors local builds/syncs, sends notifications with server tags, and serves an internal status API.

**Server 1 (Primary) `/etc/aosp_bot/config.json`:**
```json
{
  "server_name": "Server 1 (India)",
  "listen_for_commands": true,
  "enable_api": true,
  "api_port": 8088,
  "secret_key": "YOUR_SHARED_SECRET_KEY",
  "peer_servers": [
    {
      "name": "Server 2",
      "url": "http://137.23.44.237:8088/status",
      "secret_key": "YOUR_SHARED_SECRET_KEY"
    }
  ]
}
```

**Server 2 (Worker) `/etc/aosp_bot/config.json`:**
```json
{
  "server_name": "Server 2",
  "listen_for_commands": false,
  "enable_api": true,
  "api_port": 8088,
  "secret_key": "YOUR_SHARED_SECRET_KEY",
  "peer_servers": []
}
```

---

## CLI Usage

- **Check Active Builds & Syncs**:
  ```bash
  aosp-tracker status
  ```
- **Send Test Card to Telegram**:
  ```bash
  aosp-tracker test
  ```
- **Manage User Profile**:
  ```bash
  aosp-tracker profile
  ```
- **Check Daemon Logs**:
  ```bash
  sudo journalctl -u aosp-tracker -f
  ```
