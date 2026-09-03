import contextlib
import os
import tempfile
from pathlib import Path

from dlss5_enabler.platform.base import PlatformAdapter


class LinuxAdapter(PlatformAdapter):
    @staticmethod
    def _env_path(name: str, fallback_parts: tuple[str, ...]) -> Path:
        value = os.environ.get(name)
        if value:
            candidate = Path(value)
            if candidate.is_absolute():
                return candidate
        return Path.home().joinpath(*fallback_parts)

    @property
    def platform_name(self) -> str:
        return "linux"

    def get_data_dir(self) -> Path:
        base = self._env_path("XDG_DATA_HOME", (".local", "share"))
        p = base / "dlss5-enabler"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_cache_dir(self) -> Path:
        base = self._env_path("XDG_CACHE_HOME", (".cache",))
        p = base / "dlss5-enabler" / "downloads"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_log_dir(self) -> Path:
        base = self._env_path("XDG_STATE_HOME", (".local", "state"))
        p = base / "dlss5-enabler" / "logs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_config_dir(self) -> Path:
        base = self._env_path("XDG_CONFIG_HOME", (".config",))
        p = base / "dlss5-enabler"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def unblock_file(self, path: Path | str) -> None:
        pass

    def make_executable(self, path: Path | str) -> None:
        p = Path(path)
        if p.exists():
            with contextlib.suppress(Exception):
                p.chmod(p.stat().st_mode | 0o111)

    def get_curl_command(self) -> list[str]:
        return ["curl"]

    def is_wsl(self) -> bool:
        if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
            return True
        proc_ver = Path("/proc/version")
        if proc_ver.is_file():
            try:
                content = proc_ver.read_text(encoding="utf-8", errors="ignore").lower()
                if "microsoft" in content or "wsl" in content:
                    return True
            except Exception:
                pass
        return False

    def is_game_running(self, executable: Path | str) -> bool:
        _ = executable
        return False

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
            f"Directory is write-protected or owned by another user/root.\n"
            f"Fix with Bash:\n"
            f'  chmod -R u+w "{p}"\n'
            f"Or if root-owned:\n"
            f'  sudo chown -R $USER "{p}"'
        )
