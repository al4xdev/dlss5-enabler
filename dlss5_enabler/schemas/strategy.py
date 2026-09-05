from enum import Enum
from typing import Literal

GpuGeneration = Literal["rtx40", "rtx50", "older", "unknown"]


class InstallStrategy(str, Enum):
    RENODX = "renodx"
    OPTISCALER = "optiscaler"


class FrameGenerationMode(str, Enum):
    AUTO = "auto"
    OFF = "off"
    FSR = "fsr"
    DLSSG = "dlssg"


class NrPlacement(str, Enum):
    AFTER = "after"
    BEFORE = "before"
    INSIDE = "inside"
