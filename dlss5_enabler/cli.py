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
from dlss5_enabler.core.logger import get_log_dir, get_logger, setup_logger
from dlss5_enabler.core.pe import check_api_mismatches, detect_game_apis, detect_pe_arch
from dlss5_enabler.core.record import InstallRecord, OptiScalerStrategyOptions, index_load_active, record_load
from dlss5_enabler.core.util import file_is_writable, get_cache_dir, get_permission_guidance, is_directory_writable
from dlss5_enabler.core.version import InstallVersionStatus, get_install_version_status, get_tool_version
from dlss5_enabler.network.update_check import UpdateCheckResult, check_for_update
from dlss5_enabler.operations.capabilities import analyze_capabilities
from dlss5_enabler.operations.install import run_install
from dlss5_enabler.operations.uninstall import run_uninstall
from dlss5_enabler.operations.update import GameUpdateStatus, run_update
from dlss5_enabler.platform import ProtonManager, get_platform_adapter
from dlss5_enabler.schemas.strategy import FrameGenerationMode, InstallStrategy, NrPlacement

app: typer.Typer = typer.Typer(
    name="dlss5-enabler",
    help=(
        "Install and manage DLSS rendering upgrades for games. Start with 'install', use 'update' "
        "to refresh an existing setup, and use 'switch' to change its rendering engine."
    ),
    add_completion=False,
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
console: Console = Console(highlight=False)


def _version_status_markup(status: InstallVersionStatus) -> str:
    style = {
        InstallVersionStatus.CURRENT: "green",
        InstallVersionStatus.UPDATE_AVAILABLE: "yellow",
        InstallVersionStatus.NEWER_THAN_CLI: "red",
        InstallVersionStatus.UNKNOWN_LEGACY: "magenta",
    }[status]
    return f"[{style}]{status.value}[/{style}]"


def _show_update_check(result: UpdateCheckResult) -> None:
    if result.error:
        get_logger("update_check").debug(f"CLI update check failed: {result.error}")
    if result.update_available and result.latest_version is not None:
        console.print(
            f"[bold yellow]DLSS5 Enabler {result.latest_version} is available; "
            f"you are running {result.current_version}.[/bold yellow]\n"
            "Update with: [cyan]uv tool upgrade dlss5-enabler[/cyan]\n"
            "pip alternative: [cyan]python -m pip install --upgrade dlss5-enabler[/cyan]"
        )


def _check_cli_update(*, force: bool = False) -> UpdateCheckResult:
    result = check_for_update(force=force)
    _show_update_check(result)
    return result


def _show_reshade_activation_guide(lumenite: bool, native_dlss: bool = False) -> None:
    if native_dlss:
        console.print(
            Panel.fit(
                "[bold cyan]Native DLSS path selected[/bold cyan]\n\n"
                "This game already provides DLSS calls, so DLSS5-Feeder was not installed.\n\n"
                "1. Start the game and press [bold]Home[/bold].\n"
                "2. If the game's native motion vectors are unusable, enable [bold]LUMENITE: Kernel 2.0[/bold] "
                "and place it at the top. Test with it both enabled and disabled.\n\n"
                "[yellow]Do not install or enable DLSS 5 Feed for this game.[/yellow]",
                title="[bold cyan]Activate ReShade add-on[/bold cyan]",
                border_style="cyan",
            )
        )
        return
    motion_vector_provider = (
        "[green]\u2610[/] [bold]LUMENITE: Kernel 2.0[/] [dim][lumenite_Kernel.fx][/dim]"
        if lumenite
        else "[yellow]\u2610[/] [bold]A compatible motion-vector provider[/]"
    )
    console.print(
        Panel.fit(
            "[bold cyan]One final ReShade step is required[/bold cyan]\n\n"
            "1. Start the game, wait for the initial shader compilation, then press [bold]Home[/bold].\n"
            "2. In [bold]Home[/bold], enable these effects and use [bold]Active to top[/bold]:\n"
            f"   {motion_vector_provider}\n"
            "   [green]\u2610[/] [bold]DLSS 5 Feed[/] [dim][DLSS5_Feed.fx][/dim]\n\n"
            "[yellow]Lumenite is recommended when the game does not expose usable motion vectors; "
            "test with it enabled and disabled.[/yellow]\n"
            "[yellow]Required order, top to bottom:[/yellow] motion-vector provider \u2192 DLSS 5 Feed. "
            "You may enable other effects after these two.",
            title="[bold cyan]Activate ReShade effects[/bold cyan]",
            border_style="cyan",
        )
    )


def _show_optiscaler_activation_guide(record: InstallRecord) -> None:
    options = record.strategy_options
    configuration = ""
    if isinstance(options, OptiScalerStrategyOptions):
        frame_generation = options.frame_generation.value
        if options.frame_generation is FrameGenerationMode.DLSSG:
            frame_generation = f"dlssg {options.fg_multiplier}x"
        configuration = f"\n4. Saved setup: NR {options.nr_placement.value}; frame generation {frame_generation}."
    console.print(
        Panel.fit(
            "[bold cyan]OptiScaler installed[/bold cyan]\n\n"
            "1. Start the game in DirectX 11 or DirectX 12 and enable its native DLSS option.\n"
            "2. Press [bold]Delete[/bold] to open the OptiScaler overlay.\n"
            "3. DLSS Neural Rendering starts enabled with the recorded number of passes."
            f"{configuration}\n\n"
            "[yellow]Tip:[/yellow] NR placement 'before' can improve FPS; 'inside' is experimental. "
            "In the Control smoke test, FSR frame generation worked while DLSSG reported an HDR10 requirement. "
            "Use FSR when that DLSSG limitation appears.",
            title="[bold cyan]Activate OptiScaler[/bold cyan]",
            border_style="cyan",
        )
    )


def _show_activation_guide(record: InstallRecord | None, lumenite: bool) -> None:
    if record is not None and record.strategy is InstallStrategy.OPTISCALER:
        _show_optiscaler_activation_guide(record)
        return
    _show_reshade_activation_guide(lumenite, record.native_dlss_detected if record is not None else False)


def _path_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _resolve_managed_target(target: str, *, executable_required: bool = False) -> Path:
    target = target.strip()
    if not target:
        raise ValueError("A game executable path or managed executable name is required.")
    candidate = Path(target).expanduser()
    if candidate.exists():
        resolved = candidate.resolve()
    elif any(separator in target for separator in ("/", "\\", ":")):
        raise ValueError(f"Target path does not exist: {target}")
    else:
        matches = [entry for entry in index_load_active() if Path(entry.game_exe).name.casefold() == target.casefold()]
        if not matches:
            raise ValueError(
                f"No managed installation named '{target}' was found. Pass the full executable path instead."
            )
        if len(matches) == 1:
            resolved = Path(matches[0].game_exe)
        else:
            locations = "\n".join(
                f"- {entry.game_exe}" for entry in sorted(matches, key=lambda entry: entry.game_exe.casefold())
            )
            raise ValueError(f"More than one managed executable is named '{target}':\n{locations}\nPass the full path.")
    if executable_required and not resolved.is_file():
        raise ValueError(f"Target is not a game executable: {resolved}")
    return resolved


def _resolve_target_or_exit(target: str, *, executable_required: bool = False) -> Path:
    try:
        return _resolve_managed_target(target, executable_required=executable_required)
    except ValueError as error:
        console.print(f"[bold red]{error}[/bold red]")
        raise typer.Exit(code=2) from error


@app.callback()
def main_callback(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable verbose debug logging")] = False,
) -> None:
    setup_logger(verbose=verbose)


@app.command(name="check", help="Run the project's code quality and test checks.", rich_help_panel="Development")
def check_cmd() -> None:
    success: bool = run_all_checks()
    if not success:
        raise typer.Exit(code=1)


@app.command(
    name="install",
    help="Install a new managed setup for a game executable.",
    epilog=(
        "Examples: dlss5-enabler install C:\\Games\\Game\\game.exe | "
        "dlss5-enabler install C:\\Games\\Game\\game.exe --engine optiscaler --optiscaler-archive package.zip"
    ),
    rich_help_panel="Game setup",
)
def install_cmd(
    target: Annotated[
        str,
        typer.Argument(
            help="Full path to the game's .exe (an existing managed executable name also works)",
        ),
    ],
    lumenite: Annotated[
        bool,
        typer.Option(
            "--lumenite/--no-lumenite",
            help="RenoDX only: install the recommended LumeniteFX motion-vector provider",
            rich_help_panel="RenoDX / ReShade options",
        ),
    ] = True,
    d3d9: Annotated[
        bool,
        typer.Option(
            "--d3d9",
            help="RenoDX only: translate a DirectX 9 game through dgVoodoo2",
            rich_help_panel="RenoDX / ReShade options",
        ),
    ] = False,
    opengl: Annotated[
        bool,
        typer.Option(
            "--opengl",
            help="RenoDX only: install the ReShade proxy for an OpenGL game",
            rich_help_panel="RenoDX / ReShade options",
        ),
    ] = False,
    vulkan_layer: Annotated[
        bool,
        typer.Option(
            "--vulkan-layer",
            help="RenoDX only: install the Vulkan layer for a Vulkan game",
            rich_help_panel="RenoDX / ReShade options",
        ),
    ] = False,
    engine: Annotated[
        InstallStrategy,
        typer.Option(
            "--engine",
            help="Engine: renodx for broad compatibility, or optiscaler for native-DLSS Windows x64 DX11/12 games",
            rich_help_panel="Setup",
        ),
    ] = InstallStrategy.RENODX,
    optiscaler_archive: Annotated[
        Path | None,
        typer.Option(
            "--optiscaler-archive",
            help="OptiScaler only: path to the supported y4my4my4m v3 ZIP (a verified cached copy may be reused)",
            rich_help_panel="OptiScaler options",
        ),
    ] = None,
    nr_passes: Annotated[
        int,
        typer.Option(
            "--nr-passes",
            min=1,
            max=5,
            help="OptiScaler only: Neural Rendering passes; more passes cost more GPU time",
            rich_help_panel="OptiScaler options",
        ),
    ] = 1,
    optiscaler_proxy: Annotated[
        str,
        typer.Option(
            "--optiscaler-proxy",
            help=(
                "OptiScaler only: proxy DLL filename "
                "(keep the default unless the game requires another supported proxy)"
            ),
            rich_help_panel="OptiScaler options",
        ),
    ] = "dxgi.dll",
    frame_generation: Annotated[
        FrameGenerationMode,
        typer.Option(
            "--frame-generation",
            help=(
                "OptiScaler only: auto selects the broadly compatible FSR backend; off disables it; "
                "fsr or dlssg selects a backend explicitly"
            ),
            rich_help_panel="OptiScaler options",
        ),
    ] = FrameGenerationMode.AUTO,
    fg_multiplier: Annotated[
        int,
        typer.Option(
            "--fg-multiplier",
            min=2,
            max=6,
            help="OptiScaler only: DLSSG frame multiplier from 2x to 6x",
            rich_help_panel="OptiScaler options",
        ),
    ] = 2,
    nr_placement: Annotated[
        NrPlacement,
        typer.Option(
            "--nr-placement",
            help=(
                "OptiScaler only: after favors image quality; before can improve FPS; "
                "inside experimentally runs NR within the upscaler"
            ),
            rich_help_panel="OptiScaler options",
        ),
    ] = NrPlacement.AFTER,
    force_download: Annotated[
        bool,
        typer.Option(
            "--force-download",
            "-f",
            help="Ignore cached downloads and fetch components again",
            rich_help_panel="Setup",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Enable verbose debug logging",
            rich_help_panel="Setup",
        ),
    ] = False,
) -> None:
    setup_logger(verbose=verbose)
    exe = _resolve_target_or_exit(target, executable_required=True)
    _check_cli_update()
    if d3d9 and opengl:
        console.print(
            "[bold red]Choose only one graphics mode: --d3d9 and --opengl cannot be used together.[/bold red]"
        )
        raise typer.Exit(code=2)
    if engine is InstallStrategy.OPTISCALER and (d3d9 or opengl or vulkan_layer):
        console.print(
            "[bold red]OptiScaler supports native-DLSS DirectX 11/12 games only. "
            "Remove --d3d9, --opengl, or --vulkan-layer, or install with --engine renodx.[/bold red]"
        )
        raise typer.Exit(code=2)
    if engine is InstallStrategy.RENODX and (
        optiscaler_archive is not None
        or nr_passes != 1
        or optiscaler_proxy != "dxgi.dll"
        or frame_generation is not FrameGenerationMode.AUTO
        or fg_multiplier != 2
        or nr_placement is not NrPlacement.AFTER
    ):
        console.print(
            "[bold red]The supplied OptiScaler options require OptiScaler. "
            "Add --engine optiscaler or remove those options.[/bold red]"
        )
        raise typer.Exit(code=2)
    if fg_multiplier != 2 and frame_generation is not FrameGenerationMode.DLSSG:
        console.print("[bold red]--fg-multiplier above 2 requires --frame-generation dlssg.[/bold red]")
        raise typer.Exit(code=2)
    adapter = get_platform_adapter()
    plan = [
        "[bold cyan]Installing game setup[/bold cyan]",
        f"Game: [yellow]{exe}[/yellow]",
        f"Platform: [green]{adapter.platform_name}[/green]",
        f"Engine: [green]{engine.value}[/green]",
    ]
    if engine is InstallStrategy.OPTISCALER:
        archive = str(optiscaler_archive) if optiscaler_archive is not None else "verified cached copy"
        plan.extend(
            (
                f"OptiScaler package: [cyan]{archive}[/cyan]",
                f"Neural Rendering passes: [cyan]{nr_passes}[/cyan]",
                f"NR placement: [cyan]{nr_placement.value}[/cyan]",
                f"Frame generation: [cyan]{frame_generation.value}[/cyan]",
                f"Frame multiplier: [cyan]{fg_multiplier}x[/cyan]",
                f"Proxy DLL: [cyan]{optiscaler_proxy}[/cyan]",
            )
        )
    else:
        plan.extend(
            (
                f"LumeniteFX: [{'green' if lumenite else 'dim'}]{'enabled' if lumenite else 'disabled'}[/]",
                f"DirectX 9 translation: [{'green' if d3d9 else 'dim'}]{'enabled' if d3d9 else 'disabled'}[/]",
                f"OpenGL mode: [{'green' if opengl else 'dim'}]{'enabled' if opengl else 'disabled'}[/]",
                f"Vulkan layer: [{'green' if vulkan_layer else 'dim'}]{'enabled' if vulkan_layer else 'disabled'}[/]",
            )
        )
    plan.append(f"Refresh downloads: [{'yellow' if force_download else 'dim'}]{'yes' if force_download else 'no'}[/]")
    console.print(Panel.fit("\n".join(plan), border_style="cyan"))

    success: bool = run_install(
        game_exe_path=exe,
        install_lumenite=lumenite,
        d3d9_translate=d3d9,
        opengl=opengl,
        install_vulkan_layer=vulkan_layer,
        force_download=force_download,
        verbose=verbose,
        strategy=engine,
        optiscaler_archive=optiscaler_archive,
        optiscaler_nr_passes=nr_passes,
        optiscaler_proxy=optiscaler_proxy,
        optiscaler_frame_generation=frame_generation,
        optiscaler_fg_multiplier=fg_multiplier,
        optiscaler_nr_placement=nr_placement,
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
    _show_activation_guide(rec, lumenite)


@app.command(
    name="uninstall",
    help="Remove a managed setup and restore the original game files.",
    rich_help_panel="Game setup",
)
def uninstall_cmd(
    target: Annotated[
        str,
        typer.Argument(
            help="Path to the game executable or directory, or a managed executable name",
        ),
    ],
) -> None:
    resolved_target = _resolve_target_or_exit(target)
    console.print(f"[bold red]Uninstalling DLSS5 Enabler from:[/bold red] {resolved_target}")
    success: bool = run_uninstall(resolved_target)
    if not success:
        console.print(
            f"[bold red]Uninstallation failed. Check log file: {get_log_dir() / 'dlss5-enabler.log'}[/bold red]"
        )
        raise typer.Exit(code=1)


@app.command(
    name="update",
    help="Update a managed game while preserving its saved engine and options.",
    epilog=(
        "Use 'dlss5-enabler switch GAME ENGINE' when you want to change engines. "
        "The older 'update GAME --engine ENGINE' form remains supported."
    ),
    rich_help_panel="Game setup",
)
def update_cmd(
    target: Annotated[
        str,
        typer.Argument(
            help="Managed game executable, game directory, or executable name shown by the list command",
        ),
    ],
    reinstall: Annotated[
        bool,
        typer.Option("--reinstall", help="Reapply the saved setup even when it is already current"),
    ] = False,
    force_download: Annotated[
        bool,
        typer.Option("--force-download", "-f", help="Ignore cached downloads and fetch components again"),
    ] = False,
    engine: Annotated[
        InstallStrategy | None,
        typer.Option(
            "--engine",
            help="Compatibility option for switching engines; new commands should use 'switch'",
            rich_help_panel="Engine switch compatibility",
        ),
    ] = None,
    frame_generation: Annotated[
        FrameGenerationMode | None,
        typer.Option(
            "--frame-generation",
            help="OptiScaler only: change the saved backend (auto, off, fsr, or dlssg)",
            rich_help_panel="OptiScaler overrides",
        ),
    ] = None,
    fg_multiplier: Annotated[
        int | None,
        typer.Option(
            "--fg-multiplier",
            min=2,
            max=6,
            help="OptiScaler only: change the saved DLSSG frame multiplier",
            rich_help_panel="OptiScaler overrides",
        ),
    ] = None,
    nr_placement: Annotated[
        NrPlacement | None,
        typer.Option(
            "--nr-placement",
            help="OptiScaler only: change where NR runs (after, before, or inside the upscaler)",
            rich_help_panel="OptiScaler overrides",
        ),
    ] = None,
    optiscaler_archive: Annotated[
        Path | None,
        typer.Option(
            "--optiscaler-archive",
            help="OptiScaler only: replace or import the supported y4my4my4m v3 ZIP",
            rich_help_panel="OptiScaler overrides",
        ),
    ] = None,
    nr_passes: Annotated[
        int | None,
        typer.Option(
            "--nr-passes",
            min=1,
            max=5,
            help="OptiScaler only: change the saved Neural Rendering pass count",
            rich_help_panel="OptiScaler overrides",
        ),
    ] = None,
    optiscaler_proxy: Annotated[
        str | None,
        typer.Option(
            "--optiscaler-proxy",
            help="OptiScaler only: change the saved proxy DLL filename",
            rich_help_panel="OptiScaler overrides",
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable verbose debug logging"),
    ] = False,
) -> None:
    setup_logger(verbose=verbose)
    resolved_target = _resolve_target_or_exit(target)
    _check_cli_update()
    if engine is InstallStrategy.RENODX and any(
        value is not None
        for value in (
            optiscaler_archive,
            nr_passes,
            optiscaler_proxy,
            frame_generation,
            fg_multiplier,
            nr_placement,
        )
    ):
        console.print(
            "[bold red]OptiScaler options cannot be used with RenoDX. Remove them or choose optiscaler.[/bold red]"
        )
        raise typer.Exit(code=2)
    if fg_multiplier is not None and fg_multiplier != 2 and frame_generation is not FrameGenerationMode.DLSSG:
        console.print("[bold red]--fg-multiplier above 2 requires --frame-generation dlssg.[/bold red]")
        raise typer.Exit(code=2)
    requested_engine = engine.value if engine is not None else "saved engine"
    action = "Switching or reapplying" if engine is not None else ("Reinstalling" if reinstall else "Updating")
    plan = [
        f"[bold cyan]{action} game setup[/bold cyan]",
        f"Game: [yellow]{resolved_target}[/yellow]",
        f"Engine: [green]{requested_engine}[/green]",
    ]
    optiscaler_overrides = (
        ("OptiScaler package", str(optiscaler_archive) if optiscaler_archive is not None else None),
        ("Neural Rendering passes", str(nr_passes) if nr_passes is not None else None),
        ("Proxy DLL", optiscaler_proxy),
        ("Frame generation", frame_generation.value if frame_generation is not None else None),
        ("Frame multiplier", f"{fg_multiplier}x" if fg_multiplier is not None else None),
        ("NR placement", nr_placement.value if nr_placement is not None else None),
    )
    plan.extend(f"{label}: [cyan]{value}[/cyan]" for label, value in optiscaler_overrides if value is not None)
    plan.append(f"Refresh downloads: [{'yellow' if force_download else 'dim'}]{'yes' if force_download else 'no'}[/]")
    console.print(Panel.fit("\n".join(plan), border_style="cyan"))
    result = run_update(
        resolved_target,
        reinstall=reinstall,
        force_download=force_download,
        verbose=verbose,
        log=console.print,
        strategy=engine,
        optiscaler_archive=optiscaler_archive,
        optiscaler_nr_passes=nr_passes,
        optiscaler_proxy=optiscaler_proxy,
        optiscaler_frame_generation=frame_generation,
        optiscaler_fg_multiplier=fg_multiplier,
        optiscaler_nr_placement=nr_placement,
    )
    style = "green" if result.success else "red"
    console.print(f"[bold {style}]{result.message}[/bold {style}]")
    if not result.success:
        raise typer.Exit(code=1)
    if result.status in {GameUpdateStatus.UPDATED, GameUpdateStatus.REINSTALLED}:
        record = record_load(resolved_target.parent if resolved_target.is_file() else resolved_target)
        _show_activation_guide(record, result.options.lumenite if result.options is not None else True)


@app.command(
    name="switch",
    help="Change the rendering engine of an existing managed game.",
    epilog=(
        "Examples: dlss5-enabler switch game.exe renodx | "
        "dlss5-enabler switch game.exe optiscaler --optiscaler-archive package.zip"
    ),
    rich_help_panel="Game setup",
)
def switch_cmd(
    target: Annotated[
        str,
        typer.Argument(help="Managed game executable, game directory, or executable name shown by the list command"),
    ],
    engine: Annotated[
        InstallStrategy,
        typer.Argument(help="New engine: renodx or optiscaler"),
    ],
    optiscaler_archive: Annotated[
        Path | None,
        typer.Option(
            "--optiscaler-archive",
            help="OptiScaler only: path to the supported y4my4my4m v3 ZIP",
            rich_help_panel="OptiScaler options",
        ),
    ] = None,
    nr_passes: Annotated[
        int | None,
        typer.Option(
            "--nr-passes",
            min=1,
            max=5,
            help="OptiScaler only: Neural Rendering passes; defaults to the saved value when available",
            rich_help_panel="OptiScaler options",
        ),
    ] = None,
    optiscaler_proxy: Annotated[
        str | None,
        typer.Option(
            "--optiscaler-proxy",
            help="OptiScaler only: proxy DLL filename",
            rich_help_panel="OptiScaler options",
        ),
    ] = None,
    frame_generation: Annotated[
        FrameGenerationMode | None,
        typer.Option(
            "--frame-generation",
            help="OptiScaler only: choose auto, off, fsr, or dlssg",
            rich_help_panel="OptiScaler options",
        ),
    ] = None,
    fg_multiplier: Annotated[
        int | None,
        typer.Option(
            "--fg-multiplier",
            min=2,
            max=6,
            help="OptiScaler only: DLSSG frame multiplier from 2x to 6x",
            rich_help_panel="OptiScaler options",
        ),
    ] = None,
    nr_placement: Annotated[
        NrPlacement | None,
        typer.Option(
            "--nr-placement",
            help=(
                "OptiScaler only: after favors image quality; before can improve FPS; "
                "inside experimentally runs NR within the upscaler"
            ),
            rich_help_panel="OptiScaler options",
        ),
    ] = None,
    force_download: Annotated[
        bool,
        typer.Option("--force-download", "-f", help="Ignore cached downloads and fetch components again"),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable verbose debug logging"),
    ] = False,
) -> None:
    update_cmd(
        target=target,
        reinstall=False,
        force_download=force_download,
        engine=engine,
        optiscaler_archive=optiscaler_archive,
        nr_passes=nr_passes,
        optiscaler_proxy=optiscaler_proxy,
        frame_generation=frame_generation,
        fg_multiplier=fg_multiplier,
        nr_placement=nr_placement,
        verbose=verbose,
    )


@app.command(name="list", help="List games currently managed by DLSS5 Enabler.", rich_help_panel="Inspect and maintain")
def list_cmd() -> None:
    _check_cli_update()
    entries = index_load_active()
    if not entries:
        console.print("[dim]No installed games found in DLSS5 Enabler index.[/dim]")
        return

    table = Table(title="DLSS5 Enabler Installed Games", border_style="cyan")
    table.add_column("Executable", style="green", no_wrap=True)
    table.add_column("Type / Arch", style="yellow")
    table.add_column("Version", style="cyan")
    table.add_column("Status")
    table.add_column("Directory", style="blue")

    current_version = get_tool_version()
    for e in entries:
        exe_name = Path(e.game_exe).name
        status = get_install_version_status(e.tool_version, current_version)
        table.add_row(
            exe_name,
            f"{e.install_type} / {e.architecture}",
            e.tool_version,
            _version_status_markup(status),
            e.game_dir,
        )

    console.print(table)


@app.command(name="info", help="Inspect a game and its saved setup.", rich_help_panel="Inspect and maintain")
def info_cmd(
    target: Annotated[
        str,
        typer.Argument(
            help="Path to game executable (.exe), or its managed executable name",
        ),
    ],
) -> None:
    exe = _resolve_target_or_exit(target, executable_required=True)
    _check_cli_update()
    arch = detect_pe_arch(exe)
    writable = file_is_writable(exe)
    game_dir = exe.parent
    rec = record_load(game_dir)
    detected_apis = detect_game_apis(exe)
    if not detected_apis:
        detected_apis = list(analyze_capabilities(exe, rec).apis)
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
        current_version = get_tool_version()
        status = get_install_version_status(rec.tool_version, current_version)
        options = rec.install_options
        table.add_row("Installed By Version", rec.tool_version)
        table.add_row("Current CLI Version", current_version)
        table.add_row("Install Status", _version_status_markup(status))
        table.add_row("Record Schema", str(rec.schema_version))
        table.add_row("Installed Engine", rec.strategy.value)
        if isinstance(rec.strategy_options, OptiScalerStrategyOptions):
            table.add_row(
                "Saved Options",
                f"Variant={rec.strategy_options.variant}, Proxy={rec.strategy_options.proxy_name}, "
                f"NR passes={rec.strategy_options.nr_passes}, "
                f"NR placement={rec.strategy_options.nr_placement.value}, "
                f"Frame generation={rec.strategy_options.frame_generation.value}, "
                f"FG multiplier={rec.strategy_options.fg_multiplier}x, "
                f"GPU profile={rec.strategy_options.gpu_generation}, "
                f"Revision={rec.strategy_options.source_revision}",
            )
        else:
            table.add_row(
                "Saved Options",
                f"Lumenite={'Yes' if options.lumenite else 'No'}, "
                f"D3D9={'Yes' if options.d3d9 else 'No'}, "
                f"OpenGL={'Yes' if options.opengl else 'No'}, "
                f"Vulkan={'Yes' if options.vulkan_layer else 'No'}",
            )
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


@app.command(name="version", help="Show the installed CLI version.", rich_help_panel="Inspect and maintain")
def version_cmd(
    check: Annotated[bool, typer.Option("--check", help="Check PyPI now for a newer release")] = False,
) -> None:
    current = get_tool_version()
    console.print(f"DLSS5 Enabler {current}")
    if check:
        result = _check_cli_update(force=True)
        if result.error:
            console.print("[yellow]Could not check PyPI for a newer version.[/yellow]")
        elif not result.update_available and result.latest_version is not None:
            console.print(f"Latest published version: {result.latest_version}")


@app.command(name="cache", help="Show or clear downloaded component files.", rich_help_panel="Inspect and maintain")
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
