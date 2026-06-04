# Manual Version Overrides

> When APK version extraction fails, the pipeline pauses and opens a GitHub Issue.
> Fill in this file, then set `STATE.md` to `Resumed`.

**How:** Download the APK → run `aapt dump badging filename.apk` → note `versionName` and `versionCode`.
**arch values:** `java`, `arm`, `arm64`, `x86`, `x64`, `universal`

### Examples

| Filename | versionName | versionCode | arch | App |
|---|---|---|---|---|
| MiXplorer_v6.70.3_B26022812-arm64.apk | 6.70.3 | 26022812 | arm64 | MiXplorer |
| MiXArchive_B2602262-arm64.apk | 3.20 | 2602262 | arm64 | MiX_Archive |

> The examples above are filled in correctly. Your pending rows will show `FILL_ME` — replace those values.

## Pending

| Filename | versionName | versionCode | arch | App |
|---|---|---|---|---|
