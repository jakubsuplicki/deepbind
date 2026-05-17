from __future__ import annotations

import hmac
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from services.source_import.archives import ZIP_EXTENSION


GRANT_TTL = timedelta(minutes=10)
SOURCE_KINDS = {"local_folder", "local_archive"}


class SourceGrantError(ValueError):
    pass


@dataclass(frozen=True)
class SourceGrant:
    token: str
    root_path: Path
    display_name: str
    source_kind: str
    expires_at: datetime


_LOCK = threading.Lock()
_GRANTS: dict[str, SourceGrant] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _prune_expired(now: Optional[datetime] = None) -> None:
    now = now or _now()
    expired = [token for token, grant in _GRANTS.items() if grant.expires_at <= now]
    for token in expired:
        _GRANTS.pop(token, None)


def create_grant(path: str, *, source_kind: str = "local_folder") -> SourceGrant:
    if source_kind not in SOURCE_KINDS:
        raise SourceGrantError("Unsupported source kind")

    root = Path(path).expanduser()
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise SourceGrantError("Selected source does not exist") from exc

    if source_kind == "local_folder":
        if not resolved.is_dir():
            raise SourceGrantError("Selected source is not a folder")
    elif source_kind == "local_archive":
        if not resolved.is_file():
            raise SourceGrantError("Selected source is not a file")
        if resolved.suffix.lower() != ZIP_EXTENSION:
            raise SourceGrantError("Selected source is not a ZIP archive")

    token = secrets.token_urlsafe(32)
    grant = SourceGrant(
        token=token,
        root_path=resolved,
        display_name=resolved.name or str(resolved),
        source_kind=source_kind,
        expires_at=_now() + GRANT_TTL,
    )
    with _LOCK:
        _prune_expired()
        _GRANTS[token] = grant
    return grant


def consume_grant(token: str) -> SourceGrant:
    with _LOCK:
        _prune_expired()
        matched: Optional[str] = None
        for stored in _GRANTS:
            if hmac.compare_digest(stored, token):
                matched = stored
                break
        if not matched:
            raise SourceGrantError("Source grant is invalid or expired")
        grant = _GRANTS.pop(matched)
    return grant


def clear_grants_for_tests() -> None:
    with _LOCK:
        _GRANTS.clear()
