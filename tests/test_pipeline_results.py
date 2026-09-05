from pathlib import Path

import pytest

from dlss5_enabler.core.fileio import atomic_copy_file
from dlss5_enabler.core.record import (
    InstallRecord,
    RecordedFile,
    RuntimeArtifacts,
    index_add,
    index_load,
    record_load,
    record_save,
)
from dlss5_enabler.operations import uninstall
from dlss5_enabler.operations.contexts import RenoDxContext
from dlss5_enabler.operations.pipeline import PipelineContext, PipelineRunner, PipelineStatus, PipelineStep
from dlss5_enabler.operations.steps import _place_file
from dlss5_enabler.operations.steps_common import StepCleanPreviousInstall, StepSaveRecord


class InstallCandidateStep(PipelineStep[RenoDxContext]):
    def __init__(self, source: Path) -> None:
        self.source = source

    @property
    def name(self) -> str:
        return "InstallCandidate"

    @property
    def description(self) -> str:
        return "Places a managed candidate binary"

    def execute(self, ctx: RenoDxContext) -> bool:
        _place_file(ctx, self.source, ctx.game_dir / "managed.dll")
        return True


class FinalizationFailureStep(PipelineStep[PipelineContext]):
    @property
    def name(self) -> str:
        return "FinalizeCandidate"

    @property
    def description(self) -> str:
        return "Checks persistence before failing critical finalization"

    def execute(self, ctx: PipelineContext) -> bool:
        return True

    def commit(self, ctx: PipelineContext) -> None:
        assert record_load(ctx.game_dir) == ctx.record
        assert any(entry.game_dir == ctx.record.game_dir for entry in index_load())
        raise RuntimeError("critical finalization failed")


def _prepare_game(tmp_path: Path, previous_install: bool) -> tuple[RenoDxContext, Path]:
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    game_exe = game_dir / "game.exe"
    game_exe.write_bytes(b"synthetic executable")
    managed = game_dir / "managed.dll"
    managed.write_bytes(b"user-owned original")

    unrelated = tmp_path / "other-game"
    unrelated.mkdir()
    unrelated_exe = unrelated / "other.exe"
    unrelated_exe.write_bytes(b"unrelated executable")
    other_record = InstallRecord(game_exe=unrelated_exe.as_posix(), game_dir=unrelated.as_posix())
    assert record_save(other_record)
    assert index_add(other_record)

    if previous_install:
        backup = game_dir / "managed.dll.original"
        backup.write_bytes(managed.read_bytes())
        managed.write_bytes(b"previous installed binary")
        runtime_log = game_dir / "managed-runtime-1.log"
        runtime_log.write_bytes(b"previous session diagnostics")
        previous = InstallRecord(
            game_exe=game_exe.as_posix(),
            game_dir=game_dir.as_posix(),
            tool_version="1.1.3",
            files=[RecordedFile(path=managed.as_posix(), backup=backup.as_posix())],
            runtime_artifacts=[RuntimeArtifacts(directory=game_dir.as_posix(), pattern="managed-runtime-*.log")],
        )
        assert record_save(previous)
        previous.record_path().write_bytes(previous.record_path().read_bytes() + b"\n  \n")
        assert index_add(previous)

    source = tmp_path / "candidate.dll"
    source.write_bytes(b"new installed binary")
    ctx = RenoDxContext(
        game_exe=game_exe,
        game_dir=game_dir,
        reshade_dir=game_dir,
        record=InstallRecord(game_exe=game_exe.as_posix(), game_dir=game_dir.as_posix(), tool_version="1.2.0"),
    )
    return ctx, source


def _game_files(game_dir: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in game_dir.iterdir() if path.is_file()}


@pytest.mark.parametrize("previous_install", [False, True])
def test_critical_commit_failure_restores_files_record_and_index(tmp_path: Path, previous_install: bool) -> None:
    ctx, source = _prepare_game(tmp_path, previous_install)
    original_files = _game_files(ctx.game_dir)
    original_index = (tmp_path / "global-state" / "installs.json").read_bytes()
    steps: list[PipelineStep[RenoDxContext]] = [
        StepCleanPreviousInstall(),
        InstallCandidateStep(source),
        StepSaveRecord(),
        FinalizationFailureStep(),
    ]

    result = PipelineRunner(steps).run_result(ctx)

    assert result.status is PipelineStatus.FAILED
    assert not result.success
    assert result.failed_step == "FinalizeCandidate"
    assert "critical finalization failed" in result.message
    assert result.recovery_errors == ()
    assert result.recovery_path is None
    assert ctx.previous_install_snapshot is None
    assert _game_files(ctx.game_dir) == original_files
    assert (tmp_path / "global-state" / "installs.json").read_bytes() == original_index


def test_failed_previous_file_restore_reports_retained_recovery_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, source = _prepare_game(tmp_path, previous_install=True)
    copy_file = atomic_copy_file
    managed = ctx.game_dir / "managed.dll"

    def fail_previous_binary_restore(source: Path | str, destination: Path | str) -> None:
        if Path(destination) == managed and Path(source).parent.name.startswith("dlss5-enabler-install-snapshot-"):
            raise PermissionError("previous binary is locked")
        copy_file(source, destination)

    monkeypatch.setattr(uninstall, "atomic_copy_file", fail_previous_binary_restore)
    steps: list[PipelineStep[RenoDxContext]] = [
        StepCleanPreviousInstall(),
        InstallCandidateStep(source),
        StepSaveRecord(),
        FinalizationFailureStep(),
    ]

    result = PipelineRunner(steps).run_result(ctx)
    snapshot = ctx.previous_install_snapshot
    try:
        assert result.status is PipelineStatus.RECOVERY_FAILED
        assert not result.success
        assert result.failed_step == "FinalizeCandidate"
        assert any("CleanPreviousInstall" in error for error in result.recovery_errors)
        assert snapshot is not None
        assert result.recovery_path == snapshot.root
        assert (snapshot.root / "recovery.json").is_file()
        assert snapshot.files[managed.resolve().as_posix()].read_bytes() == b"previous installed binary"
        assert any("previous binary is locked" in error for error in snapshot.recovery_errors)
        assert managed.read_bytes() == b"user-owned original"
        previous = record_load(ctx.game_dir)
        assert previous is not None
        assert previous.tool_version == "1.1.3"
    finally:
        if snapshot is not None:
            uninstall.cleanup_install_snapshot(snapshot)


def test_snapshot_cleanup_failure_keeps_committed_candidate_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, source = _prepare_game(tmp_path, previous_install=True)

    def fail_snapshot_cleanup(snapshot: uninstall.InstallSnapshot) -> None:
        assert snapshot.root.is_dir()
        raise PermissionError("snapshot is locked")

    monkeypatch.setattr("dlss5_enabler.operations.steps_common.cleanup_install_snapshot", fail_snapshot_cleanup)
    steps: list[PipelineStep[RenoDxContext]] = [
        StepCleanPreviousInstall(),
        InstallCandidateStep(source),
        StepSaveRecord(),
    ]

    result = PipelineRunner(steps).run_result(ctx)
    snapshot = ctx.previous_install_snapshot
    try:
        assert result.status is PipelineStatus.CLEANUP_PENDING
        assert result.success
        assert result.failed_step == ""
        assert result.recovery_errors == ()
        assert result.cleanup_errors == ("CleanPreviousInstall: snapshot is locked",)
        assert snapshot is not None
        assert result.recovery_path == snapshot.root
        assert (snapshot.root / "recovery.json").is_file()
        assert (ctx.game_dir / "managed.dll").read_bytes() == source.read_bytes()
        assert record_load(ctx.game_dir) == ctx.record
        assert any(entry.game_dir == ctx.record.game_dir and entry.tool_version == "1.2.0" for entry in index_load())
        backup = ctx.record.files[0].backup
        assert Path(backup).read_bytes() == b"user-owned original"
    finally:
        if snapshot is not None:
            uninstall.cleanup_install_snapshot(snapshot)


def test_common_record_step_accepts_context_without_renodx_state(tmp_path: Path) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"synthetic executable")
    ctx = PipelineContext(
        game_exe=game_exe,
        game_dir=tmp_path,
        record=InstallRecord(game_exe=game_exe.as_posix(), game_dir=tmp_path.as_posix()),
    )
    runner: PipelineRunner[PipelineContext] = PipelineRunner([StepSaveRecord()])

    result = runner.run_result(ctx)

    assert result.status is PipelineStatus.COMPLETED
    assert result.success
    assert record_load(tmp_path) == ctx.record
    assert not hasattr(ctx, "feeder_bundle")
    assert not hasattr(ctx, "reshade_dir")
