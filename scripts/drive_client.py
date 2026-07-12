"""
Google Drive API v3 client.

Uses a plain API key (no OAuth) — works for any publicly shared folder.
Falls back gracefully on transient network or rate-limit errors.
"""
import hashlib
import time
from pathlib import Path
from typing import Optional

import requests

from logger import get_logger

log = get_logger(__name__)

_DRIVE_BASE = "https://www.googleapis.com/drive/v3"
_FOLDER_MIME = "application/vnd.google-apps.folder"

_MAX_RETRIES = 3
_RETRY_DELAYS = (30, 60, 120)       # Seconds between each retry attempt
_DOWNLOAD_TIMEOUT = 180             # Seconds for streaming download


class DriveError(RuntimeError):
    """Raised when the Drive API returns an unrecoverable error."""


class DriveClient:
    def __init__(self, api_key: str) -> None:
        self._key = api_key
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "APK-Mirror-Bot/1.0 (GitHub Actions)"

    # ── Internal helpers ──────────────────────────────────────────────────

    def _get(self, endpoint: str, params: Optional[dict] = None) -> dict:
        """
        GET wrapper with retry + back-off.
        Always injects the API key.
        """
        url = f"{_DRIVE_BASE}/{endpoint}"
        params = dict(params or {})
        params["key"] = self._key

        for attempt in range(_MAX_RETRIES):
            try:
                resp = self._session.get(url, params=params, timeout=30)

                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 60))
                    log.warning(f"Drive rate-limited. Waiting {wait}s …")
                    time.sleep(wait)
                    continue

                if resp.status_code == 403:
                    body = resp.json().get("error", {})
                    raise DriveError(f"Drive 403: {body.get('message', resp.text[:200])}")

                resp.raise_for_status()
                return resp.json()

            except DriveError:
                raise
            except requests.RequestException as exc:
                log.warning(f"Drive request error (attempt {attempt + 1}/{_MAX_RETRIES}): {exc}")
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_DELAYS[attempt])
                else:
                    raise DriveError(f"Drive API unreachable after {_MAX_RETRIES} attempts: {exc}") from exc

        raise DriveError("Unreachable")   # safety net

    # ── Public API ────────────────────────────────────────────────────────

    def list_folder(self, folder_id: str) -> list[dict]:
        """
        Return all non-trashed items directly inside *folder_id*.
        Handles pagination automatically.
        """
        items: list[dict] = []
        page_token: Optional[str] = None

        while True:
            params: dict = {
                "q": f"'{folder_id}' in parents and trashed=false",
                "fields": (
                    "nextPageToken,"
                    "files(id,name,mimeType,size,modifiedTime,md5Checksum)"
                ),
                "pageSize": 100,
                "orderBy": "name",
            }
            if page_token:
                params["pageToken"] = page_token

            result = self._get("files", params)
            items.extend(result.get("files", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                break

        log.debug(f"list_folder({folder_id}): {len(items)} items")
        return items

    def list_subfolders(self, folder_id: str) -> list[dict]:
        """Return only sub-folders."""
        return [i for i in self.list_folder(folder_id) if i["mimeType"] == _FOLDER_MIME]

    def list_apks(self, folder_id: str) -> list[dict]:
        """Return only .apk files."""
        return [
            i for i in self.list_folder(folder_id)
            if i["name"].lower().endswith(".apk") and i["mimeType"] != _FOLDER_MIME
        ]

    def download_file(
        self,
        file_id: str,
        dest: Path,
        expected_md5: Optional[str] = None,
    ) -> bool:
        """
        Download *file_id* to *dest*.

        Verifies MD5 when *expected_md5* is provided.
        Retries up to _MAX_RETRIES times on failure.

        Returns True on success, False after all retries exhausted.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = f"{_DRIVE_BASE}/files/{file_id}"
        params = {"alt": "media", "key": self._key}

        for attempt in range(_MAX_RETRIES):
            try:
                log.info(
                    f"Downloading {dest.name} "
                    f"(attempt {attempt + 1}/{_MAX_RETRIES}) …"
                )
                md5 = hashlib.md5()

                with self._session.get(
                    url, params=params, stream=True, timeout=_DOWNLOAD_TIMEOUT
                ) as resp:
                    resp.raise_for_status()
                    with open(dest, "wb") as fh:
                        for chunk in resp.iter_content(chunk_size=65_536):
                            fh.write(chunk)
                            md5.update(chunk)

                # Integrity check
                if expected_md5:
                    actual = md5.hexdigest()
                    if actual != expected_md5:
                        log.error(
                            f"MD5 mismatch for {dest.name}: "
                            f"expected={expected_md5} actual={actual}"
                        )
                        dest.unlink(missing_ok=True)
                        if attempt < _MAX_RETRIES - 1:
                            time.sleep(_RETRY_DELAYS[attempt])
                        continue

                size = dest.stat().st_size
                log.info(f"Downloaded {dest.name} ({size:,} bytes)")
                return True

            except Exception as exc:
                log.error(f"Download error for {dest.name}: {exc}")
                dest.unlink(missing_ok=True)
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_DELAYS[attempt])

        log.error(f"All download attempts failed for {dest.name}")
        return False
