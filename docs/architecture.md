# AutoFlow CLI architecture

## Scope and current state

AutoFlow CLI targets Linux automation and system administration using Python
3.12+. This document describes the intended architecture. At this stage, the
repository contains packaging configuration, documentation, reserved module
boundaries, and a Typer application with help, version, and read-only `status`.
Rich provides terminal presentation; JSON supports machine consumers. `backup`
creates archives in an explicit destination. Restoration and scheduling are
outside the current scope.

## Module responsibilities

| Location | Intended responsibility |
| --- | --- |
| `src/autoflow/cli.py` | Compose the Typer application and expose `main` |
| `src/autoflow/commands/` | Parse command-specific input and coordinate use cases |
| `src/autoflow/commands/status.py` | Invoke collection, serialize JSON, and render a Rich snapshot |
| `src/autoflow/core/system.py` | Collect typed read-only snapshots using psutil and standard library OS metadata |
| `src/autoflow/core/health.py` | Classify raw percentages using 70% and 90% thresholds |
| `src/autoflow/commands/backup.py` | Parse backup options, render progress/results, and serialize JSON/errors |
| `src/autoflow/core/backup.py` | Discover safe entries, validate consistency, and create/publish archives |
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

### Backup execution and trust boundaries

`cli.py` registers `backup` centrally. The command invokes `create_backup` with
`pathlib.Path` inputs and optional dry-run and progress callback. Core types
`BackupResult` and `SkippedEntry` carry data without importing Typer or Rich.
Expected I/O, tar, compression, and validation errors become `BackupError`.
The command maps these to exit code 1, with one plain JSON error object when
requested; success is exit code 0. Usage errors remain Typer errors (code 2).

The core performs these steps:

1. Expand/normalize paths and reject protected sources, protected destinations,
   and destinations within the source tree. Open parent directories component
   by component using directory descriptors and `O_NOFOLLOW`. No subprocesses or
   shell interpolation are involved.
2. Inventory the source deterministically with no-follow stat/open operations.
   Retain regular files and directories, including empty ones. Exclude links,
   special files, nested mount points, and different devices with explicit
   reasons. Read Linux mountinfo to recognize same-device bind mounts too;
   inability to read it is fatal. Verify readability and metadata identities.
3. Validate existing destination components without creating anything. Compare
   directory identities to the source inventory to reject aliases into the
   source. Require an output directory owned by the effective user with no
   group/other write bits, protecting temporary names from other users. A dry
   run performs the same validation and returns the inventory and estimated
   uncompressed size.
4. For an actual backup, create missing destination directories with mode `0700`
   and an exclusively owned temporary `.tar.gz.part` file with mode `0600`.
5. Open each source entry through no-follow descriptor traversal, checking parent
   identities. Build `TarInfo` entries and pass already-open regular file streams
   to `tarfile`; never let tarfile discover paths or recurse. Regular hard links
   become independent regular members. Strip special permission bits from archive
   metadata. An optional callback reports completed/total entries to Rich.
6. Check device, inode, type/mode, owner, size, nanosecond mtime/ctime, and link
   count before/after reading. Re-scan and compare the entire retained inventory
   and exclusion list to catch additions, removals, and replacements. Atime is
   ignored because reading can change it. Reopen the source parent by its
   requested path and compare its identity to detect moved/replaced ancestors.
   Any detected inconsistency aborts.
7. Close gzip/tar output, flush and fsync the temporary file, then verify that the
   destination path still identifies the open destination directory and recheck
   its ownership and write permissions. Publish via
   Linux `renameat2(RENAME_NOREPLACE)` exposed through standard-library `ctypes`.
   Timestamp/UUID names are retried on collisions; neither pre-checks followed by
   ordinary rename nor overwriting fallbacks are used. Unsupported atomic rename
   semantics fail closed.
8. On failure/cancellation, unlink only the temporary name exclusively created
   by this invocation, through its pinned destination descriptor. Existing
   archives are untouched. Cleanup errors are explicit; newly created destination
   directories may remain. Return a successful result only after publication.

Archive names are relative and rooted at the source basename. JSON uses only
dataclass serialization and plain stdout, never the Rich console. Progress is
sent to stderr only for human output; JSON and dry-run modes start no live display.
Policy exclusions are deliberate successful omissions recorded in `skipped`;
unexpected read failures abort the whole backup.

This implementation targets Linux with readable mountinfo and support for atomic
no-replace renames. It is not a snapshot mechanism: quiescent sources and trusted
destination directories are required for strong consistency. Metadata checks
cannot eliminate the interval after validation or malicious metadata concealment.
No encryption, ACL/xattr preservation, sparse-file preservation, restoration, or
scheduling is provided. Original data is not written, but normal access-time
updates may occur. SIGKILL or power loss can leave temporary files, and syncing
the archive does not promise full power-loss durability of directory entries.

### Future process integrations

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

Backup-specific protections are implemented as described above. Future
system-changing commands must establish their own relevant safeguards.

## Testing and quality

Use pytest for behavior-focused unit tests of core logic. Test commands through
Typer's testing interface. Isolate external resources using
fakes, fixtures, and temporary directories. Integration tests must use controlled
resources and must not alter the host system by default.

Backup tests use pytest `tmp_path` exclusively. They open produced archives and
compare contents without extracting into personal folders. Failure injection
covers read permissions, compression, disk-full conditions, rename support,
concurrent name collisions, source mutation, destination replacement, and temp
cleanup. A subprocess test validates the installed CLI with piped JSON stdout.

Ruff is configured in `pyproject.toml` for linting and formatting with a Python
3.12 target. Future CI will run lint, format checks, and meaningful tests on Linux
with a documented Python version matrix. Empty directories are tracked with
`.gitkeep`; executable CI workflows are intentionally deferred.

## Staged evolution

The CLI foundation, read-only status snapshot, and safe backup creation with
temporary-fixture tests are in place. Next add CI checks and define further
command contracts; restoration and scheduling require separate design stages.
Introduce automation only after adapter boundaries and operational
safeguards have been tested. Dependency locking, release automation, configuration
storage, and additional subsystems remain future design decisions.
