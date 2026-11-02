"""Artifact Discovery and Hashing for AOSP builds.

Discovers generated build outputs (ROM zip, partition images, JSON metadata),
formats file sizes, and computes SHA256 checksums.
"""

import os
import hashlib
from typing import List, Dict, Any, Optional


def format_file_size(size_bytes: int) -> str:
    """Format file size in B, KB, MB, GB matching AOSP release conventions."""
    if size_bytes >= 1024 ** 3:
        return f"{size_bytes / (1024 ** 3):.2f} GB"
    if size_bytes >= 1024 ** 2:
        return f"{size_bytes / (1024 ** 2):.2f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.2f} KB"
    return f"{size_bytes} B"


def compute_sha256(file_path: str) -> str:
    """Compute or read precomputed SHA256 checksum."""
    # Check if AOSP already generated a .sha256sum file alongside
    for ext in [".sha256sum", ".sha256"]:
        sum_path = file_path + ext
        if os.path.isfile(sum_path):
            try:
                with open(sum_path, "r") as f:
                    content = f.read().strip()
                    parts = content.split()
                    if parts and len(parts[0]) == 64:
                        return parts[0]
            except Exception:
                pass

    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(4 * 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def detect_file_type(filename: str) -> str:
    """Determine file type from extension."""
    lower = filename.lower()
    if lower.endswith(".zip"):
        return "ZIP"
    if lower.endswith(".img"):
        return "IMG"
    if lower.endswith(".json"):
        return "JSON"
    if lower.endswith(".tar"):
        return "TAR"
    if lower.endswith(".gz"):
        return "GZ"
    return "BIN"


# Standard images of interest for Android flashing (in order of display)
PRIORITY_IMAGES = [
    "vendor_boot.img",
    "vbmeta.img",
    "dtbo.img",
    "system.img",
    "boot.img",
    "super_empty.img",
    "recovery.img",
    "init_boot.img",
    "vendor.img",
]


def discover_artifacts(
    target_product_dir: str,
    min_mtime: Optional[float] = None,
    max_items: int = 12,
    compute_hashes: bool = True,
) -> List[Dict[str, Any]]:
    """Scan product directory for build artifacts.
    
    Filters for ROM ZIPs, partition IMG files, and OTA JSON files.
    """
    if not os.path.isdir(target_product_dir):
        return []

    zips = []
    images = []
    jsons = []

    try:
        entries = os.listdir(target_product_dir)
    except Exception:
        return []

    for name in entries:
        full_path = os.path.join(target_product_dir, name)
        if not os.path.isfile(full_path):
            continue

        try:
            st = os.stat(full_path)
        except Exception:
            continue

        # If min_mtime specified, check if modified during/after build start
        # (with 15-second grace buffer)
        if min_mtime is not None and st.st_mtime < (min_mtime - 15):
            continue

        lower = name.lower()

        # ROM Zip
        if lower.endswith(".zip"):
            if any(skip in lower for skip in ["otatools", "target_files", "symbols", "-obj"]):
                continue
            zips.append((name, full_path, st.st_size, st.st_mtime))

        # Flashing partition images
        elif lower.endswith(".img"):
            if name in PRIORITY_IMAGES:
                images.append((name, full_path, st.st_size, st.st_mtime))

        # OTA JSON
        elif lower.endswith(".json"):
            if "installed-files" not in lower and "module-info" not in lower:
                jsons.append((name, full_path, st.st_size, st.st_mtime))

    # Prioritize ROM zip: custom ROM branded, non-ota first
    def zip_score(item):
        n = item[0].lower()
        score = 0
        if not n.endswith("-ota.zip"):
            score += 20
        if not n.startswith("lineage_") and not n.startswith("aosp_"):
            score += 30
        for brand in ["rising", "voltage", "mist", "crdroid", "derp", "infinity", "uwu", "lunaris", "pixel", "evolution"]:
            if brand in n:
                score += 50
        return (score, item[3])  # score then mtime

    if zips:
        zips.sort(key=zip_score, reverse=True)
        zips = zips[:1]

    # Sort images: priority ones first in exact order
    def img_priority(item):
        name = item[0]
        if name in PRIORITY_IMAGES:
            return PRIORITY_IMAGES.index(name)
        return 999

    images.sort(key=img_priority)

    # Sort JSONs: newest first
    jsons.sort(key=lambda x: x[3], reverse=True)

    # Combine: ROM zip first, then priority partition images, then OTA JSON
    selected = zips + images + jsons
    if max_items:
        selected = selected[:max_items]

    results = []
    for item in selected:
        name, full_path, size, mtime = item
        try:
            sha = compute_sha256(full_path) if compute_hashes else ""
            results.append({
                "name": name,
                "path": full_path,
                "type": detect_file_type(name),
                "size_bytes": size,
                "size_formatted": format_file_size(size),
                "sha256": sha,
            })
        except Exception:
            continue

    return results
