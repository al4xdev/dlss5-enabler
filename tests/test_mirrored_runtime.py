from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from dlss5_enabler.core.mutations import prepare_managed_path
from dlss5_enabler.core.record import InstallRecord, record_save
from dlss5_enabler.operations.contexts import RenoDxContext
from dlss5_enabler.operations.steps import StepMirrorDualLocations, _place_file
from dlss5_enabler.operations.uninstall import run_uninstall


@pytest.mark.parametrize("finalization_fails", [False, True])
def test_bin_runtime_ownership_preserves_user_files_and_failed_uninstall(
    tmp_path: Path, mocker: MockerFixture, finalization_fails: bool
) -> None:
    game = tmp_path / "game"
    host = game / "bin" / "host64"
    host.mkdir(parents=True)
    exe = game / "game.exe"
    exe.write_bytes(b"synthetic game")
    user_log = game / "bin" / "ReShade.log"
    user_log.write_bytes(b"original user log")
    user_image = host / "dlss5-feed-host64-user.png"
    user_image.write_bytes(b"original screenshot")
    source = tmp_path / "source.dll"
    source.write_bytes(b"runtime")
    ctx = RenoDxContext(
        game_exe=exe,
        game_dir=game,
        reshade_dir=game,
        d3d9_translate=True,
        is_32bit=True,
        record=InstallRecord(game_exe=exe.as_posix(), game_dir=game.as_posix(), d3d9_translate=True),
    )
    _place_file(ctx, source, game / "dxgi.dll")
    _place_file(ctx, source, game / "host64" / "dxgi.dll")
    prepare_managed_path(ctx.record, game / "ReShade.log")
    prepare_managed_path(ctx.record, game / "host64" / "dlss5-feed-host.log")
    step = StepMirrorDualLocations()
    assert step.execute(ctx)
    assert step.execute(ctx)
    assert not (game / "bin" / "bin").exists()
    user_log.write_bytes(b"new runtime log")
    runtime_log = host / "dlss5-feed-host.log"
    runtime_log.write_bytes(b"host session")
    generated = host / "dlss5-feed-host64-new.png"
    generated.write_bytes(b"new capture")
    assert record_save(ctx.record)
    before = {path.relative_to(game): path.read_bytes() for path in game.rglob("*") if path.is_file()}
    if finalization_fails:
        mocker.patch("dlss5_enabler.operations.uninstall.index_remove", return_value=False)
    assert run_uninstall(game) is not finalization_fails
    if finalization_fails:
        assert {path.relative_to(game): path.read_bytes() for path in game.rglob("*") if path.is_file()} == before
    else:
        assert user_log.read_bytes() == b"original user log"
        assert user_image.read_bytes() == b"original screenshot"
        assert not runtime_log.exists()
        assert not generated.exists()
        assert host.is_dir()
        assert not (game / "host64").exists()
        assert not tuple(game.rglob("*.dlss5-enabler.bak*"))
