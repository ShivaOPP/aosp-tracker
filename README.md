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

Run the setup script from your checkout on the build server:

```bash
git clone https://github.com/ShivaOPP/aosp-tracker.git
cd aosp-tracker
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

### Connect workers to Server 5

Server 5 polls Telegram for commands and calls each worker's status and task
API. Workers continue to monitor their own builds and send Telegram build
notifications using the same bot token. Server 5 needs a route to each
worker's API. Use a private network or VPN for HTTP, or an HTTPS endpoint.
Restrict the worker API port (default TCP 8088) to Server 5 in the worker's
host firewall and cloud ingress rules.

Run the following from each installed node's tracker checkout. Copy the updated
checkout or the standalone `bin/aosp-node` script to nodes that lack it.

1. On the previous primary, if any, switch it to worker mode first. Repeat on
   the other workers, giving each a distinct name.

   ```bash
   sudo ./bin/aosp-node worker "Server 1"
   ```

   Enter a worker-specific API secret at the hidden prompt, or press Enter to
   keep the existing one. The command saves the role and restarts the service.
   For a fresh worker, initialize `/etc/aosp_bot/config.json` from the example,
   set the existing fleet bot token and chat settings, set
   `listen_for_commands` to false, clear the example `peer_servers`, and set a
   real `secret_key` before running `sudo ./setup.sh`.

2. On Server 5, activate Telegram polling.

   ```bash
   cd /home/ubuntu/aosp-tracker
   sudo ./bin/aosp-node primary
   sudo journalctl -u aosp-tracker -n 15 --no-pager
   ```

   Look for `Telegram updates polling loop started` and send `/status` to the
   bot. Only Server 5 should poll Telegram.

3. On Server 5, register each worker by its reachable address. Substitute the
   actual name, IP or hostname, and port.

   ```bash
   sudo ./bin/aosp-node add-worker "Server 1" http://10.0.0.11:8088
   ```

   Enter that worker's API secret. The command checks its authenticated
   `/status` response before adding or updating its `peer_servers` entry.
   Server 5 reloads the list on its next monitor cycle. Send `/status` to see
   the new node, or `/roms` to browse its releases.

The helper writes the config atomically with owner-only permissions. Use
`sudo` for local CLI commands that need to read this system configuration.

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


## Release uploads

Use `/search <device or ROM name>` to find a release. Admins can also open
`/download` or **Choose upload host** on a build completion message. The package
buttons transfer the displayed file directly from its build node. GoFile can
also publish a folder of images or the complete artifact set. The transfer card
shows bytes sent, throughput and elapsed-time estimates. **Cancel Upload** stops
an active transfer. The node runs up to two manually requested transfers at once.

| Destination | Files sent | Node setup |
| --- | --- | --- |
| GoFile | Selected artifacts in one folder | Existing configuration |
| [Pixeldrain](https://pixeldrain.com/api) | Main release package | Set `PIXELDRAIN_API_KEY` |
| [FuckingFast.net](https://fuckingfast.net/developers) | Main release package | Anonymous raw PUT |
| [Storage.to](https://storage.to/docs/api) | Main release package, up to 25 GB | Anonymous API; multipart uploads above 50 MB |
| [SendIt.sh](https://sendit.sh/) | Main release package, up to 10 GB | Anonymous PUT; links have service retention limits |
| [Temp.sh](https://temp.sh/) | Main release package, up to 4 GB | Anonymous multipart upload |
| [X0.at](https://x0.at/) | Packages up to 1 GiB | Anonymous multipart upload |
| [QURL.sh](https://qurl.sh/) | Packages up to 500 MB | Anonymous PUT |
| [Transfer (Whalebone)](https://transfer.whalebone.io/) | Packages up to 250 MB | Anonymous PUT |
| [Uguu.se](https://uguu.se/) | Packages up to 128 MB | Anonymous multipart upload |
| [TmpFiles.org](https://tmpfiles.org/) | Packages up to 100 MB | Anonymous multipart upload |

Install this revision on the Telegram primary and each uploading worker with
`sudo ./setup.sh` from its checkout. Setup preserves the existing JSON config.
Keep Telegram command polling enabled on the primary only. Each updated worker
advertises its available package hosts to the primary.

For Pixeldrain, place `PIXELDRAIN_API_KEY=your-key` in a root-owned, mode-0600
`/etc/aosp_bot/upload.env` on the uploading node, then restart
`aosp-tracker.service`. The CLI reads the variable from its own environment.
If an admin selects Pixeldrain before a key is configured, the bot requests the
key in that admin's private chat, deletes the submitted message, and stores the
key in the uploading node's root-only upload environment file. For a worker
upload, the worker API URL must use HTTPS and the shared API secret must be set.

```bash
aosp-tracker roms
aosp-tracker upload rom_0 --provider fuckingfast
aosp-tracker upload rom_0 --provider pixeldrain
aosp-tracker upload rom_0 --provider storage_to
aosp-tracker upload rom_0 --provider sendit
```

The bot lists package hosts according to the package size and the upload
capabilities of its build node. Host buttons show their per-file size limit;
Pixeldrain and GoFile display a plan/account limit because it varies by account.
Some free hosts retain files only briefly or limit downloads. BuzzHeavier,
BashUpload, Catbox/Litterbox and pomf.lain.la did not pass the live upload check
in this environment, so they are not offered.
`file.io` no longer provides its former anonymous upload API, `oshi.at` was
unreachable, and `Plowshare` is a client for other file hosts rather than a
host. `0x0.st` prohibits automated build artifacts/backups. The remaining
services from the original list did not provide a sufficiently verified,
appropriate ROM-upload route. Send.vis.ee was not live-upload tested, so it is
not offered either.

Repeated library requests share a 20-second cache. **Refresh library** requests
a fresh scan. Status and health requests run independently of catalog scans.
If a worker stops responding, `/status` reports its last successful status
response observed since the primary started, when available.


## Running the tests

From the repository root, run:

```bash
python3 -m unittest discover -s tests -v
```

The release-flow tests use mocks for uploads and Telegram calls.
