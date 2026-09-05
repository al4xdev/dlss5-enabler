import re
import subprocess
from dataclasses import dataclass
from enum import Enum


class NvidiaGpuGeneration(str, Enum):
    RTX40 = "rtx40"
    RTX50 = "rtx50"
    OLDER = "older"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class NvidiaGpuInfo:
    name: str | None
    generation: NvidiaGpuGeneration


_NVIDIA_SMI_TIMEOUT_SECONDS = 3.0
_GEFORCE_RTX_40_PATTERN = re.compile(r"\bGEFORCE\s+RTX\s+40\d{2}\b", re.IGNORECASE)
_GEFORCE_RTX_50_PATTERN = re.compile(r"\bGEFORCE\s+RTX\s+50\d{2}\b", re.IGNORECASE)
_OLDER_GEFORCE_PATTERN = re.compile(r"\bGEFORCE\s+(?:GTX\s+\d+|RTX\s+(?:20|30)\d{2})\b", re.IGNORECASE)


def _classify_gpu_name(name: str) -> NvidiaGpuGeneration:
    if re.search(r"\bBLACKWELL\b", name, re.IGNORECASE) or _GEFORCE_RTX_50_PATTERN.search(name):
        return NvidiaGpuGeneration.RTX50
    if re.search(r"\bADA(?:\s+GENERATION)?\b", name, re.IGNORECASE) or _GEFORCE_RTX_40_PATTERN.search(name):
        return NvidiaGpuGeneration.RTX40
    if _OLDER_GEFORCE_PATTERN.search(name):
        return NvidiaGpuGeneration.OLDER
    return NvidiaGpuGeneration.UNKNOWN


def detect_nvidia_gpu_generation() -> NvidiaGpuInfo:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader,nounits"],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            text=True,
            timeout=_NVIDIA_SMI_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return NvidiaGpuInfo(name=None, generation=NvidiaGpuGeneration.UNKNOWN)
    if result.returncode != 0:
        return NvidiaGpuInfo(name=None, generation=NvidiaGpuGeneration.UNKNOWN)
    names = tuple(line.strip().strip('"') for line in result.stdout.splitlines() if line.strip())
    if not names:
        return NvidiaGpuInfo(name=None, generation=NvidiaGpuGeneration.UNKNOWN)
    generations = {_classify_gpu_name(name) for name in names}
    if len(generations) != 1:
        return NvidiaGpuInfo(name="; ".join(names), generation=NvidiaGpuGeneration.UNKNOWN)
    return NvidiaGpuInfo(name="; ".join(names), generation=generations.pop())
