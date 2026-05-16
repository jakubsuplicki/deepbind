from __future__ import annotations

import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from services.source_import.models import SourceScanReport


SCAN_TTL = timedelta(minutes=30)


_LOCK = threading.Lock()
_SCANS: dict[str, SourceScanReport] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _prune_expired(now: Optional[datetime] = None) -> None:
    now = now or _now()
    expired: list[str] = []
    for scan_id, report in _SCANS.items():
        created = _parse_iso(report.created_at)
        if created + SCAN_TTL <= now:
            expired.append(scan_id)
    for scan_id in expired:
        _SCANS.pop(scan_id, None)


def new_scan_id() -> str:
    return f"scan_{secrets.token_urlsafe(12)}"


def save_scan(report: SourceScanReport) -> None:
    with _LOCK:
        _prune_expired()
        _SCANS[report.scan_id] = report


def get_scan(scan_id: str) -> SourceScanReport:
    with _LOCK:
        _prune_expired()
        try:
            return _SCANS[scan_id]
        except KeyError as exc:
            raise KeyError("Scan not found") from exc


def clear_scans_for_tests() -> None:
    with _LOCK:
        _SCANS.clear()
