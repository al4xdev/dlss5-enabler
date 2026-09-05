import json
import zipfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from dlss5_enabler.core.fileio import atomic_copy_file, resource_lock
from dlss5_enabler.core.mutations import prepare_managed_path, prepare_runtime_artifacts, track_created_directories
from dlss5_enabler.core.record import InstallRecord, RecordedFile, index_add, index_load, record_save
from dlss5_enabler.operations.reshade import (
    ensure_mv_provider_def,
    extract_reshade_dlls_from_installer,
    normalize_search_paths,
)
from dlss5_enabler.operations.uninstall import (
    capture_install_snapshot,
    cleanup_install_snapshot,
    restore_install_snapshot,
    revert_record_mutations,
    run_uninstall,
)


def _record(game_dir: Path) -> InstallRecord:
    return InstallRecord(game_exe=(game_dir / "game.exe").as_posix(), game_dir=game_dir.as_posix(), reshade_by_us=True)


def test_uninstall_restores_preexisting_runtime_log_without_deleting_original(tmp_path: Path) -> None:
    log_path = tmp_path / "ReShade.log"
    original = b"Earlier user log\r\n\x00\xff"
    log_path.write_bytes(original)
    rec = _record(tmp_path)
    item = prepare_managed_path(rec, log_path)
    log_path.write_bytes(b"Installed runtime log")
    prepare_runtime_artifacts(rec, tmp_path, "ReShade.log")
    assert record_save(rec)

    assert run_uninstall(tmp_path)

    assert log_path.read_bytes() == original
    assert not Path(item.backup).exists()
    assert not rec.record_path().exists()


@pytest.mark.parametrize("failure_after_first_edit", [False, True])
def test_existing_ini_restores_exact_bytes_after_configuration_or_failure(
    tmp_path: Path, mocker: MockerFixture, failure_after_first_edit: bool
) -> None:
    ini = tmp_path / "ReShade.ini"
    original = (
        b"\xef\xbb\xbf; user formatting \xff\r\n[GENERAL]\r\n"
        b" EffectSearchPaths = ./shaders/**,./shaders/** \r\nTextureSearchPaths=\r\n"
        b"PreprocessorDefinitions = USER=1 \r\nPreProcessorDefinitions = PreserveThis\r\n"
    )
    ini.write_bytes(original)
    rec = _record(tmp_path)
    assert normalize_search_paths(ini, rec)
    if failure_after_first_edit:
        mocker.patch("dlss5_enabler.operations.reshade.ini_set_exact", return_value=False)
        assert not ensure_mv_provider_def(ini, rec)
        assert revert_record_mutations(rec)
    else:
        assert ensure_mv_provider_def(ini, rec)
        assert "PreProcessorDefinitions" not in ini.read_text(encoding="utf-8")
        assert record_save(rec)
        assert run_uninstall(tmp_path)

    assert ini.read_bytes() == original
    assert len(rec.files) == 1
    assert not Path(rec.files[0].backup).exists()


def test_missing_backup_preserves_all_installed_files_before_mutating(tmp_path: Path) -> None:
    modified = tmp_path / "dxgi.dll"
    modified.write_bytes(b"Keep available runtime")
    candidate = tmp_path / "new.addon64"
    candidate.write_bytes(b"Keep installation consistent")
    rec = _record(tmp_path)
    rec.files = [
        RecordedFile(path=modified.as_posix(), backup=(tmp_path / "missing.bak").as_posix()),
        RecordedFile(path=candidate.as_posix()),
    ]

    assert not revert_record_mutations(rec)

    assert modified.read_bytes() == b"Keep available runtime"
    assert candidate.read_bytes() == b"Keep installation consistent"


def test_failed_backup_copy_preserves_destination_and_backup(tmp_path: Path, mocker: MockerFixture) -> None:
    target = tmp_path / "dxgi.dll"
    target.write_bytes(b"Original")
    rec = _record(tmp_path)
    item = prepare_managed_path(rec, target)
    target.write_bytes(b"Installed")
    mocker.patch("dlss5_enabler.operations.uninstall.atomic_copy_file", side_effect=OSError("disk failure"))

    assert not revert_record_mutations(rec)

    assert target.read_bytes() == b"Installed"
    assert Path(item.backup).read_bytes() == b"Original"


def test_legacy_untracked_runtime_artifacts_and_empty_user_directories_survive(tmp_path: Path) -> None:
    host = tmp_path / "host64"
    host.mkdir()
    log_path = host / "dlss5-feed-host.log"
    log_path.write_bytes(b"User-owned old log")
    screenshot = host / "dlss5-feed-host64 old.png"
    screenshot.write_bytes(b"User screenshot")
    user_shaders = tmp_path / "reshade-shaders" / "Shaders"
    user_shaders.mkdir(parents=True)
    rec = _record(tmp_path)
    assert record_save(rec)

    assert run_uninstall(tmp_path)

    assert log_path.read_bytes() == b"User-owned old log"
    assert screenshot.read_bytes() == b"User screenshot"
    assert user_shaders.is_dir()


@pytest.mark.parametrize("finalization_fails", [False, True])
def test_runtime_inventory_preserves_old_screenshots_and_recovers_generated_files(
    tmp_path: Path, mocker: MockerFixture, finalization_fails: bool
) -> None:
    host = tmp_path / "host64"
    host.mkdir()
    original = host / "dlss5-feed-host64 old.png"
    original.write_bytes(b"Old screenshot")
    rec = _record(tmp_path)
    prepare_runtime_artifacts(rec, host, "dlss5-feed-host64*.png")
    generated = host / "dlss5-feed-host64 new.png"
    generated.write_bytes(b"Generated screenshot")
    unknown = host / "family.png"
    unknown.write_bytes(b"User photo")
    assert record_save(rec)
    if finalization_fails:
        mocker.patch("dlss5_enabler.operations.uninstall.index_remove", return_value=False)

    assert run_uninstall(tmp_path) is not finalization_fails

    assert original.read_bytes() == b"Old screenshot"
    assert unknown.read_bytes() == b"User photo"
    assert generated.exists() is finalization_fails
    if finalization_fails:
        assert generated.read_bytes() == b"Generated screenshot"
        assert rec.record_path().is_file()


def test_uninstall_removes_only_created_empty_directories(tmp_path: Path) -> None:
    preexisting = tmp_path / "reshade-shaders"
    preexisting.mkdir()
    created = preexisting / "Shaders" / "include"
    rec = _record(tmp_path)
    track_created_directories(rec, created)
    created.mkdir(parents=True)
    assert record_save(rec)

    assert run_uninstall(tmp_path)

    assert preexisting.is_dir()
    assert not created.parent.exists()


def test_failed_uninstall_restores_original_absence_and_backup(tmp_path: Path, mocker: MockerFixture) -> None:
    target = tmp_path / "dxgi.dll"
    target.write_bytes(b"Original file")
    rec = _record(tmp_path)
    item = prepare_managed_path(rec, target)
    target.unlink()
    assert record_save(rec)
    original_record = rec.record_path().read_bytes()
    mocker.patch("dlss5_enabler.operations.uninstall.index_remove", return_value=False)

    assert not run_uninstall(tmp_path)

    assert not target.exists()
    assert Path(item.backup).read_bytes() == b"Original file"
    assert rec.record_path().read_bytes() == original_record


@pytest.mark.parametrize("invalid", ["{broken json", '{"schema_version": 999}'])
def test_invalid_existing_install_record_preserves_index(tmp_path: Path, invalid: str) -> None:
    rec = _record(tmp_path)
    assert index_add(rec)
    rec.record_path().write_text(invalid, encoding="utf-8")
    messages: list[str] = []

    assert not run_uninstall(tmp_path, log=messages.append)

    assert rec.record_path().read_text(encoding="utf-8") == invalid
    assert [entry.game_dir for entry in index_load()] == [tmp_path.as_posix()]
    assert any("could not be read or validated" in message for message in messages)


def test_failed_snapshot_recovery_attempts_remaining_files_and_retains_manifest(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    first = tmp_path / "first.dll"
    second = tmp_path / "second.dll"
    first.write_bytes(b"First installation")
    second.write_bytes(b"Second installation")
    rec = _record(tmp_path)
    rec.files = [RecordedFile(path=first.as_posix()), RecordedFile(path=second.as_posix())]
    assert record_save(rec)
    snapshot = capture_install_snapshot(rec)
    first.unlink()
    second.unlink()

    def fail_first(source: Path, destination: Path) -> None:
        if destination == first:
            raise OSError("Locked")
        atomic_copy_file(source, destination)

    mocker.patch("dlss5_enabler.operations.uninstall.atomic_copy_file", side_effect=fail_first)
    try:
        assert not restore_install_snapshot(snapshot)
        assert second.read_bytes() == b"Second installation"
        assert any("first.dll" in error for error in snapshot.recovery_errors)
        manifest = snapshot.root / "recovery.json"
        assert manifest.is_file()
        assert "first.dll" in manifest.read_text(encoding="utf-8")
        assert all(saved.is_file() for saved in snapshot.files.values())
    finally:
        cleanup_install_snapshot(snapshot)


def test_snapshot_restores_empty_owned_directories_after_failed_finalization(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    rec = _record(tmp_path)
    host = tmp_path / "host64"
    track_created_directories(rec, host)
    host.mkdir()
    assert record_save(rec)
    mocker.patch("dlss5_enabler.operations.uninstall.index_remove", return_value=False)

    assert not run_uninstall(tmp_path)

    assert host.is_dir()
    assert rec.record_path().is_file()


def test_snapshot_preserves_older_index_entry_and_other_game_changes(tmp_path: Path) -> None:
    rec = _record(tmp_path)
    assert record_save(rec)
    assert index_add(rec)
    before = index_load()[0]
    snapshot = capture_install_snapshot(rec)
    changed = rec.model_copy(update={"timestamp": "changed"})
    assert index_add(changed)
    other = _record(tmp_path / "another")
    assert index_add(other)

    assert restore_install_snapshot(snapshot)

    restored = next(entry for entry in index_load() if entry.game_dir == rec.game_dir)
    assert restored == before
    assert any(entry.game_dir == other.game_dir for entry in index_load())


def test_invalid_runtime_pattern_is_rejected_before_file_mutation(tmp_path: Path) -> None:
    rec = _record(tmp_path)
    target = tmp_path / "dxgi.dll"
    target.write_bytes(b"Installed")
    rec.files = [RecordedFile(path=target.as_posix())]
    data = rec.model_dump(mode="json")
    data["runtime_artifacts"] = [{"directory": tmp_path.as_posix(), "pattern": "../*.dll", "preexisting": []}]
    rec.record_path().write_text(json.dumps(data), encoding="utf-8")

    assert not run_uninstall(tmp_path)

    assert target.read_bytes() == b"Installed"


def test_snapshot_can_be_restored_without_discarding_recovery_data(tmp_path: Path) -> None:
    rec = _record(tmp_path)
    assert record_save(rec)
    snapshot = capture_install_snapshot(rec)
    try:
        rec.record_path().unlink()

        assert restore_install_snapshot(snapshot, cleanup=False)

        assert rec.record_path().is_file()
        assert (snapshot.root / "recovery.json").is_file()
        assert all(saved.is_file() for saved in snapshot.files.values())
    finally:
        cleanup_install_snapshot(snapshot)


def test_unexpected_revert_exception_recovers_installed_files(tmp_path: Path, mocker: MockerFixture) -> None:
    rec = _record(tmp_path)
    installed = tmp_path / "dxgi.dll"
    installed.write_bytes(b"Installed")
    rec.files = [RecordedFile(path=installed.as_posix())]
    assert record_save(rec)

    def fail_after_remove(_rec: InstallRecord, _log: object) -> bool:
        installed.unlink()
        raise OSError("Unexpected failure")

    mocker.patch("dlss5_enabler.operations.uninstall.revert_record_mutations", side_effect=fail_after_remove)

    assert not run_uninstall(tmp_path)

    assert installed.read_bytes() == b"Installed"
    assert rec.record_path().is_file()


@pytest.mark.parametrize(
    "unsafe_member",
    ["../escape.dll", "..\\escape.dll", "C:/outside.dll", "nested/ReShade64.dll", "RESHade64.DLL"],
)
def test_reshade_archive_rejects_unsafe_paths_and_ambiguous_runtime_without_partial_output(
    tmp_path: Path, unsafe_member: str
) -> None:
    setup = tmp_path / "setup.exe"
    with zipfile.ZipFile(setup, "w") as archive:
        archive.writestr("ReShade64.dll", b"DLL")
        archive.writestr(unsafe_member, b"INVALID")
    destination = tmp_path / "destination"
    destination.mkdir()
    trusted = destination / "ReShade64.dll"
    trusted.write_bytes(b"Previous valid runtime")

    assert extract_reshade_dlls_from_installer(setup, destination) == {}

    assert trusted.read_bytes() == b"Previous valid runtime"
    assert list(destination.iterdir()) == [trusted]
    assert not (tmp_path / "escape.dll").exists()
    assert not list(tmp_path.glob("dlss5-enabler-reshade-extract-*"))


def test_reshade_archive_normalizes_windows_paths_and_extracts_only_runtimes(tmp_path: Path) -> None:
    setup = tmp_path / "setup.exe"
    with zipfile.ZipFile(setup, "w") as archive:
        archive.writestr("embedded\\ReShade64.dll", b"DLL64")
        archive.writestr("embedded\\ReShade32.dll", b"DLL32")
        archive.writestr("unrelated.txt", b"Do not install")
    setup.write_bytes(b"Installer executable prefix" + setup.read_bytes())
    destination = tmp_path / "destination"

    extracted = extract_reshade_dlls_from_installer(setup, destination)

    assert extracted["reshade64.dll"].read_bytes() == b"DLL64"
    assert extracted["reshade32.dll"].read_bytes() == b"DLL32"
    assert not list(destination.rglob("unrelated.txt"))


def test_reshade_archive_crc_failure_discards_already_extracted_runtime(tmp_path: Path) -> None:
    setup = tmp_path / "setup.exe"
    with zipfile.ZipFile(setup, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("ReShade64.dll", b"DLL64")
        archive.writestr("ReShade32.dll", b"DLL32")
    setup.write_bytes(setup.read_bytes().replace(b"DLL32", b"BAD32"))
    destination = tmp_path / "destination"

    assert extract_reshade_dlls_from_installer(setup, destination) == {}

    assert not destination.exists()
    assert not list(tmp_path.glob("dlss5-enabler-reshade-extract-*"))


def test_reshade_invalid_archive_does_not_return_old_destination_files(tmp_path: Path) -> None:
    setup = tmp_path / "setup.exe"
    setup.write_bytes(b"Invalid archive")
    destination = tmp_path / "destination"
    destination.mkdir()
    trusted = destination / "ReShade64.dll"
    trusted.write_bytes(b"Previous valid runtime")

    assert extract_reshade_dlls_from_installer(setup, destination) == {}

    assert trusted.read_bytes() == b"Previous valid runtime"


def test_reshade_archive_rejects_symbolic_link_members(tmp_path: Path) -> None:
    setup = tmp_path / "setup.exe"
    link = zipfile.ZipInfo("linked")
    link.create_system = 3
    link.external_attr = 0o120777 << 16
    with zipfile.ZipFile(setup, "w") as archive:
        archive.writestr("ReShade64.dll", b"DLL")
        archive.writestr(link, "../../outside")

    assert extract_reshade_dlls_from_installer(setup, tmp_path / "destination") == {}

    assert not (tmp_path / "destination").exists()


def test_managed_ini_updates_hold_one_destination_lock_without_reacquiring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acquisitions: list[Path] = []

    @contextmanager
    def short_lock(resource: Path | str, timeout: float = 0.05) -> Generator[None, None, None]:
        acquisitions.append(Path(resource).resolve())
        with resource_lock(resource, timeout=0.05):
            yield

    monkeypatch.setattr("dlss5_enabler.core.mutations.resource_lock", short_lock)
    monkeypatch.setattr("dlss5_enabler.core.fileio.resource_lock", short_lock)
    monkeypatch.setattr("dlss5_enabler.core.ini.resource_lock", short_lock)
    ini = tmp_path / "ReShade.ini"
    original = b"[GENERAL]\r\nEffectSearchPaths = ./shaders/**,./shaders/**\r\n"
    ini.write_bytes(original)
    rec = _record(tmp_path)

    assert normalize_search_paths(ini, rec)
    assert ensure_mv_provider_def(ini, rec)

    assert acquisitions.count(ini.resolve()) == 2
    assert len(rec.files) == 1
    assert revert_record_mutations(rec)
    assert ini.read_bytes() == original
