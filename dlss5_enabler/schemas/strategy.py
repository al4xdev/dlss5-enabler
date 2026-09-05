from enum import Enum


class InstallStrategy(str, Enum):
    RENODX = "renodx"
    OPTISCALER = "optiscaler"
