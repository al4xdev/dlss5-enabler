from typing import Any

import pytest

from dlss5_enabler.network.adapters import (
    GitHubDownloadSourceAdapter,
    get_download_source_adapter,
    validate_download_url,
)


def test_github_adapter_discovers_latest_release_asset() -> None:
    def fetch_json(_url: str) -> Any:
        return {
            "tag_name": "v1.2.3",
            "assets": [
                {"name": "checksums.txt", "browser_download_url": "https://example.com/checksums.txt"},
                {
                    "id": 123,
                    "name": "package.zip",
                    "browser_download_url": "https://example.com/package.zip",
                    "size": 456,
                    "digest": "sha256:" + "a" * 64,
                },
            ],
        }

    release = GitHubDownloadSourceAdapter(fetch_json).latest_release("owner/project")
    asset = release.require_asset(("*.zip",), "project")

    assert release.tag == "v1.2.3"
    assert asset.name == "package.zip"
    assert asset.url == "https://example.com/package.zip"
    assert asset.revision == "v1.2.3"
    assert asset.asset_id == 123
    assert asset.size_bytes == 456
    assert asset.sha256 == "a" * 64


def test_github_adapter_reports_published_assets_when_match_is_missing() -> None:
    def fetch_json(_url: str) -> Any:
        return {
            "tag_name": "v1.0.0",
            "assets": [
                {"name": "checksums.txt", "browser_download_url": "https://example.com/checksums.txt"},
            ],
        }

    release = GitHubDownloadSourceAdapter(fetch_json).latest_release("owner/project")

    with pytest.raises(RuntimeError, match=r"published assets: checksums\.txt"):
        release.require_asset(("*.zip",), "project")


def test_github_adapter_rejects_invalid_asset_url() -> None:
    def fetch_json(_url: str) -> Any:
        return {
            "tag_name": "v1.0.0",
            "assets": [{"name": "package.zip", "browser_download_url": "http://example.com/package.zip"}],
        }

    with pytest.raises(RuntimeError, match="valid HTTPS"):
        GitHubDownloadSourceAdapter(fetch_json).latest_release("owner/project")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("id", 0, "invalid id"),
        ("size", "large", "invalid size"),
        ("digest", "md5:bad", "invalid digest"),
    ],
)
def test_github_adapter_rejects_invalid_asset_provenance(field: str, value: object, message: str) -> None:
    def fetch_json(_url: str) -> Any:
        asset: dict[str, object] = {
            "name": "package.zip",
            "browser_download_url": "https://example.com/package.zip",
        }
        asset[field] = value
        return {"tag_name": "v1.0.0", "assets": [asset]}

    with pytest.raises(TypeError, match=message):
        GitHubDownloadSourceAdapter(fetch_json).latest_release("owner/project")


def test_github_adapter_lists_releases() -> None:
    requested: list[str] = []

    def fetch_json(url: str) -> Any:
        requested.append(url)
        return [
            {
                "tag_name": "v2.0.0",
                "assets": [{"name": "package.zip", "browser_download_url": "https://example.com/v2.zip"}],
            }
        ]

    releases = GitHubDownloadSourceAdapter(fetch_json).releases("owner/project", per_page=25)

    assert releases[0].tag == "v2.0.0"
    assert requested == ["https://api.github.com/repos/owner/project/releases?per_page=25"]


def test_github_adapter_builds_immutable_repository_sources() -> None:
    revision = "abc123def456abc123def456abc123def456abcd"
    responses: list[Any] = [{"default_branch": "mainline"}, {"sha": revision}]

    def fetch_json(_url: str) -> Any:
        return responses.pop(0)

    adapter = GitHubDownloadSourceAdapter(fetch_json)
    snapshot = adapter.repository_snapshot("owner/project")
    archive = adapter.repository_archive(snapshot)
    source_file = adapter.repository_file(snapshot.repository, snapshot.revision, "Shaders/ReShade.fxh")

    assert snapshot.branch == "mainline"
    assert snapshot.revision == revision
    assert archive.url == f"https://codeload.github.com/owner/project/zip/{revision}"
    assert source_file.url == f"https://raw.githubusercontent.com/owner/project/{revision}/Shaders/ReShade.fxh"


def test_github_adapter_rejects_mutable_repository_reference() -> None:
    adapter = GitHubDownloadSourceAdapter(lambda _url: {"sha": "main"})

    with pytest.raises(RuntimeError, match="immutable commit revision"):
        adapter.repository_snapshot("owner/project", "main")
    with pytest.raises(ValueError, match="immutable 40-character commit"):
        adapter.repository_file("owner/project", "main", "file.txt")


def test_download_source_adapter_factory_is_extensible() -> None:
    adapter = get_download_source_adapter("github", lambda _url: {})

    assert adapter.name == "github"
    with pytest.raises(ValueError, match="Unknown download source adapter"):
        get_download_source_adapter("mirror", lambda _url: {})


@pytest.mark.parametrize(
    "url",
    ["", "http://example.com/file.zip", "not-a-url", "https://token@example.com/file.zip"],
)
def test_validate_download_url_rejects_non_https(url: str) -> None:
    with pytest.raises(RuntimeError, match="valid HTTPS"):
        validate_download_url(url, "component")
