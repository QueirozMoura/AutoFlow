# AutoFlow CLI

AutoFlow CLI is an open source project for Linux automation and system
administration, developed primarily in Python. It aims to provide a modular,
maintainable command-line interface with readable terminal output and explicit
control over operations that affect the system.

**Status: system status, backup creation, and safe restoration.** Typer provides
help, version, read-only `status`, `.tar.gz` backup creation, and validated
restoration into new directories. Commands support Rich output and plain JSON.
Scheduling and Bash integrations are not implemented.

## Technology stack

| Technology | Planned role |
| --- | --- |
| Python 3.12+ | Application language and packaging |
| Typer | CLI commands, arguments, and help |
| Rich | Terminal presentation and structured output |
| psutil | System metrics and process information |
| pytest | Automated testing |
| Ruff | Linting and formatting |
| Bash | Targeted Linux integrations when appropriate |
| GitHub Actions | Continuous integration |

Linux is the target platform. Compatibility across distributions and Python
versions will be verified as implementations and CI are added.

## Project structure

```text
.
├── .github/workflows/     # Reserved for future CI workflows
├── docs/
│   └── architecture.md   # Module boundaries and design principles
├── scripts/              # Reserved for development and Linux helper scripts
├── src/
│   └── autoflow/
│       ├── __init__.py
│       ├── cli.py        # Typer application and console entry point
│       ├── commands/     # Status/backup/restore commands and terminal presentation
│       ├── core/         # System metrics, thresholds, backup and restore operations
│       └── integrations/ # Future Linux and process adapters
├── tests/                # CLI interface tests
├── .gitignore
├── LICENSE
├── README.md
└── pyproject.toml
```

Empty working directories contain `.gitkeep` files so Git can track them.

## Planned architecture

The CLI will parse input and render output, command groups will coordinate use
cases, and the core will own application rules. Integration adapters will isolate
Linux APIs, metrics collection, and external processes. Importing a module must
not execute system operations.

See [the architecture document](docs/architecture.md) for dependency boundaries,
testing strategy, and security principles.

## Development setup

From the repository root on a system with Python 3.12+ and pip:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

With the environment activated, try the CLI:

```bash
autoflow --help
autoflow --version
autoflow status
autoflow status --json
```

Running `autoflow` without arguments also displays help. The version comes from
the installed package metadata, using `pyproject.toml` as the source of truth.

## System status

`autoflow status` displays the Linux distribution name and version, hostname,
uptime, CPU usage, RAM usage, and usage of the filesystem mounted at `/`.
CPU is sampled for 100 ms. RAM used is `total - available`, consistent with
psutil's memory percentage; reclaimable cache counts as available. Disk usage
and percentage are reported by psutil, including its treatment of reserved space.

The dashboard uses green for usage below 70%, yellow for 70% to below 90%, and
red for 90% or above. Classification uses the unrounded measurement. Labels
remain visible when colors are disabled.

`autoflow status --json` emits a single JSON object, including when redirected
or piped, with no ANSI formatting. Fields are `operating_system` (`name`,
`version`), `hostname`, `uptime_seconds`, `cpu_percent`, `memory` and `disk`
(`percent`, `used_bytes`, `total_bytes`), `disk_path`, and `errors`.

Successful snapshots exit with code 0. If a read fails, other metrics remain
available, the failed field becomes `null` in JSON or `Unavailable` in the
dashboard, and `errors` maps field names to read errors. The command exits with
code 1 while still emitting the snapshot. No elevated privileges are required.

## Backups

```bash
autoflow backup ~/Documents --output ~/Backups
autoflow backup ~/Documents --output ~/Backups --dry-run
autoflow backup ~/Documents --output ~/Backups --json
autoflow backup ~/Documents/report.txt --output ~/Backups
autoflow backup ~/Documents --output ~/Backups --dry-run --json
```

The destination is a directory, created if necessary, and must be outside the
source tree. Each archive has a UTC timestamp and random UUID, for example
`autoflow-20261106T153000.123456Z-<uuid>.tar.gz`. Existing backups are never
overwritten, including during concurrent runs. Directory archives contain a
top-level directory named after the source, retaining relative paths and empty
directories. A single-file backup contains that file's basename.

The human interface displays entry-based Rich progress, file count, original
logical size, compressed archive size, and elapsed seconds. `--dry-run` lists
files, directories, exclusions, and total uncompressed file bytes without
creating any files or directories. It checks discovery and file-open permissions
but does not read every file's contents or predict the compressed size.

`--json` emits exactly one JSON object without progress, ANSI codes, or other
messages, including with redirected stdout. Successful results contain:

| Field | Meaning |
| --- | --- |
| `success`, `dry_run` | Completion and preview flags |
| `source`, `output`, `archive` | Absolute paths; archive is null for previews |
| `file_count`, `original_bytes` | Regular file count and uncompressed logical size |
| `compressed_bytes`, `duration_seconds` | Archive size (null for previews) and elapsed time |
| `files`, `directories` | Relative archive entries |
| `skipped` | Explicit safety exclusions, each with `path` and `reason` |

Completion exits with code 0. An operational failure exits with code 1; JSON
contains `success: false`, `dry_run`, and `error`. Invalid CLI syntax uses Typer's
standard usage error and exit code 2. Policy exclusions are reported, not treated
as read failures; inspect `skipped` when complete coverage matters.

### Backup safety policy and limits

- Symbolic links discovered inside the source are excluded, whether internal,
  external, dangling, or cyclic. Links supplied as the source or in any source
  or destination path component are rejected. No links are followed during
  discovery or copying.
- Nested mount points, including same-device bind mounts, and entries on a
  different device are excluded. The source's own filesystem is allowed.
  Linux `/proc/self/mountinfo` must be readable; otherwise the backup fails.
- FIFOs, sockets, and device files are excluded without reading them. `/` and
  sources within `/proc`, `/sys`, `/dev`, and `/run` are rejected. Destinations
  within these special trees are rejected too.
- Read errors, permission errors, disappearing entries, and detected changes
  cancel the operation. Metadata is checked before/after copying and the source
  is scanned again before publication. No partially completed archive is
  reported as successful.
- New destination directories request mode `0700`; temporary and final archives
  request mode `0600` (a restrictive umask may reduce permissions further).
  Existing directory permissions are preserved, but the destination must be
  owned by the current user and must not be group/world-writable. This prevents
  other users from replacing temporary files before publication or cleanup.
- A uniquely and exclusively created `.tar.gz.part` file is closed and synced
  before atomic publication using Linux `renameat2(RENAME_NOREPLACE)`. An
  unsupported libc/kernel/filesystem causes an error; there is no overwriting
  fallback. Only this invocation's temporary file is removed on failure or
  ordinary cancellation. Existing backups are retained. Newly created empty
  destination directories may remain after failure.
  Temporary-file identity is verified before publication and cleanup; a replaced
  name is neither published nor deleted, and the conflict is reported.
- This is a file-level backup, not an atomic filesystem snapshot. Stop writers
  for consistency: changes after the final validation, or changes concealed by
  filesystem metadata behavior, cannot be ruled out. Do not rearrange mount
  points or source/destination directories while backing up.
- File contents are streamed, but the entry inventory is held in memory. Deep
  trees are subject to Python recursion and OS file-descriptor limits. Progress
  advances after each entry, so large files may take time between updates. Regular
  hard links are stored as independent file contents. Basic modes, timestamps,
  and numeric owners are recorded; setuid/setgid/sticky bits, ACLs, extended
  attributes, sparse-file layout, and hard-link relationships are not retained.
- Original content and permissions are never written. The filesystem may update
  access timestamps as files and directories are read. Backups are not encrypted.
  Forced termination or power loss can leave a `.part` file; full crash durability
  is not guaranteed. Cleanup failures are reported explicitly.

## Safe restoration

```bash
autoflow restore backup.tar.gz --output ~/Restaurados
autoflow restore backup.tar.gz --output ~/Restaurados --dry-run
autoflow restore backup.tar.gz --output ~/Restaurados --json
autoflow restore backup.tar.gz --output ~/Restaurados --dry-run --json
autoflow restore backup.tar.gz --output ~/Restaurados --max-files 20000 --max-bytes 21474836480
autoflow restore --help
```

The destination **must not exist**, even as an empty directory, and its parent
must already exist. No merging, replacement, or overwrite option is provided.
A directory backup restores its original top-level basename under the output;
single-file backups restore the basename directly under the output. Dry-run
validates every entry and reads all file data, including the gzip checksum,
without creating files, directories, or temporary storage. Reads can update atime.

The default limits are **10,000 entries** and **10 GiB of uncompressed file data**.
`--max-files` counts files and directories, including implicit parent directories;
`--max-bytes` counts logical regular-file bytes and accepts zero for empty files.
Limits apply to previews as well as actual restoration. Metadata records are
limited to 1 MiB each, names to 4096 encoded bytes and 128 path components, and
trailing zero padding to 1 MiB. Total decompressed framing (headers and padding)
is limited to `max_bytes + max_files * 8192 + 1048576` bytes. These additional
fixed safeguards can reject unusual but otherwise valid tar archives.

Safety decisions:

- Reject absolute paths, empty/dot/parent components, backslashes, control
  characters, duplicate names, and file/directory conflicts. PAX path overrides
  undergo the same validation. Reject symbolic links, hard links, sparse entries,
  devices, FIFOs, and all other special member types.
- Check tar headers, declared sizes, complete payloads, two zero tar end records,
  trailing data, gzip CRC, and gzip EOF before any filesystem writes. CRC detects
  accidental corruption; it does not authenticate the archive's author.
- Validate again while writing into a private sibling `.autoflow-restore-*.part`
  directory. All writes use validated path components and no-follow directory
  descriptors, never `extract()` or `extractall()`. Only after validation and
  file syncing succeeds is the whole prepared tree atomically renamed into place
  using Linux `renameat2(RENAME_NOREPLACE)`. A concurrent destination wins and is
  preserved; restoration fails without publishing its partial tree.
- Require a destination parent owned by the current user without group/other
  write permission. Ancestors must belong to the system root owner or current
  user and cannot be shared writable directories, except system-owned sticky
  directories such as `/tmp`. Symlink components and protected system destinations
  (`/`, `/proc`, `/sys`, `/dev`, `/run` and their special subtrees) are rejected.
- Directories request `0700`, files `0600`, subject to stricter umask. Archive
  owners, permissions (including execute/setuid), and timestamps are not applied.
  Input archives and existing files are never written or removed.
- Permission, read/write, disk-full, checksum, limit, source-change, or publication
  failures abort and remove the owned staging tree. Ordinary cancellation also
  cleans up. Cleanup failures are explicit errors and can leave staging data.

Rich shows validation/preparation progress and a summary with file count, bytes,
and duration. JSON emits a single object without ANSI/progress messages, with
`success`, `dry_run`, `archive`, `output`, `file_count`, `total_bytes`,
`duration_seconds`, `files`, `directories`, `max_files`, and `max_bytes`.
Operational failures emit `success: false`, `dry_run`, and `error`, exiting 1.
Success exits 0. Invalid CLI syntax uses Typer's normal usage errors and exits 2.

Limitations: Linux no-replace rename support is required. Staging needs space
for the entire restored tree on the destination filesystem. Validation reads the
archive twice and retains a bounded inventory in memory; file contents stream
in 64 KiB chunks. Limits do not provide a CPU/time quota. Stop archive writers
and avoid changing mounts or ancestor permissions while restoring. Metadata
checks detect ordinary concurrent changes but do not provide a snapshot or
isolation against root or hostile processes with the same user credentials.
SIGKILL or power loss may leave staging data; atomic publication does not promise
full power-loss durability. No scheduling is included.

## Quality checks

The package uses the `src/` layout; install it in the development environment
before running package tests. Quality checks are:

```bash
python -m ruff check .
python -m ruff format --check .
python -m pytest
```

Tests cover the base CLI, mocked metric reads and failures, utilization
boundaries, dashboard rendering, and plain JSON output. Backup tests use only
temporary fixtures and cover archive content, source integrity, exclusions,
concurrent publication, change detection, permission errors, and failure cleanup.
Restore tests additionally cover hostile paths/types/PAX metadata, gzip and tar
corruption, configurable limits, atomic publication races, restrictive permissions,
cleanup failures, dry-run, and CLI integration using `tmp_path` only.
CI workflows remain a future stage. Runtime dependencies and development tools are declared in
`pyproject.toml`; reproducible dependency locking is deferred to the CI stage.

## Security principles

- Keep secrets, credentials, and local environment files out of version control.
- Validate external input and avoid interpolating it into shell commands.
- Use least privilege and make system-changing operations explicit.
- Plan previews and confirmation for destructive operations before adding them.
- Test system integrations through controlled fixtures and temporary resources.

The ignore rules are a convenience, not a substitute for reviewing staged files.

## Roadmap

- [x] Prepare packaging, module boundaries, tooling configuration, and documentation.
- [x] Establish the Typer application with help and version options.
- [x] Add basic CLI interface tests.
- [ ] Define initial system command contracts and add GitHub Actions quality checks.
- [x] Introduce read-only system inspection and metrics with `status` and JSON output.
- [x] Create safe file and directory backups, previews, and structured JSON results.
- [x] Restore validated backups atomically into new directories with bounded resource usage.
- [ ] Design scheduling in a separate stage.
- [ ] Add carefully scoped automation and Bash integrations.
- [ ] Document supported distributions, operational safeguards, and release procedures.

## Contributing

Development will proceed in small, reviewable stages. Discuss command behavior
and security implications before implementation, keep application logic separate
from terminal presentation, and add meaningful tests with each feature.

## License

AutoFlow CLI is licensed under the [MIT License](LICENSE).
