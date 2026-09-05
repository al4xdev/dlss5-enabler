# Changelog

All notable user-facing changes to DLSS5 Enabler are documented here.

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
