"""Source Sync Tracker for AOSP repo sync operations.

Detects running repo sync processes, reads manifest configuration,
tracks project fetch progress, and formats notifications.
"""

import os
import time
import re
import xml.etree.ElementTree as ET
from typing import Optional, Dict, Any, List, Set
from .rom_detector import detect_rom_name_from_string, get_rom_avatar


class RepoSyncSession:
    def __init__(
        self,
        pid: int,
        username: str,
        repo_dir: str,
        start_time: float,
    ):
        self.pid = pid
        self.username = username
        self.repo_dir = repo_dir
        self.source_root = os.path.dirname(repo_dir) if repo_dir.endswith("/.repo") else repo_dir
        self.start_time = start_time
        self.message_id: Optional[int] = None
        self.chat_id: Optional[str] = None
        self.total_projects = 0
        self.last_synced_count = 0
        self.last_update_time = 0.0
        self.project_paths: List[str] = []

        # Manifest metadata
        self.remote_url = "AOSP"
        self.branch = "default"
        self.rom_title = "AOSP"
        self.device = self._detect_device()
        self.avatar_url = get_rom_avatar("AOSP")

        self._load_manifest_metadata()
        self._load_project_paths()

    def _detect_device(self) -> Optional[str]:
        """Detect target device from out/target/product or device/*/* trees."""
        # Check out/target/product/<device>
        prod_dir = os.path.join(self.source_root, "out", "target", "product")
        if os.path.isdir(prod_dir):
            try:
                for item in os.listdir(prod_dir):
                    if item not in ["generic", "generic_arm64", "generic_x86"]:
                        if os.path.isdir(os.path.join(prod_dir, item)):
                            return item
            except Exception:
                pass

        # Check device/*/*
        dev_root = os.path.join(self.source_root, "device")
        if os.path.isdir(dev_root):
            try:
                for oem in os.listdir(dev_root):
                    oem_path = os.path.join(dev_root, oem)
                    if os.path.isdir(oem_path) and oem not in ["generic", "google", "common", "sample"]:
                        for dev in os.listdir(oem_path):
                            dev_path = os.path.join(oem_path, dev)
                            if os.path.isdir(dev_path) and not dev.endswith("-common") and not dev.startswith("sepolicy"):
                                return dev
            except Exception:
                pass
        return None

    def _load_manifest_metadata(self):
        """Parse .repo/manifests.git/config."""
        config_path = os.path.join(self.repo_dir, "manifests.git", "config")
        if os.path.isfile(config_path):
            try:
                with open(config_path, "r", errors="ignore") as f:
                    content = f.read()

                # Remote URL
                m_url = re.search(r'url\s*=\s*(https?://[^\s]+|git@[^\s]+)', content)
                if m_url:
                    self.remote_url = m_url.group(1)
                    rom = detect_rom_name_from_string(self.remote_url)
                    if rom:
                        self.rom_title = rom

                # Branch
                m_branch = re.search(r'merge\s*=\s*refs/heads/([^\s]+)', content)
                if m_branch:
                    self.branch = m_branch.group(1)

            except Exception:
                pass

        # If ROM not yet determined, check source root directory name
        if self.rom_title == "AOSP":
            root_name = os.path.basename(os.path.abspath(self.source_root)).lower()
            if "rise" in root_name or "rising" in root_name:
                self.rom_title = "RisingOS"
            elif "voltage" in root_name:
                self.rom_title = "VoltageOS"
            elif "mist" in root_name:
                self.rom_title = "Mist-OS"
            elif "uwu" in root_name:
                self.rom_title = "UwU-AOSP"
            elif "luna" in root_name:
                self.rom_title = "Lunaris-AOSP"

        self.avatar_url = get_rom_avatar(self.rom_title)

    def _load_project_paths(self):
        """Discover all project paths from project.list, manifest.xml, or .repo/projects."""
        # Method 1: Check project.list if checkout already occurred
        project_list = os.path.join(self.repo_dir, "project.list")
        if os.path.isfile(project_list):
            try:
                with open(project_list, "r", errors="ignore") as f:
                    lines = [line.strip() for line in f if line.strip()]
                    if lines:
                        self.project_paths = lines
                        self.total_projects = len(lines)
                        return
            except Exception:
                pass

        # Method 2: Recursively parse manifest XMLs (default.xml, snippets, local_manifests)
        manifest_xml = os.path.join(self.repo_dir, "manifest.xml")
        manifests_dir = os.path.join(self.repo_dir, "manifests")
        local_manifests_dir = os.path.join(self.repo_dir, "local_manifests")

        if os.path.isfile(manifest_xml):
            projects: List[str] = []
            removes: Set[str] = set()
            parsed_files: Set[str] = set()

            def parse_xml_file(fpath: str):
                real_f = os.path.realpath(fpath)
                if real_f in parsed_files or not os.path.isfile(real_f):
                    return
                parsed_files.add(real_f)
                try:
                    tree = ET.parse(real_f)
                    root = tree.getroot()
                    for elem in root:
                        if elem.tag == "project":
                            path = elem.get("path") or elem.get("name")
                            if path:
                                projects.append(path)
                        elif elem.tag == "remove-project":
                            name = elem.get("name")
                            if name:
                                removes.add(name)
                        elif elem.tag == "include":
                            inc = elem.get("name")
                            if inc:
                                parse_xml_file(os.path.join(manifests_dir, inc))
                except Exception:
                    pass

            parse_xml_file(manifest_xml)

            # Also parse any XMLs in local_manifests/
            if os.path.isdir(local_manifests_dir):
                try:
                    for fname in sorted(os.listdir(local_manifests_dir)):
                        if fname.endswith(".xml"):
                            parse_xml_file(os.path.join(local_manifests_dir, fname))
                except Exception:
                    pass

            final_list = [p for p in projects if p not in removes]
            if final_list:
                self.project_paths = final_list
                self.total_projects = len(final_list)
                return

        # Method 3: Fallback scan of .repo/projects
        projects_dir = os.path.join(self.repo_dir, "projects")
        if os.path.isdir(projects_dir):
            discovered = []
            try:
                for root, dirs, files in os.walk(projects_dir):
                    if "FETCH_HEAD" in files or "config" in files:
                        rel = os.path.relpath(root, projects_dir)
                        if rel.endswith(".git"):
                            rel = rel[:-4]
                        discovered.append(rel)
            except Exception:
                pass
            if discovered:
                self.project_paths = discovered
                self.total_projects = len(discovered)
                return

        if self.total_projects <= 0:
            self.total_projects = 1000

    def count_synced_projects(self) -> int:
        """Count projects with FETCH_HEAD updated since sync started."""
        projects_dir = os.path.join(self.repo_dir, "projects")
        if not os.path.isdir(projects_dir):
            return self.last_synced_count

        if not self.project_paths or self.total_projects <= 0:
            self._load_project_paths()

        # If project_paths still empty, count FETCH_HEAD files directly
        if not self.project_paths:
            count = 0
            try:
                threshold = self.start_time - 60.0
                for root, dirs, files in os.walk(projects_dir):
                    if "FETCH_HEAD" in files:
                        fh = os.path.join(root, "FETCH_HEAD")
                        try:
                            if os.path.getmtime(fh) >= threshold:
                                count += 1
                        except Exception:
                            pass
            except Exception:
                pass
            self.last_synced_count = count
            self.total_projects = max(self.total_projects, count)
            return count

        threshold = self.start_time - 60.0
        synced = 0

        for p in self.project_paths:
            fh1 = os.path.join(projects_dir, f"{p}.git", "FETCH_HEAD")
            fh2 = os.path.join(projects_dir, p, "FETCH_HEAD")
            try:
                m1 = os.path.getmtime(fh1) if os.path.isfile(fh1) else 0
                m2 = os.path.getmtime(fh2) if os.path.isfile(fh2) else 0
                if max(m1, m2) >= threshold:
                    synced += 1
            except Exception:
                pass

        self.last_synced_count = synced
        if synced > self.total_projects:
            self.total_projects = synced
        return self.last_synced_count

    def calculate_eta(self, elapsed: float, synced: int) -> Optional[float]:
        """Estimate remaining time for sync."""
        if synced <= 0 or elapsed < 5.0 or synced >= self.total_projects:
            return None
        rate = synced / elapsed
        if rate <= 0:
            return None
        remaining = self.total_projects - synced
        return remaining / rate
