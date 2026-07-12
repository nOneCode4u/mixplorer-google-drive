"""
Rename APKs to the standardised format:

    {AppName}_v{versionName}_B{versionCode}[-{arch_suffix}].apk

Arch suffix rules:
    java      → (no suffix)
    arm       → -arm
    arm64     → -arm64
    x86       → -x86
    x64       → -x64
    universal → -universal
"""
import json
import re
from pathlib import Path
from typing import Optional

from apk_extractor import APKInfo
from logger import get_logger

log = get_logger(__name__)

RENAME_MAP_PATH = Path("config/rename_map.json")

_ARCH_SUFFIX: dict[str, str] = {
    "java":      "",
    "arm":       "-arm",
    "arm64":     "-arm64",
    "x86":       "-x86",
    "x64":       "-x64",
    "universal": "-universal",
}

# Known folder-name → clean app-name overrides.
# These take priority over the auto-mapping logic.
_KNOWN_OVERRIDES: dict[str, str] = {
    "MiXplorer":         "MiXplorer",
    "MiXArchive":        "MiX_Archive",
    "MiXCodecs":         "MiX_Codecs",
    "MiXPlayerCodecs":   "MiX_Codecs",   # Drive folder includes "Player"
    "MiX Player Codecs": "MiX_Codecs",
    "MiXEncrypt":        "MiX_Encrypt",
    "MiXImage":          "MiX_Image",
    "MIXPDF":            "MiX_PDF",
    "MiXPDF":            "MiX_PDF",
    "MiXTagger":         "MiX_Tagger",
}


def load_rename_map() -> dict[str, str]:
    if RENAME_MAP_PATH.exists():
        with open(RENAME_MAP_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_rename_map(mapping: dict[str, str]) -> None:
    RENAME_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RENAME_MAP_PATH, "w", encoding="utf-8") as fh:
        json.dump(mapping, fh, indent=2, ensure_ascii=False)
    log.debug(f"Saved rename map ({len(mapping)} entries)")


def _strip_version_suffix(name: str) -> str:
    """
    Strip trailing version suffixes from a folder name.

    Examples:
        'MiXplorer-v6.70.3'   →  'MiXplorer'
        'MiXplorer_v6.70.3'   →  'MiXplorer'
        'MiXplorer v6.70.3'   →  'MiXplorer'
        'MiX_Archive-v3.20'   →  'MiX_Archive'
        'MiXplorer'           →  'MiXplorer'  (unchanged)
    """
    return re.sub(r'[-_ ]v\d+(?:[.\d]+)*\s*$', '', name).strip()


def auto_map_folder_name(folder_name: str) -> str:
    """
    Convert a Drive folder name to a standardised app name.

    Priority:
    1. Exact match in _KNOWN_OVERRIDES
    2. Strip version suffix, then try _KNOWN_OVERRIDES again
    3. Auto-derive: MiX_Foo from MiXFoo
    4. Sanitise fallback

    NOTE: We always map using the *clean* (version-stripped) name so
    that rename_map.json never needs a version-specific entry.
    """
    # 1. Direct exact match
    if folder_name in _KNOWN_OVERRIDES:
        return _KNOWN_OVERRIDES[folder_name]

    # 2. Strip version suffix and retry
    clean = _strip_version_suffix(folder_name)
    if clean in _KNOWN_OVERRIDES:
        return _KNOWN_OVERRIDES[clean]

    # 3. Auto-derive
    if clean == "MiXplorer":
        return "MiXplorer"
    m = re.match(r"^(MiX)[_\s]?([A-Z].*)$", clean)
    if m:
        return f"MiX_{m.group(2)}"

    # 4. Last-resort sanitise
    return re.sub(r"[\s\-]+", "_", clean).strip("_")


def get_display_name(app_name: str) -> str:
    """'MiX_Archive' → 'MiX Archive'"""
    return app_name.replace("_", " ")


def build_filename(app_name: str, info: APKInfo) -> str:
    """
    Build the final APK filename.

    Result: {app_name}_v{versionName}_B{versionCode}[-{arch}].apk
    app_name must already be the clean name (e.g. 'MiXplorer', 'MiX_Archive').
    """
    suffix = _ARCH_SUFFIX.get(info.arch, f"-{info.arch}")
    return f"{app_name}_v{info.version_name}_B{info.version_code}{suffix}.apk"


def finalize_filenames(
    app_name: str,
    apk_infos: list[tuple[Path, APKInfo]],
) -> list[tuple[Path, str]]:
    """Return (source_path, new_filename) pairs sorted by versionCode."""
    sorted_pairs = sorted(apk_infos, key=lambda x: _safe_int(x[1].version_code))

    results: list[tuple[Path, str]] = []
    for path, info in sorted_pairs:
        filename = build_filename(app_name, info)
        results.append((path, filename))
        log.debug(f"  {path.name}  →  {filename}")

    return results


def _safe_int(s: str, default: int = 0) -> int:
    try:
        return int(s)
    except (ValueError, TypeError):
        return default
