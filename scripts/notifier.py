"""
Human-readable GitHub Issue notifications for pipeline failures.
"""
from release_manager import ReleaseManager
from logger import get_logger

log = get_logger(__name__)


class Notifier:
    def __init__(self, rm: ReleaseManager) -> None:
        self._rm = rm

    def extraction_failure(self, filenames: list[str], app_name: str) -> None:
        names_md = "\n".join(f"- `{n}`" for n in filenames)
        body = f"""\
## ⚠️ Action Required — APK version extraction failed

Version extraction could not be completed automatically for the following APK(s) \
belonging to **{app_name}**:

{names_md}

### How to fix

1. Download the APK locally and run:
      aapt dump badging <filename.apk>
   Note the `versionName` and `versionCode` values from the output.
2. Open **`MANUAL_VERSIONS.md`** in this repository.
3. Find the pending row for the affected APK(s) and fill in the columns.
4. Open **`STATE.md`** and change `Paused` → `Resumed`.
5. The next workflow run will pick up your changes automatically.

---
*This issue was created automatically by the APK update pipeline.*
"""
        self._rm.create_issue(
            title=f"⚠️ Version extraction failed: {app_name}",
            body=body,
            labels=["automated", "needs-attention", "extraction-failure"],
        )

    def download_failure(self, filenames: list[str], app_name: str) -> None:
        names_md = "\n".join(f"- `{n}`" for n in filenames)
        body = f"""\
## ⚠️ Action Required — APK download failed

The following file(s) from **{app_name}** could not be downloaded from Google Drive \
after 3 attempts:

{names_md}

### Possible causes
- The Google Drive folder permissions changed.
- `GDRIVE_API_KEY` secret is expired or invalid.
- Temporary network issue on the runner.

### How to fix
1. Verify the Drive folder is still publicly accessible.
2. Check the `GDRIVE_API_KEY` secret under **Settings → Secrets → Actions**.
3. Set **`STATE.md`** → `Resumed` to trigger a retry on the next run.

---
*This issue was created automatically by the APK update pipeline.*
"""
        self._rm.create_issue(
            title=f"⚠️ Download failed: {app_name}",
            body=body,
            labels=["automated", "needs-attention", "download-failure"],
        )

    def upload_failure(self, release_tag: str, missing: list[str]) -> None:
        names_md = "\n".join(f"- `{n}`" for n in missing)
        body = f"""\
## ⚠️ Action Required — Release asset upload incomplete

Release **`{release_tag}`** was created but the following assets are missing:

{names_md}

### How to fix
1. Open the release on GitHub and upload the missing files manually, **or**
2. Delete the incomplete release and set **`STATE.md`** → `Resumed` to let the \
pipeline retry.

---
*This issue was created automatically by the APK update pipeline.*
"""
        self._rm.create_issue(
            title=f"⚠️ Upload verification failed: {release_tag}",
            body=body,
            labels=["automated", "needs-attention", "upload-failure"],
        )

    def new_app_discovered(self, folder_name: str, mapped_name: str) -> None:
        body = f"""\
## ℹ️ New App Folder Discovered

A new folder was found in the Google Drive source:

| Drive Folder | Auto-mapped Name |
|---|---|
| `{folder_name}` | `{mapped_name}` |

### Verify
- If the mapping looks correct, no action is needed — close this issue.
- If the mapping is wrong, update `config/rename_map.json` manually and commit the change.

---
*This issue was created automatically by the APK update pipeline.*
"""
        self._rm.create_issue(
            title=f"ℹ️ New app discovered: {folder_name} → {mapped_name}",
            body=body,
            labels=["automated", "info", "new-app"],
        )

    def critical_error(self, context: str, error: str) -> None:
        body = f"""\
## 🔴 Critical Pipeline Error

The APK update pipeline encountered an unrecoverable error.

**Context:** {context}

**Error:**
{error[:2000]}
### How to fix
1. Review the Actions workflow log for the full stack trace.
2. Fix the underlying issue.
3. Set **`STATE.md`** → `Resumed` to retry.

---
*This issue was created automatically by the APK update pipeline.*
"""
        self._rm.create_issue(
            title=f"🔴 Critical pipeline error: {context[:60]}",
            body=body,
            labels=["automated", "needs-attention"],
        )
