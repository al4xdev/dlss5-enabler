import contextlib
import os
import subprocess
import tempfile
from pathlib import Path

from dlss5_enabler.platform.base import PlatformAdapter


class WindowsAdapter(PlatformAdapter):
    @property
    def platform_name(self) -> str:
        return "windows"

    def get_data_dir(self) -> Path:
        appdata = os.environ.get("LOCALAPPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Local"
        p = base / "DLSS5 Enabler"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_cache_dir(self) -> Path:
        p = self.get_data_dir() / "downloads"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_log_dir(self) -> Path:
        p = self.get_data_dir() / "logs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_config_dir(self) -> Path:
        return self.get_data_dir()

    def unblock_file(self, path: Path | str) -> None:
        p = Path(path)
        if not p.exists():
            return
        with contextlib.suppress(Exception):
            ads = f"{p}:Zone.Identifier"
            if os.path.exists(ads):
                os.remove(ads)

    def make_executable(self, path: Path | str) -> None:
        pass

    def get_curl_command(self) -> list[str]:
        return ["curl.exe"]

    def is_wsl(self) -> bool:
        return False

    def is_game_running(self, executable: Path | str) -> bool:
        target = Path(executable).resolve()
        name = target.name
        if not name:
            return False
        escaped_name = name.replace("'", "''")
        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    (
                        "$ErrorActionPreference = 'Stop'; "
                        f"Get-CimInstance -ClassName Win32_Process -Filter \"Name = '{escaped_name}'\" | "
                        "ForEach-Object { $_.ExecutablePath }"
                    ),
                ],
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode != 0:
            return False
        target_key = os.path.normcase(str(target))
        return any(
            os.path.normcase(str(Path(path.strip()).resolve())) == target_key
            for path in result.stdout.splitlines()
            if path.strip()
        )

    def is_directory_writable(self, directory: Path | str) -> bool:
        p = Path(directory)
        if not p.exists() or not p.is_dir():
            return False
        probe: Path | None = None
        try:
            fd, name = tempfile.mkstemp(prefix=".dlss5-enabler-perm-probe.", dir=p)
            os.close(fd)
            probe = Path(name)
            return True
        except (PermissionError, OSError):
            return False
        finally:
            if probe is not None:
                probe.unlink(missing_ok=True)

    def get_permission_guidance(self, directory: Path | str) -> str:
        p = Path(directory).resolve()
        return (
            f"Directory is write-protected or requires Administrator elevation.\n"
            f"Fix with PowerShell (Administrator):\n"
            f'  icacls "{p}" /grant "${{env:USERNAME}}:(OI)(CI)F" /T /Q\n'
            f"Or re-run DLSS5 Enabler in an elevated terminal (Run as Administrator)."
        )
