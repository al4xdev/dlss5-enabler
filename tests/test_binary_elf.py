import struct
from pathlib import Path

from dlss5_enabler.core.pe import (
    BinaryFormat,
    PeArch,
    check_api_mismatches,
    detect_binary_format,
    detect_elf_arch,
)


def _create_mock_elf(machine: int = 0x3E, is_64bit: bool = True, little_endian: bool = True) -> bytes:
    header = bytearray(20)
    header[0:4] = b"\x7fELF"
    header[4] = 2 if is_64bit else 1
    header[5] = 1 if little_endian else 2
    header[6] = 1  # version

    endian = "<" if little_endian else ">"
    struct.pack_into(f"{endian}H", header, 18, machine)
    return bytes(header)


def test_detect_binary_format(tmp_path: Path) -> None:
    pe_file = tmp_path / "game.exe"
    pe_file.write_bytes(b"MZ\x90\x00" + b"\x00" * 60)
    assert detect_binary_format(pe_file) == BinaryFormat.PE

    elf_file = tmp_path / "game.x86_64"
    elf_file.write_bytes(_create_mock_elf())
    assert detect_binary_format(elf_file) == BinaryFormat.ELF

    txt_file = tmp_path / "readme.txt"
    txt_file.write_text("Hello world", encoding="utf-8")
    assert detect_binary_format(txt_file) == BinaryFormat.UNKNOWN

    assert detect_binary_format(tmp_path / "missing") == BinaryFormat.UNKNOWN


def test_detect_elf_arch(tmp_path: Path) -> None:
    elf_x64 = tmp_path / "elf_x64"
    elf_x64.write_bytes(_create_mock_elf(machine=0x3E))
    assert detect_elf_arch(elf_x64) == PeArch.X64

    elf_x86 = tmp_path / "elf_x86"
    elf_x86.write_bytes(_create_mock_elf(machine=0x03, is_64bit=False))
    assert detect_elf_arch(elf_x86) == PeArch.X86

    elf_arm64 = tmp_path / "elf_arm64"
    elf_arm64.write_bytes(_create_mock_elf(machine=0xB7))
    assert detect_elf_arch(elf_arm64) == PeArch.ARM64

    elf_unknown = tmp_path / "elf_unknown"
    elf_unknown.write_bytes(_create_mock_elf(machine=0x9999))
    assert detect_elf_arch(elf_unknown) == PeArch.UNKNOWN

    assert detect_elf_arch(tmp_path / "missing") == PeArch.UNKNOWN


def test_check_api_mismatches_elf_binary(tmp_path: Path) -> None:
    elf_file = tmp_path / "linux_game"
    elf_file.write_bytes(_create_mock_elf(machine=0x3E))

    warnings = check_api_mismatches(elf_file, d3d9=False, opengl=False, vulkan_layer=False)
    assert len(warnings) == 1
    assert "native Linux ELF binary" in warnings[0]
    assert "Proton/Wine" in warnings[0]
