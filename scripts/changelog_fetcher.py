"""
Changelog fetcher — APKMirror + XDA (MiXplorer only).

HTML target on APKMirror release pages:
    <div class="notes wrapText ">
        <p>...</p>
    </div>

The content uses <br /> to separate bullet points inside <p> tags.

Strategy per app:
  MiXplorer  → XDA thread post first, APKMirror as fallback
  All others → APKMirror only

Returns None on any failure — callers omit the changelog section silently.
"""
import re
import time
from pathlib import Path
from typing import Optional

import requests

from logger import get_logger

log = get_logger(__name__)

_BASE = "https://www.apkmirror.com"

# Known APKMirror slugs per app.
# "slug"   = folder slug in the APKMirror developer path
# "prefix" = release page slug prefix (most match the folder slug; Codecs differs)
_APP_CONFIG: dict[str, dict] = {
    "MiXplorer":   {"slug": "mixplorer-hootanparsa",  "prefix": "mixplorer"},
    "MiX_Archive": {"slug": "mix-archive",             "prefix": "mix-archive"},
    "MiX_Codecs":  {"slug": "mix-codecs",              "prefix": "mix-codecs-mixplorer-addon"},
    "MiX_Encrypt": {"slug": "mix-encrypt",             "prefix": "mix-encrypt"},
    "MiX_Image":   {"slug": "mix-image",               "prefix": "mix-image"},
    "MiX_PDF":     {"slug": "mix-pdf",                 "prefix": "mix-pdf"},
    "MiX_Tagger":  {"slug": "mix-tagger",              "prefix": "mix-tagger"},
}

_DEV_SLUG = "hootan-parsa"

# XDA post URL for MiXplorer changelogs (primary source for MiXplorer)
_XDA_MIXPLORER_URL = (
    "https://xdaforums.com/t/"
    "app-2-3-mixplorer-v6-x-released-fully-featured-file-manager"
    ".1523691/post-23374098"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Mobile Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.apkmirror.com/",
}

_TIMEOUT = 20

# Metadata line patterns to discard (version stamps, "From version X" headings)
_METADATA_RE = re.compile(
    r'^(?:'
    r'from\s+version'
    r'|v?\d+[\.\d]+[_\-]\d+\s*:?'
    r')',
    re.IGNORECASE,
)


def _version_to_slug(version_name: str) -> str:
    """'6.70.3' → '6-70-3',  '2.9' → '2-9'"""
    return version_name.replace(".", "-")


def _derive_slug(app_name: str) -> str:
    """
    Auto-derive APKMirror slugs for unknown future add-ons.
    'MiX_NewName' → slug='mix-newname', prefix='mix-newname'
    """
    return app_name.lower().replace("_", "-")


def _apkmirror_url(app_name: str, version_name: str) -> str:
    ver = _version_to_slug(version_name)
    if app_name in _APP_CONFIG:
        cfg    = _APP_CONFIG[app_name]
        slug   = cfg["slug"]
        prefix = cfg["prefix"]
    else:
        slug   = _derive_slug(app_name)
        prefix = slug
    return f"{_BASE}/apk/{_DEV_SLUG}/{slug}/{prefix}-{ver}-release/"


def _get_page(url: str, referer: Optional[str] = None) -> Optional[str]:
    """Fetch a URL and return HTML text, or None on any error."""
    headers = dict(_HEADERS)
    if referer:
        headers["Referer"] = referer
    try:
        time.sleep(1.5)
        resp = requests.get(url, headers=headers, timeout=_TIMEOUT, allow_redirects=True)
        if resp.status_code == 200:
            return resp.text
        log.debug(f"  HTTP {resp.status_code}: {url}")
        return None
    except requests.RequestException as exc:
        log.debug(f"  Request failed ({url}): {exc}")
        return None


# ── APKMirror parser ──────────────────────────────────────────────────────────

def _parse_apkmirror_notes(html: str) -> Optional[str]:
    """
    Extract the content of <div class="notes wrapText..."> from an
    APKMirror release page and return a Markdown bullet list, or None.
    """
    # Target the notes div — class attribute may have trailing space
    m = re.search(
        r'<div\s+class=["\']notes\s+wrapText[^"\']*["\'][^>]*>(.*?)</div>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return None

    raw = m.group(1).strip()
    if not raw:
        return None

    return _html_to_bullets(raw)


def _html_to_bullets(raw: str) -> Optional[str]:
    """
    Convert the inner HTML of the notes div to a Markdown bullet list.

    APKMirror uses <br /> to separate bullets within a single <p>.
    Single-bullet entries have no <br />.
    """
    # Split on <br /> to get individual line candidates
    parts = re.split(r'<br\s*/?>', raw, flags=re.IGNORECASE)

    lines = []
    for part in parts:
        # Strip all HTML tags (including <p>, <b>, <strong>, <a>, etc.)
        text = re.sub(r'<[^>]+>', ' ', part)

        # Decode common HTML entities
        text = (text
                .replace("&bull;",  "•")
                .replace("&amp;",   "&")
                .replace("&lt;",    "<")
                .replace("&gt;",    ">")
                .replace("&#8226;", "•")
                .replace("&nbsp;",  " "))

        # Collapse whitespace
        text = re.sub(r'\s+', ' ', text).strip()

        if not text:
            continue

        # Strip leading bullet characters
        text = re.sub(r'^[•\-\*·▪▸➤→\u2022]\s*', '', text).strip()

        if not text:
            continue

        # Discard metadata lines (version stamps, "From version X:")
        if _METADATA_RE.match(text):
            continue

        # Discard short noise (e.g. lone punctuation)
        if len(text) < 4:
            continue

        lines.append(f"- {text}")

    return "\n".join(lines) if lines else None


# ── XDA parser (MiXplorer only) ───────────────────────────────────────────────

def _parse_xda_post(html: str, version_name: str) -> Optional[str]:
    """
    Extract the changelog for *version_name* from the XDA post HTML.
    Returns a Markdown bullet list or None.
    """
    # XDA post content is typically in a <div class="bbWrapper"> or similar
    # Look for the version string followed by bullet points
    # Pattern: "v6.70.3" or "v6.70.3-26022810:" followed by • lines

    escaped_ver = re.escape(version_name)

    # Strip all HTML tags first for plain-text matching
    text = re.sub(r'<[^>]+>', '\n', html)
    text = (text
            .replace("&bull;",  "•")
            .replace("&amp;",   "&")
            .replace("&lt;",    "<")
            .replace("&gt;",    ">")
            .replace("&nbsp;",  " "))
    text = re.sub(r'\n{3,}', '\n\n', text)

    # Find section starting with the version
    ver_pattern = re.compile(
        rf'v{escaped_ver}[^\n]*\n((?:[^\n]*[•\-\*][^\n]+\n?)+)',
        re.IGNORECASE,
    )
    m = ver_pattern.search(text)
    if not m:
        return None

    block = m.group(1).strip()
    lines = []
    for line in block.splitlines():
        line = line.strip()
        line = re.sub(r'^[•\-\*·▪▸➤→\u2022]\s*', '', line).strip()
        if len(line) > 3:
            lines.append(f"- {line}")

    return "\n".join(lines) if lines else None


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_changelog(apk_path: Optional[Path], version_name: str, app_name: str = "MiXplorer") -> Optional[str]:
    """
    Fetch changelog for *app_name* at *version_name*.

    Returns a Markdown bullet list string on success, None if unavailable.
    Callers must treat None as "omit changelog section".
    apk_path may be None — APK asset search is skipped, network sources still tried.
    """
    log.info(f"Fetching changelog: {app_name} v{version_name}")

    result: Optional[str] = None

    # ── MiXplorer: try XDA first ──────────────────────────────────────────
    if app_name == "MiXplorer":
        log.debug(f"  Trying XDA: {_XDA_MIXPLORER_URL}")
        html = _get_page(_XDA_MIXPLORER_URL, referer="https://xdaforums.com/")
        if html:
            result = _parse_xda_post(html, version_name)
            if result:
                log.info(f"  Changelog from XDA ({len(result.splitlines())} line(s))")
                return result
            log.debug("  XDA: page fetched but version not found — trying APKMirror")
        else:
            log.debug("  XDA: page unavailable — trying APKMirror")

    # ── APKMirror (all apps, including MiXplorer fallback) ────────────────
    url = _apkmirror_url(app_name, version_name)
    log.debug(f"  Trying APKMirror: {url}")
    html = _get_page(url, referer=_BASE + "/")
    if html:
        result = _parse_apkmirror_notes(html)
        if result:
            log.info(f"  Changelog from APKMirror ({len(result.splitlines())} line(s))")
            return result
        log.debug("  APKMirror: page fetched but notes not parsed")
    else:
        log.debug("  APKMirror: page unavailable")

    log.info(f"  No changelog found for {app_name} v{version_name} — section will be omitted")
    return None
