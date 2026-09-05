import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from dlss5_enabler.core.record import (
    InstallOptions,
    InstallRecord,
    OptiScalerStrategyOptions,
    RenoDxStrategyOptions,
    record_load,
    record_save,
)
from dlss5_enabler.schemas import migrations
from dlss5_enabler.schemas.migrations import CURRENT_RECORD_SCHEMA_VERSION, migrate_record
from dlss5_enabler.schemas.strategy import FrameGenerationMode, InstallStrategy, NrPlacement


def _legacy_record() -> dict[str, object]:
    return {
        "game_exe": "C:/Games/Title/game.exe",
        "game_dir": "C:/Games/Title",
        "tool_version": "1.0.0",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "lumenite_installed": False,
        "d3d9_translate": True,
        "vulkan_layer": True,
        "binaries": {
            "RenoDX": {
                "name": "RenoDX",
                "version": "renodx-dlss5-v1",
                "source_url": "https://example.test/renodx.addon64",
                "sha256": "a" * 64,
                "size_bytes": 32,
            }
        },
        "files": [{"path": "C:/Games/Title/dxgi.dll", "backup": "C:/Games/Title/dxgi.dll.bak"}],
    }


@pytest.mark.parametrize("version", [None, 1, 2])
def test_legacy_records_follow_migration_chain(version: int | None) -> None:
    source = _legacy_record()
    if version is not None:
        source["schema_version"] = version
    original = deepcopy(source)

    migrated = migrate_record(source)
    record = InstallRecord.model_validate(migrated)

    assert migrated["schema_version"] == CURRENT_RECORD_SCHEMA_VERSION
    assert migrated["strategy"] == "renodx"
    assert record.strategy is InstallStrategy.RENODX
    assert record.install_options == InstallOptions(lumenite=False, d3d9=True, vulkan_layer=True)
    assert isinstance(record.strategy_options, RenoDxStrategyOptions)
    assert record.strategy_options.as_install_options() == record.install_options
    assert record.tool_version == "1.0.0"
    assert record.timestamp == "2026-01-01T00:00:00+00:00"
    assert record.runtime_artifacts == []
    assert record.created_directories == []
    assert source == original
    assert migrated["binaries"] is not source["binaries"]
    assert migrated["files"] is not source["files"]
    assert migrated["binaries"] == original["binaries"]
    assert record.binaries["RenoDX"].source_revision == ""


@pytest.mark.parametrize("version", [1, 2])
def test_migration_preserves_requested_options_separately_from_installed_flags(version: int) -> None:
    source = _legacy_record()
    source["schema_version"] = version
    options = {"lumenite": True, "d3d9": False, "opengl": True, "vulkan_layer": False}
    source["install_options"] = options

    record = InstallRecord.model_validate(source)

    assert record.install_options == InstallOptions(**options)
    assert record.lumenite_installed is False
    assert record.d3d9_translate is True
    assert record.vulkan_layer is True


@pytest.mark.parametrize("version", [1, 2])
def test_migration_supports_legacy_null_options(version: int) -> None:
    source = _legacy_record()
    source.update(schema_version=version, install_options=None)

    assert InstallRecord.model_validate(source).install_options == InstallOptions(
        lumenite=False, d3d9=True, vulkan_layer=True
    )


def test_migration_is_idempotent_and_keeps_current_record_isolated() -> None:
    current = InstallRecord.model_validate(_legacy_record()).model_dump(mode="json")
    once = migrate_record(current)
    twice = migrate_record(once)

    assert current == once == twice
    assert twice is not once
    assert once["files"] is not current["files"]
    assert InstallRecord.model_validate(twice).model_dump(mode="json") == current


@pytest.mark.parametrize("version", [None, True, False, 0, -1, 1.0, "1", "2", [], {}])
def test_migration_rejects_malformed_explicit_versions(version: object) -> None:
    source = _legacy_record()
    source["schema_version"] = version

    with pytest.raises(ValueError, match="positive integer"):
        migrate_record(source)
    with pytest.raises(ValidationError, match="positive integer"):
        InstallRecord.model_validate(source)


def test_migration_rejects_future_schema_without_modifying_input() -> None:
    source = _legacy_record()
    source["schema_version"] = CURRENT_RECORD_SCHEMA_VERSION + 1
    original = deepcopy(source)

    with pytest.raises(ValueError, match="Unsupported install record schema"):
        migrate_record(source)

    assert source == original


def test_migration_rejects_missing_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(migrations._MIGRATIONS, 2)
    source = _legacy_record()
    original = deepcopy(source)

    with pytest.raises(ValueError, match="Missing install record migration from schema 2"):
        migrate_record(source)

    assert source == original


@pytest.mark.parametrize("next_version", [1, 3, True, "2", None])
def test_migration_rejects_transition_that_does_not_advance_one_version(
    next_version: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_migration(data: dict[str, object]) -> dict[str, object]:
        data["schema_version"] = next_version
        return data

    monkeypatch.setitem(migrations._MIGRATIONS, 1, broken_migration)
    source = _legacy_record()
    original = deepcopy(source)

    with pytest.raises(ValueError, match="Invalid install record migration from schema 1"):
        migrate_record(source)

    assert source == original


@pytest.mark.parametrize("strategy", ["auto", "optiscaler", "unknown", "", None])
def test_current_record_rejects_unresolved_or_inconsistent_strategy(strategy: object) -> None:
    source = migrate_record(_legacy_record())
    source["strategy"] = strategy

    with pytest.raises(ValidationError, match="strategy"):
        InstallRecord.model_validate(source)


def test_current_record_requires_explicit_strategy() -> None:
    source = migrate_record(_legacy_record())
    del source["strategy"]

    with pytest.raises(ValidationError, match="requires an explicit strategy"):
        InstallRecord.model_validate(source)


def test_current_record_does_not_reinterpret_null_options_as_legacy() -> None:
    source = migrate_record(_legacy_record())
    source["install_options"] = None

    with pytest.raises(ValidationError, match="install_options"):
        InstallRecord.model_validate(source)


def test_current_record_requires_explicit_install_options() -> None:
    source = migrate_record(_legacy_record())
    del source["install_options"]

    with pytest.raises(ValidationError, match="requires explicit install_options"):
        InstallRecord.model_validate(source)


@pytest.mark.parametrize("version", [1, 2, 3, CURRENT_RECORD_SCHEMA_VERSION])
@pytest.mark.parametrize("invalid_path", [None, False, True, 12, [], {"path": "C:/Games/Title"}])
def test_record_rejects_non_path_json_values(version: int, invalid_path: object) -> None:
    source = migrate_record(_legacy_record())
    if version < CURRENT_RECORD_SCHEMA_VERSION:
        del source["strategy_options"]
    source["schema_version"] = version
    source["game_dir"] = invalid_path

    with pytest.raises(ValidationError, match="Recorded paths must be strings"):
        InstallRecord.model_validate(source)


@pytest.mark.parametrize("version", [1, 2, 3, CURRENT_RECORD_SCHEMA_VERSION])
@pytest.mark.parametrize(
    "extra",
    [
        {"unknown_field": True},
        {"install_options": {"lumenite": True, "unknown_option": True}},
        {"binaries": {"RenoDX": {"name": "RenoDX", "unknown_identity": "unknown"}}},
        {"files": [{"path": "C:/Games/Title/dxgi.dll", "unknown_backup": "unknown"}]},
        {"ini_touched": [{"path": "C:/Games/Title/ReShade.ini", "section": "S", "key": "K", "other": 1}]},
        {"registry_touched": [{"reg_path": "C:/user.reg", "key": "K", "value_name": "V", "other": 1}]},
        {"runtime_artifacts": [{"directory": "C:/Games/Title", "pattern": "*.log", "other": 1}]},
    ],
)
def test_record_rejects_unknown_fields_in_all_supported_versions(version: int, extra: dict[str, object]) -> None:
    source = migrate_record(_legacy_record())
    if version < CURRENT_RECORD_SCHEMA_VERSION:
        del source["strategy_options"]
    source.update(schema_version=version, strategy="renodx")
    source.update(extra)

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        InstallRecord.model_validate(source)


def test_record_load_migrates_without_touching_disk_and_save_persists_current_schema(tmp_path: Path) -> None:
    record_path = tmp_path / "dlss5-enabler.install.json"
    original = (
        '{\r\n  "schema_version": 1, "game_exe": "'
        + (tmp_path / "game.exe").as_posix()
        + '", "game_dir": "'
        + tmp_path.as_posix()
        + '", "d3d9_translate": true\r\n}\r\n'
    ).encode("utf-8")
    record_path.write_bytes(original)

    record = record_load(tmp_path)

    assert record is not None
    assert record.schema_version == CURRENT_RECORD_SCHEMA_VERSION
    assert record_path.read_bytes() == original
    assert record_save(record)
    assert record_path.read_bytes() != original
    reloaded = record_load(tmp_path)
    assert reloaded == record


@pytest.mark.parametrize("schema_version", [True, None, "2", CURRENT_RECORD_SCHEMA_VERSION + 1])
def test_record_load_rejects_unsupported_data_without_touching_disk(tmp_path: Path, schema_version: object) -> None:
    data = _legacy_record()
    data["schema_version"] = schema_version
    record_path = tmp_path / "dlss5-enabler.install.json"
    original = json.dumps(data).encode("utf-8")
    record_path.write_bytes(original)

    assert record_load(tmp_path) is None
    assert record_path.read_bytes() == original


def test_schema_three_migrates_options_and_preserves_all_existing_identity_and_mutations() -> None:
    source = _legacy_record()
    source.update(
        schema_version=3,
        strategy="renodx",
        install_options={"lumenite": True, "d3d9": False, "opengl": True, "vulkan_layer": False},
        runtime_artifacts=[{"directory": "C:/Games/Title", "pattern": "feed-*.log", "preexisting": list[str]()}],
        created_directories=["C:/Games/Title/host64"],
    )
    original = deepcopy(source)

    migrated = migrate_record(source)
    record = InstallRecord.model_validate(migrated)

    assert source == original
    assert migrated == {
        **original,
        "schema_version": CURRENT_RECORD_SCHEMA_VERSION,
        "strategy_options": {"kind": "renodx", **record.install_options.model_dump()},
    }
    assert isinstance(record.strategy_options, RenoDxStrategyOptions)
    assert record.strategy_options.as_install_options() == record.install_options
    assert record.lumenite_installed is False
    assert record.d3d9_translate is True
    assert record.tool_version == "1.0.0"


def test_schema_three_record_load_does_not_write_disk(tmp_path: Path) -> None:
    source = _legacy_record()
    source.update(
        schema_version=3, strategy="renodx", install_options={"lumenite": False}, game_dir=tmp_path.as_posix()
    )
    path = tmp_path / "dlss5-enabler.install.json"
    original = json.dumps(source, indent=3).encode("utf-8") + b"\r\n"
    path.write_bytes(original)

    record = record_load(tmp_path)

    assert record is not None
    assert record.schema_version == CURRENT_RECORD_SCHEMA_VERSION
    assert record.strategy_options == RenoDxStrategyOptions(lumenite=False)
    assert record.install_options == InstallOptions(lumenite=False)
    assert path.read_bytes() == original


@pytest.mark.parametrize("strategy", ["optiscaler", "unknown", None])
def test_schema_three_cannot_claim_a_strategy_it_did_not_support(strategy: object) -> None:
    source = _legacy_record()
    source.update(schema_version=3, strategy=strategy, install_options={})

    with pytest.raises(ValueError, match="schema 3 only supports"):
        migrate_record(source)


def test_schema_three_rejects_future_strategy_options_instead_of_overwriting_them() -> None:
    source = _legacy_record()
    source.update(schema_version=3, strategy="renodx", install_options={}, strategy_options={"kind": "optiscaler"})
    original = deepcopy(source)

    with pytest.raises(ValueError, match="schema 3 cannot contain strategy_options"):
        migrate_record(source)

    assert source == original


def test_schema_four_requires_strategy_options() -> None:
    source = migrate_record(_legacy_record())
    del source["strategy_options"]

    with pytest.raises(ValidationError, match="requires explicit strategy_options"):
        InstallRecord.model_validate(source)


@pytest.mark.parametrize("options", [None, {}, {"kind": "auto"}, {"kind": "unknown"}])
def test_schema_four_rejects_missing_or_unresolved_options_discriminant(options: object) -> None:
    source = migrate_record(_legacy_record())
    source["strategy_options"] = options

    with pytest.raises(ValidationError, match="strategy_options"):
        InstallRecord.model_validate(source)


def test_schema_four_rejects_cross_strategy_options() -> None:
    source = migrate_record(_legacy_record())
    source["strategy_options"] = {
        "kind": "optiscaler",
        "proxy_name": "winmm.dll",
        "source_revision": "a" * 64,
    }

    with pytest.raises(ValidationError, match="strategy does not match"):
        InstallRecord.model_validate(source)


def test_schema_four_rejects_divergent_renodx_options() -> None:
    source = migrate_record(_legacy_record())
    source["strategy_options"] = {"kind": "renodx", "lumenite": True}

    with pytest.raises(ValidationError, match="must match legacy install_options"):
        InstallRecord.model_validate(source)


@pytest.mark.parametrize("passes", [0, 6, -1, True, False, "2", 1.5])
def test_optiscaler_passes_are_strict_and_bounded(passes: object) -> None:
    with pytest.raises(ValidationError, match="nr_passes"):
        OptiScalerStrategyOptions.model_validate(
            {"proxy_name": "dxgi.dll", "source_revision": "a" * 64, "nr_passes": passes}
        )


@pytest.mark.parametrize("passes", [1, 5])
def test_optiscaler_accepts_supported_pass_bounds(passes: int) -> None:
    options = OptiScalerStrategyOptions(proxy_name="winmm.dll", source_revision="b" * 64, nr_passes=passes)

    assert options.nr_passes == passes
    assert options.variant == "y4my4my4m-v3"


@pytest.mark.parametrize("revision", [None, "", " ", "\t", " hash", "hash\n"])
def test_optiscaler_requires_source_identity(revision: object) -> None:
    with pytest.raises(ValidationError, match="source_revision"):
        OptiScalerStrategyOptions.model_validate({"proxy_name": "dxgi.dll", "source_revision": revision})


@pytest.mark.parametrize(
    "proxy", ["dxgi.dll", "winmm.dll", "d3d12.dll", "dbghelp.dll", "version.dll", "wininet.dll", "winhttp.dll"]
)
def test_optiscaler_accepts_concrete_supported_proxy(proxy: str) -> None:
    assert OptiScalerStrategyOptions(proxy_name=proxy, source_revision="a" * 64).proxy_name == proxy


@pytest.mark.parametrize(
    "proxy", ["auto", "../dxgi.dll", "C:/game/dxgi.dll", "bin\\dxgi.dll", "custom.dll", "", "DXGI.DLL"]
)
def test_optiscaler_rejects_unresolved_or_unsafe_proxy(proxy: str) -> None:
    with pytest.raises(ValidationError, match="proxy filename"):
        OptiScalerStrategyOptions(proxy_name=proxy, source_revision="a" * 64)


def test_optiscaler_rejects_unknown_variant_and_renodx_only_options() -> None:
    payload: dict[str, object] = {"proxy_name": "dxgi.dll", "source_revision": "a" * 64, "variant": "official"}
    with pytest.raises(ValidationError, match="variant"):
        OptiScalerStrategyOptions.model_validate(payload)
    payload.pop("variant")
    payload["lumenite"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        OptiScalerStrategyOptions.model_validate(payload)


def test_optiscaler_record_round_trip_keeps_source_variant_and_specific_options(tmp_path: Path) -> None:
    options = OptiScalerStrategyOptions(proxy_name="winmm.dll", nr_passes=3, source_revision="c" * 64)
    record = InstallRecord(
        schema_version=CURRENT_RECORD_SCHEMA_VERSION,
        strategy=InstallStrategy.OPTISCALER,
        strategy_options=options,
        game_exe=(tmp_path / "game.exe").as_posix(),
        game_dir=tmp_path.as_posix(),
    )

    assert record_save(record)
    loaded = record_load(tmp_path)

    assert loaded is not None
    assert loaded.strategy is InstallStrategy.OPTISCALER
    assert loaded.strategy_options == options
    assert isinstance(loaded.strategy_options, OptiScalerStrategyOptions)
    assert loaded.install_options == InstallOptions()
    assert loaded == record
    serialized = record.model_dump(mode="json")
    assert migrate_record(serialized) == serialized


def test_schema_four_optiscaler_migrates_new_options_without_changing_old_behavior(tmp_path: Path) -> None:
    record = InstallRecord(
        schema_version=CURRENT_RECORD_SCHEMA_VERSION,
        strategy=InstallStrategy.OPTISCALER,
        strategy_options=OptiScalerStrategyOptions(proxy_name="dxgi.dll", source_revision="d" * 64),
        game_exe=(tmp_path / "game.exe").as_posix(),
        game_dir=tmp_path.as_posix(),
    )
    source = record.model_dump(mode="json")
    source["schema_version"] = 4
    options = source["strategy_options"]
    assert isinstance(options, dict)
    for key in ("frame_generation", "fg_multiplier", "nr_placement", "gpu_generation"):
        options.pop(key)

    migrated = InstallRecord.model_validate(migrate_record(source))

    assert isinstance(migrated.strategy_options, OptiScalerStrategyOptions)
    assert migrated.strategy_options.frame_generation is FrameGenerationMode.OFF
    assert migrated.strategy_options.fg_multiplier == 2
    assert migrated.strategy_options.nr_placement is NrPlacement.AFTER
    assert migrated.strategy_options.gpu_generation == "unknown"


def test_optiscaler_record_rejects_auto_and_non_dlssg_multiplier() -> None:
    with pytest.raises(ValidationError, match="must be concrete"):
        OptiScalerStrategyOptions(
            proxy_name="dxgi.dll",
            source_revision="a" * 64,
            frame_generation=FrameGenerationMode.AUTO,
        )
    with pytest.raises(ValidationError, match="Only DLSSG"):
        OptiScalerStrategyOptions(
            proxy_name="dxgi.dll",
            source_revision="a" * 64,
            frame_generation=FrameGenerationMode.FSR,
            fg_multiplier=3,
        )


def test_optiscaler_creation_requires_explicit_current_schema() -> None:
    with pytest.raises(ValidationError, match="schema 3 only supports"):
        InstallRecord(
            strategy=InstallStrategy.OPTISCALER,
            strategy_options=OptiScalerStrategyOptions(proxy_name="dxgi.dll", source_revision="a" * 64),
            game_exe="C:/Games/Title/game.exe",
            game_dir="C:/Games/Title",
        )


def test_renodx_compatibility_factory_preserves_exact_requested_options() -> None:
    legacy = InstallOptions(lumenite=False, d3d9=True, opengl=False, vulkan_layer=True)
    strategy = RenoDxStrategyOptions.from_install_options(legacy)

    assert strategy.as_install_options() == legacy
    assert strategy.kind == "renodx"


def test_save_rejects_in_memory_strategy_option_divergence_without_overwriting(tmp_path: Path) -> None:
    record = InstallRecord(game_exe=(tmp_path / "game.exe").as_posix(), game_dir=tmp_path.as_posix())
    assert record_save(record)
    original = record.record_path().read_bytes()
    record.install_options = InstallOptions(lumenite=False)

    assert not record_save(record)
    assert record.record_path().read_bytes() == original
    record.strategy_options = RenoDxStrategyOptions.from_install_options(record.install_options)
    assert record_save(record)
    assert record_load(tmp_path) == record
