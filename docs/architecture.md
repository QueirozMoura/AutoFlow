# AutoFlow CLI architecture

## Scope and current state

AutoFlow CLI targets Linux automation and system administration using Python
3.12+. This document describes the intended architecture. At this stage, the
repository contains packaging configuration, documentation, reserved module
boundaries, and a Typer application with help, version, and read-only `status`.
Rich provides terminal presentation; `status --json` supports machine consumers.
No system-changing operations are implemented.

## Module responsibilities

| Location | Intended responsibility |
| --- | --- |
| `src/autoflow/cli.py` | Compose the Typer application and expose `main` |
| `src/autoflow/commands/` | Parse command-specific input and coordinate use cases |
| `src/autoflow/commands/status.py` | Invoke collection, serialize JSON, and render a Rich snapshot |
| `src/autoflow/core/system.py` | Collect typed read-only snapshots using psutil and standard library OS metadata |
| `src/autoflow/core/health.py` | Classify raw percentages using 70% and 90% thresholds |
| `src/autoflow/core/` | Own application rules, use cases, and adapter contracts |
| `src/autoflow/integrations/` | Implement Linux, psutil, and external-process adapters |
| `tests/` | Verify behavior with unit tests and controlled integration tests |
| `scripts/` | Hold narrowly scoped development or Linux helper scripts |
| `.github/workflows/` | Hold future GitHub Actions workflows |

The installed package name is `autoflow-cli`; its Python import name is
`autoflow`. Setuptools discovers packages below `src/`, and the `autoflow` console
script resolves to `autoflow.cli:main`.

The CLI callback handles global options and displays help when invoked without
arguments. The eager `--version` option reads installed package metadata and
exits successfully. CLI tests use Typer's `CliRunner` to validate this interface.

## Dependency direction

```text
CLI composition → command groups → core use cases and contracts
       │                                  ↑
       └── integration adapters ──────────┘
```

The CLI registers `status` centrally. Its command function delegates to
`collect_system_status`, then selects JSON serialization or `render_status`.
The collector returns typed dataclasses without terminal dependencies. At this
stage, `core/system.py` reads psutil directly; future adapters can be extracted
when additional use cases require them. OS identity uses `freedesktop_os_release`
and hostname uses the standard library because psutil does not provide those fields.

Future CLI composition will supply concrete adapters to use cases. Core
logic must not depend on Typer, Rich, or Bash.
Integrations may depend on contracts defined in the core. Command groups will
handle terminal presentation with Rich while delegating application decisions
to the core. Additional modules should be created only when a feature needs them.

## Execution model

Status reads each field independently. Expected OS and psutil failures produce
`None` and a field-specific error, preserving successful readings. JSON uses
dataclass serialization and plain stdout, with bytes and seconds as numeric
units; it never passes through Rich. Any read failure yields exit code 1 after
output, including for JSON. A complete snapshot yields exit code 0.

CPU uses a bounded 0.1-second psutil sample, avoiding the meaningless initial
nonblocking reading. RAM used is total minus available, matching psutil's
percentage. The disk target is the filesystem mounted at `/`. Presentation
formats byte counts in binary units and uptime as days, hours, minutes, seconds.
Pure threshold classification is separate from collection and color mapping.

A future command will validate arguments, invoke a core use case with explicit
dependencies, and render the result. Adapters will perform external I/O and
translate platform errors into actionable application errors. The CLI boundary
will map errors to readable messages and documented exit codes.

Imports must be free of operational side effects. No metrics collection,
filesystem changes, process execution, or privilege requests should occur merely
by importing the package.

## Linux and Bash integrations

Prefer Python APIs for portable, testable operations. Introduce Bash only when
it provides a clear Linux integration benefit. Launch processes with explicit
argument lists, timeouts, and checked results; avoid constructing shell commands
from user input. Document distribution-specific assumptions and required tools
when an integration is introduced.

## Security boundaries

- Default to least privilege; never elevate permissions implicitly.
- Validate paths and arguments before accessing resources or starting processes.
- Keep secrets out of source code, exceptions, terminal output, and logs.
- Define previews, confirmations, and recovery expectations before implementing
  destructive operations.
- Separate read-only inspection from operations that change system state.

These are implementation requirements for later stages, not implemented
protections in the current scaffold.

## Testing and quality

Use pytest for behavior-focused unit tests of core logic. Test commands through
Typer's testing interface once commands exist. Isolate external resources using
fakes, fixtures, and temporary directories. Integration tests must use controlled
resources and must not alter the host system by default.

Ruff is configured in `pyproject.toml` for linting and formatting with a Python
3.12 target. Future CI will run lint, format checks, and meaningful tests on Linux
with a documented Python version matrix. Empty directories are tracked with
`.gitkeep`; executable CI workflows are intentionally deferred.

## Staged evolution

The CLI foundation, read-only status snapshot, and mocked metric tests are in
place. Next add CI checks and define further command contracts.
Introduce automation only after adapter boundaries and operational
safeguards have been tested. Dependency locking, release automation, configuration
storage, and additional subsystems remain future design decisions.
