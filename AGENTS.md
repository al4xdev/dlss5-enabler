# AGENTS.md - AI Coding Assistant Instructions

Welcome, Agent. This document defines the engineering standards, architecture rules, anti-looping constraints, and operational protocols for working within the **`dlss5-enabler`** codebase.

---

## 1. Project Overview & Architecture

`dlss5-enabler` is a cross-platform Python CLI inspired by [ntqueryinformation/FeedKit](https://github.com/ntqueryinformation/FeedKit). It installs and manages DLSS5-Feeder, RenoDX, ReShade with Addon support, NVIDIA NGX binaries, and LumeniteFX for DirectX 9, 11, 12, OpenGL, and Vulkan games across **Windows, Linux, Wine, Proton, and SteamOS**.

### Directory Structure
```
dlss5-enabler/
├── dlss5_enabler/
│   ├── core/                  # Binary PE/ELF analysis, INI parser, structured logger, record models, utilities
│   │   ├── pe.py              # PE32/PE32+ & ELF32/ELF64 parser, DataDirectory[1] import scanner, API heuristics
│   │   ├── ini.py             # Exact-case INI parser preserving ReShade case-sensitivity
│   │   ├── record.py          # Pydantic models for per-game dlss5-enabler.install.json and global index
│   │   ├── logger.py          # Structured file and console logging
│   │   └── util.py            # Hashing, hardlink/copy fallbacks, empty dir cleanup
│   ├── network/               # Resilient networking & upstream component discovery
│   │   ├── http.py            # HTTP client (httpx streaming with retries + curl fallback)
│   │   └── sources.py         # Dynamic release discovery & caching for GitHub/ReShade/NVIDIA assets
│   ├── operations/            # Modular installation/uninstallation pipelines
│   │   ├── pipeline.py        # PipelineContext, PipelineStep (ABC), and PipelineRunner
│   │   ├── steps.py           # 12 isolated pipeline steps with rollback capabilities
│   │   ├── reshade.py         # Headless ReShade setup and py7zr in-process extraction
│   │   ├── install.py         # Installation orchestrator
│   │   └── uninstall.py       # Uninstallation orchestrator (with backup & Wine registry restoration)
│   ├── platform/              # Cross-Platform Adapter Subsystem
│   │   ├── base.py            # PlatformAdapter Abstract Base Class (OS primitives)
│   │   ├── windows.py         # WindowsAdapter (%LOCALAPPDATA%, Zone.Identifier ADS)
│   │   ├── linux.py           # LinuxAdapter (XDG Base Directory, chmod +x, POSIX normalization)
│   │   └── proton.py          # ProtonManager & WineRegParser (Steam prefix discovery, user.reg injection)
│   ├── check.py               # Unified Quality & Test Orchestrator
│   └── cli.py                 # Typer CLI user interface
├── tests/                     # Comprehensive pytest test suite (120+ unit tests)
├── pyproject.toml             # Package metadata, dependencies, linters, and strict type settings
└── README.md                  # User-facing documentation
```

---

## 2. Tooling & Environment Standards

- **Python Manager**: Use `uv` strictly. Never use system `pip` or global `python`.
- **Environment**: Virtual environment located at `.venv/`.
- **Command Invocation**: Always prefix CLI executions with `uv run` (e.g., `uv run dlss5-enabler check`).
- **Dependencies**: Managed exclusively in `pyproject.toml`. Run `uv sync` after editing dependencies.

---

## 3. Strict Coding & Codebase Rules

### 3.1 Zero Comments / Zero Docstrings in Source Code
- **Rule**: All Python source files (`dlss5_enabler/*.py`) must have **ZERO inline comments (`# ...`)** and **ZERO internal docstrings (`"""..."""`)**.
- **Rationale**: Keeps source code compact, token-efficient, and cleanly structured. All documentation belongs in `README.md` and `AGENTS.md`.

### 3.2 Ultra-Strict Type Checking (Mypy Strict + Pyright / Pylance Strict)
- `pyproject.toml` is configured with:
  - `mypy`: `strict = true`
  - `pyright`: `typeCheckingMode = "strict"`
- **Mandatory Requirements**:
  - No `Unknown` variable types, missing return types, or untyped parameters (`reportUnknownVariableType`, `reportUnknownParameterType`, `reportUnknownArgumentType`).
  - Variables assigned from external JSON / HTTP calls must be explicitly annotated (e.g. `data: dict[str, Any] = ...`).
  - Pydantic models with generic containers must use properly typed factories: `Field(default_factory=list)` or `Field(default_factory=dict)`.

### 3.3 Cross-Platform Adapter Pattern
- **Rule**: Never call `os.environ["LOCALAPPDATA"]` or hardcode Windows paths directly in business logic.
- Always route OS-level paths and primitives through `get_platform_adapter()` (`dlss5_enabler.platform`).
- File paths stored in `InstallRecord` must use canonical forward-slash POSIX representation (`.as_posix()`).

---

## 4. Unified Quality & Verification Protocol

Whenever you make any change to `dlss5_enabler/` or `tests/`, execute the unified quality orchestrator:

```bash
uv run dlss5-enabler check
```

This single command automatically executes:
1. **Ruff Format Check** (`uv run ruff format --check dlss5_enabler tests`)
2. **Ruff Strict Lint** (`uv run ruff check dlss5_enabler tests`)
3. **Mypy Strict Mode** (`uv run mypy --strict dlss5_enabler tests`)
4. **Pyright Strict Mode** (`uv run pyright dlss5_enabler tests`)
5. **Pytest Test Suite** (`uv run pytest -q`)

**Zero errors or warnings are tolerated across all five tools.**

---

## 5. Agent Anti-Looping & Execution Guidelines

To prevent execution freezes, subagent loops, or runaway processes:

1. **Subagent Lifecycle Management**:
   - Subagents run asynchronously in the background. Do not poll in a loop.
   - Use reactive timers or message wakeups.
   - Upon completion of a multi-agent workflow, always invoke `manage_subagents(Action="kill_all")` to cleanly terminate idle subagents and release memory.
2. **Watchdog Protocol**:
   - If a background command takes longer than 30 seconds, register and monitor the task ID via `manage_task(Action="status")`.
3. **No Silent Retries / Loop Prevention**:
   - If a linter check or unit test fails twice with the same diagnostic, stop. Read the exact traceback, isolate root cause, and correct the source before re-running.
4. **Token Management**:
   - Never dump raw binary outputs, large JSON payloads, or multi-hundred line logs directly into chat responses. State the summary, root cause, and action items concisely.
