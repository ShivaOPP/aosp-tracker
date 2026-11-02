#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Installing AOSP Telegram Tracker ==="

# 0. Check and install dependencies
if ! python3 -c "import psutil" >/dev/null 2>&1; then
    echo "Installing python3-psutil dependency..."
    sudo apt-get update -qq && sudo apt-get install -y -qq python3-psutil
fi

# 1. Install CLI binary symlinks
sudo ln -sf "${SCRIPT_DIR}/bin/aosp-tracker" /usr/local/bin/aosp-tracker
sudo ln -sf "${SCRIPT_DIR}/bin/aosp-tracker" /usr/local/bin/aosp-bot
echo " Installed /usr/local/bin/aosp-tracker and /usr/local/bin/aosp-bot"

# 2. Setup /etc/aosp_bot/config.json
sudo mkdir -p /etc/aosp_bot
if [ ! -f /etc/aosp_bot/config.json ]; then
    sudo cp "${SCRIPT_DIR}/config.example.json" /etc/aosp_bot/config.json
    sudo chmod 644 /etc/aosp_bot/config.json
    echo " Initialized /etc/aosp_bot/config.json"
else
    echo " Preserved existing /etc/aosp_bot/config.json"
fi

# 3. Install and start systemd service
SERVICE_FILE="/etc/systemd/system/aosp-tracker.service"
sudo cp "${SCRIPT_DIR}/service/aosp-tracker.service" "${SERVICE_FILE}"
sudo sed -i "s|/home/ubuntu/Downloads/aosp|${SCRIPT_DIR}|g" "${SERVICE_FILE}"
sudo systemctl daemon-reload
sudo systemctl enable aosp-tracker.service
sudo systemctl restart aosp-tracker.service

echo " AOSP Tracker Daemon enabled and started as a systemd service!"
echo ""
echo "Commands available:"
echo "  aosp-tracker status   - View active builds and syncs"
echo "  aosp-tracker test     - Send a test message to Telegram"
echo "  aosp-tracker profile  - Configure maintainer profile"
echo "  sudo systemctl status aosp-tracker - Check service status"
