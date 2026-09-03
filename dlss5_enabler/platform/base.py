import abc
from pathlib import Path


class PlatformAdapter(abc.ABC):
    @property
    @abc.abstractmethod
    def platform_name(self) -> str: ...

    @abc.abstractmethod
    def get_data_dir(self) -> Path: ...

    @abc.abstractmethod
    def get_cache_dir(self) -> Path: ...

    @abc.abstractmethod
    def get_log_dir(self) -> Path: ...

    @abc.abstractmethod
    def get_config_dir(self) -> Path: ...

    @abc.abstractmethod
    def unblock_file(self, path: Path | str) -> None: ...

    @abc.abstractmethod
    def make_executable(self, path: Path | str) -> None: ...

    @abc.abstractmethod
    def get_curl_command(self) -> list[str]: ...

    @abc.abstractmethod
    def is_wsl(self) -> bool: ...

    @abc.abstractmethod
    def is_game_running(self, executable: Path | str) -> bool: ...

    @abc.abstractmethod
    def is_directory_writable(self, directory: Path | str) -> bool: ...

    @abc.abstractmethod
    def get_permission_guidance(self, directory: Path | str) -> str: ...
