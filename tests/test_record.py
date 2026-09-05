from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from dlss5_enabler.core.record import (
    CURRENT_RECORD_SCHEMA_VERSION,
    BinaryInfo,
    IndexEntry,
    IndexEntrySnapshot,
    IniTouch,
    InstallOptions,
    InstallRecord,
    RecordedFile,
    RegistryTouch,
    RuntimeArtifacts,
    capture_index_entry,
    index_add,
    index_load,
    index_load_active,
    index_remove,
    index_save,
    record_exists,
    record_load,
    record_save,
    restore_index_entry,
)
from dlss5_enabler.core.version import get_tool_version
from dlss5_enabler.schemas.strategy import InstallStrategy


def test_models_instantiation() -> None:
    rec_file = RecordedFile(path="C:/game/d3d11.dll", backup="C:/game/d3d11.dll.bak", size_bytes=1024, sha256="abc")
    assert rec_file.path == "C:/game/d3d11.dll"
    assert rec_file.size_bytes == 1024

    touch = IniTouch(path="C:/game/ReShade.ini", section="GENERAL", key="EffectSearchPaths", original=".\\Shaders")
    assert touch.key == "EffectSearchPaths"

    bin_info = BinaryInfo(
        name="ReShade", version="6.8.0", sha256="123", size_bytes=2048, source_url="http://reshade.me"
    )
    assert bin_info.version == "6.8.0"


def test_record_paths_are_canonical_posix() -> None:
    rec = InstallRecord(
        game_exe=r"C:\Games\Title\game.exe",
        game_dir=r"C:\Games\Title",
        files=[RecordedFile(path=r"C:\Games\Title\dxgi.dll", backup=r"C:\Games\Title\dxgi.dll.bak")],
    )

    assert rec.game_exe == "C:/Games/Title/game.exe"
    assert rec.game_dir == "C:/Games/Title"
    assert rec.files[0].path == "C:/Games/Title/dxgi.dll"


@pytest.mark.parametrize(
    "model, fields, path_field",
    [
        (RecordedFile, {"path": "C:/Games/Title/dxgi.dll"}, "backup"),
        (IniTouch, {"path": "C:/Games/Title/ReShade.ini", "section": "S", "key": "K"}, "path"),
        (RegistryTouch, {"reg_path": "C:/user.reg", "key": "K", "value_name": "V"}, "reg_path"),
        (RuntimeArtifacts, {"directory": "C:/Games/Title", "pattern": "*.log"}, "directory"),
        (
            IndexEntry,
            {"game_exe": "C:/Games/Title/game.exe", "game_dir": "C:/Games/Title", "timestamp": "t"},
            "game_dir",
        ),
    ],
)
def test_nested_records_reject_non_path_json_values(
    model: type[RecordedFile | IniTouch | RegistryTouch | RuntimeArtifacts | IndexEntry],
    fields: dict[str, object],
    path_field: str,
) -> None:
    invalid = dict(fields)
    invalid[path_field] = {"invalid": "path"}

    with pytest.raises(ValidationError, match="Recorded paths must be strings"):
        model.model_validate(invalid)


def test_runtime_ownership_paths_are_canonical_and_independent() -> None:
    rec = InstallRecord(
        game_exe="C:/Games/Title/game.exe",
        game_dir="C:/Games/Title",
        created_directories=[r"C:\Games\Title\reshade-shaders"],
        runtime_artifacts=[
            RuntimeArtifacts(
                directory=r"C:\Games\Title",
                pattern="*.log",
                preexisting=[r"C:\Games\Title\ReShade.log"],
            )
        ],
    )
    empty = InstallRecord(game_exe="C:/Other/game.exe", game_dir="C:/Other")

    assert rec.created_directories == ["C:/Games/Title/reshade-shaders"]
    assert rec.runtime_artifacts[0].directory == "C:/Games/Title"
    assert rec.runtime_artifacts[0].preexisting == ["C:/Games/Title/ReShade.log"]
    assert empty.runtime_artifacts == []
    assert empty.created_directories == []


def test_binary_identity_survives_serialization_without_inventing_missing_metadata() -> None:
    revision = "a" * 40
    identity = BinaryInfo(
        name="RenoDX",
        version="renodx-dlss5-v1",
        source_url="https://example.test/renodx.addon64",
        source_revision=revision,
        sha256="b" * 64,
        size_bytes=1234,
    )
    record = InstallRecord(game_exe="C:/Games/Title/game.exe", game_dir="C:/Games/Title", binaries={"RenoDX": identity})

    restored = InstallRecord.model_validate_json(record.model_dump_json())

    assert restored.strategy is InstallStrategy.RENODX
    assert restored.binaries["RenoDX"] == identity
    assert BinaryInfo(name="legacy").source_revision == ""


def test_install_record_methods(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    game_exe = game_dir / "game.exe"
    rec = InstallRecord(game_exe=str(game_exe), game_dir=str(game_dir), reshade_dir="")

    assert rec.record_path() == game_dir / "dlss5-enabler.install.json"
    assert rec.effective_reshade_dir() == game_dir

    rec.reshade_dir = str(game_dir / "bin")
    assert rec.effective_reshade_dir() == game_dir / "bin"


def test_record_save_load_exists(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir(parents=True, exist_ok=True)
    game_exe = game_dir / "game.exe"

    rec = InstallRecord(
        game_exe=str(game_exe),
        game_dir=str(game_dir),
        architecture="x64",
        install_type="D3D11/D3D12",
        files=[RecordedFile(path=str(game_dir / "dxgi.dll"), size_bytes=100)],
        binaries={"dxgi": BinaryInfo(name="dxgi", version="1.0")},
    )

    assert not record_exists(game_dir)
    assert record_load(game_dir) is None

    assert record_save(rec)
    assert record_exists(game_dir)

    loaded = record_load(game_dir)
    assert loaded is not None
    assert loaded.game_exe == game_exe.as_posix()
    assert loaded.architecture == "x64"
    assert len(loaded.files) == 1
    assert loaded.files[0].path == (game_dir / "dxgi.dll").as_posix()
    assert "dxgi" in loaded.binaries
    assert loaded.schema_version == CURRENT_RECORD_SCHEMA_VERSION
    assert loaded.strategy is InstallStrategy.RENODX
    assert loaded.tool_version == get_tool_version()
    assert loaded.install_options == InstallOptions()


def test_record_load_migrates_legacy_options_in_memory(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    record_path = game_dir / "dlss5-enabler.install.json"
    original = (
        '{"tool_version":"1.0.0","game_exe":"game.exe","game_dir":"'
        + game_dir.as_posix()
        + '","lumenite_installed":false,"d3d9_translate":true,"opengl":false,"vulkan_layer":true}'
    )
    record_path.write_text(original, encoding="utf-8")

    loaded = record_load(game_dir)

    assert loaded is not None
    assert loaded.schema_version == CURRENT_RECORD_SCHEMA_VERSION
    assert loaded.strategy is InstallStrategy.RENODX
    assert loaded.tool_version == "1.0.0"
    assert loaded.install_options == InstallOptions(lumenite=False, d3d9=True, opengl=False, vulkan_layer=True)
    assert record_path.read_text(encoding="utf-8") == original


def test_record_load_corrupted_json(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir(parents=True, exist_ok=True)
    bad_json = game_dir / "dlss5-enabler.install.json"
    bad_json.write_text("{corrupted json", encoding="utf-8")

    assert record_load(game_dir) is None


def test_record_save_failure(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    rec = InstallRecord(game_exe=str(game_dir / "g.exe"), game_dir=str(game_dir))

    with patch("dlss5_enabler.core.record.atomic_write_text", side_effect=OSError("Permission denied")):
        assert not record_save(rec)


@pytest.mark.parametrize("invalid", [{"schema_version": CURRENT_RECORD_SCHEMA_VERSION + 1}, {"game_dir": None}])
def test_record_save_revalidates_mutated_models_before_touching_disk(
    tmp_path: Path, invalid: dict[str, object]
) -> None:
    record = InstallRecord(game_exe=(tmp_path / "game.exe").as_posix(), game_dir=tmp_path.as_posix())
    assert record_save(record)
    original = record.record_path().read_bytes()
    corrupted = record.model_copy(update=invalid)

    assert not record_save(corrupted)
    assert record.record_path().read_bytes() == original


def test_global_index_operations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_index = tmp_path / "appdata" / "installs.json"
    fake_index.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("dlss5_enabler.core.record.get_global_index_path", lambda: fake_index)

    assert index_load() == []

    rec1 = InstallRecord(game_exe="C:/g1/g1.exe", game_dir="C:/g1", architecture="x64", install_type="D3D12")
    rec2 = InstallRecord(game_exe="C:/g2/g2.exe", game_dir="C:/g2", architecture="x86", install_type="D3D9")

    assert index_add(rec1)
    entries = index_load()
    assert len(entries) == 1
    assert entries[0].game_exe == "C:/g1/g1.exe"
    assert entries[0].schema_version == CURRENT_RECORD_SCHEMA_VERSION
    assert entries[0].tool_version == rec1.tool_version

    assert index_add(rec2)
    entries = index_load()
    assert len(entries) == 2

    # Update existing record
    rec1_updated = InstallRecord(game_exe="C:/g1/g1.exe", game_dir="C:/g1", architecture="x64", install_type="OpenGL")
    assert index_add(rec1_updated)
    entries = index_load()
    assert len(entries) == 2
    g1_entry = next(e for e in entries if e.game_dir == "C:/g1")
    assert g1_entry.install_type == "OpenGL"

    # Remove entry
    assert index_remove("C:/g1")
    entries = index_load()
    assert len(entries) == 1
    assert entries[0].game_dir == "C:/g2"

    # Remove non-existent returns True
    assert index_remove("C:/nonexistent")
    assert len(index_load()) == 1


def test_index_load_active_prunes_stale_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_index = tmp_path / "appdata" / "installs.json"
    fake_index.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("dlss5_enabler.core.record.get_global_index_path", lambda: fake_index)
    monkeypatch.setattr("dlss5_enabler.core.record.gettempdir", lambda: str(tmp_path / "unrelated-temp"))
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    game_exe = game_dir / "game.exe"
    game_exe.write_bytes(b"MZ")
    active_record = InstallRecord(game_exe=str(game_exe), game_dir=str(game_dir))
    assert record_save(active_record)
    active_entry = IndexEntry(
        game_exe=active_record.game_exe,
        game_dir=active_record.game_dir,
        timestamp=active_record.timestamp,
    )
    stale_entry = IndexEntry(
        game_exe=str(tmp_path / "missing" / "game.exe"),
        game_dir=str(tmp_path / "missing"),
        timestamp="2026-01-01T00:00:00+00:00",
    )
    assert index_save([active_entry, stale_entry])

    active = index_load_active()

    assert [entry.game_exe for entry in active] == [active_entry.game_exe]
    assert [entry.game_exe for entry in index_load()] == [active_entry.game_exe]


def test_index_load_active_prunes_temporary_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_index = tmp_path / "appdata" / "installs.json"
    fake_index.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("dlss5_enabler.core.record.get_global_index_path", lambda: fake_index)
    monkeypatch.setattr("dlss5_enabler.core.record.gettempdir", lambda: str(tmp_path))
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    game_exe = game_dir / "game.exe"
    game_exe.write_bytes(b"MZ")
    record = InstallRecord(game_exe=str(game_exe), game_dir=str(game_dir))
    assert record_save(record)
    assert index_save([IndexEntry(game_exe=record.game_exe, game_dir=record.game_dir, timestamp=record.timestamp)])

    assert index_load_active() == []
    assert index_load() == []


def test_index_load_corrupted_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_index = tmp_path / "appdata" / "installs.json"
    fake_index.parent.mkdir(parents=True, exist_ok=True)
    fake_index.write_text("invalid json", encoding="utf-8")
    monkeypatch.setattr("dlss5_enabler.core.record.get_global_index_path", lambda: fake_index)

    assert index_load() == []


def test_index_add_preserves_corrupted_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_index = tmp_path / "appdata" / "installs.json"
    fake_index.parent.mkdir(parents=True, exist_ok=True)
    fake_index.write_text("invalid json", encoding="utf-8")
    monkeypatch.setattr("dlss5_enabler.core.record.get_global_index_path", lambda: fake_index)
    rec = InstallRecord(game_exe="C:/g.exe", game_dir="C:/g")

    assert not index_add(rec)
    assert fake_index.read_text(encoding="utf-8") == "invalid json"


def test_index_add_is_concurrency_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_index = tmp_path / "appdata" / "installs.json"
    fake_index.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("dlss5_enabler.core.record.get_global_index_path", lambda: fake_index)
    records = [InstallRecord(game_exe=f"C:/g{i}/g.exe", game_dir=f"C:/g{i}") for i in range(8)]

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(index_add, records))

    assert all(results)
    assert {entry.game_dir for entry in index_load()} == {record.game_dir for record in records}


def test_index_save_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_index = tmp_path / "appdata" / "installs.json"
    monkeypatch.setattr("dlss5_enabler.core.record.get_global_index_path", lambda: fake_index)

    entries = [IndexEntry(game_exe="C:/g.exe", game_dir="C:/g", timestamp="2026-01-01")]
    with patch("dlss5_enabler.core.record.atomic_write_text", side_effect=OSError("Disk full")):
        assert not index_save(entries)


def test_index_snapshot_restores_previous_entry_and_preserves_other_games(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_index = tmp_path / "installs.json"
    monkeypatch.setattr("dlss5_enabler.core.record.get_global_index_path", lambda: fake_index)
    old = IndexEntry(game_exe="C:/Games/Title/game.exe", game_dir="C:/Games/Title", timestamp="old", tool_version="1.0")
    other = IndexEntry(game_exe="C:/Games/Other/game.exe", game_dir="C:/Games/Other", timestamp="other")
    assert index_save([old, other])
    snapshot = capture_index_entry(old.game_dir)
    assert snapshot.entry == old
    assert snapshot.entry is not old
    assert index_add(InstallRecord(game_exe=old.game_exe, game_dir=old.game_dir))

    assert restore_index_entry(old.game_dir, snapshot)

    restored = {entry.game_dir: entry for entry in index_load()}
    assert restored == {old.game_dir: old, other.game_dir: other}


def test_index_snapshot_restores_absent_entry_and_preserves_other_games(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_index = tmp_path / "installs.json"
    monkeypatch.setattr("dlss5_enabler.core.record.get_global_index_path", lambda: fake_index)
    record = InstallRecord(game_exe="C:/Games/Title/game.exe", game_dir="C:/Games/Title")
    other = IndexEntry(game_exe="C:/Games/Other/game.exe", game_dir="C:/Games/Other", timestamp="other")
    assert index_save([other])
    snapshot = capture_index_entry(record.game_dir)
    assert snapshot.entry is None
    assert index_add(record)

    assert restore_index_entry(record.game_dir, snapshot)

    assert index_load() == [other]


def test_index_snapshot_of_missing_index_does_not_create_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_index = tmp_path / "installs.json"
    monkeypatch.setattr("dlss5_enabler.core.record.get_global_index_path", lambda: fake_index)

    snapshot = capture_index_entry("C:/Games/Title")
    assert snapshot.entry is None
    assert snapshot.original_bytes is None
    assert restore_index_entry("C:/Games/Title", snapshot)
    assert not fake_index.exists()


def test_index_snapshot_rejects_corrupted_index_without_overwriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_index = tmp_path / "installs.json"
    monkeypatch.setattr("dlss5_enabler.core.record.get_global_index_path", lambda: fake_index)
    snapshot = capture_index_entry("C:/Games/Title")
    original = b'{"invalid":"index"}'
    fake_index.write_bytes(original)

    with pytest.raises(TypeError, match="JSON array"):
        capture_index_entry("C:/Games/Title")
    assert not restore_index_entry("C:/Games/Title", snapshot)
    assert fake_index.read_bytes() == original


def test_index_snapshot_rejects_mismatched_game_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_index = tmp_path / "installs.json"
    monkeypatch.setattr("dlss5_enabler.core.record.get_global_index_path", lambda: fake_index)
    other = IndexEntry(game_exe="C:/Games/Other/game.exe", game_dir="C:/Games/Other", timestamp="other")
    assert index_save([other])
    original = fake_index.read_bytes()
    snapshot = capture_index_entry(other.game_dir)

    assert not restore_index_entry("C:/Games/Title", snapshot)
    assert fake_index.read_bytes() == original


def test_index_snapshot_rejects_duplicate_game_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_index = tmp_path / "installs.json"
    monkeypatch.setattr("dlss5_enabler.core.record.get_global_index_path", lambda: fake_index)
    entry = IndexEntry(game_exe="C:/Games/Title/game.exe", game_dir="C:/Games/Title", timestamp="original")
    assert index_save([entry, entry.model_copy(update={"timestamp": "duplicate"})])
    original = fake_index.read_bytes()

    with pytest.raises(ValueError, match="Multiple install index entries"):
        capture_index_entry(entry.game_dir)

    assert fake_index.read_bytes() == original


def test_index_snapshot_restore_is_concurrency_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_index = tmp_path / "installs.json"
    monkeypatch.setattr("dlss5_enabler.core.record.get_global_index_path", lambda: fake_index)
    entries = [
        IndexEntry(game_exe=f"C:/Games/{i}/game.exe", game_dir=f"C:/Games/{i}", timestamp=str(i)) for i in range(8)
    ]
    assert index_save(entries)
    snapshots = [capture_index_entry(entry.game_dir) for entry in entries]
    original = fake_index.read_bytes()
    assert index_save([])

    def restore(snapshot: IndexEntrySnapshot) -> bool:
        return restore_index_entry(snapshot.game_dir, snapshot)

    with ThreadPoolExecutor(max_workers=8) as executor:
        restored = list(executor.map(restore, snapshots))

    assert all(restored)
    assert {entry.game_dir for entry in index_load()} == {entry.game_dir for entry in entries}
    assert fake_index.read_bytes() == original


@pytest.mark.parametrize("original", [None, b"[\r\n\r\n]\r\n"])
def test_index_snapshot_restores_missing_or_empty_index_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, original: bytes | None
) -> None:
    fake_index = tmp_path / "installs.json"
    monkeypatch.setattr("dlss5_enabler.core.record.get_global_index_path", lambda: fake_index)
    if original is not None:
        fake_index.write_bytes(original)
    record = InstallRecord(game_exe="C:/Games/Title/game.exe", game_dir="C:/Games/Title")
    snapshot = capture_index_entry(record.game_dir)
    assert index_add(record)

    assert restore_index_entry(record.game_dir, snapshot)

    if original is None:
        assert not fake_index.exists()
    else:
        assert fake_index.read_bytes() == original


def test_index_snapshot_restores_legacy_bytes_without_adding_default_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_index = tmp_path / "installs.json"
    monkeypatch.setattr("dlss5_enabler.core.record.get_global_index_path", lambda: fake_index)
    original = (
        b'[\r\n {"game_exe": "C:/Games/Title/game.exe", "game_dir": "C:/Games/Title", "timestamp": "old"},\r\n'
        b' {"game_exe": "C:/Games/Other/game.exe", "game_dir": "C:/Games/Other", "timestamp": "other"}\r\n]\r\n'
    )
    fake_index.write_bytes(original)
    snapshot = capture_index_entry("C:/Games/Title")
    assert index_add(InstallRecord(game_exe="C:/Games/Title/game.exe", game_dir="C:/Games/Title"))

    assert restore_index_entry("C:/Games/Title", snapshot)

    assert fake_index.read_bytes() == original


def test_index_snapshot_preserves_concurrent_other_game_updates_and_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_index = tmp_path / "installs.json"
    monkeypatch.setattr("dlss5_enabler.core.record.get_global_index_path", lambda: fake_index)
    old = IndexEntry(game_exe="C:/Games/Title/game.exe", game_dir="C:/Games/Title", timestamp="old")
    other = IndexEntry(game_exe="C:/Games/Other/game.exe", game_dir="C:/Games/Other", timestamp="other")
    removed = IndexEntry(game_exe="C:/Games/Removed/game.exe", game_dir="C:/Games/Removed", timestamp="removed")
    assert index_save([old, other, removed])
    snapshot = capture_index_entry(old.game_dir)
    updated = other.model_copy(update={"timestamp": "updated"})
    added = IndexEntry(game_exe="C:/Games/Added/game.exe", game_dir="C:/Games/Added", timestamp="added")
    assert index_save([updated, added])

    assert restore_index_entry(old.game_dir, snapshot)

    assert index_load() == [old, updated, added]


def test_missing_index_snapshot_keeps_concurrent_other_game_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_index = tmp_path / "installs.json"
    monkeypatch.setattr("dlss5_enabler.core.record.get_global_index_path", lambda: fake_index)
    record = InstallRecord(game_exe="C:/Games/Title/game.exe", game_dir="C:/Games/Title")
    snapshot = capture_index_entry(record.game_dir)
    assert index_add(record)
    other = InstallRecord(game_exe="C:/Games/Other/game.exe", game_dir="C:/Games/Other")
    assert index_add(other)

    assert restore_index_entry(record.game_dir, snapshot)

    assert [entry.game_dir for entry in index_load()] == [other.game_dir]


def test_index_snapshot_reports_restore_write_failure_without_truncating_current_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_index = tmp_path / "installs.json"
    monkeypatch.setattr("dlss5_enabler.core.record.get_global_index_path", lambda: fake_index)
    fake_index.write_bytes(b"[\r\n]\r\n")
    record = InstallRecord(game_exe="C:/Games/Title/game.exe", game_dir="C:/Games/Title")
    snapshot = capture_index_entry(record.game_dir)
    assert index_add(record)
    current = fake_index.read_bytes()

    with patch("dlss5_enabler.core.record.atomic_write_bytes", side_effect=OSError("Disk full")):
        assert not restore_index_entry(record.game_dir, snapshot)

    assert fake_index.read_bytes() == current
