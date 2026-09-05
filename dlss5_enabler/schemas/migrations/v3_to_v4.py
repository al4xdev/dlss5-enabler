from copy import deepcopy
from typing import cast


def migrate(data: dict[str, object]) -> dict[str, object]:
    migrated = deepcopy(data)
    if migrated.get("strategy") != "renodx":
        raise ValueError("Install record schema 3 only supports the renodx strategy")
    options = migrated.get("install_options")
    if "strategy_options" in migrated:
        raise ValueError("Install record schema 3 cannot contain strategy_options")
    if isinstance(options, dict):
        migrated["strategy_options"] = {"kind": "renodx", **cast(dict[str, object], options)}
        migrated["schema_version"] = 4
        return migrated
    raise ValueError("Install record schema 3 requires explicit install_options")
