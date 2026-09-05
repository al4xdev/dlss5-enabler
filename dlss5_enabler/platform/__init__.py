import sys

from dlss5_enabler.platform.base import PlatformAdapter
from dlss5_enabler.platform.gpu import NvidiaGpuGeneration, NvidiaGpuInfo, detect_nvidia_gpu_generation
from dlss5_enabler.platform.linux import LinuxAdapter
from dlss5_enabler.platform.proton import ProtonManager, SteamPrefixInfo, WineRegParser
from dlss5_enabler.platform.windows import WindowsAdapter

__all__ = [
    "LinuxAdapter",
    "NvidiaGpuGeneration",
    "NvidiaGpuInfo",
    "PlatformAdapter",
    "ProtonManager",
    "SteamPrefixInfo",
    "WindowsAdapter",
    "WineRegParser",
    "detect_nvidia_gpu_generation",
    "get_platform_adapter",
]


class _PlatformState:
    adapter: PlatformAdapter | None = None


def get_platform_adapter(force_platform: str | None = None) -> PlatformAdapter:
    if force_platform:
        if force_platform.lower() == "windows":
            return WindowsAdapter()
        return LinuxAdapter()

    if _PlatformState.adapter is None:
        if sys.platform == "win32":
            _PlatformState.adapter = WindowsAdapter()
        else:
            _PlatformState.adapter = LinuxAdapter()

    return _PlatformState.adapter
