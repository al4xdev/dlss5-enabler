import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

console: Console = Console(highlight=False)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run_tool(cmd: Sequence[str], cwd: Path = PROJECT_ROOT) -> tuple[bool, float, str]:
    start: float = time.perf_counter()
    try:
        proc: subprocess.CompletedProcess[str] = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
        )
        duration: float = time.perf_counter() - start
        output: str = (proc.stdout + "\n" + proc.stderr).strip()
        return proc.returncode == 0, duration, output
    except Exception as e:
        duration = time.perf_counter() - start
        return False, duration, str(e)


def run_all_checks() -> bool:
    source_root = PROJECT_ROOT / "dlss5_enabler"
    tests_root = PROJECT_ROOT / "tests"
    if not source_root.is_dir() or not tests_root.is_dir() or not (PROJECT_ROOT / "pyproject.toml").is_file():
        console.print(
            "[bold red]dlss5-enabler check requires a source checkout containing pyproject.toml and tests/.[/bold red]"
        )
        return False
    console.print(
        Panel.fit(
            "[bold cyan]DLSS5 Enabler Unified Quality Orchestrator[/bold cyan]\n"
            "Running Ruff (Format & Lint), Mypy Strict, Pyright Strict, and Pytest across dlss5_enabler & tests...",
            border_style="cyan",
        )
    )

    tasks: list[tuple[str, Sequence[str]]] = [
        ("Ruff Format Check", [sys.executable, "-m", "ruff", "format", "--check", str(source_root), str(tests_root)]),
        (
            "Ruff Lint (dlss5_enabler & tests)",
            [sys.executable, "-m", "ruff", "check", str(source_root), str(tests_root)],
        ),
        (
            "Mypy Strict (dlss5_enabler & tests)",
            [sys.executable, "-m", "mypy", "--strict", str(source_root), str(tests_root)],
        ),
        (
            "Pyright Strict (dlss5_enabler & tests)",
            [sys.executable, "-m", "pyright", str(source_root), str(tests_root)],
        ),
        ("Pytest Test Suite", [sys.executable, "-m", "pytest", "-q", str(tests_root)]),
    ]

    results: list[tuple[str, bool, float, str]] = []
    all_passed: bool = True

    for name, cmd in tasks:
        console.print(f"[dim]Running {name}...[/dim]")
        passed, duration, output = run_tool(cmd)
        results.append((name, passed, duration, output))
        if not passed:
            all_passed = False
            console.print(f"[bold red][FAIL] {name} failed in {duration:.2f}s[/bold red]")
            if output:
                console.print(Panel(escape(output), title=f"[red]{name} Error Output[/red]", border_style="red"))
        else:
            console.print(f"[bold green][PASS] {name} passed ({duration:.2f}s)[/bold green]")

    table: Table = Table(title="Unified Quality & Test Results", border_style="cyan")
    table.add_column("Tool / Check", style="bold")
    table.add_column("Status", justify="center")
    table.add_column("Duration", justify="right")

    for name, passed, duration, _ in results:
        status_str: str = "[bold green]PASSED[/bold green]" if passed else "[bold red]FAILED[/bold red]"
        table.add_row(name, status_str, f"{duration:.2f}s")

    console.print("")
    console.print(table)

    if all_passed:
        console.print("\n[bold green][OK] All linters, type checks, and unit tests passed flawlessly![/bold green]")
    else:
        console.print("\n[bold red][X] One or more quality checks failed. See details above.[/bold red]")

    return all_passed


def main() -> None:
    success: bool = run_all_checks()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
