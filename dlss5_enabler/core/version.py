from enum import Enum
from importlib.metadata import PackageNotFoundError, version

from packaging.version import InvalidVersion, Version

UNKNOWN_VERSION = "0+unknown"


class InstallVersionStatus(str, Enum):
    CURRENT = "Current"
    UPDATE_AVAILABLE = "Update available"
    NEWER_THAN_CLI = "Newer than this CLI"
    UNKNOWN_LEGACY = "Unknown legacy version"


def get_tool_version() -> str:
    try:
        return version("dlss5-enabler")
    except PackageNotFoundError:
        return UNKNOWN_VERSION


def parse_tool_version(value: str) -> Version | None:
    if value == UNKNOWN_VERSION:
        return None
    try:
        return Version(value)
    except InvalidVersion:
        return None


def get_install_version_status(
    installed_version: str,
    current_version: str | None = None,
) -> InstallVersionStatus:
    installed = parse_tool_version(installed_version)
    current = parse_tool_version(current_version or get_tool_version())
    if installed is None or current is None:
        return InstallVersionStatus.UNKNOWN_LEGACY
    if installed < current:
        return InstallVersionStatus.UPDATE_AVAILABLE
    if installed > current:
        return InstallVersionStatus.NEWER_THAN_CLI
    return InstallVersionStatus.CURRENT
