"""ROM and Target Detection for AOSP builds.

Extracts ROM name, device, variant, release, build type, and avatar from
soong.log, Kati make variables, environment, or repository manifests.
"""

import os
import re
from typing import Dict, Any, Optional

# High-resolution GitHub organization avatars for known custom ROMs
ROM_AVATARS = {
    "voltageos": "https://avatars.githubusercontent.com/u/81792437?v=4",
    "voltage": "https://avatars.githubusercontent.com/u/81792437?v=4",
    "mist-os": "https://avatars.githubusercontent.com/u/140271839?v=4",
    "mistos": "https://avatars.githubusercontent.com/u/140271839?v=4",
    "mist": "https://avatars.githubusercontent.com/u/140271839?v=4",
    "risingos": "https://avatars.githubusercontent.com/u/197353021?v=4",
    "rising": "https://avatars.githubusercontent.com/u/197353021?v=4",
    "crdroid": "https://avatars.githubusercontent.com/u/6179331?v=4",
    "crdroidandroid": "https://avatars.githubusercontent.com/u/6179331?v=4",
    "derpfest": "https://avatars.githubusercontent.com/u/55519395?v=4",
    "project-infinity-x": "https://avatars.githubusercontent.com/u/104193562?v=4",
    "infinity-x": "https://avatars.githubusercontent.com/u/104193562?v=4",
    "infinity": "https://avatars.githubusercontent.com/u/104193562?v=4",
    "lineageos": "https://avatars.githubusercontent.com/u/23307567?v=4",
    "lineage": "https://avatars.githubusercontent.com/u/23307567?v=4",
    "pixelos": "https://avatars.githubusercontent.com/u/90899268?v=4",
    "evolutionx": "https://avatars.githubusercontent.com/u/49089606?v=4",
    "evolution-x": "https://avatars.githubusercontent.com/u/49089606?v=4",
    "paranoidandroid": "https://avatars.githubusercontent.com/u/2967670?v=4",
    "arrowos": "https://avatars.githubusercontent.com/u/40351870?v=4",
    "havoc-os": "https://avatars.githubusercontent.com/u/38166549?v=4",
    "lunaris": "https://avatars.githubusercontent.com/u/193316573?v=4",
    "lunaris-aosp": "https://avatars.githubusercontent.com/u/193316573?v=4",
    "lunaris aosp": "https://avatars.githubusercontent.com/u/193316573?v=4",
    "uwu": "https://avatars.githubusercontent.com/u/269222443?v=4",
    "uwu-aosp": "https://avatars.githubusercontent.com/u/269222443?v=4",
    "uwuaosp": "https://avatars.githubusercontent.com/u/269222443?v=4",
    "uwu aosp": "https://avatars.githubusercontent.com/u/269222443?v=4",
    "matrixx": "https://avatars.githubusercontent.com/u/130198031?v=4",
    "projectmatrixx": "https://avatars.githubusercontent.com/u/130198031?v=4",
    "superior": "https://avatars.githubusercontent.com/u/41551679?v=4",
    "superioros": "https://avatars.githubusercontent.com/u/41551679?v=4",
    "bliss": "https://avatars.githubusercontent.com/u/10411319?v=4",
    "blissroms": "https://avatars.githubusercontent.com/u/10411319?v=4",
    "aosp": "https://avatars.githubusercontent.com/u/81792437?v=4",
}

DEFAULT_AVATAR = "https://avatars.githubusercontent.com/u/81792437?v=4"


def get_rom_avatar(rom_name: str) -> str:
    """Resolve the avatar URL for a given ROM name."""
    clean = rom_name.lower().replace(" ", "").replace("_", "").replace("-", "")
    for key, url in ROM_AVATARS.items():
        k_clean = key.lower().replace(" ", "").replace("_", "").replace("-", "")
        if k_clean in clean or clean in k_clean:
            return url
    return DEFAULT_AVATAR


def detect_rom_name_from_string(s: str) -> Optional[str]:
    """Detect known ROM name from strings, giving priority to custom ROM brands."""
    lower = s.lower()
    # Check specific custom ROMs first (do NOT check lineage first, as many ROMs have lineage_product)
    if "rising" in lower:
        return "RisingOS"
    if "voltage" in lower:
        return "VoltageOS"
    if "mist" in lower:
        return "Mist-OS"
    if "crdroid" in lower:
        return "crDroid"
    if "derp" in lower:
        return "DerpFest"
    if "infinity" in lower:
        return "Infinity-X"
    if "uwu" in lower:
        return "UwU-AOSP"
    if "lunaris" in lower:
        return "Lunaris-AOSP"
    if "matrixx" in lower:
        return "ProjectMatrixx"
    if "superior" in lower:
        return "SuperiorOS"
    if "bliss" in lower:
        return "BlissROM"
    # Never match pixel-style or pixel_style theme repos as PixelOS
    if "pixelos" in lower or (lower == "pixel") or (lower.startswith("pixel_") and not any(k in lower for k in ["style", "prop", "framework", "launcher", "theme"])):
        return "PixelOS"
    if "evolution" in lower:
        return "EvolutionX"
    if "paranoid" in lower:
        return "ParanoidAndroid"
    if "arrow" in lower:
        return "ArrowOS"
    if "havoc" in lower:
        return "Havoc-OS"
    if "lineage" in lower and not lower.startswith("lineage_"):
        return "LineageOS"
    return None


def detect_rom_from_tree(build_dir: str) -> Optional[str]:
    """Check vendor directory or .repo manifests to find ROM identity."""
    # 1. Check build directory name itself (e.g. /home/mrjellyxd/rise)
    dir_name = os.path.basename(os.path.abspath(build_dir)).lower()
    if "rise" in dir_name or "rising" in dir_name:
        return "RisingOS"
    if "voltage" in dir_name:
        return "VoltageOS"
    if "mist" in dir_name:
        return "Mist-OS"
    if "uwu" in dir_name:
        return "UwU-AOSP"
    if "luna" in dir_name:
        return "Lunaris-AOSP"

    # 2. Authoritative check: .repo/manifests.git/config
    manifest_config = os.path.join(build_dir, ".repo", "manifests.git", "config")
    if os.path.isfile(manifest_config):
        try:
            with open(manifest_config, "r", errors="ignore") as f:
                content = f.read()
                candidate = detect_rom_name_from_string(content)
                if candidate and candidate != "LineageOS":
                    return candidate
        except Exception:
            pass

    # 3. Check vendor/<rom>
    vendor_dir = os.path.join(build_dir, "vendor")
    if os.path.isdir(vendor_dir):
        try:
            entries = os.listdir(vendor_dir)
            known_brands = ["uwu", "rising", "voltage", "mist", "crdroid", "derp", "infinity", "lunaris", "pixelos", "evolution"]

            # First pass: check for full ROM vendor tree (contains config, build, overlay, or release)
            for brand in known_brands:
                for item in entries:
                    if brand in item.lower():
                        item_path = os.path.join(vendor_dir, item)
                        if os.path.isdir(item_path):
                            try:
                                subdirs = os.listdir(item_path)
                                if any(k in subdirs for k in ["config", "build", "overlay", "release", "products"]):
                                    res = detect_rom_name_from_string(item)
                                    if res:
                                        return res
                            except Exception:
                                pass

            # Second pass: fallback if vendor directory doesn't have standard subdirs
            for brand in known_brands:
                for item in entries:
                    if brand in item.lower():
                        res = detect_rom_name_from_string(item)
                        if res:
                            return res
        except Exception:
            pass

    return None


def parse_soong_log(soong_log_path: str, build_dir: Optional[str] = None) -> Dict[str, Any]:
    """Extract build variables from out/soong.log."""
    info: Dict[str, Any] = {
        "device": "generic",
        "product": "aosp",
        "variant": "user",
        "type": "release",
        "release": "unknown",
        "rom": None,
        "start_time": None,
        "display_version": None,
        "out": None,
        "out_dir": None,
    }

    # If build directory indicates the ROM (e.g. /home/mrjellyxd/rise), initialize with it
    if build_dir:
        tree_rom = detect_rom_from_tree(build_dir)
        if tree_rom:
            info["rom"] = tree_rom

    if not os.path.exists(soong_log_path):
        if not info["rom"]:
            info["rom"] = "AOSP"
        # If soong.log doesn't exist yet, try to inspect the product directory on disk
        if build_dir:
            p_base = os.path.join(build_dir, "out", "target", "product")
            if os.path.isdir(p_base):
                try:
                    cands = [
                        d for d in os.listdir(p_base)
                        if os.path.isdir(os.path.join(p_base, d))
                        and d not in ["generic", "obj", "common", "install", "symbols", "fake_packages", "temp", "tmp"]
                        and not d.startswith(".")
                    ]
                    if len(cands) == 1:
                        info["device"] = cands[0]
                    elif len(cands) > 1:
                        cands.sort(key=lambda d: os.path.getmtime(os.path.join(p_base, d)), reverse=True)
                        info["device"] = cands[0]
                except Exception:
                    pass
        return info

    lineage_build = None
    target_device_dir = None

    try:
        with open(soong_log_path, "r", encoding="utf-8", errors="ignore") as f:
            for idx, line in enumerate(f):
                if idx > 5000:
                    break

                # Extract first timestamp as start time
                if info["start_time"] is None:
                    ts_match = re.match(r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})", line)
                    if ts_match:
                        info["start_time"] = ts_match.group(1)

                # TARGET_DEVICE matches: TARGET_DEVICE marble, TARGET_DEVICE = marble, TARGET_DEVICE := marble, TARGET_DEVICE="marble"
                m = re.search(r"(?:^|[\s\[])TARGET_DEVICE\b\s*(?::=|[:=]|\s)\s*[\"']?([a-zA-Z0-9_-]+)[\"']?", line)
                if m:
                    info["device"] = m.group(1)

                m = re.search(r"(?:^|[\s\[])TARGET_PRODUCT\b\s*(?::=|[:=]|\s)\s*[\"']?([a-zA-Z0-9_-]+)[\"']?", line)
                if m:
                    info["product"] = m.group(1)
                    prod = m.group(1).lower()
                    if not prod.startswith("lineage_"):
                        candidate = detect_rom_name_from_string(prod)
                        if candidate:
                            info["rom"] = candidate

                m = re.search(r"(?:^|[\s\[])TARGET_BUILD_VARIANT\b\s*(?::=|[:=]|\s)\s*[\"']?([a-zA-Z0-9_-]+)[\"']?", line)
                if m:
                    info["variant"] = m.group(1)

                m = re.search(r"(?:^|[\s\[])TARGET_BUILD_TYPE\b\s*(?::=|[:=]|\s)\s*[\"']?([a-zA-Z0-9_-]+)[\"']?", line)
                if m:
                    info["type"] = m.group(1)

                m = re.search(r"(?:^|[\s\[])TARGET_RELEASE\b\s*(?::=|[:=]|\s)\s*[\"']?([a-zA-Z0-9_-]+)[\"']?", line)
                if m:
                    info["release"] = m.group(1)

                m = re.search(r"(?:^|[\s\[])OUT\s*[:=]\s*[\"']?([^\s\"'\],]+)[\"']?", line)
                if m and not info["out"]:
                    info["out"] = m.group(1)

                m = re.search(r"(?:^|[\s\[])OUT_DIR\s*[:=]\s*[\"']?([^\s\"'\],]+)[\"']?", line)
                if m and not info["out_dir"]:
                    info["out_dir"] = m.group(1)

                m = re.search(r"(?:^|[\s\[])LINEAGE_BUILD\b\s*(?::=|[:=]|\s)\s*[\"']?([a-zA-Z0-9_-]+)[\"']?", line)
                if m:
                    lineage_build = m.group(1)

                m = re.search(r"TARGET_DEVICE_DIR\s*(?::=|[:=])?\s*device/[^/]+/([a-zA-Z0-9_-]+)", line)
                if m:
                    target_device_dir = m.group(1)

                m = re.search(r"LINEAGE_DISPLAY_VERSION\s*(?::=|[:=])?\s*[\"']?([a-zA-Z0-9_.-]+)[\"']?", line)
                if m:
                    info["display_version"] = m.group(1)
                    candidate = detect_rom_name_from_string(m.group(1))
                    if candidate:
                        info["rom"] = candidate

                # Custom ROM version variables always override generic product names
                if "RISING_" in line or "RISING_VERSION" in line:
                    info["rom"] = "RisingOS"
                elif "VOLTAGE_" in line or "VOLTAGE_VERSION" in line:
                    info["rom"] = "VoltageOS"
                elif "MIST_" in line or "MIST_VERSION" in line:
                    info["rom"] = "Mist-OS"
                elif "CRDROID_" in line or "CR_VERSION" in line:
                    info["rom"] = "crDroid"
                elif "DERP_" in line or "DERPFEST_" in line:
                    info["rom"] = "DerpFest"
                elif "INFINITY_" in line:
                    info["rom"] = "Infinity-X"
                elif "UWU_" in line or "UWU_VERSION" in line:
                    info["rom"] = "UwU-AOSP"
                elif "LUNARIS_" in line:
                    info["rom"] = "Lunaris-AOSP"
                elif "MATRIXX_" in line or "MATRIXX_VERSION" in line:
                    info["rom"] = "ProjectMatrixx"
                elif "SUPERIOR_" in line:
                    info["rom"] = "SuperiorOS"
                elif "BLISS_" in line or "BLISS_VERSION" in line:
                    info["rom"] = "BlissROM"
                elif "PIXELOS_" in line:
                    info["rom"] = "PixelOS"
                elif "EVOLUTION_" in line:
                    info["rom"] = "EvolutionX"

    except Exception:
        pass

    # Derive device if missing or generic
    if info["device"] in [None, "", "generic"]:
        if info.get("out"):
            cand = os.path.basename(info["out"].rstrip("/\\"))
            if cand and cand not in ["generic", "obj", "product"]:
                info["device"] = cand
        elif lineage_build:
            info["device"] = lineage_build
        elif target_device_dir:
            info["device"] = target_device_dir
        elif info.get("product") and info["product"] != "aosp":
            prod = info["product"]
            if "_" in prod:
                parts = prod.split("_")
                last = parts[-1]
                if last not in ["generic", "arm64", "x86_64", "64", "user", "userdebug"]:
                    info["device"] = last
                elif len(parts) > 2 and parts[-2] not in ["generic", "arm64"]:
                    info["device"] = parts[-2]

    # If still generic, inspect filesystem if build_dir is provided
    if info["device"] in [None, "", "generic"] and build_dir:
        odir = info.get("out_dir") or "out"
        p_base = os.path.join(build_dir, odir, "target", "product")
        if os.path.isdir(p_base):
            try:
                cands = [
                    d for d in os.listdir(p_base)
                    if os.path.isdir(os.path.join(p_base, d))
                    and d not in ["generic", "obj", "common", "install", "symbols", "fake_packages", "temp", "tmp"]
                    and not d.startswith(".")
                ]
                if len(cands) == 1:
                    info["device"] = cands[0]
                elif len(cands) > 1:
                    cands.sort(key=lambda d: os.path.getmtime(os.path.join(p_base, d)), reverse=True)
                    info["device"] = cands[0]
            except Exception:
                pass

    # If ROM not yet determined, check source tree vendor or directory
    if not info["rom"] and build_dir:
        info["rom"] = detect_rom_from_tree(build_dir)

    # Final fallback
    if not info["rom"]:
        if info["product"].startswith("lineage_"):
            info["rom"] = "LineageOS"
        else:
            info["rom"] = "AOSP"

    return info
