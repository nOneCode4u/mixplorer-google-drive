# Workflow State

<!-- STATE: Running -->

| Key | Value |
|-----|-------|
| **Last Updated** | 2026-07-19 07:53:42 UTC |
| **Last Status**  | Success |
| **Details**      | Released=0  Skipped=7  Failed=0 |

---

## Control Reference

### Resume After Pause
1. Resolve the open GitHub Issue that describes the problem.
2. If version extraction failed, fill in `MANUAL_VERSIONS.md`.
3. Change **`Paused`** → **`Resumed`** in the HTML comment above (line 3).
4. The next scheduled run will execute, then auto-reset to `Running`.

### Force a Manual Run
Go to **Actions → Daily APK Update Check → Run workflow**.

### State Definitions
| State | Meaning |
|-------|---------|
| `Running`  | Normal — scheduled runs active |
| `Paused`   | Manual intervention required — see linked Issue |
| `Resumed`  | Will execute once, then auto-reset to `Running` |
| `Error`    | Critical failure — inspect Actions logs |
