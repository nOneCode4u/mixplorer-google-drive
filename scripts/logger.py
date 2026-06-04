"""Centralised logging — console output only."""
import logging
import os

_initialized = False


def _ensure_initialized() -> None:
    global _initialized
    if _initialized:
        return
    debug = os.environ.get("DEBUG_MODE", "false").lower() == "true"
    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-8s] %(name)-20s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG if debug else logging.INFO)
    ch.setFormatter(fmt)
    root.addHandler(ch)
    for lib in ("androguard", "pyaxmlparser", "urllib3", "requests"):
        logging.getLogger(lib).setLevel(logging.WARNING)
    _initialized = True


def get_logger(name: str) -> logging.Logger:
    _ensure_initialized()
    return logging.getLogger(name)
