"""ROM Scanner for Detecting Compiled and Downloaded ROMs on the Server.

Searches build outputs in out/target/product/<device> and ROM packages in
user directories, collecting file lists ready for GoFile upload.
"""

import os
import glob
import time
import re
import zipfile
import logging
from typing import List, Dict, Any, Optional, Tuple

from .artifacts import discover_artifacts, format_file_size
from .rom_detector import detect_rom_name_from_string, parse_soong_log

logger = logging.getLogger("aosp_tracker.rom_scanner")

_SCAN_CACHE: List[Dict[str, Any]] = []
_LAST_SCAN_TIME: float = 0.0
_SCAN_TTL: float = 20.0  # Cache for 20 seconds


def detect_kernel_metadata_from_tree(kernel_dir: str, out_dir: Optional[str] = None, username: str = "unknown") -> Tuple[str, str, str]:
    """Robustly extract (kernel_title, device, localversion) for a kernel build tree."""
    config_candidates = [
        os.path.join(out_dir, ".config") if out_dir else None,
        os.path.join(kernel_dir, ".config"),
    ]
    config_content = ""
    for cfg in config_candidates:
        if cfg and os.path.isfile(cfg):
            try:
                with open(cfg, "r", errors="ignore") as f:
                    config_content = f.read()
                break
            except Exception:
                pass

    localversion = ""
    title = None
    device = None
    soc = None

    if config_content:
        m_lv = re.search(r'^CONFIG_LOCALVERSION="([^"]+)"', config_content, re.MULTILINE)
        if m_lv:
            localversion = m_lv.group(1).lstrip("-").strip()
            tokens = [t for t in re.split(r'[-_.]', localversion) if t]
            for t in tokens:
                t_low = t.lower()
                if "kernel" in t_low or t_low in ["meow", "zangetsu", "sultan", "kirisakura", "quicksilver", "elementalx", "arter97"]:
                    title = t
                    break
            if not title:
                filtered = [t for t in tokens if not re.match(r'^(android\d*|gki|arm64|perf|dirty|user|release|v\d+.*)$', t, re.I)]
                if filtered:
                    title = filtered[0]

        mach_matches = re.findall(r'^CONFIG_MACH_(?:XIAOMI_)?([A-Z0-9_]+)=y', config_content, re.MULTILINE)
        for m in mach_matches:
            m_low = m.lower()
            if m_low in ["xiaomi", "qcom", "samsung", "oneplus", "google", "oppo", "vivo", "realme"]:
                continue
            if re.match(r"^sm[0-9]+", m_low) or re.match(r"^[0-9]+", m_low):
                soc = m_low
            else:
                device = m_low

        if not soc:
            soc_matches = re.findall(r'^CONFIG_ARCH_(SM[0-9]+|QTI|LAHAINA|TARO|YUPIC)=y', config_content, re.MULTILINE)
            if soc_matches:
                soc = soc_matches[0].lower()

    # Check local AnyKernel3 inside kernel_dir or out_dir ONLY
    ak3_candidates = [
        os.path.join(kernel_dir, "AnyKernel3", "anykernel.sh"),
        os.path.join(kernel_dir, "anykernel.sh"),
        os.path.join(out_dir, "anykernel.sh") if out_dir else None,
    ]
    for ak in ak3_candidates:
        if ak and os.path.isfile(ak):
            try:
                with open(ak, "r", errors="ignore") as f:
                    ak_txt = f.read()
                m_dev = re.search(r'device\.name1=([a-zA-Z0-9_-]+)', ak_txt)
                if m_dev:
                    device = m_dev.group(1)
                m_str = re.search(r'kernel\.string=([^\n;]+)', ak_txt)
                if m_str and not title:
                    title = m_str.group(1).split(" by ")[0].strip()
                break
            except Exception:
                pass

    # Check git config & HEAD for clues
    git_text = ""
    git_config = os.path.join(kernel_dir, ".git", "config")
    git_head = os.path.join(kernel_dir, ".git", "HEAD")
    if os.path.isfile(git_config):
        try:
            with open(git_config, "r", errors="ignore") as f:
                git_text += f.read() + "\n"
        except Exception:
            pass
    if os.path.isfile(git_head):
        try:
            with open(git_head, "r", errors="ignore") as f:
                git_text += f.read() + "\n"
        except Exception:
            pass

    combined_clues = (os.path.basename(os.path.abspath(kernel_dir)) + " " + git_text).lower()

    KNOWN_DEVICES = [
        ("oneplus_13r", "OnePlus 13R"),
        ("oneplus-13r", "OnePlus 13R"),
        ("13r", "OnePlus 13R"),
        ("lisa", "lisa"),
        ("peridot", "peridot"),
        ("porsche", "porsche"),
        ("dodge", "dodge"),
        ("munch", "munch"),
        ("alioth", "alioth"),
        ("marble", "marble"),
        ("apollo", "apollo"),
        ("lemonadep", "lemonadep"),
    ]
    if not device or device == "generic":
        for cand_key, cand_name in KNOWN_DEVICES:
            if cand_key in combined_clues:
                device = cand_name
                break

    KNOWN_SOCS = ["sm8650", "sm8550", "sm8450", "sm8350", "sm7325", "sm6375", "sm8250", "lahaina", "taro"]
    if not soc:
        for s in KNOWN_SOCS:
            if s in combined_clues:
                soc = s.upper()
                break

    if not title:
        base = os.path.basename(os.path.abspath(kernel_dir)).lower()
        if "zangetsu" in base:
            title = "Zangetsu"
        elif "meow" in base:
            title = "MeowKernel"
        elif "lineage" in base:
            title = "Lineage Kernel"
        elif "oneplus" in base or "13r" in base:
            title = "OnePlus 13R Kernel" if username != "meowing" else "MeowKernel"
        elif "kernel" in base:
            title = os.path.basename(os.path.abspath(kernel_dir)).replace("_", "-").capitalize()
        else:
            title = "Custom Kernel"

    kernel_title = title or ("MeowKernel" if username == "meowing" else "Kernel")

    if device and soc and soc.lower() not in device.lower():
        device_display = f"{device} ({soc.upper()})"
    elif device:
        device_display = device
    elif soc:
        device_display = f"SoC: {soc.upper()}"
    else:
        device_display = "arm64 (GKI)"

    return kernel_title, device_display, localversion


def extract_device_from_filename(filename: str, fallback_device: str = "unknown") -> str:
    """Extract target device codename from an AOSP ROM or Kernel filename."""
    if not filename:
        return fallback_device
    fname_no_ext = re.sub(r"\.(?:zip|img|tar|gz|json|sha256sum)$", "", filename, flags=re.IGNORECASE)
    parts = re.split(r"[-_]", fname_no_ext)

    blacklist = {
        "axion", "mist", "voltage", "voltageos", "crdroid", "crdroidandroid", "lineage", "lineageos",
        "rising", "risingos", "derpfest", "infinity", "uwu", "lunaris", "pixel", "evolution",
        "matrixx", "superior", "bliss", "miku", "mikuui", "alpha", "elixir", "spark", "flavor",
        "cherish", "stag", "ancient", "corvus", "octavi", "paranoid", "aospa", "evox", "arrow",
        "havoc", "aosp", "unofficial", "official", "community", "gms", "gapps", "vanilla", "ota",
        "fastboot", "signed", "release", "test", "flamegapps", "core", "micro", "arm64", "gki",
        "generic", "user", "userdebug", "eng", "weekly", "nightly", "stable", "beta", "noesis"
    }

    for p in reversed(parts):
        p_clean = p.strip()
        p_low = p_clean.lower()
        if not p_clean or p_clean.isdigit():
            continue
        if re.match(r"^v?[0-9]+(?:\.[0-9]+)*$", p_clean, re.IGNORECASE):
            continue
        if len(p_clean) == 8 and p_clean.isdigit():
            continue
        if p_low in blacklist:
            continue
        return p_clean

    return fallback_device


def get_path_owner(path: str) -> str:
    """Resolve username for a given filesystem path."""
    try:
        import pwd
        return pwd.getpwuid(os.stat(path).st_uid).pw_name
    except Exception:
        parts = path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "home":
            return parts[1]
        return "root"


def get_all_scan_bases() -> List[str]:
    """Return all valid root directories to scan for ROM builds, packages, and kernels.
    
    Includes standard paths (/android, /home, /root, /mnt/data, /mnt, /data, /build, /work)
    as well as dynamically detected mounted partitions with substantial storage.
    """
    bases = [
        "/android",
        "/home",
        "/root",
        "/mnt/data",
        "/mnt",
        "/data",
        "/build",
        "/builds",
        "/work",
        "/workspace",
        "/srv",
    ]
    try:
        import psutil
        for p in psutil.disk_partitions(all=False):
            mp = p.mountpoint
            if mp == "/" or mp.startswith(("/snap", "/boot", "/run", "/var", "/sys", "/proc", "/dev", "/etc", "/usr", "/tmp")):
                continue
            if p.fstype in ["squashfs", "iso9660", "vfat", "tmpfs"]:
                continue
            if mp not in bases and os.path.isdir(mp):
                bases.append(mp)
    except Exception:
        pass

    valid = []
    for b in bases:
        b_clean = b.rstrip("/")
        if b_clean and os.path.isdir(b_clean) and b_clean not in valid:
            valid.append(b_clean)
    return valid


def scan_all_roms(force_refresh: bool = False) -> List[Dict[str, Any]]:
    """Scan the server for all available ROM builds and standalone ROM packages."""
    global _SCAN_CACHE, _LAST_SCAN_TIME
    now = time.time()
    if not force_refresh and _SCAN_CACHE and (now - _LAST_SCAN_TIME) < _SCAN_TTL:
        return _SCAN_CACHE

    results: List[Dict[str, Any]] = []
    seen_paths = set()
    bases = get_all_scan_bases()

    # 1. Scan AOSP Build Product Directories across all possible paths
    search_patterns = []
    for b in bases:
        if b != "/home":
            search_patterns.extend([
                f"{b}/out/target/product/*",
                f"{b}/out*/target/product/*",
            ])
        search_patterns.extend([
            f"{b}/*/out/target/product/*",
            f"{b}/*/out*/target/product/*",
            f"{b}/*/*/out/target/product/*",
            f"{b}/*/*/out*/target/product/*",
            f"{b}/*/*/*/out/target/product/*",
            f"{b}/*/*/*/out*/target/product/*",
            f"{b}/*/*/*/*/out/target/product/*",
            f"{b}/*/*/*/*/out*/target/product/*",
            f"{b}/*/*/*/*/*/out/target/product/*",
            f"{b}/*/*/*/*/*/out*/target/product/*",
        ])

    candidate_dirs = []
    for pat in search_patterns:
        candidate_dirs.extend(glob.glob(pat))

    FASTBOOT_IMAGES = [
        "boot.img", "vendor_boot.img", "recovery.img", "dtbo.img", "vbmeta.img",
        "init_boot.img", "vbmeta_system.img", "vbmeta_vendor.img", "vendor_kernel_boot.img"
    ]

    for pdir in candidate_dirs:
        if not os.path.isdir(pdir):
            continue
        pdir = os.path.abspath(pdir)
        if pdir in seen_paths:
            continue
        seen_paths.add(pdir)

        dir_device = os.path.basename(pdir)
        if dir_device in ["obj", "installed-files", "fake_packages", "generic", "common", "install", "symbols", "temp", "tmp"]:
            continue

        # Look for soong.log
        out_dir = os.path.abspath(os.path.join(pdir, "..", ".."))
        build_root = os.path.abspath(os.path.join(out_dir, ".."))
        soong_log = os.path.join(out_dir, "soong.log")
        if not os.path.isfile(soong_log):
            soong_log = os.path.join(build_root, "out", "soong.log")
        rom_info = parse_soong_log(soong_log, build_root) if os.path.isfile(soong_log) else {}

        artifacts = discover_artifacts(pdir, max_items=30, compute_hashes=False, pick_single_zip=False)
        if not artifacts:
            continue

        all_zips = [a for a in artifacts if a["type"] == "ZIP"]
        non_zips = [a for a in artifacts if a["type"] != "ZIP"]

        # Filter out raw intermediate ota zips (e.g. lineage_giulia-ota.zip) if branded custom ROM zips exist
        branded_zips = [
            z for z in all_zips
            if not (z["name"].endswith("-ota.zip") and (z["name"].startswith("lineage_") or z["name"].startswith("aosp_")))
        ]
        if branded_zips:
            all_zips = branded_zips

        if not all_zips:
            all_zips = [None]

        for current_zip in all_zips:
            # Determine device:
            # 1. From zip name (e.g. axion-...-giulia.zip or axion-...-giuliac.zip)
            # 2. From directory name (e.g. giulia, giuliac)
            # 3. From soong.log
            detected_dev = dir_device
            if current_zip:
                zip_dev = extract_device_from_filename(current_zip["name"], fallback_device="")
                if zip_dev and zip_dev.lower() not in ["generic", "arm64", "unknown", ""]:
                    detected_dev = zip_dev
            if detected_dev in ["unknown", "generic", ""] and rom_info.get("device"):
                detected_dev = rom_info["device"]
            if not detected_dev:
                detected_dev = "unknown"

            # Determine ROM name
            rom_name = rom_info.get("rom")
            if not rom_name and current_zip:
                rom_name = detect_rom_name_from_string(current_zip["name"])
            if not rom_name:
                rom_name = detect_rom_name_from_string(os.path.basename(build_root))
            if not rom_name:
                rom_name = detect_rom_name_from_string(os.path.basename(out_dir))
            if not rom_name:
                rom_name = detect_rom_name_from_string(detected_dev)
            if not rom_name:
                rom_name = "Custom ROM"

            files_for_upload = []
            if current_zip and os.path.isfile(current_zip["path"]):
                files_for_upload.append(current_zip["path"])

            for a in non_zips:
                fname = a["name"]
                fpath = a["path"]
                if fname.endswith(".json") or fname in FASTBOOT_IMAGES:
                    if fpath not in files_for_upload and os.path.isfile(fpath):
                        files_for_upload.append(fpath)

            if not files_for_upload:
                for a in non_zips[:5]:
                    fpath = a.get("path")
                    if fpath and os.path.isfile(fpath) and fpath not in files_for_upload:
                        files_for_upload.append(fpath)

            if not files_for_upload:
                continue

            existing_upload_files = [p for p in files_for_upload if os.path.exists(p)]
            total_bytes = sum(os.path.getsize(p) for p in existing_upload_files)
            mtime = (
                os.path.getmtime(current_zip["path"])
                if current_zip and os.path.exists(current_zip["path"])
                else max((os.path.getmtime(p) for p in existing_upload_files), default=os.path.getmtime(pdir) if os.path.exists(pdir) else time.time())
            )
            u_owner = get_path_owner(pdir)

            entry_artifacts = ([current_zip] if current_zip else []) + non_zips

            results.append({
                "id": f"rom_{len(results)}",
                "type": "build",
                "name": rom_name,
                "device": detected_dev,
                "directory": pdir,
                "username": u_owner,
                "main_zip_name": current_zip["name"] if current_zip else "Partition Images",
                "main_zip_size": current_zip["size_formatted"] if current_zip else "",
                "file_count": len(files_for_upload),
                "total_size_bytes": total_bytes,
                "total_size_formatted": format_file_size(total_bytes),
                "files_to_upload": files_for_upload,
                "artifacts_detail": entry_artifacts,
                "mtime": mtime,
            })

    # 2. Scan Standalone ROM Zip Packages across all storage locations
    standalone_patterns = []
    for b in bases:
        standalone_patterns.extend([
            f"{b}/*.zip",
            f"{b}/*/*.zip",
            f"{b}/*/*/*.zip",
            f"{b}/*/*/*/*.zip",
            f"{b}/Downloads/*.zip",
            f"{b}/Downloads/*/*.zip",
            f"{b}/*/Downloads/*.zip",
            f"{b}/*/Downloads/*/*.zip",
        ])

    standalone_files = []
    for pat in standalone_patterns:
        standalone_files.extend(glob.glob(pat))

    known_keywords = [
        "rising", "mist", "voltage", "crdroid", "derp", "infinity",
        "uwu", "luna", "pixel", "lineage", "evolution", "arrow", "havoc",
        "unofficial", "community", "gapps", "vanilla", "matrixx", "superior", "bliss",
        "miku", "axion", "alpha", "elixir", "spark", "flavor", "cherish", "stag",
        "ancient", "corvus", "octavi", "paranoid", "aospa", "evox", "aosp",
    ]

    for fpath in standalone_files:
        if not os.path.isfile(fpath):
            continue
        fname = os.path.basename(fpath)
        lower = fname.lower()

        # Skip small helper zips or non-ROM archives
        try:
            fsize = os.path.getsize(fpath)
        except Exception:
            continue

        # Real ROM zips are at least 250 MB
        if fsize < 250 * 1024 * 1024:
            continue

        if not any(k in lower for k in known_keywords):
            continue

        rom_name = detect_rom_name_from_string(fname) or "Custom ROM"

        # Extract device if present in filename
        device = extract_device_from_filename(fname, fallback_device="unknown")

        files_to_upload = [fpath]
        dir_path = os.path.dirname(fpath)

        # Look for companion .sha256sum or .json in the same folder
        base_no_ext = os.path.splitext(fpath)[0]
        for comp_ext in [".sha256sum", ".sha256", ".json", ".zip.json"]:
            comp_path = base_no_ext + comp_ext
            if os.path.isfile(comp_path):
                files_to_upload.append(comp_path)

        total_bytes = sum(os.path.getsize(p) for p in files_to_upload if os.path.exists(p))
        mtime = os.path.getmtime(fpath)
        u_owner = get_path_owner(fpath)

        results.append({
            "id": f"rom_{len(results)}",
            "type": "package",
            "name": rom_name,
            "device": device,
            "directory": dir_path,
            "username": u_owner,
            "main_zip_name": fname,
            "main_zip_size": format_file_size(fsize),
            "file_count": len(files_to_upload),
            "total_size_bytes": total_bytes,
            "total_size_formatted": format_file_size(total_bytes),
            "files_to_upload": files_to_upload,
            "artifacts_detail": [{
                "name": os.path.basename(p),
                "path": p,
                "size_formatted": format_file_size(os.path.getsize(p)),
            } for p in files_to_upload if os.path.exists(p)],
            "mtime": mtime,
        })

    # 3. Scan Kernel Releases across all storage locations
    kernel_patterns = []
    for b in bases:
        kernel_patterns.extend([
            f"{b}/KernelReleases/*.zip",
            f"{b}/AnyKernel3/*.zip",
            f"{b}/*/KernelReleases/*.zip",
            f"{b}/*/AnyKernel3/*.zip",
            f"{b}/*/*/KernelReleases/*.zip",
            f"{b}/*/*/AnyKernel3/*.zip",
        ])
    candidate_kernel_zips = []
    for pat in kernel_patterns:
        candidate_kernel_zips.extend(glob.glob(pat))

    for kz in candidate_kernel_zips:
        if not os.path.isfile(kz):
            continue
        kz = os.path.abspath(kz)
        if kz in seen_paths:
            continue
        seen_paths.add(kz)

        fname = os.path.basename(kz)
        fsize = os.path.getsize(kz)
        if fsize < 1024 * 1024:  # At least 1MB
            continue

        dir_path = os.path.dirname(kz)
        user_name = get_path_owner(kz)

        # Determine kernel name & device
        kname = "Custom Kernel"
        device = "unknown"

        # 1. Inspect anykernel.sh inside the zip archive itself
        try:
            with zipfile.ZipFile(kz, "r") as zf:
                if "anykernel.sh" in zf.namelist():
                    ak_txt = zf.read("anykernel.sh").decode("utf-8", errors="ignore")
                    m_dev = re.search(r"device\.name1=([a-zA-Z0-9_-]+)", ak_txt)
                    if m_dev:
                        device = m_dev.group(1)
                    m_str = re.search(r"kernel\.string=([^\n;]+)", ak_txt)
                    if m_str:
                        kname = m_str.group(1).split(" by ")[0].strip()
        except Exception:
            pass

        # 2. Check local anykernel.sh in the directory where the zip was packaged
        if device == "unknown":
            local_ak = os.path.join(dir_path, "anykernel.sh")
            if os.path.isfile(local_ak):
                try:
                    with open(local_ak, "r", errors="ignore") as f:
                        ak_txt = f.read()
                    m_dev = re.search(r"device\.name1=([a-zA-Z0-9_-]+)", ak_txt)
                    if m_dev:
                        device = m_dev.group(1)
                    m_str = re.search(r"kernel\.string=([^\n;]+)", ak_txt)
                    if m_str and kname == "Custom Kernel":
                        kname = m_str.group(1).split(" by ")[0].strip()
                except Exception:
                    pass

        # 3. Fallback title
        if kname == "Custom Kernel":
            if "meow" in fname.lower() or user_name == "meowing":
                kname = "MeowKernel"
            else:
                m = re.match(r"^([A-Za-z0-9_]+)[-_]", fname)
                if m:
                    kname = m.group(1).replace("_", " ")

        # 4. Check filename for device clues
        if device == "unknown":
            for cand_k, cand_name in [("13r", "OnePlus 13R"), ("oneplus", "OnePlus"), ("lisa", "lisa"), ("peridot", "peridot"), ("porsche", "porsche"), ("dodge", "dodge"), ("munch", "munch"), ("alioth", "alioth"), ("marble", "marble"), ("apollo", "apollo"), ("lemonadep", "lemonadep")]:
                if cand_k in fname.lower():
                    device = cand_name
                    break

        files_to_upload = [kz]

        # Companion boot images if present in out/arch/arm64/boot/
        boot_dirs = [
            f"/home/{user_name}/kernels/meowkernel/out/arch/arm64/boot",
            f"/home/{user_name}/kernel/out/arch/arm64/boot",
            os.path.join(dir_path, "..", "out", "arch", "arm64", "boot"),
        ]
        for bdir in boot_dirs:
            if os.path.isdir(bdir):
                for img in ["Image", "dtbo.img"]:
                    ipath = os.path.join(bdir, img)
                    if os.path.isfile(ipath) and ipath not in files_to_upload:
                        files_to_upload.append(ipath)

        total_bytes = sum(os.path.getsize(p) for p in files_to_upload if os.path.exists(p))
        mtime = os.path.getmtime(kz)

        results.append({
            "id": f"rom_{len(results)}",
            "type": "kernel",
            "name": kname,
            "device": device,
            "directory": dir_path,
            "username": user_name,
            "main_zip_name": fname,
            "main_zip_size": format_file_size(fsize),
            "file_count": len(files_to_upload),
            "total_size_bytes": total_bytes,
            "total_size_formatted": format_file_size(total_bytes),
            "files_to_upload": files_to_upload,
            "artifacts_detail": [{
                "name": os.path.basename(p),
                "path": p,
                "size_formatted": format_file_size(os.path.getsize(p)),
            } for p in files_to_upload if os.path.exists(p)],
            "mtime": mtime,
        })

    # 4. Scan Compiled Kernel Boot Images across all storage locations
    boot_image_patterns = []
    for b in bases:
        boot_image_patterns.extend([
            f"{b}/out/arch/arm64/boot/Image",
            f"{b}/*/out/arch/arm64/boot/Image",
            f"{b}/*/*/out/arch/arm64/boot/Image",
            f"{b}/*/kernels/*/out/arch/arm64/boot/Image",
            f"{b}/*/kernel/*/out/arch/arm64/boot/Image",
            f"{b}/*/kernel/out/arch/arm64/boot/Image",
        ])
    candidate_images = []
    for pat in boot_image_patterns:
        candidate_images.extend(glob.glob(pat))

    for img_path in candidate_images:
        if not os.path.isfile(img_path):
            continue
        img_path = os.path.abspath(img_path)

        # Skip if this image was already packaged inside or attached to a release zip
        if any(img_path in r.get("files_to_upload", []) for r in results):
            continue

        boot_dir = os.path.dirname(img_path)
        out_dir = os.path.dirname(os.path.dirname(os.path.dirname(boot_dir)))
        kernel_dir = os.path.dirname(out_dir)

        user_name = get_path_owner(img_path)

        # Detect metadata from kernel_dir and out_dir
        kname, kdevice, _ = detect_kernel_metadata_from_tree(kernel_dir, out_dir, user_name)

        # Collect companion files in boot_dir (Image.gz, dtbo.img, dtb, boot.img)
        files_to_upload = [img_path]
        for cname in ["Image.gz", "dtbo.img", "dtb", "boot.img"]:
            cpath = os.path.join(boot_dir, cname)
            if os.path.isfile(cpath) and cpath not in files_to_upload:
                files_to_upload.append(cpath)

        fsize = os.path.getsize(img_path)
        total_bytes = sum(os.path.getsize(p) for p in files_to_upload if os.path.exists(p))
        mtime = os.path.getmtime(img_path)

        results.append({
            "id": f"rom_{len(results)}",
            "type": "kernel",
            "name": kname,
            "device": kdevice,
            "directory": boot_dir,
            "username": user_name,
            "main_zip_name": os.path.basename(img_path),
            "main_zip_size": format_file_size(fsize),
            "file_count": len(files_to_upload),
            "total_size_bytes": total_bytes,
            "total_size_formatted": format_file_size(total_bytes),
            "files_to_upload": files_to_upload,
            "artifacts_detail": [{
                "name": os.path.basename(p),
                "path": p,
                "size_formatted": format_file_size(os.path.getsize(p)),
            } for p in files_to_upload if os.path.exists(p)],
            "mtime": mtime,
        })

    # Sort results: newest modification first
    results.sort(key=lambda r: r["mtime"], reverse=True)

    # Re-assign sequential IDs after sorting
    for idx, r in enumerate(results):
        r["id"] = f"rom_{idx}"

    _SCAN_CACHE = results
    _LAST_SCAN_TIME = now
    return results


def get_rom_by_id(rom_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve a scanned ROM by its unique identifier."""
    roms = scan_all_roms()
    for r in roms:
        if r["id"] == rom_id:
            return r
    return None
