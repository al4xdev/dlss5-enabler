# DLSS5 Enabler

Transactional command-line installer for managing the DLSS5-Feeder Neural Rendering stack in Windows games, either natively or through Wine and Proton.

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![CI](https://github.com/al4xdev/dlss5-enabler/actions/workflows/ci.yml/badge.svg)](https://github.com/al4xdev/dlss5-enabler/actions/workflows/ci.yml)
[![Typing](https://img.shields.io/badge/typing-mypy%20%2B%20pyright%20strict-blue)](pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)

DLSS5 Enabler automates a setup that would otherwise require manually coordinating several upstream projects, selecting the correct 32-bit or 64-bit binaries, configuring ReShade, editing Wine overrides, and preserving enough state to undo every change later. The CLI runs on Windows, Linux, and SteamOS, while the installation target is always a Windows PE executable.

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

## Engineering scope

Although the user-facing job is game mod orchestration, the project exercises broader software-engineering concerns:

- PE32 and PE32+ binary inspection, architecture selection, and graphics API heuristics;
- provider-neutral upstream discovery with bounded HTTP retries and immutable fallbacks;
- streaming downloads, content hashing, cache identity, and software supply-chain validation;
- safe ZIP and 7z extraction across Windows and POSIX path conventions;
- transactional filesystem and Wine registry changes with rollback and byte-for-byte restoration;
- typed metadata migrations that preserve installation choices across CLI upgrades;
- cross-platform packaging and automated validation on Windows, Linux, and macOS.

macOS is a CI portability target for the Python, archive, cache, and packaging layers. It is not presented as a DLSS runtime or game-installation target.

## Reliability by design

The project is built to fail safely when downloads, permissions, extraction, or configuration do not behave as expected.

- **Transactional installation:** all 12 stages participate in rollback, including the stage that reports the failure.
- **Recoverable refreshes:** an existing managed installation is snapshotted before replacement and restored if the new installation fails.
- **Safe uninstallation:** original DLLs, INI values, and Wine registry values are restored before metadata is removed.
- **Atomic writes:** records, indexes, cache metadata, registry files, and INI files are written through a temporary file followed by replacement.
- **Concurrent-operation locks:** per-game operations and shared state use filesystem locks to prevent overlapping writes.
- **Non-destructive backups:** existing files receive unique backup names; an older backup is never silently overwritten.
- **Validated downloads:** HTTPS certificate verification remains enabled, incomplete downloads use isolated temporary files, and an existing valid destination survives a failed refresh.
- **Validated fallback:** incompatible latest artifacts produce a warning and fall back to a pinned revision with an exact SHA-256 check.
- **Safe extraction:** archive members are checked for absolute paths, parent traversal, and flattened-name collisions before extraction.
- **Cache identity checks:** cached components are tied to their source URL, version or revision, and SHA-256 digest.
- **Preflight resolution:** every required upstream is downloaded and validated before an existing installation is removed or a game file is changed.
- **Strict verification:** Ruff, Mypy strict, Pyright strict, and the complete test suite run through one command.

These protections reduce the chance of a broken game directory, but they cannot guarantee compatibility with every game, mod loader, anti-cheat system, or upstream release.

## Installation paths

| Rendering path | Mode | Installed integration |
| --- | --- | --- |
| DirectX 11 / 12 | Default | ReShade `dxgi.dll` |
| DirectX 9 | `--d3d9` | dgVoodoo2 translation plus the 64-bit feeder host when required |
| OpenGL | `--opengl` | ReShade `opengl32.dll` |
| Vulkan | `--vulkan-layer` | Feeder Vulkan-layer fallback when available upstream |
| 32-bit games | Automatic detection | 32-bit feeder addon with a 64-bit host bridge |

These are implemented installer paths, not claims of universal compatibility with every engine or game. Automated tests validate API detection, architecture selection, file placement, configuration, rollback, and cleanup with synthetic binaries. Actual rendering compatibility still depends on the game, driver, mod stack, and upstream components.

## Platform and target model

| Environment | Role |
| --- | --- |
| Windows | Runs the CLI and manages a native Windows game executable |
| Linux / SteamOS | Runs the CLI and manages a Windows game executable through Wine or Proton |
| macOS | Runs portability, packaging, and synthetic-artifact checks in CI only |

The installation target must be a Windows PE executable. Native Linux ELF binaries are inspected for diagnostics but are rejected as installation targets, and the project does not claim a native macOS DLSS runtime.

## Requirements

- Python 3.10 or newer
- [`uv`](https://docs.astral.sh/uv/) (recommended) or pip
- An NVIDIA RTX GPU supported by the downloaded NGX runtime
- A game installation you can write to
- Internet access for the first component download

## Install

Run the latest release without installing it:

```console
uvx dlss5-enabler@latest --help
uvx dlss5-enabler@latest info "/path/to/game.exe"
uvx dlss5-enabler@latest install "/path/to/game.exe"
```

For frequent use, install the latest release persistently with `uv`:

```console
uv tool install dlss5-enabler@latest
dlss5-enabler --help
```

Update a persistent `uv` installation with `uv tool upgrade dlss5-enabler`.

### Install with pip

```console
python -m pip install --upgrade dlss5-enabler
dlss5-enabler --help
```

### Run from source

From a local checkout:

```console
uv sync
uv run dlss5-enabler --help
uv run dlss5-enabler info "/path/to/game.exe"
uv run dlss5-enabler install "/path/to/game.exe"
```

On Windows, PowerShell and `cmd.exe` paths work normally:

```console
dlss5-enabler install "C:\Games\Example\game.exe"
```

On SteamOS or Linux, point to the Windows executable inside the Steam library:

```console
dlss5-enabler install "/home/deck/.local/share/Steam/steamapps/common/Example/game.exe"
```

When the matching Proton prefix can be identified, the tool updates the required Wine DLL override and prints the corresponding Steam launch options.

## Commands

### Inspect a game

```console
dlss5-enabler info "/path/to/game.exe"
```

Reports architecture, imported graphics APIs, write access, installed and current tool versions, saved options, component versions, and a detected Proton prefix.

For a game already managed by DLSS5 Enabler, `info`, `install`, `update`, and `uninstall` also accept its executable name, for example `dlss5-enabler info Control_DX12.exe`. If more than one managed game has that name, the CLI lists the matching paths and requires the full path.

### Install

```console
dlss5-enabler install [OPTIONS] "/path/to/game.exe"
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

### Update a managed game

```console
dlss5-enabler update "/path/to/game.exe"
dlss5-enabler update Control_DX12.exe
```

`update` reads the installation metadata beside the game, then transactionally reapplies the same LumeniteFX, D3D9, OpenGL, and Vulkan choices with the components managed by the current CLI. Use `--reinstall` to reapply an already-current installation or `--force-download` to bypass component caches. A game installed by a newer CLI is never downgraded.

### Uninstall

```console
dlss5-enabler uninstall "/path/to/game.exe"
```

The target may be the game executable or its directory. For a uniquely managed game, its executable name also works:

```console
dlss5-enabler uninstall Control_DX12.exe
```

If multiple managed games have the same executable name, the command lists the matching paths and requires a full path. Only files and settings recorded by DLSS5 Enabler are reverted.

### List managed games

```console
dlss5-enabler list
```

The list compares each saved installation version with the running CLI locally; it does not make one network request per game.

### Inspect or clear the download cache

```console
dlss5-enabler cache
dlss5-enabler cache --clean
```

### Show or check the CLI version

```console
dlss5-enabler version
dlss5-enabler version --check
```

`install`, `update`, `info`, and `list` perform a non-blocking PyPI version check at most once every 24 hours per shared cache. The marker is empty and stores no version data. A newer release only produces an update recommendation; the CLI never updates itself. Use `uv tool upgrade dlss5-enabler` or `python -m pip install --upgrade dlss5-enabler` to update explicitly.

## Installation pipeline

An installation runs these stages in order:

1. Validate the executable, architecture, graphics API hints, permissions, and requested mode.
2. Discover, download, and validate every required upstream component.
3. Snapshot and remove a previous managed installation when refreshing.
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

## Upstream fallback policy

The wheel contains `dlss5_enabler/upstreams.json`, which pins a known-compatible fallback for every downloaded component. A normal installation still tries the newest upstream revision first. The candidate is downloaded to an isolated temporary file and checked for HTTPS provenance, size or digest when published, archive safety, required contents, supported layout, and architecture before entering the cache.

When the latest revision cannot be discovered, downloaded, or validated, the CLI emits an `UPSTREAM_*` warning and tries the pinned fallback. A fallback is accepted only when its exact SHA-256 and content policy match the embedded manifest. The successful installation summary lists every fallback used. If both candidates fail, the command stops without cleaning an existing installation or modifying the game.

The main warning codes distinguish discovery, missing or ambiguous assets, timeout, rejected HTTP responses, digest mismatch, unsafe archives, missing content, unsupported formats, fallback use, and fallback failure. The detailed log includes the component and revisions involved without exposing authenticated URLs.

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
├── operations/    Transactional install, update, and uninstall pipelines
├── platform/      Windows, Linux, Wine, Proton, and Steam discovery adapters
├── check.py       Unified quality runner
└── cli.py         Typer command-line interface
```

```mermaid
flowchart TB
    user([User]) --> cli["Typer CLI<br/>info · install · update · uninstall<br/>list · cache · version · check"]

    subgraph target["Target and host inspection"]
        pe["PE and API analysis<br/>PE32 / PE32+ · DXGI · D3D9 · OpenGL · Vulkan"]
        platform_contract["PlatformAdapter contract"]
        platform_impl["WindowsAdapter / LinuxAdapter"]
        proton["ProtonManager / WineRegParser<br/>Steam prefix discovery and DLL overrides"]
        platform_contract --> platform_impl
    end

    cli --> pe
    cli --> platform_contract

    subgraph operations["Transactional operations"]
        install["install"] --> pipeline["12-stage installation pipeline<br/>validate · preflight · apply · record · commit"]
        update["update"] -->|"reuse saved options"| pipeline
        uninstall["uninstall"] --> recovery["Snapshot and recorded-mutation reversion"]
        pipeline -->|"any stage fails"| rollback["Reverse-order rollback"]
        rollback --> recovery
    end

    cli --> install
    cli --> update
    cli --> uninstall
    pe --> pipeline
    platform_impl --> pipeline
    proton --> pipeline

    subgraph supply["Upstream resolution and supply-chain validation"]
        fetch["Component source functions"] --> provider["DownloadSourceAdapter<br/>provider-neutral contract"]
        provider --> github["GitHubDownloadSourceAdapter<br/>releases · files · snapshots · archives"]
        github --> upstreams["GitHub repositories"]
        fetch --> direct["Direct discovery<br/>ReShade site and RHI-provided NVIDIA URLs"]
        github --> resolver["UpstreamResolver"]
        direct --> resolver
        manifest["Embedded upstreams.json<br/>pinned revisions · SHA-256 · required members<br/>architecture and format bounds"] --> resolver
        resolver --> http["Bounded HTTP<br/>deadlines · classified retries · curl fallback"]
        http --> hosts["Original upstream hosts"]
        resolver <--> cache["Locked, atomic cache<br/>URL + revision + SHA-256 identity"]
        resolver --> validation["HTTPS, digest, size, archive safety,<br/>CRC, content, format, and architecture validation"]
        validation --> bundles["Validated component bundles<br/>Feeder + Vulkan · RenoDX · NGX NR/SR<br/>ReShade + headers · LumeniteFX · dgVoodoo2"]
    end

    pipeline -->|"preflight before mutation"| fetch
    bundles --> pipeline

    subgraph mutation["Managed state and reversible mutations"]
        fileio["Locked atomic file I/O<br/>safe archive extraction and unique backups"]
        ini["Exact-case INI mutations"]
        registry["Wine / Proton registry mutations"]
        game["Windows PE game directory<br/>hook DLLs · addons · shaders · NGX · optional layers"]
        record["Per-game dlss5-enabler.install.json<br/>tool version · options · files · original values"]
        index["Global managed-game index"]
        fileio --> game
        ini --> game
        registry --> proton_prefix["Wine / Proton prefix"]
        record --> index
    end

    pipeline --> fileio
    pipeline --> ini
    pipeline --> registry
    pipeline --> record
    update -->|"read metadata"| record
    uninstall -->|"read metadata"| record
    recovery -->|"restore bytes and settings"| game
    recovery -->|"restore overrides"| proton_prefix
    recovery -->|"preserve or remove consistently"| record

    subgraph verification["Verification and delivery"]
        ci["GitHub Actions<br/>Ubuntu · Windows · macOS<br/>Python 3.10–3.13"] --> check["Unified check<br/>Ruff · Mypy strict · Pyright strict · Pytest"]
        ci --> package["Build and isolated-install verification<br/>wheel resources · uv · pip"]
        fixtures["Offline synthetic PE32 / PE32+<br/>archives and fake component binaries"] --> check
    end

    check -.-> cli
    check -.-> pipeline
    package -.-> manifest
```

The Python import namespace uses an underscore (`dlss5_enabler`); the package and executable use a hyphen (`dlss5-enabler`).

GitHub is implemented behind the provider-neutral download-source contract in `network/adapters.py`. Release, repository-file, snapshot, and archive discovery stay in the adapter; component validation and fallback policy stay in the resolver. A future mirror should implement the same adapter contract and return the same neutral asset models instead of adding provider-specific branches to component code.

## Development

Install all dependencies and run the complete verification suite:

```console
uv sync
uv run dlss5-enabler check
```

`uv run` is reserved for commands executed from a project checkout. Installed users should use `dlss5-enabler` directly or `uvx dlss5-enabler@latest` for ephemeral execution.

The unified check requires all of the following to pass with no warnings:

- Ruff formatting
- Ruff linting
- Mypy strict type checking
- Pyright strict type checking
- Pytest

Maintainers can inspect a candidate pin without modifying the manifest:

```console
uv run dlss5-enabler-update-upstream COMPONENT REVISION ASSET_NAME HTTPS_URL
```

The command downloads to a temporary directory, validates the component layout, and prints the resolved revision, size, SHA-256, and recognized format. Add `--write --manifest dlss5_enabler/upstreams.json` only after reviewing that output; the tool never chooses `latest` or rewrites the manifest implicitly.

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

## Disclaimer

DLSS5 Enabler is an independent, unofficial community project. It is not an NVIDIA product, is not affiliated with NVIDIA Corporation, and is not sponsored, reviewed, approved, or endorsed by NVIDIA or any of the upstream projects named in this document. NVIDIA, DLSS, GeForce, and related names and marks belong to NVIDIA Corporation in the United States and other countries. Other names and marks belong to their respective owners.

The maintainer develops only the installer and orchestration code in this repository. The maintainer does not create, own, host, bundle, redistribute, audit, warrant, support, or maintain NVIDIA DLSS/NGX binaries or the third-party components installed by this tool. DLSS5 Enabler only discovers release information and directs downloads to the original upstream websites, repositories, manifests, or content servers at runtime. Availability, licensing, integrity, compatibility, behavior, and support for those downloads remain the responsibility of their respective providers.

Report problems with DLSS5 Enabler's installation, rollback, detection, or command-line behavior in this project's issue tracker. Report problems inside DLSS, DLSS5-Feeder, RenoDX, ReShade, LumeniteFX, dgVoodoo2, or another downloaded component to that component's own maintainer.

Use this tool at your own risk. Back up important game files and respect each game's modding, multiplayer, and anti-cheat policies. No guarantee is made that any particular game, driver, GPU, mod stack, or future upstream release will work.
