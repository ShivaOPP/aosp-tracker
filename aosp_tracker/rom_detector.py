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
    "aospa": "https://avatars.githubusercontent.com/u/2967670?v=4",
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
    "miku": "https://avatars.githubusercontent.com/u/104190886?v=4",
    "mikuui": "https://avatars.githubusercontent.com/u/104190886?v=4",
    "miku-ui": "https://avatars.githubusercontent.com/u/104190886?v=4",
    "axion": "https://avatars.githubusercontent.com/u/181827446?v=4",
    "axionaosp": "https://avatars.githubusercontent.com/u/181827446?v=4",
    "axion-aosp": "https://avatars.githubusercontent.com/u/181827446?v=4",
    "alpha": "https://avatars.githubusercontent.com/u/119932062?v=4",
    "alphadroid": "https://avatars.githubusercontent.com/u/119932062?v=4",
    "elixir": "https://avatars.githubusercontent.com/u/98379989?v=4",
    "projectelixir": "https://avatars.githubusercontent.com/u/98379989?v=4",
    "spark": "https://avatars.githubusercontent.com/u/103525287?v=4",
    "sparkos": "https://avatars.githubusercontent.com/u/103525287?v=4",
    "flavor": "https://avatars.githubusercontent.com/u/152936735?v=4",
    "flavoros": "https://avatars.githubusercontent.com/u/152936735?v=4",
    "cherish": "https://avatars.githubusercontent.com/u/69796245?v=4",
    "cherishos": "https://avatars.githubusercontent.com/u/69796245?v=4",
    "stag": "https://avatars.githubusercontent.com/u/47545939?v=4",
    "stagos": "https://avatars.githubusercontent.com/u/47545939?v=4",
    "ancient": "https://avatars.githubusercontent.com/u/60455827?v=4",
    "ancientos": "https://avatars.githubusercontent.com/u/60455827?v=4",
    "corvus": "https://avatars.githubusercontent.com/u/61799042?v=4",
    "corvusos": "https://avatars.githubusercontent.com/u/61799042?v=4",
    "corvusrom": "https://avatars.githubusercontent.com/u/61799042?v=4",
    "octavi": "https://avatars.githubusercontent.com/u/69906660?v=4",
    "octavios": "https://avatars.githubusercontent.com/u/69906660?v=4",
    "aosp": "https://avatars.githubusercontent.com/u/81792437?v=4",
}

DEFAULT_AVATAR = "https://github.com/aosp-mirror.png"

# Name aliases and GitHub owners used by the community ROM index. Keep aliases
# explicit so words such as "mist", "alpha", or "pixel" do not match unrelated
# project names. More specific entries are checked first.
ROM_ALIASES = {
    "RisingOS": ("risingos", "risingrevived", "risingosrevived", "rise", "rising"),
    "VoltageOS": ("voltageos",),
    "Mist-OS": ("projectmistos", "mist-os", "mistos"),
    "crDroid": ("crdroidandroid", "crdroidx", "crdroid"),
    "DerpFest": ("derpfestaosp", "derpfest"),
    "Infinity-X": ("projectinfinityx", "infinityx"),
    "UwU-AOSP": ("uwuaosp",),
    "Lunaris-AOSP": ("lunaris-aosp", "lunarisaosp"),
    "ProjectMatrixx": ("projectmatrixx", "matrixx"),
    "SuperiorOS": ("superioros", "superior"),
    "BlissROM": ("blissroms", "blissrom", "bliss"),
    "MikuUI": ("miku-ui", "mikuui"),
    "AxionAOSP": ("axionaosp",),
    "AlphaDroid": ("alphadroid-project", "alphadroid"),
    "ProjectElixir": ("projectelixir",),
    "SparkOS": ("sparkos",),
    "FlavorOS": ("flavoros",),
    "CherishOS": ("cherishos",),
    "StagOS": ("stagos",),
    "AncientOS": ("ancientos",),
    "CorvusROM": ("corvusrom", "corvus"),
    "OctaviOS": ("octavios",),
    "PixelOS": ("pixelos-aosp", "pixelos-clo", "pixelos-gm", "pixelos"),
    "EvolutionX": ("evolution-x", "evolutionx", "evox"),
    "ParanoidAndroid": ("aospa", "paranoidandroid", "paranoid"),
    "ArrowOS": ("arrowos",),
    "Havoc-OS": ("havoc-os", "havocos"),
    # `lineage_<device>` is a generic lunch product used by several ROM forks.
    # Only the explicit `lineageos` token identifies LineageOS itself.
    "LineageOS": ("lineageos",),
    "Alch3myOS": ("alch3myos",),
    "Altair ROM": ("altairrom",),
    "AresOS": ("aresos-aosp", "aresos"),
    "ASCP": ("pixelify-aosp", "ascp"),
    "AstralOS": ("astralosproject", "astralos"),
    "AviumUI": ("aviumui",),
    "AyakaUI": ("ayakaui",),
    "BaikalOS": ("baikalos",),
    "CalyxOS": ("calyxos",),
    "CharaROM": ("chararomandroid", "chararom"),
    "DescartesOS": ("descartesos",),
    "DiamaneOS": ("diamaneos",),
    "Edith-AOSP": ("edith-aosp", "edithaosp"),
    "Frosty": ("frosty-aosp", "frosty"),
    "Fundamental OS": ("fundamentalos",),
    "GrapheneOS": ("grapheneos",),
    "Good4Nothing-AOSP": ("good4nothing-aosp", "good4nothing"),
    "Halcyon": ("halcyonproject",),
    "halogenOS": ("halogenos",),
    "HaloUI": ("haloui-aosp", "haloui"),
    "HertzifyOS": ("hertzifyos",),
    "KleeUI": ("kleeui",),
    "LightningOS": ("lightningos-aosp", "lightningos"),
    "LMODroid": ("lmodroid", "libremobileos"),
    "LumineDroid": ("luminedroid",),
    "MIRACLO": ("miraclo-project", "miraclo"),
    "MosaicOS": ("mosaic-os", "mosaicos"),
    "Neoteric-OS": ("neoteric-os", "neotericos"),
    "PenguinOS": ("project-penguinos", "penguinos"),
    "PixelLines": ("pixellines",),
    "Project Shinkai": ("shinkaiproject", "shinkai"),
    "Project Yugen": ("yugenproject", "yugen"),
    "Raven AOSP": ("raven-aosp", "ravenaosp"),
    "Resurrection Remix": ("resurrectedremixos", "resurrectionremix"),
    "RuriseOS": ("ruriseos",),
    "StrixOS": ("project-strixos", "strixos"),
    "The Clover Project": ("the-clover-project", "cloverproject"),
    "The Diva Project": ("the-diva-project", "divaproject"),
    "The XPerience Project": ("thexperienceproject", "xperience"),
    "WitAqua": ("witaqua",),
    "Wtf!OS": ("wtf-os", "wtfos"),
    "YAAP": ("yaap",),
    "AICP": ("aicp",),
    "ArkUI": ("arkui-project", "arkui"),
    "DarwinAOSP": ("darwinaosp",),
    "LOS Modified": ("losmodified",),
    "NervAOSP": ("nervaosp",),
    "petalOS": ("petalftw", "petalos"),
    "Project Sakura": ("projectsakura",),
    "StatiXOS": ("statixos",),
    "xdroid-oss": ("xdroid-oss", "xdroid"),
}


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


_ROM_ALIAS_INDEX = {
    _compact(alias): name
    for name, aliases in ROM_ALIASES.items()
    for alias in aliases
}


def _catalog_match(value: str) -> Optional[str]:
    tokens = re.findall(r"[a-z0-9]+", value.lower())
    for start in range(len(tokens)):
        candidate = ""
        for end in range(start, min(len(tokens), start + 4)):
            candidate += tokens[end]
            if candidate in _ROM_ALIAS_INDEX:
                return _ROM_ALIAS_INDEX[candidate]
    return None


def get_rom_avatar(rom_name: str) -> Optional[str]:
    """Resolve a ROM avatar by exact normalized name, avoiding cross-ROM matches."""
    clean = _compact(rom_name)
    for key, url in ROM_AVATARS.items():
        if _compact(key) == clean:
            return url
    return None


def detect_rom_name_from_string(s: str) -> Optional[str]:
    """Resolve a ROM using known project names and repository owner aliases."""
    return _catalog_match(s)


def detect_rom_from_product(product: str) -> Optional[str]:
    """Detect ROMs from lunch products without mistaking generic lineage targets."""
    value = str(product or "").strip().lower()
    if re.fullmatch(r"lineage_[a-z0-9][a-z0-9_-]*", value):
        return None
    return detect_rom_name_from_string(product)


def detect_rom_from_tree(build_dir: str) -> Optional[str]:
    """Inspect high-signal, bounded metadata without walking a huge Android tree."""
    if not build_dir:
        return None
    root = os.path.abspath(build_dir)

    # Manifest remote identifies a ROM before its project checkout is complete.
    manifest_config = os.path.join(root, ".repo", "manifests.git", "config")
    try:
        with open(manifest_config, "r", errors="ignore") as f:
            candidate = detect_rom_name_from_string(f.read(64 * 1024))
        if candidate:
            return candidate
    except OSError:
        pass

    # ROM-specific vendor project names are strong signals during sync and build.
    vendor_dir = os.path.join(root, "vendor")
    try:
        vendors = sorted(os.scandir(vendor_dir), key=lambda entry: entry.name.lower())[:160]
    except OSError:
        vendors = []
    for entry in vendors:
        candidate = detect_rom_name_from_string(entry.name)
        if candidate:
            return candidate

    for relative in ("build/make/core/version_defaults.mk", "vendor/build/Android.mk", "vendor/build/core/main.mk"):
        try:
            with open(os.path.join(root, relative), "r", errors="ignore") as f:
                candidate = detect_rom_name_from_string(f.read(48 * 1024))
            if candidate:
                return candidate
        except OSError:
            continue

    # Checkout directory is a weak fallback, used only after source metadata.
    return detect_rom_name_from_string(os.path.basename(root))


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

                # Newer ROMs often expose their name only through custom
                # *_VERSION, *_BUILD, or PRODUCT variables in the Soong log.
                if re.search(r"(?:ROM|OS|AOSP|VERSION|BUILD|PRODUCT|TARGET)", line, re.IGNORECASE):
                    candidate = detect_rom_name_from_string(line)
                    # Many ROM forks retain Lineage product identifiers, so
                    # only use LineageOS as the final fallback below.
                    if candidate and candidate != "LineageOS":
                        info["rom"] = candidate

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
                elif "MIKU_" in line or "MIKU_VERSION" in line or "MIKU_BUILD" in line:
                    info["rom"] = "MikuUI"
                elif "AXION_" in line or "AXION_VERSION" in line or "AXION_BUILD" in line:
                    info["rom"] = "AxionAOSP"
                elif "ALPHA_" in line or "ALPHADROID_" in line:
                    info["rom"] = "AlphaDroid"
                elif "ELIXIR_" in line or "ELIXIR_VERSION" in line:
                    info["rom"] = "ProjectElixir"
                elif "SPARK_" in line or "SPARK_VERSION" in line:
                    info["rom"] = "SparkOS"
                elif "FLAVOR_" in line or "FLAVOR_VERSION" in line:
                    info["rom"] = "FlavorOS"
                elif "CHERISH_" in line or "CHERISH_VERSION" in line:
                    info["rom"] = "CherishOS"
                elif "STAG_" in line or "STAG_VERSION" in line:
                    info["rom"] = "StagOS"
                elif "ANCIENT_" in line or "ANCIENT_VERSION" in line:
                    info["rom"] = "AncientOS"
                elif "CORVUS_" in line or "CORVUS_VERSION" in line:
                    info["rom"] = "CorvusROM"
                elif "OCTAVI_" in line or "OCTAVI_VERSION" in line:
                    info["rom"] = "OctaviOS"
                elif "PARANOID_" in line or "AOSPA_" in line:
                    info["rom"] = "ParanoidAndroid"
                elif "ARROW_" in line or "ARROW_VERSION" in line:
                    info["rom"] = "ArrowOS"
                elif "HAVOC_" in line or "HAVOC_VERSION" in line:
                    info["rom"] = "Havoc-OS"
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
