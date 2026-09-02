from importlib.metadata import PackageNotFoundError
from pathlib import Path

from pytest_mock import MockerFixture

import dlss5_enabler
from dlss5_enabler.core.record import IndexEntry, InstallRecord
from dlss5_enabler.core.version import (
    UNKNOWN_VERSION,
    InstallVersionStatus,
    get_install_version_status,
    get_tool_version,
)


def test_runtime_version_uses_distribution_metadata(mocker: MockerFixture) -> None:
    metadata = mocker.patch("dlss5_enabler.core.version.version", return_value="2.4.1")

    assert get_tool_version() == "2.4.1"
    metadata.assert_called_once_with("dlss5-enabler")


def test_runtime_version_without_distribution_metadata(mocker: MockerFixture) -> None:
    mocker.patch("dlss5_enabler.core.version.version", side_effect=PackageNotFoundError)

    assert get_tool_version() == UNKNOWN_VERSION


def test_runtime_models_share_the_version_source() -> None:
    record = InstallRecord(game_exe="C:/game/game.exe", game_dir="C:/game")
    entry = IndexEntry(game_exe=record.game_exe, game_dir=record.game_dir, timestamp=record.timestamp)

    assert record.tool_version == get_tool_version()
    assert entry.tool_version == get_tool_version()
    assert dlss5_enabler.__version__ == get_tool_version()


def test_install_version_status_uses_pep440_comparison() -> None:
    assert get_install_version_status("1.1.0", "1.1.0") is InstallVersionStatus.CURRENT
    assert get_install_version_status("1.0.1", "1.1.0") is InstallVersionStatus.UPDATE_AVAILABLE
    assert get_install_version_status("2.0.0", "1.1.0") is InstallVersionStatus.NEWER_THAN_CLI
    assert get_install_version_status("legacy", "1.1.0") is InstallVersionStatus.UNKNOWN_LEGACY


def test_records_do_not_contain_a_literal_package_version() -> None:
    source = (Path(__file__).parents[1] / "dlss5_enabler" / "core" / "record.py").read_text(encoding="utf-8")

    assert '"1.0.0"' not in source
    assert '"1.0.1"' not in source
