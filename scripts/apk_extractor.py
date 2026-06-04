"""
Multi-method APK version and architecture extractor.
"""
import os
import re
import subprocess
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from logger import get_logger

log = get_logger(__name__)

AAPT_PATH: str = os.environ.get("AAPT_PATH", "aapt")

_ARCH_DIRS: dict[str, str] = {
    "lib/armeabi-v7a": "arm",
    "lib/arm64-v8a":   "arm64",
    "lib/x86":         "x86",
    "lib/x86_64":      "x64",
}


@dataclass
class APKInfo:
    version_name:   str
    version_code:   str
    package_name:   str
    arch:           str
    source_methods: list[str] = field(default_factory=list)
    confidence:     str = "unknown"


# ── Architecture detection ────────────────────────────────────────────────────

def detect_arch(apk_path: Path) -> str:
    """
    Returns one of: 'java', 'arm', 'arm64', 'x86', 'x64', 'universal'.

    Special case: if the APK filename contains '-universal', we trust that
    developer label even when no native libs are detected (e.g. MiXEncrypt).
    """
    with zipfile.ZipFile(apk_path, "r") as zf:
        names = zf.namelist()

    found: set[str] = set()
    for dir_prefix, arch_name in _ARCH_DIRS.items():
        if any(n.startswith(dir_prefix + "/") for n in names):
            found.add(arch_name)

    if len(found) == 0:
        # No native lib dirs found — check filename for developer's own label
        fname_lower = apk_path.name.lower()
        if "-universal" in fname_lower:
            result = "universal"
        elif "-arm64" in fname_lower:
            result = "arm64"
        elif "-arm" in fname_lower:
            result = "arm"
        elif "-x64" in fname_lower:
            result = "x64"
        elif "-x86" in fname_lower:
            result = "x86"
        else:
            result = "java"
    elif len(found) == 1:
        result = next(iter(found))
    else:
        result = "universal"

    log.debug(f"arch({apk_path.name}) = {result}  (lib dirs: {found or 'none'})")
    return result


# ── Individual extraction methods ─────────────────────────────────────────────

def _try_call(obj, method_name: str):
    """Safely call a method on obj, returning None on any failure."""
    fn = getattr(obj, method_name, None)
    if fn and callable(fn):
        try:
            return fn()
        except Exception:
            pass
    return None


def _via_aapt(apk_path: Path) -> Optional[tuple[str, str, str]]:
    try:
        result = subprocess.run(
            [AAPT_PATH, "dump", "badging", str(apk_path)],
            capture_output=True,
            text=True,
            timeout=45,
        )
        if result.returncode != 0:
            log.debug(f"aapt non-zero exit for {apk_path.name}: {result.stderr[:200]}")
            return None

        for line in result.stdout.splitlines():
            if not line.startswith("package:"):
                continue
            vn  = re.search(r"versionName='([^']+)'", line)
            vc  = re.search(r"versionCode='([^']+)'", line)
            pkg = re.search(r"\bname='([^']+)'",       line)
            if vn and vc and pkg:
                return vn.group(1).strip(), vc.group(1).strip(), pkg.group(1).strip()

        log.debug(f"aapt: no 'package:' line found for {apk_path.name}")
        return None

    except FileNotFoundError:
        log.warning(f"aapt binary not found at '{AAPT_PATH}'")
        return None
    except subprocess.TimeoutExpired:
        log.warning(f"aapt timed out on {apk_path.name}")
        return None
    except Exception as exc:
        log.warning(f"aapt error on {apk_path.name}: {exc}")
        return None


def _via_pyaxmlparser(apk_path: Path) -> Optional[tuple[str, str, str]]:
    """
    Pure-Python AXML parser.
    Handles multiple API versions of pyaxmlparser gracefully.
    """
    try:
        from pyaxmlparser import APK  # type: ignore[import]

        apk = APK(str(apk_path))

        # versionName — try every known attribute across versions
        vn = (
            _try_call(apk, "get_app_version") or
            _try_call(apk, "get_androidversion_name") or
            getattr(apk, "version_name", None) or
            getattr(apk, "android_version_name", None)
        )

        # versionCode
        vc = (
            _try_call(apk, "get_androidversion_code") or
            getattr(apk, "version_code", None) or
            getattr(apk, "android_version_code", None)
        )

        # packageName
        pkg = (
            _try_call(apk, "get_package") or
            getattr(apk, "package", None) or
            getattr(apk, "packagename", None)
        )

        if vn and vc and pkg:
            return str(vn).strip(), str(vc).strip(), str(pkg).strip()
        return None

    except ImportError:
        log.warning("pyaxmlparser not installed — skipping method")
        return None
    except Exception as exc:
        log.warning(f"pyaxmlparser error on {apk_path.name}: {exc}")
        return None


def _via_androguard(apk_path: Path) -> Optional[tuple[str, str, str]]:
    try:
        try:
            from androguard.misc import AnalyzeAPK  # type: ignore[import]
            a, _, _ = AnalyzeAPK(str(apk_path))
        except ImportError:
            from androguard.core.apk import APK as AG_APK  # type: ignore[import]
            a = AG_APK(str(apk_path))

        vn  = a.get_androidversion_name()
        vc  = a.get_androidversion_code()
        pkg = a.get_package()

        if vn and vc and pkg:
            return str(vn).strip(), str(vc).strip(), str(pkg).strip()
        return None

    except ImportError:
        log.warning("androguard not installed — skipping method")
        return None
    except Exception as exc:
        log.warning(f"androguard error on {apk_path.name}: {exc}")
        return None


# ── Orchestrator ──────────────────────────────────────────────────────────────

_METHODS = [
    ("aapt",         _via_aapt),
    ("pyaxmlparser", _via_pyaxmlparser),
    ("androguard",   _via_androguard),
]


def extract_apk_info(apk_path: Path) -> Optional[APKInfo]:
    """
    Extract version info using majority-vote cross-verification.
    Returns APKInfo on success, None if extraction is impossible/ambiguous.
    """
    log.info(f"Extracting version from {apk_path.name} …")

    if not zipfile.is_zipfile(apk_path):
        log.error(f"Not a valid ZIP/APK: {apk_path.name}")
        return None

    results: dict[str, tuple[str, str, str]] = {}

    # Phase 1: fast methods
    for name, fn in _METHODS[:2]:
        res = fn(apk_path)
        if res:
            results[name] = res
            log.debug(f"[{name}] vn={res[0]}  vc={res[1]}  pkg={res[2]}")

    # Phase 2: decide if tiebreaker needed
    need_tiebreaker = False
    if len(results) == 0:
        need_tiebreaker = True
    elif len(results) == 1:
        log.warning(f"Only 1/2 fast methods succeeded for {apk_path.name} — running tiebreaker.")
        need_tiebreaker = True
    else:
        version_keys = {(v[0], v[1]) for v in results.values()}
        if len(version_keys) > 1:
            log.warning(f"Methods disagree on {apk_path.name}: {results} — running tiebreaker.")
            need_tiebreaker = True

    # Phase 3: tiebreaker
    if need_tiebreaker:
        name_tb, fn_tb = _METHODS[2]
        res = fn_tb(apk_path)
        if res:
            results[name_tb] = res
            log.debug(f"[{name_tb}] vn={res[0]}  vc={res[1]}  pkg={res[2]}")

    if not results:
        log.error(f"All extraction methods failed for {apk_path.name}")
        return None

    # Majority vote
    version_counter: Counter = Counter()
    method_map: dict[tuple, list[str]] = {}
    for method, (vn, vc, pkg) in results.items():
        key = (vn, vc)
        version_counter[key] += 1
        method_map.setdefault(key, []).append(method)

    best_key, best_count = version_counter.most_common(1)[0]
    total = len(results)

    if total >= 3 and best_count < 2:
        log.error(f"All {total} methods disagree for {apk_path.name}: {results}")
        return None

    vn, vc = best_key
    pkg = results[method_map[best_key][0]][2]
    confidence = "high" if best_count == total else ("medium" if best_count >= 2 else "low")

    arch = detect_arch(apk_path)

    log.info(
        f"✓ {apk_path.name}: vn={vn}  vc={vc}  arch={arch}  "
        f"confidence={confidence}  methods={method_map[best_key]}"
    )

    return APKInfo(
        version_name=vn,
        version_code=vc,
        package_name=pkg,
        arch=arch,
        source_methods=method_map[best_key],
        confidence=confidence,
    )
