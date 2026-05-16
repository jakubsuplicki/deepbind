from __future__ import annotations

import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from services.source_import.models import (
    SourceScanReport,
    SourceScanResult,
    SourceSelectionRecord,
)


SCAN_TTL = timedelta(minutes=30)


_LOCK = threading.Lock()
_SCANS: dict[str, SourceScanResult] = {}
_SELECTIONS: dict[str, SourceSelectionRecord] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _prune_expired(now: Optional[datetime] = None) -> None:
    now = now or _now()
    expired: list[str] = []
    for scan_id, scan in _SCANS.items():
        created = _parse_iso(scan.report.created_at)
        if created + SCAN_TTL <= now:
            expired.append(scan_id)
    for scan_id in expired:
        _SCANS.pop(scan_id, None)

    expired_selections: list[str] = []
    for selection_id, selection in _SELECTIONS.items():
        created = _parse_iso(selection.summary.created_at)
        if created + SCAN_TTL <= now:
            expired_selections.append(selection_id)
    for selection_id in expired_selections:
        _SELECTIONS.pop(selection_id, None)


def new_scan_id() -> str:
    return f"scan_{secrets.token_urlsafe(12)}"


def new_selection_id() -> str:
    return f"sel_{secrets.token_urlsafe(12)}"


def new_import_batch_id() -> str:
    return f"import_{secrets.token_urlsafe(10)}"


def save_scan(scan: SourceScanResult) -> None:
    with _LOCK:
        _prune_expired()
        _SCANS[scan.report.scan_id] = scan


def get_scan(scan_id: str) -> SourceScanReport:
    return get_scan_record(scan_id).report


def get_scan_record(scan_id: str) -> SourceScanResult:
    with _LOCK:
        _prune_expired()
        try:
            return _SCANS[scan_id]
        except KeyError as exc:
            raise KeyError("Scan not found") from exc


def save_selection(selection: SourceSelectionRecord) -> None:
    with _LOCK:
        _prune_expired()
        _SELECTIONS[selection.summary.selection_id] = selection


def get_selection(selection_id: str) -> SourceSelectionRecord:
    with _LOCK:
        _prune_expired()
        try:
            return _SELECTIONS[selection_id]
        except KeyError as exc:
            raise KeyError("Selection not found") from exc


def clear_scans_for_tests() -> None:
    with _LOCK:
        _SCANS.clear()
        _SELECTIONS.clear()
