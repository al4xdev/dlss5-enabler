from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

CommandRunner = Callable[[Sequence[str], Mapping[str, str] | None], None]


def _run(command: Sequence[str], environment: Mapping[str, str] | None = None) -> None:
    subprocess.run(command, check=True, env=environment)


def _executable(directory: Path, name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return directory / f"{name}{suffix}"


def verify_isolated_installs(wheel: Path, runner: CommandRunner | None = None) -> None:
    execute = runner or _run
    wheel = wheel.resolve()
    with tempfile.TemporaryDirectory(prefix="dlss5-enabler-install-check-") as temporary:
        root = Path(temporary)
        tool_root = root / "uv-tools"
        tool_bin = root / "uv-bin"
        tool_environment = os.environ.copy()
        tool_environment["UV_TOOL_DIR"] = str(tool_root)
        tool_environment["UV_TOOL_BIN_DIR"] = str(tool_bin)
        tool_environment["UV_CACHE_DIR"] = str(root / "uv-cache")
        execute(("uv", "tool", "install", "--force", str(wheel)), tool_environment)
        tool_command = _executable(tool_bin, "dlss5-enabler")
        execute((str(tool_command), "--help"), tool_environment)
        execute((str(tool_command), "version"), tool_environment)

        pip_venv = root / "pip-venv"
        pip_environment = os.environ.copy()
        pip_environment["PIP_CACHE_DIR"] = str(root / "pip-cache")
        execute((sys.executable, "-m", "venv", str(pip_venv)), pip_environment)
        pip_bin = pip_venv / ("Scripts" if os.name == "nt" else "bin")
        pip_python = _executable(pip_bin, "python")
        execute((str(pip_python), "-m", "pip", "install", "--upgrade", str(wheel)), pip_environment)
        pip_command = _executable(pip_bin, "dlss5-enabler")
        execute((str(pip_command), "--help"), pip_environment)
        execute((str(pip_command), "version"), pip_environment)


def main(argv: Sequence[str] | None = None) -> None:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    directory = Path(arguments[0]) if arguments else Path("dist")
    wheels = tuple(sorted(directory.glob("*.whl")))
    if len(wheels) != 1:
        raise RuntimeError(f"Expected exactly one wheel in {directory}, found {len(wheels)}")
    verify_isolated_installs(wheels[0])


if __name__ == "__main__":
    main()
