# Delete Codex Session

[![Test](https://github.com/Xv-Bowen/delete-codex-session/actions/workflows/test.yml/badge.svg)](https://github.com/Xv-Bowen/delete-codex-session/actions/workflows/test.yml)

A safety-focused Codex skill for reporting and permanently deleting one or more Codex Desktop or CLI sessions. It covers recursive subagents, related files, database rows, UI state, and optional historical residuals while preserving ambiguous or protected objects.

## Safety warning

Deletion is permanent and is performed without an automatic backup. Review the generated report, preserved warnings, exact scope, and approval request before authorizing deletion. Do not publish real session IDs, local Codex databases, staged deletion jobs, approval material, receipts, or diagnostic reports.

## Requirements

- Python 3.10 or newer
- Codex Desktop or CLI
- macOS and Ghostty 1.3.0 or newer when an offline Desktop handoff is required

## Installation

Place this repository at:

```text
$CODEX_HOME/skills/delete-codex-session
```

If `CODEX_HOME` is not set, Codex commonly uses `~/.codex`.

## Validation

Run the regression suite from the repository root:

```bash
python3 tests/smoke_test.py
```

When the Codex system skill creator is available, also run its `quick_validate.py` script against this repository.

## Repository contents

- `SKILL.md`: workflow and safety instructions
- `scripts/`: reporting, deletion, and offline-handoff implementation
- `references/`: safety, compatibility, handoff, and result contracts
- `tests/`: synthetic regression coverage
- `agents/openai.yaml`: Codex skill interface metadata

## Platform scope

The reporting logic inspects local Codex state. The offline mutation workflow is designed for macOS and uses Ghostty's native AppleScript interface. It does not fall back to another terminal when that handoff is unavailable.

## License

This project is licensed under the MIT License. See `LICENSE` for details.
