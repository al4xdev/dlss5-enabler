import shutil
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

if sys.platform == "win32":
    out_stream: Any = sys.stdout
    err_stream: Any = sys.stderr
    if hasattr(out_stream, "reconfigure"):
        out_stream.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(err_stream, "reconfigure"):
        err_stream.reconfigure(encoding="utf-8", errors="replace")

from dlss5_enabler.check import run_all_checks
from dlss5_enabler.core.logger import get_log_dir, setup_logger
from dlss5_enabler.core.pe import check_api_mismatches, detect_game_apis, detect_pe_arch
from dlss5_enabler.core.record import index_load, record_load
from dlss5_enabler.core.util import file_is_writable, get_cache_dir, get_permission_guidance, is_directory_writable
from dlss5_enabler.operations.install import run_install
from dlss5_enabler.operations.uninstall import run_uninstall
from dlss5_enabler.platform import ProtonManager, get_platform_adapter

app: typer.Typer = typer.Typer(
    name="dlss5-enabler",
    help="DLSS5 Enabler installs and manages the DLSS5-Feeder stack across Windows, Linux, and Proton",
    add_completion=False,
)
console: Console = Console(highlight=False)


def _path_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


@app.callback()
def main_callback(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable verbose debug logging")] = False,
) -> None:
    setup_logger(verbose=verbose)


@app.command(name="check")
def check_cmd() -> None:
    success: bool = run_all_checks()
    if not success:
        raise typer.Exit(code=1)


@app.command(name="install")
def install_cmd(
    exe: Annotated[
        Path,
        typer.Argument(
            help="Path to the game executable (.exe)",
            exists=True,
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ],
    lumenite: Annotated[
        bool,
        typer.Option(
            "--lumenite/--no-lumenite",
            help="Install LumeniteFX motion-vector provider (default: enabled)",
        ),
    ] = True,
    d3d9: Annotated[
        bool,
        typer.Option(
            "--d3d9",
            help="D3D9 game: install dgVoodoo2 D3D9->D3D11 translation",
        ),
    ] = False,
    opengl: Annotated[
        bool,
        typer.Option(
            "--opengl",
            help="OpenGL game: install ReShade as opengl32.dll",
        ),
    ] = False,
    vulkan_layer: Annotated[
        bool,
        typer.Option(
            "--vulkan-layer",
            help="Install Vulkan layer fallback for Vulkan games",
        ),
    ] = False,
    force_download: Annotated[
        bool,
        typer.Option(
            "--force-download",
            "-f",
            help="Bypass local cache and re-download all components",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Enable verbose debug logging",
        ),
    ] = False,
) -> None:
    setup_logger(verbose=verbose)
    if d3d9 and opengl:
        console.print("[bold red]--d3d9 and --opengl cannot be used together.[/bold red]")
        raise typer.Exit(code=2)
    adapter = get_platform_adapter()
    console.print(
        Panel.fit(
            f"[bold cyan]DLSS5 Enabler[/bold cyan]\n"
            f"Target Executable: [yellow]{exe}[/yellow]\n"
            f"Platform: [green]{adapter.platform_name}[/green]\n"
            f"LumeniteFX: [{'green' if lumenite else 'red'}]{'Enabled (Recommended)' if lumenite else 'Disabled'}[/]\n"
            f"D3D9 dgVoodoo2: [{'green' if d3d9 else 'dim'}]{'Yes' if d3d9 else 'No'}[/]\n"
            f"OpenGL Mode: [{'green' if opengl else 'dim'}]{'Yes' if opengl else 'No'}[/]\n"
            f"Vulkan Layer: [{'green' if vulkan_layer else 'dim'}]{'Yes' if vulkan_layer else 'No'}[/]\n"
            f"Force Re-download: [{'yellow' if force_download else 'dim'}]{'Yes' if force_download else 'No'}[/]",
            border_style="cyan",
        )
    )

    success: bool = run_install(
        game_exe_path=exe,
        install_lumenite=lumenite,
        d3d9_translate=d3d9,
        opengl=opengl,
        install_vulkan_layer=vulkan_layer,
        force_download=force_download,
        verbose=verbose,
    )
    if not success:
        console.print(
            f"[bold red]Installation failed. Check log file: {get_log_dir() / 'dlss5-enabler.log'}[/bold red]"
        )
        raise typer.Exit(code=1)

    rec = record_load(exe.parent)
    if rec and rec.proton_prefix:
        pfx_info = rec.proton_prefix
        launch_opts = ProtonManager.get_launch_options(["d3d9" if d3d9 else ("opengl32" if opengl else "dxgi")])
        console.print(
            Panel.fit(
                f"[bold green]Steam / Proton Integration Active[/bold green]\n"
                f"Proton Prefix: [cyan]{pfx_info}[/cyan]\n"
                f"Steam Launch Options: [bold yellow]{launch_opts}[/bold yellow]",
                border_style="green",
            )
        )


@app.command(name="uninstall")
def uninstall_cmd(
    target: Annotated[
        Path,
        typer.Argument(
            help="Path to the game executable (.exe) or game directory",
            exists=True,
            resolve_path=True,
        ),
    ],
) -> None:
    console.print(f"[bold red]Uninstalling DLSS5 Enabler from:[/bold red] {target}")
    success: bool = run_uninstall(target)
    if not success:
        console.print(
            f"[bold red]Uninstallation failed. Check log file: {get_log_dir() / 'dlss5-enabler.log'}[/bold red]"
        )
        raise typer.Exit(code=1)


@app.command(name="list")
def list_cmd() -> None:
    entries = index_load()
    if not entries:
        console.print("[dim]No installed games found in DLSS5 Enabler index.[/dim]")
        return

    table = Table(title="DLSS5 Enabler Installed Games", border_style="cyan")
    table.add_column("Executable", style="green")
    table.add_column("Type", style="yellow")
    table.add_column("Arch", style="magenta")
    table.add_column("Installed At", style="dim")
    table.add_column("Directory", style="blue")

    for e in entries:
        exe_name = Path(e.game_exe).name
        table.add_row(exe_name, e.install_type, e.architecture, e.timestamp[:19].replace("T", " "), e.game_dir)

    console.print(table)


@app.command(name="info")
def info_cmd(
    exe: Annotated[
        Path,
        typer.Argument(
            help="Path to game executable (.exe)",
            exists=True,
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ],
) -> None:
    arch = detect_pe_arch(exe)
    writable = file_is_writable(exe)
    game_dir = exe.parent
    rec = record_load(game_dir)
    detected_apis = detect_game_apis(exe)
    prefix_info = ProtonManager.find_prefix_for_game(exe)

    table = Table(title=f"Game Info: {exe.name}", border_style="cyan")
    table.add_column("Property", style="bold")
    table.add_column("Value")

    table.add_row("Path", str(exe))
    table.add_row("Directory", str(game_dir))
    table.add_row("Architecture", f"[green]{arch.value}[/green]")
    if detected_apis:
        table.add_row("Detected Graphics APIs", ", ".join(a.value for a in detected_apis))
    table.add_row("Executable Writable", f"[{'green' if writable else 'red'}]{writable}[/]")
    dir_writable = is_directory_writable(game_dir)
    table.add_row("Directory Writable", f"[{'green' if dir_writable else 'red'}]{dir_writable}[/]")
    table.add_row("DLSS5 Enabler Installed", f"[{'green' if rec else 'yellow'}]{'Yes' if rec else 'No'}[/]")

    if rec:
        table.add_row("Platform", rec.platform)
        table.add_row("Install Type", rec.install_type)
        table.add_row("Total Files Placed", str(len(rec.files)))
        table.add_row("Install Date", rec.timestamp[:19].replace("T", " "))
        if rec.proton_prefix:
            table.add_row("Proton Prefix", rec.proton_prefix)
        if rec.registry_touched:
            overrides_summary = ", ".join(t.value_name for t in rec.registry_touched)
            table.add_row("Wine DLL Overrides", overrides_summary)
        if rec.binaries:
            bin_summary = ", ".join(f"{k}: {v.version}" for k, v in rec.binaries.items() if v.version)
            table.add_row("Binaries", bin_summary)
    elif prefix_info:
        table.add_row("Detected Proton Prefix", str(prefix_info.prefix_path))
        table.add_row("Steam App ID", prefix_info.appid)
        table.add_row("Steam Launch Options", ProtonManager.get_launch_options(["dxgi"]))

    console.print(table)

    if not dir_writable:
        guidance = get_permission_guidance(game_dir)
        console.print(
            Panel(
                f"[bold yellow]Directory is write-protected:[/bold yellow]\n{guidance}",
                title="Permission Notice",
                border_style="yellow",
            )
        )

    warnings = check_api_mismatches(exe, d3d9=False, opengl=False, vulkan_layer=False)
    for w in warnings:
        console.print(f"[bold yellow][WARNING] {w}[/bold yellow]")


@app.command(name="cache")
def cache_cmd(
    clean: Annotated[bool, typer.Option("--clean", "-c", help="Delete all downloaded files in cache")] = False,
) -> None:
    cache_dir = get_cache_dir()
    files = list(cache_dir.glob("*"))

    if clean:
        failures: list[str] = []
        for f in files:
            try:
                if f.is_file():
                    f.unlink()
                elif f.is_dir():
                    shutil.rmtree(f)
            except Exception as error:
                failures.append(f"{f.name}: {error}")
        failed_names = {entry.split(":", 1)[0] for entry in failures}
        failures.extend(f"{f.name}: still exists" for f in files if f.exists() and f.name not in failed_names)
        if failures:
            console.print("[bold red]Cache cleanup incomplete:[/bold red]")
            for failure in failures:
                console.print(f"  [red]{failure}[/red]")
            raise typer.Exit(code=1)
        console.print("[green]DLSS5 Enabler download cache cleared successfully.[/green]")
        return

    table = Table(title=f"DLSS5 Enabler Cache ({cache_dir})", border_style="cyan")
    table.add_column("File / Directory", style="green")
    table.add_column("Size", style="yellow")

    total_size: float = 0.0
    for f in files:
        if f.is_file():
            sz = f.stat().st_size
            total_size += sz
            table.add_row(f.name, f"{sz / (1024 * 1024):.2f} MB")
        elif f.is_dir():
            sz = _path_size(f)
            total_size += sz
            table.add_row(f.name + "/", f"{sz / (1024 * 1024):.2f} MB")

    console.print(table)
    console.print(f"[bold]Total Cache Size:[/bold] {total_size / (1024 * 1024):.2f} MB")


if __name__ == "__main__":
    app()
