import struct
from enum import Enum
from pathlib import Path


class BinaryFormat(str, Enum):
    UNKNOWN = "unknown"
    PE = "pe"
    ELF = "elf"


class PeArch(str, Enum):
    UNKNOWN = "unknown"
    X86 = "x86 (32-bit)"
    X64 = "x64 (64-bit)"
    ARM64 = "arm64"


class DetectedApi(str, Enum):
    D3D12 = "DirectX 12 (d3d12.dll)"
    D3D11 = "DirectX 11 / DXGI (dxgi.dll / d3d11.dll)"
    D3D9 = "DirectX 9 (d3d9.dll)"
    OPENGL = "OpenGL (opengl32.dll)"
    VULKAN = "Vulkan (vulkan-1.dll)"


IMAGE_DOS_SIGNATURE = 0x5A4D
IMAGE_NT_SIGNATURE = 0x00004550

IMAGE_FILE_MACHINE_I386 = 0x014C
IMAGE_FILE_MACHINE_AMD64 = 0x8664
IMAGE_FILE_MACHINE_ARM64 = 0xAA64

OPTIONAL_HEADER_MAGIC_PE32 = 0x10B
OPTIONAL_HEADER_MAGIC_PE32_PLUS = 0x20B

ELF_MAGIC = b"\x7fELF"
EM_386 = 0x03
EM_X86_64 = 0x3E
EM_AARCH64 = 0xB7

_NATIVE_DLSS_DLLS = frozenset({"nvngx_dlss.dll", "nvngx_dlssg.dll"})
_NATIVE_DLSS_PLUGIN_ROOTS = ("Engine/Plugins", "Plugins")
_NATIVE_DLSS_EXECUTABLE_DIRECTORIES = frozenset({"bin", "binaries", "win32", "win64", "x64", "x86"})


def detect_binary_format(exe_path: Path | str) -> BinaryFormat:
    path = Path(exe_path)
    if not path.is_file():
        return BinaryFormat.UNKNOWN

    try:
        with path.open("rb") as f:
            magic = f.read(4)
            if len(magic) >= 2 and magic[:2] == b"MZ":
                return BinaryFormat.PE
            if len(magic) == 4 and magic == ELF_MAGIC:
                return BinaryFormat.ELF
    except Exception:
        pass
    return BinaryFormat.UNKNOWN


def detect_elf_arch(exe_path: Path | str) -> PeArch:
    path = Path(exe_path)
    if not path.is_file():
        return PeArch.UNKNOWN

    try:
        with path.open("rb") as f:
            header = f.read(20)
            if len(header) >= 20 and header[:4] == ELF_MAGIC:
                ei_data = header[5]
                endian = "<" if ei_data == 1 else ">"
                e_machine: int = struct.unpack_from(f"{endian}H", header, 18)[0]
                if e_machine == EM_386:
                    return PeArch.X86
                if e_machine == EM_X86_64:
                    return PeArch.X64
                if e_machine == EM_AARCH64:
                    return PeArch.ARM64
    except Exception:
        pass
    return PeArch.UNKNOWN


def detect_pe_arch(exe_path: Path | str) -> PeArch:
    path = Path(exe_path)
    result = PeArch.UNKNOWN
    if not path.is_file():
        return result

    try:
        with path.open("rb") as f:
            dos_header = f.read(64)
            if len(dos_header) >= 64:
                magic: int = struct.unpack_from("<H", dos_header, 0)[0]
                if magic == IMAGE_DOS_SIGNATURE:
                    e_lfanew: int = struct.unpack_from("<I", dos_header, 0x3C)[0]
                    f.seek(e_lfanew)
                    nt_signature_bytes = f.read(4)
                    if len(nt_signature_bytes) == 4:
                        nt_signature: int = struct.unpack("<I", nt_signature_bytes)[0]
                        if nt_signature == IMAGE_NT_SIGNATURE:
                            file_header = f.read(20)
                            if len(file_header) >= 20:
                                machine: int = struct.unpack_from("<H", file_header, 0)[0]
                                if machine == IMAGE_FILE_MACHINE_I386:
                                    result = PeArch.X86
                                elif machine == IMAGE_FILE_MACHINE_AMD64:
                                    result = PeArch.X64
                                elif machine == IMAGE_FILE_MACHINE_ARM64:
                                    result = PeArch.ARM64
    except Exception:
        pass
    return result


def _rva_to_offset(rva: int, sections: list[tuple[int, int, int, int]]) -> int | None:
    for va, vs, raw_ptr, raw_size in sections:
        size = max(vs, raw_size)
        if va <= rva < va + size:
            return raw_ptr + (rva - va)
    return None


def detect_imported_dlls(exe_path: Path | str) -> list[str]:
    path = Path(exe_path)
    if not path.is_file():
        return []

    dlls: list[str] = []
    try:
        with path.open("rb") as f:
            dos_header = f.read(64)
            if len(dos_header) >= 64 and struct.unpack_from("<H", dos_header, 0)[0] == IMAGE_DOS_SIGNATURE:
                e_lfanew: int = struct.unpack_from("<I", dos_header, 0x3C)[0]
                f.seek(e_lfanew)
                if f.read(4) == b"PE\x00\x00":
                    file_header = f.read(20)
                    if len(file_header) >= 20:
                        num_sections: int = struct.unpack_from("<H", file_header, 2)[0]
                        size_of_opt_hdr: int = struct.unpack_from("<H", file_header, 16)[0]

                        opt_hdr_pos = f.tell()
                        opt_hdr_magic = struct.unpack("<H", f.read(2))[0]
                        is_64bit = opt_hdr_magic == OPTIONAL_HEADER_MAGIC_PE32_PLUS

                        import_dir_offset_in_opt = 120 if is_64bit else 104
                        f.seek(opt_hdr_pos + import_dir_offset_in_opt)
                        import_rva, _ = struct.unpack("<II", f.read(8))

                        if import_rva != 0:
                            f.seek(opt_hdr_pos + size_of_opt_hdr)
                            sections: list[tuple[int, int, int, int]] = []
                            for _ in range(num_sections):
                                sec_bytes = f.read(40)
                                if len(sec_bytes) < 40:
                                    break
                                va = struct.unpack_from("<I", sec_bytes, 12)[0]
                                vs = struct.unpack_from("<I", sec_bytes, 8)[0]
                                raw_size = struct.unpack_from("<I", sec_bytes, 16)[0]
                                raw_ptr = struct.unpack_from("<I", sec_bytes, 20)[0]
                                sections.append((va, vs, raw_ptr, raw_size))

                            import_offset = _rva_to_offset(import_rva, sections)
                            if import_offset is not None:
                                f.seek(import_offset)
                                while True:
                                    desc_bytes = f.read(20)
                                    if len(desc_bytes) < 20 or desc_bytes == b"\x00" * 20:
                                        break
                                    name_rva = struct.unpack_from("<I", desc_bytes, 12)[0]
                                    if name_rva == 0:
                                        continue

                                    name_offset = _rva_to_offset(name_rva, sections)
                                    if name_offset is not None:
                                        curr_pos = f.tell()
                                        f.seek(name_offset)
                                        raw_name = b""
                                        while char := f.read(1):
                                            if char == b"\x00":
                                                break
                                            raw_name += char
                                        dll_name = raw_name.decode("utf-8", errors="ignore").lower()
                                        if dll_name and dll_name not in dlls:
                                            dlls.append(dll_name)
                                        f.seek(curr_pos)
    except Exception:
        pass
    return dlls


def detect_game_apis(exe_path: Path | str) -> list[DetectedApi]:
    imported = [name.lower() for name in detect_imported_dlls(exe_path)]
    apis: list[DetectedApi] = []

    if "d3d12.dll" in imported:
        apis.append(DetectedApi.D3D12)
    if "dxgi.dll" in imported or "d3d11.dll" in imported or "d3d10.dll" in imported:
        apis.append(DetectedApi.D3D11)
    if "d3d9.dll" in imported:
        apis.append(DetectedApi.D3D9)
    if "opengl32.dll" in imported:
        apis.append(DetectedApi.OPENGL)
    if "vulkan-1.dll" in imported:
        apis.append(DetectedApi.VULKAN)

    return apis


def detect_native_dlss(exe_path: Path | str) -> bool:
    path = Path(exe_path)
    imported = set(detect_imported_dlls(path))
    if imported.intersection(_NATIVE_DLSS_DLLS):
        return True
    roots = [path.parent]
    current = path.parent
    for _ in range(3):
        if current.name.lower() not in _NATIVE_DLSS_EXECUTABLE_DIRECTORIES or current.parent == current:
            break
        current = current.parent
        roots.append(current)
    if len(roots) > 1 and roots[-1].parent != roots[-1]:
        roots.append(roots[-1].parent)
    for root in roots:
        try:
            if any(item.is_file() and item.name.lower() in _NATIVE_DLSS_DLLS for item in root.iterdir()):
                return True
            for relative_plugin_root in _NATIVE_DLSS_PLUGIN_ROOTS:
                plugin_root = root / relative_plugin_root
                if plugin_root.is_dir() and any(
                    artifact.is_file() for name in _NATIVE_DLSS_DLLS for artifact in plugin_root.rglob(name)
                ):
                    return True
        except OSError:
            continue
    return False


def check_api_mismatches(
    exe_path: Path | str,
    d3d9: bool,
    opengl: bool,
    vulkan_layer: bool,
) -> list[str]:
    warnings: list[str] = []

    if detect_binary_format(exe_path) == BinaryFormat.ELF:
        elf_arch = detect_elf_arch(exe_path)
        warnings.append(
            f"Target executable is a native Linux ELF binary [{elf_arch.value}]. "
            "DLSS5 Enabler targets Windows PE binaries running natively or under Proton/Wine. "
            "Native Linux binaries cannot load Windows DirectX/DXGI ReShade DLLs. "
            "Please target the Windows version of the game (.exe) running via Proton/Wine."
        )
        return warnings

    apis = detect_game_apis(exe_path)
    if not apis:
        return warnings

    api_names = ", ".join(a.value for a in apis)

    if d3d9 and (DetectedApi.D3D12 in apis or DetectedApi.D3D11 in apis) and DetectedApi.D3D9 not in apis:
        warnings.append(
            f"Flag --d3d9 was specified, but this executable appears to natively target modern DirectX ({api_names}). "
            "dgVoodoo2 D3D9 translation is likely unnecessary."
        )

    if (
        opengl
        and (DetectedApi.D3D12 in apis or DetectedApi.D3D11 in apis or DetectedApi.D3D9 in apis)
        and DetectedApi.OPENGL not in apis
    ):
        warnings.append(
            f"Flag --opengl was specified, but this executable imports DirectX ({api_names}). "
            "ReShade as opengl32.dll might not hook unless the game has an in-game OpenGL renderer option."
        )

    if (
        vulkan_layer
        and (DetectedApi.D3D12 in apis or DetectedApi.D3D11 in apis or DetectedApi.D3D9 in apis)
        and DetectedApi.VULKAN not in apis
    ):
        warnings.append(
            f"Flag --vulkan-layer was specified, but this executable imports DirectX ({api_names}) without "
            "vulkan-1.dll."
        )

    if not d3d9 and not opengl and not vulkan_layer:
        if DetectedApi.D3D9 in apis and DetectedApi.D3D11 not in apis and DetectedApi.D3D12 not in apis:
            warnings.append(
                f"Executable imports DirectX 9 ({api_names}) without DXGI/D3D11. "
                "Consider passing --d3d9 to enable dgVoodoo2 D3D9->D3D11 translation if standard DXGI hook fails."
            )
        elif DetectedApi.OPENGL in apis and DetectedApi.D3D11 not in apis and DetectedApi.D3D12 not in apis:
            warnings.append(
                f"Executable imports pure OpenGL ({api_names}). "
                "Consider passing --opengl if ReShade dxgi.dll does not inject automatically."
            )
        elif DetectedApi.VULKAN in apis and DetectedApi.D3D11 not in apis and DetectedApi.D3D12 not in apis:
            warnings.append(
                f"Executable imports pure Vulkan ({api_names}). "
                "If standard hook fails, consider passing --vulkan-layer."
            )

    return warnings
