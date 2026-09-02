import struct
from pathlib import Path
from unittest.mock import patch

from dlss5_enabler.core.pe import (
    IMAGE_DOS_SIGNATURE,
    IMAGE_FILE_MACHINE_AMD64,
    IMAGE_FILE_MACHINE_ARM64,
    IMAGE_FILE_MACHINE_I386,
    IMAGE_NT_SIGNATURE,
    DetectedApi,
    PeArch,
    check_api_mismatches,
    detect_game_apis,
    detect_imported_dlls,
    detect_pe_arch,
)


def _create_mock_pe(
    machine: int = IMAGE_FILE_MACHINE_AMD64,
    valid_dos: bool = True,
    valid_nt: bool = True,
    imported_dlls: list[str] | None = None,
) -> bytes:
    dos_header = bytearray(64)
    if valid_dos:
        struct.pack_into("<H", dos_header, 0, IMAGE_DOS_SIGNATURE)
    struct.pack_into("<I", dos_header, 0x3C, 64)

    nt_signature = struct.pack("<I", IMAGE_NT_SIGNATURE if valid_nt else 0x12345678)
    num_sections = 1 if imported_dlls else 0
    size_of_opt_hdr = 240
    file_header = bytearray(20)
    struct.pack_into("<H", file_header, 0, machine)
    struct.pack_into("<H", file_header, 2, num_sections)
    struct.pack_into("<H", file_header, 16, size_of_opt_hdr)

    opt_header = bytearray(size_of_opt_hdr)
    struct.pack_into("<H", opt_header, 0, 0x20B)  # PE32+

    if not imported_dlls:
        return bytes(dos_header + nt_signature + file_header + opt_header)

    section_va = 0x1000
    section_raw_ptr = 512
    import_rva = section_va

    struct.pack_into("<I", opt_header, 120, import_rva)
    struct.pack_into("<I", opt_header, 124, 1024)

    sec_hdr = bytearray(40)
    sec_hdr[0:5] = b".rdata"
    struct.pack_into("<I", sec_hdr, 8, 4096)  # VirtualSize
    struct.pack_into("<I", sec_hdr, 12, section_va)  # VirtualAddress
    struct.pack_into("<I", sec_hdr, 16, 4096)  # SizeOfRawData
    struct.pack_into("<I", sec_hdr, 20, section_raw_ptr)  # PointerToRawData

    headers_blob = bytes(dos_header + nt_signature + file_header + opt_header + sec_hdr)
    padding = b"\x00" * (section_raw_ptr - len(headers_blob))

    descriptors_blob = bytearray()
    names_blob = bytearray()

    name_offset_in_sec = (len(imported_dlls) + 1) * 20
    for dll in imported_dlls:
        desc = bytearray(20)
        dll_name_rva = section_va + name_offset_in_sec + len(names_blob)
        struct.pack_into("<I", desc, 12, dll_name_rva)
        descriptors_blob.extend(desc)
        names_blob.extend(dll.encode("ascii") + b"\x00")

    descriptors_blob.extend(b"\x00" * 20)  # Null terminator descriptor
    section_data = bytes(descriptors_blob + names_blob)
    section_padding = b"\x00" * (4096 - len(section_data))

    return headers_blob + padding + section_data + section_padding


def test_detect_pe_arch_non_existent_file(tmp_path: Path) -> None:
    non_existent = tmp_path / "does_not_exist.exe"
    assert detect_pe_arch(non_existent) == PeArch.UNKNOWN


def test_detect_pe_arch_directory(tmp_path: Path) -> None:
    assert detect_pe_arch(tmp_path) == PeArch.UNKNOWN


def test_detect_pe_arch_truncated_dos_header(tmp_path: Path) -> None:
    target = tmp_path / "truncated_dos.exe"
    target.write_bytes(b"MZ" + b"\x00" * 10)
    assert detect_pe_arch(target) == PeArch.UNKNOWN


def test_detect_pe_arch_invalid_dos_magic(tmp_path: Path) -> None:
    target = tmp_path / "invalid_magic.exe"
    target.write_bytes(_create_mock_pe(valid_dos=False))
    assert detect_pe_arch(target) == PeArch.UNKNOWN


def test_detect_pe_arch_truncated_nt_header(tmp_path: Path) -> None:
    target = tmp_path / "truncated_nt.exe"
    dos_header = bytearray(64)
    struct.pack_into("<H", dos_header, 0, IMAGE_DOS_SIGNATURE)
    struct.pack_into("<I", dos_header, 0x3C, 64)
    target.write_bytes(bytes(dos_header) + b"PE")
    assert detect_pe_arch(target) == PeArch.UNKNOWN


def test_detect_pe_arch_invalid_nt_signature(tmp_path: Path) -> None:
    target = tmp_path / "invalid_nt.exe"
    target.write_bytes(_create_mock_pe(valid_nt=False))
    assert detect_pe_arch(target) == PeArch.UNKNOWN


def test_detect_pe_arch_truncated_file_header(tmp_path: Path) -> None:
    target = tmp_path / "truncated_file_hdr.exe"
    dos_header = bytearray(64)
    struct.pack_into("<H", dos_header, 0, IMAGE_DOS_SIGNATURE)
    struct.pack_into("<I", dos_header, 0x3C, 64)
    nt_signature = struct.pack("<I", IMAGE_NT_SIGNATURE)
    target.write_bytes(bytes(dos_header) + nt_signature + b"\x00" * 5)
    assert detect_pe_arch(target) == PeArch.UNKNOWN


def test_detect_pe_arch_x86(tmp_path: Path) -> None:
    target = tmp_path / "game_x86.exe"
    target.write_bytes(_create_mock_pe(machine=IMAGE_FILE_MACHINE_I386))
    assert detect_pe_arch(target) == PeArch.X86


def test_detect_pe_arch_x64(tmp_path: Path) -> None:
    target = tmp_path / "game_x64.exe"
    target.write_bytes(_create_mock_pe(machine=IMAGE_FILE_MACHINE_AMD64))
    assert detect_pe_arch(target) == PeArch.X64


def test_detect_pe_arch_arm64(tmp_path: Path) -> None:
    target = tmp_path / "game_arm64.exe"
    target.write_bytes(_create_mock_pe(machine=IMAGE_FILE_MACHINE_ARM64))
    assert detect_pe_arch(target) == PeArch.ARM64


def test_detect_pe_arch_unknown_machine(tmp_path: Path) -> None:
    target = tmp_path / "game_unknown.exe"
    target.write_bytes(_create_mock_pe(machine=0x9999))
    assert detect_pe_arch(target) == PeArch.UNKNOWN


def test_detect_pe_arch_exception_handling(tmp_path: Path) -> None:
    target = tmp_path / "error.exe"
    target.write_bytes(b"MZdummy")

    with patch.object(Path, "open", side_effect=OSError("Read error")):
        assert detect_pe_arch(target) == PeArch.UNKNOWN


def test_detect_imported_dlls_and_apis(tmp_path: Path) -> None:
    target = tmp_path / "dx12_game.exe"
    target.write_bytes(_create_mock_pe(imported_dlls=["d3d12.dll", "dxgi.dll", "kernel32.dll"]))

    dlls = detect_imported_dlls(target)
    assert "d3d12.dll" in dlls
    assert "dxgi.dll" in dlls
    assert "kernel32.dll" in dlls

    apis = detect_game_apis(target)
    assert DetectedApi.D3D12 in apis
    assert DetectedApi.D3D11 in apis


def test_detect_imported_dlls_missing_file(tmp_path: Path) -> None:
    assert detect_imported_dlls(tmp_path / "missing.exe") == []


def test_check_api_mismatches_warnings(tmp_path: Path) -> None:
    # DX12 game where user wrongly passes --d3d9
    dx12_target = tmp_path / "dx12.exe"
    dx12_target.write_bytes(_create_mock_pe(imported_dlls=["d3d12.dll", "dxgi.dll"]))

    w1 = check_api_mismatches(dx12_target, d3d9=True, opengl=False, vulkan_layer=False)
    assert len(w1) == 1
    assert "Flag --d3d9 was specified" in w1[0]

    # DX12 game where user wrongly passes --opengl
    w2 = check_api_mismatches(dx12_target, d3d9=False, opengl=True, vulkan_layer=False)
    assert len(w2) == 1
    assert "Flag --opengl was specified" in w2[0]

    # D3D9 game where user forgets --d3d9
    d3d9_target = tmp_path / "d3d9.exe"
    d3d9_target.write_bytes(_create_mock_pe(imported_dlls=["d3d9.dll", "user32.dll"]))
    w3 = check_api_mismatches(d3d9_target, d3d9=False, opengl=False, vulkan_layer=False)
    assert len(w3) == 1
    assert "Consider passing --d3d9" in w3[0]

    # OpenGL game where user forgets --opengl
    gl_target = tmp_path / "gl.exe"
    gl_target.write_bytes(_create_mock_pe(imported_dlls=["opengl32.dll", "user32.dll"]))
    w4 = check_api_mismatches(gl_target, d3d9=False, opengl=False, vulkan_layer=False)
    assert len(w4) == 1
    assert "Consider passing --opengl" in w4[0]
