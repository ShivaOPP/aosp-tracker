"""User Detection and Profile Management for Multi-User Build Systems.

Resolves maintainer display name, emoji, avatar, and target Telegram chat ID
for any Linux user (current or future) using local user config, global config,
git config, and system account data.
"""

import os
import pwd
import json
import configparser
from typing import Dict, Any, Optional

GLOBAL_CONFIG_PATH = "/etc/aosp_bot/config.json"


def get_user_home(username: str) -> Optional[str]:
    """Get home directory for username."""
    try:
        return pwd.getpwnam(username).pw_dir
    except KeyError:
        return f"/home/{username}"


def get_git_config_user(home_dir: str) -> Dict[str, str]:
    """Read user.name and user.email from user's .gitconfig."""
    info = {}
    gitconfig_path = os.path.join(home_dir, ".gitconfig")
    if os.path.isfile(gitconfig_path):
        try:
            config = configparser.ConfigParser()
            config.read(gitconfig_path)
            if "user" in config:
                if "name" in config["user"]:
                    info["name"] = config["user"]["name"]
                if "email" in config["user"]:
                    info["email"] = config["user"]["email"]
        except Exception:
            pass
    return info


def load_global_config() -> Dict[str, Any]:
    """Load system-wide global configuration."""
    if os.path.isfile(GLOBAL_CONFIG_PATH):
        try:
            with open(GLOBAL_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def load_user_config(home_dir: str) -> Dict[str, Any]:
    """Load per-user configuration from ~/.config/aosp_bot.json or ~/.aosp_bot.json."""
    candidates = [
        os.path.join(home_dir, ".config", "aosp_bot.json"),
        os.path.join(home_dir, ".aosp_bot.json"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    return {}


def resolve_user_profile(username: str, rom_name: Optional[str] = None) -> Dict[str, Any]:
    """Resolve full profile for any Linux user.
    
    Returns:
      {
        "username": username,
        "name": display_name,
        "emoji": emoji,
        "avatar": avatar_url,
        "chat_id": telegram_chat_id,
        "theme": theme
      }
    """
    home = get_user_home(username) or f"/home/{username}"
    global_cfg = load_global_config()
    user_cfg = load_user_config(home)

    global_users = global_cfg.get("users", {})
    global_user_profile = global_users.get(username, {})

    default_chat_id = global_cfg.get("default_chat_id")
    default_theme = global_cfg.get("default_theme", "midnightBloom")

    # 1. Name resolution
    # Priority: user_cfg -> global_cfg -> gitconfig -> gecos -> username
    PLACEHOLDER_NAMES = {"your name", "name", "user", "username", "root", "example"}
    name = user_cfg.get("name") or global_user_profile.get("name")
    if name and name.strip().lower() in PLACEHOLDER_NAMES:
        name = None

    if not name:
        git_info = get_git_config_user(home)
        candidate = git_info.get("name")
        if candidate and candidate.strip().lower() not in PLACEHOLDER_NAMES:
            name = candidate.strip()

    if not name:
        try:
            gecos = pwd.getpwnam(username).pw_gecos.split(",")[0].strip()
            if gecos and gecos.lower() not in PLACEHOLDER_NAMES:
                name = gecos
        except Exception:
            pass

    if not name:
        name = username.capitalize()

    # 2. Emoji resolution
    emoji = user_cfg.get("emoji") or global_user_profile.get("emoji") or "🔥"

    # 3. Chat ID resolution
    chat_id = user_cfg.get("chat_id") or global_user_profile.get("chat_id") or default_chat_id

    # 4. Avatar resolution
    avatar = user_cfg.get("avatar") or global_user_profile.get("avatar")

    # 5. Theme
    theme = user_cfg.get("theme") or global_user_profile.get("theme") or default_theme

    return {
        "username": username,
        "name": name,
        "emoji": emoji,
        "avatar": avatar,
        "chat_id": chat_id,
        "theme": theme,
        "home": home,
    }


def find_username_by_chat_id(chat_id: str) -> Optional[str]:
    """Find the Linux username associated with a given Telegram chat ID."""
    chat_str = str(chat_id).strip()
    if chat_str.startswith("-"):
        return None
    global_cfg = load_global_config()
    for u, prof in global_cfg.get("users", {}).items():
        if str(prof.get("chat_id", "")).strip() == chat_str:
            return u
    return None
