import hashlib
import json
import zipfile
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

import pytest

from dlss5_enabler.network.manifest import ComponentPolicy, load_upstream_manifest
from dlss5_enabler.network.resolver import (
    ArtifactCandidate,
    ArtifactOrigin,
    ArtifactResolutionError,
    ArtifactValidator,
    ResolutionWarningCode,
    UpstreamResolutionError,
    UpstreamResolver,
)


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _policy(component: str = "renodx_dlss5") -> ComponentPolicy:
    assert component == "renodx_dlss5"
    return ComponentPolicy.model_validate(
        {
            "provider": "github",
            "repository": "example/project",
            "artifact_kind": "zip",
            "discovery": {"kind": "latest_release", "asset_patterns": ["renodx*.zip"]},
            "stable_artifacts": [
                {
                    "revision": "stable",
                    "name": "renodx-stable.zip",
                    "url": "https://example.com/renodx-stable.zip",
                    "sha256": "0123456789abcdef" * 4,
                }
            ],
            "formats": [{"version": 1, "required_members": ["*renodx-dlss5.addon64"]}],
        }
    )


def _candidate(name: str = "renodx.zip", revision: str = "latest") -> ArtifactCandidate:
    return ArtifactCandidate(
        provider="github",
        revision=revision,
        name=name,
        url=f"https://example.com/{name}",
    )


def _downloader(payloads: dict[str, bytes], calls: list[str]) -> Callable[[str, Path | str, object], Path]:
    def download(url: str, destination: Path | str, _progress: object) -> Path:
        calls.append(url)
        path = Path(destination)
        path.write_bytes(payloads[url])
        return path

    return download


def test_latest_compatible_artifact_is_promoted_with_provenance(tmp_path: Path) -> None:
    calls: list[str] = []
    payload = _zip({"renodx-dlss5.addon64": b"addon"})
    latest = _candidate()
    resolver = UpstreamResolver(downloader=_downloader({latest.url: payload}, calls))

    resolved = resolver.resolve("renodx_dlss5", _policy(), tmp_path / "cached.zip", lambda: (latest,))

    assert resolved.origin is ArtifactOrigin.LATEST
    assert resolved.path.read_bytes() == payload
    assert resolved.format_version == 1
    assert resolved.warnings == ()
    assert calls == [latest.url]
    metadata = json.loads(Path(f"{resolved.path}.dlss5-enabler-cache.json").read_text(encoding="utf-8"))
    assert metadata == {
        "url": latest.url,
        "provider": "github",
        "revision": "latest",
        "asset_id": None,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "format_version": 1,
        "origin": "latest",
    }


def test_single_unexpected_zip_is_validated_by_content(tmp_path: Path) -> None:
    calls: list[str] = []
    latest = _candidate("unexpected-name.zip")
    payload = _zip({"folder/renodx-dlss5.addon64": b"addon"})
    resolver = UpstreamResolver(downloader=_downloader({latest.url: payload}, calls))

    resolved = resolver.resolve("renodx_dlss5", _policy(), tmp_path / "cached.zip", lambda: (latest,))

    assert resolved.revision == "latest"
    assert resolved.origin is ArtifactOrigin.LATEST


@pytest.mark.parametrize(
    ("candidates", "code"),
    [
        ((), ResolutionWarningCode.ASSET_MISSING),
        (
            (_candidate("renodx-one.zip"), _candidate("renodx-two.zip")),
            ResolutionWarningCode.AMBIGUOUS_ASSETS,
        ),
    ],
)
def test_asset_selection_failure_uses_stable_fallback(
    tmp_path: Path,
    candidates: tuple[ArtifactCandidate, ...],
    code: ResolutionWarningCode,
) -> None:
    calls: list[str] = []
    policy = _policy()
    stable = policy.stable_artifacts[0]
    stable_payload = _zip({"renodx-dlss5.addon64": b"stable"})
    stable_candidate = ArtifactCandidate(
        provider=policy.provider,
        revision=stable.revision,
        name=stable.name,
        url=stable.url,
        sha256=hashlib.sha256(stable_payload).hexdigest(),
    )
    policy = policy.model_copy(
        update={"stable_artifacts": (stable.model_copy(update={"sha256": stable_candidate.sha256}),)}
    )
    resolver = UpstreamResolver(downloader=_downloader({stable.url: stable_payload}, calls))

    resolved = resolver.resolve("renodx_dlss5", policy, tmp_path / "cached.zip", lambda: candidates)

    assert resolved.origin is ArtifactOrigin.STABLE_FALLBACK
    assert tuple(warning.code for warning in resolved.warnings) == (
        code,
        ResolutionWarningCode.STABLE_FALLBACK_USED,
    )


@pytest.mark.parametrize(
    ("latest_payload", "expected_code"),
    [
        (b"not-a-zip", ResolutionWarningCode.FORMAT_UNSUPPORTED),
        (_zip({"wrong.dll": b"wrong"}), ResolutionWarningCode.CONTENT_MISSING),
        (_zip({"../renodx-dlss5.addon64": b"unsafe"}), ResolutionWarningCode.ARCHIVE_UNSAFE),
    ],
)
def test_invalid_latest_uses_verified_stable_fallback(
    tmp_path: Path,
    latest_payload: bytes,
    expected_code: ResolutionWarningCode,
) -> None:
    calls: list[str] = []
    policy = _policy()
    latest = _candidate()
    stable = policy.stable_artifacts[0]
    stable_payload = _zip({"renodx-dlss5.addon64": b"stable"})
    policy = policy.model_copy(
        update={"stable_artifacts": (stable.model_copy(update={"sha256": hashlib.sha256(stable_payload).hexdigest()}),)}
    )
    resolver = UpstreamResolver(downloader=_downloader({latest.url: latest_payload, stable.url: stable_payload}, calls))

    resolved = resolver.resolve("renodx_dlss5", policy, tmp_path / "cached.zip", lambda: (latest,))

    assert resolved.path.read_bytes() == stable_payload
    assert resolved.origin is ArtifactOrigin.STABLE_FALLBACK
    assert tuple(warning.code for warning in resolved.warnings) == (
        expected_code,
        ResolutionWarningCode.STABLE_FALLBACK_USED,
    )


def test_timeout_is_classified_and_falls_back(tmp_path: Path) -> None:
    policy = _policy()
    stable = policy.stable_artifacts[0]
    stable_payload = _zip({"renodx-dlss5.addon64": b"stable"})
    policy = policy.model_copy(
        update={"stable_artifacts": (stable.model_copy(update={"sha256": hashlib.sha256(stable_payload).hexdigest()}),)}
    )

    def download(url: str, destination: Path | str, _progress: object) -> Path:
        if url != stable.url:
            raise TimeoutError("operation timed out")
        path = Path(destination)
        path.write_bytes(stable_payload)
        return path

    resolved = UpstreamResolver(downloader=download).resolve(
        "renodx_dlss5",
        policy,
        tmp_path / "cached.zip",
        lambda: (_candidate(),),
    )

    assert resolved.warnings[0].code is ResolutionWarningCode.DOWNLOAD_TIMEOUT


def test_valid_cache_is_reused_without_download(tmp_path: Path) -> None:
    calls: list[str] = []
    candidate = _candidate()
    payload = _zip({"renodx-dlss5.addon64": b"addon"})
    resolver = UpstreamResolver(downloader=_downloader({candidate.url: payload}, calls))
    destination = tmp_path / "cached.zip"
    resolver.resolve("renodx_dlss5", _policy(), destination, lambda: (candidate,))

    resolved = resolver.resolve("renodx_dlss5", _policy(), destination, lambda: (candidate,))

    assert resolved.path.read_bytes() == payload
    assert calls == [candidate.url]


def test_changed_revision_invalidates_cache(tmp_path: Path) -> None:
    calls: list[str] = []
    first = _candidate(revision="one")
    second = _candidate(revision="two")
    first_payload = _zip({"renodx-dlss5.addon64": b"one"})
    second_payload = _zip({"renodx-dlss5.addon64": b"two"})
    resolver = UpstreamResolver(downloader=_downloader({first.url: first_payload, second.url: second_payload}, calls))
    destination = tmp_path / "cached.zip"
    resolver.resolve("renodx_dlss5", _policy(), destination, lambda: (first,))

    resolved = resolver.resolve("renodx_dlss5", _policy(), destination, lambda: (second,))

    assert resolved.revision == "two"
    assert resolved.path.read_bytes() == second_payload
    assert len(calls) == 2


def test_failed_latest_and_fallback_preserve_known_good_cache(tmp_path: Path) -> None:
    calls: list[str] = []
    destination = tmp_path / "cached.zip"
    known_good = _candidate(revision="good")
    known_good_payload = _zip({"renodx-dlss5.addon64": b"good"})
    policy = _policy()
    resolver = UpstreamResolver(downloader=_downloader({known_good.url: known_good_payload}, calls))
    resolver.resolve("renodx_dlss5", policy, destination, lambda: (known_good,))
    metadata_path = Path(f"{destination}.dlss5-enabler-cache.json")
    original_metadata = metadata_path.read_bytes()
    bad_latest = _candidate(revision="bad")
    stable = policy.stable_artifacts[0]
    bad_payload = b"invalid"
    resolver = UpstreamResolver(downloader=_downloader({bad_latest.url: bad_payload, stable.url: bad_payload}, calls))

    with pytest.raises(UpstreamResolutionError):
        resolver.resolve("renodx_dlss5", policy, destination, lambda: (bad_latest,))

    assert destination.read_bytes() == known_good_payload
    assert metadata_path.read_bytes() == original_metadata


def test_fallback_digest_mismatch_is_aggregated(tmp_path: Path) -> None:
    policy = _policy()
    stable = policy.stable_artifacts[0]
    resolver = UpstreamResolver(downloader=_downloader({stable.url: b"wrong"}, []))

    with pytest.raises(UpstreamResolutionError) as captured:
        resolver.resolve("renodx_dlss5", policy, tmp_path / "cached.zip", lambda: ())

    fallback = captured.value.fallback
    assert isinstance(fallback, ArtifactResolutionError)
    assert fallback.code is ResolutionWarningCode.DIGEST_MISMATCH


def test_non_https_latest_is_rejected_before_download(tmp_path: Path) -> None:
    calls: list[str] = []
    policy = _policy()
    stable = policy.stable_artifacts[0]
    stable_payload = _zip({"renodx-dlss5.addon64": b"stable"})
    policy = policy.model_copy(
        update={"stable_artifacts": (stable.model_copy(update={"sha256": hashlib.sha256(stable_payload).hexdigest()}),)}
    )
    insecure = ArtifactCandidate(
        provider="github",
        revision="latest",
        name="renodx.zip",
        url="http://example.com/renodx.zip",
    )
    resolver = UpstreamResolver(downloader=_downloader({stable.url: stable_payload}, calls))

    resolved = resolver.resolve("renodx_dlss5", policy, tmp_path / "cached.zip", lambda: (insecure,))

    assert resolved.warnings[0].code is ResolutionWarningCode.HTTP_REJECTED
    assert calls == [stable.url]


def test_declared_format_outside_supported_range_is_rejected(tmp_path: Path) -> None:
    payload = _zip({"renodx-dlss5.addon64": b"addon"})
    path = tmp_path / "archive.zip"
    path.write_bytes(payload)
    candidate = ArtifactCandidate(
        provider="github",
        revision="latest",
        name="renodx.zip",
        url="https://example.com/renodx.zip",
        format_version=2,
    )

    with pytest.raises(ArtifactResolutionError) as captured:
        ArtifactValidator().validate(path, _policy(), candidate)

    assert captured.value.code is ResolutionWarningCode.FORMAT_UNSUPPORTED


@pytest.mark.parametrize(
    ("component", "stable_name", "architecture", "payload"),
    [
        (
            "feeder",
            None,
            None,
            _zip(
                {
                    "dlss5-feed.addon64": b"64",
                    "dlss5-feed.addon32": b"32",
                    "DLSS5_Feed.fx": b"fx",
                    "dlss5-feed-host64.exe": b"host",
                }
            ),
        ),
        ("renodx_dlss5", None, None, _zip({"renodx-dlss5.addon64": b"addon"})),
        (
            "rhi_manifest",
            "dlss_manifest.json",
            None,
            b'{"dlssnr":[{"version":"1","url":"https://example.com/nr.zip"}],'
            b'"dlss":[{"version":"1","url":"https://example.com/sr.zip"}]}',
        ),
        ("ngx_nr", None, None, _zip({"nvngx_dlssnr.dll": b"nr"})),
        ("ngx_sr", None, None, _zip({"nvngx_dlss.dll": b"sr"})),
        ("reshade_headers", "ReShade.fxh", None, b"header"),
        (
            "lumenite",
            None,
            None,
            _zip(
                {
                    "Lumenite/Shaders/lumenite_Kernel.fx": b"fx",
                    "Lumenite/Shaders/include/lumenite.fxh": b"include",
                    "Lumenite/Textures/lumenite_bluenoise256.png": b"png",
                }
            ),
        ),
        (
            "dgvoodoo2",
            None,
            "x64",
            _zip(
                {
                    "MS/x64/D3D9.dll": b"dll",
                    "dgVoodoo.conf": b"config",
                    "dgVoodooCpl.exe": b"control",
                }
            ),
        ),
        ("reshade_addon", None, None, b"setup"),
    ],
)
def test_every_upstream_policy_has_a_validated_fallback(
    tmp_path: Path,
    component: str,
    stable_name: str | None,
    architecture: str | None,
    payload: bytes,
) -> None:
    policy = load_upstream_manifest().components[component]
    selected = tuple(
        artifact
        for artifact in policy.stable_artifacts
        if stable_name is None or artifact.name.casefold() == stable_name.casefold()
    )
    assert len(selected) == 1
    stable = selected[0]
    updated_stable = stable.model_copy(update={"sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": None})
    artifacts = tuple(updated_stable if artifact is stable else artifact for artifact in policy.stable_artifacts)
    policy = policy.model_copy(update={"stable_artifacts": artifacts})
    latest = ArtifactCandidate(
        provider=policy.provider,
        revision="latest",
        name=stable.name,
        url=f"https://invalid.example/{component}/{stable.name}",
    )
    calls: list[str] = []
    resolver = UpstreamResolver(
        downloader=_downloader({latest.url: b"", stable.url: payload}, calls),
    )

    resolved = resolver.resolve(
        component,
        policy,
        tmp_path / f"{component}.artifact",
        lambda: (latest,),
        stable_name=stable_name,
        architecture=architecture,
    )

    assert resolved.component == component
    assert resolved.origin is ArtifactOrigin.STABLE_FALLBACK
    assert resolved.sha256 == hashlib.sha256(payload).hexdigest()
    assert tuple(warning.code for warning in resolved.warnings) == (
        ResolutionWarningCode.CONTENT_MISSING,
        ResolutionWarningCode.STABLE_FALLBACK_USED,
    )
    assert calls == [latest.url, stable.url]


def test_zip_validator_rejects_flattening_collision(tmp_path: Path) -> None:
    path = tmp_path / "archive.zip"
    path.write_bytes(
        _zip(
            {
                "one/renodx-dlss5.addon64": b"one",
                "two/renodx-dlss5.addon64": b"two",
            }
        )
    )

    with pytest.raises(ArtifactResolutionError) as captured:
        ArtifactValidator().validate(path, _policy(), _candidate())

    assert captured.value.code is ResolutionWarningCode.ARCHIVE_UNSAFE


def test_zip_validator_rejects_corrupt_member_data(tmp_path: Path) -> None:
    payload = bytearray(_zip({"renodx-dlss5.addon64": b"unique-payload"}))
    payload[payload.index(b"unique-payload")] ^= 0xFF
    path = tmp_path / "archive.zip"
    path.write_bytes(payload)

    with pytest.raises(ArtifactResolutionError) as captured:
        ArtifactValidator().validate(path, _policy(), _candidate())

    assert captured.value.code is ResolutionWarningCode.FORMAT_UNSUPPORTED


def test_metadata_write_failure_restores_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "cached.zip"
    first = _candidate(name="renodx-first.zip", revision="first")
    second = _candidate(name="renodx-second.zip", revision="second")
    stable_payload = _zip({"renodx-dlss5.addon64": b"stable"})
    first_payload = _zip({"renodx-dlss5.addon64": b"first"})
    second_payload = _zip({"renodx-dlss5.addon64": b"second"})
    policy = _policy()
    stable = policy.stable_artifacts[0]
    policy = policy.model_copy(
        update={"stable_artifacts": (stable.model_copy(update={"sha256": hashlib.sha256(stable_payload).hexdigest()}),)}
    )
    resolver = UpstreamResolver(
        downloader=_downloader(
            {first.url: first_payload, second.url: second_payload, stable.url: stable_payload},
            [],
        )
    )
    resolver.resolve("renodx_dlss5", policy, destination, lambda: (first,))
    metadata = Path(f"{destination}.dlss5-enabler-cache.json")
    original_metadata = metadata.read_bytes()

    def fail_metadata(_path: Path | str, _content: str, _encoding: str = "utf-8") -> None:
        raise OSError("metadata failed")

    monkeypatch.setattr(
        "dlss5_enabler.network.resolver.atomic_write_text",
        fail_metadata,
    )

    with pytest.raises(UpstreamResolutionError):
        resolver.resolve("renodx_dlss5", policy, destination, lambda: (second,), force=True)

    assert destination.read_bytes() == first_payload
    assert metadata.read_bytes() == original_metadata


def test_warning_code_contract_is_complete() -> None:
    assert {code.value for code in ResolutionWarningCode} == {
        "UPSTREAM_DISCOVERY_FAILED",
        "UPSTREAM_ASSET_MISSING",
        "UPSTREAM_AMBIGUOUS_ASSETS",
        "UPSTREAM_DOWNLOAD_TIMEOUT",
        "UPSTREAM_HTTP_REJECTED",
        "UPSTREAM_DIGEST_MISMATCH",
        "UPSTREAM_ARCHIVE_UNSAFE",
        "UPSTREAM_CONTENT_MISSING",
        "UPSTREAM_FORMAT_UNSUPPORTED",
        "UPSTREAM_STABLE_FALLBACK_USED",
        "UPSTREAM_FALLBACK_FAILED",
    }
