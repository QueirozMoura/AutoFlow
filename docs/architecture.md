# AutoFlow CLI architecture

## Scope and current state

AutoFlow CLI targets Linux automation and system administration using Python
3.12+. This document describes the intended architecture. At this stage, the
repository contains packaging configuration, documentation, reserved module
boundaries, and a Typer application with help, version, and read-only `status`.
Rich provides terminal presentation; JSON supports machine consumers. `backup`
creates archives in an explicit destination. `restore` validates untrusted
archives and publishes a new directory atomically. `logs` analyzes local files
and accessible systemd journal records without modifying them. Scheduling is outside scope.

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
| `src/autoflow/commands/restore.py` | Parse restore limits, render progress/previews, and serialize results/errors |
| `src/autoflow/core/restore.py` | Bound and validate untrusted archives, stage contents, and publish a new directory |
| `src/autoflow/commands/logs.py` | Select log source, configure limits, and render Rich/JSON summaries |
| `src/autoflow/core/logs.py` | Read regular files, interpret levels, sanitize templates, and aggregate bounded streams |
| `src/autoflow/integrations/journal.py` | Validate journal queries and manage bounded journalctl subprocess I/O |
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
   its ownership and write permissions. Check that the temporary name still
   identifies the completed stream's inode and metadata. Publish via
   Linux `renameat2(RENAME_NOREPLACE)` exposed through standard-library `ctypes`.
   Timestamp/UUID names are retried on collisions; neither pre-checks followed by
   ordinary rename nor overwriting fallbacks are used. Unsupported atomic rename
   semantics fail closed.
8. On failure/cancellation, unlink only the temporary name exclusively created
   by this invocation, through its pinned destination descriptor. Existing
   archives are untouched. Verify temporary inode ownership before unlinking;
   report replaced names without removing them. Cleanup errors are explicit; newly created destination
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
Backup creation provides no encryption, ACL/xattr preservation, sparse-file
preservation, or scheduling. Original data is not written, but normal access-time
updates may occur. SIGKILL or power loss can leave temporary files, and syncing
the archive does not promise full power-loss durability of directory entries.

### Restore execution and trust boundaries

`restore_backup` returns a typed `RestoreResult` and raises `RestoreError` on
expected failures. The command owns Typer, Rich, and JSON presentation; the core
has no terminal dependencies and runs no processes. Existing Linux descriptor,
metadata-signature, and no-replace rename helpers are reused from `core/backup.py`.

1. Normalize paths, reject protected destinations, and open archive/destination
   parents component by component without following symlinks. Require an absent
   output and an existing, user-owned parent with no group/other write bits.
   Walk the destination ancestry to reject untrusted ownership and shared writable
   ancestors, allowing system-owned sticky directories such as `/tmp`. The root
   directory's owner identifies the system owner even in mapped user namespaces.
2. Open a regular archive with `O_NOFOLLOW` and retain its descriptor and metadata
   signature. Perform a complete read-only validation pass before creating staging.
   `_SafeTarInfo` checks extension sizes before inherited tarfile metadata parsing;
   `_BoundedGzip` bounds individual reads and all decompressed offsets, including
   seeks. The subclass uses tarfile's header/extension processing hook and requires
   compatibility checks when upgrading Python.
3. Validate effective member names (including PAX overrides), types, declared sizes,
   duplicates, and file/directory conflicts. Reject absolute paths, dot/parent/empty
   components, backslashes, control characters, links, sparse and special entries.
   Build an inventory counting implicit directories against `max_files` and file
   bytes against `max_bytes`. Defaults: 10,000 entries, 10 GiB. Independently bound
   metadata records (1 MiB), path bytes (4096), depth (128), trailing padding (1 MiB),
   and total inflated framing (`max_bytes + max_files * 8192 + 1048576`). Read every
   regular payload and require two zero tar end records, only bounded zero padding,
   valid gzip CRC, and EOF. Dry-run returns now without filesystem writes.
4. Create a private sibling staging directory exclusively, tracking ownership only
   after `mkdir` succeeds. Validate and stream the archive a second time. Create
   directories with `0700` and files with `0600` using anchored directory descriptors,
   `O_NOFOLLOW`, and `O_EXCL`. Do not call tarfile extraction APIs; `extractfile`
   only supplies streams for previously validated regular entries. Do not apply
   archive permissions, owners, or timestamps. Sync each completed file.
5. Compare inventories and archive metadata, reopen the destination parent and
   recheck its identity, ownership, and ancestry. Publish the complete staging tree
   using `renameat2(RENAME_NOREPLACE)`. The output never exposes an in-progress
   restore, and concurrent outputs are never overwritten. Unsupported rename
   semantics fail closed. No merging into an existing directory is supported.
6. On failure or ordinary cancellation, clean only the staging name this invocation
   created, using descriptor-relative, symlink-resistant `shutil.rmtree`. Report
   cleanup errors explicitly. If publication succeeds, stop treating the directory
   as temporary. Return success only after the atomic rename.

JSON success contains `success`, `dry_run`, absolute `archive`/`output`, file
count, total logical bytes, duration, entry lists, and configured limits. JSON
operational errors contain `success: false`, `dry_run`, and `error`; success/error
exit codes are 0/1. Typer usage errors retain code 2 and its standard formatting.
Human progress goes to stderr and is disabled for JSON/previews.

The archive is not authenticated by CRC and resource limits impose no CPU quota.
The archive is read twice; staging needs the full expanded disk space. Memory
holds bounded metadata/inventories, while payloads stream in 64 KiB blocks.
Input contents are never modified, though reads may update atime. This is not a
snapshot or a defense against root/same-credential attackers changing files,
mounts, or permissions during execution. SIGKILL/power loss can leave staging;
directory publication is atomic but full crash durability is not guaranteed.

### Log analysis execution and trust boundaries

`commands/logs.py` selects exactly one source and passes the same bounded
analysis options to the core. `core/logs.py` separates file opening (`analyze_file`),
stream consumption/aggregation (`analyze_stream`), severity interpretation
(`severity`), and privacy/group template normalization (`normalize_message`).
`LogEntry`, `MessageGroup`, and `LogsResult` are terminal-independent dataclasses.
The journal adapter depends on these core contracts; the core never imports the
subprocess adapter, Typer, or Rich.

File input uses existing Linux no-follow descriptor traversal from `core/backup.py`.
Only regular files are accepted, opened read-only. Iterative binary `readline`
calls consume at most the configured byte prefix, with bounded line buffers and
UTF-8 replacement decoding. Ordinary buffered file I/O can prefetch a small
amount beyond consumed bytes. Defaults are 10 MiB, 100,000 lines, 64 KiB per line,
1,000 selected groups, and top 10. A per-line overflow uses at most one lookahead
byte within the total budget, stops analysis, and never groups the partial line.
A byte-cut line is omitted. Caps conservatively mark a limited result, including
when exactly aligned with EOF. No whole-file reads or line lists are retained.

Structured journal priority wins over syslog PRI, followed by the first recognized
textual severity token. Missing levels become UNKNOWN. Critical/fatal and priorities
0–3 map to ERROR; priority 4 to WARNING, 5–6 to INFO, 7 to DEBUG. Text variants are
documented in README. Counts always describe all analyzed lines; `--errors` affects
only selected groups. Templates normalize common timestamps, syslog hostnames,
numeric values, IDs, network addresses, paths, and credential fields; terminal
escapes/control/format characters are stripped. Only sanitized templates, capped
at 512 characters, enter group storage. Redaction is best effort, not a promise of
complete anonymization. No raw samples, journal diagnostic text, or remote
transmission are part of the report.

Groups use `(severity, template)` keys and deterministic count/severity/template
sorting. At the group cap, tracked counts continue updating and new templates
increment `ungrouped_lines`; results explicitly mark `max_groups` and cannot claim
an exact global top ranking. `--top` only slices the final bounded ranking.

`integrations/journal.py` validates positive relative s/m/h/d/w intervals, translating
`1h` to `--since=-1h`. It invokes `Popen` with an argument list, `shell=False`,
DEVNULL stdin, no pager or password prompts, and JSON output restricted to MESSAGE
and PRIORITY (mandatory journal metadata may still be emitted but is discarded).
MESSAGE may be a UTF-8 string or byte array; malformed records fail without echoing
their content. The adapter never requests sudo or retries with higher privileges.

`JournalReader` exposes bounded `readline` to the core. Nonblocking pipes and a
selector consume stdout and stderr concurrently under a monotonic deadline
(default 10 seconds). Stderr is counted, not retained, and capped at 64 KiB.
Nonzero exits and startup/permission/timeout failures abort analysis. Successful
stderr produces a generic warning about potentially restricted visibility.
When core read limits stop before EOF, the child is killed and reaped; the report
states that the final exit status is unavailable. Context-manager cleanup also
runs on analysis errors or interruption. No real journal is read in tests.

JSON emits one unformatted object with counts, selected ranked groups, configured
limits, consumed bytes, duration, warnings, and explicit limitation reasons.
Operational errors emit `success: false`/`error` and exit 1. Successful complete or
explicitly limited analyses exit 0; Typer syntax errors retain code 2. Rich renders
counts, sanitized templates, and visible partial-analysis warnings, never raw logs.

This is a prefix analysis, not tail/follow or a snapshot. Rotation, truncation,
and appends can change what is observed. Multiline records are not reconstructed;
severity and normalization remain heuristics. Read/group caps bound memory/work
but provide no CPU quota or local-filesystem I/O deadline. Journal visibility
depends on the invoking user's existing permissions.

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
Restore unit and CLI tests add hostile member paths/types, metadata-size attacks,
PAX overrides, archive/gzip corruption, limits, concurrent publication, original
integrity, permission policy, and failure/cancellation cleanup. End-to-end tests
create backups and restore them using only `tmp_path` resources.
Log tests create synthetic temporary files and in-memory streams. Journal tests
replace journalctl launches with controlled synthetic child processes, exercising
real pipe draining, deadlines, early cancellation, error exits, and cleanup while
never reading the host journal. CLI integration checks piped JSON and verifies the
synthetic source content, mode, and modification time remain intact.

Ruff is configured in `pyproject.toml` for linting and formatting with a Python
3.12 target. Future CI will run lint, format checks, and meaningful tests on Linux
with a documented Python version matrix. Empty directories are tracked with
`.gitkeep`; executable CI workflows are intentionally deferred.

## Staged evolution

The CLI foundation, read-only status snapshot, safe backup creation/restoration and log analysis with
temporary-fixture tests are in place. Next add CI checks and define further
command contracts; scheduling requires a separate design stage.
Introduce automation only after adapter boundaries and operational
safeguards have been tested. Dependency locking, release automation, configuration
storage, and additional subsystems remain future design decisions.
