import os
import time
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict

from dlss5_enabler.core.fileio import atomic_write_bytes, resource_lock
from dlss5_enabler.core.util import get_cache_dir
from dlss5_enabler.core.version import get_tool_version, parse_tool_version

PYPI_PROJECT_URL = "https://pypi.org/pypi/dlss5-enabler/json"
UPDATE_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
FUTURE_CLOCK_TOLERANCE_SECONDS = 5 * 60
UPDATE_CHECK_TIMEOUT_SECONDS = 4.0
UPDATE_CHECK_LOCK_TIMEOUT_SECONDS = 0.5


class _PyPIInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: str


class _PyPIResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    info: _PyPIInfo


class UpdateCheckResult(BaseModel):
    current_version: str
    latest_version: str | None = None
    check_performed: bool = False
    update_available: bool = False
    error: str = ""


def get_update_check_marker() -> Path:
    return get_cache_dir() / "update-check.lock"


def marker_is_due(marker: Path, now: float) -> bool:
    if not marker.is_file():
        return True
    if marker.stat().st_size != 0:
        return True
    modified = marker.stat().st_mtime
    if modified > now + FUTURE_CLOCK_TOLERANCE_SECONDS:
        return True
    return now - modified >= UPDATE_CHECK_INTERVAL_SECONDS


def fetch_latest_version() -> str:
    timeout = httpx.Timeout(UPDATE_CHECK_TIMEOUT_SECONDS)
    headers = {"Accept": "application/json", "User-Agent": "dlss5-enabler-update-check"}
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        response = client.get(PYPI_PROJECT_URL)
        response.raise_for_status()
        payload = _PyPIResponse.model_validate(response.json())
    parsed = parse_tool_version(payload.info.version)
    if parsed is None:
        raise ValueError("PyPI returned an invalid project version")
    return str(parsed)


def _refresh_marker(marker: Path, completed_at: float) -> None:
    atomic_write_bytes(marker, b"")
    os.utime(marker, (completed_at, completed_at))


def check_for_update(
    *,
    force: bool = False,
    marker: Path | None = None,
    now: float | None = None,
) -> UpdateCheckResult:
    current_text = get_tool_version()
    marker_path = marker or get_update_check_marker()
    checked_at = time.time() if now is None else now
    try:
        with resource_lock(marker_path, timeout=UPDATE_CHECK_LOCK_TIMEOUT_SECONDS):
            if not force and not marker_is_due(marker_path, checked_at):
                return UpdateCheckResult(current_version=current_text)
            try:
                latest_text = fetch_latest_version()
                current = parse_tool_version(current_text)
                latest = parse_tool_version(latest_text)
                available = bool(
                    current is not None
                    and latest is not None
                    and latest > current
                    and (current.is_prerelease or not latest.is_prerelease)
                )
                result = UpdateCheckResult(
                    current_version=current_text,
                    latest_version=latest_text,
                    check_performed=True,
                    update_available=available,
                )
            except Exception as error:
                result = UpdateCheckResult(
                    current_version=current_text,
                    check_performed=True,
                    error=str(error),
                )
            try:
                completed_at = checked_at if now is not None else time.time()
                _refresh_marker(marker_path, completed_at)
            except Exception as error:
                if not result.error:
                    result.error = f"Could not refresh update-check marker: {error}"
            return result
    except Exception as error:
        return UpdateCheckResult(current_version=current_text, error=str(error))
