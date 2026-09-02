import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any, cast
from urllib.parse import quote, urlparse

JsonFetcher = Callable[[str], Any]


def _validate_repository(repository: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError(f"Invalid source repository: {repository}")
    return repository


def validate_download_url(value: object, component: str) -> str:
    url = str(value)
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError(f"{component} has no valid HTTPS download URL")
    return url


@dataclass(frozen=True)
class SourceAsset:
    name: str
    url: str
    revision: str


@dataclass(frozen=True)
class SourceRelease:
    tag: str
    assets: tuple[SourceAsset, ...]

    def find_asset(self, patterns: Sequence[str]) -> SourceAsset | None:
        for pattern in patterns:
            for asset in self.assets:
                if fnmatch(asset.name.lower(), pattern.lower()):
                    return asset
        return None

    def require_asset(self, patterns: Sequence[str], component: str) -> SourceAsset:
        asset = self.find_asset(patterns)
        if asset is None:
            expected = ", ".join(patterns)
            published = ", ".join(asset.name for asset in self.assets) or "none"
            raise RuntimeError(
                f"{component} release {self.tag} has no compatible asset ({expected}); published assets: {published}"
            )
        return asset


@dataclass(frozen=True)
class RepositorySnapshot:
    repository: str
    branch: str
    revision: str


class DownloadSourceAdapter(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @abstractmethod
    def latest_release(self, repository: str) -> SourceRelease:
        pass

    @abstractmethod
    def releases(self, repository: str, per_page: int = 100) -> tuple[SourceRelease, ...]:
        pass

    @abstractmethod
    def repository_snapshot(self, repository: str, branch: str | None = None) -> RepositorySnapshot:
        pass

    @abstractmethod
    def repository_file(self, repository: str, reference: str, relative_path: str) -> SourceAsset:
        pass

    @abstractmethod
    def repository_archive(self, snapshot: RepositorySnapshot) -> SourceAsset:
        pass


class GitHubDownloadSourceAdapter(DownloadSourceAdapter):
    def __init__(self, json_fetcher: JsonFetcher) -> None:
        self._json_fetcher = json_fetcher

    @property
    def name(self) -> str:
        return "github"

    def latest_release(self, repository: str) -> SourceRelease:
        repository = _validate_repository(repository)
        raw: object = self._json_fetcher(f"https://api.github.com/repos/{repository}/releases/latest")
        if not isinstance(raw, dict):
            raise TypeError(f"GitHub latest release response for {repository} is not an object")
        return self._parse_release(cast(dict[str, Any], raw), repository)

    def releases(self, repository: str, per_page: int = 100) -> tuple[SourceRelease, ...]:
        repository = _validate_repository(repository)
        if per_page < 1 or per_page > 100:
            raise ValueError("GitHub releases per_page must be between 1 and 100")
        raw: object = self._json_fetcher(f"https://api.github.com/repos/{repository}/releases?per_page={per_page}")
        if not isinstance(raw, list):
            raise TypeError(f"GitHub releases response for {repository} is not an array")
        releases: list[SourceRelease] = []
        raw_releases = cast(list[object], raw)
        for item in raw_releases:
            if not isinstance(item, dict):
                raise TypeError(f"GitHub release entry for {repository} is not an object")
            releases.append(self._parse_release(cast(dict[str, Any], item), repository))
        return tuple(releases)

    def repository_snapshot(self, repository: str, branch: str | None = None) -> RepositorySnapshot:
        repository = _validate_repository(repository)
        selected_branch = branch
        if selected_branch is None:
            raw_meta: object = self._json_fetcher(f"https://api.github.com/repos/{repository}")
            if not isinstance(raw_meta, dict):
                raise RuntimeError(f"GitHub repository response for {repository} is not an object")
            meta = cast(dict[str, Any], raw_meta)
            selected_branch = str(meta.get("default_branch", ""))
        if not selected_branch:
            raise RuntimeError(f"GitHub repository {repository} has no default branch")
        raw_commit: object = self._json_fetcher(
            f"https://api.github.com/repos/{repository}/commits/{quote(selected_branch, safe='')}"
        )
        if not isinstance(raw_commit, dict):
            raise TypeError(f"GitHub commit response for {repository}@{selected_branch} is not an object")
        commit = cast(dict[str, Any], raw_commit)
        revision = str(commit.get("sha", ""))
        if not revision:
            raise RuntimeError(f"GitHub repository {repository}@{selected_branch} has no commit revision")
        return RepositorySnapshot(repository=repository, branch=selected_branch, revision=revision)

    def repository_file(self, repository: str, reference: str, relative_path: str) -> SourceAsset:
        repository = _validate_repository(repository)
        if not reference:
            raise ValueError("GitHub file reference cannot be empty")
        path_parts = relative_path.replace("\\", "/").split("/")
        if not relative_path or relative_path.startswith("/") or any(part in {"", ".", ".."} for part in path_parts):
            raise ValueError(f"Invalid repository file path: {relative_path}")
        encoded_reference = quote(reference, safe="")
        encoded_path = "/".join(quote(part, safe="") for part in path_parts)
        url = f"https://raw.githubusercontent.com/{repository}/{encoded_reference}/{encoded_path}"
        return SourceAsset(name=path_parts[-1], url=url, revision=reference)

    def repository_archive(self, snapshot: RepositorySnapshot) -> SourceAsset:
        repository = _validate_repository(snapshot.repository)
        if not snapshot.revision:
            raise ValueError("Repository archive revision cannot be empty")
        revision = quote(snapshot.revision, safe="")
        return SourceAsset(
            name=f"{repository.rsplit('/', 1)[1]}-{snapshot.revision[:12]}.zip",
            url=f"https://codeload.github.com/{repository}/zip/{revision}",
            revision=snapshot.revision,
        )

    @staticmethod
    def _parse_release(data: dict[str, Any], repository: str) -> SourceRelease:
        tag = str(data.get("tag_name", ""))
        if not tag:
            raise RuntimeError(f"GitHub release for {repository} has no tag_name")
        raw_assets: object = data.get("assets", [])
        if not isinstance(raw_assets, list):
            raise TypeError(f"GitHub release {tag} assets for {repository} is not an array")
        assets: list[SourceAsset] = []
        asset_entries = cast(list[object], raw_assets)
        for raw_asset in asset_entries:
            if not isinstance(raw_asset, dict):
                raise TypeError(f"GitHub release {tag} contains an invalid asset entry")
            asset = cast(dict[str, Any], raw_asset)
            asset_name = str(asset.get("name", ""))
            if not asset_name:
                raise RuntimeError(f"GitHub release {tag} contains an asset without a name")
            asset_url = validate_download_url(asset.get("browser_download_url", ""), f"GitHub asset {asset_name}")
            assets.append(SourceAsset(name=asset_name, url=asset_url, revision=tag))
        return SourceRelease(tag=tag, assets=tuple(assets))


def get_download_source_adapter(name: str, json_fetcher: JsonFetcher) -> DownloadSourceAdapter:
    if name.lower() == "github":
        return GitHubDownloadSourceAdapter(json_fetcher)
    raise ValueError(f"Unknown download source adapter: {name}")
