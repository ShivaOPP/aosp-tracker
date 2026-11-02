"""Kernel Build Tracker and Session Manager for Linux / Android Kernel Builds.

Tracks make/kbuild processes across system users, reads .config and Makefile metadata,
detects target device (or kernel type/architecture), monitors object compilation progress,
calculates dynamic ETAs, and discovers/packages release artifacts for GoFile upload.
"""

import os
import re
import time
import glob
import logging
import psutil
from typing import Dict, Any, Optional, List, Tuple

from .artifacts import format_file_size, compute_sha256
from .user_detector import resolve_user_profile
from .rom_detector import get_rom_avatar
from .ogpreview import build_ogpreview_url

logger = logging.getLogger("aosp_tracker.kernel")


def read_kernel_version(kernel_dir: str) -> str:
    """Read Linux kernel version from top-level Makefile."""
    makefile = os.path.join(kernel_dir, "Makefile")
    if not os.path.isfile(makefile):
        return "Linux"
    v, p, s, n = "", "", "", ""
    try:
        with open(makefile, "r", errors="ignore") as f:
            for line in f:
                if line.startswith("VERSION ="):
                    v = line.split("=")[1].strip()
                elif line.startswith("PATCHLEVEL ="):
                    p = line.split("=")[1].strip()
                elif line.startswith("SUBLEVEL ="):
                    s = line.split("=")[1].strip()
                elif line.startswith("NAME ="):
                    n = line.split("=")[1].strip()
                if v and p and s:
                    break
        res = f"Linux {v}.{p}.{s}" if v and p and s else "Linux"
        return res
    except Exception:
        return "Linux"


class KernelBuildSession:
    def __init__(
        self,
        pid: int,
        username: str,
        kernel_dir: str,
        out_dir: str,
        start_time: float,
    ):
        self.pid = pid
        self.username = username
        self.kernel_dir = kernel_dir
        self.out_dir = out_dir
        self.start_time = start_time

        # User profile
        self.user_profile = resolve_user_profile(username)
        self.chat_id: Optional[str] = self.user_profile.get("chat_id")

        # Telegram message tracking
        self.message_id: Optional[int] = None
        self.last_edit_time: float = 0.0
        self.last_pct: float = -1.0
        self.action_history: List[Tuple[float, int]] = []

        # Kernel and Device Metadata
        self.kernel_version = "Linux"
        self.kernel_title = "Kernel"
        self.localversion = ""
        self.device = "generic"
        self.device_display = ""
        self.compiler_str = "Clang / LLVM"
        self.avatar_url = self.user_profile.get("avatar") or get_rom_avatar("AOSP")

        # Object tracking baseline
        self.total_objects = 3600
        self.last_compiled_count = 0

        self.refresh_metadata()

    def refresh_metadata(self):
        """Extract device name, kernel title, and localversion from out/.config, AnyKernel3, and Makefile."""
        self.kernel_version = read_kernel_version(self.kernel_dir)

        # 1. Parse .config
        config_candidates = [
            os.path.join(self.out_dir, ".config"),
            os.path.join(self.kernel_dir, ".config"),
        ]
        config_content = ""
        for cfg in config_candidates:
            if os.path.isfile(cfg):
                try:
                    with open(cfg, "r", errors="ignore") as f:
                        config_content = f.read()
                    break
                except Exception:
                    pass

        detected_device = None
        detected_soc = None
        detected_title = None

        if config_content:
            # CONFIG_LOCALVERSION
            m_lv = re.search(r'^CONFIG_LOCALVERSION="([^"]+)"', config_content, re.MULTILINE)
            if m_lv:
                self.localversion = m_lv.group(1).lstrip("-").strip()
                tokens = [t for t in re.split(r'[-_.]', self.localversion) if t]
                for t in tokens:
                    t_low = t.lower()
                    if "kernel" in t_low or t_low in ["meow", "zangetsu", "sultan", "kirisakura", "quicksilver", "elementalx", "arter97"]:
                        detected_title = t
                        break
                if not detected_title:
                    filtered = [t for t in tokens if not re.match(r'^(android\d*|gki|arm64|perf|dirty|user|release|v\d+.*)$', t, re.I)]
                    if filtered:
                        detected_title = filtered[0]

            # Device detection via CONFIG_MACH_
            # Distinguish device codenames (e.g. LISA) from SoC numbers (e.g. SM7325, SM8350)
            mach_matches = re.findall(r'^CONFIG_MACH_(?:XIAOMI_)?([A-Z0-9_]+)=y', config_content, re.MULTILINE)
            for m in mach_matches:
                m_low = m.lower()
                if m_low in ["xiaomi", "qcom", "samsung", "oneplus", "google", "oppo", "vivo", "realme"]:
                    continue
                if re.match(r"^sm[0-9]+", m_low) or re.match(r"^[0-9]+", m_low):
                    detected_soc = m_low
                else:
                    detected_device = m_low

            # SoC detection
            if not detected_soc:
                soc_matches = re.findall(r'^CONFIG_ARCH_(SM[0-9]+|QTI|LAHAINA|TARO|YUPIC)=y', config_content, re.MULTILINE)
                if soc_matches:
                    detected_soc = soc_matches[0].lower()

        # 2. Check local AnyKernel3 inside kernel_dir or out_dir ONLY (never global home to prevent cross-device contamination)
        ak3_candidates = [
            os.path.join(self.kernel_dir, "AnyKernel3", "anykernel.sh"),
            os.path.join(self.kernel_dir, "anykernel.sh"),
            os.path.join(self.out_dir, "anykernel.sh"),
        ]
        for ak in ak3_candidates:
            if os.path.isfile(ak):
                try:
                    with open(ak, "r", errors="ignore") as f:
                        ak_txt = f.read()
                    m_dev = re.search(r'device\.name1=([a-zA-Z0-9_-]+)', ak_txt)
                    if m_dev:
                        detected_device = m_dev.group(1)
                    m_str = re.search(r'kernel\.string=([^\n;]+)', ak_txt)
                    if m_str and not detected_title:
                        detected_title = m_str.group(1).split(" by ")[0].strip()
                    break
                except Exception:
                    pass

        # 3. Check git config and HEAD for device and SoC clues
        git_text = ""
        git_config = os.path.join(self.kernel_dir, ".git", "config")
        git_head = os.path.join(self.kernel_dir, ".git", "HEAD")
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

        combined_clues = (os.path.basename(os.path.abspath(self.kernel_dir)) + " " + git_text).lower()

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
        if not detected_device or detected_device == "generic":
            for cand_key, cand_name in KNOWN_DEVICES:
                if cand_key in combined_clues:
                    detected_device = cand_name
                    break

        KNOWN_SOCS = ["sm8650", "sm8550", "sm8450", "sm8350", "sm7325", "sm6375", "sm8250", "lahaina", "taro"]
        if not detected_soc:
            for s in KNOWN_SOCS:
                if s in combined_clues:
                    detected_soc = s.upper()
                    break

        # 4. Fallback title from directory name or user identity
        if not detected_title:
            base = os.path.basename(os.path.abspath(self.kernel_dir)).lower()
            if "zangetsu" in base:
                detected_title = "Zangetsu"
            elif "meow" in base:
                detected_title = "MeowKernel"
            elif "lineage" in base:
                detected_title = "Lineage Kernel"
            elif "oneplus" in base or "13r" in base:
                detected_title = "OnePlus 13R Kernel" if self.username != "meowing" else "MeowKernel"
            elif "kernel" in base:
                detected_title = os.path.basename(os.path.abspath(self.kernel_dir)).replace("_", "-").capitalize()
            else:
                detected_title = "Custom Kernel"

        self.kernel_title = detected_title or ("MeowKernel" if self.username == "meowing" else "Kernel")

        # Final device resolution
        if detected_device and detected_soc and detected_soc.lower() not in detected_device.lower():
            self.device = detected_device
            self.device_display = f"{detected_device} ({detected_soc.upper()})"
        elif detected_device:
            self.device = detected_device
            self.device_display = detected_device
        elif detected_soc:
            self.device = detected_soc
            self.device_display = f"SoC: {detected_soc.upper()}"
        else:
            self.device = "arm64 (GKI)"
            self.device_display = "arm64 (GKI)"

        # Avatar
        user_av = self.user_profile.get("avatar")
        self.avatar_url = user_av or get_rom_avatar("AOSP")

        # Determine baseline object count if previous build exists
        if os.path.isdir(self.out_dir):
            try:
                existing_o = 0
                for root, _, files in os.walk(self.out_dir):
                    existing_o += sum(1 for f in files if f.endswith(".o"))
                if existing_o > 500:
                    self.total_objects = existing_o
            except Exception:
                pass

    def count_compiled_objects(self) -> int:
        """Count .o files compiled since start_time in out_dir."""
        if not os.path.isdir(self.out_dir):
            return self.last_compiled_count

        threshold = self.start_time - 10.0
        compiled = 0
        try:
            for root, dirs, files in os.walk(self.out_dir):
                for f in files:
                    if f.endswith(".o"):
                        p = os.path.join(root, f)
                        try:
                            if os.path.getmtime(p) >= threshold:
                                compiled += 1
                        except Exception:
                            pass
        except Exception:
            pass

        self.last_compiled_count = compiled
        if compiled > self.total_objects:
            self.total_objects = int(compiled * 1.05)
        return compiled

    def count_active_compiler_tasks(self) -> int:
        """Count active compiler tasks (clang, gcc, ld, ar) running under this session."""
        active = 0
        try:
            p_root = psutil.Process(self.pid)
            children = p_root.children(recursive=True)
            for c in children:
                try:
                    cname = c.name().lower()
                    if any(t in cname for t in ["clang", "gcc", "ld", "llvm", "cc1", "as"]):
                        active += 1
                except Exception:
                    pass
        except Exception:
            pass
        return max(1, active)

    def calculate_progress(self) -> Tuple[float, int, int]:
        """Compute progress percentage based on compiled objects and kernel milestones."""
        compiled = self.count_compiled_objects()
        total = max(1, self.total_objects)

        pct = (compiled / total) * 100.0

        # Milestones
        # 1. vmlinux linking: 92%
        vmlinux = os.path.join(self.out_dir, "vmlinux")
        if os.path.isfile(vmlinux) and os.path.getmtime(vmlinux) >= (self.start_time - 10):
            pct = max(pct, 92.0)

        # 2. Kernel Image produced: 96%
        image = os.path.join(self.out_dir, "arch", "arm64", "boot", "Image")
        if os.path.isfile(image) and os.path.getmtime(image) >= (self.start_time - 10):
            pct = max(pct, 96.0)

        # 3. DTBO / DTB produced: 98%
        dtbo = os.path.join(self.out_dir, "arch", "arm64", "boot", "dtbo.img")
        if os.path.isfile(dtbo) and os.path.getmtime(dtbo) >= (self.start_time - 10):
            pct = max(pct, 98.0)

        # 4. AnyKernel3 zip produced: 100%
        home = self.user_profile.get("home") or f"/home/{self.username}"
        rel_dir = os.path.join(home, "KernelReleases")
        if os.path.isdir(rel_dir):
            for z in glob.glob(os.path.join(rel_dir, "*.zip")):
                if os.path.getmtime(z) >= (self.start_time - 10):
                    pct = 100.0
                    break

        return min(100.0, pct), compiled, total

    def calculate_eta(self, compiled: int, total: int) -> Optional[float]:
        """Calculate ETA using a 60-second rolling speed window."""
        now = time.time()
        self.action_history.append((now, compiled))
        cutoff = now - 60.0
        self.action_history = [(t, a) for t, a in self.action_history if t >= cutoff]

        if len(self.action_history) < 2:
            return None

        dt = self.action_history[-1][0] - self.action_history[0][0]
        da = self.action_history[-1][1] - self.action_history[0][1]

        if dt <= 0.5 or da <= 0:
            return None

        rate = da / dt
        remaining = total - compiled
        if remaining <= 0:
            return 0.0

        return remaining / rate

    def discover_artifacts(self) -> List[Dict[str, Any]]:
        """Find the packaged kernel release .zip and compiled boot images."""
        artifacts = []
        home = self.user_profile.get("home") or f"/home/{self.username}"
        threshold = self.start_time - 30.0

        # 1. Primary AnyKernel3 Release Zip
        search_dirs = [
            os.path.join(home, "KernelReleases"),
            os.path.join(home, "AnyKernel3"),
            os.path.join(self.kernel_dir, "AnyKernel3"),
            self.out_dir,
        ]

        found_zip = None
        newest_mtime = 0.0

        for sdir in search_dirs:
            if not os.path.isdir(sdir):
                continue
            for z in glob.glob(os.path.join(sdir, "*.zip")):
                try:
                    mtime = os.path.getmtime(z)
                    if mtime >= threshold and mtime > newest_mtime:
                        newest_mtime = mtime
                        found_zip = z
                except Exception:
                    pass

        if found_zip and os.path.isfile(found_zip):
            size = os.path.getsize(found_zip)
            sha = compute_sha256(found_zip)
            artifacts.append({
                "name": os.path.basename(found_zip),
                "path": found_zip,
                "type": "ZIP (AnyKernel3)",
                "size_formatted": format_file_size(size),
                "size_bytes": size,
                "sha256": sha,
                "is_primary": True,
            })

        # 2. Boot Images from out/arch/arm64/boot/
        boot_dir = os.path.join(self.out_dir, "arch", "arm64", "boot")
        if os.path.isdir(boot_dir):
            for img_name in ["Image", "Image.gz", "dtbo.img", "dtb", "boot.img"]:
                img_path = os.path.join(boot_dir, img_name)
                if os.path.isfile(img_path):
                    try:
                        mtime = os.path.getmtime(img_path)
                        if mtime >= threshold:
                            size = os.path.getsize(img_path)
                            sha = compute_sha256(img_path)
                            itype = "IMG" if img_name.endswith(".img") else "KERNEL"
                            artifacts.append({
                                "name": img_name,
                                "path": img_path,
                                "type": itype,
                                "size_formatted": format_file_size(size),
                                "size_bytes": size,
                                "sha256": sha,
                                "is_primary": False,
                            })
                    except Exception:
                        pass

        return artifacts

    def is_successful(self) -> bool:
        """Verify whether kernel compilation completed successfully."""
        # Check Image or release zip
        image = os.path.join(self.out_dir, "arch", "arm64", "boot", "Image")
        if os.path.isfile(image) and os.path.getmtime(image) >= (self.start_time - 30):
            return True

        home = self.user_profile.get("home") or f"/home/{self.username}"
        rel_dir = os.path.join(home, "KernelReleases")
        if os.path.isdir(rel_dir):
            for z in glob.glob(os.path.join(rel_dir, "*.zip")):
                if os.path.getmtime(z) >= (self.start_time - 30):
                    return True

        return False

    def get_ogpreview_url(self) -> str:
        """Construct serverhive ogpreview URL for kernel build."""
        return build_ogpreview_url(
            rom_title=self.kernel_title,
            avatar_url=self.avatar_url,
            maintainer_name=self.user_profile.get("name", self.username),
            device=self.device_display,
            emoji=self.user_profile.get("emoji", "🐱"),
            theme=self.user_profile.get("theme", "midnightBloom"),
        )
