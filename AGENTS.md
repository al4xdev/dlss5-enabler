# AGENTS.md — dlss5-enabler

This file is the operational reference for agents working in this repository. It describes the current project. Do not preserve names, structures, or assumptions from early prototypes when the code shows a different reality.

## 1. Product and scope

`dlss5-enabler` is a transactional Python CLI for installing and removing the DLSS5-Feeder stack in Windows games. The CLI runs on Windows, Linux, Wine, Proton, and SteamOS, but the installation target is a Windows PE32 or PE32+ executable.

The managed stack can include:

- DLSS5-Feeder;
- RenoDX DLSS5;
- ReShade with addon support;
- NVIDIA NGX DLSS Neural Rendering and Super Resolution;
- official ReShade shader headers;
- LumeniteFX;
- dgVoodoo2 for optional DirectX 9 translation;
- the Vulkan layer supplied by Feeder.

The project prioritizes safe failure, rollback, byte-for-byte restoration, verifiable caching, x86/x64 compatibility, and consistent behavior across platforms.

## 2. Current architecture

```text
dlss5_enabler/
├── core/
│   ├── archive.py        validates extraction targets and blocks traversal
│   ├── fileio.py         locks, atomic writes, copies, and unique backups
│   ├── ini.py            exact-case INI parser
│   ├── logger.py         structured console and file logging
│   ├── pe.py             PE/ELF, architecture, import, and graphics API analysis
│   ├── record.py         per-game and global-index Pydantic records
│   └── util.py           hashes, permissions, cache paths, and filesystem helpers
├── network/
│   ├── adapters.py       provider contract and GitHub implementation
│   ├── http.py           streaming HTTP, classified retries, deadline, and curl
│   └── sources.py        component resolution, caching, download, and extraction
├── operations/
│   ├── install.py        installation pipeline composition
│   ├── pipeline.py       context, steps, commit, and rollback
│   ├── reshade.py        headless ReShade installation and extraction
│   ├── steps.py          isolated installation steps
│   └── uninstall.py      transactional removal and restoration
├── platform/
│   ├── base.py           platform primitive contract
│   ├── linux.py          XDG, permissions, and POSIX paths
│   ├── proton.py         prefix discovery and user.reg handling
│   └── windows.py        LocalAppData and Zone.Identifier handling
├── check.py              unified quality orchestrator
└── cli.py                Typer interface
```

Tests live in `tests/` and workflows in `.github/workflows/`.

## 3. Python tooling and environment

- Use `uv` exclusively. Never use system `pip` or global Python.
- The local virtual environment is `.venv/`.
- Run project commands through `uv run`.
- Dependencies belong only in `pyproject.toml`. After changing them, run `uv sync --all-groups` and update `uv.lock`.
- To run pytest by itself in this environment, use `uv run python -m pytest -q`.
- For official validation, always use `uv run dlss5-enabler check`.
- Validate packaging with `uv build`, and inspect the wheel and sdist whenever non-Python package resources are added.

## 4. Python source rules

### 4.1 Zero comments and zero docstrings in the package

Python files under `dlss5_enabler/` must not contain inline comments or internal docstrings. Documentation belongs in `README.md` or this file.

This rule does not prohibit user-facing strings, error messages, metadata, or Markdown documentation.

### 4.2 Strict typing

Mypy and Pyright run in strict mode.

- Every function must have typed parameters and a typed return value.
- Do not introduce `Any` beyond unavoidable boundaries such as external JSON. Convert and validate it immediately.
- Do not leave `Unknown` types from JSON, mocks, libraries, or containers.
- Use parameterized containers and typed factories in Pydantic models.
- Preserve compatibility with Python 3.10 through 3.13.

### 4.3 Platform adapters

- Do not read `LOCALAPPDATA`, prefix paths, or ADS details directly in business logic.
- Use `get_platform_adapter()` for operating-system primitives.
- Use `ProtonManager` and `WineRegParser` for prefixes and Wine registry handling.
- Persist paths in canonical POSIX form with `.as_posix()`.
- Do not assume archive separators match the host platform. Normalize both `/` and `\` before matching.

### 4.4 Files, locks, and rollback

- Use the helpers in `core/fileio.py` for shared resources, atomic writes, copies, and backups.
- Keep every shared read-modify-write sequence under the same lock.
- Never silently overwrite an older backup.
- Preserve a known-good destination when refresh, download, validation, or installation fails.
- Extract archives only through `safe_archive_destination()` or helpers that use it.
- Reject absolute paths, `..`, destination escapes, and collisions caused by flattening.
- A new pipeline step must implement error handling, rollback, and commit behavior consistent with its mutations.
- When a step fails, test residual state and restoration, not only its boolean return value.

## 5. Network, upstreams, and cache

### 5.1 Providers

GitHub-hosted downloads must pass through the contract in `network/adapters.py`. The current implementation is `GitHubDownloadSourceAdapter`. Do not duplicate release, asset, snapshot, raw-file, or codeload discovery in `sources.py`.

New mirrors must implement `DownloadSourceAdapter` and return the same provider-neutral models. Component logic must not depend on details of one provider API.

### 5.2 Discovery and URLs

- Never invent a `releases/latest/download` URL when the API did not publish the corresponding asset.
- Discover assets from provider metadata and validate their name, HTTPS URL, and revision.
- Missing or incompatible assets must clearly list what the upstream published.
- Repository files and archives must resolve to immutable commits rather than mutable branches.
- URLs obtained from external manifests also require HTTPS validation.
- Upstream layout changes must fail explicitly or use a validated fallback policy. Never continue with partial files.

### 5.3 Timeouts and retries

- Definitive 4xx responses, especially 404, fail on the first attempt and do not invoke curl.
- Only timeouts, network failures, and explicitly classified transient status codes may be retried.
- Every retry count must be finite and respect the global operation deadline.
- Curl fallback is allowed only for transient failures and must receive the remaining time, never a new unlimited deadline.
- Do not add `while True`, busy polling, or recursive retries.
- Keep partial downloads isolated and remove them when the operation finishes.

### 5.4 Cache

- Cache identity includes URL, revision, and SHA-256.
- An existing file without valid metadata is not a trusted cache entry.
- Complete downloads in a temporary file and promote them atomically.
- Failures must not truncate or remove an older valid destination.
- A release, URL, or hash change invalidates the cache.

## 6. Known component details

- Current Feeder releases may publish one `DLSS5-Feeder-*.zip` instead of the four individual assets used by older releases.
- The consolidated Feeder archive contains x86/x64 addons, the shader, host64, and may contain `layer-x64/` and `layer-x86/` directly.
- Vulkan installation must select only the game architecture. Keep support for the legacy `feed-vk-layer.zip` layout while tests cover it.
- RenoDX DLSS5 shares the `RankFTW/rhi-repo` release repository. Select only tags with the correct prefix.
- NGX binary URLs come from the `RankFTW/RHI` manifest and remain untrusted external input.
- ReShade Addon comes from the official website rather than GitHub, but it uses the same HTTP bounds and guarantees.
- ReShade headers and LumeniteFX must resolve to immutable commits.
- dgVoodoo2 requires the real release asset and the correct x86/x64 subdirectory. Do not assume versioned filenames.

### 6.1 Component integration playbook

Apply this playbook whenever adding or replacing a managed rendering engine, translation layer, hook, add-on, shader provider, DLSS runtime, or other game-side binary component.

1. Establish the component contract before coding: supported APIs and architectures, upstream ownership, required companion files, configuration, platform-specific behavior, and interactions with existing managed components.
2. Resolve upstream artifacts through the provider and cache contracts. Validate the real release layout, select the correct architecture, and add a pinned fallback when the component is a required upstream.
3. Map every mutation before installation: placed binaries, edited files and INI keys, registry or Wine changes, created directories, generated logs, screenshots, caches, shader outputs, and helper-process artifacts. Classify each as pre-existing user state, installer-owned state, or runtime-generated state.
4. Record or snapshot every installer-owned mutation so rollback and uninstall restore pre-existing bytes exactly. Runtime-generated artifacts must be explicitly included in the uninstall snapshot and cleanup only when their ownership is unambiguous.
5. Add isolated tests for discovery, architecture selection, safe placement, install/uninstall round trip, rollback after a later failure, and generated-artifact cleanup with restoration if finalization fails.
6. Perform an opt-in real-game smoke test when practical: inventory the target directory before installation, inspect it again after the game has run, compare the new files with the mutation map, then uninstall with the game closed and verify that only declared user-owned files remain.
7. If a component conflicts with native game functionality, detect the native capability early and select the least invasive compatible path. Do not install a generic hook merely because it is available.

## 7. Test strategy

### 7.1 Standard tests

- Standard unit and integration tests must not access the internet.
- Mock HTTP responses and create temporary archives in memory or under `tmp_path`.
- Use fake bytes for DLLs, addons, shaders, and executables when a stage only downloads, extracts, copies, or hashes them.
- When architecture detection is part of a test, use minimal structurally valid PE32 and PE32+ fixtures.
- Test every component independently even when cases look redundant. Different repositories and upstream layouts are independent contracts.
- For every fetch, cover success, the exact requested URL, a missing asset, an invalid response, and propagation into the pipeline.
- For HTTP, cover 404 without retry, bounded transient retries, total deadline, incomplete downloads, curl fallback, and preservation of an existing destination.
- For archives, cover Windows separators, flattening, collisions, traversal, missing members, and architecture selection.
- For cache and operations, cover round trips, invalidation, idempotency, relevant concurrency, and rollback.

Real downloads are manual, opt-in smoke tests only. Never make the standard suite depend on GitHub, ReShade, NVIDIA, or another upstream being available.

### 7.2 Required verification

After any change under `dlss5_enabler/` or `tests/`, run:

```bash
uv run dlss5-enabler check
```

It must pass:

1. Ruff format check;
2. Ruff lint;
3. Mypy strict;
4. Pyright strict;
5. the complete pytest suite.

No errors or warnings from those five tools are accepted. Before a release, also run `uv build`.

CI repeats the check and build on Ubuntu, Windows, and macOS with Python 3.10, 3.11, 3.12, and 3.13. A green Windows CI validates Python behavior and synthetic artifacts, but it is not equivalent to running a game, ReShade, or real upstream binaries.

## 8. Commits, signatures, and history

- Every commit in this repository must be signed. Use `git commit -S` and `git commit --amend -S` as appropriate.
- Every release tag must be annotated and signed with `git tag -s`.
- Before pushing, verify the commit with `git log -1 --show-signature` or `%G?`. The expected state is a valid signature.
- Before pushing a tag, run `git verify-tag <tag>`.
- Use the configured `al4xdev` identity. Never replace the author, email, or signing key with invented values.
- Never use `git push --force`. When a rewrite has explicit authorization, capture the remote SHA first and use `--force-with-lease=<ref>:<expected-sha>`.
- Prefer an atomic push when several related refs must change together.
- Create a recoverable bundle or backup before rewriting history.
- Do not move published version tags or rewrite their history without explicit authorization. Doing so can invalidate PyPI provenance references, alter releases, and trigger the publishing workflow again.
- Preserve user commits, files, and changes outside the task.

## 9. Release process

The `.github/workflows/publish.yml` workflow publishes every `v*` tag. Never push a tag as a test.

Required sequence:

1. Update the version in `pyproject.toml` and `uv.lock`.
2. Run the unified check.
3. Build the wheel and sdist locally.
4. Create a signed commit and verify its signature.
5. Push the primary branch.
6. Wait for the complete CI matrix to reach a successful terminal state.
7. Create an annotated, signed tag that exactly matches the package version.
8. Verify and push the tag.
9. Monitor validation, build, PyPI Trusted Publishing, and GitHub Release creation.
10. Confirm published files and provenance.

A network failure while reading status does not imply a failed deployment. Do not retry silently: report it, apply a timeout, and use another official source to verify the state.

## 10. Execution and loop prevention

- Do not busy-poll. Use process wait mechanisms or a watch tool with an interval and a terminal result.
- Do not leave long-running commands without progress updates.
- If the same test or lint diagnostic occurs twice, stop repeating it and fix the cause.
- Do not dump binaries, large JSON documents, or huge logs into the conversation. Summarize and preserve only necessary evidence.
- Windows Docker is not required for fetch, cache, archive, or synthetic pipeline tests. Prefer small fixtures to conserve memory.
- If subagents are explicitly used, do not poll them in a loop and terminate any that remain active.

## 11. Documentation

- README is user documentation. AGENTS.md is operational documentation for coding agents.
- Keep temporary investigation details out of tracked documentation.
