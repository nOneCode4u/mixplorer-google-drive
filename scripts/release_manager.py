"""
GitHub Releases API wrapper.

Handles:
  • Creating releases
  • Uploading assets with retry
  • Post-upload verification
  • Creating / closing GitHub Issues for notifications
"""
import time
from pathlib import Path
from typing import Optional

import requests

from logger import get_logger

log = get_logger(__name__)

_GH_API_BASE    = "https://api.github.com"
_GH_UPLOAD_BASE = "https://uploads.github.com"
_MAX_RETRIES    = 3
_RETRY_DELAYS   = (30, 60, 120)


class GitHubError(RuntimeError):
    """Raised for unrecoverable GitHub API errors."""


class ReleaseManager:
    def __init__(self, token: str, repo: str) -> None:
        """
        Parameters
        ----------
        token : str
            GitHub personal access token or GITHUB_TOKEN.
        repo : str
            Repository in 'owner/name' format.
        """
        self._repo = repo
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization":        f"Bearer {token}",
                "Accept":               "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    # ── Internal helpers ──────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        base: str = _GH_API_BASE,
        **kwargs,
    ) -> requests.Response:
        """
        Authenticated request with rate-limit awareness and retry.
        *path* is relative to repos/{repo}/.
        """
        url = f"{base}/repos/{self._repo}/{path}"
        for attempt in range(_MAX_RETRIES):
            try:
                resp = self._session.request(method, url, timeout=120, **kwargs)

                # Rate-limit
                if resp.status_code in (403, 429) and "rate limit" in resp.text.lower():
                    reset = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
                    wait  = max(reset - int(time.time()), 5)
                    log.warning(f"GitHub rate limit. Waiting {wait}s …")
                    time.sleep(wait)
                    continue

                # Secondary rate limit (abuse detection)
                if resp.status_code == 403 and "secondary" in resp.text.lower():
                    time.sleep(60 * (attempt + 1))
                    continue

                return resp

            except requests.RequestException as exc:
                log.error(f"GitHub API error (attempt {attempt + 1}): {exc}")
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_DELAYS[attempt])
                else:
                    raise GitHubError(f"GitHub API unreachable: {exc}") from exc

        raise GitHubError("Max retries exceeded")

    # ── Releases ──────────────────────────────────────────────────────────

    def get_release_by_tag(self, tag: str) -> Optional[dict]:
        resp = self._request("GET", f"releases/tags/{tag}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def create_release(
        self,
        tag: str,
        name: str,
        body: str,
    ) -> dict:
        resp = self._request(
            "POST",
            "releases",
            json={
                "tag_name":   tag,
                "name":       name,
                "body":       body,
                "draft":      False,
                "prerelease": False,
                "make_latest": "false",   # Multi-app repo: don't crown one as "latest"
            },
        )
        resp.raise_for_status()
        release = resp.json()
        log.info(f"Created release: {release['html_url']}")
        # Emit a GitHub Actions notice annotation
        print(f"::notice title=New Release Created::{release['html_url']}")
        return release


    def upload_asset(self, release_id: int, file_path: Path) -> dict:
        """
        Upload *file_path* as an asset to *release_id*.
        Uses the dedicated uploads endpoint.
        """
        data = file_path.read_bytes()
        upload_url = (
            f"{_GH_UPLOAD_BASE}/repos/{self._repo}/"
            f"releases/{release_id}/assets"
        )

        for attempt in range(_MAX_RETRIES):
            try:
                resp = self._session.post(
                    upload_url,
                    headers={"Content-Type": "application/vnd.android.package-archive"},
                    params={"name": file_path.name},
                    data=data,
                    timeout=300,
                )
                resp.raise_for_status()
                asset = resp.json()
                log.info(f"  Uploaded: {asset['name']}  ({asset['size']:,} bytes)")
                return asset

            except Exception as exc:
                log.error(f"Asset upload error for {file_path.name} (attempt {attempt + 1}): {exc}")
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_DELAYS[attempt])
                else:
                    raise

        raise RuntimeError(f"Failed to upload {file_path.name} after {_MAX_RETRIES} attempts")

    def verify_release(self, release_id: int, expected_names: list[str]) -> bool:
        """
        Confirm that every expected asset filename is present in the release.
        Returns True on success, False if any are missing.
        """
        resp = self._request("GET", f"releases/{release_id}")
        resp.raise_for_status()
        uploaded = {a["name"] for a in resp.json().get("assets", [])}

        missing = set(expected_names) - uploaded
        if missing:
            log.error(f"Verification FAILED — missing assets: {missing}")
            return False

        log.info(f"Verification PASSED — all {len(expected_names)} assets confirmed.")
        return True

    # ── Issues (notifications) ────────────────────────────────────────────

    def update_release_body(self, release_id: int, body: str) -> None:
        """Update the description (body) of an existing release."""
        resp = self._request(
            "PATCH",
            f"releases/{release_id}",
            json={"body": body},
        )
        resp.raise_for_status()
        log.info(f"  Release body updated (release_id={release_id})")


    def get_release_assets(self, release_id: int) -> set[str]:
        """Return the set of asset filenames currently attached to *release_id*."""
        resp = self._request("GET", f"releases/{release_id}")
        resp.raise_for_status()
        return {a["name"] for a in resp.json().get("assets", [])}


    def create_issue(
        self,
        title: str,
        body: str,
        labels: Optional[list[str]] = None,
    ) -> dict:
        resp = self._request(
            "POST",
            "issues",
            json={
                "title":  title,
                "body":   body,
                "labels": labels or ["automated", "needs-attention"],
            },
        )
        resp.raise_for_status()
        issue = resp.json()
        log.info(f"Issue #{issue['number']} created: {issue['html_url']}")
        return issue

    def close_issues_with_label_containing(self, title_substring: str) -> None:
        """Close any open automated issues whose title contains *title_substring*."""
        resp = self._request(
            "GET", "issues",
            params={"state": "open", "labels": "automated,needs-attention", "per_page": 50},
        )
        if resp.status_code != 200:
            return
        for issue in resp.json():
            if title_substring.lower() in issue["title"].lower():
                self._request("PATCH", f"issues/{issue['number']}", json={"state": "closed"})
                log.info(f"Closed issue #{issue['number']}")
