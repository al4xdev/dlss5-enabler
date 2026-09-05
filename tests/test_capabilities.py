import struct
from pathlib import Path

import pytest

from dlss5_enabler.core.pe import DetectedApi, PeArch
from dlss5_enabler.core.record import InstallRecord, RecordedFile, record_save
from dlss5_enabler.core.util import sha256_file
from dlss5_enabler.operations.capabilities import (
    CapabilityLimits,
    EvidenceLevel,
    EvidenceOrigin,
    TemporalInput,
    analyze_capabilities,
)


def _pe(imports: tuple[str, ...] = (), *, x86: bool = False) -> bytes:
    data = bytearray(4096)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 64)
    data[64:68] = b"PE\0\0"
    struct.pack_into("<H", data, 68, 0x14C if x86 else 0x8664)
    struct.pack_into("<H", data, 70, 1)
    optional_size = 224 if x86 else 240
    struct.pack_into("<H", data, 84, optional_size)
    struct.pack_into("<H", data, 88, 0x10B if x86 else 0x20B)
    struct.pack_into("<II", data, 88 + (104 if x86 else 120), 0x1000, (len(imports) + 1) * 20)
    section = 88 + optional_size
    data[section : section + 6] = b".idata"
    struct.pack_into("<IIII", data, section + 8, 3584, 0x1000, 3584, 512)
    names = (len(imports) + 1) * 20
    for index, name in enumerate(imports):
        struct.pack_into("<I", data, 512 + index * 20 + 12, 0x1000 + names)
        encoded = name.encode("ascii") + b"\0"
        data[512 + names : 512 + names + len(encoded)] = encoded
        names += len(encoded)
    return bytes(data)


def _write_module(path: Path, imports: tuple[str, ...] = (), *, x86: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_pe(imports, x86=x86))
    return path


def _record(game: Path, files: list[RecordedFile]) -> InstallRecord:
    return InstallRecord(game_exe=game.as_posix(), game_dir=game.parent.as_posix(), files=files)


@pytest.mark.parametrize(
    "runtime, feature",
    [
        ("NVNGX_DLSS.DLL", TemporalInput.DLSS),
        ("libxess.dll", TemporalInput.XESS),
        ("libxess_dx11.dll", TemporalInput.XESS),
        ("ffx_fsr2_api_x64.dll", TemporalInput.FSR2_PLUS),
        ("ffx_fsr3upscaler_x64.dll", TemporalInput.FSR2_PLUS),
        ("amd_fidelityfx_upscaler_dx12.dll", TemporalInput.FSR2_PLUS),
    ],
)
def test_direct_temporal_import_is_static_integration_evidence(
    tmp_path: Path, runtime: str, feature: TemporalInput
) -> None:
    game = _write_module(tmp_path / "game.exe", ("d3d11.dll", runtime))

    result = analyze_capabilities(game)

    assert result.architecture is PeArch.X64
    assert result.apis == (DetectedApi.D3D11,)
    assert result.temporal_inputs == (feature,)
    assert result.has_reliable_temporal_input
    evidence = next(item for item in result.temporal_evidence if item.feature is feature)
    assert evidence.level is EvidenceLevel.DIRECT_IMPORT
    assert evidence.path == game
    assert "execution are not verified" in evidence.reason


@pytest.mark.parametrize("runtime", ["nvngx_dlss.dll", "libxess.dll", "ffx_fsr2_api_x64.dll"])
def test_residual_runtime_dll_does_not_prove_game_integration(tmp_path: Path, runtime: str) -> None:
    game = _write_module(tmp_path / "game.exe", ("d3d11.dll",))
    _write_module(tmp_path / runtime)

    result = analyze_capabilities(game)

    assert result.complete
    assert not result.has_reliable_temporal_input
    assert result.temporal_evidence[0].level is EvidenceLevel.FILE_ONLY


@pytest.mark.parametrize("runtime", ["ffx_fsr1_api_x64.dll", "nvngx_dlssg.dll", "nvngx_dlssnr.dll"])
def test_fsr1_frame_generation_and_nr_are_not_temporal_inputs(tmp_path: Path, runtime: str) -> None:
    game = _write_module(tmp_path / "game.exe", (runtime,))
    _write_module(tmp_path / runtime)

    result = analyze_capabilities(game)

    assert result.temporal_inputs == ()
    assert result.temporal_evidence == ()


def test_shared_fidelityfx_library_does_not_prove_temporal_upscaling(tmp_path: Path) -> None:
    game = _write_module(tmp_path / "game.exe", ("amd_fidelityfx_dx12.dll",))
    _write_module(tmp_path / "amd_fidelityfx_dx12.dll")

    result = analyze_capabilities(game)

    assert not result.has_reliable_temporal_input
    assert result.temporal_evidence[0].level is EvidenceLevel.UNKNOWN
    assert any("does not prove FSR2+" in warning for warning in result.warnings)


def test_crash_shape_ignores_installer_dlss_and_proxy_without_writing(tmp_path: Path) -> None:
    game = _write_module(tmp_path / "game.exe", ("d3d11.dll",))
    sr = _write_module(tmp_path / "nvngx_dlss.dll")
    nr = _write_module(tmp_path / "nvngx_dlssnr.dll")
    proxy = _write_module(tmp_path / "dxgi.dll", ("nvngx_dlss.dll", "d3d12.dll"))
    record = _record(game, [RecordedFile(path=path.as_posix(), sha256=sha256_file(path)) for path in (sr, nr, proxy)])
    assert record_save(record)
    before = {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}

    result = analyze_capabilities(game)

    assert result.complete
    assert result.apis == (DetectedApi.D3D11,)
    assert not result.has_reliable_temporal_input
    assert len(result.temporal_evidence) == 1
    assert result.temporal_evidence[0].level is EvidenceLevel.INSTALLER_OWNED
    assert result.temporal_evidence[0].path == sr
    assert {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()} == before


def test_black_mesa_shape_follows_local_engine_dependency_for_d3d9(tmp_path: Path) -> None:
    game = _write_module(tmp_path / "game.exe", ("engine.dll",), x86=True)
    _write_module(tmp_path / "bin" / "engine.dll", ("shaderapi.dll",), x86=True)
    shader = _write_module(tmp_path / "bin" / "shaderapi.dll", ("d3d9.dll",), x86=True)
    _write_module(tmp_path / "bin" / "unused64.dll", ("d3d12.dll",))

    result = analyze_capabilities(game)

    assert result.architecture is PeArch.X86
    assert result.apis == (DetectedApi.D3D9,)
    assert result.api_evidence[0].level is EvidenceLevel.DEPENDENCY_IMPORT
    assert result.api_evidence[0].path == shader
    assert not result.has_reliable_temporal_input


def test_dynamically_loaded_engine_candidate_is_weaker_than_executable_import(tmp_path: Path) -> None:
    game = _write_module(tmp_path / "game.exe", x86=True)
    shader = _write_module(tmp_path / "bin" / "shaderapi.dll", ("d3d9.dll", "libxess.dll"), x86=True)

    result = analyze_capabilities(game)

    assert result.apis == (DetectedApi.D3D9,)
    assert result.api_evidence[0].path == shader
    assert result.api_evidence[0].level is EvidenceLevel.MODULE_IMPORT
    assert result.temporal_evidence[0].level is EvidenceLevel.MODULE_IMPORT
    assert not result.has_reliable_temporal_input


def test_unknown_backup_is_not_native_temporal_proof(tmp_path: Path) -> None:
    game = _write_module(tmp_path / "game.exe", ("d3d11.dll",))
    runtime = _write_module(tmp_path / "nvngx_dlss.dll")
    backup = tmp_path / "nvngx_dlss.dll.bak"
    backup.write_bytes(_pe())
    record = _record(
        game, [RecordedFile(path=runtime.as_posix(), backup=backup.as_posix(), sha256=sha256_file(runtime))]
    )

    result = analyze_capabilities(game, record)

    assert result.complete
    assert not result.has_reliable_temporal_input
    assert result.temporal_evidence[0].origin is EvidenceOrigin.BACKUP
    assert result.temporal_evidence[0].level is EvidenceLevel.UNKNOWN


def test_managed_host_copy_does_not_suppress_independent_game_import_or_plugin_file(tmp_path: Path) -> None:
    game = _write_module(tmp_path / "Binaries" / "Win64" / "game.exe", ("nvngx_dlss.dll",))
    managed = _write_module(game.parent / "host64" / "nvngx_dlss.dll")
    plugin = _write_module(tmp_path / "Engine" / "Plugins" / "DLSS" / "Binaries" / "nvngx_dlss.dll")
    record = _record(game, [RecordedFile(path=managed.as_posix())])

    result = analyze_capabilities(game, record)

    assert result.complete
    assert result.temporal_inputs == (TemporalInput.DLSS,)
    assert any(
        item.path == managed and item.level is EvidenceLevel.INSTALLER_OWNED for item in result.temporal_evidence
    )
    assert any(item.path == plugin and item.level is EvidenceLevel.FILE_ONLY for item in result.temporal_evidence)


def test_managed_source_cannot_supply_imports_as_game_dependency(tmp_path: Path) -> None:
    game = _write_module(tmp_path / "game.exe", ("wrapper.dll",))
    wrapper = _write_module(tmp_path / "wrapper.dll", ("nvngx_dlss.dll",))

    result = analyze_capabilities(game, _record(game, [RecordedFile(path=wrapper.as_posix())]))

    assert result.temporal_evidence == ()
    assert not result.has_reliable_temporal_input


def test_unknown_proxy_imports_are_not_promoted_to_native_game_integration(tmp_path: Path) -> None:
    game = _write_module(tmp_path / "game.exe", ("dxgi.dll",))
    _write_module(tmp_path / "dxgi.dll", ("nvngx_dlss.dll",))

    result = analyze_capabilities(game)

    assert result.temporal_evidence[0].level is EvidenceLevel.MODULE_IMPORT
    assert not result.has_reliable_temporal_input


@pytest.mark.parametrize("imported", ["dxgi.dll", "d3d10.dll"])
def test_dxgi_and_d3d10_do_not_establish_d3d11(tmp_path: Path, imported: str) -> None:
    game = _write_module(tmp_path / "game.exe", (imported,))

    assert analyze_capabilities(game).apis == ()


def test_missing_backup_and_changed_managed_bytes_are_reported_as_unknown(tmp_path: Path) -> None:
    game = _write_module(tmp_path / "game.exe")
    sr = _write_module(tmp_path / "nvngx_dlss.dll")
    xess = _write_module(tmp_path / "libxess.dll")
    record = _record(
        game,
        [
            RecordedFile(path=sr.as_posix(), backup=(tmp_path / "missing.bak").as_posix()),
            RecordedFile(path=xess.as_posix(), sha256="a" * 64),
        ],
    )

    result = analyze_capabilities(game, record)

    assert not result.complete
    assert not result.has_reliable_temporal_input
    assert all(item.level is EvidenceLevel.UNKNOWN for item in result.temporal_evidence)
    assert any("backup" in warning for warning in result.warnings)
    assert any("digest" in warning for warning in result.warnings)


def test_unreadable_record_prevents_reliable_ownership_conclusion(tmp_path: Path) -> None:
    game = _write_module(tmp_path / "game.exe", ("nvngx_dlss.dll",))
    record_path = tmp_path / "dlss5-enabler.install.json"
    record_path.write_bytes(b'{"schema_version":999}')

    result = analyze_capabilities(game)

    assert not result.complete
    assert not result.has_reliable_temporal_input
    assert record_path.read_bytes() == b'{"schema_version":999}'


@pytest.mark.parametrize("x86", [False, True])
def test_wrong_runtime_architecture_is_unknown(tmp_path: Path, x86: bool) -> None:
    game = _write_module(tmp_path / "game.exe", x86=x86)
    _write_module(tmp_path / "libxess.dll", x86=not x86)

    result = analyze_capabilities(game)

    assert result.temporal_evidence[0].level is EvidenceLevel.UNKNOWN
    assert "architecture" in result.temporal_evidence[0].reason


def test_ambiguous_dependency_does_not_promote_either_module(tmp_path: Path) -> None:
    game = _write_module(tmp_path / "game.exe", ("renderer.dll",))
    _write_module(tmp_path / "renderer.dll", ("nvngx_dlss.dll",))
    _write_module(tmp_path / "bin" / "renderer.dll", ("libxess.dll",))

    result = analyze_capabilities(game)

    assert all(item.level is EvidenceLevel.MODULE_IMPORT for item in result.temporal_evidence)
    assert not result.has_reliable_temporal_input


def test_dependency_cycles_terminate_and_keep_provenance(tmp_path: Path) -> None:
    game = _write_module(tmp_path / "game.exe", ("renderer.dll",))
    _write_module(tmp_path / "renderer.dll", ("helper.dll",))
    _write_module(tmp_path / "helper.dll", ("renderer.dll", "libxess.dll"))

    result = analyze_capabilities(game)

    assert result.temporal_inputs == (TemporalInput.XESS,)
    assert result.temporal_evidence[0].level is EvidenceLevel.DEPENDENCY_IMPORT


@pytest.mark.parametrize(
    "limits",
    [
        CapabilityLimits(max_files=1),
        CapabilityLimits(max_directories=1),
        CapabilityLimits(max_directory_entries=1),
        CapabilityLimits(max_module_bytes=32),
        CapabilityLimits(max_depth=1),
    ],
)
def test_scan_limits_are_explicit_and_prevent_confident_conclusions(tmp_path: Path, limits: CapabilityLimits) -> None:
    game = _write_module(tmp_path / "game.exe", ("libxess.dll",))
    _write_module(tmp_path / "bin" / "deep" / "helper.dll")
    _write_module(tmp_path / "extra.dll")

    result = analyze_capabilities(game, limits=limits)

    assert not result.complete
    assert not result.has_reliable_temporal_input
    assert any("limit" in warning for warning in result.warnings)


def test_invalid_target_returns_incomplete_diagnostics(tmp_path: Path) -> None:
    game = tmp_path / "game.exe"
    game.write_bytes(b"invalid executable")

    result = analyze_capabilities(game)

    assert result.architecture is PeArch.UNKNOWN
    assert not result.complete
    assert result.apis == ()


def test_limits_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        CapabilityLimits(max_files=0)
