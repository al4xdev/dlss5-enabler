import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from dlss5_enabler.core.fileio import resource_lock
from dlss5_enabler.core.util import unblock_file
from dlss5_enabler.platform import get_platform_adapter

DEFAULT_TIMEOUT: httpx.Timeout = httpx.Timeout(300.0, connect=60.0, read=180.0, write=60.0)
DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}


def create_client() -> httpx.Client:
    return httpx.Client(
        timeout=DEFAULT_TIMEOUT,
        follow_redirects=True,
        headers=DEFAULT_HEADERS,
        http2=False,
    )


def http_get_text(url: str, retries: int = 3, headers: dict[str, str] | None = None) -> str:
    req_headers = DEFAULT_HEADERS.copy()
    if headers:
        req_headers.update(headers)

    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with create_client() as client:
                res = client.get(url, headers=req_headers)
                res.raise_for_status()
                return res.text
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5 * attempt)
    raise RuntimeError(f"HTTP GET failed for {url}: {last_err}")


def http_get_json(url: str, retries: int = 3, headers: dict[str, str] | None = None) -> Any:
    text = http_get_text(url, retries=retries, headers=headers)
    return json.loads(text)


def _curl_download(url: str, dest: Path) -> bool:
    try:
        curl_cmd = get_platform_adapter().get_curl_command()
        cmd = [
            *curl_cmd,
            "-L",
            "--fail",
            "--retry",
            "5",
            "--retry-delay",
            "2",
            "--connect-timeout",
            "60",
            "-m",
            "300",
            "-o",
            str(dest),
            url,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=360, check=False)
        return res.returncode == 0 and dest.is_file() and dest.stat().st_size > 0
    except Exception:
        return False


def _verify_download_size(downloaded: int, total: int) -> None:
    if total > 0 and downloaded < total:
        raise OSError(f"Incomplete download ({downloaded}/{total} bytes)")


def http_download_file(
    url: str,
    dest_path: Path | str,
    progress_fn: Callable[[int, int], None] | None = None,
    retries: int = 3,
) -> Path:
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with resource_lock(dest):
        return _http_download_file_unlocked(url, dest, progress_fn, retries)


def _http_download_file_unlocked(
    url: str,
    dest: Path,
    progress_fn: Callable[[int, int], None] | None,
    retries: int,
) -> Path:
    fd, temp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".part", dir=dest.parent)
    os.close(fd)
    temp_dest = Path(temp_name)

    last_err: Exception | None = None
    try:
        for attempt in range(1, retries + 1):
            try:
                with create_client() as client, client.stream("GET", url) as res:
                    res.raise_for_status()
                    total = int(res.headers.get("content-length", 0))
                    downloaded = 0
                    if progress_fn:
                        progress_fn(0, total)
                    with temp_dest.open("wb") as stream:
                        for chunk in res.iter_bytes(chunk_size=131072):
                            stream.write(chunk)
                            downloaded += len(chunk)
                            if progress_fn:
                                progress_fn(downloaded, total)
                    _verify_download_size(downloaded, total)
                if temp_dest.stat().st_size > 0:
                    temp_dest.replace(dest)
                    unblock_file(dest)
                    return dest
            except Exception as error:
                last_err = error
                if attempt < retries:
                    time.sleep(2.0 * attempt)

        temp_dest.write_bytes(b"")
        if _curl_download(url, temp_dest):
            temp_dest.replace(dest)
            unblock_file(dest)
            return dest
        raise RuntimeError(f"Download failed for {url} -> {dest}: {last_err}")
    finally:
        temp_dest.unlink(missing_ok=True)
