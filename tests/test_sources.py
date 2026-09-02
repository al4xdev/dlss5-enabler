import json
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from pytest_mock import MockerFixture

from dlss5_enabler.network.resolver import ArtifactResolutionError, ResolutionWarningCode, UpstreamResolutionError
from dlss5_enabler.network.sources import (
    fetch_dgvoodoo,
    fetch_feeder,
    fetch_lumenite,
    fetch_ngx_dlls,
    fetch_renodx_dlss5,
    fetch_reshade,
    fetch_reshade_headers,
    zip_extract_matching,
)

MANIFEST_REVISION = "a" * 40
HEADERS_REVISION = "b" * 40
LUMENITE_REVISION = "c" * 40


def _create_mock_zip(files: dict[str, bytes]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_zip_extract_matching_flatten(tmp_path: Path) -> None:
    zip_bytes = _create_mock_zip(
        {
            "sub/folder/file1.addon64": b"ADDON64_CONTENT",
            "sub/folder/file2.txt": b"TXT_CONTENT",
        }
    )
    zip_path = tmp_path / "archive.zip"
    zip_path.write_bytes(zip_bytes)

    dest_dir = tmp_path / "extracted"
    results = zip_extract_matching(zip_path, dest_dir, ["*.addon64"], flatten=True)

    assert len(results) == 1
    assert results[0] == dest_dir / "file1.addon64"
    assert results[0].read_bytes() == b"ADDON64_CONTENT"


def test_zip_extract_matching_no_flatten(tmp_path: Path) -> None:
    zip_bytes = _create_mock_zip(
        {
            "MS/x86/D3D9.dll": b"D3D9_CONTENT",
        }
    )
    zip_path = tmp_path / "dgVoodoo.zip"
    zip_path.write_bytes(zip_bytes)

    dest_dir = tmp_path / "extracted"
    results = zip_extract_matching(zip_path, dest_dir, ["MS/x86/*.dll"], flatten=False)

    assert len(results) == 1
    assert results[0] == dest_dir / "MS" / "x86" / "D3D9.dll"
    assert results[0].read_bytes() == b"D3D9_CONTENT"


def test_zip_extract_matching_no_matches_raises(tmp_path: Path) -> None:
    zip_bytes = _create_mock_zip({"file.txt": b"DATA"})
    zip_path = tmp_path / "test.zip"
    zip_path.write_bytes(zip_bytes)

    with pytest.raises(ValueError, match="No matching files"):
        zip_extract_matching(zip_path, tmp_path / "out", ["*.dll"])


def test_zip_extract_matching_rejects_path_traversal(tmp_path: Path) -> None:
    zip_path = tmp_path / "unsafe.zip"
    zip_path.write_bytes(_create_mock_zip({"../../outside.dll": b"UNSAFE"}))

    with pytest.raises(ValueError, match="Unsafe archive member"):
        zip_extract_matching(zip_path, tmp_path / "out", ["*"], flatten=False)
    assert not (tmp_path / "outside.dll").exists()


def test_zip_extract_matching_rejects_flatten_collisions(tmp_path: Path) -> None:
    zip_path = tmp_path / "collision.zip"
    zip_path.write_bytes(_create_mock_zip({"x/same.dll": b"ONE", "y/same.dll": b"TWO"}))

    with pytest.raises(ValueError, match="collide"):
        zip_extract_matching(zip_path, tmp_path / "out", ["*"], flatten=True)


def test_fetch_feeder(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("dlss5_enabler.network.sources.get_cache_dir", return_value=tmp_path)
    metadata = mocker.patch(
        "dlss5_enabler.network.sources.http_get_json",
        return_value={
            "tag_name": "v0.12.0",
            "assets": [
                {
                    "name": "DLSS5-Feeder-0.12.0.zip",
                    "browser_download_url": "https://example.com/DLSS5-Feeder-0.12.0.zip",
                }
            ],
        },
    )

    def mock_download(_url: str, dest: Path | str, progress_fn: Any = None) -> Path:
        p = Path(dest)
        p.write_bytes(
            _create_mock_zip(
                {
                    "addon64/dlss5-feed.addon64": b"ADDON64",
                    "addon32/dlss5-feed.addon32": b"ADDON32",
                    "shared/DLSS5_Feed.fx": b"SHADER",
                    "host64/dlss5-feed-host64.exe": b"HOST64",
                    "layer-x64/VkLayer_feed_vk.dll": b"VULKAN64",
                    "layer-x86/VkLayer_feed_vk32.dll": b"VULKAN32",
                }
            )
        )
        return p

    download = mocker.patch("dlss5_enabler.network.sources.http_download_file", side_effect=mock_download)

    bundle = fetch_feeder(log=lambda msg: None, force=True)
    assert bundle.release_tag == "v0.12.0"
    assert bundle.addon64 is not None and bundle.addon64.is_file()
    assert bundle.addon32 is not None and bundle.addon32.is_file()
    assert bundle.fx_shader is not None and bundle.fx_shader.is_file()
    assert bundle.host64_exe is not None and bundle.host64_exe.is_file()
    assert bundle.vk_layer_zip is not None and bundle.vk_layer_zip.is_file()
    assert bundle.vk_layer_zip.name == "DLSS5-Feeder.zip"
    assert "dlss5-feed.addon64" in bundle.binaries
    assert download.call_count == 1
    assert download.call_args.args[0] == "https://example.com/DLSS5-Feeder-0.12.0.zip"
    metadata.assert_called_once_with("https://api.github.com/repos/jlrouzies-fr/DLSS5-Feeder/releases/latest")


def test_fetch_feeder_rejects_release_without_compatible_assets(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("dlss5_enabler.network.sources.get_cache_dir", return_value=tmp_path)
    mocker.patch(
        "dlss5_enabler.network.sources.http_get_json",
        return_value={
            "tag_name": "v1.0.0",
            "assets": [
                {"name": "checksums.txt", "browser_download_url": "https://example.com/checksums.txt"},
            ],
        },
    )
    download = mocker.patch(
        "dlss5_enabler.network.sources.http_download_file",
        side_effect=RuntimeError("download rejected"),
    )

    with pytest.raises(UpstreamResolutionError, match="no matching asset"):
        fetch_feeder(log=lambda _message: None)
    download.assert_called_once()
    assert download.call_args.args[0].endswith("/v0.12.0/DLSS5-Feeder-0.12.0.zip")


def test_fetch_feeder_invalidates_cache_when_release_changes(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("dlss5_enabler.network.sources.get_cache_dir", return_value=tmp_path)
    mocker.patch(
        "dlss5_enabler.network.sources.http_get_json",
        side_effect=[
            {
                "tag_name": "v1.0.0",
                "assets": [
                    {
                        "name": "DLSS5-Feeder-1.0.0.zip",
                        "browser_download_url": "https://example.com/feeder-v1.zip",
                    }
                ],
            },
            {
                "tag_name": "v2.0.0",
                "assets": [
                    {
                        "name": "DLSS5-Feeder-2.0.0.zip",
                        "browser_download_url": "https://example.com/feeder-v2.zip",
                    }
                ],
            },
        ],
    )
    download = mocker.patch("dlss5_enabler.network.sources.http_download_file")

    def write_download(_url: str, dest: Path | str, progress_fn: Any = None) -> Path:
        path = Path(dest)
        path.write_bytes(
            _create_mock_zip(
                {
                    "dlss5-feed.addon64": b"ADDON64",
                    "dlss5-feed.addon32": b"ADDON32",
                    "DLSS5_Feed.fx": b"SHADER",
                    "dlss5-feed-host64.exe": b"HOST64",
                }
            )
        )
        return path

    download.side_effect = write_download
    fetch_feeder(log=lambda _message: None)
    fetch_feeder(log=lambda _message: None)

    assert download.call_count == 2


def test_fetch_renodx_dlss5(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("dlss5_enabler.network.sources.get_cache_dir", return_value=tmp_path)
    mock_releases: list[dict[str, Any]] = [
        {
            "tag_name": "renodx-dlss5-4.70",
            "assets": [
                {"name": "renodx-dlss5_4.70.zip", "browser_download_url": "https://example.com/renodx.zip"},
            ],
        },
    ]
    metadata = mocker.patch("dlss5_enabler.network.sources.http_get_json", return_value=mock_releases)

    def mock_download(url: str, dest: Path | str, progress_fn: Any = None) -> Path:
        zip_bytes = _create_mock_zip({"renodx-dlss5.addon64": b"RENODX_BINARY"})
        p = Path(dest)
        p.write_bytes(zip_bytes)
        return p

    download = mocker.patch("dlss5_enabler.network.sources.http_download_file", side_effect=mock_download)

    bundle = fetch_renodx_dlss5(log=lambda msg: None, force=True)
    assert bundle.version == "4.70"
    assert bundle.addon64_path is not None and bundle.addon64_path.is_file()
    assert bundle.addon64_path.read_bytes() == b"RENODX_BINARY"
    assert download.call_count == 1
    assert download.call_args.args[0] == "https://example.com/renodx.zip"
    metadata.assert_called_once_with("https://api.github.com/repos/RankFTW/rhi-repo/releases?per_page=100")


def test_fetch_renodx_selects_highest_version(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("dlss5_enabler.network.sources.get_cache_dir", return_value=tmp_path)
    releases: list[dict[str, Any]] = [
        {
            "tag_name": "renodx-dlss5-4.2",
            "assets": [{"name": "old.zip", "browser_download_url": "https://example.com/old.zip"}],
        },
        {
            "tag_name": "renodx-dlss5-4.10",
            "assets": [{"name": "new.zip", "browser_download_url": "https://example.com/new.zip"}],
        },
    ]
    mocker.patch("dlss5_enabler.network.sources.http_get_json", return_value=releases)

    def mock_download(url: str, dest: Path | str, progress_fn: Any = None) -> Path:
        path = Path(dest)
        path.write_bytes(_create_mock_zip({"renodx-dlss5.addon64": url.encode()}))
        return path

    mocker.patch("dlss5_enabler.network.sources.http_download_file", side_effect=mock_download)
    bundle = fetch_renodx_dlss5(log=lambda _message: None)

    assert bundle.version == "4.10"
    assert bundle.addon64_path is not None
    assert bundle.addon64_path.read_bytes() == b"https://example.com/new.zip"


def test_fetch_ngx_dlls(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("dlss5_enabler.network.sources.get_cache_dir", return_value=tmp_path)
    mock_manifest: dict[str, Any] = {
        "dlssnr": [{"version": "310.8.SF-v2", "url": "https://example.com/nr.zip"}],
        "dlss": [{"version": "310.8.0", "url": "https://example.com/sr.zip"}],
    }
    metadata = mocker.patch(
        "dlss5_enabler.network.sources.http_get_json",
        return_value={"sha": MANIFEST_REVISION},
    )

    def mock_download(url: str, dest: Path | str, progress_fn: Any = None) -> Path:
        p = Path(dest)
        if url.endswith("dlss_manifest.json"):
            p.write_text(json.dumps(mock_manifest), encoding="utf-8")
        else:
            dll_name = "nvngx_dlssnr.dll" if "nr" in url else "nvngx_dlss.dll"
            p.write_bytes(_create_mock_zip({dll_name: b"NGX_DLL_BINARY"}))
        return p

    download = mocker.patch("dlss5_enabler.network.sources.http_download_file", side_effect=mock_download)

    bundle = fetch_ngx_dlls(log=lambda msg: None, force=True)
    assert bundle.nr_version == "310.8.SF-v2"
    assert bundle.sr_version == "310.8.0"
    assert bundle.nr_dll_path is not None and bundle.nr_dll_path.is_file()
    assert bundle.sr_dll_path is not None and bundle.sr_dll_path.is_file()
    assert [item.args[0] for item in download.call_args_list] == [
        f"https://raw.githubusercontent.com/RankFTW/RHI/{MANIFEST_REVISION}/dlss_manifest.json",
        "https://example.com/nr.zip",
        "https://example.com/sr.zip",
    ]
    metadata.assert_called_once_with("https://api.github.com/repos/RankFTW/RHI/commits/main")


def test_fetch_ngx_requires_canonical_dll_names(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("dlss5_enabler.network.sources.get_cache_dir", return_value=tmp_path)
    mock_manifest = {
        "dlssnr": [{"version": "1.0.SF", "url": "https://example.com/nr.zip"}],
        "dlss": [{"version": "1.0", "url": "https://example.com/sr.zip"}],
    }
    mocker.patch(
        "dlss5_enabler.network.sources.http_get_json",
        return_value={"sha": MANIFEST_REVISION},
    )

    def mock_download(url: str, dest: Path | str, progress_fn: Any = None) -> Path:
        path = Path(dest)
        if url.endswith("dlss_manifest.json"):
            path.write_text(json.dumps(mock_manifest), encoding="utf-8")
        else:
            path.write_bytes(_create_mock_zip({"unrelated.dll": b"WRONG"}))
        return path

    mocker.patch("dlss5_enabler.network.sources.http_download_file", side_effect=mock_download)
    with pytest.raises(UpstreamResolutionError) as captured:
        fetch_ngx_dlls(log=lambda _message: None, force=True)
    assert isinstance(captured.value.latest, ArtifactResolutionError)
    assert captured.value.latest.code is ResolutionWarningCode.CONTENT_MISSING


def test_fetch_reshade(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("dlss5_enabler.network.sources.get_cache_dir", return_value=tmp_path)
    mocker.patch(
        "dlss5_enabler.network.sources.http_get_text",
        return_value='<a href="/downloads/ReShade_Setup_6.8.0_Addon.exe">Download</a>',
    )

    def mock_download(url: str, dest: Path | str, progress_fn: Any = None) -> Path:
        p = Path(dest)
        p.write_bytes(b"RESHADE_SETUP_EXE")
        return p

    mocker.patch("dlss5_enabler.network.sources.http_download_file", side_effect=mock_download)

    bundle = fetch_reshade(log=lambda msg: None, force=True)
    assert bundle.version == "6.8.0"
    assert bundle.setup_exe_path is not None and bundle.setup_exe_path.is_file()


def test_fetch_reshade_headers(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("dlss5_enabler.network.sources.get_cache_dir", return_value=tmp_path)
    metadata = mocker.patch(
        "dlss5_enabler.network.sources.http_get_json",
        return_value={"sha": HEADERS_REVISION},
    )

    def mock_download(url: str, dest: Path | str, progress_fn: Any = None) -> Path:
        p = Path(dest)
        p.write_bytes(b"HEADER_CONTENT")
        return p

    download = mocker.patch("dlss5_enabler.network.sources.http_download_file", side_effect=mock_download)

    bundle = fetch_reshade_headers(log=lambda msg: None, force=True)
    assert bundle.fxh_path is not None and bundle.fxh_path.is_file()
    assert bundle.ui_fxh_path is not None and bundle.ui_fxh_path.is_file()
    assert bundle.drawtext_path is not None and bundle.drawtext_path.is_file()
    assert [item.args[0] for item in download.call_args_list] == [
        f"https://raw.githubusercontent.com/crosire/reshade-shaders/{HEADERS_REVISION}/Shaders/ReShade.fxh",
        f"https://raw.githubusercontent.com/crosire/reshade-shaders/{HEADERS_REVISION}/Shaders/ReShadeUI.fxh",
        f"https://raw.githubusercontent.com/crosire/reshade-shaders/{HEADERS_REVISION}/Shaders/DrawText.fxh",
    ]
    assert [item.args[0] for item in metadata.call_args_list] == [
        "https://api.github.com/repos/crosire/reshade-shaders/commits/slim",
    ]


def test_fetch_lumenite(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("dlss5_enabler.network.sources.get_cache_dir", return_value=tmp_path)
    metadata = mocker.patch(
        "dlss5_enabler.network.sources.http_get_json",
        return_value={"sha": LUMENITE_REVISION},
    )

    def mock_download(url: str, dest: Path | str, progress_fn: Any = None) -> Path:
        zip_bytes = _create_mock_zip(
            {
                "LumeniteFX-main/Shaders/lumenite_Kernel.fx": b"LUMEN_FX",
                "LumeniteFX-main/Shaders/include/lumenite_common.fxh": b"INCLUDE",
                "LumeniteFX-main/Textures/lumenite_bluenoise256.png": b"PNG_DATA",
                "LumeniteFX-main/README.md": b"IGNORED_MD",
            }
        )
        p = Path(dest)
        p.write_bytes(zip_bytes)
        return p

    download = mocker.patch("dlss5_enabler.network.sources.http_download_file", side_effect=mock_download)

    bundle = fetch_lumenite(log=lambda msg: None, force=True)
    assert len(bundle.files) == 3
    filenames = [f.name for f in bundle.files]
    assert "lumenite_Kernel.fx" in filenames
    assert "lumenite_common.fxh" in filenames
    assert "lumenite_bluenoise256.png" in filenames
    assert download.call_count == 1
    assert download.call_args.args[0] == f"https://codeload.github.com/umar-afzaal/LumeniteFX/zip/{LUMENITE_REVISION}"
    assert [item.args[0] for item in metadata.call_args_list] == [
        "https://api.github.com/repos/umar-afzaal/LumeniteFX/commits/mainline",
    ]


def test_fetch_lumenite_rejects_traversal(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("dlss5_enabler.network.sources.get_cache_dir", return_value=tmp_path)
    mocker.patch(
        "dlss5_enabler.network.sources.http_get_json",
        return_value={"sha": LUMENITE_REVISION},
    )

    def mock_download(_url: str, dest: Path | str, progress_fn: Any = None) -> Path:
        path = Path(dest)
        path.write_bytes(_create_mock_zip({"LumeniteFX-main/Shaders/../../outside.fx": b"UNSAFE"}))
        return path

    mocker.patch("dlss5_enabler.network.sources.http_download_file", side_effect=mock_download)
    with pytest.raises(UpstreamResolutionError) as captured:
        fetch_lumenite(log=lambda _message: None, force=True)
    assert isinstance(captured.value.latest, ArtifactResolutionError)
    assert captured.value.latest.code is ResolutionWarningCode.ARCHIVE_UNSAFE
    assert not (tmp_path / "outside.fx").exists()


def test_fetch_dgvoodoo(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("dlss5_enabler.network.sources.get_cache_dir", return_value=tmp_path)
    mock_release: dict[str, Any] = {
        "tag_name": "v2.87.3",
        "assets": [
            {"name": "dgVoodoo2_87_3.zip", "browser_download_url": "https://example.com/dgvoodoo.zip"},
        ],
    }
    metadata = mocker.patch("dlss5_enabler.network.sources.http_get_json", return_value=mock_release)

    def mock_download(url: str, dest: Path | str, progress_fn: Any = None) -> Path:
        zip_bytes = _create_mock_zip(
            {
                "MS/x86/D3D9.dll": b"D3D9_BIN",
                "dgVoodoo.conf": b"CONF_DATA",
                "dgVoodooCpl.exe": b"CPL_BIN",
            }
        )
        p = Path(dest)
        p.write_bytes(zip_bytes)
        return p

    download = mocker.patch("dlss5_enabler.network.sources.http_download_file", side_effect=mock_download)

    bundle = fetch_dgvoodoo(log=lambda msg: None, force=True)
    assert bundle.d3d9_dll is not None and bundle.d3d9_dll.is_file()
    assert bundle.conf is not None and bundle.conf.is_file()
    assert bundle.cpl is not None and bundle.cpl.is_file()
    assert download.call_count == 1
    assert download.call_args.args[0] == "https://example.com/dgvoodoo.zip"
    metadata.assert_called_once_with("https://api.github.com/repos/dege-diosg/dgVoodoo2/releases/latest")


def test_fetch_dgvoodoo_uses_requested_architecture(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("dlss5_enabler.network.sources.get_cache_dir", return_value=tmp_path)
    mocker.patch(
        "dlss5_enabler.network.sources.http_get_json",
        return_value={
            "tag_name": "v2.87.3",
            "assets": [{"name": "dgVoodoo2_87_3.zip", "browser_download_url": "https://example.com/dg.zip"}],
        },
    )

    def mock_download(_url: str, dest: Path | str, progress_fn: Any = None) -> Path:
        path = Path(dest)
        path.write_bytes(
            _create_mock_zip(
                {
                    "MS/x86/D3D9.dll": b"X86",
                    "MS/x64/D3D9.dll": b"X64",
                    "dgVoodoo.conf": b"CONF",
                    "dgVoodooCpl.exe": b"CPL",
                }
            )
        )
        return path

    mocker.patch("dlss5_enabler.network.sources.http_download_file", side_effect=mock_download)
    bundle = fetch_dgvoodoo(log=lambda _message: None, force=True, architecture="x64")

    assert bundle.d3d9_dll is not None
    assert bundle.d3d9_dll.read_bytes() == b"X64"
