"""
APK Update Pipeline — Orchestrator
"""
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
import urllib.parse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from logger import get_logger
from state_manager import read_state, write_state
from drive_client import DriveClient, DriveError
from apk_extractor import extract_apk_info, APKInfo
from apk_renamer import (
    load_rename_map,
    save_rename_map,
    auto_map_folder_name,
    get_display_name,
    finalize_filenames,
    _strip_version_suffix,
)
from release_manager import ReleaseManager, GitHubError
from notifier import Notifier
from changelog_fetcher import fetch_changelog

log = get_logger("main")

GDRIVE_API_KEY = os.environ["GDRIVE_API_KEY"]
GH_TOKEN       = os.environ["GH_TOKEN"]
GH_REPO        = os.environ["GH_REPO"]
ROOT_FOLDER_ID = os.environ.get("GDRIVE_ROOT_FOLDER_ID", "1BfeK39boriHy-9q76eXLLqbCwfV17-Gv")
DEBUG_MODE     = os.environ.get("DEBUG_MODE", "false").lower() == "true"

MANUAL_VERSIONS_FILE = Path("MANUAL_VERSIONS.md")
DESCRIPTIONS_FILE    = Path("config/descriptions.json")


def load_json(path: Path, default):
    if path.exists():
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return default
        return json.loads(content)
    return default





def load_manual_overrides() -> dict[str, dict]:
    overrides: dict[str, dict] = {}
    if not MANUAL_VERSIONS_FILE.exists():
        return overrides
    for line in MANUAL_VERSIONS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|") or "|---|" in line:
            continue
        parts = [p.strip() for p in line.strip("|").split("|")]
        if len(parts) < 4:
            continue
        filename, vn, vc = parts[0], parts[1], parts[2]
        arch = parts[3] if len(parts) > 3 else "java"
        if "FILL_ME" in (vn, vc) or not vn or not vc:
            continue
        if "Filename" in filename:
            continue
        overrides[filename] = {"version_name": vn, "version_code": vc, "arch": arch}
    log.info(f"Loaded {len(overrides)} manual override(s)")
    return overrides


def append_pending_overrides(entries: list[dict]) -> None:
    if not entries:
        return
    if not MANUAL_VERSIONS_FILE.exists():
        MANUAL_VERSIONS_FILE.write_text(
            "# Manual Version Overrides\n\n"
            "> Fill in `FILL_ME` cells, then set `STATE.md` to Resumed.\n\n"
            "## Pending\n\n"
            "| Filename | versionName | versionCode | arch | App |\n"
            "|---|---|---|---|---|\n",
            encoding="utf-8",
        )
    content  = MANUAL_VERSIONS_FILE.read_text(encoding="utf-8")
    new_rows = [
        f"| {e['filename']} | FILL_ME | FILL_ME | {e.get('arch', 'java')} | {e['app_name']} |"
        for e in entries
    ]
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if "## Pending" in line:
            for j in range(i, min(i + 6, len(lines))):
                if "|---|" in lines[j]:
                    lines = lines[: j + 1] + new_rows + lines[j + 1:]
                    MANUAL_VERSIONS_FILE.write_text("\n".join(lines), encoding="utf-8")
                    return
    MANUAL_VERSIONS_FILE.write_text(content + "\n" + "\n".join(new_rows) + "\n", encoding="utf-8")


def _get_obtainium_url(app_name: str, display_name: str) -> str:
    """
    Return the Obtainium deep link for a known app.
    URLs sourced directly from README.md — identical to what users tap.
    For unknown future apps, falls back to a generic repo link.
    """
    _URLS: dict[str, str] = {
    "MiX_Archive": "https://apps.obtainium.imranr.dev/redirect?r=obtainium://app/%7B%22id%22%3A%20%22com.mixplorer.addon.archive%22%2C%22url%22%3A%20%22https%3A%2F%2Fgithub.com%2FnOneCode4u%2Fmixplorer-google-drive%22%2C%22author%22%3A%20%22nOneCode4u%22%2C%22name%22%3A%20%22MiX%20Archive%22%2C%22additionalSettings%22%3A%20%22%7B%5C%22includePrereleases%5C%22%3Afalse%2C%5C%22fallbackToOlderReleases%5C%22%3Atrue%2C%5C%22filterReleaseTitlesByRegEx%5C%22%3A%5C%22Archive%5C%22%2C%5C%22filterReleaseNotesByRegEx%5C%22%3A%5C%22%5C%22%2C%5C%22verifyLatestTag%5C%22%3Afalse%2C%5C%22sortMethodChoice%5C%22%3A%5C%22date%5C%22%2C%5C%22useLatestAssetDateAsReleaseDate%5C%22%3Afalse%2C%5C%22releaseTitleAsVersion%5C%22%3Afalse%2C%5C%22trackOnly%5C%22%3Afalse%2C%5C%22versionExtractionRegEx%5C%22%3A%5C%22%5C%22%2C%5C%22matchGroupToUse%5C%22%3A%5C%22%5C%22%2C%5C%22versionDetection%5C%22%3Atrue%2C%5C%22releaseDateAsVersion%5C%22%3Afalse%2C%5C%22useVersionCodeAsOSVersion%5C%22%3Afalse%2C%5C%22apkFilterRegEx%5C%22%3A%5C%22%5C%22%2C%5C%22invertAPKFilter%5C%22%3Afalse%2C%5C%22autoApkFilterByArch%5C%22%3Atrue%2C%5C%22appName%5C%22%3A%5C%22MiX%20Archive%5C%22%2C%5C%22appAuthor%5C%22%3A%5C%22nOneCode4u%5C%22%2C%5C%22shizukuPretendToBeGooglePlay%5C%22%3Atrue%2C%5C%22allowInsecure%5C%22%3Atrue%2C%5C%22exemptFromBackgroundUpdates%5C%22%3Afalse%2C%5C%22skipUpdateNotifications%5C%22%3Afalse%2C%5C%22about%5C%22%3A%5C%22Archive%20add-on%20%E2%80%94%20full%20create%2Fextract%20support%20for%20ZIP%2C%20RAR%2C%20RAR5%2C%207z%2C%20TAR%2C%20GZ%2C%20BZ2%2C%20XZ%2C%20ISO%20and%20more.%20Supports%20password-protected%20and%20multi-volume%20archives.%20Requires%20MiXplorer.%5C%22%2C%5C%22refreshBeforeDownload%5C%22%3Atrue%2C%5C%22includeZips%5C%22%3Afalse%2C%5C%22zippedApkFilterRegEx%5C%22%3A%5C%22%5C%22%7D%22%2C%22overrideSource%22%3A%20%22GitHub%22%2C%22allowIdChange%22%3A%20true%7D",
    "MiX_Codecs": "https://apps.obtainium.imranr.dev/redirect?r=obtainium://app/%7B%22id%22%3A%20%22com.mixplorer.addon.codecs%22%2C%22url%22%3A%20%22https%3A%2F%2Fgithub.com%2FnOneCode4u%2Fmixplorer-google-drive%22%2C%22author%22%3A%20%22nOneCode4u%22%2C%22name%22%3A%20%22MiX%20Codecs%22%2C%22additionalSettings%22%3A%20%22%7B%5C%22includePrereleases%5C%22%3Afalse%2C%5C%22fallbackToOlderReleases%5C%22%3Atrue%2C%5C%22filterReleaseTitlesByRegEx%5C%22%3A%5C%22Codecs%5C%22%2C%5C%22filterReleaseNotesByRegEx%5C%22%3A%5C%22%5C%22%2C%5C%22verifyLatestTag%5C%22%3Afalse%2C%5C%22sortMethodChoice%5C%22%3A%5C%22date%5C%22%2C%5C%22useLatestAssetDateAsReleaseDate%5C%22%3Afalse%2C%5C%22releaseTitleAsVersion%5C%22%3Afalse%2C%5C%22trackOnly%5C%22%3Afalse%2C%5C%22versionExtractionRegEx%5C%22%3A%5C%22%5C%22%2C%5C%22matchGroupToUse%5C%22%3A%5C%22%5C%22%2C%5C%22versionDetection%5C%22%3Atrue%2C%5C%22releaseDateAsVersion%5C%22%3Afalse%2C%5C%22useVersionCodeAsOSVersion%5C%22%3Afalse%2C%5C%22apkFilterRegEx%5C%22%3A%5C%22%5C%22%2C%5C%22invertAPKFilter%5C%22%3Afalse%2C%5C%22autoApkFilterByArch%5C%22%3Atrue%2C%5C%22appName%5C%22%3A%5C%22MiX%20Codecs%5C%22%2C%5C%22appAuthor%5C%22%3A%5C%22nOneCode4u%5C%22%2C%5C%22shizukuPretendToBeGooglePlay%5C%22%3Atrue%2C%5C%22allowInsecure%5C%22%3Atrue%2C%5C%22exemptFromBackgroundUpdates%5C%22%3Afalse%2C%5C%22skipUpdateNotifications%5C%22%3Afalse%2C%5C%22about%5C%22%3A%5C%22Media%20codec%20add-on%20%E2%80%94%20extends%20MiXplorer%27s%20built-in%20player%20with%20additional%20audio%2Fvideo%20decoding%20for%20FLAC%2C%20OGG%2C%20MKV%2C%20AVI%20and%20formats%20not%20natively%20supported%20by%20Android.%20Requires%20MiXplorer.%5C%22%2C%5C%22refreshBeforeDownload%5C%22%3Atrue%2C%5C%22includeZips%5C%22%3Afalse%2C%5C%22zippedApkFilterRegEx%5C%22%3A%5C%22%5C%22%7D%22%2C%22overrideSource%22%3A%20%22GitHub%22%2C%22allowIdChange%22%3A%20true%7D",
    "MiX_Encrypt": "https://apps.obtainium.imranr.dev/redirect?r=obtainium://app/%7B%22id%22%3A%20%22com.mixplorer.addon.encrypt%22%2C%22url%22%3A%20%22https%3A%2F%2Fgithub.com%2FnOneCode4u%2Fmixplorer-google-drive%22%2C%22author%22%3A%20%22nOneCode4u%22%2C%22name%22%3A%20%22MiX%20Encrypt%22%2C%22additionalSettings%22%3A%20%22%7B%5C%22includePrereleases%5C%22%3Afalse%2C%5C%22fallbackToOlderReleases%5C%22%3Atrue%2C%5C%22filterReleaseTitlesByRegEx%5C%22%3A%5C%22Encrypt%5C%22%2C%5C%22filterReleaseNotesByRegEx%5C%22%3A%5C%22%5C%22%2C%5C%22verifyLatestTag%5C%22%3Afalse%2C%5C%22sortMethodChoice%5C%22%3A%5C%22date%5C%22%2C%5C%22useLatestAssetDateAsReleaseDate%5C%22%3Afalse%2C%5C%22releaseTitleAsVersion%5C%22%3Afalse%2C%5C%22trackOnly%5C%22%3Afalse%2C%5C%22versionExtractionRegEx%5C%22%3A%5C%22%5C%22%2C%5C%22matchGroupToUse%5C%22%3A%5C%22%5C%22%2C%5C%22versionDetection%5C%22%3Atrue%2C%5C%22releaseDateAsVersion%5C%22%3Afalse%2C%5C%22useVersionCodeAsOSVersion%5C%22%3Afalse%2C%5C%22apkFilterRegEx%5C%22%3A%5C%22%5C%22%2C%5C%22invertAPKFilter%5C%22%3Afalse%2C%5C%22autoApkFilterByArch%5C%22%3Atrue%2C%5C%22appName%5C%22%3A%5C%22MiX%20Encrypt%5C%22%2C%5C%22appAuthor%5C%22%3A%5C%22nOneCode4u%5C%22%2C%5C%22shizukuPretendToBeGooglePlay%5C%22%3Atrue%2C%5C%22allowInsecure%5C%22%3Atrue%2C%5C%22exemptFromBackgroundUpdates%5C%22%3Afalse%2C%5C%22skipUpdateNotifications%5C%22%3Afalse%2C%5C%22about%5C%22%3A%5C%22Encryption%20add-on%20%E2%80%94%20AES-based%20file%20encryption%20and%20decryption%20directly%20inside%20MiXplorer.%20Manage%20encrypted%20containers%20without%20leaving%20the%20file%20manager.%20Requires%20MiXplorer.%5C%22%2C%5C%22refreshBeforeDownload%5C%22%3Atrue%2C%5C%22includeZips%5C%22%3Afalse%2C%5C%22zippedApkFilterRegEx%5C%22%3A%5C%22%5C%22%7D%22%2C%22overrideSource%22%3A%20%22GitHub%22%2C%22allowIdChange%22%3A%20true%7D",
    "MiX_Image": "https://apps.obtainium.imranr.dev/redirect?r=obtainium://app/%7B%22id%22%3A%20%22com.mixplorer.addon.image%22%2C%22url%22%3A%20%22https%3A%2F%2Fgithub.com%2FnOneCode4u%2Fmixplorer-google-drive%22%2C%22author%22%3A%20%22nOneCode4u%22%2C%22name%22%3A%20%22MiX%20Image%22%2C%22additionalSettings%22%3A%20%22%7B%5C%22includePrereleases%5C%22%3Afalse%2C%5C%22fallbackToOlderReleases%5C%22%3Atrue%2C%5C%22filterReleaseTitlesByRegEx%5C%22%3A%5C%22Image%5C%22%2C%5C%22filterReleaseNotesByRegEx%5C%22%3A%5C%22%5C%22%2C%5C%22verifyLatestTag%5C%22%3Afalse%2C%5C%22sortMethodChoice%5C%22%3A%5C%22date%5C%22%2C%5C%22useLatestAssetDateAsReleaseDate%5C%22%3Afalse%2C%5C%22releaseTitleAsVersion%5C%22%3Afalse%2C%5C%22trackOnly%5C%22%3Afalse%2C%5C%22versionExtractionRegEx%5C%22%3A%5C%22%5C%22%2C%5C%22matchGroupToUse%5C%22%3A%5C%22%5C%22%2C%5C%22versionDetection%5C%22%3Atrue%2C%5C%22releaseDateAsVersion%5C%22%3Afalse%2C%5C%22useVersionCodeAsOSVersion%5C%22%3Afalse%2C%5C%22apkFilterRegEx%5C%22%3A%5C%22%5C%22%2C%5C%22invertAPKFilter%5C%22%3Afalse%2C%5C%22autoApkFilterByArch%5C%22%3Atrue%2C%5C%22appName%5C%22%3A%5C%22MiX%20Image%5C%22%2C%5C%22appAuthor%5C%22%3A%5C%22nOneCode4u%5C%22%2C%5C%22shizukuPretendToBeGooglePlay%5C%22%3Atrue%2C%5C%22allowInsecure%5C%22%3Atrue%2C%5C%22exemptFromBackgroundUpdates%5C%22%3Afalse%2C%5C%22skipUpdateNotifications%5C%22%3Afalse%2C%5C%22about%5C%22%3A%5C%22Image%20viewer%20add-on%20%E2%80%94%20wide%20format%20support%20including%20RAW%2C%20TIFF%2C%20and%20WEBP%2C%20with%20smooth%20zoom%2C%20pan%2C%20and%20gallery%20navigation.%20Includes%20basic%20editing%20and%20rotation.%20Requires%20MiXplorer.%5C%22%2C%5C%22refreshBeforeDownload%5C%22%3Atrue%2C%5C%22includeZips%5C%22%3Afalse%2C%5C%22zippedApkFilterRegEx%5C%22%3A%5C%22%5C%22%7D%22%2C%22overrideSource%22%3A%20%22GitHub%22%2C%22allowIdChange%22%3A%20true%7D",
    "MiX_PDF": "https://apps.obtainium.imranr.dev/redirect?r=obtainium://app/%7B%22id%22%3A%20%22com.mixplorer.addon.pdf%22%2C%22url%22%3A%20%22https%3A%2F%2Fgithub.com%2FnOneCode4u%2Fmixplorer-google-drive%22%2C%22author%22%3A%20%22nOneCode4u%22%2C%22name%22%3A%20%22MiX%20PDF%22%2C%22additionalSettings%22%3A%20%22%7B%5C%22includePrereleases%5C%22%3Afalse%2C%5C%22fallbackToOlderReleases%5C%22%3Atrue%2C%5C%22filterReleaseTitlesByRegEx%5C%22%3A%5C%22PDF%5C%22%2C%5C%22filterReleaseNotesByRegEx%5C%22%3A%5C%22%5C%22%2C%5C%22verifyLatestTag%5C%22%3Afalse%2C%5C%22sortMethodChoice%5C%22%3A%5C%22date%5C%22%2C%5C%22useLatestAssetDateAsReleaseDate%5C%22%3Afalse%2C%5C%22releaseTitleAsVersion%5C%22%3Afalse%2C%5C%22trackOnly%5C%22%3Afalse%2C%5C%22versionExtractionRegEx%5C%22%3A%5C%22%5C%22%2C%5C%22matchGroupToUse%5C%22%3A%5C%22%5C%22%2C%5C%22versionDetection%5C%22%3Atrue%2C%5C%22releaseDateAsVersion%5C%22%3Afalse%2C%5C%22useVersionCodeAsOSVersion%5C%22%3Afalse%2C%5C%22apkFilterRegEx%5C%22%3A%5C%22%5C%22%2C%5C%22invertAPKFilter%5C%22%3Afalse%2C%5C%22autoApkFilterByArch%5C%22%3Atrue%2C%5C%22appName%5C%22%3A%5C%22MiX%20PDF%5C%22%2C%5C%22appAuthor%5C%22%3A%5C%22nOneCode4u%5C%22%2C%5C%22shizukuPretendToBeGooglePlay%5C%22%3Atrue%2C%5C%22allowInsecure%5C%22%3Atrue%2C%5C%22exemptFromBackgroundUpdates%5C%22%3Afalse%2C%5C%22skipUpdateNotifications%5C%22%3Afalse%2C%5C%22about%5C%22%3A%5C%22PDF%20viewer%20add-on%20%E2%80%94%20render%20and%20navigate%20PDF%20documents%20of%20any%20length%20directly%20inside%20MiXplorer.%20Supports%20text%20selection%2C%20zoom%2C%20and%20multi-page%20navigation.%20Requires%20MiXplorer.%5C%22%2C%5C%22refreshBeforeDownload%5C%22%3Atrue%2C%5C%22includeZips%5C%22%3Afalse%2C%5C%22zippedApkFilterRegEx%5C%22%3A%5C%22%5C%22%7D%22%2C%22overrideSource%22%3A%20%22GitHub%22%2C%22allowIdChange%22%3A%20true%7D",
    "MiX_Tagger": "https://apps.obtainium.imranr.dev/redirect?r=obtainium://app/%7B%22id%22%3A%20%22com.mixplorer.addon.tagger%22%2C%22url%22%3A%20%22https%3A%2F%2Fgithub.com%2FnOneCode4u%2Fmixplorer-google-drive%22%2C%22author%22%3A%20%22nOneCode4u%22%2C%22name%22%3A%20%22MiX%20Tagger%22%2C%22additionalSettings%22%3A%20%22%7B%5C%22includePrereleases%5C%22%3Afalse%2C%5C%22fallbackToOlderReleases%5C%22%3Atrue%2C%5C%22filterReleaseTitlesByRegEx%5C%22%3A%5C%22Tagger%5C%22%2C%5C%22filterReleaseNotesByRegEx%5C%22%3A%5C%22%5C%22%2C%5C%22verifyLatestTag%5C%22%3Afalse%2C%5C%22sortMethodChoice%5C%22%3A%5C%22date%5C%22%2C%5C%22useLatestAssetDateAsReleaseDate%5C%22%3Afalse%2C%5C%22releaseTitleAsVersion%5C%22%3Afalse%2C%5C%22trackOnly%5C%22%3Afalse%2C%5C%22versionExtractionRegEx%5C%22%3A%5C%22%5C%22%2C%5C%22matchGroupToUse%5C%22%3A%5C%22%5C%22%2C%5C%22versionDetection%5C%22%3Atrue%2C%5C%22releaseDateAsVersion%5C%22%3Afalse%2C%5C%22useVersionCodeAsOSVersion%5C%22%3Afalse%2C%5C%22apkFilterRegEx%5C%22%3A%5C%22%5C%22%2C%5C%22invertAPKFilter%5C%22%3Afalse%2C%5C%22autoApkFilterByArch%5C%22%3Atrue%2C%5C%22appName%5C%22%3A%5C%22MiX%20Tagger%5C%22%2C%5C%22appAuthor%5C%22%3A%5C%22nOneCode4u%5C%22%2C%5C%22shizukuPretendToBeGooglePlay%5C%22%3Atrue%2C%5C%22allowInsecure%5C%22%3Atrue%2C%5C%22exemptFromBackgroundUpdates%5C%22%3Afalse%2C%5C%22skipUpdateNotifications%5C%22%3Afalse%2C%5C%22about%5C%22%3A%5C%22Audio%20tag%20editor%20add-on%20%E2%80%94%20view%20and%20edit%20ID3v1%2C%20ID3v2%2C%20Vorbis%20Comment%2C%20APEv2%20and%20other%20tag%20formats%20for%20MP3%2C%20FLAC%2C%20OGG%2C%20M4A%2C%20OPUS%20and%20more.%20Supports%20batch%20editing%20and%20artwork.%20Requires%20MiXplorer.%5C%22%2C%5C%22refreshBeforeDownload%5C%22%3Atrue%2C%5C%22includeZips%5C%22%3Afalse%2C%5C%22zippedApkFilterRegEx%5C%22%3A%5C%22%5C%22%7D%22%2C%22overrideSource%22%3A%20%22GitHub%22%2C%22allowIdChange%22%3A%20true%7D",
    "MiXplorer": "https://apps.obtainium.imranr.dev/redirect?r=obtainium://app/%7B%22id%22%3A%20%22com.mixplorer%22%2C%22url%22%3A%20%22https%3A%2F%2Fgithub.com%2FnOneCode4u%2Fmixplorer-google-drive%22%2C%22author%22%3A%20%22nOneCode4u%22%2C%22name%22%3A%20%22MiXplorer%22%2C%22additionalSettings%22%3A%20%22%7B%5C%22includePrereleases%5C%22%3Afalse%2C%5C%22fallbackToOlderReleases%5C%22%3Atrue%2C%5C%22filterReleaseTitlesByRegEx%5C%22%3A%5C%22MiXplorer%5C%22%2C%5C%22filterReleaseNotesByRegEx%5C%22%3A%5C%22%5C%22%2C%5C%22verifyLatestTag%5C%22%3Afalse%2C%5C%22sortMethodChoice%5C%22%3A%5C%22date%5C%22%2C%5C%22useLatestAssetDateAsReleaseDate%5C%22%3Afalse%2C%5C%22releaseTitleAsVersion%5C%22%3Afalse%2C%5C%22trackOnly%5C%22%3Afalse%2C%5C%22versionExtractionRegEx%5C%22%3A%5C%22%5C%22%2C%5C%22matchGroupToUse%5C%22%3A%5C%22%5C%22%2C%5C%22versionDetection%5C%22%3Atrue%2C%5C%22releaseDateAsVersion%5C%22%3Afalse%2C%5C%22useVersionCodeAsOSVersion%5C%22%3Afalse%2C%5C%22apkFilterRegEx%5C%22%3A%5C%22%5C%22%2C%5C%22invertAPKFilter%5C%22%3Afalse%2C%5C%22autoApkFilterByArch%5C%22%3Atrue%2C%5C%22appName%5C%22%3A%5C%22MiXplorer%5C%22%2C%5C%22appAuthor%5C%22%3A%5C%22nOneCode4u%5C%22%2C%5C%22shizukuPretendToBeGooglePlay%5C%22%3Atrue%2C%5C%22allowInsecure%5C%22%3Atrue%2C%5C%22exemptFromBackgroundUpdates%5C%22%3Afalse%2C%5C%22skipUpdateNotifications%5C%22%3Afalse%2C%5C%22about%5C%22%3A%5C%22Fast%2C%20smooth%2C%20dual-pane%20file%20manager%20with%20cloud%20storage%2C%20archive%20support%2C%20built-in%20media%20player%2C%20and%20deep%20customisation.%20No%20ads%2C%20no%20trackers.%5C%22%2C%5C%22refreshBeforeDownload%5C%22%3Atrue%2C%5C%22includeZips%5C%22%3Afalse%2C%5C%22zippedApkFilterRegEx%5C%22%3A%5C%22%5C%22%7D%22%2C%22overrideSource%22%3A%20%22GitHub%22%2C%22allowIdChange%22%3A%20true%7D",
    }
    if app_name in _URLS:
        return _URLS[app_name]
    # Future unknown add-on fallback — opens repo in Obtainium
    import urllib.parse, json as _json
    cfg = {
        "id":     f"com.mixplorer.{app_name.lower().replace('_', '.')}",
        "url":    f"https://github.com/{GH_REPO}",
        "author": "nOneCode4u",
        "name":   display_name,
        "additionalSettings": _json.dumps({
            "includePrereleases":         False,
            "fallbackToOlderReleases":    True,
            "filterReleaseTitlesByRegEx": app_name.split("_")[-1] if "_" in app_name else app_name,
            "sortMethodChoice":           "date",
            "autoApkFilterByArch":        True,
            "refreshBeforeDownload":      True,
        }),
        "overrideSource": "GitHub",
        "allowIdChange":  True,
    }
    return ("https://apps.obtainium.imranr.dev/redirect?r=obtainium://app/"
            + urllib.parse.quote(_json.dumps(cfg, separators=(",", ":")), safe=""))


def _body_is_valid(body: str) -> bool:
    """
    Check that a release body contains all required sections.
    Returns False if any required marker is missing — triggering a body update.
    """
    required_markers = [
        "Obtainium-Get%20App",       # Obtainium badge
        "Mirrored from the developer",  # Footer
    ]
    return all(marker in body for marker in required_markers)


def build_release_body(app_name: str, version_name: str, descriptions: dict, changelog: Optional[str]) -> str:
    info          = descriptions.get(app_name, {})
    display       = info.get("display_name", get_display_name(app_name))
    icon          = info.get("icon", "📦")
    obtainium_url = _get_obtainium_url(app_name, display)

    if changelog:
        middle = f"### What's New\n\n{changelog}\n\n---\n\n"
    else:
        middle = ""

    return (
        f"## {icon} {display}\n\n"
        f"[![Get on Obtainium](https://img.shields.io/badge/Obtainium-Get%20App-7040D4?style=flat-square)]({obtainium_url})\n\n"
        f"---\n\n"
        f"{middle}"
        f"*Mirrored from the developer's official Google Drive.*\n"
    )


def process_app(
    *,
    drive, rm, notifier, folder_info, app_name,
    descriptions, manual_overrides, work_dir,
) -> tuple[bool, Optional[dict]]:

    folder_id = folder_info["id"]
    log.info("─" * 50)
    log.info(f"App: {app_name}  (Drive folder: {folder_info['name']})")

    apk_metas = drive.list_apks(folder_id)
    if not apk_metas:
        log.warning(f"No APKs found for {app_name} — skipping.")
        return True, None

    log.info(f"Found {len(apk_metas)} APK(s): {[m['name'] for m in apk_metas]}")

    app_dir = work_dir / app_name
    app_dir.mkdir(parents=True, exist_ok=True)

    downloaded:       list[tuple[Path, dict]] = []
    failed_downloads: list[str]               = []

    for meta in apk_metas:
        dest = app_dir / meta["name"]
        ok   = drive.download_file(meta["id"], dest, expected_md5=meta.get("md5Checksum"))
        if ok:
            downloaded.append((dest, meta))
        else:
            failed_downloads.append(meta["name"])

    if failed_downloads:
        notifier.download_failure(failed_downloads, app_name)
        write_state("Paused", "Download failure", f"{app_name}: {failed_downloads}")
        return False, None

    apk_infos:          list[tuple[Path, APKInfo]] = []
    failed_extractions: list[str]                  = []

    for path, meta in downloaded:
        override = manual_overrides.get(meta["name"])
        if override:
            log.info(f"  Manual override applied for {meta['name']}")
            info = APKInfo(
                version_name   = override["version_name"],
                version_code   = override["version_code"],
                package_name   = "manual-override",
                arch           = override.get("arch", "java"),
                source_methods = ["manual"],
                confidence     = "manual",
            )
            apk_infos.append((path, info))
            continue
        try:
            info = extract_apk_info(path)
        except Exception as exc:
            log.error(f"  Extraction exception for {meta['name']}: {exc}")
            info = None
        if info is None:
            failed_extractions.append(meta["name"])
        else:
            apk_infos.append((path, info))

    if failed_extractions:
        append_pending_overrides(
            [{"filename": f, "app_name": app_name, "arch": "unknown"} for f in failed_extractions]
        )
        notifier.extraction_failure(failed_extractions, app_name)
        write_state("Paused", "Extraction failure", f"{app_name}: {failed_extractions}")
        return False, None

    if not apk_infos:
        log.error(f"No APKInfo produced for {app_name}")
        return False, None

    version_counts     = Counter(i.version_name for _, i in apk_infos)
    primary_version, _ = version_counts.most_common(1)[0]
    if len(version_counts) > 1:
        log.warning(f"Mixed versions in {app_name}: {dict(version_counts)}. Using majority: {primary_version}")
        apk_infos = [(p, i) for p, i in apk_infos if i.version_name == primary_version]

    version_name = primary_version

    # ── Check what actually exists on GitHub right now ──────────────────
    tag          = f"{app_name}_v{version_name}"
    existing     = rm.get_release_by_tag(tag)
    missing      = set()  # assets that need uploading; empty means all

    if existing:
        existing_assets = rm.get_release_assets(existing["id"])
        # Build the expected final filenames so we can compare
        renamed_preview = finalize_filenames(app_name, apk_infos)
        expected_names  = {new_name for _, new_name in renamed_preview}
        missing         = expected_names - existing_assets

        if not missing:
            # Also verify the release description is complete
            current_body = existing.get("body") or ""
            if _body_is_valid(current_body):
                log.info(f"  {app_name} v{version_name} fully released — nothing to do.")
                return True, None
            log.info(f"  {app_name} v{version_name}: assets complete but body incomplete — will update")

        log.info(
            f"  {app_name} v{version_name}: release exists but "
            f"{len(missing)} asset(s) missing — will upload: {missing}"
        )
    else:
        log.info(f"  New version detected: {app_name} v{version_name}")

    # ── Body-only update: no assets missing, just description incomplete ──
    # Skip APK download/extract/rename — just rebuild body and update.
    if existing and not missing:
        changelog     = fetch_changelog(None, version_name, app_name=app_name)
        release_body  = build_release_body(app_name, version_name, descriptions, changelog)
        rm.update_release_body(existing["id"], release_body)
        log.info(f"  {app_name} v{version_name}: body restored")
        return True, {"version_name": version_name, "body_updated": True}

    renamed_pairs = finalize_filenames(app_name, apk_infos)
    final_files: list[Path] = []

    for src, new_name in renamed_pairs:
        dst = app_dir / new_name
        if src.resolve() == dst.resolve():
            log.info(f"  Already named correctly: {new_name}")
            final_files.append(dst)
        else:
            shutil.copy2(src, dst)
            final_files.append(dst)
            log.info(f"  Renamed: {src.name}  →  {new_name}")

    for f in final_files:
        hits = re.findall(r"_v\d+[\.\d]*", f.name)
        if len(hits) > 1:
            log.error(f"  Double version detected in filename: {f.name}")
            write_state("Paused", "Filename validation failure", f.name)
            notifier.critical_error("Double version in filename",
                f"'{f.name}' contains the version twice. Fix config/rename_map.json.")
            return False, None

    changelog_apk = max(final_files, key=lambda p: p.stat().st_size)
    changelog     = fetch_changelog(changelog_apk, version_name, app_name=app_name)

    display      = descriptions.get(app_name, {}).get("display_name", get_display_name(app_name))
    release_name = f"{display} v{version_name}"
    tag          = f"{app_name}_v{version_name}"
    release_body = build_release_body(app_name, version_name, descriptions, changelog)

    if existing:
        release_id = existing["id"]
        log.info(f"  Uploading missing assets to existing release {tag}")
    else:
        release    = rm.create_release(tag, release_name, release_body)
        release_id = release["id"]

    for file_path in final_files:
        # Skip assets that already exist on the release (partial-release recovery)
        if existing and file_path.name not in missing:
            log.info(f"  Asset already present, skipping: {file_path.name}")
            continue
        try:
            rm.upload_asset(release_id, file_path)
        except Exception as exc:
            log.error(f"  Upload failed for {file_path.name}: {exc}")

    expected = [f.name for f in final_files]
    if not rm.verify_release(release_id, expected):
        notifier.upload_failure(tag, expected)
        return False, None

    return True, {
        "version_name": version_name,
        "version_code": apk_infos[0][1].version_code,
        "release_tag":  tag,
        "release_id":   release_id,
        "assets":       expected,
        "updated_at":   datetime.now(timezone.utc).isoformat(),
    }


def main() -> None:
    log.info("=" * 60)
    log.info("APK Update Pipeline — Start")
    log.info(f"  Repository : {GH_REPO}")
    log.info(f"  Debug      : {DEBUG_MODE}")
    log.info("=" * 60)

    state = read_state()
    log.info(f"Workflow state: {state}")
    if state == "Paused":
        log.info("State is Paused — exiting without processing.")
        sys.exit(0)

    drive    = DriveClient(GDRIVE_API_KEY)
    rm       = ReleaseManager(GH_TOKEN, GH_REPO)
    notifier = Notifier(rm)
    descriptions      = load_json(DESCRIPTIONS_FILE, {})
    manual_overrides  = load_manual_overrides()
    rename_map        = load_rename_map()

    try:
        subfolders = drive.list_subfolders(ROOT_FOLDER_ID)
    except DriveError as exc:
        log.error(f"Cannot list Drive root: {exc}")
        write_state("Error", "Drive API failure", str(exc))
        notifier.critical_error("Listing Drive root folder", str(exc))
        sys.exit(1)

    log.info(f"Drive folders found: {[f['name'] for f in subfolders]}")

    app_folders: list[tuple[dict, str]] = []
    for folder in subfolders:
        fname       = folder["name"]
        clean_fname = _strip_version_suffix(fname)
        lookup_key  = fname if fname in rename_map else clean_fname

        if lookup_key in rename_map:
            app_name = rename_map[lookup_key]
        else:
            app_name = auto_map_folder_name(fname)
            log.info(f"Auto-mapped: {fname!r} → {app_name!r} (stored as {clean_fname!r})")
            rename_map[clean_fname] = app_name
            if clean_fname != fname:
                rename_map[fname] = app_name
            save_rename_map(rename_map)
            notifier.new_app_discovered(fname, app_name)

        app_folders.append((folder, app_name))

    results: dict[str, str] = {}
    overall_success = True

    with tempfile.TemporaryDirectory(prefix="apk_update_") as tmp:
        work_dir = Path(tmp)

        for folder_info, app_name in app_folders:
            try:
                ok, release_info = process_app(
                    drive             = drive,
                    rm                = rm,
                    notifier          = notifier,
                    folder_info       = folder_info,
                    app_name          = app_name,
                    descriptions      = descriptions,
                    manual_overrides  = manual_overrides,
                    work_dir          = work_dir,
                )
            except (DriveError, GitHubError) as exc:
                log.error(f"API error for {app_name}: {exc}")
                write_state("Error", f"API error in {app_name}", str(exc))
                notifier.critical_error(app_name, traceback.format_exc())
                results[app_name] = "api_error"
                overall_success   = False
                break
            except Exception as exc:
                log.exception(f"Unexpected error for {app_name}: {exc}")
                write_state("Error", f"Unexpected error in {app_name}", str(exc))
                notifier.critical_error(app_name, traceback.format_exc())
                results[app_name] = "exception"
                overall_success   = False
                break

            if ok and release_info:
                results[app_name]           = "released"
                log.info(f"✓ Released: {app_name} v{release_info['version_name']}")
            elif ok:
                results[app_name] = "no_update"
                log.info(f"○ No update: {app_name}")
            else:
                results[app_name] = "failed"
                overall_success   = False
                log.error(f"✗ Failed: {app_name}")
                break

    released_n = sum(1 for v in results.values() if v == "released")
    skipped_n  = sum(1 for v in results.values() if v == "no_update")
    failed_n   = sum(1 for v in results.values() if v not in ("released", "no_update"))
    summary    = f"Released={released_n}  Skipped={skipped_n}  Failed={failed_n}"

    log.info("=" * 60)
    log.info(f"Pipeline complete — {summary}")
    log.info(f"Results: {results}")
    log.info("=" * 60)

    if overall_success:
        write_state("Running", "Success", summary)

    sys.exit(0 if overall_success else 1)


if __name__ == "__main__":
    main()
