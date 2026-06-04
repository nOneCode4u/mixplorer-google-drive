"""
Read and write workflow state via STATE.md.

The canonical state is stored as an HTML comment on a dedicated line:
    <!-- STATE: Running -->

Valid states: Running | Paused | Resumed | Error
"""
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from logger import get_logger

log = get_logger(__name__)

StateValue = Literal["Running", "Paused", "Resumed", "Error"]
STATE_FILE = Path("STATE.md")

_STATE_RE = re.compile(r"<!--\s*STATE:\s*(\w+)\s*-->")

_TEMPLATE = """\
# Workflow State

<!-- STATE: {state} -->

| Key | Value |
|-----|-------|
| **Last Updated** | {timestamp} UTC |
| **Last Status**  | {status} |
| **Details**      | {details} |

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
"""


def read_state() -> StateValue:
    """Return current workflow state. Defaults to 'Running' if file missing."""
    try:
        content = STATE_FILE.read_text(encoding="utf-8")
        m = _STATE_RE.search(content)
        if m and m.group(1) in ("Running", "Paused", "Resumed", "Error"):
            return m.group(1)  # type: ignore[return-value]
    except FileNotFoundError:
        pass
    return "Running"


def write_state(
    state: StateValue,
    status: str = "",
    details: str = "",
) -> None:
    """Overwrite STATE.md with the given state and metadata."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    content = _TEMPLATE.format(
        state=state,
        timestamp=timestamp,
        status=status or "—",
        details=details or "—",
    )
    STATE_FILE.write_text(content, encoding="utf-8")
    log.info(f"State → {state}  |  {status}")
