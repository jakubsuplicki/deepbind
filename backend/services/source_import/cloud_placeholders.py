from __future__ import annotations

import errno
import os
import stat as stat_module
from pathlib import Path
from typing import Optional


ONLINE_ONLY_PLACEHOLDER_REASON = "online_only_placeholder"

_EXPLICIT_PLACEHOLDER_MARKERS = (
    "cloudfile",
    "dataless",
    "notdownloaded",
    "onlineonly",
    "placeholder",
    "recallondataccess",
    "recallondataaccess",
    "recallonopen",
)

_CLOUD_XATTR_PREFIXES = (
    "com.apple.fileprovider",
    "com.apple.icloud",
    "com.dropbox",
    "com.google.drive",
    "com.microsoft",
    "com.microsoft.onedrive",
)

_READ_ERROR_MARKERS = (
    "cloud file provider",
    "cloud operation",
    "file provider",
    "not downloaded",
    "online-only",
    "online only",
    "placeholder",
)


def filename_indicates_cloud_placeholder(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(".icloud")


def display_extension_for_cloud_placeholder(path: Path) -> str:
    name = path.name
    if name.lower().endswith(".icloud"):
        without_marker = name[: -len(".icloud")]
        extension = Path(without_marker).suffix.lower()
        return extension or "(none)"
    return path.suffix.lower() or "(none)"


def detect_online_only_placeholder(
    path: Path,
    *,
    stat_result: Optional[os.stat_result] = None,
) -> Optional[str]:
    if filename_indicates_cloud_placeholder(path):
        return ONLINE_ONLY_PLACEHOLDER_REASON
    if stat_result is not None and _stat_indicates_windows_placeholder(stat_result):
        return ONLINE_ONLY_PLACEHOLDER_REASON
    if _xattrs_indicate_cloud_placeholder(path):
        return ONLINE_ONLY_PLACEHOLDER_REASON
    return None


def classify_read_error_reason(exc: BaseException) -> str:
    text = str(exc).lower()
    if any(marker in text for marker in _READ_ERROR_MARKERS):
        return ONLINE_ONLY_PLACEHOLDER_REASON
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, OSError) and exc.errno in {errno.EPERM, errno.EACCES}:
        return "permission_denied"
    return "unreadable_file"


def _normalise_marker(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _contains_placeholder_marker(text: str) -> bool:
    normalised = _normalise_marker(text)
    return any(marker in normalised for marker in _EXPLICIT_PLACEHOLDER_MARKERS)


def _stat_indicates_windows_placeholder(stat_result: os.stat_result) -> bool:
    attrs = int(getattr(stat_result, "st_file_attributes", 0) or 0)
    if not attrs:
        return False
    for flag_name in (
        "FILE_ATTRIBUTE_OFFLINE",
        "FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS",
        "FILE_ATTRIBUTE_RECALL_ON_OPEN",
    ):
        flag = int(getattr(stat_module, flag_name, 0) or 0)
        if flag and attrs & flag:
            return True
    return False


def _xattrs_indicate_cloud_placeholder(path: Path) -> bool:
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except (AttributeError, OSError):
        return False

    for raw_name in names:
        name = _decode_xattr(raw_name)
        normalised_name = _normalise_marker(name)
        is_cloud_xattr = name.lower().startswith(_CLOUD_XATTR_PREFIXES)
        if is_cloud_xattr and (
            _contains_placeholder_marker(name) or normalised_name.endswith("fpfsp")
        ):
            return True
        if not is_cloud_xattr:
            continue
        try:
            value = os.getxattr(path, raw_name, follow_symlinks=False)
        except (AttributeError, OSError):
            continue
        if _contains_placeholder_marker(_decode_xattr(value)):
            return True
    return False


def _decode_xattr(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value)
