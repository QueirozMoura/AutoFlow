# AutoFlow CLI

AutoFlow CLI is an open source project for Linux automation and system
administration, developed primarily in Python. It aims to provide a modular,
maintainable command-line interface with readable terminal output and explicit
control over operations that affect the system.

**Status: CLI foundation.** Typer provides help and version options with Rich
terminal presentation. No system operations, metrics collection, or Bash
integrations are implemented.

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
│       ├── commands/     # Future Typer command groups
│       ├── core/         # Future application logic
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
```

Running `autoflow` without arguments also displays help. The version comes from
the installed package metadata, using `pyproject.toml` as the source of truth.

The package uses the `src/` layout; install it in the development environment
before running package tests. Quality checks are:

```bash
python -m ruff check .
python -m ruff format --check .
python -m pytest
```

Tests cover help, version, invocation without arguments, and invalid input.
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
- [ ] Introduce read-only system inspection and metrics.
- [ ] Add carefully scoped automation and Bash integrations.
- [ ] Document supported distributions, operational safeguards, and release procedures.

## Contributing

Development will proceed in small, reviewable stages. Discuss command behavior
and security implications before implementation, keep application logic separate
from terminal presentation, and add meaningful tests with each feature.

## License

AutoFlow CLI is licensed under the [MIT License](LICENSE).
