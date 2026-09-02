import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from pytest_mock import MockerFixture

from dlss5_enabler.network.http import _curl_download, create_client, http_download_file, http_get_json, http_get_text


def test_create_client() -> None:
    client = create_client()
    assert client.headers["User-Agent"].startswith("Mozilla/5.0")
    assert not client.is_closed
    client.close()


def test_http_get_text_success(mocker: MockerFixture) -> None:
    mock_response = mocker.MagicMock(spec=httpx.Response)
    mock_response.text = "OK_RESPONSE"
    mock_response.raise_for_status = mocker.MagicMock()

    mock_client = mocker.MagicMock(spec=httpx.Client)
    mock_client.get.return_value = mock_response
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = None

    mocker.patch("dlss5_enabler.network.http.create_client", return_value=mock_client)

    result = http_get_text("http://example.com")
    assert result == "OK_RESPONSE"


def test_http_get_text_retry_and_recover(mocker: MockerFixture) -> None:
    mock_fail = mocker.MagicMock(spec=httpx.Response)
    mock_fail.raise_for_status.side_effect = httpx.HTTPStatusError(
        "500", request=mocker.MagicMock(), response=mock_fail
    )

    mock_success = mocker.MagicMock(spec=httpx.Response)
    mock_success.text = "RECOVERED"
    mock_success.raise_for_status = mocker.MagicMock()

    mock_client = mocker.MagicMock(spec=httpx.Client)
    mock_client.get.side_effect = [mock_fail, mock_success]
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = None

    mocker.patch("dlss5_enabler.network.http.create_client", return_value=mock_client)
    mocker.patch("time.sleep", return_value=None)

    result = http_get_text("http://example.com", retries=2)
    assert result == "RECOVERED"


def test_http_get_text_retries_exhausted(mocker: MockerFixture) -> None:
    mock_client = mocker.MagicMock(spec=httpx.Client)
    mock_client.get.side_effect = httpx.ConnectError("Connection refused")
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = None

    mocker.patch("dlss5_enabler.network.http.create_client", return_value=mock_client)
    mocker.patch("time.sleep", return_value=None)

    with pytest.raises(RuntimeError, match="HTTP GET failed"):
        http_get_text("http://example.com", retries=2)


def test_http_get_text_custom_headers(mocker: MockerFixture) -> None:
    mock_response = mocker.MagicMock(spec=httpx.Response)
    mock_response.text = "CUSTOM"
    mock_response.raise_for_status = mocker.MagicMock()

    mock_client = mocker.MagicMock(spec=httpx.Client)
    mock_client.get.return_value = mock_response
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = None

    mocker.patch("dlss5_enabler.network.http.create_client", return_value=mock_client)

    result = http_get_text("http://example.com", headers={"X-Test": "123"})
    assert result == "CUSTOM"
    mock_client.get.assert_called_once()
    called_headers = mock_client.get.call_args[1]["headers"]
    assert called_headers["X-Test"] == "123"


def test_http_get_json_success(mocker: MockerFixture) -> None:
    mocker.patch("dlss5_enabler.network.http.http_get_text", return_value='{"version": "1.3.1", "tag": "v1.0"}')
    res: dict[str, Any] = http_get_json("http://api.example.com")
    assert res == {"version": "1.3.1", "tag": "v1.0"}


def test_http_get_json_invalid(mocker: MockerFixture) -> None:
    mocker.patch("dlss5_enabler.network.http.http_get_text", return_value="INVALID_JSON")
    with pytest.raises(Exception):
        http_get_json("http://api.example.com")


def test_curl_download_success(tmp_path: Path, mocker: MockerFixture) -> None:
    dest = tmp_path / "downloaded.zip"
    dest.write_bytes(b"DATA")

    mock_res = mocker.MagicMock(spec=subprocess.CompletedProcess)
    mock_res.returncode = 0
    mock_run = mocker.patch("subprocess.run", return_value=mock_res)

    assert _curl_download("http://example.com/file.zip", dest)
    command = mock_run.call_args.args[0]
    assert "-k" not in command
    assert "-C" not in command
    assert "--fail" in command


def test_curl_download_failure_nonzero_exit(tmp_path: Path, mocker: MockerFixture) -> None:
    dest = tmp_path / "downloaded.zip"
    dest.write_bytes(b"DATA")

    mock_res = mocker.MagicMock(spec=subprocess.CompletedProcess)
    mock_res.returncode = 28
    mocker.patch("subprocess.run", return_value=mock_res)

    assert not _curl_download("http://example.com/file.zip", dest)


def test_curl_download_empty_file(tmp_path: Path, mocker: MockerFixture) -> None:
    dest = tmp_path / "empty.zip"
    dest.write_bytes(b"")

    mock_res = mocker.MagicMock(spec=subprocess.CompletedProcess)
    mock_res.returncode = 0
    mocker.patch("subprocess.run", return_value=mock_res)

    assert not _curl_download("http://example.com/file.zip", dest)


def test_curl_download_exception(tmp_path: Path, mocker: MockerFixture) -> None:
    dest = tmp_path / "file.zip"
    mocker.patch("subprocess.run", side_effect=Exception("Curl not found"))
    assert not _curl_download("http://example.com/file.zip", dest)


def test_http_download_file_stream_success(tmp_path: Path, mocker: MockerFixture) -> None:
    dest = tmp_path / "output.bin"
    mock_stream_res = mocker.MagicMock()
    mock_stream_res.raise_for_status = mocker.MagicMock()
    mock_stream_res.headers = {"content-length": "10"}
    mock_stream_res.iter_bytes.return_value = [b"12345", b"67890"]

    mock_client = mocker.MagicMock(spec=httpx.Client)
    mock_client.stream.return_value.__enter__.return_value = mock_stream_res
    mock_client.stream.return_value.__exit__.return_value = None
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = None

    mocker.patch("dlss5_enabler.network.http.create_client", return_value=mock_client)

    progress_records: list[tuple[int, int]] = []

    def on_progress(dl: int, total: int) -> None:
        progress_records.append((dl, total))

    result = http_download_file("http://example.com/file.bin", dest, progress_fn=on_progress)
    assert result == dest
    assert dest.is_file()
    assert dest.read_bytes() == b"1234567890"
    assert (10, 10) in progress_records


def test_http_download_file_incomplete_stream_then_retry(tmp_path: Path, mocker: MockerFixture) -> None:
    dest = tmp_path / "retry.bin"

    # Attempt 1: Incomplete stream
    mock_bad = mocker.MagicMock()
    mock_bad.raise_for_status = mocker.MagicMock()
    mock_bad.headers = {"content-length": "10"}
    mock_bad.iter_bytes.return_value = [b"12345"]

    # Attempt 2: Complete stream
    mock_good = mocker.MagicMock()
    mock_good.raise_for_status = mocker.MagicMock()
    mock_good.headers = {"content-length": "10"}
    mock_good.iter_bytes.return_value = [b"1234567890"]

    mock_client = mocker.MagicMock(spec=httpx.Client)
    mock_client.stream.return_value.__enter__.side_effect = [mock_bad, mock_good]
    mock_client.stream.return_value.__exit__.return_value = None
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = None

    mocker.patch("dlss5_enabler.network.http.create_client", return_value=mock_client)
    mocker.patch("time.sleep", return_value=None)

    result = http_download_file("http://example.com/retry.bin", dest, retries=2)
    assert result == dest
    assert dest.read_bytes() == b"1234567890"


def test_http_download_file_curl_fallback(tmp_path: Path, mocker: MockerFixture) -> None:
    dest = tmp_path / "fallback.bin"

    # Httpx fails all retries
    mock_client = mocker.MagicMock(spec=httpx.Client)
    mock_client.stream.side_effect = httpx.ReadTimeout("Timeout")
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = None

    mocker.patch("dlss5_enabler.network.http.create_client", return_value=mock_client)
    mocker.patch("time.sleep", return_value=None)

    def mock_curl(url: str, d: Path) -> bool:
        d.write_bytes(b"CURL_DOWNLOADED")
        return True

    mocker.patch("dlss5_enabler.network.http._curl_download", side_effect=mock_curl)

    result = http_download_file("http://example.com/fallback.bin", dest, retries=2)
    assert result == dest
    assert dest.read_bytes() == b"CURL_DOWNLOADED"


def test_http_download_file_total_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    dest = tmp_path / "fail.bin"

    mock_client = mocker.MagicMock(spec=httpx.Client)
    mock_client.stream.side_effect = httpx.ReadTimeout("Timeout")
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = None

    mocker.patch("dlss5_enabler.network.http.create_client", return_value=mock_client)
    mocker.patch("time.sleep", return_value=None)
    mocker.patch("dlss5_enabler.network.http._curl_download", return_value=False)

    with pytest.raises(RuntimeError, match="Download failed for"):
        http_download_file("http://example.com/fail.bin", dest, retries=2)


def test_http_download_failure_preserves_existing_destination(tmp_path: Path, mocker: MockerFixture) -> None:
    dest = tmp_path / "existing.bin"
    dest.write_bytes(b"KNOWN_GOOD")
    mock_client = mocker.MagicMock(spec=httpx.Client)
    mock_client.stream.side_effect = httpx.ReadTimeout("Timeout")
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = None
    mocker.patch("dlss5_enabler.network.http.create_client", return_value=mock_client)
    mocker.patch("dlss5_enabler.network.http._curl_download", return_value=False)
    mocker.patch("time.sleep", return_value=None)

    with pytest.raises(RuntimeError):
        http_download_file("https://example.com/new.bin", dest, retries=1)
    assert dest.read_bytes() == b"KNOWN_GOOD"
