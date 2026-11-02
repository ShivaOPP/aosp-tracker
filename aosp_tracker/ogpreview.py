"""OpenGraph Preview Banner Generator for serverhive.in.

Generates dynamic banner preview URLs:
https://ogpreview.serverhive.in/api/generate?title=...&avatar=...&theme=...&bio=...&version=6
"""

import urllib.parse
from typing import Optional


def build_ogpreview_url(
    rom_title: str,
    avatar_url: str,
    maintainer_name: str,
    device: Optional[str] = None,
    branch: Optional[str] = None,
    emoji: str = "❄️",
    theme: str = "midnightBloom",
    version: int = 6,
) -> str:
    """Constructs the serverhive ogpreview image URL."""
    if device and device.lower() != "source":
        tag = f"📱Device: {device}"
    elif branch:
        tag = f"🌿Branch: {branch}"
    else:
        tag = f"📱Device: {device or 'AOSP'}"

    bio = f"👤Maintainer: {maintainer_name} ‎ ‎ ‎ ‎ ‎ ‎ ‎ ‎ ‎ ‎ ‎ ‎ ‎{emoji} | {tag}"

    params = {
        "title": rom_title,
        "avatar": avatar_url,
        "theme": theme,
        "bio": bio,
        "version": str(version),
    }

    query = urllib.parse.urlencode(params)
    return f"https://ogpreview.serverhive.in/api/generate?{query}"
