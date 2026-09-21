from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__, log
from .config import load_settings
from .models import ConfigError

MODES = ("monitor", "dry-run", "discovery", "selftest")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="waslmon", description="wasl.ae listing monitor")
    p.add_argument("--mode", choices=MODES, default=os.environ.get("WASLMON_MODE") or "monitor")
    p.add_argument("--root", default=os.environ.get("WASLMON_ROOT") or ".")
    p.add_argument("--config", default=None)
    a = p.parse_args(argv)
    root = Path(a.root).resolve()
    cfg = Path(a.config) if a.config else root / "config.yaml"
    log.info(f"waslmon v{__version__} mode={a.mode} root={root}")
    try:
        settings = load_settings(cfg, os.environ)
    except ConfigError as e:
        log.error(str(e))
        return 3
    if a.mode == "discovery":
        from .discovery import run as run_discovery
        return run_discovery(settings, root)
    from .runner import run
    return run(settings, a.mode, root, os.environ)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
