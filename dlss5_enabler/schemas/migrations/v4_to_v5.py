from copy import deepcopy
from typing import cast


def migrate(data: dict[str, object]) -> dict[str, object]:
    migrated = deepcopy(data)
    options = migrated.get("strategy_options")
    if not isinstance(options, dict):
        raise TypeError("Install record schema 4 requires explicit strategy_options")
    typed = cast(dict[str, object], options)
    if typed.get("kind") == "optiscaler":
        typed["frame_generation"] = "off"
        typed["fg_multiplier"] = 2
        typed["nr_placement"] = "after"
        typed["gpu_generation"] = "unknown"
    migrated["schema_version"] = 5
    return migrated
