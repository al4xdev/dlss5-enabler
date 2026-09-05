import json
import zipfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from dlss5_enabler.core.fileio import atomic_copy_file, resource_lock
from dlss5_enabler.core.mutations import prepare_managed_path, prepare_runtime_artifacts, track_created_directories
from dlss5_enabler.core.record import (
    CURRENT_RECORD_SCHEMA_VERSION,
    InstallRecord,
    OptiScalerStrategyOptions,
    RecordedFile,
    RuntimeArtifacts,
    index_add,
    index_load,
    record_save,
)
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
from dlss5_enabler.schemas.strategy import InstallStrategy


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


def _optiscaler_record(game_dir: Path) -> InstallRecord:
    return InstallRecord(
        schema_version=CURRENT_RECORD_SCHEMA_VERSION,
        game_exe=(game_dir / "game.exe").as_posix(),
        game_dir=game_dir.as_posix(),
        strategy=InstallStrategy.OPTISCALER,
        strategy_options=OptiScalerStrategyOptions(proxy_name="dxgi.dll", source_revision="fixture-revision"),
    )


@pytest.mark.parametrize("finalization_fails", [False, True])
def test_optiscaler_proxy_ini_zip_files_and_runtime_restore_transactionally(
    tmp_path: Path, mocker: MockerFixture, finalization_fails: bool
) -> None:
    rec = _optiscaler_record(tmp_path)
    originals = {
        "dxgi.dll": b"Existing user proxy",
        "OptiScaler.ini": b"\xef\xbb\xbf; custom\r\n[DlssNr]\r\n Passes = 2 \r\n",
        "OptiScaler.log": b"Previous runtime log\r\n",
        "Licenses/OptiScaler/LICENSE.txt": b"Preexisting license bytes",
    }
    for name, content in originals.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    installed = {
        "dxgi.dll": b"Renamed OptiScaler DLL",
        "OptiScaler.ini": b"[DlssNr]\nEnabled=true\nPasses=1\n",
        "Licenses/OptiScaler/LICENSE.txt": b"Upstream license",
        "Plugins/helper.dll": b"Bundled helper",
        "nvngx.dll_dlssnr.dll": b"Bundled NGX proxy",
    }
    for name, content in installed.items():
        path = tmp_path / name
        prepare_managed_path(rec, path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    prepare_managed_path(rec, tmp_path / "OptiScaler.log")
    prepare_runtime_artifacts(rec, tmp_path, "OptiScaler.log*")
    (tmp_path / "OptiScaler.log").write_bytes(b"Current game session")
    rotated = tmp_path / "OptiScaler.log.1"
    rotated.write_bytes(b"Generated rotation")
    empty = tmp_path / "Plugins" / "empty"
    track_created_directories(rec, empty)
    empty.mkdir()
    assert record_save(rec)
    if finalization_fails:
        mocker.patch("dlss5_enabler.operations.uninstall.index_remove", return_value=False)

    assert run_uninstall(tmp_path) is not finalization_fails

    if finalization_fails:
        for name, content in installed.items():
            assert (tmp_path / name).read_bytes() == content
        assert (tmp_path / "OptiScaler.log").read_bytes() == b"Current game session"
        assert rotated.read_bytes() == b"Generated rotation"
        assert empty.is_dir()
        assert rec.record_path().is_file()
        assert all(Path(item.backup).is_file() for item in rec.files if item.backup)
    else:
        for name, content in originals.items():
            assert (tmp_path / name).read_bytes() == content
        assert (tmp_path / "Licenses" / "OptiScaler").is_dir()
        assert not (tmp_path / "Plugins").exists()
        assert not (tmp_path / "nvngx.dll_dlssnr.dll").exists()
        assert not rotated.exists()
        assert not rec.record_path().exists()
        assert not any(Path(item.backup).exists() for item in rec.files if item.backup)


@pytest.mark.parametrize("finalization_fails", [False, True])
def test_owned_optiscaler_capture_directory_is_snapshotted_and_cleaned_by_inventory(
    tmp_path: Path, mocker: MockerFixture, finalization_fails: bool
) -> None:
    rec = _optiscaler_record(tmp_path)
    capture_dir = tmp_path / "dlssnr-capture"
    track_created_directories(rec, capture_dir)
    prepare_runtime_artifacts(rec, capture_dir, "*.png")
    capture_dir.mkdir()
    captured = capture_dir / "before.png"
    captured.write_bytes(b"Generated comparison frame")
    assert record_save(rec)
    if finalization_fails:
        mocker.patch("dlss5_enabler.operations.uninstall.index_remove", return_value=False)

    assert run_uninstall(tmp_path) is not finalization_fails

    assert capture_dir.exists() is finalization_fails
    if finalization_fails:
        assert captured.read_bytes() == b"Generated comparison frame"
        assert rec.record_path().is_file()


def test_optiscaler_capture_cleanup_preserves_files_outside_recorded_pattern(tmp_path: Path) -> None:
    rec = _optiscaler_record(tmp_path)
    capture_dir = tmp_path / "dlssnr-capture"
    track_created_directories(rec, capture_dir)
    prepare_runtime_artifacts(rec, capture_dir, "*.png")
    capture_dir.mkdir()
    generated = capture_dir / "before.png"
    generated.write_bytes(b"Generated frame")
    unknown = capture_dir / "user-notes.txt"
    unknown.write_bytes(b"User notes after installation")
    assert record_save(rec)

    assert run_uninstall(tmp_path)

    assert unknown.read_bytes() == b"User notes after installation"
    assert not generated.exists()


def test_optiscaler_untracked_runtime_names_are_preserved(tmp_path: Path) -> None:
    rec = _optiscaler_record(tmp_path)
    log_path = tmp_path / "OptiScaler.log"
    log_path.write_bytes(b"Untracked log")
    capture_dir = tmp_path / "dlssnr-capture"
    capture_dir.mkdir()
    captured = capture_dir / "before.png"
    captured.write_bytes(b"Untracked frame")
    assert record_save(rec)

    assert run_uninstall(tmp_path)

    assert log_path.read_bytes() == b"Untracked log"
    assert captured.read_bytes() == b"Untracked frame"


def test_runtime_preparation_rejects_unknown_preexisting_capture_directory(tmp_path: Path) -> None:
    rec = _optiscaler_record(tmp_path)
    capture_dir = tmp_path / "dlssnr-capture"
    capture_dir.mkdir()
    user_file = capture_dir / "before.png"
    user_file.write_bytes(b"User frame")

    with pytest.raises(ValueError, match="not owned"):
        prepare_runtime_artifacts(rec, capture_dir, "*.png")

    assert rec.runtime_artifacts == []
    assert user_file.read_bytes() == b"User frame"


def test_invalid_created_directory_is_rejected_before_reverting_proxy(tmp_path: Path) -> None:
    game = tmp_path / "game"
    game.mkdir()
    foreign = tmp_path / "outside"
    foreign.mkdir()
    rec = _optiscaler_record(game)
    proxy = game / "dxgi.dll"
    proxy.write_bytes(b"Installed proxy")
    rec.files = [RecordedFile(path=proxy.as_posix())]
    rec.created_directories = [foreign.as_posix()]

    assert not revert_record_mutations(rec)

    assert proxy.read_bytes() == b"Installed proxy"
    assert foreign.is_dir()


def test_invalid_runtime_rule_is_rejected_before_reverting_proxy(tmp_path: Path) -> None:
    rec = _optiscaler_record(tmp_path)
    proxy = tmp_path / "dxgi.dll"
    proxy.write_bytes(b"Installed proxy")
    rec.files = [RecordedFile(path=proxy.as_posix())]
    rec.runtime_artifacts = [RuntimeArtifacts(directory=tmp_path.as_posix(), pattern="../*.log")]

    assert not revert_record_mutations(rec)

    assert proxy.read_bytes() == b"Installed proxy"


def test_runtime_capture_symlink_cannot_escape_installation(tmp_path: Path) -> None:
    game = tmp_path / "game"
    game.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "before.png"
    external.write_bytes(b"External user frame")
    capture_dir = game / "dlssnr-capture"
    rec = _optiscaler_record(game)
    track_created_directories(rec, capture_dir)
    prepare_runtime_artifacts(rec, capture_dir, "*.png")
    try:
        capture_dir.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating directory symlinks requires permission on this platform")
    proxy = game / "dxgi.dll"
    proxy.write_bytes(b"Installed proxy")
    rec.files = [RecordedFile(path=proxy.as_posix())]

    assert not revert_record_mutations(rec)

    assert proxy.read_bytes() == b"Installed proxy"
    assert external.read_bytes() == b"External user frame"
