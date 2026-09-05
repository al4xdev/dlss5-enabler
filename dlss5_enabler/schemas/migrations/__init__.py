from collections.abc import Callable
from copy import deepcopy

from dlss5_enabler.schemas.migrations.v1_to_v2 import migrate as migrate_v1_to_v2
from dlss5_enabler.schemas.migrations.v2_to_v3 import migrate as migrate_v2_to_v3

CURRENT_RECORD_SCHEMA_VERSION = 3

RecordMigration = Callable[[dict[str, object]], dict[str, object]]

_MIGRATIONS: dict[int, RecordMigration] = {
    1: migrate_v1_to_v2,
    2: migrate_v2_to_v3,
}


def migrate_record(data: dict[str, object]) -> dict[str, object]:
    version = data.get("schema_version", 1)
    if type(version) is not int or version < 1:
        raise ValueError("Install record schema_version must be a positive integer")
    if version > CURRENT_RECORD_SCHEMA_VERSION:
        raise ValueError(f"Unsupported install record schema version: {version}")
    migrated = deepcopy(data)
    for source_version in range(version, CURRENT_RECORD_SCHEMA_VERSION):
        migrate = _MIGRATIONS.get(source_version)
        if migrate is None:
            raise ValueError(f"Missing install record migration from schema {source_version}")
        migrated = migrate(migrated)
        next_version = migrated.get("schema_version")
        if type(next_version) is not int or next_version != source_version + 1:
            raise ValueError(f"Invalid install record migration from schema {source_version}")
    return migrated
