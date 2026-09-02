# DLSS5 Enabler

Transactional command-line installer for the DLSS5-Feeder rendering stack on Windows, Linux, Wine, Proton, and SteamOS.

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-165%20passing-brightgreen)](tests/)
[![Typing](https://img.shields.io/badge/typing-mypy%20%2B%20pyright%20strict-blue)](pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)

DLSS5 Enabler automates the setup that would otherwise require manually downloading several projects, placing the correct 32-bit or 64-bit binaries, configuring ReShade, editing Wine overrides, and keeping enough state to undo everything later.

It combines:

- [DLSS5-Feeder](https://github.com/jlrouzies-fr/DLSS5-Feeder)
- [RenoDX](https://github.com/RankFTW/rhi-repo)
- [ReShade with Addon support](https://reshade.me/)
- NVIDIA NGX DLSS binaries discovered through the RenoDX manifest
- [LumeniteFX](https://github.com/umar-afzaal/LumeniteFX) motion-vector shaders
- [dgVoodoo2](https://github.com/dege-diosg/dgVoodoo2) for optional DirectX 9 translation

> [!IMPORTANT]
> DLSS5 Enabler is an unofficial community tool. It is not affiliated with or endorsed by NVIDIA, ReShade, RenoDX, or the other upstream projects. “DLSS 5” is used because that is how users commonly search for this stack; the tool does not add native engine integration and compatibility varies by game.

## Why this exists

The rendering stack spans multiple binaries, configuration files, graphics APIs, architectures, and operating systems. A failed manual installation can overwrite an existing hook DLL, leave a partial ReShade setup, or persist a Wine registry override after the files it points to are gone.

DLSS5 Enabler treats installation as a transaction. It validates the target first, records every managed mutation, and rolls changes back in reverse order if a later stage fails.

## Reliability by design

The project is built to fail safely when downloads, permissions, extraction, or configuration do not behave as expected.

- **Transactional installation:** all 12 stages participate in rollback, including the stage that reports the failure.
- **Recoverable refreshes:** an existing managed installation is snapshotted before replacement and restored if the new installation fails.
- **Safe uninstallation:** original DLLs, INI values, and Wine registry values are restored before metadata is removed.
- **Atomic writes:** records, indexes, cache metadata, registry files, and INI files are written through a temporary file followed by replacement.
- **Concurrent-operation locks:** per-game operations and shared state use filesystem locks to prevent overlapping writes.
- **Non-destructive backups:** existing files receive unique backup names; an older backup is never silently overwritten.
- **Validated downloads:** HTTPS certificate verification remains enabled, incomplete downloads use isolated temporary files, and an existing valid destination survives a failed refresh.
- **Safe extraction:** archive members are checked for absolute paths, parent traversal, and flattened-name collisions before extraction.
- **Cache identity checks:** cached components are tied to their source URL, version or revision, and SHA-256 digest.
- **Strict verification:** Ruff, Mypy strict, Pyright strict, and the complete test suite run through one command.

These protections reduce the chance of a broken game directory, but they cannot guarantee compatibility with every game, mod loader, anti-cheat system, or upstream release.

## Supported targets

| Target | Mode | Main hook |
| --- | --- | --- |
| DirectX 11 / 12 | Default | ReShade `dxgi.dll` |
| DirectX 9 | `--d3d9` | dgVoodoo2 translation plus the 64-bit feeder host when required |
| OpenGL | `--opengl` | ReShade `opengl32.dll` |
| Vulkan | `--vulkan-layer` | Feeder Vulkan-layer fallback when available upstream |
| 32-bit games | Automatic detection | 32-bit feeder addon with a 64-bit host bridge |

The target must be a Windows PE executable, either running directly on Windows or through Wine/Proton. Native Linux ELF binaries are inspected for diagnostics but are not installation targets.

## Requirements

- Python 3.10 or newer
- [`uv`](https://docs.astral.sh/uv/)
- An NVIDIA RTX GPU supported by the downloaded NGX runtime
- A game installation you can write to
- Internet access for the first component download

## Run from source

The package has not been published yet. From this repository:

```console
uv sync
uv run dlss5-enabler --help
uv run dlss5-enabler info "/path/to/game.exe"
uv run dlss5-enabler install "/path/to/game.exe"
```

On Windows, PowerShell and `cmd.exe` paths work normally:

```console
uv run dlss5-enabler install "C:\Games\Example\game.exe"
```

On SteamOS or Linux, point to the Windows executable inside the Steam library:

```console
uv run dlss5-enabler install "/home/deck/.local/share/Steam/steamapps/common/Example/game.exe"
```

When the matching Proton prefix can be identified, the tool updates the required Wine DLL override and prints the corresponding Steam launch options.

## Commands

### Inspect a game

```console
uv run dlss5-enabler info "/path/to/game.exe"
```

Reports architecture, imported graphics APIs, write access, the current managed installation, and a detected Proton prefix.

### Install

```console
uv run dlss5-enabler install [OPTIONS] "/path/to/game.exe"
```

Options:

| Option | Purpose |
| --- | --- |
| `--lumenite` / `--no-lumenite` | Enable or disable LumeniteFX; enabled by default |
| `--d3d9` | Install the dgVoodoo2 DirectX 9 translation path |
| `--opengl` | Use the OpenGL ReShade hook |
| `--vulkan-layer` | Request the Vulkan-layer fallback |
| `-f`, `--force-download` | Ignore cached assets and fetch them again |
| `-v`, `--verbose` | Enable detailed console and file logging |

`--d3d9` and `--opengl` cannot be combined.

### Uninstall

```console
uv run dlss5-enabler uninstall "/path/to/game.exe"
```

The target may be the game executable or its directory. Only files and settings recorded by DLSS5 Enabler are reverted.

### List managed games

```console
uv run dlss5-enabler list
```

### Inspect or clear the download cache

```console
uv run dlss5-enabler cache
uv run dlss5-enabler cache --clean
```

### Run project verification

```console
uv run dlss5-enabler check
```

## Installation pipeline

An installation runs these stages in order:

1. Validate the executable, architecture, graphics API hints, permissions, and requested mode.
2. Snapshot and remove a previous managed installation when refreshing.
3. Discover and fetch upstream components.
4. Install or extract the correct ReShade Addon DLL.
5. Configure dgVoodoo2 when DirectX 9 translation is requested.
6. Place the Feeder addon, shader, and ReShade headers.
7. Place RenoDX and the architecture-appropriate NGX binaries.
8. Place LumeniteFX and configure its motion-vector provider.
9. Install the Vulkan fallback when requested and available.
10. Mirror managed files into `bin/` for layouts that require it.
11. Apply Wine or Proton DLL overrides while recording their prior values.
12. Atomically save `dlss5-enabler.install.json` and update the global index.

If a stage fails, recorded mutations are reversed and any previous managed installation snapshot is restored.

## Local state

| Platform | Data and cache location |
| --- | --- |
| Windows | `%LOCALAPPDATA%\DLSS5 Enabler` |
| Linux / SteamOS | XDG data, cache, config, and state directories under `dlss5-enabler` |
| Per game | `dlss5-enabler.install.json` beside the game executable |

The log file is named `dlss5-enabler.log`.

## Architecture

```text
dlss5_enabler/
├── core/          Binary inspection, exact-case INI handling, records, atomic I/O
├── network/       HTTPS downloads, release discovery, cache validation
├── operations/    Transactional install and uninstall pipelines
├── platform/      Windows, Linux, Wine, Proton, and Steam discovery adapters
├── check.py       Unified quality runner
└── cli.py         Typer command-line interface
```

The Python import namespace uses an underscore (`dlss5_enabler`); the package and executable use a hyphen (`dlss5-enabler`).

## Development

Install all dependencies and run the complete verification suite:

```console
uv sync
uv run dlss5-enabler check
```

The unified check requires all of the following to pass with no warnings:

- Ruff formatting
- Ruff linting
- Mypy strict type checking
- Pyright strict type checking
- Pytest

## Upstream projects

DLSS5 Enabler is inspired by and builds on the work of:

- [FeedKit](https://github.com/ntqueryinformation/FeedKit)
- [DLSS5-Feeder](https://github.com/jlrouzies-fr/DLSS5-Feeder)
- [RenoDX / RHI](https://github.com/RankFTW/rhi-repo)
- [ReShade](https://github.com/crosire/reshade)
- [LumeniteFX](https://github.com/umar-afzaal/LumeniteFX)
- [dgVoodoo2](https://github.com/dege-diosg/dgVoodoo2)

Each downloaded component remains subject to its upstream license and terms.

## License

DLSS5 Enabler itself is distributed under the [MIT License](LICENSE).
