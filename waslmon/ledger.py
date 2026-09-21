"""The committed ledger (state/seen.json): atomic writes, strict validation on read."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from . import __version__
from .config import Settings
from .models import Ledger, LedgerCorrupt, LedgerMeta, ListingRecord


def atomic_write_json(path: Path, data: Any) -> None:
    """temp file + fsync + os.replace so a killed job never leaves a truncated ledger."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_ledger(path: Path) -> Optional[Ledger]:
    """None when absent (baseline gate). Raises LedgerCorrupt instead of returning an empty set."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Ledger.model_validate(data)
    except (ValueError, ValidationError) as e:
        raise LedgerCorrupt(f"{path} failed validation: {str(e)[:300]} "
                            f"(recover with: git show HEAD~1:{path.as_posix()})") from e


def save_ledger(path: Path, ledger: Ledger) -> None:
    atomic_write_json(path, ledger.model_dump(mode="json"))


def new_ledger(settings: Settings, now: datetime) -> Ledger:
    return Ledger(meta=LedgerMeta(query_url=settings.search_url(), scraper_version=__version__,
                                  created_at=now, source_host=settings.source.host))


def forget_refs(ledger: Ledger, refs: list[str]) -> list[str]:
    dropped = []
    for ref in refs:
        ref = ref.strip().upper()
        if ref and ledger.listings.pop(ref, None) is not None:
            dropped.append(ref)
    return dropped


def compact(ledger: Ledger, now: datetime, archive_dir: Path, after_days: int) -> int:
    """Move records removed more than after_days ago into state/archive-YYYY.json."""
    cutoff = now - timedelta(days=after_days)
    to_archive: list[ListingRecord] = [r for r in ledger.listings.values()
                                       if r.removed_at is not None and r.removed_at < cutoff]
    if not to_archive:
        return 0
    path = archive_dir / f"archive-{now.year}.json"
    existing: list[dict] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            existing = []
    existing.extend(r.model_dump(mode="json") for r in to_archive)
    atomic_write_json(path, existing)
    for r in to_archive:
        ledger.listings.pop(r.ref, None)
    return len(to_archive)
