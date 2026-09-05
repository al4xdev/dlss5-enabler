# Changelog

All notable user-facing changes to DLSS5 Enabler are documented here.

## 1.2.1 - 2026-09-05

- Replaced the initial local-only OptiScaler V3 source with automatic acquisition of the official y4my4my4m V4 `_with_DLSS.7z` release asset, pinned by SHA-256.
- Kept an explicit local archive option and compatibility for updating previously recorded V3 installations from their verified cache entry.
- Added initial OptiScaler support for native-DLSS x64 DirectX 12 games on Linux / Proton when the game prefix can be detected.
- Added transactional Wine / Proton proxy overrides, including recorded rollback and uninstall restoration.
- Retained Windows x64 support for DirectX 11 and DirectX 12, plus RTX 40-series and RTX 50-series GPU profiles for explicit DLSS-G from 2x through 6x. Automatic frame generation continues to select FSR.
- Kept Vulkan, DirectX 11 under Proton, and a DirectX 11-to-DirectX 12 Proton bridge outside the initial OptiScaler Linux support boundary.
- Documented the upstream RTX 4090 RoboCop test with Neural Rendering and Multi Frame Generation while limiting this project's current claim to synthetic Linux integration coverage from a Windows development host.
- Added PE Delay-Load Import Directory parsing (`IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT`) to ensure games delay-loading `d3d12.dll` and `dxgi.dll` (such as Death Stranding) are accurately recognized as DirectX 12 targets.
- Added automatic OptiScaler proxy detection (`--optiscaler-proxy auto`), dynamically selecting `winmm.dll` when `dxgi.dll` is absent or delay-loaded and game companion binaries (such as `bink2w64.dll` or `EOSSDK-Win64-Shipping.dll`) statically import `winmm.dll` (such as Death Stranding and Decima Engine games), while defaulting safely to `dxgi.dll` for standard DirectX 11/12 games.
- Disabled `LogToFile` by default in `OptiScaler.ini` to avoid real-time frame logging overhead, disk bloat, and micro-stutter during active gameplay.
- Documented real-world hardware smoke testing in Death Stranding on RTX 5060 Ti: achieving 40–50 FPS baseline with Neural Rendering `inside`, scaling to 120–140 FPS with Multi-Frame Generation (MFG / DLSSG), and contrasted modern engine temporal scaling vs older title overhead.
- Kept `inside` Neural Rendering placement experimental and non-default.

## 1.2.0 - 2026-09-05

- Added a separate transactional OptiScaler strategy for native-DLSS x64 DirectX 11/12 games on Windows.
- Added verified import and hash-addressed caching for the pinned y4my4my4m OptiScaler DLSSNR Multipass v3 archive.
- Added configurable Neural Rendering pass count and placement relative to the upscaler.
- Added `auto`, `off`, FSR, and DLSS-G frame-generation choices, with OptiScaler-only GPU-generation profiles for DLSS-G compatibility.
- Added explicit strategy persistence and the clearer `switch GAME ENGINE` command; `update --engine` remains compatible, while ordinary updates preserve the selected strategy.
- Added OptiScaler staging, archive-path validation, destination-collision checks, rollback, uninstall restoration, and cleanup of recorded runtime artifacts.
- Made DirectX 9 translation automatic for new RenoDX installations, with `--d3d9` and `--no-d3d9` as explicit overrides.
- Documented `before` upscaler placement as an optional performance tuning path. In a manual Control smoke test it improved performance without a visible quality loss, but results can vary by game, resolution, and driver.
- Documented experimental frame-generation results from Control: the FSR output worked even though the game has no native frame generation; the DLSS-G output reported an HDR10 requirement and did not work in this test. These observations are not a general compatibility guarantee.

## 1.1.4

- Added versioned installation records with chained migrations.
- Improved transactional recovery, concurrent index restoration, and cleanup of managed runtime artifacts.
