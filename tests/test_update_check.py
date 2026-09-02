import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from pytest_mock import MockerFixture

from dlss5_enabler.network.update_check import (
    FUTURE_CLOCK_TOLERANCE_SECONDS,
    UPDATE_CHECK_INTERVAL_SECONDS,
    UpdateCheckResult,
    check_for_update,
    fetch_latest_version,
)


def test_missing_marker_checks_once_and_stays_empty(tmp_path: Path, mocker: MockerFixture) -> None:
    marker = tmp_path / "update-check.lock"
    fetch = mocker.patch("dlss5_enabler.network.update_check.fetch_latest_version", return_value="1.2.0")
    mocker.patch("dlss5_enabler.network.update_check.get_tool_version", return_value="1.1.0")

    result = check_for_update(marker=marker, now=100_000.0)

    assert result.check_performed
    assert result.update_available
    assert marker.read_bytes() == b""
    assert marker.stat().st_mtime == 100_000.0
    fetch.assert_called_once_with()


def test_fresh_marker_skips_request(tmp_path: Path, mocker: MockerFixture) -> None:
    marker = tmp_path / "update-check.lock"
    marker.touch()
    os.utime(marker, (100_000.0, 100_000.0))
    fetch = mocker.patch("dlss5_enabler.network.update_check.fetch_latest_version")

    result = check_for_update(marker=marker, now=100_000.0 + UPDATE_CHECK_INTERVAL_SECONDS - 1)

    assert not result.check_performed
    fetch.assert_not_called()


def test_marker_exactly_24_hours_old_checks(tmp_path: Path, mocker: MockerFixture) -> None:
    marker = tmp_path / "update-check.lock"
    marker.touch()
    os.utime(marker, (100_000.0, 100_000.0))
    fetch = mocker.patch("dlss5_enabler.network.update_check.fetch_latest_version", return_value="1.0.1")

    result = check_for_update(marker=marker, now=100_000.0 + UPDATE_CHECK_INTERVAL_SECONDS)

    assert result.check_performed
    fetch.assert_called_once_with()


def test_future_marker_is_invalidated(tmp_path: Path, mocker: MockerFixture) -> None:
    marker = tmp_path / "update-check.lock"
    marker.touch()
    now = 100_000.0
    future = now + FUTURE_CLOCK_TOLERANCE_SECONDS + 1
    os.utime(marker, (future, future))
    fetch = mocker.patch("dlss5_enabler.network.update_check.fetch_latest_version", return_value="1.0.1")

    assert check_for_update(marker=marker, now=now).check_performed
    fetch.assert_called_once_with()


def test_failure_renews_empty_marker_without_raising(tmp_path: Path, mocker: MockerFixture) -> None:
    marker = tmp_path / "update-check.lock"
    fetch = mocker.patch(
        "dlss5_enabler.network.update_check.fetch_latest_version",
        side_effect=httpx.ReadTimeout("offline"),
    )

    result = check_for_update(marker=marker, now=200_000.0)

    assert result.check_performed
    assert result.error == "offline"
    assert marker.read_bytes() == b""
    assert marker.stat().st_mtime == 200_000.0
    fetch.assert_called_once_with()


def test_force_ignores_fresh_marker(tmp_path: Path, mocker: MockerFixture) -> None:
    marker = tmp_path / "update-check.lock"
    marker.touch()
    os.utime(marker, (100_000.0, 100_000.0))
    fetch = mocker.patch("dlss5_enabler.network.update_check.fetch_latest_version", return_value="1.0.1")

    result = check_for_update(force=True, marker=marker, now=100_001.0)

    assert result.check_performed
    fetch.assert_called_once_with()


def test_concurrent_checks_make_one_request(tmp_path: Path, mocker: MockerFixture) -> None:
    marker = tmp_path / "update-check.lock"
    fetch = mocker.patch("dlss5_enabler.network.update_check.fetch_latest_version", return_value="1.2.0")

    def check(_index: int) -> UpdateCheckResult:
        return check_for_update(marker=marker, now=100_000.0)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(check, range(2)))

    assert sum(result.check_performed for result in results) == 1
    fetch.assert_called_once_with()


def test_stable_install_ignores_remote_prerelease(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("dlss5_enabler.network.update_check.get_tool_version", return_value="1.1.0")
    mocker.patch("dlss5_enabler.network.update_check.fetch_latest_version", return_value="1.2.0rc1")

    result = check_for_update(marker=tmp_path / "marker", now=100_000.0)

    assert not result.update_available


def test_prerelease_install_accepts_newer_prerelease(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("dlss5_enabler.network.update_check.get_tool_version", return_value="1.2.0rc1")
    mocker.patch("dlss5_enabler.network.update_check.fetch_latest_version", return_value="1.2.0rc2")

    result = check_for_update(marker=tmp_path / "marker", now=100_000.0)

    assert result.update_available


@pytest.mark.parametrize(
    ("current", "latest"),
    [("1.1.0", "1.1.0"), ("1.2.0", "1.1.0")],
    ids=["equal", "remote-older"],
)
def test_checker_never_recommends_same_version_or_downgrade(
    tmp_path: Path,
    mocker: MockerFixture,
    current: str,
    latest: str,
) -> None:
    mocker.patch("dlss5_enabler.network.update_check.get_tool_version", return_value=current)
    mocker.patch("dlss5_enabler.network.update_check.fetch_latest_version", return_value=latest)

    result = check_for_update(marker=tmp_path / "marker", now=100_000.0)

    assert not result.update_available


def test_invalid_pypi_response_is_rejected(mocker: MockerFixture) -> None:
    response = mocker.MagicMock(spec=httpx.Response)
    response.json.return_value = {"info": {"version": "not a version"}}
    client = mocker.MagicMock(spec=httpx.Client)
    client.__enter__.return_value = client
    client.get.return_value = response
    mocker.patch("dlss5_enabler.network.update_check.httpx.Client", return_value=client)

    try:
        fetch_latest_version()
    except ValueError as error:
        assert "invalid project version" in str(error)
    else:
        raise AssertionError("invalid PyPI version was accepted")

    client.get.assert_called_once_with("https://pypi.org/pypi/dlss5-enabler/json")
    response.raise_for_status.assert_called_once_with()
