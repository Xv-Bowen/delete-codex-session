#!/usr/bin/env python3
"""Delete Codex session state by thread/session ID.

Default mode is report-only. Use --apply to mutate local Codex state.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import errno
import fcntl
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable


CANONICAL_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-" r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-" r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
UUID_BYTES_RE = re.compile(UUID_RE.pattern.encode("ascii"))
SCRIPT_VERSION = "4.0"
PLAN_CONTRACT_VERSION = 14
APPROVAL_CONTRACT_VERSION = 4
APPROVAL_SCOPE_CONTRACT_VERSION = 2
MAX_APPROVAL_PAYLOAD_BYTES = 1024 * 1024
MAX_APPROVAL_TOKEN_CHARS = 1024 * 1024
MAX_RUNTIME_SCHEMA_SCAN_BYTES = 64 * 1024 * 1024
RECENT_LOG_ONLY_PROTECTION_SECONDS = 60 * 60
SQLITE_MAX_INTEGER = 2**63 - 1
CLOSED_EDGE_STATUSES = {
    "cancelled",
    "canceled",
    "closed",
    "complete",
    "completed",
    "failed",
    "shutdown",
}
KNOWN_STATE_THREAD_REFERENCES = {
    ("agent_job_items", "assigned_thread_id"),
    ("stage1_outputs", "thread_id"),
    ("thread_dynamic_tools", "thread_id"),
    ("thread_goals", "thread_id"),
    ("thread_spawn_edges", "child_thread_id"),
    ("thread_spawn_edges", "parent_thread_id"),
}
KNOWN_STATE_NON_OWNING_THREAD_REFERENCES = {
    ("rollout_migration_state", "last_checked_thread_id"),
}
ROLLOUT_MIGRATION_STATE_COLUMNS = (
    "migration_id",
    "last_checked_thread_created_at",
    "last_checked_thread_id",
    "updated_at",
)
ROLLOUT_MIGRATION_SKIPPED_COLUMNS = (
    "migration_id",
    "rollout_path",
    "rollout_size_bytes",
    "rollout_modified_at_ns",
    "skip_reason",
    "skipped_at",
)
KNOWN_STATE_TABLE_COLUMNS = {
    "agent_job_items": {"assigned_thread_id"},
    "rollout_migration_skipped_rollouts": {
        "migration_id",
        "rollout_path",
        "rollout_size_bytes",
        "rollout_modified_at_ns",
        "skip_reason",
        "skipped_at",
    },
    "rollout_migration_state": {
        "migration_id",
        "last_checked_thread_created_at",
        "last_checked_thread_id",
        "updated_at",
    },
    "stage1_outputs": {"thread_id"},
    "thread_dynamic_tools": {"thread_id"},
    "thread_goals": {"thread_id"},
    "thread_sections": {"id", "name"},
    "thread_spawn_edges": {"child_thread_id", "parent_thread_id", "status"},
    "threads": {"id"},
}
EXACT_STATE_TABLE_COLUMNS = {
    "rollout_migration_state": set(ROLLOUT_MIGRATION_STATE_COLUMNS),
    "rollout_migration_skipped_rollouts": set(ROLLOUT_MIGRATION_SKIPPED_COLUMNS),
}
EXPECTED_STATE_PRIMARY_KEYS = {
    "rollout_migration_state": ["migration_id"],
    "rollout_migration_skipped_rollouts": ["migration_id", "rollout_path"],
}
KNOWN_STATE_TABLES = {
    "_sqlx_migrations",
    "agent_job_items",
    "agent_jobs",
    "backfill_state",
    "external_agent_config_imports",
    "remote_control_enrollments",
    "rollout_migration_skipped_rollouts",
    "rollout_migration_state",
    "stage1_outputs",
    "thread_dynamic_tools",
    "thread_goals",
    "thread_sections",
    "thread_spawn_edges",
    "threads",
}
KNOWN_LOG_TABLES = {"_sqlx_migrations", "logs"}
DESKTOP_CATALOG_LEGACY_FILENAMES = ("codex.db", "codex-dev.db")
DESKTOP_CATALOG_KNOWN_TABLES = {
    "automation_runs",
    "automations",
    "inbox_items",
    "local_app_server_feature_enablement",
    "local_thread_catalog",
    "local_thread_catalog_hosts",
    "local_thread_catalog_metadata",
    "local_thread_catalog_sync_state",
    "thread_timeline_ledger",
}
DESKTOP_CATALOG_THREAD_REFERENCES = {
    ("automation_runs", "thread_id"),
    ("inbox_items", "thread_id"),
    ("local_thread_catalog", "thread_id"),
    ("thread_timeline_ledger", "thread_id"),
}
DESKTOP_CATALOG_CANONICAL_UUID_REFERENCES = {
    ("local_thread_catalog", "thread_id"),
    ("thread_timeline_ledger", "thread_id"),
}
DESKTOP_CATALOG_MAX_USER_VERSION = 31
DESKTOP_CATALOG_DELETABLE_TABLES = {
    "automation_runs": ("thread_id",),
    "local_thread_catalog": ("host_id", "thread_id"),
    "thread_timeline_ledger": ("host_id", "thread_id", "sequence"),
}
DESKTOP_CATALOG_REQUIRED_COLUMNS = {
    "automation_runs": {
        "thread_id",
        "automation_id",
        "status",
        "read_at",
        "thread_title",
        "source_cwd",
        "inbox_title",
        "inbox_summary",
        "created_at",
        "updated_at",
        "archived_user_message",
        "archived_assistant_message",
        "archived_reason",
    },
    "local_thread_catalog": {
        "host_id",
        "thread_id",
        "display_title",
        "source_created_at",
        "source_updated_at",
        "cwd",
        "source_kind",
        "source_detail",
        "model_provider",
        "git_branch",
        "observation_sequence",
        "missing_candidate",
    },
    "local_thread_catalog_metadata": {"id", "catalog_revision"},
    "local_thread_catalog_hosts": {"host_id", "host_kind"},
    "local_thread_catalog_sync_state": {
        "host_id",
        "watermark_updated_at",
        "initial_build_complete",
        "observation_sequence",
    },
    "thread_timeline_ledger": {
        "host_id",
        "thread_id",
        "sequence",
        "record_id",
        "payload_json",
    },
}
DESKTOP_CATALOG_PRIMARY_KEYS = {
    "automation_runs": ["thread_id"],
    "local_thread_catalog": ["host_id", "thread_id"],
    "local_thread_catalog_metadata": ["id"],
    "local_thread_catalog_hosts": ["host_id"],
    "local_thread_catalog_sync_state": ["host_id"],
    "thread_timeline_ledger": ["host_id", "thread_id", "sequence"],
}
THREAD_AUXILIARY_TABLE_ROLES = {
    "app_server_history_snapshots": (
        "app_server_history_snapshots",
        "thread_id",
        3,
    ),
    "thread_turn_summaries": ("thread_turn_summaries", "thread_id", 2),
}
THREAD_AUXILIARY_PRIMARY_KEYS = {
    "app_server_history_snapshots": ["principal_key", "host_id", "thread_id"],
    "thread_turn_summaries": ["principal_key", "host_key", "thread_id"],
}
GLOBAL_STATE_FILENAMES = (
    ".codex-global-state.json",
    ".codex-global-state.json.bak",
)
GLOBAL_STATE_MAP_CONTAINERS = (
    ("electron-persisted-atom-state", "heartbeat-thread-permissions-by-id"),
    ("electron-persisted-atom-state", "prompt-history"),
    ("electron-persisted-atom-state", "thread-descriptions-v1"),
    ("electron-remote-hosted-pip-task-visibility-state",),
    ("queued-follow-ups",),
    ("thread-project-assignments",),
    ("thread-projectless-output-directories",),
    ("thread-workspace-root-hints",),
    ("thread-writable-roots",),
)
GLOBAL_STATE_ARRAY_CONTAINERS = (
    ("pinned-thread-ids",),
    ("projectless-thread-ids",),
    ("remote-hosted-pip-hidden-thread-ids",),
    ("electron-persisted-atom-state", "remote-hosted-pip-hidden-thread-ids"),
)
GLOBAL_STATE_SCALAR_CONTAINERS = (
    ("realtime-voice-most-recent-thread",),
    ("electron-persisted-atom-state", "realtime-voice-most-recent-thread"),
)
GLOBAL_STATE_VOICE_SELECTOR_KEYS = {"conversationId", "hostId", "version"}
GLOBAL_STATE_DYNAMIC_KEY_PREFIXES = (
    "codex-writing-block-deleted-thread-v1:",
    "thread-browser-tabs-v1:",
    "thread-reference-capability:",
    "thread-tab-routes-v1:",
)
GLOBAL_STATE_DYNAMIC_KEY_ENCODED_PREFIXES = (
    "thread-client-id-v1:local%3A",
    "thread-client-id-v1:local%3a",
)
KNOWN_STATE_TRIGGER_HASHES = {
    "threads_created_at_ms_after_insert": "586126949b44728f1aceff4a390164913f5906a01111a2c8dad855287342b511",
    "threads_created_at_ms_after_update": "5a54826b36daa1b4451714ce187b238116390a16a53e513cc30074c1b4fc3611",
    "threads_recency_at_after_insert": "44e9dd026465a1ebdcfcb53f3a87f35cf860af73d37f5dde2c0a29b55dab9650",
    "threads_updated_at_ms_after_insert": "6f880f1cc9191fe60877a7cbb2b1538ea4b2d7b9dfacd2992f03f4795b8ba800",
    "threads_updated_at_ms_after_update": "9de2de92b3540251ab93ca8bf7041dff312084a5e1174bf0dc4eb41bb3faaf65",
}
STATE_REFERENCE_LOCATIONS = [
    ("threads", "id"),
    ("thread_spawn_edges", "parent_thread_id"),
    ("thread_spawn_edges", "child_thread_id"),
    ("thread_dynamic_tools", "thread_id"),
    ("thread_goals", "thread_id"),
    ("stage1_outputs", "thread_id"),
    ("agent_job_items", "assigned_thread_id"),
]
STATE_REFERENCE_FORMAT_LOCATIONS = STATE_REFERENCE_LOCATIONS + sorted(
    KNOWN_STATE_NON_OWNING_THREAD_REFERENCES
)
SESSION_REFERENCE_COLUMNS = {
    "conversation_id",
    "conversation_uuid",
    "session_id",
    "session_uuid",
    "thread_id",
    "thread_uuid",
}
EXIT_USAGE_OR_BLOCKED = 2
EXIT_PLAN_CHANGED = 3
EXIT_VERIFICATION_FAILED = 4

COMPONENT_HISTORICAL = "historical"
COMPONENT_CORE = "state_and_index"
COMPONENT_LOGS = "logs"
COMPONENT_ROLLOUTS = "rollout_artifacts"
COMPONENT_SNAPSHOTS = "shell_snapshots"
COMPONENT_GENERATED = "generated_artifacts"
COMPONENT_CATALOG = "desktop_catalog"
COMPONENT_AUXILIARY = "auxiliary_thread_databases"
COMPONENT_GLOBAL_STATE = "global_state"
COMPONENT_PAGINATED_HISTORY = "paginated_history"
ALL_COMPONENTS = (
    COMPONENT_HISTORICAL,
    COMPONENT_CORE,
    COMPONENT_LOGS,
    COMPONENT_ROLLOUTS,
    COMPONENT_SNAPSHOTS,
    COMPONENT_GENERATED,
    COMPONENT_CATALOG,
    COMPONENT_AUXILIARY,
    COMPONENT_GLOBAL_STATE,
    COMPONENT_PAGINATED_HISTORY,
)

DISPOSITION_WARN = "warn"
DISPOSITION_RETAIN_OBJECT = "retain_object"
DISPOSITION_RETAIN_TARGET_COMPONENT = "retain_target_component"
DISPOSITION_SKIP_COMPONENT = "skip_component"

PAGINATED_HISTORY_PRIMARY_KEYS = {
    "thread_history_projection_state": ["thread_id"],
    "thread_turns": ["thread_id", "turn_id"],
    "thread_items": ["thread_id", "turn_id", "item_id"],
}
PAGINATED_HISTORY_REQUIRED_COLUMNS = {
    "thread_history_projection_state": {
        "thread_id",
        "next_rollout_byte_offset",
        "next_rollout_ordinal",
    },
    "thread_turns": {"thread_id", "turn_id", "rollout_ordinal"},
    "thread_items": {
        "thread_id",
        "turn_id",
        "item_id",
        "rollout_ordinal",
    },
}
PAGINATED_HISTORY_KNOWN_TABLES = set(PAGINATED_HISTORY_PRIMARY_KEYS) | {
    "_sqlx_migrations"
}
PAGINATED_HISTORY_THREAD_REFERENCES = {
    (table, "thread_id") for table in PAGINATED_HISTORY_PRIMARY_KEYS
}


@dataclass
class ThreadInfo:
    id: str
    title: str = ""
    rollout_path: str = ""
    agent_nickname: str = ""
    agent_role: str = ""
    created_at_ms: int | None = None
    updated_at_ms: int | None = None
    edge_status: str = ""
    history_mode: str = "legacy"


@dataclass
class Plan:
    codex_home: Path
    root_ids: list[str]
    target_ids: list[str]
    state_database_path: Path | None = None
    logs_database_path: Path | None = None
    threads: dict[str, ThreadInfo] = field(default_factory=dict)
    open_subagents: list[str] = field(default_factory=list)
    rollout_files: list[Path] = field(default_factory=list)
    shell_snapshots: list[Path] = field(default_factory=list)
    generated_artifacts: list[Path] = field(default_factory=list)
    unsafe_paths: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    bytes_to_remove: int = 0
    warnings: list[str] = field(default_factory=list)
    safety_warnings: list[dict[str, Any]] = field(default_factory=list)
    component_plans: dict[str, dict[str, Any]] = field(default_factory=dict)
    target_dispositions: dict[str, dict[str, Any]] = field(default_factory=dict)
    executable_units: list[dict[str, Any]] = field(default_factory=list)
    retained_objects: list[dict[str, Any]] = field(default_factory=list)
    historical_residuals: dict[str, Any] = field(default_factory=dict)
    preflight: dict[str, Any] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    fingerprint: str = ""
    include_subagents: bool = True
    include_logs: bool = True
    scan_historical: bool = True
    target_incoming_edges: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    target_edge_rows: list[dict[str, str]] = field(default_factory=list)
    initial_state_thread_ids: set[str] = field(default_factory=set)
    artifact_contracts: dict[str, dict[str, Any]] = field(default_factory=dict)
    artifact_ownership_evidence: list[dict[str, Any]] = field(default_factory=list)
    desktop_catalog_path: Path | None = None
    desktop_catalog_rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    desktop_catalog_schema_signature: str = ""
    desktop_catalog_user_version: int | None = None
    desktop_catalog_revision: int | None = None
    auxiliary_thread_rows: dict[str, int] = field(default_factory=dict)
    auxiliary_thread_databases_present: list[str] = field(default_factory=list)
    auxiliary_thread_contracts: dict[str, dict[str, Any]] = field(default_factory=dict)
    auxiliary_thread_database_plans: dict[str, dict[str, Any]] = field(
        default_factory=dict
    )
    paginated_history_database_path: Path | None = None
    paginated_history_rows: dict[str, int] = field(default_factory=dict)
    paginated_history_contract: dict[str, Any] = field(default_factory=dict)
    paginated_history_database_plan: dict[str, Any] = field(default_factory=dict)
    global_state_refs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    global_state_files_present: dict[str, bool] = field(default_factory=dict)
    global_state_non_owning_mentions: dict[str, list[dict[str, Any]]] = field(
        default_factory=dict
    )
    rollout_migration_state_rows: list[dict[str, Any]] = field(default_factory=list)
    rollout_migration_skipped_rows: list[dict[str, Any]] = field(default_factory=list)


class PartialMutationError(RuntimeError):
    """A component failed after it may have started changing managed state."""

    def __init__(self, component: str, cause: BaseException):
        super().__init__(f"{component}: {cause}")
        self.component = component
        self.cause = cause


class DesktopOfflineGate(RuntimeError):
    """Apply cannot start while Desktop ownership is live or indeterminate."""

    def __init__(self, reason: str, next_action: str):
        super().__init__(reason)
        self.reason = reason
        self.next_action = next_action


class MutationConcurrencyGate(RuntimeError):
    """Another deletion apply owns the selected Codex home's mutation lock."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
        self.next_action = "wait_for_receipt"


def safety_warning(
    code: str,
    message: str,
    component: str,
    disposition: str,
    *,
    session_id: str | None = None,
    object_id: str | None = None,
    safe_operations_remaining: bool = True,
    retry_hint: str | None = None,
) -> dict[str, Any]:
    schema_disposition = (
        "scope_downgraded"
        if disposition == DISPOSITION_WARN
        else "requires_explicit_include"
        if disposition == "requires_explicit_include"
        else "skip_and_preserve"
    )
    scope = "target" if session_id is not None else "object" if object_id else "component"
    finding: dict[str, Any] = {
        "code": code,
        "scope": scope,
        "affected_ids": [session_id] if session_id is not None else [],
        "affected_objects": [object_id] if object_id is not None else [],
        "reason": message,
        "disposition": schema_disposition,
        "safe_operations_remaining": safe_operations_remaining,
        # Compatibility fields retained for older report consumers.
        "message": message,
        "component": component,
        "legacy_disposition": disposition,
    }
    if session_id is not None:
        finding["session_id"] = session_id
    if object_id is not None:
        finding["object_id"] = object_id
    if retry_hint is not None:
        finding["retry_hint"] = retry_hint
    return finding


def append_safety_warning(plan: Plan, finding: dict[str, Any]) -> None:
    identity = json.dumps(finding, ensure_ascii=False, sort_keys=True)
    if any(
        json.dumps(item, ensure_ascii=False, sort_keys=True) == identity
        for item in plan.safety_warnings
    ):
        return
    plan.safety_warnings.append(finding)


def component_plan_enabled(plan: Plan, component: str) -> bool:
    return plan.component_plans.get(component, {}).get("status", "enabled") == "enabled"


def skip_plan_component(plan: Plan, component: str, message: str) -> None:
    entry = plan.component_plans.setdefault(
        component,
        {"status": "enabled", "reasons": []},
    )
    entry["status"] = "skipped"
    reasons = entry.setdefault("reasons", [])
    if message not in reasons:
        reasons.append(message)


def unsafe_artifact_record(
    codex_home: Path,
    message: str,
) -> tuple[str, Path, dict[str, Any] | None] | None:
    """Extract an exact managed artifact path from a scanner safety finding."""
    match = re.search(r"(/[^\n]*?)$", message)
    if match is None:
        return None
    path = Path(match.group(1).strip())
    if not path.is_absolute() or ".." in path.parts:
        return None
    roots = {
        COMPONENT_ROLLOUTS: codex_home / "sessions",
        COMPONENT_SNAPSHOTS: codex_home / "shell_snapshots",
        COMPONENT_GENERATED: codex_home / "generated_images",
    }
    component = next(
        (
            candidate
            for candidate, root in roots.items()
            if path == root or path.is_relative_to(root)
        ),
        "",
    )
    if not component:
        return None
    contract: dict[str, Any] | None = None
    if path_is_present(path):
        try:
            contract = path_contract_entry(path)
        except (OSError, RuntimeError, UnicodeError):
            contract = None
    return component, path, contract


def notify_mutation(observer: Any | None, component: str) -> None:
    if observer is not None:
        observer(component)


def acquire_global_mutation_lock(codex_home: Path) -> int:
    """Acquire the non-blocking cross-job mutation lock for one Codex home."""
    if codex_home.is_symlink() or not codex_home.is_dir():
        raise RuntimeError(
            f"Selected Codex home is not a safe real directory: {codex_home}"
        )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(codex_home, flags)
    try:
        opened = os.fstat(fd)
        current = codex_home.lstat()
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.getuid()
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise RuntimeError(
                f"Selected Codex home is not an owner-controlled real directory: {codex_home}"
            )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MutationConcurrencyGate(
                "Another deletion apply is already mutating this Codex home."
            ) from exc
        except PermissionError as exc:
            raise RuntimeError(
                "The selected Codex home directory cannot be locked for mutation."
            ) from exc
        # Recheck the directory entry after locking so a replaced lock path cannot
        # let two jobs believe they own distinct coordination objects.
        current = codex_home.lstat()
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise RuntimeError("Codex home was replaced while its mutation lock was acquired")
        return fd
    except Exception:
        os.close(fd)
        raise


def release_global_mutation_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report or delete Codex session state by thread/session ID."
    )
    parser.add_argument(
        "session_ids", nargs="+", help="Codex thread/session IDs to remove."
    )
    parser.add_argument(
        "--codex-home",
        default=os.environ.get("CODEX_HOME", "~/.codex"),
        help="Codex home directory. Defaults to CODEX_HOME or ~/.codex.",
    )
    parser.add_argument(
        "--apply", action="store_true", help="Actually delete/update state."
    )
    parser.add_argument(
        "--confirm-plan",
        metavar="APPROVAL_CAPSULE",
        help="Required with --apply; must match the approved scope token from the report.",
    )
    parser.add_argument(
        "--no-subagents",
        action="store_true",
        help="Do not recursively include child subagent sessions.",
    )
    parser.add_argument(
        "--no-logs",
        action="store_true",
        help="Do not delete matching rows from the selected logs_*.sqlite database.",
    )
    parser.add_argument(
        "--force-open",
        action="store_true",
        help="Allow apply when target set includes open subagents.",
    )
    parser.add_argument(
        "--no-historical-scan",
        action="store_true",
        help="Skip the separate scan for old residual session artifacts.",
    )
    parser.add_argument(
        "--apply-historical-residuals",
        action="store_true",
        help=(
            "With --apply, also remove old residual artifacts reported by the historical scan. "
            "Does not delete state threads whose rollout file is missing."
        ),
    )
    parser.add_argument(
        "--apply-missing-rollout-threads",
        action="store_true",
        help=(
            "With --apply-historical-residuals, also delete state threads whose rollout file "
            "is missing. Requires separate explicit approval."
        ),
    )
    parser.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON."
    )
    return parser.parse_args()


def lstat_identity(path: Path) -> tuple[int, int, int, int, int]:
    info = path.lstat()
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)


def stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)


def connect_sqlite(path: Path, mode: str) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    before = lstat_identity(path)
    uri = f"{path.absolute().as_uri()}?mode={mode}"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        after = lstat_identity(path)
        database_rows = conn.execute("PRAGMA database_list").fetchall()
        main_paths = [str(row[2]) for row in database_rows if str(row[1]) == "main"]
        if before != after or len(main_paths) != 1:
            raise sqlite3.OperationalError(
                f"SQLite file identity changed while opening: {path}"
            )
        opened_path = Path(main_paths[0]).resolve()
        if opened_path != path.resolve():
            raise sqlite3.OperationalError(
                f"SQLite opened an unexpected path: {opened_path}"
            )
        family_issues = managed_sqlite_issues(path.parent, path)
        if family_issues:
            raise sqlite3.OperationalError(
                "SQLite file safety changed while opening: " + " | ".join(family_issues)
            )
    except Exception:
        conn.close()
        raise
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def connect_ro(path: Path) -> sqlite3.Connection | None:
    return connect_sqlite(path, "ro")


def connect_rw(path: Path) -> sqlite3.Connection | None:
    conn = connect_sqlite(path, "rw")
    if conn is None:
        return None
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    except Exception:
        conn.close()
        raise


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not table_exists(conn, table):
        return set()
    return {row[1] for row in conn.execute(f"PRAGMA table_info({quote_ident(table)})")}


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def ordered_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    if not table_exists(conn, table):
        return []
    return [
        str(row[1])
        for row in sorted(
            conn.execute(f"PRAGMA table_info({quote_ident(table)})"),
            key=lambda row: int(row[0]),
        )
    ]


def sqlite_row_sha256(column_names: list[str], values: Iterable[Any]) -> str:
    digest = hashlib.sha256()
    for column, value in zip(column_names, values, strict=True):
        encoded_column = column.encode("utf-8")
        digest.update(len(encoded_column).to_bytes(4, "big"))
        digest.update(encoded_column)
        if value is None:
            marker, encoded_value = b"N", b""
        elif isinstance(value, bytes):
            marker, encoded_value = b"B", value
        elif isinstance(value, str):
            marker, encoded_value = b"T", value.encode("utf-8")
        elif isinstance(value, int):
            marker, encoded_value = b"I", str(value).encode("ascii")
        elif isinstance(value, float):
            marker, encoded_value = b"F", repr(value).encode("ascii")
        else:
            marker, encoded_value = b"R", repr(value).encode("utf-8")
        digest.update(marker)
        digest.update(len(encoded_value).to_bytes(8, "big"))
        digest.update(encoded_value)
    return digest.hexdigest()


def reference_row_digests(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    session_id: str,
) -> list[str]:
    column_names = ordered_columns(conn, table)
    if not column_names:
        return []
    rows = conn.execute(
        f"SELECT * FROM {quote_ident(table)} "
        f"WHERE lower(CAST({quote_ident(column)} AS TEXT))=?",
        (session_id.lower(),),
    )
    return sorted(sqlite_row_sha256(column_names, row) for row in rows)


def placeholders(values: Iterable[str]) -> str:
    vals = list(values)
    if not vals:
        return "NULL"
    return ",".join("?" for _ in vals)


def normalize_ids(ids: list[str]) -> list[str]:
    bad = [sid for sid in ids if not CANONICAL_UUID_RE.fullmatch(sid)]
    if bad:
        raise ValueError(f"Refusing suspicious session id(s): {', '.join(bad)}")
    return list(dict.fromkeys(sid.lower() for sid in ids))


def database_check(path: Path, pragma: str) -> str:
    conn = connect_ro(path)
    if conn is None:
        return "missing"
    try:
        return str(conn.execute(f"PRAGMA {pragma}").fetchone()[0])
    finally:
        conn.close()


def is_thread_reference_column(column: str) -> bool:
    lowered = column.lower()
    return (
        lowered in SESSION_REFERENCE_COLUMNS
        or lowered.endswith("_thread_id")
        or lowered.endswith("_session_id")
        or lowered.endswith("_conversation_id")
    )


def user_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def schema_extension_locations(
    conn: sqlite3.Connection,
    known_tables: set[str],
    known_thread_references: set[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Return storage locations that a fixed schema contract does not own.

    Every column in an unknown table is inspected.  In a known table, only a
    newly introduced thread/session/conversation reference-shaped column is
    inspected so ordinary extensible payload columns do not become blockers.
    """

    locations: list[tuple[str, str]] = []
    for table in sorted(user_tables(conn)):
        table_columns = sorted(columns(conn, table))
        if table not in known_tables:
            locations.extend((table, column) for column in table_columns)
            continue
        locations.extend(
            (table, column)
            for column in table_columns
            if is_thread_reference_column(column)
            and (table, column) not in known_thread_references
        )
    return locations


def runtime_schema_compatibility(
    conn: sqlite3.Connection | None,
    known_tables: set[str],
    known_thread_references: set[tuple[str, str]],
    candidate_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Conservatively assess schema extensions without authorizing their deletion.

    Unknown storage may protect a candidate UUID, but it never expands a delete
    set.  A bounded scan protects every candidate if the extension data is too
    large to inspect completely.
    """

    if conn is None:
        return {
            "available": False,
            "unknown_tables": [],
            "candidate_reference_locations": [],
            "protected_ids": [],
            "target_reference_hits": [],
            "scan_complete": True,
            "scan_limit_bytes": MAX_RUNTIME_SCHEMA_SCAN_BYTES,
            "scanned_bytes": 0,
        }
    candidates = {
        str(value).lower()
        for value in candidate_ids
        if isinstance(value, str) and CANONICAL_UUID_RE.fullmatch(value)
    }
    unknown_tables = sorted(user_tables(conn) - known_tables)
    locations = schema_extension_locations(
        conn,
        known_tables,
        known_thread_references,
    )
    location_hits: dict[tuple[str, str], set[str]] = {}
    scanned_bytes = 0
    scan_complete = True
    if candidates:
        locations_by_table: dict[str, list[str]] = {}
        for table, column in locations:
            locations_by_table.setdefault(table, []).append(column)
        for table, table_columns in sorted(locations_by_table.items()):
            select_columns = ", ".join(quote_ident(column) for column in table_columns)
            for row in conn.execute(
                f"SELECT {select_columns} FROM {quote_ident(table)}"
            ):
                for column, value in zip(table_columns, row, strict=True):
                    if isinstance(value, str):
                        encoded = value.encode("utf-8", errors="replace")
                        matches = {
                            match.lower() for match in UUID_RE.findall(value)
                        }
                    elif isinstance(value, bytes):
                        encoded = value
                        matches = {
                            match.decode("ascii").lower()
                            for match in UUID_BYTES_RE.findall(value)
                        }
                    else:
                        continue
                    scanned_bytes += len(encoded)
                    if scanned_bytes > MAX_RUNTIME_SCHEMA_SCAN_BYTES:
                        scan_complete = False
                        break
                    protected = matches & candidates
                    if protected:
                        location_hits.setdefault((table, column), set()).update(
                            protected
                        )
                if not scan_complete:
                    break
            if not scan_complete:
                break
    protected_ids = set().union(*location_hits.values()) if location_hits else set()
    ambiguous_ids = set()
    if not scan_complete:
        ambiguous_ids = set(candidates)
        protected_ids.update(ambiguous_ids)
    return {
        "available": True,
        "unknown_tables": unknown_tables,
        "candidate_reference_locations": [
            {"table": table, "column": column} for table, column in locations
        ],
        "protected_ids": sorted(protected_ids),
        "target_reference_hits": [
            {
                "table": table,
                "column": column,
                "ids": sorted(ids),
            }
            for (table, column), ids in sorted(location_hits.items())
        ],
        "ambiguous_candidate_ids": sorted(ambiguous_ids),
        "scan_complete": scan_complete,
        "scan_limit_bytes": MAX_RUNTIME_SCHEMA_SCAN_BYTES,
        "scanned_bytes": scanned_bytes,
    }


def state_schema_compatibility(
    state: sqlite3.Connection | None,
    candidate_ids: Iterable[str] = (),
) -> dict[str, Any]:
    return runtime_schema_compatibility(
        state,
        KNOWN_STATE_TABLES,
        KNOWN_STATE_THREAD_REFERENCES
        | KNOWN_STATE_NON_OWNING_THREAD_REFERENCES,
        candidate_ids,
    )


def desktop_catalog_schema_compatibility(
    catalog: sqlite3.Connection | None,
    candidate_ids: Iterable[str] = (),
) -> dict[str, Any]:
    assessment = runtime_schema_compatibility(
        catalog,
        DESKTOP_CATALOG_KNOWN_TABLES,
        DESKTOP_CATALOG_THREAD_REFERENCES,
        candidate_ids,
    )
    assessment["user_version"] = desktop_catalog_user_version(catalog)
    assessment["newer_user_version_accepted"] = bool(
        assessment["user_version"] is not None
        and assessment["user_version"] > DESKTOP_CATALOG_MAX_USER_VERSION
    )
    return assessment


def runtime_schema_target_issues(
    database_label: str,
    assessment: dict[str, Any],
) -> list[str]:
    issues: list[str] = []
    hits = assessment.get("target_reference_hits", [])
    if hits:
        rendered = ", ".join(
            f"{entry['table']}.{entry['column']}="
            + "/".join(entry.get("ids", []))
            for entry in hits
        )
        issues.append(
            f"Runtime-compatible {database_label} extension contains target session "
            f"references that must be preserved: {rendered}."
        )
    ambiguous = assessment.get("ambiguous_candidate_ids", [])
    if ambiguous:
        issues.append(
            f"Runtime-compatible {database_label} extension exceeded the bounded "
            "inspection limit; affected target sessions must be preserved: "
            + ", ".join(ambiguous)
            + "."
        )
    return issues


class MutationEffectIndeterminate(RuntimeError):
    """A shadow mutation could not produce a complete, bounded effect envelope."""


def sqlite_value_size(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf-8", errors="replace"))
    return len(repr(value).encode("utf-8", errors="replace"))


def sqlite_reference_ids(value: Any) -> set[str]:
    if isinstance(value, str):
        return {match.lower() for match in UUID_RE.findall(value)}
    if isinstance(value, bytes):
        return {
            match.decode("ascii").lower() for match in UUID_BYTES_RE.findall(value)
        }
    return set()


def mutation_dependency_objects(
    conn: sqlite3.Connection,
    anchor_tables: set[str],
) -> dict[str, list[dict[str, Any]]]:
    """Describe schema objects that can extend a mutation beyond its direct SQL.

    Presence alone is evidence, not a blocker.  The caller decides safety from a
    shadow execution of the actual mutation.
    """

    triggers: list[dict[str, Any]] = []
    for name, table, sql in conn.execute(
        "SELECT name, tbl_name, sql FROM sqlite_master "
        "WHERE type='trigger' ORDER BY name"
    ):
        normalized_sql = re.sub(r"\s+", " ", str(sql or "").strip())
        lowered_sql = normalized_sql.lower()
        if str(table) in anchor_tables or any(
            re.search(rf"\b{re.escape(candidate.lower())}\b", lowered_sql)
            for candidate in anchor_tables
        ):
            triggers.append(
                {
                    "name": str(name),
                    "table": str(table),
                    "sql": normalized_sql,
                    "sql_sha256": normalized_sql_hash(normalized_sql),
                }
            )

    foreign_keys: list[dict[str, Any]] = []
    for table in sorted(user_tables(conn)):
        for row in conn.execute(f"PRAGMA foreign_key_list({quote_ident(table)})"):
            referenced_table = str(row[2])
            if table not in anchor_tables and referenced_table not in anchor_tables:
                continue
            foreign_keys.append(
                {
                    "table": table,
                    "column": str(row[3]),
                    "referenced_table": referenced_table,
                    "referenced_column": str(row[4]),
                    "on_update": str(row[5]),
                    "on_delete": str(row[6]),
                }
            )
    return {"triggers": triggers, "foreign_keys": foreign_keys}


def mutation_effect_snapshot(
    conn: sqlite3.Connection,
    known_tables: set[str],
    known_thread_references: set[tuple[str, str]],
) -> dict[str, Any]:
    scanned_bytes = 0
    tables: dict[str, Any] = {}
    for table in sorted(user_tables(conn)):
        column_names = ordered_columns(conn, table)
        primary_key = primary_key_columns(conn, table)
        positions = {name: index for index, name in enumerate(column_names)}
        reference_columns = {
            column
            for candidate_table, column in known_thread_references
            if candidate_table == table and column in positions
        }
        if table not in known_tables:
            reference_columns.update(column_names)
        else:
            reference_columns.update(
                column for column in column_names if is_thread_reference_column(column)
            )
        rows: dict[str, dict[str, Any]] = {}
        duplicate_keys: dict[str, int] = {}
        for row in conn.execute(f"SELECT * FROM {quote_ident(table)}"):
            scanned_bytes += sum(sqlite_value_size(value) for value in row)
            if scanned_bytes > MAX_RUNTIME_SCHEMA_SCAN_BYTES:
                raise MutationEffectIndeterminate(
                    "shadow effect scan exceeded the bounded inspection limit"
                )
            row_sha256 = sqlite_row_sha256(column_names, row)
            if primary_key:
                key_values = [row[positions[column]] for column in primary_key]
                key = sqlite_row_sha256(primary_key, key_values)
            else:
                duplicate_index = duplicate_keys.get(row_sha256, 0)
                duplicate_keys[row_sha256] = duplicate_index + 1
                key = f"row:{row_sha256}:{duplicate_index}"
            reference_ids: set[str] = set()
            for column in reference_columns:
                reference_ids.update(sqlite_reference_ids(row[positions[column]]))
            rows[key] = {
                "row_sha256": row_sha256,
                "reference_ids": sorted(reference_ids),
            }
        tables[table] = {
            "primary_key": primary_key,
            "row_count": len(rows),
            "rows": rows,
        }
    return {"tables": tables, "scanned_bytes": scanned_bytes}


def mutation_effect_diff(
    before: dict[str, Any],
    after: dict[str, Any],
    target_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    effects: list[dict[str, Any]] = []
    outside_scope: list[dict[str, Any]] = []
    before_tables = before.get("tables", {})
    after_tables = after.get("tables", {})
    for table in sorted(set(before_tables) | set(after_tables)):
        before_rows = before_tables.get(table, {}).get("rows", {})
        after_rows = after_tables.get(table, {}).get("rows", {})
        removed = 0
        added = 0
        changed = 0
        target_owned = 0
        escaped = 0
        samples: list[dict[str, Any]] = []
        change_contract: list[dict[str, Any]] = []
        for key in sorted(set(before_rows) | set(after_rows)):
            old = before_rows.get(key)
            new = after_rows.get(key)
            if old == new:
                continue
            old_refs = set(old.get("reference_ids", [])) if old else set()
            new_refs = set(new.get("reference_ids", [])) if new else set()
            if old is None:
                change = "added"
                added += 1
                in_scope = False
            elif new is None:
                change = "removed"
                removed += 1
                in_scope = bool(old_refs & target_ids)
            else:
                change = "changed"
                changed += 1
                removed_targets = (old_refs - new_refs) & target_ids
                introduced_refs = new_refs - old_refs
                in_scope = bool(removed_targets) and not introduced_refs
            if in_scope:
                target_owned += 1
            else:
                escaped += 1
                finding = {
                    "table": table,
                    "change": change,
                    "before_reference_ids": sorted(old_refs),
                    "after_reference_ids": sorted(new_refs),
                    "row_key_sha256": key,
                }
                outside_scope.append(finding)
                if len(samples) < 5:
                    samples.append(finding)
            change_contract.append(
                {
                    "row_key_sha256": key,
                    "change": change,
                    "before_row_sha256": old.get("row_sha256", "") if old else "",
                    "after_row_sha256": new.get("row_sha256", "") if new else "",
                    "before_reference_ids": sorted(old_refs),
                    "after_reference_ids": sorted(new_refs),
                }
            )
        if removed or added or changed:
            effects.append(
                {
                    "table": table,
                    "removed": removed,
                    "added": added,
                    "changed": changed,
                    "target_owned_changes": target_owned,
                    "outside_scope_changes": escaped,
                    "outside_scope_samples": samples,
                    "change_contract_sha256": json_value_sha256(change_contract),
                }
            )
    return effects, outside_scope


def sqlite_mutation_effect_assessment(
    conn: sqlite3.Connection | None,
    target_ids: Iterable[str],
    known_tables: set[str],
    known_thread_references: set[tuple[str, str]],
    anchor_tables: set[str],
    mutate: Any,
    required_absent_tables: set[str] | None = None,
) -> dict[str, Any]:
    """Run the real mutation against an in-memory clone and classify its effects."""

    targets = {
        str(value).lower()
        for value in target_ids
        if isinstance(value, str) and CANONICAL_UUID_RE.fullmatch(value)
    }
    required_absent_tables = required_absent_tables or set()
    if conn is None:
        return {"status": "indeterminate", "reason": "database is unavailable"}
    dependencies = mutation_dependency_objects(conn, anchor_tables)
    if (
        not dependencies["triggers"]
        and not dependencies["foreign_keys"]
        and not required_absent_tables
    ):
        return {
            "status": "not_required",
            "reason": "no trigger or foreign-key dependency extends the direct mutation",
            **dependencies,
        }
    shadow = sqlite3.connect(":memory:")
    try:
        if conn.in_transaction:
            database_rows = list(conn.execute("PRAGMA database_list"))
            main_paths = [
                Path(str(row[2]))
                for row in database_rows
                if str(row[1]) == "main" and row[2]
            ]
            if len(main_paths) != 1:
                raise MutationEffectIndeterminate(
                    "active transaction has no unique on-disk database identity"
                )
            source = connect_ro(main_paths[0])
            if source is None:
                raise MutationEffectIndeterminate(
                    "active transaction database disappeared during shadow analysis"
                )
            try:
                source.backup(shadow)
            finally:
                source.close()
        else:
            conn.backup(shadow)
        shadow.execute("PRAGMA foreign_keys=ON")
        before = mutation_effect_snapshot(
            shadow,
            known_tables,
            known_thread_references,
        )
        shadow.execute("BEGIN")
        mutation_result = mutate(shadow, sorted(targets))
        quick_check = str(shadow.execute("PRAGMA quick_check").fetchone()[0])
        after = mutation_effect_snapshot(
            shadow,
            known_tables,
            known_thread_references,
        )
        effects, outside_scope = mutation_effect_diff(before, after, targets)
        remaining_target_references: list[dict[str, Any]] = []
        for table in sorted(required_absent_tables):
            rows = after.get("tables", {}).get(table, {}).get("rows", {})
            count = sum(
                1
                for row in rows.values()
                if set(row.get("reference_ids", [])) & targets
            )
            if count:
                remaining_target_references.append({"table": table, "rows": count})
        if quick_check != "ok" or outside_scope:
            status = "outside_scope"
        elif remaining_target_references:
            status = "target_residual"
        else:
            status = "target_only"
        return {
            "status": status,
            "reason": (
                "shadow execution changed only rows owned by the approved targets"
                if status == "target_only"
                else (
                    "shadow execution left target references in runtime-discovered storage"
                    if status == "target_residual"
                    else "shadow execution changed rows outside the approved target ownership envelope"
                )
            ),
            "quick_check": quick_check,
            "mutation_result": mutation_result,
            "effects": effects,
            "outside_scope_change_count": len(outside_scope),
            "outside_scope_samples": outside_scope[:20],
            "remaining_target_references": remaining_target_references,
            "scanned_bytes": int(before["scanned_bytes"]) + int(after["scanned_bytes"]),
            "scan_limit_bytes_per_snapshot": MAX_RUNTIME_SCHEMA_SCAN_BYTES,
            **dependencies,
        }
    except (MutationEffectIndeterminate, OSError, RuntimeError, sqlite3.Error) as exc:
        return {
            "status": "indeterminate",
            "reason": str(exc),
            **dependencies,
        }
    finally:
        shadow.close()


def mutation_effect_issues(database_label: str, assessment: dict[str, Any]) -> list[str]:
    status = assessment.get("status")
    if status in {"not_required", "target_only"}:
        return []
    dependency_kind = "trigger/foreign-key"
    return [
        f"Shadow execution of {database_label} {dependency_kind} effects was "
        f"{status}: {assessment.get('reason', 'unknown effect')}."
    ]


def normalized_sql_hash(sql: str) -> str:
    normalized = re.sub(r"\s+", " ", sql.strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def state_trigger_issues(state: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    for name, table, sql in state.execute(
        "SELECT name, tbl_name, sql FROM sqlite_master "
        "WHERE type='trigger' ORDER BY name"
    ):
        trigger_name = str(name)
        expected_hash = KNOWN_STATE_TRIGGER_HASHES.get(trigger_name)
        normalized_sql = re.sub(r"\s+", " ", str(sql or "").strip())
        actual_hash = normalized_sql_hash(normalized_sql)
        if expected_hash is not None and (
            table != "threads" or expected_hash != actual_hash
        ):
            issues.append(
                f"Unsupported or modified state trigger discovered: {trigger_name}."
            )
        # Unknown triggers are assessed by executing the exact mutation against
        # an in-memory clone.  Their presence is not itself evidence of danger.
    return issues


def trigger_names(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name"
        )
    ]


def state_schema_issues(state: sqlite3.Connection | None) -> list[str]:
    if state is None:
        return []

    issues: list[str] = []
    issues.extend(state_trigger_issues(state))
    for table, required in KNOWN_STATE_TABLE_COLUMNS.items():
        if not table_exists(state, table):
            if table == "threads":
                issues.append("Required state table 'threads' is missing.")
            continue
        actual_columns = columns(state, table)
        missing = sorted(required - actual_columns)
        if missing:
            issues.append(
                f"State table '{table}' is missing required column(s): {', '.join(missing)}."
            )
        exact_columns = EXACT_STATE_TABLE_COLUMNS.get(table)
        if exact_columns is not None:
            extra = sorted(actual_columns - exact_columns)
            if extra:
                issues.append(
                    f"State table '{table}' has unsupported column(s): {', '.join(extra)}."
                )
            primary_key = [
                str(row[1])
                for row in sorted(
                    (
                        row
                        for row in state.execute(
                            f"PRAGMA table_info({quote_ident(table)})"
                        )
                        if int(row[5]) > 0
                    ),
                    key=lambda row: int(row[5]),
                )
            ]
            expected_primary_key = EXPECTED_STATE_PRIMARY_KEYS[table]
            if primary_key != expected_primary_key:
                issues.append(
                    f"State table '{table}' has unsupported primary key: "
                    f"{primary_key}; expected {expected_primary_key}."
                )
            if list(state.execute(f"PRAGMA foreign_key_list({quote_ident(table)})")):
                issues.append(
                    f"State table '{table}' unexpectedly contains foreign keys."
                )

    if table_exists(state, "rollout_migration_state"):
        migration_columns = columns(state, "rollout_migration_state")
        cursor_columns = {
            "migration_id",
            "last_checked_thread_created_at",
            "last_checked_thread_id",
        }
        if cursor_columns <= migration_columns:
            for migration_id, created_at, thread_id in state.execute(
                "SELECT migration_id, last_checked_thread_created_at, "
                "last_checked_thread_id FROM rollout_migration_state"
            ):
                if (created_at is None) != (thread_id is None):
                    issues.append(
                        "rollout_migration_state contains an incomplete last-checked "
                        f"thread cursor for migration {migration_id}."
                    )
    return sorted(dict.fromkeys(issues))


def state_mutation_schema_issues(
    state: sqlite3.Connection | None,
) -> list[str]:
    return [
        issue
        for issue in state_schema_issues(state)
        if "incomplete last-checked thread cursor" not in issue
    ]


def state_runtime_mutation_issues(
    state: sqlite3.Connection | None,
    target_ids: Iterable[str],
) -> list[str]:
    assessment = state_schema_compatibility(state, target_ids)
    effect_assessment = state_mutation_effect_assessment(state, target_ids)
    extension_issues = (
        []
        if effect_assessment.get("status") == "target_only"
        else runtime_schema_target_issues("state database", assessment)
    )
    return sorted(
        dict.fromkeys(
            state_mutation_schema_issues(state)
            + extension_issues
            + mutation_effect_issues("state database", effect_assessment)
        )
    )


def state_mutation_effect_assessment(
    state: sqlite3.Connection | None,
    target_ids: Iterable[str],
) -> dict[str, Any]:
    target_ids = list(target_ids)
    compatibility = state_schema_compatibility(state, target_ids)
    required_absent_tables = {
        str(hit.get("table", ""))
        for hit in compatibility.get("target_reference_hits", [])
        if hit.get("table")
    }
    known_references = KNOWN_STATE_THREAD_REFERENCES | {
        ("threads", "id"),
    }
    anchor_tables = {table for table, _column in known_references}
    return sqlite_mutation_effect_assessment(
        state,
        target_ids,
        KNOWN_STATE_TABLES,
        known_references,
        anchor_tables,
        delete_state_rows_on_conn,
        required_absent_tables,
    )


def desktop_catalog_runtime_mutation_issues(
    catalog: sqlite3.Connection | None,
    target_ids: Iterable[str],
) -> list[str]:
    assessment = desktop_catalog_schema_compatibility(catalog, target_ids)
    return sorted(
        dict.fromkeys(
            desktop_catalog_schema_issues(catalog)
            + runtime_schema_target_issues(
                "Desktop catalog database",
                assessment,
            )
        )
    )


def logs_schema_compatibility(
    logs: sqlite3.Connection | None,
    candidate_ids: Iterable[str] = (),
) -> dict[str, Any]:
    return runtime_schema_compatibility(
        logs,
        KNOWN_LOG_TABLES,
        {("logs", "thread_id")},
        candidate_ids,
    )


def logs_schema_issues(logs: sqlite3.Connection | None) -> list[str]:
    if logs is None:
        return []
    issues: list[str] = []
    if not table_exists(logs, "logs"):
        issues.append("Required logs table 'logs' is missing.")
        return issues
    log_columns = columns(logs, "logs")
    for required_column in ["id", "thread_id"]:
        if required_column not in log_columns:
            issues.append(
                f"Logs table 'logs' is missing required column '{required_column}'."
            )
    id_info = next(
        (row for row in logs.execute("PRAGMA table_info(logs)") if str(row[1]) == "id"),
        None,
    )
    if id_info is not None and (
        str(id_info[2]).upper() != "INTEGER" or int(id_info[5]) != 1
    ):
        issues.append("Logs table 'logs.id' is not the expected INTEGER PRIMARY KEY.")
    for _object_type, name, table, sql in logs.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE type='trigger' ORDER BY name"
    ):
        normalized_sql = re.sub(r"\s+", " ", str(sql or "").strip()).lower()
        if str(table) == "logs" or re.search(r"\blogs\b", normalized_sql):
            issues.append(
                f"Unsupported logs trigger can affect known log storage: {name}."
            )
    for table in sorted(user_tables(logs)):
        for fk in logs.execute(f"PRAGMA foreign_key_list({quote_ident(table)})"):
            if table == "logs" or str(fk[2]) == "logs":
                issues.append(
                    "Unsupported logs foreign key can affect known log storage: "
                    f"{table}.{fk[3]} -> {fk[2]}.{fk[4]}."
                )
    return sorted(dict.fromkeys(issues))


def logs_runtime_mutation_issues(
    logs: sqlite3.Connection | None,
    target_ids: Iterable[str],
) -> list[str]:
    assessment = logs_schema_compatibility(logs, target_ids)
    return sorted(
        dict.fromkeys(
            logs_schema_issues(logs)
            + runtime_schema_target_issues("logs database", assessment)
        )
    )


def database_family_discovery(
    codex_home: Path,
    family: str,
    anchor_table: str,
    anchor_columns: set[str],
) -> dict[str, Any]:
    """Discover one authoritative SQLite family member by structural anchors.

    Filenames are inventory hints only.  A single safe family member is selected
    even when its numeric suffix is new.  With multiple members, exactly one must
    expose the required anchor or the family remains ambiguous and untouched.
    """

    candidates = sorted(
        path
        for path in codex_home.glob(f"{family}_*.sqlite")
        if path_is_present(path)
    ) if codex_home.is_dir() else []
    inventory: list[dict[str, Any]] = []
    anchor_matches: list[Path] = []
    safe_candidates: list[Path] = []
    issues: list[str] = []
    for path in candidates:
        path_issues = managed_sqlite_issues(codex_home, path)
        entry: dict[str, Any] = {
            "path": str(path),
            "safe": not path_issues,
            "anchor_match": False,
            "issues": path_issues,
        }
        if path_issues:
            issues.extend(path_issues)
            inventory.append(entry)
            continue
        safe_candidates.append(path)
        conn: sqlite3.Connection | None = None
        try:
            conn = connect_ro(path)
            entry["anchor_match"] = bool(
                conn is not None
                and table_exists(conn, anchor_table)
                and anchor_columns <= columns(conn, anchor_table)
            )
        except (OSError, RuntimeError, sqlite3.Error, UnicodeError) as exc:
            entry["issues"] = [f"Unable to inspect database anchor: {exc}"]
        finally:
            if conn is not None:
                conn.close()
        if entry["anchor_match"]:
            anchor_matches.append(path)
        inventory.append(entry)

    selected: Path | None = None
    if len(candidates) == 1 and len(safe_candidates) == 1:
        selected = safe_candidates[0]
    elif len(anchor_matches) == 1:
        selected = anchor_matches[0]
    elif len(anchor_matches) > 1:
        issues.append(
            f"Multiple {family}_*.sqlite databases match the required "
            f"{anchor_table} anchor; refusing to guess: "
            + ", ".join(path.name for path in anchor_matches)
        )
    elif candidates:
        issues.append(
            f"No unique safe {family}_*.sqlite database exposes the required "
            f"{anchor_table} anchor."
        )
    return {
        "family": family,
        "selected_path": str(selected) if selected is not None else "",
        "candidates": inventory,
        "issues": sorted(dict.fromkeys(issues)),
        "ambiguous": bool(issues),
    }


def discover_state_database(codex_home: Path) -> dict[str, Any]:
    return database_family_discovery(codex_home, "state", "threads", {"id"})


def discover_logs_database(codex_home: Path) -> dict[str, Any]:
    return database_family_discovery(codex_home, "logs", "logs", {"id", "thread_id"})


def discover_paginated_history_database(codex_home: Path) -> dict[str, Any]:
    return database_family_discovery(
        codex_home,
        "thread_history",
        "thread_items",
        {"thread_id", "turn_id", "item_id"},
    )


def discovered_database_path(discovery: dict[str, Any]) -> Path | None:
    value = discovery.get("selected_path", "")
    return Path(value) if isinstance(value, str) and value else None


def primary_key_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    if not table_exists(conn, table):
        return []
    return [
        str(row[1])
        for row in sorted(
            (
                row
                for row in conn.execute(f"PRAGMA table_info({quote_ident(table)})")
                if int(row[5]) > 0
            ),
            key=lambda row: int(row[5]),
        )
    ]


def sqlite_directory_database_candidates(codex_home: Path) -> list[Path]:
    """Return managed SQLite candidates without assuming product filenames."""

    sqlite_root = codex_home / "sqlite"
    if not sqlite_root.is_dir():
        return []
    return sorted(
        path
        for path in sqlite_root.iterdir()
        if path_is_present(path) and path.suffix.lower() in {".db", ".sqlite"}
    )


def discover_desktop_catalog_path(
    codex_home: Path,
) -> tuple[Path | None, list[str]]:
    matches: list[Path] = []
    issues: list[str] = []
    for path in sqlite_directory_database_candidates(codex_home):
        path_issues = managed_sqlite_issues(codex_home, path)
        if path_issues:
            if path.name in DESKTOP_CATALOG_LEGACY_FILENAMES:
                issues.extend(path_issues)
            continue
        conn: sqlite3.Connection | None = None
        try:
            conn = connect_ro(path)
            tables = user_tables(conn)
            if tables & DESKTOP_CATALOG_KNOWN_TABLES:
                matches.append(path)
        except (OSError, RuntimeError, sqlite3.Error, UnicodeError) as exc:
            if path.name in DESKTOP_CATALOG_LEGACY_FILENAMES:
                issues.append(f"Unable to inspect desktop catalog candidate {path}: {exc}")
        finally:
            if conn is not None:
                conn.close()
    if len(matches) > 1:
        issues.append(
            "Multiple structurally matching desktop catalog databases exist; "
            "refusing to guess: " + ", ".join(str(path) for path in matches)
        )
        return None, sorted(dict.fromkeys(issues))
    return (matches[0] if matches else None), sorted(dict.fromkeys(issues))


def sqlite_schema_signature(
    conn: sqlite3.Connection | None,
    *,
    include_indexes: bool = False,
) -> str:
    if conn is None:
        return ""
    schema_types = (
        "'table', 'view', 'trigger', 'index'"
        if include_indexes
        else ("'table', 'view', 'trigger'")
    )
    rows = [
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table": str(row[2]),
            "sql": re.sub(r"\s+", " ", str(row[3] or "").strip()),
        }
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            f"WHERE type IN ({schema_types}) "
            "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )
    ]
    encoded = json.dumps(rows, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def desktop_catalog_schema_signature(conn: sqlite3.Connection | None) -> str:
    return sqlite_schema_signature(conn)


def auxiliary_thread_schema_signature(conn: sqlite3.Connection | None) -> str:
    return sqlite_schema_signature(conn, include_indexes=True)


def desktop_catalog_user_version(conn: sqlite3.Connection | None) -> int | None:
    if conn is None:
        return None
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def desktop_catalog_schema_issues(
    catalog: sqlite3.Connection | None,
) -> list[str]:
    if catalog is None:
        return []
    issues: list[str] = []
    for object_type, name, table, sql in catalog.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE type='trigger' ORDER BY name"
    ):
        normalized_sql = re.sub(r"\s+", " ", str(sql or "").strip()).lower()
        touches_deletable_storage = str(table) in DESKTOP_CATALOG_DELETABLE_TABLES or any(
            re.search(rf"\b{re.escape(candidate)}\b", normalized_sql)
            for candidate in DESKTOP_CATALOG_DELETABLE_TABLES
        )
        if touches_deletable_storage:
            issues.append(
                f"Unsupported desktop catalog {object_type} can affect known "
                f"thread storage: {name}."
            )
    user_version = desktop_catalog_user_version(catalog)
    if user_version is None or user_version < 0:
        issues.append("Desktop catalog user_version is invalid.")
    elif user_tables(catalog) and user_version == 0:
        issues.append(
            "Desktop catalog contains tables but has user_version=0; refusing an "
            "unversioned schema."
        )
    if table_exists(catalog, "local_thread_catalog") and (
        user_version is None or user_version < 22
    ):
        issues.append("Desktop catalog table requires user_version >= 22.")
    if table_exists(catalog, "thread_timeline_ledger") and (
        user_version is None or user_version < 26
    ):
        issues.append("Desktop timeline table requires user_version >= 26.")
    for table, required in DESKTOP_CATALOG_REQUIRED_COLUMNS.items():
        if not table_exists(catalog, table):
            continue
        actual = columns(catalog, table)
        missing = sorted(required - actual)
        if missing:
            issues.append(
                f"Desktop catalog table '{table}' is missing required column(s): "
                + ", ".join(missing)
                + "."
            )
        expected_primary_key = DESKTOP_CATALOG_PRIMARY_KEYS[table]
        actual_primary_key = primary_key_columns(catalog, table)
        if actual_primary_key != expected_primary_key:
            issues.append(
                f"Desktop catalog table '{table}' has unsupported primary key: "
                f"{actual_primary_key}; expected {expected_primary_key}."
            )
    for table in sorted(user_tables(catalog)):
        for fk in catalog.execute(f"PRAGMA foreign_key_list({quote_ident(table)})"):
            referenced_table = str(fk[2])
            if (
                table in DESKTOP_CATALOG_DELETABLE_TABLES
                or referenced_table in DESKTOP_CATALOG_DELETABLE_TABLES
            ):
                issues.append(
                    "Unsupported desktop catalog foreign key can affect known "
                    f"thread storage: {table}.{fk[3]} -> "
                    f"{referenced_table}.{fk[4]}."
                )
    if table_exists(catalog, "local_thread_catalog"):
        for required_table in [
            "local_thread_catalog_hosts",
            "local_thread_catalog_metadata",
            "local_thread_catalog_sync_state",
        ]:
            if not table_exists(catalog, required_table):
                issues.append(
                    f"Desktop catalog table '{required_table}' is missing while "
                    "local_thread_catalog exists."
                )
    if table_exists(catalog, "local_thread_catalog_metadata") and {
        "id",
        "catalog_revision",
    }.issubset(columns(catalog, "local_thread_catalog_metadata")):
        rows = catalog.execute(
            "SELECT id, catalog_revision FROM local_thread_catalog_metadata ORDER BY id"
        ).fetchall()
        if (
            len(rows) != 1
            or rows[0][0] != 1
            or not isinstance(rows[0][1], int)
            or int(rows[0][1]) < 0
            or int(rows[0][1]) >= 2**63 - 1
        ):
            issues.append(
                "Desktop catalog metadata must contain exactly id=1 with a non-negative "
                "INTEGER catalog_revision."
            )
    return sorted(dict.fromkeys(issues))


def desktop_catalog_target_host_issues(
    catalog: sqlite3.Connection | None,
    catalog_contracts: dict[str, list[dict[str, Any]]],
) -> list[str]:
    if catalog is None:
        return []
    if not all(
        table_exists(catalog, table)
        for table in [
            "local_thread_catalog_hosts",
            "local_thread_catalog_sync_state",
        ]
    ):
        return []
    increments: dict[str, int] = {}
    for contract in catalog_contracts.get("local_thread_catalog", []):
        host_id = str(contract["host_id"])
        increments[host_id] = increments.get(host_id, 0) + 1
    issues: list[str] = []
    for host_id, increment in sorted(increments.items()):
        host_row = catalog.execute(
            "SELECT host_id FROM local_thread_catalog_hosts WHERE host_id=?",
            (host_id,),
        ).fetchone()
        if host_row is None:
            issues.append(
                f"Desktop catalog target host is missing from hosts: {host_id}."
            )
        sync_row = catalog.execute(
            "SELECT observation_sequence FROM local_thread_catalog_sync_state "
            "WHERE host_id=?",
            (host_id,),
        ).fetchone()
        if (
            sync_row is None
            or not isinstance(sync_row[0], int)
            or int(sync_row[0]) < 0
            or int(sync_row[0]) > SQLITE_MAX_INTEGER - 1 - increment
        ):
            issues.append(
                f"Desktop catalog target host has an invalid sync sequence: {host_id}."
            )
    visible_removals = sum(
        1
        for contract in catalog_contracts.get("local_thread_catalog", [])
        if int(contract.get("missing_candidate", 1)) == 0
    )
    revision = desktop_catalog_revision(catalog)
    if visible_removals and (
        revision is None
        or revision < 0
        or revision > SQLITE_MAX_INTEGER - 1 - visible_removals
    ):
        issues.append(
            "Desktop catalog revision has insufficient integer headroom for the "
            "approved visible-row removals."
        )
    return issues


def desktop_catalog_revision(catalog: sqlite3.Connection | None) -> int | None:
    if catalog is None or not table_exists(catalog, "local_thread_catalog_metadata"):
        return None
    row = catalog.execute(
        "SELECT catalog_revision FROM local_thread_catalog_metadata WHERE id=1"
    ).fetchone()
    return int(row[0]) if row is not None and isinstance(row[0], int) else None


def desktop_catalog_row_contracts(
    catalog: sqlite3.Connection | None,
    target_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    contracts = {table: [] for table in DESKTOP_CATALOG_DELETABLE_TABLES}
    if catalog is None or not target_ids:
        return contracts
    for table, locator_columns in DESKTOP_CATALOG_DELETABLE_TABLES.items():
        if not table_exists(catalog, table):
            continue
        column_names = ordered_columns(catalog, table)
        positions = {name: column_names.index(name) for name in column_names}
        rows = catalog.execute(
            f"SELECT * FROM {quote_ident(table)} "
            f"WHERE lower(CAST(thread_id AS TEXT)) IN ({placeholders(target_ids)})",
            target_ids,
        )
        table_contracts: list[dict[str, Any]] = []
        for row in rows:
            contract: dict[str, Any] = {
                name: row[positions[name]] for name in locator_columns
            }
            contract["row_sha256"] = sqlite_row_sha256(column_names, row)
            if table == "local_thread_catalog":
                contract["display_title"] = str(row[positions["display_title"]])
                contract["missing_candidate"] = int(row[positions["missing_candidate"]])
            table_contracts.append(contract)
        contracts[table] = sorted(
            table_contracts,
            key=lambda item: tuple(str(item[name]) for name in locator_columns),
        )
    return contracts


def desktop_catalog_unsupported_target_counts(
    catalog: sqlite3.Connection | None,
    target_ids: list[str],
) -> dict[str, int]:
    if catalog is None:
        return {"inbox_items": 0}
    return {
        table: count_in_table(catalog, table, "thread_id", target_ids)
        for table in ["inbox_items"]
    }


def discover_auxiliary_thread_database_inventory(
    codex_home: Path,
) -> tuple[dict[str, tuple[str, str, int]], list[str]]:
    discovered: dict[str, tuple[str, str, int]] = {}
    issues: list[str] = []
    legacy_prefixes = {
        "codex-history-snapshots",
        "codex-thread-summaries",
    }
    for path in sqlite_directory_database_candidates(codex_home):
        path_issues = managed_sqlite_issues(codex_home, path)
        if path_issues:
            if any(path.name.startswith(prefix) for prefix in legacy_prefixes):
                issues.extend(path_issues)
            continue
        conn: sqlite3.Connection | None = None
        try:
            conn = connect_ro(path)
            matches = [
                metadata
                for table, metadata in THREAD_AUXILIARY_TABLE_ROLES.items()
                if table_exists(conn, table)
            ]
            if len(matches) == 1:
                discovered[path.name] = matches[0]
            elif len(matches) > 1:
                issues.append(
                    "Auxiliary database matches multiple thread-storage roles; "
                    f"refusing to guess: {path}"
                )
        except (OSError, RuntimeError, sqlite3.Error, UnicodeError) as exc:
            if any(path.name.startswith(prefix) for prefix in legacy_prefixes):
                issues.append(f"Unable to inspect auxiliary database {path}: {exc}")
        finally:
            if conn is not None:
                conn.close()
    return dict(sorted(discovered.items())), sorted(dict.fromkeys(issues))


def discover_auxiliary_thread_databases(
    codex_home: Path,
) -> dict[str, tuple[str, str, int]]:
    discovered, _issues = discover_auxiliary_thread_database_inventory(codex_home)
    return discovered


def auxiliary_thread_schema_compatibility(
    conn: sqlite3.Connection | None,
    table: str,
    column: str,
    candidate_ids: Iterable[str] = (),
) -> dict[str, Any]:
    assessment = runtime_schema_compatibility(
        conn,
        {table},
        {(table, column)},
        candidate_ids,
    )
    user_version = (
        int(conn.execute("PRAGMA user_version").fetchone()[0])
        if conn is not None
        else None
    )
    assessment["user_version"] = user_version
    known_role = THREAD_AUXILIARY_TABLE_ROLES.get(table)
    known_max_user_version = int(known_role[2]) if known_role is not None else 0
    assessment["newer_user_version_accepted"] = bool(
        isinstance(user_version, int)
        and user_version > known_max_user_version
    )
    return assessment


def auxiliary_thread_anchor_issues(
    conn: sqlite3.Connection | None,
    filename: str,
    table: str,
    column: str,
) -> list[str]:
    if conn is None:
        return [f"Auxiliary thread database disappeared: {filename}."]
    if not table_exists(conn, table):
        return [
            f"Auxiliary thread database {filename} is missing required table {table}."
        ]
    issues: list[str] = []
    required_columns = set(THREAD_AUXILIARY_PRIMARY_KEYS.get(table, [])) | {column}
    missing_columns = sorted(required_columns - columns(conn, table))
    if missing_columns:
        issues.append(
            f"Auxiliary thread database {filename}:{table} is missing mutation "
            "anchor column(s): " + ", ".join(missing_columns) + "."
        )
    expected_pk = THREAD_AUXILIARY_PRIMARY_KEYS.get(table, [])
    actual_pk = primary_key_columns(conn, table)
    if actual_pk != expected_pk:
        issues.append(
            f"Auxiliary thread database {filename}:{table} has unsupported primary "
            f"key {actual_pk}; expected {expected_pk}."
        )
    for _object_type, name, trigger_table, sql in conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE type='trigger' ORDER BY name"
    ):
        normalized_sql = re.sub(r"\s+", " ", str(sql or "").strip()).lower()
        if str(trigger_table) == table or re.search(
            rf"\b{re.escape(table.lower())}\b", normalized_sql
        ):
            issues.append(
                "Unsupported auxiliary trigger can affect known thread storage: "
                f"{filename}:{name}."
            )
    for candidate_table in sorted(user_tables(conn)):
        for fk in conn.execute(
            f"PRAGMA foreign_key_list({quote_ident(candidate_table)})"
        ):
            if candidate_table == table or str(fk[2]) == table:
                issues.append(
                    "Unsupported auxiliary foreign key can affect known thread storage: "
                    f"{filename}:{candidate_table}.{fk[3]} -> {fk[2]}.{fk[4]}."
                )
    issues.extend(reference_format_issues(conn, [(table, column)], filename))
    return sorted(dict.fromkeys(issues))


def auxiliary_thread_runtime_issues(
    conn: sqlite3.Connection | None,
    filename: str,
    table: str,
    column: str,
    candidate_ids: Iterable[str],
) -> tuple[list[str], dict[str, Any]]:
    compatibility = auxiliary_thread_schema_compatibility(
        conn,
        table,
        column,
        candidate_ids,
    )
    issues = auxiliary_thread_anchor_issues(conn, filename, table, column)
    issues.extend(
        runtime_schema_target_issues(
            f"auxiliary thread database {filename}",
            compatibility,
        )
    )
    return sorted(dict.fromkeys(issues)), compatibility


def auxiliary_thread_row_contracts(
    conn: sqlite3.Connection | None,
    table: str,
    column: str,
    target_ids: list[str],
) -> list[dict[str, Any]]:
    if conn is None or not target_ids or not table_exists(conn, table):
        return []
    column_names = ordered_columns(conn, table)
    primary_key = THREAD_AUXILIARY_PRIMARY_KEYS.get(table, [])
    positions = {name: column_names.index(name) for name in column_names}
    rows = conn.execute(
        f"SELECT * FROM {quote_ident(table)} "
        f"WHERE lower(CAST({quote_ident(column)} AS TEXT)) "
        f"IN ({placeholders(target_ids)})",
        target_ids,
    )
    contracts: list[dict[str, Any]] = []
    for row in rows:
        contract = {name: row[positions[name]] for name in primary_key}
        contract["row_sha256"] = sqlite_row_sha256(column_names, row)
        contracts.append(contract)
    return sorted(
        contracts,
        key=lambda item: tuple(str(item[name]) for name in primary_key),
    )


def auxiliary_thread_database_assessment(
    codex_home: Path,
    target_ids: list[str],
    check_kind: str = "quick_check",
) -> dict[str, Any]:
    counts: dict[str, int] = {}
    contracts: dict[str, dict[str, Any]] = {}
    issues: list[str] = []
    checks: dict[str, str] = {}
    database_plans: dict[str, dict[str, Any]] = {}
    sqlite_root = codex_home / "sqlite"
    discovered, discovery_issues = discover_auxiliary_thread_database_inventory(
        codex_home
    )
    issues.extend(discovery_issues)
    for filename, (table, column, max_user_version) in discovered.items():
        path = sqlite_root / filename
        path_issues = managed_sqlite_issues(codex_home, path)
        if path_issues:
            checks[filename] = "unsafe"
            reasons = sorted(dict.fromkeys(path_issues))
            issues.extend(reasons)
            database_plans[filename] = {
                "status": "skipped",
                "reasons": reasons,
                "table": table,
                "thread_column": column,
                "compatibility": {"available": False},
                "preserved_contract": {
                    "path_identities": nofollow_path_identities(
                        sqlite_family_paths(path)
                    )
                },
            }
            continue
        checks[filename] = database_check(path, check_kind)
        if checks[filename] != "ok":
            reason = (
                f"Auxiliary thread database {check_kind} failed for {filename}: "
                f"{checks[filename]}"
            )
            issues.append(reason)
            database_plans[filename] = {
                "status": "skipped",
                "reasons": [reason],
                "table": table,
                "thread_column": column,
                "compatibility": {"available": False},
                "preserved_contract": {
                    "path_identities": nofollow_path_identities(
                        sqlite_family_paths(path)
                    )
                },
            }
            continue
        conn = connect_ro(path)
        try:
            schema_issues, compatibility = auxiliary_thread_runtime_issues(
                conn,
                filename,
                table,
                column,
                target_ids,
            )
            issues.extend(schema_issues)
            rows = auxiliary_thread_row_contracts(
                conn,
                table,
                column,
                target_ids,
            ) if not auxiliary_thread_anchor_issues(
                conn, filename, table, column
            ) else []
            contract = {
                "table": table,
                "thread_column": column,
                "max_user_version": max_user_version,
                "user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
                "schema_signature": auxiliary_thread_schema_signature(conn),
                "primary_key": THREAD_AUXILIARY_PRIMARY_KEYS[table],
                "rows": rows,
            }
            database_plans[filename] = {
                "status": "skipped" if schema_issues else "enabled",
                "reasons": schema_issues,
                "table": table,
                "thread_column": column,
                "compatibility": compatibility,
                "preserved_contract": contract,
            }
            if not schema_issues:
                counts[filename] = len(rows)
                contracts[filename] = contract
        finally:
            if conn is not None:
                conn.close()
    return {
        "counts": counts,
        "contracts": contracts,
        "issues": sorted(dict.fromkeys(issues)),
        "checks": checks,
        "presence": sorted(discovered),
        "database_plans": database_plans,
    }


def auxiliary_thread_database_snapshot(
    codex_home: Path,
    target_ids: list[str],
    check_kind: str = "quick_check",
) -> tuple[
    dict[str, int],
    dict[str, dict[str, Any]],
    list[str],
    dict[str, str],
    list[str],
]:
    assessment = auxiliary_thread_database_assessment(
        codex_home,
        target_ids,
        check_kind,
    )
    return (
        assessment["counts"],
        assessment["contracts"],
        assessment["issues"],
        assessment["checks"],
        assessment["presence"],
    )


def auxiliary_thread_reference_counts(
    codex_home: Path,
    target_ids: list[str],
    check_kind: str = "quick_check",
) -> tuple[dict[str, int], list[str], dict[str, str], list[str]]:
    counts, _contracts, issues, checks, presence = auxiliary_thread_database_snapshot(
        codex_home, target_ids, check_kind
    )
    return counts, issues, checks, presence


def paginated_history_schema_compatibility(
    conn: sqlite3.Connection | None,
    candidate_ids: Iterable[str] = (),
) -> dict[str, Any]:
    assessment = runtime_schema_compatibility(
        conn,
        PAGINATED_HISTORY_KNOWN_TABLES,
        PAGINATED_HISTORY_THREAD_REFERENCES,
        candidate_ids,
    )
    assessment["user_version"] = (
        int(conn.execute("PRAGMA user_version").fetchone()[0])
        if conn is not None
        else None
    )
    return assessment


def paginated_history_anchor_issues(
    conn: sqlite3.Connection | None,
    database_label: str,
) -> list[str]:
    if conn is None:
        return [f"Paginated history database disappeared: {database_label}."]
    issues: list[str] = []
    for table, required_columns in PAGINATED_HISTORY_REQUIRED_COLUMNS.items():
        if not table_exists(conn, table):
            issues.append(
                f"Paginated history database {database_label} is missing required "
                f"table {table}."
            )
            continue
        missing = sorted(required_columns - columns(conn, table))
        if missing:
            issues.append(
                f"Paginated history table {database_label}:{table} is missing "
                "mutation anchor column(s): " + ", ".join(missing) + "."
            )
        expected_pk = PAGINATED_HISTORY_PRIMARY_KEYS[table]
        actual_pk = primary_key_columns(conn, table)
        if actual_pk != expected_pk:
            issues.append(
                f"Paginated history table {database_label}:{table} has unsupported "
                f"primary key {actual_pk}; expected {expected_pk}."
            )

    issues.extend(
        reference_format_issues(
            conn,
            sorted(PAGINATED_HISTORY_THREAD_REFERENCES),
            database_label,
        )
    )
    return sorted(dict.fromkeys(issues))


def paginated_history_runtime_issues(
    conn: sqlite3.Connection | None,
    database_label: str,
    candidate_ids: Iterable[str],
) -> tuple[list[str], dict[str, Any]]:
    compatibility = paginated_history_schema_compatibility(conn, candidate_ids)
    issues = paginated_history_anchor_issues(conn, database_label)
    effect_assessment = paginated_history_mutation_effect_assessment(
        conn,
        candidate_ids,
        compatibility,
    )
    compatibility["mutation_effect_assessment"] = effect_assessment
    if effect_assessment.get("status") != "target_only":
        issues.extend(
            runtime_schema_target_issues(
                f"paginated history database {database_label}",
                compatibility,
            )
        )
    issues.extend(
        mutation_effect_issues(
            f"paginated history database {database_label}",
            effect_assessment,
        )
    )
    return sorted(dict.fromkeys(issues)), compatibility


def delete_paginated_history_rows_on_conn(
    conn: sqlite3.Connection,
    target_ids: list[str],
) -> dict[str, int]:
    removed: dict[str, int] = {}
    for table in [
        "thread_items",
        "thread_turns",
        "thread_history_projection_state",
    ]:
        cursor = conn.execute(
            f"DELETE FROM {quote_ident(table)} "
            f"WHERE lower(CAST(thread_id AS TEXT)) "
            f"IN ({placeholders(target_ids)})",
            target_ids,
        )
        removed[table] = int(cursor.rowcount)
    return removed


def paginated_history_mutation_effect_assessment(
    conn: sqlite3.Connection | None,
    target_ids: Iterable[str],
    compatibility: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target_ids = list(target_ids)
    compatibility = compatibility or paginated_history_schema_compatibility(
        conn,
        target_ids,
    )
    required_absent_tables = {
        str(hit.get("table", ""))
        for hit in compatibility.get("target_reference_hits", [])
        if hit.get("table")
    }
    return sqlite_mutation_effect_assessment(
        conn,
        target_ids,
        PAGINATED_HISTORY_KNOWN_TABLES,
        PAGINATED_HISTORY_THREAD_REFERENCES,
        set(PAGINATED_HISTORY_PRIMARY_KEYS),
        delete_paginated_history_rows_on_conn,
        required_absent_tables,
    )


def paginated_history_row_contracts(
    conn: sqlite3.Connection | None,
    target_ids: Iterable[str],
) -> dict[str, list[dict[str, Any]]]:
    ids = sorted(
        {
            str(sid).lower()
            for sid in target_ids
            if isinstance(sid, str) and CANONICAL_UUID_RE.fullmatch(sid)
        }
    )
    contracts = {table: [] for table in PAGINATED_HISTORY_PRIMARY_KEYS}
    if conn is None or not ids:
        return contracts
    for table, primary_key in PAGINATED_HISTORY_PRIMARY_KEYS.items():
        if not table_exists(conn, table):
            continue
        column_names = ordered_columns(conn, table)
        positions = {name: column_names.index(name) for name in column_names}
        rows = conn.execute(
            f"SELECT * FROM {quote_ident(table)} "
            f"WHERE lower(CAST(thread_id AS TEXT)) IN ({placeholders(ids)})",
            ids,
        )
        table_contracts: list[dict[str, Any]] = []
        for row in rows:
            contract = {name: row[positions[name]] for name in primary_key}
            contract["row_sha256"] = sqlite_row_sha256(column_names, row)
            table_contracts.append(contract)
        contracts[table] = sorted(
            table_contracts,
            key=lambda item: tuple(str(item[name]) for name in primary_key),
        )
    return contracts


def paginated_history_thread_ids(
    conn: sqlite3.Connection | None,
) -> set[str]:
    ids: set[str] = set()
    if conn is None:
        return ids
    for table in PAGINATED_HISTORY_PRIMARY_KEYS:
        if not table_exists(conn, table) or "thread_id" not in columns(conn, table):
            continue
        ids.update(
            str(row[0]).lower()
            for row in conn.execute(
                f"SELECT DISTINCT thread_id FROM {quote_ident(table)} "
                "WHERE thread_id IS NOT NULL AND thread_id != ''"
            )
            if CANONICAL_UUID_RE.fullmatch(str(row[0]))
        )
    return ids


def paginated_history_database_assessment(
    codex_home: Path,
    target_ids: Iterable[str],
    check_kind: str = "quick_check",
) -> dict[str, Any]:
    discovery = discover_paginated_history_database(codex_home)
    path = discovered_database_path(discovery)
    base: dict[str, Any] = {
        "path": str(path) if path is not None else "",
        "discovery": discovery,
        "check": "missing",
        "counts": {table: 0 for table in PAGINATED_HISTORY_PRIMARY_KEYS},
        "contract": {},
        "database_plan": {
            "status": "not_present" if path is None and not discovery.get("issues") else "skipped",
            "reasons": list(discovery.get("issues", [])),
            "compatibility": {"available": False},
            "preserved_contract": {
                "path_identities": nofollow_path_identities(
                    [
                        Path(str(item.get("path")))
                        for item in discovery.get("candidates", [])
                        if item.get("path")
                    ]
                )
            },
        },
    }
    if path is None:
        return base
    path_issues = managed_sqlite_issues(codex_home, path)
    if path_issues:
        base["database_plan"]["reasons"] = path_issues
        return base
    check = database_check(path, check_kind)
    base["check"] = check
    if check != "ok":
        reason = f"Paginated history database {check_kind} failed for {path.name}: {check}"
        base["database_plan"]["reasons"] = [reason]
        return base
    conn = connect_ro(path)
    try:
        issues, compatibility = paginated_history_runtime_issues(
            conn,
            path.name,
            target_ids,
        )
        rows = (
            paginated_history_row_contracts(conn, target_ids)
            if not paginated_history_anchor_issues(conn, path.name)
            else {table: [] for table in PAGINATED_HISTORY_PRIMARY_KEYS}
        )
        contract = {
            "path": str(path),
            "schema_signature": sqlite_schema_signature(conn, include_indexes=True),
            "primary_keys": PAGINATED_HISTORY_PRIMARY_KEYS,
            "rows": rows,
            "mutation_effect_assessment": compatibility.get(
                "mutation_effect_assessment", {}
            ),
        }
        base["counts"] = {table: len(entries) for table, entries in rows.items()}
        base["contract"] = contract if not issues else {}
        base["database_plan"] = {
            "status": "skipped" if issues else "enabled",
            "reasons": issues,
            "compatibility": compatibility,
            "preserved_contract": {
                **contract,
                "database_identity": stable_sqlite_database_identity(path),
            },
        }
        return base
    finally:
        if conn is not None:
            conn.close()


def filtered_paginated_history_contract(
    contract: dict[str, Any],
    target_ids: set[str],
) -> dict[str, Any]:
    if not contract:
        return {}
    filtered = json.loads(json.dumps(contract, ensure_ascii=False))
    rows = filtered.get("rows", {})
    if isinstance(rows, dict):
        filtered["rows"] = {
            table: [
                row
                for row in entries
                if str(row.get("thread_id", "")).lower() in target_ids
            ]
            for table, entries in rows.items()
            if isinstance(entries, list)
        }
    return filtered


def paginated_prewrite_comparison_contract(value: Any) -> Any:
    """Remove runtime scan counters that do not define deletion authority."""

    if isinstance(value, dict):
        return {
            key: paginated_prewrite_comparison_contract(item)
            for key, item in value.items()
            if key != "scanned_bytes"
        }
    if isinstance(value, list):
        return [paginated_prewrite_comparison_contract(item) for item in value]
    return value


def json_value_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def json_path_value(root: Any, path: Iterable[str]) -> Any:
    current = root
    for component in path:
        if not isinstance(current, dict) or component not in current:
            return None
        current = current[component]
    return current


def json_path_label(path: Iterable[Any]) -> str:
    return "/".join(str(component) for component in path)


def global_state_snapshot(
    data: dict[str, Any],
    target_ids: list[str],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[str],
    list[str],
]:
    refs: list[dict[str, Any]] = []
    mentions: list[dict[str, Any]] = []
    issues: list[str] = []
    warnings: list[str] = []
    owned_prefixes: list[tuple[Any, ...]] = []
    recognized_scalar_paths: set[tuple[Any, ...]] = set()
    target_set = set(target_ids)

    for container_path in GLOBAL_STATE_MAP_CONTAINERS:
        container = json_path_value(data, container_path)
        if container is None:
            continue
        if not isinstance(container, dict):
            warnings.append(
                "Global state thread map has an unsupported type at "
                + json_path_label(container_path)
            )
            continue
        for sid in target_ids:
            if sid not in container:
                continue
            value = container[sid]
            refs.append(
                {
                    "kind": "map_key",
                    "container_path": list(container_path),
                    "key": sid,
                    "value_sha256": json_value_sha256(value),
                }
            )
            owned_prefixes.append((*container_path, sid))

    atom_path = ("electron-persisted-atom-state",)
    atom = json_path_value(data, atom_path)
    if atom is not None and not isinstance(atom, dict):
        warnings.append("Global state electron-persisted-atom-state is not an object")
        atom = None
    if isinstance(atom, dict):
        for sid in target_ids:
            dynamic_keys = [
                prefix + sid for prefix in GLOBAL_STATE_DYNAMIC_KEY_PREFIXES
            ] + [prefix + sid for prefix in GLOBAL_STATE_DYNAMIC_KEY_ENCODED_PREFIXES]
            for key in dynamic_keys:
                if key not in atom:
                    continue
                refs.append(
                    {
                        "kind": "map_key",
                        "container_path": list(atom_path),
                        "key": key,
                        "value_sha256": json_value_sha256(atom[key]),
                    }
                )
                owned_prefixes.append((*atom_path, key))

    for container_path in GLOBAL_STATE_ARRAY_CONTAINERS:
        container = json_path_value(data, container_path)
        if container is None:
            continue
        if not isinstance(container, list):
            warnings.append(
                "Global state thread list has an unsupported type at "
                + json_path_label(container_path)
            )
            continue
        for sid in target_ids:
            indexes = [index for index, value in enumerate(container) if value == sid]
            if not indexes:
                continue
            refs.append(
                {
                    "kind": "array_value",
                    "container_path": list(container_path),
                    "value": sid,
                    "count": len(indexes),
                }
            )
            recognized_scalar_paths.update(
                (*container_path, index) for index in indexes
            )

    unread_path = ("electron-persisted-atom-state", "unread-thread-ids-by-host-v1")
    unread_by_host = json_path_value(data, unread_path)
    if unread_by_host is not None:
        if not isinstance(unread_by_host, dict):
            warnings.append(
                "Global state unread-thread-ids-by-host-v1 is not an object"
            )
        else:
            for host_id, values in sorted(unread_by_host.items()):
                if not isinstance(values, list):
                    warnings.append(
                        "Global state unread thread list is not an array at "
                        + json_path_label((*unread_path, host_id))
                    )
                    continue
                for sid in target_ids:
                    indexes = [
                        index for index, value in enumerate(values) if value == sid
                    ]
                    if not indexes:
                        continue
                    container_path = (*unread_path, host_id)
                    refs.append(
                        {
                            "kind": "array_value",
                            "container_path": list(container_path),
                            "value": sid,
                            "count": len(indexes),
                        }
                    )
                    recognized_scalar_paths.update(
                        (*container_path, index) for index in indexes
                    )

    for container_path in GLOBAL_STATE_SCALAR_CONTAINERS:
        value = json_path_value(data, container_path)
        for sid in target_ids:
            if isinstance(value, str) and value.lower() == sid:
                if value != sid:
                    warnings.append(
                        "Global state realtime voice selector uses a non-canonical "
                        "session ID; the exact scalar will still be removed at "
                        + json_path_label(container_path)
                    )
                refs.append(
                    {
                        "kind": "scalar_value",
                        "container_path": list(container_path),
                        "value": value,
                    }
                )
                recognized_scalar_paths.add(tuple(container_path))
                continue
            conversation_id = (
                value.get("conversationId") if isinstance(value, dict) else None
            )
            if not isinstance(conversation_id, str) or conversation_id.lower() != sid:
                continue
            selector_valid = (
                set(value) == GLOBAL_STATE_VOICE_SELECTOR_KEYS
                and CANONICAL_UUID_RE.fullmatch(conversation_id) is not None
                and conversation_id == sid
                and isinstance(value.get("hostId"), str)
                and bool(value["hostId"])
                and type(value.get("version")) is int
                and 0 <= value["version"] <= SQLITE_MAX_INTEGER
            )
            if not selector_valid:
                warnings.append(
                    "Global state realtime voice selector has a nonstandard shape; "
                    "the whole selector will still be cleared at "
                    + json_path_label(container_path)
                )
            refs.append(
                {
                    "kind": "voice_selector",
                    "container_path": list(container_path),
                    "conversation_id": conversation_id,
                    "value_sha256": json_value_sha256(value),
                }
            )
            owned_prefixes.append(tuple(container_path))

    def path_is_owned(path: tuple[Any, ...]) -> bool:
        return any(path[: len(prefix)] == prefix for prefix in owned_prefixes)

    prompt_history_prefix = (
        "electron-persisted-atom-state",
        "prompt-history",
    )

    def path_is_prompt_history(path: tuple[Any, ...]) -> bool:
        return len(path) >= 2 and path[:2] == prompt_history_prefix

    def is_uuid_map(value: dict[Any, Any]) -> bool:
        candidates = [item for item in value.values() if item is not None]
        return bool(candidates) and all(
            isinstance(item, str)
            and CANONICAL_UUID_RE.fullmatch(item.lower()) is not None
            for item in candidates
        )

    def is_uuid_list(value: list[Any]) -> bool:
        return bool(value) and all(
            isinstance(item, str)
            and CANONICAL_UUID_RE.fullmatch(item.lower()) is not None
            for item in value
        )

    def walk(value: Any, path: tuple[Any, ...]) -> None:
        if path_is_owned(path):
            return
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = (*path, key)
                if path_is_owned(child_path):
                    continue
                if path_is_prompt_history(child_path):
                    walk(child, child_path)
                    continue
                if isinstance(key, str) and any(
                    sid in key.lower() for sid in target_ids
                ):
                    refs.append(
                        {
                            "kind": "map_key",
                            "container_path": list(path),
                            "key": key,
                            "value_sha256": json_value_sha256(child),
                            "discovered": "exact_target_id_in_key",
                        }
                    )
                    owned_prefixes.append(child_path)
                    continue
                if (
                    isinstance(child, str)
                    and child.lower() in target_set
                    and is_uuid_map(value)
                ):
                    refs.append(
                        {
                            "kind": "map_key",
                            "container_path": list(path),
                            "key": str(key),
                            "target_session_id": child.lower(),
                            "value_sha256": json_value_sha256(child),
                            "discovered": "exact_uuid_map_value",
                        }
                    )
                    owned_prefixes.append(child_path)
                    continue
                if isinstance(child, str) and child.lower() in target_set:
                    warnings.append(
                        "Unrecognized global state object contains an exact target "
                        "session ID and was preserved at " + json_path_label(child_path)
                    )
                    continue
                walk(child, child_path)
            return
        if isinstance(value, list):
            if is_uuid_list(value):
                matching_values = sorted(
                    {
                        item
                        for item in value
                        if isinstance(item, str) and item.lower() in target_set
                    }
                )
                for matched in matching_values:
                    indexes = [
                        index for index, item in enumerate(value) if item == matched
                    ]
                    if any(
                        (*path, index) in recognized_scalar_paths for index in indexes
                    ):
                        continue
                    refs.append(
                        {
                            "kind": "array_value",
                            "container_path": list(path),
                            "value": matched,
                            "count": len(indexes),
                            "discovered": "exact_uuid_list_value",
                        }
                    )
                    recognized_scalar_paths.update((*path, index) for index in indexes)
            for index, child in enumerate(value):
                walk(child, (*path, index))
            return
        if not isinstance(value, str) or not any(
            sid in value.lower() for sid in target_ids
        ):
            return
        if path in recognized_scalar_paths:
            return
        if path_is_prompt_history(path):
            mentions.append(
                {
                    "path": list(path),
                    "match": ("exact" if value.lower() in target_set else "contains"),
                }
            )
            return
        warnings.append(
            "Unrecognized global state value contains a target session ID and was "
            "preserved at " + json_path_label(path)
        )

    walk(data, ())
    refs.sort(key=lambda item: json.dumps(item, sort_keys=True))
    mentions.sort(key=lambda item: json.dumps(item, sort_keys=True))
    return (
        refs,
        mentions,
        sorted(dict.fromkeys(issues)),
        sorted(dict.fromkeys(warnings)),
    )


def load_global_state_file(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("global state root is not a JSON object")
    return data, raw


def inspect_global_state_files(
    codex_home: Path,
    target_ids: list[str],
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, bool],
    dict[str, list[dict[str, Any]]],
    list[str],
    list[str],
]:
    refs_by_file: dict[str, list[dict[str, Any]]] = {}
    presence: dict[str, bool] = {}
    mentions_by_file: dict[str, list[dict[str, Any]]] = {}
    issues: list[str] = []
    warnings: list[str] = []
    for filename in GLOBAL_STATE_FILENAMES:
        path = codex_home / filename
        present = path_is_present(path)
        presence[filename] = present
        refs_by_file[filename] = []
        mentions_by_file[filename] = []
        if not present:
            continue
        path_issue = managed_file_issue(codex_home, path)
        if path_issue is not None:
            issues.append(path_issue)
            continue
        try:
            data, _raw = load_global_state_file(path)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            issues.append(f"Unable to parse managed global state file {path}: {exc}")
            continue
        refs, mentions, file_issues, file_warnings = global_state_snapshot(
            data, target_ids
        )
        refs_by_file[filename] = refs
        mentions_by_file[filename] = mentions
        issues.extend(f"{filename}: {issue}" for issue in file_issues)
        warnings.extend(f"{filename}: {warning}" for warning in file_warnings)
    return (
        refs_by_file,
        presence,
        mentions_by_file,
        sorted(dict.fromkeys(issues)),
        sorted(dict.fromkeys(warnings)),
    )


def authoritative_state_available(state: sqlite3.Connection | None) -> bool:
    schema_issues = state_schema_issues(state)
    owning_schema_issues = [
        issue
        for issue in schema_issues
        if "incomplete last-checked thread cursor" not in issue
    ]
    return (
        state is not None
        and table_exists(state, "threads")
        and "id" in columns(state, "threads")
        and not owning_schema_issues
    )


def edge_status_is_open(status: str) -> bool:
    return status.lower() not in CLOSED_EDGE_STATUSES


def incoming_edge_rows(
    state: sqlite3.Connection | None,
    target_ids: Iterable[str],
) -> dict[str, list[dict[str, str]]]:
    ids = list(target_ids)
    if state is None or not ids or not table_exists(state, "thread_spawn_edges"):
        return {}
    edge_cols = columns(state, "thread_spawn_edges")
    if not {"parent_thread_id", "child_thread_id", "status"}.issubset(edge_cols):
        return {}
    sql = (
        "SELECT parent_thread_id, child_thread_id, status FROM thread_spawn_edges "
        f"WHERE child_thread_id IN ({placeholders(ids)}) "
        "ORDER BY child_thread_id, parent_thread_id, status"
    )
    result: dict[str, list[dict[str, str]]] = {}
    for parent_id, child_id, status in state.execute(sql, ids):
        child = str(child_id).lower()
        result.setdefault(child, []).append(
            {
                "parent_thread_id": str(parent_id).lower(),
                "status": str(status or ""),
            }
        )
    return result


def touching_edge_rows(
    state: sqlite3.Connection | None,
    target_ids: Iterable[str],
) -> list[dict[str, str]]:
    ids = list(dict.fromkeys(target_ids))
    if state is None or not ids or not table_exists(state, "thread_spawn_edges"):
        return []
    edge_cols = columns(state, "thread_spawn_edges")
    if not {"parent_thread_id", "child_thread_id", "status"}.issubset(edge_cols):
        return []
    sql = (
        "SELECT parent_thread_id, child_thread_id, status FROM thread_spawn_edges "
        f"WHERE parent_thread_id IN ({placeholders(ids)}) "
        f"OR child_thread_id IN ({placeholders(ids)}) "
        "ORDER BY parent_thread_id, child_thread_id, status"
    )
    return [
        {
            "parent_thread_id": str(parent_id).lower(),
            "child_thread_id": str(child_id).lower(),
            "status": str(status or ""),
        }
        for parent_id, child_id, status in state.execute(sql, ids + ids)
    ]


def summarized_edge_status(edges: list[dict[str, str]]) -> str:
    statuses = [edge.get("status", "") for edge in edges]
    open_statuses = [status for status in statuses if edge_status_is_open(status)]
    if open_statuses:
        return open_statuses[0]
    return statuses[0] if statuses else ""


def resolve_targets(
    state: sqlite3.Connection | None, root_ids: list[str], include_subagents: bool
) -> tuple[list[str], dict[str, str], list[str]]:
    target_ids: list[str] = []
    seen: set[str] = set()
    edge_status: dict[str, str] = {}
    graph_issues: list[str] = []
    queue = list(root_ids)

    while queue:
        sid = queue.pop(0)
        if sid in seen:
            continue
        seen.add(sid)
        target_ids.append(sid)
        if (
            not include_subagents
            or state is None
            or not table_exists(state, "thread_spawn_edges")
        ):
            continue
        edge_cols = columns(state, "thread_spawn_edges")
        if not {"parent_thread_id", "child_thread_id", "status"}.issubset(edge_cols):
            continue
        for child_id, status in state.execute(
            "SELECT child_thread_id, status FROM thread_spawn_edges "
            "WHERE parent_thread_id=? ORDER BY child_thread_id",
            (sid,),
        ):
            raw_child = str(child_id)
            if not CANONICAL_UUID_RE.fullmatch(raw_child):
                graph_issues.append(
                    "Invalid child session id in thread_spawn_edges; recursive "
                    f"expansion was skipped: {raw_child}"
                )
                # A malformed graph edge makes the recursive closure untrustworthy.
                # Keep exact user roots independently actionable, but do not retain a
                # partially traversed child set that could imply complete coverage.
                target_ids = list(dict.fromkeys(root_ids))
                edge_status = {}
                queue = []
                break
            child = raw_child.lower()
            new_status = str(status or "")
            current_status = edge_status.get(child)
            if current_status is None or (
                not edge_status_is_open(current_status)
                and edge_status_is_open(new_status)
            ):
                edge_status[child] = new_status
            if child not in seen:
                queue.append(child)

    for child, edges in incoming_edge_rows(state, target_ids).items():
        edge_status[child] = summarized_edge_status(edges)
    return target_ids, edge_status, sorted(dict.fromkeys(graph_issues))


def load_threads(
    state: sqlite3.Connection | None, target_ids: list[str], edge_status: dict[str, str]
) -> dict[str, ThreadInfo]:
    threads = {
        sid: ThreadInfo(id=sid, edge_status=edge_status.get(sid, ""))
        for sid in target_ids
    }
    if state is None or not target_ids or not table_exists(state, "threads"):
        return threads

    cols = columns(state, "threads")
    if "id" not in cols:
        return threads
    select_cols = [
        "id",
        "title" if "title" in cols else "'' AS title",
        "rollout_path" if "rollout_path" in cols else "'' AS rollout_path",
        "agent_nickname" if "agent_nickname" in cols else "'' AS agent_nickname",
        "agent_role" if "agent_role" in cols else "'' AS agent_role",
        "created_at_ms" if "created_at_ms" in cols else "NULL AS created_at_ms",
        "updated_at_ms" if "updated_at_ms" in cols else "NULL AS updated_at_ms",
        "history_mode" if "history_mode" in cols else "'legacy' AS history_mode",
    ]
    sql = (
        f"SELECT {', '.join(select_cols)} FROM threads "
        f"WHERE id IN ({placeholders(target_ids)})"
    )
    for row in state.execute(sql, target_ids):
        sid = row[0]
        threads[sid] = ThreadInfo(
            id=sid,
            title=row[1] or "",
            rollout_path=row[2] or "",
            agent_nickname=row[3] or "",
            agent_role=row[4] or "",
            created_at_ms=row[5],
            updated_at_ms=row[6],
            edge_status=edge_status.get(sid, ""),
            history_mode=str(row[7] or ""),
        )
    return threads


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def path_is_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def managed_root_issue(codex_home: Path, root: Path) -> str | None:
    if root.is_symlink():
        return f"Managed directory must not be a symbolic link: {root}"
    if root.exists() and not root.is_dir():
        return f"Managed directory path is not a directory: {root}"
    if root.exists() and not is_relative_to(root, codex_home):
        return f"Managed directory resolves outside the selected Codex home: {root}"
    return None


def managed_file_issue(codex_home: Path, path: Path) -> str | None:
    if path.is_symlink():
        return f"Managed state file must not be a symbolic link: {path}"
    if path.exists() and not path.is_file():
        return f"Managed state path is not a regular file: {path}"
    if path.exists() and not is_relative_to(path, codex_home):
        return f"Managed state file resolves outside the selected Codex home: {path}"
    if path.exists() and not path.is_symlink() and path.stat().st_nlink != 1:
        return f"Managed state file must not have multiple hard links: {path}"
    return None


def sqlite_family_paths(path: Path) -> list[Path]:
    return [path, Path(f"{path}-wal"), Path(f"{path}-shm"), Path(f"{path}-journal")]


def managed_sqlite_issues(codex_home: Path, path: Path) -> list[str]:
    issues = [
        issue
        for candidate in sqlite_family_paths(path)
        if (issue := managed_file_issue(codex_home, candidate)) is not None
    ]
    for candidate in sqlite_family_paths(path):
        if candidate.exists() and not candidate.is_symlink():
            if candidate.stat().st_nlink != 1:
                issues.append(
                    f"Managed SQLite file must not have multiple hard links: {candidate}"
                )
    return issues


def global_state_recovery_paths(codex_home: Path) -> list[Path]:
    return sorted(codex_home.glob(".*.delete-session-*")) if codex_home.is_dir() else []


def storage_boundary_findings(
    codex_home: Path,
    include_logs: bool,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []

    def add(component: str, values: Iterable[str]) -> None:
        findings.extend(
            {"component": component, "reason": str(value)} for value in values
        )

    state_discovery = discover_state_database(codex_home)
    logs_discovery = discover_logs_database(codex_home)
    state_path = discovered_database_path(state_discovery)
    logs_path = discovered_database_path(logs_discovery)
    add(COMPONENT_CORE, state_discovery.get("issues", []))
    if state_path is not None:
        add(COMPONENT_CORE, managed_sqlite_issues(codex_home, state_path))
    if include_logs:
        add(COMPONENT_LOGS, logs_discovery.get("issues", []))
        if logs_path is not None:
            add(COMPONENT_LOGS, managed_sqlite_issues(codex_home, logs_path))
    index_issue = managed_file_issue(codex_home, codex_home / "session_index.jsonl")
    if index_issue is not None:
        add(COMPONENT_CORE, [index_issue])
    sqlite_root_issue = managed_root_issue(codex_home, codex_home / "sqlite")
    if sqlite_root_issue is not None:
        add(COMPONENT_CORE, [sqlite_root_issue])
    catalog_path, catalog_discovery_issues = discover_desktop_catalog_path(codex_home)
    add(COMPONENT_CATALOG, catalog_discovery_issues)
    if catalog_path is not None:
        add(COMPONENT_CATALOG, managed_sqlite_issues(codex_home, catalog_path))
    auxiliary_discovered, auxiliary_discovery_issues = (
        discover_auxiliary_thread_database_inventory(codex_home)
    )
    add(COMPONENT_AUXILIARY, auxiliary_discovery_issues)
    for filename in auxiliary_discovered:
        add(
            COMPONENT_AUXILIARY,
            managed_sqlite_issues(codex_home, codex_home / "sqlite" / filename)
        )
    for filename in GLOBAL_STATE_FILENAMES:
        global_state_path = codex_home / filename
        if path_is_present(global_state_path):
            issue = managed_file_issue(codex_home, global_state_path)
            if issue is not None:
                add(COMPONENT_GLOBAL_STATE, [issue])
    recovery_paths = global_state_recovery_paths(codex_home)
    if recovery_paths:
        add(
            COMPONENT_GLOBAL_STATE,
            [
                "Unresolved global-state recovery file(s) require inspection before "
                "another deletion: " + ", ".join(str(path) for path in recovery_paths)
            ],
        )
    managed_roots = {
        "sessions": COMPONENT_ROLLOUTS,
        "shell_snapshots": COMPONENT_SNAPSHOTS,
        "generated_images": COMPONENT_GENERATED,
    }
    for name, component in managed_roots.items():
        root_issue = managed_root_issue(codex_home, codex_home / name)
        if root_issue is not None:
            add(component, [root_issue])
    paginated_discovery = discover_paginated_history_database(codex_home)
    add(COMPONENT_PAGINATED_HISTORY, paginated_discovery.get("issues", []))
    paginated_path = discovered_database_path(paginated_discovery)
    if paginated_path is not None:
        add(
            COMPONENT_PAGINATED_HISTORY,
            managed_sqlite_issues(codex_home, paginated_path),
        )
    unique = {
        (entry["component"], entry["reason"]): entry for entry in findings
    }
    return [unique[key] for key in sorted(unique)]


def storage_boundary_issues(codex_home: Path, include_logs: bool) -> list[str]:
    return [
        finding["reason"]
        for finding in storage_boundary_findings(codex_home, include_logs)
    ]


def desktop_owner_processes(
    codex_home: Path,
) -> tuple[list[dict[str, Any]], str]:
    del codex_home
    try:
        completed = subprocess.run(
            ["/bin/ps", "-axo", "pid=,comm="],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"Unable to inspect Codex Desktop owner processes: {exc}"
    owners: list[dict[str, Any]] = []
    for raw_line in completed.stdout.splitlines():
        parts = raw_line.strip().split(maxsplit=1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        executable = parts[1]
        if not any(
            marker in executable
            for marker in [
                "/ChatGPT.app/Contents/",
                "/Codex.app/Contents/",
            ]
        ):
            continue
        owners.append({"pid": int(parts[0]), "executable": executable})
    return owners, ""


def plan_requires_desktop_offline(plan: Plan) -> bool:
    return any(
        plan.counts.get(key, 0) > 0
        for key in [
            "desktop_catalog_rows",
            "desktop_timeline_rows",
            "desktop_automation_run_rows",
            "desktop_auxiliary_thread_rows",
            "paginated_history_projection_rows",
            "paginated_history_turn_rows",
            "paginated_history_item_rows",
            "global_state_structural_refs",
        ]
    )


def require_desktop_offline(plan: Plan) -> None:
    if not plan_requires_desktop_offline(plan):
        return
    owners, issue = desktop_owner_processes(plan.codex_home)
    if issue:
        raise RuntimeError(issue)
    if owners:
        raise RuntimeError(
            "Codex Desktop is still running and owns catalog/global UI state; "
            "fully exit the app, then run the approved apply command from an "
            "independent terminal"
        )


def require_managed_file(codex_home: Path, path: Path) -> None:
    issue = managed_file_issue(codex_home, path)
    if issue is not None:
        raise RuntimeError(issue)


def require_managed_sqlite(codex_home: Path, path: Path) -> None:
    issues = managed_sqlite_issues(codex_home, path)
    if issues:
        raise RuntimeError(" | ".join(issues))


def path_within_root_issue(path: Path, root: Path) -> str | None:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return f"Path is outside its managed root: {path}"
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return f"Symbolic links are not allowed inside managed artifact paths: {current}"
    if not is_relative_to(path, root):
        return f"Path resolves outside its managed root: {path}"
    return None


def record_safe_path(path: Path, root: Path, unsafe: list[str] | None) -> bool:
    issue = path_within_root_issue(path, root)
    if issue is None:
        return True
    if unsafe is not None:
        unsafe.append(issue)
    return False


def safe_scan_root(codex_home: Path, root: Path) -> bool:
    return root.is_dir() and managed_root_issue(codex_home, root) is None


def unique_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(path)
    return out


def safe_existing_file(
    path_text: str,
    allowed_root: Path,
    expected_session_id: str,
    unsafe: list[str],
    ownership_evidence: list[dict[str, Any]] | None = None,
    authoritative_owners_by_path: dict[str, list[str]] | None = None,
) -> Path | None:
    if not path_text:
        return None
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        unsafe.append(f"relative rollout path: {path}")
        return None
    issue = path_within_root_issue(path, allowed_root)
    if issue is not None:
        unsafe.append(f"unsafe rollout path: {issue}")
        return None
    relative = path.relative_to(allowed_root)
    relative_ids = {match.lower() for match in UUID_RE.findall(str(relative))}
    if path.suffix.lower() != ".jsonl":
        unsafe.append(
            "rollout path is not a JSONL session artifact: "
            f"{expected_session_id} -> {path}"
        )
        return None
    owners = set((authoritative_owners_by_path or {}).get(str(path), []))
    if owners and owners != {expected_session_id.lower()}:
        unsafe.append(
            "Managed rollout path has conflicting authoritative state owners: "
            f"{path} -> {', '.join(sorted(owners))}"
        )
        return None
    if ownership_evidence is not None and relative_ids != {expected_session_id.lower()}:
        ownership_evidence.append(
            {
                "path": str(path),
                "owner_session_id": expected_session_id.lower(),
                "filename_session_id_hints": sorted(relative_ids),
                "basis": "authoritative_state_rollout_path",
            }
        )
    if path.exists() and not path.is_file():
        unsafe.append(f"non-file rollout path: {path}")
        return None
    if path.is_file():
        return path
    return None


def collect_files(plan: Plan) -> None:
    sessions_root = plan.codex_home / "sessions"
    shell_root = plan.codex_home / "shell_snapshots"
    generated_root = plan.codex_home / "generated_images"

    rollout_groups = rollout_paths_by_session(
        plan.codex_home,
        plan.unsafe_paths,
        plan.initial_state_thread_ids,
        plan.artifact_ownership_evidence,
    )
    snapshot_groups = shell_snapshot_paths_by_session(
        plan.codex_home,
        plan.unsafe_paths,
        plan.initial_state_thread_ids,
        plan.artifact_ownership_evidence,
    )
    generated_groups = generated_paths_by_session(
        plan.codex_home,
        plan.unsafe_paths,
        plan.initial_state_thread_ids,
        plan.artifact_ownership_evidence,
    )

    rollout_files: list[Path] = paths_for_ids(rollout_groups, set(plan.target_ids))
    for thread in plan.threads.values():
        path = safe_existing_file(
            thread.rollout_path,
            sessions_root,
            thread.id,
            plan.unsafe_paths,
            plan.artifact_ownership_evidence,
            plan.preflight.get("rollout_path_owners", {}),
        )
        if path is not None:
            rollout_files.append(path)

    shell_files = paths_for_ids(snapshot_groups, set(plan.target_ids))
    generated = paths_for_ids(generated_groups, set(plan.target_ids))

    rollout_path_owners = plan.preflight.get("rollout_path_owners", {})
    owned_rollout_files: list[Path] = []
    for path in rollout_files:
        owners = set(rollout_path_owners.get(str(path), []))
        if len(owners) > 1:
            plan.unsafe_paths.append(
                "Managed rollout path has conflicting authoritative state owners: "
                f"{path} -> {', '.join(sorted(owners))}"
            )
            continue
        owned_rollout_files.append(path)

    plan.rollout_files = unique_paths(
        path
        for path in owned_rollout_files
        if path.is_file() and record_safe_path(path, sessions_root, plan.unsafe_paths)
    )
    plan.shell_snapshots = unique_paths(
        path
        for path in shell_files
        if path.is_file() and record_safe_path(path, shell_root, plan.unsafe_paths)
    )
    plan.generated_artifacts = unique_paths(
        path
        for path in generated
        if path_is_present(path)
        and record_safe_path(path, generated_root, plan.unsafe_paths)
    )
    plan.bytes_to_remove = sum(
        path.stat().st_size
        for path in plan.rollout_files + plan.shell_snapshots
        if path.exists()
    )
    for path in plan.generated_artifacts:
        if path.is_file():
            plan.bytes_to_remove += path.stat().st_size
        elif path.is_dir():
            plan.bytes_to_remove += dir_size(path)
    plan.artifact_contracts = {
        str(path): path_contract_entry(path)
        for path in (
            plan.rollout_files + plan.shell_snapshots + plan.generated_artifacts
        )
        if path_is_present(path)
    }
    plan.artifact_ownership_evidence = [
        json.loads(encoded)
        for encoded in sorted(
            {
                json.dumps(item, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                for item in plan.artifact_ownership_evidence
            }
        )
    ]


def dir_size(path: Path) -> int:
    total = 0
    for current, dirnames, filenames in os.walk(path, followlinks=False):
        current_path = Path(current)
        dirnames[:] = [
            name for name in dirnames if not (current_path / name).is_symlink()
        ]
        for name in filenames:
            item = current_path / name
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
    return total


def directory_tree_entries(path: Path) -> list[dict[str, Any]]:
    root_identity = lstat_identity(path)
    entries: list[dict[str, Any]] = []
    for current, dirnames, filenames in os.walk(path, followlinks=False):
        current_path = Path(current)
        dirnames.sort()
        filenames.sort()
        for name in dirnames + filenames:
            item = current_path / name
            info = item.lstat()
            record: dict[str, Any] = {
                "relative_path": str(item.relative_to(path)),
                "device": info.st_dev,
                "inode": info.st_ino,
                "mode": info.st_mode,
                "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
            }
            if item.is_symlink():
                record["symlink_target"] = os.readlink(item)
            elif stat.S_ISREG(info.st_mode):
                record["content_sha256"] = file_content_sha256(item, info)
            entries.append(record)
    if lstat_identity(path) != root_identity:
        raise RuntimeError(f"Artifact directory identity changed while hashing: {path}")
    return entries


def directory_tree_digest(entries: Iterable[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for record in entries:
        digest.update(
            json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def file_content_sha256(
    path: Path,
    expected_info: os.stat_result | None = None,
) -> str:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        if expected_info is not None and stat_identity(before) != stat_identity(
            expected_info
        ):
            raise RuntimeError(f"Artifact file identity changed before hashing: {path}")
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError(f"Artifact is not a regular file: {path}")
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(fd)
        if stat_identity(after) != stat_identity(before):
            raise RuntimeError(f"Artifact file identity changed while hashing: {path}")
        if lstat_identity(path) != stat_identity(before):
            raise RuntimeError(f"Artifact path changed while hashing: {path}")
        return digest.hexdigest()
    finally:
        os.close(fd)


def path_contract_entry(path: Path) -> dict[str, Any]:
    info = path.lstat()
    is_directory = stat.S_ISDIR(info.st_mode)
    is_symlink = stat.S_ISLNK(info.st_mode)
    entry: dict[str, Any] = {
        "path": str(path),
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": info.st_mode,
        "mtime_ns": info.st_mtime_ns,
        "type": "directory" if is_directory else "symlink" if is_symlink else "file",
        "size": dir_size(path) if is_directory else info.st_size,
    }
    if is_directory:
        tree_entries = directory_tree_entries(path)
        entry["tree_entries"] = tree_entries
        entry["tree_digest"] = directory_tree_digest(tree_entries)
    elif is_symlink:
        entry["symlink_target"] = os.readlink(path)
    elif stat.S_ISREG(info.st_mode):
        entry["content_sha256"] = file_content_sha256(path, info)
    return entry


def count_in_table(
    conn: sqlite3.Connection | None, table: str, column: str, ids: list[str]
) -> int:
    if (
        conn is None
        or not ids
        or not table_exists(conn, table)
        or column not in columns(conn, table)
    ):
        return 0
    normalized = [sid.lower() for sid in ids]
    sql = (
        f"SELECT COUNT(*) FROM {quote_ident(table)} "
        f"WHERE lower(CAST({quote_ident(column)} AS TEXT)) "
        f"IN ({placeholders(normalized)})"
    )
    return int(conn.execute(sql, normalized).fetchone()[0])


def count_edges(conn: sqlite3.Connection | None, ids: list[str]) -> int:
    if conn is None or not ids or not table_exists(conn, "thread_spawn_edges"):
        return 0
    if not {"parent_thread_id", "child_thread_id"}.issubset(
        columns(conn, "thread_spawn_edges")
    ):
        return 0
    sql = (
        "SELECT COUNT(*) FROM thread_spawn_edges "
        f"WHERE lower(CAST(parent_thread_id AS TEXT)) IN ({placeholders(ids)}) "
        f"OR lower(CAST(child_thread_id AS TEXT)) IN ({placeholders(ids)})"
    )
    normalized = [sid.lower() for sid in ids]
    return int(conn.execute(sql, normalized + normalized).fetchone()[0])


def count_session_index(index_path: Path, ids: set[str]) -> int:
    if not index_path.exists():
        return 0
    count = 0
    with index_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            sid = parse_index_id(line)
            if sid in ids:
                count += 1
    return count


def parse_index_id(line: str) -> str | None:
    try:
        payload = json.loads(line)
        if not isinstance(payload, dict):
            return None
        sid = payload.get("id")
        return sid.lower() if isinstance(sid, str) else None
    except json.JSONDecodeError:
        return None


def session_index_issues(index_path: Path) -> list[str]:
    if not index_path.exists():
        return []
    issues: list[str] = []
    try:
        with index_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    issues.append(
                        f"session_index.jsonl line {line_number} is not valid JSON: {exc.msg}."
                    )
                    continue
                if isinstance(payload, dict) and "id" in payload:
                    sid = payload["id"]
                    if not isinstance(sid, str) or not CANONICAL_UUID_RE.fullmatch(sid):
                        issues.append(
                            f"session_index.jsonl line {line_number} has an invalid session id."
                        )
    except UnicodeDecodeError as exc:
        issues.append(
            "session_index.jsonl is not valid UTF-8: " f"byte offset {exc.start}."
        )
    return issues


def skipped_historical_scan(
    reason: str = "Historical scan was disabled.",
) -> dict[str, Any]:
    return {
        "scanned": False,
        "authoritative": False,
        "reason": reason,
        "summary": {
            "has_residuals": False,
            "issue_categories": 0,
            "total_ids": 0,
            "total_items": 0,
        },
    }


def has_any_session_evidence(
    codex_home: Path,
    logs: sqlite3.Connection | None,
) -> bool:
    if session_index_row_counts(codex_home / "session_index.jsonl"):
        return True
    for root, pattern, recursive in [
        (codex_home / "sessions", "*.jsonl", True),
        (codex_home / "shell_snapshots", "*.sh", False),
    ]:
        if root.exists():
            iterator = root.rglob(pattern) if recursive else root.glob(pattern)
            if next((path for path in iterator if path.is_file()), None) is not None:
                return True
    generated_root = codex_home / "generated_images"
    if generated_root.exists() and next(generated_root.iterdir(), None) is not None:
        return True
    if (
        logs is not None
        and table_exists(logs, "logs")
        and "thread_id" in columns(logs, "logs")
    ):
        row = logs.execute(
            "SELECT 1 FROM logs WHERE thread_id IS NOT NULL AND thread_id != '' LIMIT 1"
        ).fetchone()
        if row is not None:
            return True
    return False


def session_index_row_counts(index_path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not index_path.exists():
        return counts
    with index_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            sid = parse_index_id(line)
            if sid:
                sid = sid.lower()
                counts[sid] = counts.get(sid, 0) + 1
    return counts


def state_thread_ids(state: sqlite3.Connection | None) -> set[str]:
    if (
        state is None
        or not table_exists(state, "threads")
        or "id" not in columns(state, "threads")
    ):
        return set()
    return {
        str(row[0]).lower() for row in state.execute("SELECT id FROM threads") if row[0]
    }


def reference_ids_at_locations(
    conn: sqlite3.Connection | None,
    locations: Iterable[tuple[str, str]],
) -> set[str]:
    ids: set[str] = set()
    if conn is None:
        return ids
    for table, column in locations:
        if not table_exists(conn, table) or column not in columns(conn, table):
            continue
        for (value,) in conn.execute(
            f"SELECT DISTINCT {quote_ident(column)} FROM {quote_ident(table)} "
            f"WHERE {quote_ident(column)} IS NOT NULL "
            f"AND {quote_ident(column)} != ''"
        ):
            raw = str(value)
            if CANONICAL_UUID_RE.fullmatch(raw):
                ids.add(raw.lower())
    return ids


def recent_logs_only_ids(
    logs: sqlite3.Connection | None,
    known_or_non_log_ids: set[str],
    excluded_ids: set[str],
    *,
    now_epoch_seconds: int | None = None,
) -> set[str]:
    """Protect newly active log-only IDs without freezing them into a plan.

    Codex code-mode workers can emit canonical thread-shaped log IDs that never
    become user sessions.  Treating those rows as historical immediately makes
    every report create a new residual and prevents a frozen plan from staying
    stable long enough to stage.  Old log-only IDs remain ordinary residuals.
    """

    if (
        logs is None
        or not table_exists(logs, "logs")
        or not {"thread_id", "ts"}.issubset(columns(logs, "logs"))
    ):
        return set()
    now_value = int(time.time()) if now_epoch_seconds is None else now_epoch_seconds
    protected: set[str] = set()
    for thread_id, max_ts in logs.execute(
        "SELECT thread_id, MAX(ts) FROM logs "
        "WHERE thread_id IS NOT NULL AND thread_id != '' GROUP BY thread_id"
    ):
        raw = str(thread_id)
        if not CANONICAL_UUID_RE.fullmatch(raw):
            continue
        sid = raw.lower()
        if sid in known_or_non_log_ids or sid in excluded_ids:
            continue
        if not isinstance(max_ts, int):
            continue
        age = now_value - max_ts
        if 0 <= age <= RECENT_LOG_ONLY_PROTECTION_SECONDS:
            protected.add(sid)
    return protected


def state_thread_rollout_rows(
    state: sqlite3.Connection | None,
) -> list[tuple[str, str]]:
    if state is None or not table_exists(state, "threads"):
        return []
    cols = columns(state, "threads")
    if "id" not in cols or "rollout_path" not in cols:
        return []
    return [
        (str(row[0]).lower(), row[1] or "")
        for row in state.execute("SELECT id, rollout_path FROM threads ORDER BY id")
        if row[0]
    ]


def rollout_migration_state_rows(
    state: sqlite3.Connection | None,
) -> list[dict[str, Any]]:
    if (
        state is None
        or not table_exists(state, "rollout_migration_state")
        or not set(ROLLOUT_MIGRATION_STATE_COLUMNS).issubset(
            columns(state, "rollout_migration_state")
        )
    ):
        return []
    select_columns = ", ".join(
        quote_ident(column) for column in ROLLOUT_MIGRATION_STATE_COLUMNS
    )
    return [
        dict(zip(ROLLOUT_MIGRATION_STATE_COLUMNS, row, strict=True))
        for row in state.execute(
            f"SELECT {select_columns} FROM rollout_migration_state "
            "ORDER BY migration_id"
        )
    ]


def rollout_migration_skipped_rows_for_paths(
    state: sqlite3.Connection | None,
    rollout_paths: Iterable[str],
) -> list[dict[str, Any]]:
    if (
        state is None
        or not table_exists(state, "rollout_migration_skipped_rollouts")
        or not set(ROLLOUT_MIGRATION_SKIPPED_COLUMNS).issubset(
            columns(state, "rollout_migration_skipped_rollouts")
        )
    ):
        return []
    paths = sorted({str(path) for path in rollout_paths if path})
    if not paths:
        return []
    select_columns = ", ".join(
        quote_ident(column) for column in ROLLOUT_MIGRATION_SKIPPED_COLUMNS
    )
    rows: list[dict[str, Any]] = []
    for offset in range(0, len(paths), 500):
        batch = paths[offset : offset + 500]
        rows.extend(
            dict(zip(ROLLOUT_MIGRATION_SKIPPED_COLUMNS, row, strict=True))
            for row in state.execute(
                f"SELECT {select_columns} FROM rollout_migration_skipped_rollouts "
                f"WHERE rollout_path IN ({placeholders(batch)}) "
                "ORDER BY migration_id, rollout_path",
                batch,
            )
        )
    return sorted(
        rows,
        key=lambda row: (str(row["migration_id"]), str(row["rollout_path"])),
    )


def rollout_migration_skipped_path_issues(
    state: sqlite3.Connection | None,
    target_ids: set[str],
    target_rollout_paths: set[str],
) -> list[str]:
    if (
        state is None
        or not target_ids
        or not table_exists(state, "rollout_migration_skipped_rollouts")
        or "rollout_path" not in columns(state, "rollout_migration_skipped_rollouts")
    ):
        return []
    issues: list[str] = []
    for (raw_path,) in state.execute(
        "SELECT DISTINCT rollout_path FROM rollout_migration_skipped_rollouts "
        "ORDER BY rollout_path"
    ):
        path = str(raw_path)
        path_ids = {match.lower() for match in UUID_RE.findall(path)}
        if path in target_rollout_paths:
            continue
        if path_ids & target_ids and path not in target_rollout_paths:
            issues.append(
                "rollout_migration_skipped_rollouts contains a target-like path that "
                f"does not exactly match the target thread rollout_path: {path}"
            )
    return issues


def state_rollout_path_issues(
    state: sqlite3.Connection | None,
    codex_home: Path,
) -> list[str]:
    sessions_root = codex_home / "sessions"
    issues: list[str] = []
    rollout_rows = state_thread_rollout_rows(state)
    owners_by_path: dict[str, set[str]] = {}
    for sid, rollout_path in rollout_rows:
        if rollout_path:
            owners_by_path.setdefault(rollout_path, set()).add(sid)
    for rollout_path, owners in sorted(owners_by_path.items()):
        if len(owners) > 1:
            issues.append(
                "Multiple state threads claim the same rollout_path: "
                f"{rollout_path} -> {', '.join(sorted(owners))}"
            )
    for sid, rollout_path in rollout_rows:
        if not rollout_path:
            issues.append(f"State thread {sid} has an empty rollout_path.")
            continue
        path = Path(rollout_path).expanduser()
        if not path.is_absolute():
            issues.append(f"State thread {sid} has a relative rollout_path: {path}")
            continue
        path_issue = path_within_root_issue(path, sessions_root)
        if (
            path_issue is not None
            or path.suffix.lower() != ".jsonl"
            or (path.exists() and not path.is_file())
        ):
            issues.append(
                f"State thread {sid} has an invalid rollout_path: {path}"
            )
    return issues


def logs_thread_counts(logs: sqlite3.Connection | None) -> dict[str, int]:
    if (
        logs is None
        or not table_exists(logs, "logs")
        or "thread_id" not in columns(logs, "logs")
    ):
        return {}
    return {
        str(thread_id).lower(): int(count)
        for thread_id, count in logs.execute(
            "SELECT thread_id, COUNT(*) FROM logs "
            "WHERE thread_id IS NOT NULL AND thread_id != '' GROUP BY thread_id"
        )
    }


def artifact_session_id(
    path: Path,
    root: Path,
    unsafe: list[str] | None,
    authoritative_session_ids: set[str] | None = None,
    ownership_evidence: list[dict[str, Any]] | None = None,
) -> str | None:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return
    session_ids = {match.lower() for match in UUID_RE.findall(str(relative))}
    if len(session_ids) > 1:
        authoritative_matches = session_ids & (authoritative_session_ids or set())
        if len(authoritative_matches) == 1:
            owner = next(iter(authoritative_matches))
            if ownership_evidence is not None:
                ownership_evidence.append(
                    {
                        "path": str(path),
                        "owner_session_id": owner,
                        "filename_session_id_hints": sorted(session_ids),
                        "basis": "unique_authoritative_state_match",
                    }
                )
            return owner
        if unsafe is not None:
            unsafe.append(
                "Managed artifact path contains multiple session IDs and has "
                f"ambiguous ownership: {path}"
            )
        return None
    return next(iter(session_ids), None)


def add_path_for_session(
    groups: dict[str, list[str]],
    path: Path,
    root: Path,
    unsafe: list[str] | None,
    authoritative_session_ids: set[str] | None = None,
    ownership_evidence: list[dict[str, Any]] | None = None,
) -> None:
    sid = artifact_session_id(
        path,
        root,
        unsafe,
        authoritative_session_ids,
        ownership_evidence,
    )
    if sid is not None:
        groups.setdefault(sid, []).append(str(path))


def rollout_paths_by_session(
    codex_home: Path,
    unsafe: list[str] | None = None,
    authoritative_session_ids: set[str] | None = None,
    ownership_evidence: list[dict[str, Any]] | None = None,
) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    root = codex_home / "sessions"
    if not safe_scan_root(codex_home, root):
        return groups
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        safe_dirnames: list[str] = []
        for name in dirnames:
            path = current_path / name
            if record_safe_path(path, root, unsafe):
                safe_dirnames.append(name)
        dirnames[:] = safe_dirnames
        for name in filenames:
            path = current_path / name
            if not record_safe_path(path, root, unsafe):
                continue
            if path.suffix == ".jsonl" and path.is_file():
                add_path_for_session(
                    groups,
                    path,
                    root,
                    unsafe,
                    authoritative_session_ids,
                    ownership_evidence,
                )
    return groups


def shell_snapshot_paths_by_session(
    codex_home: Path,
    unsafe: list[str] | None = None,
    authoritative_session_ids: set[str] | None = None,
    ownership_evidence: list[dict[str, Any]] | None = None,
) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    root = codex_home / "shell_snapshots"
    if not safe_scan_root(codex_home, root):
        return groups
    for path in root.iterdir():
        if not record_safe_path(path, root, unsafe):
            continue
        if path.suffix == ".sh" and path.is_file():
            add_path_for_session(
                groups,
                path,
                root,
                unsafe,
                authoritative_session_ids,
                ownership_evidence,
            )
    return groups


def generated_paths_by_session(
    codex_home: Path,
    unsafe: list[str] | None = None,
    authoritative_session_ids: set[str] | None = None,
    ownership_evidence: list[dict[str, Any]] | None = None,
) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    directory_candidates: list[tuple[Path, str]] = []
    root = codex_home / "generated_images"
    if not safe_scan_root(codex_home, root):
        return groups
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        safe_dirnames: list[str] = []
        for name in dirnames:
            path = current_path / name
            if not record_safe_path(path, root, unsafe):
                continue
            sid = artifact_session_id(
                path,
                root,
                unsafe,
                authoritative_session_ids,
                ownership_evidence,
            )
            if sid:
                directory_candidates.append((path, sid))
            elif UUID_RE.search(str(path.relative_to(root))):
                continue
            safe_dirnames.append(name)
        dirnames[:] = safe_dirnames
        for name in filenames:
            path = current_path / name
            if not record_safe_path(path, root, unsafe):
                continue
            if path.is_file():
                add_path_for_session(
                    groups,
                    path,
                    root,
                    unsafe,
                    authoritative_session_ids,
                    ownership_evidence,
                )

    collapsed_directories: list[Path] = []
    for directory, sid in sorted(
        directory_candidates,
        key=lambda item: len(item[0].relative_to(root).parts),
    ):
        if any(
            directory == collapsed or directory.is_relative_to(collapsed)
            for collapsed in collapsed_directories
        ):
            continue
        subtree_ids: set[str] = set()
        for current, dirnames, filenames in os.walk(directory, followlinks=False):
            current_path = Path(current)
            for item in [current_path] + [
                current_path / name for name in dirnames + filenames
            ]:
                subtree_ids.update(
                    match.lower()
                    for match in UUID_RE.findall(str(item.relative_to(root)))
                )
        if subtree_ids != {sid}:
            continue
        for group_sid, paths in groups.items():
            groups[group_sid] = [
                path for path in paths if not Path(path).is_relative_to(directory)
            ]
        groups.setdefault(sid, []).append(str(directory))
        collapsed_directories.append(directory)

    groups = {sid: paths for sid, paths in groups.items() if paths}
    return groups


def path_residual_entries(
    groups: dict[str, list[str]], state_ids: set[str], excluded_ids: set[str]
) -> list[dict[str, Any]]:
    entries = []
    for sid in sorted(groups):
        if sid in state_ids or sid in excluded_ids:
            continue
        paths = sorted(dict.fromkeys(groups[sid]))
        entries.append(
            {
                "id": sid,
                "count": len(paths),
                "paths": paths,
                "path_contracts": [
                    path_contract_entry(Path(path))
                    for path in paths
                    if path_is_present(Path(path))
                ],
            }
        )
    return entries


def rows_without_state_entries(
    row_counts: dict[str, int], state_ids: set[str], excluded_ids: set[str]
) -> list[dict[str, Any]]:
    return [
        {"id": sid, "rows": row_counts[sid]}
        for sid in sorted(row_counts)
        if sid not in state_ids and sid not in excluded_ids
    ]


def session_index_residual_entries(
    index_path: Path,
    state_ids: set[str],
    excluded_ids: set[str],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    if not index_path.exists():
        return []
    with index_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            sid = parse_index_id(line)
            if sid is None or sid in state_ids or sid in excluded_ids:
                continue
            grouped.setdefault(sid, []).append(
                {
                    "line_number": line_number,
                    "content_sha256": hashlib.sha256(line.encode("utf-8")).hexdigest(),
                }
            )
    return [
        {"id": sid, "rows": len(grouped[sid]), "line_contracts": grouped[sid]}
        for sid in sorted(grouped)
    ]


def session_index_entries_for_ids(
    index_path: Path,
    included_ids: set[str],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    if not index_path.exists() or not included_ids:
        return []
    with index_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            sid = parse_index_id(line)
            if sid not in included_ids:
                continue
            grouped.setdefault(sid, []).append(
                {
                    "line_number": line_number,
                    "content_sha256": hashlib.sha256(line.encode("utf-8")).hexdigest(),
                }
            )
    return [
        {"id": sid, "rows": len(grouped[sid]), "line_contracts": grouped[sid]}
        for sid in sorted(grouped)
    ]


def logs_residual_entries(
    logs: sqlite3.Connection | None,
    state_ids: set[str],
    excluded_ids: set[str],
) -> list[dict[str, Any]]:
    if (
        logs is None
        or not table_exists(logs, "logs")
        or not {"id", "thread_id"}.issubset(columns(logs, "logs"))
    ):
        return []
    column_names = ordered_columns(logs, "logs")
    id_position = column_names.index("id")
    thread_position = column_names.index("thread_id")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in logs.execute("SELECT * FROM logs ORDER BY id"):
        raw_thread_id = row[thread_position]
        if raw_thread_id is None or str(raw_thread_id) == "":
            continue
        sid = str(raw_thread_id).lower()
        if sid in state_ids or sid in excluded_ids:
            continue
        grouped.setdefault(sid, []).append(
            {
                "row_id": int(row[id_position]),
                "row_sha256": sqlite_row_sha256(column_names, row),
            }
        )
    return [
        {"id": sid, "rows": len(grouped[sid]), "row_contracts": grouped[sid]}
        for sid in sorted(grouped)
    ]


def logs_entries_for_ids(
    logs: sqlite3.Connection | None,
    included_ids: set[str],
) -> list[dict[str, Any]]:
    if (
        logs is None
        or not included_ids
        or not table_exists(logs, "logs")
        or not {"id", "thread_id"}.issubset(columns(logs, "logs"))
    ):
        return []
    column_names = ordered_columns(logs, "logs")
    id_position = column_names.index("id")
    thread_position = column_names.index("thread_id")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in logs.execute("SELECT * FROM logs ORDER BY id"):
        raw_thread_id = row[thread_position]
        if raw_thread_id is None:
            continue
        sid = str(raw_thread_id).lower()
        if sid not in included_ids:
            continue
        grouped.setdefault(sid, []).append(
            {
                "row_id": int(row[id_position]),
                "row_sha256": sqlite_row_sha256(column_names, row),
            }
        )
    return [
        {"id": sid, "rows": len(grouped[sid]), "row_contracts": grouped[sid]}
        for sid in sorted(grouped)
    ]


def entry_ids(entries: Iterable[dict[str, Any]]) -> set[str]:
    return {str(entry["id"]).lower() for entry in entries if entry.get("id")}


def orphan_reference_ids(orphan_refs: dict[str, list[dict[str, Any]]]) -> set[str]:
    ids: set[str] = set()
    for entries in orphan_refs.values():
        for entry in entries:
            if entry.get("id"):
                ids.add(str(entry["id"]).lower())
            ids.update(str(sid).lower() for sid in entry.get("missing_thread_ids", []))
    return ids


def paths_from_entries(entries: Iterable[dict[str, Any]]) -> list[Path]:
    paths: list[Path] = []
    for entry in entries:
        paths.extend(Path(path) for path in entry.get("paths", []))
    return paths


def path_contracts_from_entries(
    entries: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    contracts: dict[str, dict[str, Any]] = {}
    for entry in entries:
        for contract in entry.get("path_contracts", []):
            if isinstance(contract, dict) and contract.get("path"):
                contracts[str(contract["path"])] = contract
    return contracts


def paths_for_ids(groups: dict[str, list[str]], ids: set[str]) -> list[Path]:
    paths: list[Path] = []
    for sid in ids:
        paths.extend(Path(path) for path in groups.get(sid, []))
    return paths


def missing_rollout_entries(
    state: sqlite3.Connection | None,
    codex_home: Path,
    excluded_ids: set[str],
    rollout_groups: dict[str, list[str]] | None = None,
    snapshot_groups: dict[str, list[str]] | None = None,
    generated_groups: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    sessions_root = codex_home / "sessions"
    entries: list[dict[str, Any]] = []
    current_thread_id = os.environ.get("CODEX_THREAD_ID", "").lower()
    for sid, rollout_path in state_thread_rollout_rows(state):
        if sid in excluded_ids or not rollout_path:
            continue
        path = Path(rollout_path).expanduser()
        if not path.is_absolute() or not is_relative_to(path, sessions_root):
            continue
        if not path.exists():
            artifact_paths: list[Path] = []
            for groups in [rollout_groups, snapshot_groups, generated_groups]:
                if groups is not None:
                    artifact_paths.extend(paths_for_ids(groups, {sid}))
            artifact_paths = unique_paths(artifact_paths)
            incoming = incoming_edge_rows(state, [sid]).get(sid, [])
            touching = touching_edge_rows(state, [sid])
            entries.append(
                {
                    "id": sid,
                    "rollout_path": str(path),
                    "incoming_edges": incoming,
                    "touching_edges": touching,
                    "open_or_unknown": any(
                        edge_status_is_open(edge.get("status", "")) for edge in touching
                    ),
                    "current_session": sid == current_thread_id,
                    "rollout_migration_skipped_rows": (
                        rollout_migration_skipped_rows_for_paths(
                            state,
                            [str(path)],
                        )
                    ),
                    "paths": [str(item) for item in artifact_paths],
                    "path_contracts": [
                        path_contract_entry(item)
                        for item in artifact_paths
                        if path_is_present(item)
                    ],
                }
            )
    return entries


def orphan_reference_entries(
    state: sqlite3.Connection | None,
    state_ids: set[str],
    excluded_ids: set[str],
) -> dict[str, list[dict[str, Any]]]:
    if state is None:
        return {}

    refs: dict[str, list[dict[str, Any]]] = {}
    for table, column in [
        ("thread_dynamic_tools", "thread_id"),
        ("thread_goals", "thread_id"),
        ("stage1_outputs", "thread_id"),
        ("agent_job_items", "assigned_thread_id"),
    ]:
        if not table_exists(state, table) or column not in columns(state, table):
            continue
        rows: list[dict[str, Any]] = []
        sql = (
            f"SELECT {quote_ident(column)}, COUNT(*) FROM {quote_ident(table)} "
            f"WHERE {quote_ident(column)} IS NOT NULL AND {quote_ident(column)} != '' "
            f"GROUP BY {quote_ident(column)} ORDER BY {quote_ident(column)}"
        )
        for sid, count in state.execute(sql):
            sid = str(sid).lower()
            if sid not in state_ids and sid not in excluded_ids:
                rows.append(
                    {
                        "id": sid,
                        "rows": int(count),
                        "row_digests": reference_row_digests(state, table, column, sid),
                    }
                )
        if rows:
            refs[table] = rows

    if table_exists(state, "thread_spawn_edges") and {
        "parent_thread_id",
        "child_thread_id",
        "status",
    }.issubset(columns(state, "thread_spawn_edges")):
        edge_rows: list[dict[str, Any]] = []
        edge_columns = ordered_columns(state, "thread_spawn_edges")
        parent_position = edge_columns.index("parent_thread_id")
        child_position = edge_columns.index("child_thread_id")
        status_position = edge_columns.index("status")
        for edge_row in state.execute("SELECT * FROM thread_spawn_edges"):
            parent_id = edge_row[parent_position]
            child_id = edge_row[child_position]
            status = edge_row[status_position]
            parent = str(parent_id).lower()
            child = str(child_id).lower()
            missing = [
                sid
                for sid in [parent, child]
                if sid not in state_ids and sid not in excluded_ids
            ]
            if missing:
                edge_rows.append(
                    {
                        "parent_thread_id": parent,
                        "child_thread_id": child,
                        "status": status or "",
                        "missing_thread_ids": missing,
                        "row_sha256": sqlite_row_sha256(edge_columns, edge_row),
                    }
                )
        if edge_rows:
            refs["thread_spawn_edges"] = sorted(
                edge_rows,
                key=lambda entry: (
                    entry["parent_thread_id"],
                    entry["child_thread_id"],
                    entry["status"],
                ),
            )

    return refs


def summarize_historical_residuals(residuals: dict[str, Any]) -> None:
    ids: set[str] = set()
    total_items = 0
    issue_categories = 0

    for key in [
        "session_index_rows_without_state",
        "rollout_files_without_state",
        "shell_snapshots_without_state",
        "generated_artifacts_without_state",
        "logs_rows_without_state",
        "state_threads_missing_rollout_file",
    ]:
        entries = residuals.get(key, [])
        if entries:
            issue_categories += 1
        for entry in entries:
            sid = entry.get("id")
            if sid:
                ids.add(sid)
            total_items += int(entry.get("rows", entry.get("count", 1)))

    orphan_refs = residuals.get("state_orphan_references", {})
    for entries in orphan_refs.values():
        if entries:
            issue_categories += 1
        for entry in entries:
            sid = entry.get("id")
            if sid:
                ids.add(sid)
            for sid in entry.get("missing_thread_ids", []):
                ids.add(sid)
            total_items += int(entry.get("rows", 1))

    residuals["summary"] = {
        "has_residuals": total_items > 0,
        "issue_categories": issue_categories,
        "total_ids": len(ids),
        "total_items": total_items,
    }


def scan_historical_residuals(
    codex_home: Path,
    excluded_ids: set[str],
    include_logs: bool,
) -> dict[str, Any]:
    excluded_ids = {sid.lower() for sid in excluded_ids}
    boundary_issues = storage_boundary_issues(codex_home, include_logs)
    if boundary_issues:
        return skipped_historical_scan(
            "Historical cleanup is unavailable because managed path safety checks failed: "
            + " | ".join(boundary_issues)
        )
    index_issues = session_index_issues(codex_home / "session_index.jsonl")
    if index_issues:
        return skipped_historical_scan(
            "Historical cleanup is unavailable because session_index.jsonl is invalid: "
            + " | ".join(index_issues)
        )

    state_path = discovered_database_path(discover_state_database(codex_home))
    logs_path = discovered_database_path(discover_logs_database(codex_home))
    paginated_history_path = discovered_database_path(
        discover_paginated_history_database(codex_home)
    )
    state = connect_ro(state_path) if state_path is not None else None
    authoritative_session_ids = state_thread_ids(state)
    unsafe_artifacts: list[str] = []
    rollout_groups = rollout_paths_by_session(
        codex_home,
        unsafe_artifacts,
        authoritative_session_ids,
    )
    snapshot_groups = shell_snapshot_paths_by_session(
        codex_home,
        unsafe_artifacts,
        authoritative_session_ids,
    )
    generated_groups = generated_paths_by_session(
        codex_home,
        unsafe_artifacts,
        authoritative_session_ids,
    )
    if unsafe_artifacts:
        if state is not None:
            state.close()
        return skipped_historical_scan(
            "Historical cleanup is unavailable because symbolic-link or escaped artifact paths "
            "were discovered: " + " | ".join(sorted(dict.fromkeys(unsafe_artifacts)))
        )
    logs = (
        connect_ro(logs_path)
        if include_logs and logs_path is not None
        else None
    )
    paginated_history = (
        connect_ro(paginated_history_path)
        if paginated_history_path is not None
        else None
    )
    try:
        if not authoritative_state_available(state):
            return skipped_historical_scan(
                "Historical cleanup is unavailable because the selected state database is missing or its "
                "threads schema is unsupported."
            )
        reference_issues = reference_format_issues(
            state,
            STATE_REFERENCE_FORMAT_LOCATIONS,
            state_path.name if state_path is not None else "state_*.sqlite",
        )
        if include_logs:
            reference_issues.extend(
                reference_format_issues(
                    logs,
                    [("logs", "thread_id")],
                    logs_path.name if logs_path is not None else "logs_*.sqlite",
                )
            )
        rollout_issues = state_rollout_path_issues(state, codex_home)
        paginated_issues: list[str] = []
        if paginated_history_path is not None:
            paginated_check = database_check(
                paginated_history_path,
                "quick_check",
            )
            if paginated_check != "ok":
                paginated_issues.append(
                    "Paginated history database quick_check failed: "
                    f"{paginated_check}"
                )
            paginated_issues.extend(
                paginated_history_anchor_issues(
                    paginated_history,
                    paginated_history_path.name,
                )
            )
        if reference_issues or rollout_issues or paginated_issues:
            return skipped_historical_scan(
                "Historical cleanup is unavailable because state/log/paginated-history "
                "storage is not authoritative: "
                + " | ".join(reference_issues + rollout_issues + paginated_issues)
            )
        known_state_ids = state_thread_ids(state)
        known_paginated_history_ids = paginated_history_thread_ids(
            paginated_history
        )
        non_log_candidate_ids = (
            set(session_index_row_counts(codex_home / "session_index.jsonl"))
            | set(rollout_groups)
            | set(snapshot_groups)
            | set(generated_groups)
            | reference_ids_at_locations(state, STATE_REFERENCE_LOCATIONS)
        )
        log_candidate_ids = (
            reference_ids_at_locations(logs, [("logs", "thread_id")])
            if include_logs
            else set()
        )
        transient_log_only_ids = recent_logs_only_ids(
            logs,
            known_state_ids | non_log_candidate_ids,
            excluded_ids,
        )
        historical_candidate_ids = (
            non_log_candidate_ids | (log_candidate_ids - transient_log_only_ids)
        ) - excluded_ids
        extension_compatibility = state_schema_compatibility(
            state,
            historical_candidate_ids - known_state_ids,
        )
        logs_extension_compatibility = logs_schema_compatibility(
            logs,
            historical_candidate_ids - known_state_ids,
        )
        paginated_extension_compatibility = paginated_history_schema_compatibility(
            paginated_history,
            historical_candidate_ids - known_state_ids,
        )
        extension_protected_ids = (
            set(extension_compatibility.get("protected_ids", []))
            | set(logs_extension_compatibility.get("protected_ids", []))
            | set(paginated_extension_compatibility.get("protected_ids", []))
        )
        known_or_protected_ids = (
            known_state_ids
            | known_paginated_history_ids
            | extension_protected_ids
            | transient_log_only_ids
        )
        has_evidence = bool(
            session_index_row_counts(codex_home / "session_index.jsonl")
            or rollout_groups
            or snapshot_groups
            or generated_groups
        )
        if (
            not has_evidence
            and logs is not None
            and table_exists(logs, "logs")
            and "thread_id" in columns(logs, "logs")
        ):
            has_evidence = (
                logs.execute(
                    "SELECT 1 FROM logs WHERE thread_id IS NOT NULL "
                    "AND thread_id != '' LIMIT 1"
                ).fetchone()
                is not None
            )
        if not known_or_protected_ids and has_evidence:
            return skipped_historical_scan(
                "Historical cleanup is unavailable because the authoritative threads table is "
                "empty while session artifacts still exist."
            )
        residuals: dict[str, Any] = {
            "scanned": True,
            "authoritative": True,
            "excluded_target_ids": sorted(excluded_ids),
            "schema_compatibility": extension_compatibility,
            "logs_schema_compatibility": logs_extension_compatibility,
            "paginated_history_schema_compatibility": (
                paginated_extension_compatibility
            ),
            "paginated_history_protected_ids": sorted(
                known_paginated_history_ids - known_state_ids
            ),
            "extension_protected_ids": sorted(extension_protected_ids),
            "session_index_rows_without_state": session_index_residual_entries(
                codex_home / "session_index.jsonl",
                known_or_protected_ids,
                excluded_ids,
            ),
            "rollout_files_without_state": path_residual_entries(
                rollout_groups,
                known_or_protected_ids,
                excluded_ids,
            ),
            "shell_snapshots_without_state": path_residual_entries(
                snapshot_groups,
                known_or_protected_ids,
                excluded_ids,
            ),
            "generated_artifacts_without_state": path_residual_entries(
                generated_groups,
                known_or_protected_ids,
                excluded_ids,
            ),
            "logs_rows_without_state": logs_residual_entries(
                logs,
                known_or_protected_ids,
                excluded_ids,
            )
            if include_logs
            else [],
            "state_orphan_references": orphan_reference_entries(
                state,
                known_or_protected_ids,
                excluded_ids,
            ),
            "state_threads_missing_rollout_file": missing_rollout_entries(
                state,
                codex_home,
                excluded_ids,
                rollout_groups,
                snapshot_groups,
                generated_groups,
            ),
            "notes": [],
        }
        if extension_protected_ids:
            residuals["notes"].append(
                "Runtime schema extensions referenced candidate IDs; those IDs were "
                "conservatively protected from historical cleanup."
            )
        if known_paginated_history_ids - known_state_ids:
            residuals["notes"].append(
                "IDs present only in structurally validated paginated history are "
                "conservatively treated as live/protected, not historical deletion scope."
            )
        if include_logs:
            residuals["notes"].append(
                "Recent log-only worker IDs are treated as transient and excluded "
                "from the frozen historical scope."
            )
        if (
            not extension_compatibility.get("scan_complete", True)
            or not logs_extension_compatibility.get("scan_complete", True)
            or not paginated_extension_compatibility.get("scan_complete", True)
        ):
            residuals["notes"].append(
                "Runtime schema extension inspection reached its byte limit; all "
                "affected candidates were protected rather than classified as residuals."
            )
        if not include_logs:
            residuals["notes"].append(
                "The logs database was not scanned because --no-logs was used."
            )
        summarize_historical_residuals(residuals)
        return residuals
    finally:
        if state is not None:
            state.close()
        if logs is not None:
            logs.close()
        if paginated_history is not None:
            paginated_history.close()


def build_preflight(
    codex_home: Path,
    state_path: Path | None,
    logs_path: Path | None,
    state_discovery: dict[str, Any],
    logs_discovery: dict[str, Any],
    state: sqlite3.Connection | None,
    logs: sqlite3.Connection | None,
    desktop_catalog_path: Path | None,
    desktop_catalog: sqlite3.Connection | None,
    desktop_catalog_discovery_issues: list[str],
    include_subagents: bool,
    include_logs: bool,
) -> tuple[dict[str, Any], list[str]]:
    index_path = codex_home / "session_index.jsonl"
    sqlite_root = codex_home / "sqlite"
    state_path_issues = (
        managed_sqlite_issues(codex_home, state_path)
        if state_path is not None
        else []
    )
    logs_path_issues = (
        managed_sqlite_issues(codex_home, logs_path)
        if include_logs and logs_path is not None
        else []
    )
    index_path_issue = managed_file_issue(codex_home, index_path)
    sqlite_root_issue = managed_root_issue(codex_home, sqlite_root)
    global_state_path_issues = [
        issue
        for filename in GLOBAL_STATE_FILENAMES
        if path_is_present(codex_home / filename)
        if (issue := managed_file_issue(codex_home, codex_home / filename)) is not None
    ]
    global_state_recovery_files = global_state_recovery_paths(codex_home)
    desktop_catalog_path_issues = (
        managed_sqlite_issues(codex_home, desktop_catalog_path)
        if desktop_catalog_path is not None
        else []
    )
    index_issues = session_index_issues(index_path) if index_path_issue is None else []
    artifact_root_issues = [
        issue
        for name in ["sessions", "shell_snapshots", "generated_images"]
        if (issue := managed_root_issue(codex_home, codex_home / name)) is not None
    ]
    state_issues = state_schema_issues(state)
    log_issues = logs_schema_issues(logs) if include_logs else []
    desktop_catalog_issues = desktop_catalog_schema_issues(desktop_catalog)
    state_reference_issues = reference_format_issues(
        state,
        STATE_REFERENCE_FORMAT_LOCATIONS,
        state_path.name if state_path is not None else "state_*.sqlite",
    )
    log_reference_issues = (
        reference_format_issues(
            logs,
            [("logs", "thread_id")],
            logs_path.name if logs_path is not None else "logs_*.sqlite",
        )
        if include_logs
        else []
    )
    desktop_catalog_reference_issues = reference_format_issues(
        desktop_catalog,
        DESKTOP_CATALOG_CANONICAL_UUID_REFERENCES,
        "sqlite/codex desktop catalog database",
    )
    rollout_reference_issues = state_rollout_path_issues(state, codex_home)
    state_authoritative = authoritative_state_available(state)
    state_quick_check = (
        "missing"
        if state_path is None
        else "unsafe"
        if state_path_issues
        else database_check(state_path, "quick_check")
    )
    logs_quick_check = (
        "skipped"
        if not include_logs
        else "missing"
        if logs_path is None
        else "unsafe"
        if logs_path_issues
        else database_check(logs_path, "quick_check")
    )
    desktop_catalog_quick_check = (
        "missing"
        if desktop_catalog_path is None
        else (
            "unsafe"
            if desktop_catalog_path_issues
            else database_check(desktop_catalog_path, "quick_check")
        )
    )
    paginated_history_discovery = discover_paginated_history_database(codex_home)
    paginated_history_path = discovered_database_path(paginated_history_discovery)
    structured_diagnostics: list[dict[str, Any]] = []

    def add_structured_diagnostics(
        code: str,
        component: str,
        reasons: Iterable[str],
        dependent_components: Iterable[str] = (),
    ) -> None:
        for reason in reasons:
            structured_diagnostics.append(
                {
                    "code": code,
                    "component": component,
                    "reason": str(reason),
                    "disposition": DISPOSITION_SKIP_COMPONENT,
                    "dependent_components": sorted(set(dependent_components)),
                }
            )

    add_structured_diagnostics(
        "state_database_discovery_failed",
        COMPONENT_CORE,
        (str(issue) for issue in state_discovery.get("issues", [])),
        [COMPONENT_HISTORICAL],
    )
    if include_logs:
        add_structured_diagnostics(
            "logs_database_discovery_failed",
            COMPONENT_LOGS,
            (str(issue) for issue in logs_discovery.get("issues", [])),
        )

    blockers: list[str] = []
    if not codex_home.is_dir():
        blockers.append(f"Codex home is not an existing directory: {codex_home}")
    blockers.extend(str(issue) for issue in state_discovery.get("issues", []))
    if include_logs:
        blockers.extend(str(issue) for issue in logs_discovery.get("issues", []))
    blockers.extend(state_path_issues)
    blockers.extend(logs_path_issues)
    blockers.extend(desktop_catalog_discovery_issues)
    blockers.extend(desktop_catalog_path_issues)
    blockers.extend(global_state_path_issues)
    if global_state_recovery_files:
        blockers.append(
            "Unresolved global-state recovery file(s) require inspection before "
            "another deletion: "
            + ", ".join(str(path) for path in global_state_recovery_files)
        )
    if index_path_issue is not None:
        blockers.append(index_path_issue)
    if sqlite_root_issue is not None:
        blockers.append(sqlite_root_issue)
    blockers.extend(index_issues)
    blockers.extend(artifact_root_issues)
    if state_quick_check not in {"ok", "missing"}:
        blockers.append(
            f"Selected state database quick_check failed: {state_quick_check}"
        )
    if logs_quick_check not in {"ok", "missing", "skipped"}:
        blockers.append(
            f"Selected logs database quick_check failed: {logs_quick_check}"
        )
    if desktop_catalog_quick_check not in {"ok", "missing"}:
        blockers.append(
            "Desktop catalog database quick_check failed: "
            f"{desktop_catalog_quick_check}"
        )
    blockers.extend(state_issues)
    blockers.extend(log_issues)
    blockers.extend(desktop_catalog_issues)
    blockers.extend(state_reference_issues)
    blockers.extend(log_reference_issues)
    blockers.extend(desktop_catalog_reference_issues)
    blockers.extend(rollout_reference_issues)
    if include_subagents and not state_authoritative:
        if state_issues:
            blockers.append(
                "Cannot resolve recursive subagents because authoritative thread state is unavailable; "
                "resolve the reported state schema issues. --no-subagents disables recursive graph "
                "expansion but does not bypass schema safety blockers."
            )
        else:
            blockers.append(
                "Cannot resolve recursive subagents because authoritative thread state is unavailable; "
                "repair the state database or explicitly use --no-subagents."
            )
    writable_state_paths = [index_path]
    if state_path is not None:
        writable_state_paths.append(state_path)
    if include_logs and logs_path is not None:
        writable_state_paths.append(logs_path)
    if desktop_catalog_path is not None:
        writable_state_paths.append(desktop_catalog_path)
    if paginated_history_path is not None:
        writable_state_paths.append(paginated_history_path)
    writable_state_paths.extend(
        codex_home / filename
        for filename in GLOBAL_STATE_FILENAMES
        if path_is_present(codex_home / filename)
    )
    for path in writable_state_paths:
        if (
            path.exists()
            and managed_file_issue(codex_home, path) is None
            and not os.access(path, os.W_OK)
        ):
            blockers.append(f"Required state file is not writable: {path}")
    if codex_home.is_dir() and not os.access(codex_home, os.W_OK | os.X_OK):
        blockers.append(f"Codex home directory is not writable: {codex_home}")

    categorized_diagnostics = [
        (
            "state_storage_contract_failed",
            COMPONENT_CORE,
            [*state_path_issues, *state_issues, *state_reference_issues, *rollout_reference_issues],
            [COMPONENT_HISTORICAL],
        ),
        (
            "logs_storage_contract_failed",
            COMPONENT_LOGS,
            [*logs_path_issues, *log_issues, *log_reference_issues],
            [],
        ),
        (
            "desktop_catalog_contract_failed",
            COMPONENT_CATALOG,
            [
                *desktop_catalog_discovery_issues,
                *desktop_catalog_path_issues,
                *desktop_catalog_issues,
                *desktop_catalog_reference_issues,
            ],
            [],
        ),
        (
            "global_state_contract_failed",
            COMPONENT_GLOBAL_STATE,
            list(global_state_path_issues),
            [],
        ),
        (
            "session_index_contract_failed",
            COMPONENT_CORE,
            [*index_issues, *([index_path_issue] if index_path_issue else [])],
            [COMPONENT_HISTORICAL],
        ),
    ]
    for code, component, reasons, dependencies in categorized_diagnostics:
        add_structured_diagnostics(code, component, reasons, dependencies)
    for reason in artifact_root_issues:
        artifact = unsafe_artifact_record(codex_home, reason)
        add_structured_diagnostics(
            "artifact_root_contract_failed",
            artifact[0] if artifact is not None else COMPONENT_ROLLOUTS,
            [reason],
        )
    categorized_reasons = {
        str(item.get("reason", "")) for item in structured_diagnostics
    }
    add_structured_diagnostics(
        "general_storage_contract_failed",
        COMPONENT_CORE,
        [blocker for blocker in blockers if blocker not in categorized_reasons],
        [COMPONENT_HISTORICAL],
    )

    return {
        "state_db_present": state_path is not None and path_is_present(state_path),
        "logs_db_present": logs_path is not None and path_is_present(logs_path),
        "state_database_path": str(state_path) if state_path is not None else "",
        "logs_database_path": str(logs_path) if logs_path is not None else "",
        "state_database_discovery": state_discovery,
        "logs_database_discovery": logs_discovery,
        "desktop_catalog_db_present": desktop_catalog_path is not None,
        "desktop_catalog_path": (
            str(desktop_catalog_path) if desktop_catalog_path is not None else ""
        ),
        "session_index_present": path_is_present(index_path),
        "state_authoritative": state_authoritative,
        "state_quick_check": state_quick_check,
        "logs_quick_check": logs_quick_check,
        "desktop_catalog_quick_check": desktop_catalog_quick_check,
        "state_schema_issues": state_issues,
        "logs_schema_issues": log_issues,
        "desktop_catalog_schema_issues": desktop_catalog_issues,
        "desktop_catalog_schema_signature": desktop_catalog_schema_signature(
            desktop_catalog
        ),
        "desktop_catalog_user_version": (
            int(desktop_catalog.execute("PRAGMA user_version").fetchone()[0])
            if desktop_catalog is not None
            else None
        ),
        "state_reference_issues": state_reference_issues,
        "logs_reference_issues": log_reference_issues,
        "desktop_catalog_reference_issues": desktop_catalog_reference_issues,
        "state_rollout_path_issues": rollout_reference_issues,
        "session_index_issues": index_issues,
        "managed_path_issues": sorted(
            dict.fromkeys(
                state_path_issues
                + logs_path_issues
                + desktop_catalog_discovery_issues
                + desktop_catalog_path_issues
                + global_state_path_issues
                + ([index_path_issue] if index_path_issue is not None else [])
                + ([sqlite_root_issue] if sqlite_root_issue is not None else [])
                + artifact_root_issues
            )
        ),
        "global_state_recovery_files": [
            str(path) for path in global_state_recovery_files
        ],
        "paginated_history_database_discovery": paginated_history_discovery,
        "thread_history_databases": [
            str(item.get("path"))
            for item in paginated_history_discovery.get("candidates", [])
            if item.get("path")
        ],
        "structured_diagnostics": structured_diagnostics,
    }, sorted(dict.fromkeys(blockers))


def noncanonical_target_references(
    state: sqlite3.Connection | None,
    logs: sqlite3.Connection | None,
    desktop_catalog: sqlite3.Connection | None,
    target_ids: list[str],
    include_logs: bool,
    state_label: str = "selected state database",
    logs_label: str = "selected logs database",
    catalog_label: str = "sqlite/codex catalog database",
) -> list[dict[str, str]]:
    if not target_ids:
        return []
    findings: list[dict[str, str]] = []

    def inspect(
        conn: sqlite3.Connection | None,
        locations: Iterable[tuple[str, str]],
        database: str,
    ) -> None:
        if conn is None:
            return
        for table, column in locations:
            if not table_exists(conn, table) or column not in columns(conn, table):
                continue
            sql = (
                f"SELECT DISTINCT {quote_ident(column)} FROM {quote_ident(table)} "
                f"WHERE lower(CAST({quote_ident(column)} AS TEXT)) "
                f"IN ({placeholders(target_ids)})"
            )
            for (value,) in conn.execute(sql, target_ids):
                if value is None:
                    continue
                raw = str(value)
                if raw != raw.lower():
                    findings.append(
                        {
                            "database": database,
                            "location": f"{table}.{column}",
                            "value": raw,
                        }
                    )

    inspect(state, STATE_REFERENCE_LOCATIONS, state_label)
    if include_logs:
        inspect(logs, [("logs", "thread_id")], logs_label)
    inspect(
        desktop_catalog,
        DESKTOP_CATALOG_THREAD_REFERENCES,
        catalog_label,
    )
    return sorted(
        findings,
        key=lambda item: (item["database"], item["location"], item["value"]),
    )


def reference_format_issues(
    conn: sqlite3.Connection | None,
    locations: Iterable[tuple[str, str]],
    database: str,
) -> list[str]:
    if conn is None:
        return []
    issues: list[str] = []
    for table, column in locations:
        if not table_exists(conn, table) or column not in columns(conn, table):
            continue
        sql = (
            f"SELECT DISTINCT {quote_ident(column)} FROM {quote_ident(table)} "
            f"WHERE {quote_ident(column)} IS NOT NULL "
            f"AND CAST({quote_ident(column)} AS TEXT) != ''"
        )
        for (value,) in conn.execute(sql):
            raw = str(value)
            if not CANONICAL_UUID_RE.fullmatch(raw) or raw != raw.lower():
                issues.append(
                    f"{database} contains a non-canonical session reference at "
                    f"{table}.{column}: {raw}"
                )
                if len(issues) >= 20:
                    return issues
    return issues


def target_connected_components(plan: Plan) -> list[set[str]]:
    targets = set(plan.target_ids)
    adjacency = {sid: set() for sid in targets}
    for edge in plan.target_edge_rows:
        parent = str(edge.get("parent_thread_id", "")).lower()
        child = str(edge.get("child_thread_id", "")).lower()
        if parent in targets and child in targets:
            adjacency[parent].add(child)
            adjacency[child].add(parent)
    components: list[set[str]] = []
    unseen = set(targets)
    while unseen:
        start = min(unseen)
        pending = [start]
        connected: set[str] = set()
        while pending:
            sid = pending.pop()
            if sid in connected:
                continue
            connected.add(sid)
            pending.extend(sorted(adjacency.get(sid, set()) - connected))
        unseen -= connected
        components.append(connected)
    return sorted(components, key=lambda item: sorted(item))


def warning_mentions_only_unrelated_targets(plan: Plan, message: str) -> bool:
    mentioned = {match.lower() for match in UUID_RE.findall(message)}
    return bool(mentioned) and not bool(mentioned & set(plan.target_ids))


def finalize_plan_execution_model(plan: Plan) -> None:
    plan.component_plans = {
        component: {"status": "enabled", "reasons": []}
        for component in ALL_COMPONENTS
    }
    raw_blockers = list(plan.blockers)
    raw_unsafe_paths = list(plan.unsafe_paths)
    plan.blockers = []
    plan.unsafe_paths = []
    structured_diagnostics = {
        str(item.get("reason", "")): item
        for item in plan.preflight.get("structured_diagnostics", [])
        if isinstance(item, dict) and item.get("reason")
    }

    for message in raw_blockers:
        diagnostic = structured_diagnostics.get(message)
        component = (
            str(diagnostic.get("component"))
            if diagnostic is not None
            else COMPONENT_CORE
        )
        lowered = message.lower()
        non_owning_cursor = (
            "rollout_migration_state" in lowered
            and (
                "last-checked thread cursor" in lowered
                or "last_checked_thread_id" in lowered
            )
        )
        unrelated_reference = (
            ("non-canonical session reference" in lowered or "rollout_path" in lowered)
            and warning_mentions_only_unrelated_targets(plan, message)
        )
        target_local_retention = (
            "current codex session is inside the deletion target set" in lowered
            or "target session history_mode is unsupported" in lowered
            or "unsafe symbolic-link, escaped, or non-file artifact paths were discovered"
            in lowered
        )
        no_evidence = "no requested session id matched" in lowered
        if non_owning_cursor or unrelated_reference or target_local_retention:
            disposition = DISPOSITION_WARN
        elif no_evidence:
            disposition = DISPOSITION_WARN
            for candidate in ALL_COMPONENTS:
                if candidate != COMPONENT_HISTORICAL:
                    skip_plan_component(plan, candidate, message)
        else:
            disposition = DISPOSITION_SKIP_COMPONENT
            if "codex home is not" in lowered or "codex home directory is not writable" in lowered:
                for candidate in ALL_COMPONENTS:
                    skip_plan_component(plan, candidate, message)
            else:
                skip_plan_component(plan, component, message)
                dependent_components = (
                    diagnostic.get("dependent_components", [])
                    if diagnostic is not None
                    else [COMPONENT_HISTORICAL]
                    if component == COMPONENT_CORE
                    else []
                )
                for dependent_component in dependent_components:
                    skip_plan_component(plan, str(dependent_component), message)
        append_safety_warning(
            plan,
            safety_warning(
                str(diagnostic.get("code", "plan_safety_finding"))
                if diagnostic is not None
                else "plan_safety_finding",
                message,
                component,
                disposition,
            ),
        )
        artifact_record = unsafe_artifact_record(plan.codex_home, message)
        if artifact_record is not None:
            component, path, contract = artifact_record
            retained_object: dict[str, Any] = {
                "component": component,
                "object_id": str(path),
                "reason": message,
                "status": "retained_untrusted",
            }
            if contract is not None:
                retained_object["preserved_contract"] = contract
            if not any(
                item.get("component") == component
                and item.get("object_id") == str(path)
                for item in plan.retained_objects
            ):
                plan.retained_objects.append(retained_object)

    for message in raw_unsafe_paths:
        artifact_record = unsafe_artifact_record(plan.codex_home, message)
        component = (
            artifact_record[0]
            if artifact_record is not None
            else COMPONENT_CORE
        )
        object_id = (
            str(artifact_record[1]) if artifact_record is not None else message
        )
        append_safety_warning(
            plan,
            safety_warning(
                "unsafe_object_retained",
                message,
                component,
                DISPOSITION_RETAIN_OBJECT,
                object_id=object_id,
            ),
        )
        retained_object = {
            "component": component,
            "object_id": object_id,
            "reason": message,
            "status": "retained_untrusted",
        }
        if artifact_record is not None and artifact_record[2] is not None:
            retained_object["preserved_contract"] = artifact_record[2]
        plan.retained_objects.append(retained_object)

    for message in plan.warnings:
        append_safety_warning(
            plan,
            safety_warning(
                "diagnostic_warning",
                message,
                COMPONENT_CORE,
                DISPOSITION_WARN,
            ),
        )

    for entry in plan.historical_residuals.get(
        "state_threads_missing_rollout_file", []
    ):
        if not isinstance(entry, dict):
            continue
        sid = str(entry.get("id", "")).lower()
        if not CANONICAL_UUID_RE.fullmatch(sid):
            continue
        if entry.get("current_session") is True:
            append_safety_warning(
                plan,
                safety_warning(
                    "historical_current_session_retained",
                    "A historical missing-rollout thread is the current Codex session "
                    "and is always preserved.",
                    COMPONENT_HISTORICAL,
                    DISPOSITION_RETAIN_OBJECT,
                    session_id=sid,
                ),
            )
        elif entry.get("open_or_unknown") is True:
            append_safety_warning(
                plan,
                safety_warning(
                    "historical_open_session_requires_explicit_include",
                    "An open or unknown-status session with a missing rollout "
                    "requires the force-open approval scope.",
                    COMPONENT_HISTORICAL,
                    "requires_explicit_include",
                    session_id=sid,
                ),
            )

    plan.target_dispositions = {
        sid: {"status": "eligible", "reasons": []} for sid in plan.target_ids
    }
    current_thread_id = os.environ.get("CODEX_THREAD_ID", "").lower()
    permanent_hazards: dict[str, list[str]] = {}
    open_hazards: dict[str, list[str]] = {}
    for sid in plan.target_ids:
        info = plan.threads.get(sid, ThreadInfo(id=sid))
        if sid == current_thread_id:
            permanent_hazards.setdefault(sid, []).append(
                "The current Codex session is never deleted by its own process."
            )
        if info.rollout_path and info.history_mode not in {"legacy", "paginated"}:
            permanent_hazards.setdefault(sid, []).append(
                f"Unsupported target history_mode: {info.history_mode!r}."
            )
        if (
            info.rollout_path
            and info.history_mode == "paginated"
            and not component_plan_enabled(plan, COMPONENT_PAGINATED_HISTORY)
        ):
            reasons = plan.component_plans.get(
                COMPONENT_PAGINATED_HISTORY, {}
            ).get("reasons", [])
            permanent_hazards.setdefault(sid, []).append(
                "; ".join(str(reason) for reason in reasons)
                or "Paginated history storage is not safely actionable."
            )
        if sid in plan.open_subagents:
            open_hazards.setdefault(sid, []).append(
                "Open or unknown-status session requires the force-open approval scope."
            )

    for connected in target_connected_components(plan):
        permanent_reasons = [
            reason
            for sid in sorted(connected)
            for reason in permanent_hazards.get(sid, [])
        ]
        open_reasons = [
            reason
            for sid in sorted(connected)
            for reason in open_hazards.get(sid, [])
        ]
        if open_reasons:
            for sid in sorted(connected):
                append_safety_warning(
                    plan,
                    safety_warning(
                        "target_requires_explicit_include",
                        "; ".join(sorted(dict.fromkeys(open_reasons))),
                        COMPONENT_CORE,
                        "requires_explicit_include",
                        session_id=sid,
                    ),
                )
        if permanent_reasons:
            for sid in sorted(connected):
                plan.target_dispositions[sid] = {
                    "status": "retained",
                    "reasons": sorted(dict.fromkeys(permanent_reasons)),
                }
                append_safety_warning(
                    plan,
                    safety_warning(
                        "target_component_retained",
                        "; ".join(sorted(dict.fromkeys(permanent_reasons))),
                        COMPONENT_CORE,
                        DISPOSITION_RETAIN_TARGET_COMPONENT,
                        session_id=sid,
                    ),
                )
        elif open_reasons:
            for sid in sorted(connected):
                plan.target_dispositions[sid] = {
                    "status": "requires_force_open",
                    "reasons": sorted(dict.fromkeys(open_reasons)),
                }

    units: list[dict[str, Any]] = []
    for sid in plan.target_ids:
        for component in [
            COMPONENT_CORE,
            COMPONENT_LOGS,
            COMPONENT_CATALOG,
            COMPONENT_AUXILIARY,
            COMPONENT_GLOBAL_STATE,
            COMPONENT_PAGINATED_HISTORY,
        ]:
            units.append(
                {
                    "component": component,
                    "session_id": sid,
                    "object_id": sid,
                }
            )
    for component, paths in [
        (COMPONENT_ROLLOUTS, plan.rollout_files),
        (COMPONENT_SNAPSHOTS, plan.shell_snapshots),
        (COMPONENT_GENERATED, plan.generated_artifacts),
    ]:
        for path in paths:
            path_ids = {
                match.lower() for match in UUID_RE.findall(str(path))
            } & set(plan.target_ids)
            units.append(
                {
                    "component": component,
                    "session_id": next(iter(path_ids), ""),
                    "object_id": str(path),
                }
            )
    plan.executable_units = sorted(
        units,
        key=lambda item: (
            str(item.get("component", "")),
            str(item.get("session_id", "")),
            str(item.get("object_id", "")),
        ),
    )
    eligible_targets = {
        sid
        for sid, disposition in plan.target_dispositions.items()
        if disposition.get("status", "eligible") == "eligible"
    }
    enabled_target_components = any(
        component_plan_enabled(plan, component)
        for component in {
            COMPONENT_CORE,
            COMPONENT_LOGS,
            COMPONENT_ROLLOUTS,
            COMPONENT_SNAPSHOTS,
            COMPONENT_GENERATED,
            COMPONENT_CATALOG,
            COMPONENT_AUXILIARY,
            COMPONENT_GLOBAL_STATE,
            COMPONENT_PAGINATED_HISTORY,
        }
    )
    target_work_available = bool(
        eligible_targets
        and enabled_target_components
        and plan.preflight.get("target_evidence_items", 0)
    )
    historical_summary = plan.historical_residuals.get("summary", {})
    historical_work_available = bool(
        component_plan_enabled(plan, COMPONENT_HISTORICAL)
        and isinstance(historical_summary, dict)
        and historical_summary.get("has_residuals", False)
    )
    safe_operations_remaining = target_work_available or historical_work_available
    for finding in plan.safety_warnings:
        finding["safe_operations_remaining"] = safe_operations_remaining
    plan.safety_warnings.sort(
        key=lambda item: (
            str(item.get("component", "")),
            str(item.get("session_id", "")),
            str(item.get("object_id", "")),
            str(item.get("message", "")),
        )
    )


def make_plan(
    codex_home: Path,
    root_ids: list[str],
    include_subagents: bool,
    include_logs: bool,
    scan_historical: bool,
) -> Plan:
    state_discovery = discover_state_database(codex_home)
    logs_discovery = discover_logs_database(codex_home)
    state_path = discovered_database_path(state_discovery)
    logs_path = discovered_database_path(logs_discovery)
    index_path = codex_home / "session_index.jsonl"
    desktop_catalog_path, desktop_catalog_discovery_issues = (
        discover_desktop_catalog_path(codex_home)
    )
    state: sqlite3.Connection | None = None
    logs: sqlite3.Connection | None = None
    desktop_catalog: sqlite3.Connection | None = None
    try:
        state = (
            None
            if state_path is None or managed_sqlite_issues(codex_home, state_path)
            else connect_ro(state_path)
        )
        logs = (
            None
            if not include_logs
            or logs_path is None
            or managed_sqlite_issues(codex_home, logs_path)
            else connect_ro(logs_path)
        )
        desktop_catalog = (
            None
            if desktop_catalog_path is None
            or desktop_catalog_discovery_issues
            or managed_sqlite_issues(codex_home, desktop_catalog_path)
            else connect_ro(desktop_catalog_path)
        )
        preflight, blockers = build_preflight(
            codex_home,
            state_path,
            logs_path,
            state_discovery,
            logs_discovery,
            state,
            logs,
            desktop_catalog_path,
            desktop_catalog,
            desktop_catalog_discovery_issues,
            include_subagents,
            include_logs,
        )
        target_ids, edge_status, target_graph_issues = resolve_targets(
            state,
            root_ids,
            include_subagents,
        )
        preflight["target_graph_issues"] = target_graph_issues
        preflight["recursive_expansion_skipped"] = bool(target_graph_issues)
        threads = load_threads(state, target_ids, edge_status)
        plan = Plan(
            codex_home=codex_home,
            root_ids=root_ids,
            target_ids=target_ids,
            state_database_path=state_path,
            logs_database_path=logs_path,
            threads=threads,
            preflight=preflight,
            blockers=blockers,
            include_subagents=include_subagents,
            include_logs=include_logs,
            scan_historical=scan_historical,
            desktop_catalog_path=desktop_catalog_path,
        )
        paginated_assessment = paginated_history_database_assessment(
            codex_home,
            target_ids,
        )
        paginated_path_value = str(paginated_assessment.get("path", ""))
        plan.paginated_history_database_path = (
            Path(paginated_path_value) if paginated_path_value else None
        )
        plan.paginated_history_rows = dict(
            paginated_assessment.get("counts", {})
        )
        plan.paginated_history_contract = dict(
            paginated_assessment.get("contract", {})
        )
        plan.paginated_history_database_plan = dict(
            paginated_assessment.get("database_plan", {})
        )
        plan.preflight["paginated_history_database_discovery"] = (
            paginated_assessment.get("discovery", {})
        )
        plan.preflight["paginated_history_database_check"] = (
            paginated_assessment.get("check", "missing")
        )
        plan.preflight["paginated_history_schema_compatibility"] = (
            plan.paginated_history_database_plan.get("compatibility", {})
        )
        paginated_plan_reasons = [
            str(reason)
            for reason in plan.paginated_history_database_plan.get("reasons", [])
        ]
        paginated_targets = sorted(
            sid
            for sid, info in threads.items()
            if info.rollout_path and info.history_mode == "paginated"
        )
        if (
            paginated_targets
            and plan.paginated_history_database_plan.get("status") != "enabled"
        ):
            paginated_plan_reasons = paginated_plan_reasons or [
                "A paginated target requires a structurally compatible "
                "thread_history database, but none was available."
            ]
        plan.preflight["paginated_target_ids"] = paginated_targets
        plan.preflight["paginated_history_database_issues"] = (
            paginated_plan_reasons
        )
        plan.blockers.extend(paginated_plan_reasons)
        plan.preflight.setdefault("structured_diagnostics", []).extend(
            {
                "code": "paginated_history_runtime_contract_failed",
                "component": COMPONENT_PAGINATED_HISTORY,
                "reason": reason,
                "disposition": DISPOSITION_SKIP_COMPONENT,
                "dependent_components": [],
            }
            for reason in paginated_plan_reasons
        )
        state_compatibility = state_schema_compatibility(state, target_ids)
        logs_compatibility = logs_schema_compatibility(logs, target_ids)
        desktop_compatibility = desktop_catalog_schema_compatibility(
            desktop_catalog,
            target_ids,
        )
        plan.preflight["state_schema_compatibility"] = state_compatibility
        plan.preflight["logs_schema_compatibility"] = logs_compatibility
        plan.preflight["desktop_catalog_schema_compatibility"] = (
            desktop_compatibility
        )
        state_effect_assessment = state_mutation_effect_assessment(
            state,
            target_ids,
        )
        plan.preflight["state_mutation_effect_assessment"] = (
            state_effect_assessment
        )
        state_runtime_issues = (
            []
            if state_effect_assessment.get("status") == "target_only"
            else runtime_schema_target_issues(
                "state database", state_compatibility
            )
        ) + mutation_effect_issues(
            "state database",
            state_effect_assessment,
        )
        logs_runtime_issues = runtime_schema_target_issues(
            "logs database", logs_compatibility
        )
        plan.blockers.extend(state_runtime_issues)
        plan.blockers.extend(logs_runtime_issues)
        plan.preflight.setdefault("structured_diagnostics", []).extend(
            {
                "code": "state_schema_extension_target_reference",
                "component": COMPONENT_CORE,
                "reason": issue,
                "disposition": DISPOSITION_SKIP_COMPONENT,
                "dependent_components": [COMPONENT_HISTORICAL],
            }
            for issue in state_runtime_issues
        )
        plan.preflight.setdefault("structured_diagnostics", []).extend(
            {
                "code": "logs_schema_extension_target_reference",
                "component": COMPONENT_LOGS,
                "reason": issue,
                "disposition": DISPOSITION_SKIP_COMPONENT,
                "dependent_components": [],
            }
            for issue in logs_runtime_issues
        )
        desktop_runtime_issues = runtime_schema_target_issues(
            "Desktop catalog database",
            desktop_compatibility,
        )
        plan.blockers.extend(desktop_runtime_issues)
        for issue in target_graph_issues:
            append_safety_warning(
                plan,
                safety_warning(
                    "recursive_graph_expansion_skipped",
                    issue,
                    COMPONENT_CORE,
                    DISPOSITION_SKIP_COMPONENT,
                ),
            )
        plan.desktop_catalog_rows = desktop_catalog_row_contracts(
            desktop_catalog,
            target_ids,
        )
        plan.desktop_catalog_schema_signature = desktop_catalog_schema_signature(
            desktop_catalog
        )
        plan.desktop_catalog_user_version = desktop_catalog_user_version(
            desktop_catalog
        )
        plan.desktop_catalog_revision = desktop_catalog_revision(desktop_catalog)
        target_host_issues = desktop_catalog_target_host_issues(
            desktop_catalog,
            plan.desktop_catalog_rows,
        )
        plan.preflight["desktop_catalog_target_host_issues"] = target_host_issues
        plan.blockers.extend(target_host_issues)
        (
            plan.global_state_refs,
            plan.global_state_files_present,
            plan.global_state_non_owning_mentions,
            global_state_issues,
            global_state_warnings,
        ) = inspect_global_state_files(codex_home, target_ids)
        plan.preflight["global_state_issues"] = global_state_issues
        plan.preflight["global_state_warnings"] = global_state_warnings
        plan.blockers.extend(global_state_issues)
        plan.warnings.extend(global_state_warnings)
        unsupported_catalog_counts = desktop_catalog_unsupported_target_counts(
            desktop_catalog,
            target_ids,
        )
        plan.preflight["desktop_catalog_unsupported_target_counts"] = (
            unsupported_catalog_counts
        )
        unsupported_catalog_hits = {
            table: count for table, count in unsupported_catalog_counts.items() if count
        }
        if unsupported_catalog_hits:
            plan.blockers.append(
                "Unsupported target-owned desktop catalog references exist: "
                + ", ".join(
                    f"{table}={count}"
                    for table, count in sorted(unsupported_catalog_hits.items())
                )
            )
        auxiliary_assessment = auxiliary_thread_database_assessment(
            codex_home,
            target_ids,
        )
        plan.auxiliary_thread_rows = auxiliary_assessment["counts"]
        plan.auxiliary_thread_contracts = auxiliary_assessment["contracts"]
        plan.auxiliary_thread_databases_present = auxiliary_assessment["presence"]
        plan.auxiliary_thread_database_plans = auxiliary_assessment["database_plans"]
        plan.preflight["auxiliary_thread_database_checks"] = auxiliary_assessment[
            "checks"
        ]
        plan.preflight["auxiliary_thread_database_issues"] = auxiliary_assessment[
            "issues"
        ]
        for filename, database_plan in sorted(
            plan.auxiliary_thread_database_plans.items()
        ):
            if database_plan.get("status") != "skipped":
                continue
            reason = "; ".join(
                str(item) for item in database_plan.get("reasons", [])
            ) or "The auxiliary database is outside the safe mutation contract."
            append_safety_warning(
                plan,
                safety_warning(
                    "auxiliary_database_preserved",
                    reason,
                    COMPONENT_AUXILIARY,
                    DISPOSITION_RETAIN_OBJECT,
                    object_id=filename,
                ),
            )
        for filename, count in sorted(plan.auxiliary_thread_rows.items()):
            path = codex_home / "sqlite" / filename
            if count and not os.access(path, os.W_OK):
                plan.blockers.append(
                    f"Required auxiliary thread database is not writable: {path}"
                )
        plan.initial_state_thread_ids = state_thread_ids(state)
        rollout_path_owners: dict[str, list[str]] = {}
        for owner_sid, owner_path in state_thread_rollout_rows(state):
            if owner_path:
                rollout_path_owners.setdefault(owner_path, []).append(owner_sid)
        plan.preflight["rollout_path_owners"] = {
            path: sorted(set(owners))
            for path, owners in sorted(rollout_path_owners.items())
        }
        protected_ids_encoded = "\n".join(sorted(plan.initial_state_thread_ids)).encode(
            "utf-8"
        )
        plan.preflight["initial_state_thread_count"] = len(
            plan.initial_state_thread_ids
        )
        plan.preflight["initial_state_thread_ids_sha256"] = hashlib.sha256(
            protected_ids_encoded
        ).hexdigest()
        plan.target_incoming_edges = incoming_edge_rows(state, target_ids)
        plan.target_edge_rows = touching_edge_rows(state, target_ids)
        target_id_set = set(target_ids)
        target_rollout_paths = {
            info.rollout_path for info in plan.threads.values() if info.rollout_path
        }
        plan.rollout_migration_state_rows = rollout_migration_state_rows(state)
        plan.rollout_migration_skipped_rows = rollout_migration_skipped_rows_for_paths(
            state, target_rollout_paths
        )
        skipped_path_issues = rollout_migration_skipped_path_issues(
            state,
            target_id_set,
            target_rollout_paths,
        )
        plan.preflight["rollout_migration_skipped_path_issues"] = skipped_path_issues
        plan.blockers.extend(skipped_path_issues)
        unsupported_history_modes = {
            sid: info.history_mode
            for sid, info in plan.threads.items()
            if info.rollout_path and info.history_mode not in {"legacy", "paginated"}
        }
        plan.preflight["unsupported_target_history_modes"] = (
            unsupported_history_modes
        )
        if unsupported_history_modes:
            plan.blockers.append(
                "Target session history_mode is unsupported: "
                + ", ".join(
                    f"{sid}={mode!r}"
                    for sid, mode in sorted(unsupported_history_modes.items())
                )
            )
        plan.open_subagents = sorted(
            {
                endpoint
                for edge in plan.target_edge_rows
                if edge_status_is_open(edge.get("status", ""))
                for endpoint in [
                    edge.get("parent_thread_id", ""),
                    edge.get("child_thread_id", ""),
                ]
                if endpoint in target_id_set
            }
        )
        collect_files(plan)
        ids_set = set(target_ids)
        plan.counts = {
            "target_threads": len(target_ids),
            "state_threads": count_in_table(state, "threads", "id", target_ids),
            "state_thread_spawn_edges": count_edges(state, target_ids),
            "state_thread_dynamic_tools": count_in_table(
                state, "thread_dynamic_tools", "thread_id", target_ids
            ),
            "state_thread_goals": count_in_table(
                state, "thread_goals", "thread_id", target_ids
            ),
            "state_stage1_outputs": count_in_table(
                state, "stage1_outputs", "thread_id", target_ids
            ),
            "state_agent_job_items_assigned": count_in_table(
                state, "agent_job_items", "assigned_thread_id", target_ids
            ),
            "session_index_rows": count_session_index(index_path, ids_set)
            if (
                managed_file_issue(codex_home, index_path) is None
                and not preflight.get("session_index_issues")
            )
            else 0,
            "logs_rows": count_in_table(logs, "logs", "thread_id", target_ids)
            if include_logs
            else 0,
            "rollout_files": len(plan.rollout_files),
            "shell_snapshots": len(plan.shell_snapshots),
            "generated_artifacts": len(plan.generated_artifacts),
            "state_rollout_migration_skipped_rollouts": len(
                plan.rollout_migration_skipped_rows
            ),
            "desktop_catalog_rows": len(
                plan.desktop_catalog_rows.get("local_thread_catalog", [])
            ),
            "desktop_timeline_rows": len(
                plan.desktop_catalog_rows.get("thread_timeline_ledger", [])
            ),
            "desktop_automation_run_rows": len(
                plan.desktop_catalog_rows.get("automation_runs", [])
            ),
            "desktop_inbox_rows": unsupported_catalog_counts.get("inbox_items", 0),
            "desktop_auxiliary_thread_rows": sum(
                plan.auxiliary_thread_rows.values()
            ),
            "paginated_history_projection_rows": plan.paginated_history_rows.get(
                "thread_history_projection_state", 0
            ),
            "paginated_history_turn_rows": plan.paginated_history_rows.get(
                "thread_turns", 0
            ),
            "paginated_history_item_rows": plan.paginated_history_rows.get(
                "thread_items", 0
            ),
            "global_state_structural_refs": sum(
                len(entries) for entries in plan.global_state_refs.values()
            ),
            "global_state_non_owning_text_mentions": sum(
                len(entries)
                for entries in plan.global_state_non_owning_mentions.values()
            ),
        }
        desktop_owners, desktop_owner_issue = desktop_owner_processes(codex_home)
        plan.preflight["desktop_offline_required"] = plan_requires_desktop_offline(plan)
        plan.preflight["desktop_owner_processes"] = desktop_owners
        plan.preflight["desktop_owner_detection_issue"] = desktop_owner_issue
        if plan.preflight["desktop_offline_required"]:
            if desktop_owner_issue:
                plan.warnings.append(desktop_owner_issue)
            elif desktop_owners:
                plan.warnings.append(
                    "Codex Desktop must be fully exited before apply because it owns "
                    "the approved catalog/global UI state."
                )
        if desktop_catalog is not None:
            catalog_titles = {
                str(row["thread_id"]): str(row.get("display_title", ""))
                for row in plan.desktop_catalog_rows.get("local_thread_catalog", [])
            }
            for sid, title in catalog_titles.items():
                if sid in plan.threads and not plan.threads[sid].title:
                    plan.threads[sid].title = title
        missing_roots = [
            sid
            for sid in root_ids
            if sid not in threads or not threads[sid].rollout_path
        ]
        if missing_roots:
            plan.warnings.append(
                "Some root IDs were not found in the selected state database; "
                "filesystem matching was still attempted."
            )
        if plan.unsafe_paths:
            plan.blockers.append(
                "Unsafe symbolic-link, escaped, or non-file artifact paths were discovered."
            )
        for path in (
            plan.rollout_files + plan.shell_snapshots + plan.generated_artifacts
        ):
            if path_is_present(path) and not os.access(path.parent, os.W_OK | os.X_OK):
                plan.blockers.append(
                    f"Artifact parent directory is not writable: {path.parent}"
                )
        noncanonical = noncanonical_target_references(
            state,
            logs,
            desktop_catalog,
            target_ids,
            include_logs,
            state_path.name if state_path is not None else "selected state database",
            logs_path.name if logs_path is not None else "selected logs database",
        )
        plan.preflight["noncanonical_target_references"] = noncanonical
        if noncanonical:
            plan.blockers.append(
                "Target session references use non-canonical letter casing in SQLite; "
                "refusing a case-sensitive partial deletion."
            )
        current_thread_id = os.environ.get("CODEX_THREAD_ID", "")
        current_session_is_target = bool(
            CANONICAL_UUID_RE.fullmatch(current_thread_id)
            and current_thread_id.lower() in ids_set
        )
        plan.preflight["current_session_is_target"] = current_session_is_target
        if current_session_is_target:
            plan.blockers.append(
                "The current Codex session is inside the deletion target set; "
                "run the report and apply command from a different session."
            )
        evidence_keys = [
            "state_threads",
            "state_thread_spawn_edges",
            "state_thread_dynamic_tools",
            "state_thread_goals",
            "state_stage1_outputs",
            "state_agent_job_items_assigned",
            "session_index_rows",
            "logs_rows",
            "rollout_files",
            "shell_snapshots",
            "generated_artifacts",
            "state_rollout_migration_skipped_rollouts",
            "desktop_catalog_rows",
            "desktop_timeline_rows",
            "desktop_automation_run_rows",
            "desktop_inbox_rows",
            "desktop_auxiliary_thread_rows",
            "paginated_history_projection_rows",
            "paginated_history_turn_rows",
            "paginated_history_item_rows",
            "global_state_structural_refs",
        ]
        plan.preflight["target_evidence_items"] = sum(
            plan.counts.get(key, 0) for key in evidence_keys
        )
        if plan.preflight["target_evidence_items"] == 0:
            plan.blockers.append(
                "No requested session ID matched state rows, index rows, logs, or artifacts."
            )
        plan.historical_residuals = (
            scan_historical_residuals(codex_home, set(target_ids), include_logs)
            if scan_historical
            else skipped_historical_scan(
                "Historical scan was disabled by --no-historical-scan."
            )
        )
        plan.blockers = sorted(dict.fromkeys(plan.blockers))
        structured = plan.preflight.setdefault("structured_diagnostics", [])

        def extend_plan_diagnostics(
            code: str,
            component: str,
            reasons: Iterable[str],
            dependent_components: Iterable[str] = (),
        ) -> None:
            existing = {
                str(item.get("reason", ""))
                for item in structured
                if isinstance(item, dict)
            }
            structured.extend(
                {
                    "code": code,
                    "component": component,
                    "reason": str(reason),
                    "disposition": DISPOSITION_SKIP_COMPONENT,
                    "dependent_components": sorted(set(dependent_components)),
                }
                for reason in reasons
                if str(reason) not in existing
            )

        extend_plan_diagnostics(
            "desktop_catalog_runtime_contract_failed",
            COMPONENT_CATALOG,
            [*desktop_runtime_issues, *target_host_issues],
        )
        extend_plan_diagnostics(
            "global_state_runtime_contract_failed",
            COMPONENT_GLOBAL_STATE,
            global_state_issues,
        )
        extend_plan_diagnostics(
            "state_runtime_contract_failed",
            COMPONENT_CORE,
            skipped_path_issues,
            [COMPONENT_HISTORICAL],
        )
        existing_reasons = {
            str(item.get("reason", ""))
            for item in structured
            if isinstance(item, dict)
        }
        extend_plan_diagnostics(
            "general_runtime_contract_failed",
            COMPONENT_CORE,
            [reason for reason in plan.blockers if reason not in existing_reasons],
            [COMPONENT_HISTORICAL],
        )
        finalize_plan_execution_model(plan)
        plan.fingerprint = compute_plan_fingerprint(plan)
        return plan
    finally:
        if desktop_catalog is not None:
            desktop_catalog.close()
        if logs is not None:
            logs.close()
        if state is not None:
            state.close()


def rewrite_session_index(
    codex_home: Path,
    target_ids: set[str],
    mutation_observer: Any | None = None,
) -> int:
    index_path = codex_home / "session_index.jsonl"
    require_managed_file(codex_home, index_path)
    issues = session_index_issues(index_path)
    if issues:
        raise RuntimeError("Invalid session_index.jsonl: " + " | ".join(issues))
    if not index_path.exists():
        return 0
    fd = os.open(
        index_path,
        os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError("session_index.jsonl is not a regular file")
        candidates: list[tuple[int, bytes]] = []
        with os.fdopen(os.dup(fd), "rb") as inp:
            offset = 0
            while line_bytes := inp.readline():
                line = line_bytes.decode("utf-8")
                sid = parse_index_id(line)
                if sid in target_ids:
                    candidates.append((offset, line_bytes))
                offset += len(line_bytes)
        for offset, expected in candidates:
            if os.pread(fd, len(expected), offset) != expected:
                raise RuntimeError("A target session-index row changed during deletion")
            notify_mutation(mutation_observer, COMPONENT_CORE)
            overwrite_index_region(fd, blank_index_line(expected), offset)
        os.fsync(fd)
        current = index_path.lstat()
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError(
                "session_index.jsonl was replaced during deletion; the replacement was preserved"
            )
        return len(candidates)
    finally:
        os.close(fd)


def blank_index_line(line: bytes) -> bytes:
    if line.endswith(b"\r\n"):
        return b" " * (len(line) - 2) + b"\r\n"
    if line.endswith(b"\n"):
        return b" " * (len(line) - 1) + b"\n"
    return b" " * len(line)


def overwrite_index_region(fd: int, replacement: bytes, offset: int) -> None:
    written_total = 0
    while written_total < len(replacement):
        written = os.pwrite(
            fd,
            replacement[written_total:],
            offset + written_total,
        )
        if written <= 0:
            raise OSError(errno.EIO, "Short write while updating session_index.jsonl")
        written_total += written
    if os.pread(fd, len(replacement), offset) != replacement:
        raise OSError(
            errno.EIO,
            "session_index.jsonl did not contain the verified replacement bytes",
        )


def rewrite_approved_session_index_rows(
    codex_home: Path,
    approved_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    index_path = codex_home / "session_index.jsonl"
    require_managed_file(codex_home, index_path)
    issues = session_index_issues(index_path)
    if issues:
        raise RuntimeError("Invalid session_index.jsonl: " + " | ".join(issues))
    contracts: dict[int, tuple[str, str]] = {}
    for entry in approved_entries:
        sid = str(entry.get("id", "")).lower()
        for contract in entry.get("line_contracts", []):
            line_number = int(contract["line_number"])
            identity = (sid, str(contract["content_sha256"]))
            if line_number in contracts and contracts[line_number] != identity:
                raise RuntimeError(
                    "Approved session-index snapshot contains conflicting line identities."
                )
            contracts[line_number] = identity
    if not contracts:
        return {
            "removed": 0,
            "already_absent": 0,
            "identity_changed_retained": 0,
        }
    if not index_path.exists():
        return {
            "removed": 0,
            "already_absent": len(contracts),
            "identity_changed_retained": 0,
        }

    fd = os.open(
        index_path,
        os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
    )
    removed = 0
    changed = 0
    seen_lines: set[int] = set()
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError("session_index.jsonl is not a regular file")
        candidates: list[tuple[int, bytes]] = []
        with os.fdopen(os.dup(fd), "rb") as inp:
            offset = 0
            for line_number, line_bytes in enumerate(inp, 1):
                approved = contracts.get(line_number)
                if approved is None:
                    offset += len(line_bytes)
                    continue
                seen_lines.add(line_number)
                sid, expected_sha256 = approved
                line = line_bytes.decode("utf-8")
                actual_sha256 = hashlib.sha256(line_bytes).hexdigest()
                if parse_index_id(line) == sid and actual_sha256 == expected_sha256:
                    candidates.append((offset, line_bytes))
                else:
                    changed += 1
                offset += len(line_bytes)
        for offset, expected in candidates:
            if os.pread(fd, len(expected), offset) != expected:
                changed += 1
                continue
            overwrite_index_region(fd, blank_index_line(expected), offset)
            removed += 1
        os.fsync(fd)
        current = index_path.lstat()
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError(
                "session_index.jsonl was replaced during historical cleanup; "
                "the replacement was preserved"
            )
        return {
            "removed": removed,
            "already_absent": len(set(contracts) - seen_lines),
            "identity_changed_retained": changed,
        }
    finally:
        os.close(fd)


def delete_rollout_migration_skipped_rows_on_conn(
    conn: sqlite3.Connection,
    approved_rows: list[dict[str, Any]],
) -> int:
    if not approved_rows:
        return 0
    if not table_exists(conn, "rollout_migration_skipped_rollouts"):
        raise RuntimeError(
            "rollout_migration_skipped_rollouts disappeared before deletion"
        )
    select_columns = ", ".join(
        quote_ident(column) for column in ROLLOUT_MIGRATION_SKIPPED_COLUMNS
    )
    removed = 0
    for approved in sorted(
        approved_rows,
        key=lambda row: (str(row["migration_id"]), str(row["rollout_path"])),
    ):
        migration_id = approved.get("migration_id")
        rollout_path = approved.get("rollout_path")
        row = conn.execute(
            f"SELECT {select_columns} FROM rollout_migration_skipped_rollouts "
            "WHERE migration_id=? AND rollout_path=?",
            (migration_id, rollout_path),
        ).fetchone()
        actual = (
            dict(zip(ROLLOUT_MIGRATION_SKIPPED_COLUMNS, row, strict=True))
            if row is not None
            else None
        )
        if actual != approved:
            raise RuntimeError(
                "Approved rollout migration skipped-row identity changed before deletion"
            )
        cur = conn.execute(
            "DELETE FROM rollout_migration_skipped_rollouts "
            "WHERE migration_id=? AND rollout_path=?",
            (migration_id, rollout_path),
        )
        if cur.rowcount != 1:
            raise RuntimeError(
                "Approved rollout migration skipped row changed during deletion"
            )
        removed += 1
    return removed


def delete_state_rows_on_conn(
    conn: sqlite3.Connection, target_ids: list[str]
) -> dict[str, int]:
    result: dict[str, int] = {}
    if not target_ids:
        return result
    if table_exists(conn, "thread_spawn_edges"):
        sql = (
            "DELETE FROM thread_spawn_edges "
            f"WHERE parent_thread_id IN ({placeholders(target_ids)}) "
            f"OR child_thread_id IN ({placeholders(target_ids)})"
        )
        cur = conn.execute(sql, target_ids + target_ids)
        result["thread_spawn_edges"] = cur.rowcount
    for table, column in [
        ("thread_dynamic_tools", "thread_id"),
        ("thread_goals", "thread_id"),
        ("stage1_outputs", "thread_id"),
    ]:
        if table_exists(conn, table) and column in columns(conn, table):
            cur = conn.execute(
                f"DELETE FROM {quote_ident(table)} "
                f"WHERE {quote_ident(column)} IN ({placeholders(target_ids)})",
                target_ids,
            )
            result[table] = cur.rowcount
    if table_exists(conn, "agent_job_items") and "assigned_thread_id" in columns(
        conn, "agent_job_items"
    ):
        cur = conn.execute(
            "UPDATE agent_job_items SET assigned_thread_id=NULL "
            f"WHERE assigned_thread_id IN ({placeholders(target_ids)})",
            target_ids,
        )
        result["agent_job_items_unassigned"] = cur.rowcount
    if table_exists(conn, "threads"):
        cur = conn.execute(
            f"DELETE FROM threads WHERE id IN ({placeholders(target_ids)})",
            target_ids,
        )
        result["threads"] = cur.rowcount
    return result


def delete_state_rows(codex_home: Path, target_ids: list[str]) -> dict[str, int]:
    state_path = discovered_database_path(discover_state_database(codex_home))
    if state_path is None:
        return {}
    require_managed_sqlite(codex_home, state_path)
    conn = connect_rw(state_path)
    if conn is None:
        return {}
    try:
        conn.execute("BEGIN IMMEDIATE")
        issues = state_runtime_mutation_issues(conn, target_ids)
        if issues:
            raise RuntimeError(
                "State schema changed before deletion: " + " | ".join(issues)
            )
        result = delete_state_rows_on_conn(conn, target_ids)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_target_index_and_state(
    plan: Plan,
    historical_state_cleanup_ids: set[str] | None = None,
    mutation_observer: Any | None = None,
) -> tuple[int, dict[str, int]]:
    state_path = plan.state_database_path
    current_state_path = discovered_database_path(
        discover_state_database(plan.codex_home)
    )
    if current_state_path != state_path:
        raise RuntimeError(
            "The selected state database changed after the approved report"
        )
    index_path = plan.codex_home / "session_index.jsonl"
    state_present = state_path is not None and path_is_present(state_path)
    index_present = path_is_present(index_path)
    if state_present != bool(plan.preflight.get("state_db_present")):
        raise RuntimeError("State database presence changed after the approved report")
    if index_present != bool(plan.preflight.get("session_index_present")):
        raise RuntimeError(
            "session_index.jsonl presence changed after the approved report"
        )

    require_managed_file(plan.codex_home, index_path)
    if not state_present:
        return (
            rewrite_session_index(
                plan.codex_home,
                set(plan.target_ids),
                mutation_observer,
            ),
            {},
        )

    if state_path is None:
        raise RuntimeError("The approved state database path is unavailable")
    require_managed_sqlite(plan.codex_home, state_path)
    conn = connect_rw(state_path)
    if conn is None:
        raise RuntimeError("The selected state database disappeared before deletion")
    try:
        conn.execute("BEGIN IMMEDIATE")
        issues = state_runtime_mutation_issues(
            conn,
            set(plan.target_ids) | set(historical_state_cleanup_ids or set()),
        )
        if issues:
            raise RuntimeError(
                "State schema changed before deletion: " + " | ".join(issues)
            )
        current_ids, _current_status, current_graph_issues = resolve_targets(
            conn,
            plan.root_ids,
            plan.include_subagents,
        )
        if current_graph_issues:
            raise RuntimeError(" | ".join(current_graph_issues))
        historical_cleanup_ids = historical_state_cleanup_ids or set()
        expected_incoming_edges: dict[str, list[dict[str, str]]] = {}
        for child_id, edges in plan.target_incoming_edges.items():
            if child_id in historical_cleanup_ids:
                continue
            retained_edges = [
                edge
                for edge in edges
                if edge.get("parent_thread_id", "") not in historical_cleanup_ids
            ]
            if retained_edges:
                expected_incoming_edges[child_id] = retained_edges
        expected_edge_rows = [
            edge
            for edge in plan.target_edge_rows
            if edge.get("parent_thread_id", "") not in historical_cleanup_ids
            and edge.get("child_thread_id", "") not in historical_cleanup_ids
        ]
        current_incoming_edges = incoming_edge_rows(conn, current_ids)
        current_edge_rows = touching_edge_rows(conn, current_ids)
        if (
            current_ids != plan.target_ids
            or current_incoming_edges != expected_incoming_edges
            or current_edge_rows != expected_edge_rows
        ):
            raise RuntimeError(
                "Target/subagent graph, touching edges, or edge status changed after "
                "the approved report"
            )
        current_threads = load_threads(conn, current_ids, _current_status)
        planned_thread_storage = {
            sid: (info.rollout_path, info.history_mode)
            for sid, info in sorted(plan.threads.items())
        }
        current_thread_storage = {
            sid: (info.rollout_path, info.history_mode)
            for sid, info in sorted(current_threads.items())
        }
        if current_thread_storage != planned_thread_storage:
            raise RuntimeError(
                "Target rollout_path or history_mode changed after the approved report"
            )
        target_rollout_paths = {
            info.rollout_path for info in current_threads.values() if info.rollout_path
        }
        skipped_path_issues = rollout_migration_skipped_path_issues(
            conn,
            set(current_ids),
            target_rollout_paths,
        )
        if skipped_path_issues:
            raise RuntimeError(" | ".join(skipped_path_issues))
        current_skipped_rows = rollout_migration_skipped_rows_for_paths(
            conn,
            target_rollout_paths,
        )
        if current_skipped_rows != plan.rollout_migration_skipped_rows:
            raise RuntimeError(
                "Target rollout migration skipped-row scope changed after the approved report"
            )
        noncanonical = noncanonical_target_references(
            conn,
            None,
            None,
            plan.target_ids,
            False,
        )
        if noncanonical:
            raise RuntimeError(
                "Non-canonical target references appeared after the approved report"
            )
        will_mutate_state = bool(
            plan.rollout_migration_skipped_rows
            or count_edges(conn, plan.target_ids)
            or any(
                count_in_table(conn, table, column, plan.target_ids)
                for table, column in STATE_REFERENCE_LOCATIONS
                if table != "thread_spawn_edges"
            )
        )
        if will_mutate_state:
            notify_mutation(mutation_observer, COMPONENT_CORE)
        skipped_removed = delete_rollout_migration_skipped_rows_on_conn(
            conn,
            plan.rollout_migration_skipped_rows,
        )
        state_deleted = delete_state_rows_on_conn(conn, plan.target_ids)
        state_deleted["rollout_migration_skipped_rollouts"] = skipped_removed
        index_removed = rewrite_session_index(
            plan.codex_home,
            set(plan.target_ids),
            mutation_observer,
        )
        conn.commit()
        return index_removed, state_deleted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_desktop_catalog_contract_row(
    conn: sqlite3.Connection,
    table: str,
    locator_columns: tuple[str, ...],
    approved: dict[str, Any],
) -> None:
    column_names = ordered_columns(conn, table)
    where = " AND ".join(f"{quote_ident(name)}=?" for name in locator_columns)
    locator_values = [approved[name] for name in locator_columns]
    row = conn.execute(
        f"SELECT * FROM {quote_ident(table)} WHERE {where}",
        locator_values,
    ).fetchone()
    if row is None or sqlite_row_sha256(column_names, row) != approved["row_sha256"]:
        raise RuntimeError(
            f"Approved desktop catalog row identity changed before deletion: {table}"
        )
    cur = conn.execute(
        f"DELETE FROM {quote_ident(table)} WHERE {where}",
        locator_values,
    )
    if cur.rowcount != 1:
        raise RuntimeError(
            f"Approved desktop catalog row changed during deletion: {table}"
        )


def apply_target_desktop_catalog(
    plan: Plan,
    mutation_observer: Any | None = None,
) -> dict[str, Any]:
    require_desktop_offline(plan)
    planned_path = plan.desktop_catalog_path
    current_path, discovery_issues = discover_desktop_catalog_path(plan.codex_home)
    if discovery_issues:
        raise RuntimeError(" | ".join(discovery_issues))
    if (planned_path is None) != (current_path is None) or (
        planned_path is not None and current_path != planned_path
    ):
        raise RuntimeError(
            "Desktop catalog database presence or selected path changed after approval"
        )
    if planned_path is None:
        return {
            "database_present": False,
            "rows_removed": {},
            "catalog_revision_before": None,
            "catalog_revision_after": None,
            "catalog_revision_increment": 0,
            "observation_sequence_increments": {},
        }
    require_managed_sqlite(plan.codex_home, planned_path)
    conn = connect_rw(planned_path)
    if conn is None:
        raise RuntimeError("Desktop catalog database disappeared before deletion")
    try:
        conn.execute("BEGIN IMMEDIATE")
        issues = desktop_catalog_runtime_mutation_issues(conn, plan.target_ids)
        if issues:
            raise RuntimeError(
                "Desktop catalog schema changed before deletion: " + " | ".join(issues)
            )
        if (
            desktop_catalog_schema_signature(conn)
            != plan.desktop_catalog_schema_signature
        ):
            raise RuntimeError(
                "Desktop catalog schema signature changed after the approved report"
            )
        if desktop_catalog_user_version(conn) != plan.desktop_catalog_user_version:
            raise RuntimeError(
                "Desktop catalog user_version changed after the approved report"
            )
        current_contracts = desktop_catalog_row_contracts(conn, plan.target_ids)
        if current_contracts != plan.desktop_catalog_rows:
            raise RuntimeError(
                "Target desktop catalog or timeline rows changed after the approved report"
            )
        target_host_issues = desktop_catalog_target_host_issues(
            conn,
            current_contracts,
        )
        if target_host_issues:
            raise RuntimeError(
                "Desktop catalog target host state changed before deletion: "
                + " | ".join(target_host_issues)
            )
        unsupported_counts = desktop_catalog_unsupported_target_counts(
            conn,
            plan.target_ids,
        )
        if any(unsupported_counts.values()):
            raise RuntimeError(
                "Unsupported target-owned desktop catalog references appeared before deletion"
            )

        catalog_contracts = plan.desktop_catalog_rows.get("local_thread_catalog", [])
        host_increments: dict[str, int] = {}
        for contract in catalog_contracts:
            host_id = str(contract["host_id"])
            host_increments[host_id] = host_increments.get(host_id, 0) + 1
        host_sequences_before: dict[str, int] = {}
        for host_id, increment in sorted(host_increments.items()):
            row = conn.execute(
                "SELECT observation_sequence FROM local_thread_catalog_sync_state "
                "WHERE host_id=?",
                (host_id,),
            ).fetchone()
            if (
                row is None
                or not isinstance(row[0], int)
                or int(row[0]) < 0
                or int(row[0]) > SQLITE_MAX_INTEGER - 1 - increment
            ):
                raise RuntimeError(
                    f"Desktop catalog sync state is invalid for host {host_id}"
                )
            host_sequences_before[host_id] = int(row[0])

        revision_before = desktop_catalog_revision(conn)
        visible_removed = sum(
            1
            for contract in catalog_contracts
            if int(contract.get("missing_candidate", 1)) == 0
        )
        if visible_removed and revision_before is None:
            raise RuntimeError(
                "Desktop catalog metadata disappeared before revision update"
            )
        require_desktop_offline(plan)
        if any(plan.desktop_catalog_rows.values()):
            notify_mutation(mutation_observer, COMPONENT_CATALOG)

        for host_id, increment in sorted(host_increments.items()):
            sequence_before = host_sequences_before[host_id]
            cur = conn.execute(
                "UPDATE local_thread_catalog_sync_state "
                "SET observation_sequence=observation_sequence+? "
                "WHERE host_id=? AND observation_sequence=? "
                "AND typeof(observation_sequence)='integer'",
                (increment, host_id, sequence_before),
            )
            if cur.rowcount != 1:
                raise RuntimeError(
                    f"Desktop catalog sync state changed for host {host_id}"
                )
            updated_row = conn.execute(
                "SELECT observation_sequence FROM local_thread_catalog_sync_state "
                "WHERE host_id=?",
                (host_id,),
            ).fetchone()
            if (
                updated_row is None
                or not isinstance(updated_row[0], int)
                or int(updated_row[0]) != sequence_before + increment
            ):
                raise RuntimeError(
                    f"Desktop catalog sync sequence update failed for host {host_id}"
                )

        removed: dict[str, int] = {}
        for table in [
            "thread_timeline_ledger",
            "automation_runs",
            "local_thread_catalog",
        ]:
            contracts = plan.desktop_catalog_rows.get(table, [])
            locator_columns = DESKTOP_CATALOG_DELETABLE_TABLES[table]
            for contract in contracts:
                delete_desktop_catalog_contract_row(
                    conn,
                    table,
                    locator_columns,
                    contract,
                )
            removed[table] = len(contracts)

        if visible_removed:
            cur = conn.execute(
                "UPDATE local_thread_catalog_metadata "
                "SET catalog_revision=catalog_revision+? WHERE id=1",
                (visible_removed,),
            )
            if cur.rowcount != 1:
                raise RuntimeError(
                    "Desktop catalog revision changed during target deletion"
                )
        revision_after = desktop_catalog_revision(conn)
        if revision_before is None:
            revision_update_ok = revision_after is None and visible_removed == 0
        else:
            revision_update_ok = revision_after == revision_before + visible_removed
        if not revision_update_ok:
            raise RuntimeError("Desktop catalog revision update was not durable")
        require_desktop_offline(plan)
        conn.commit()
        return {
            "database_present": True,
            "rows_removed": removed,
            "catalog_revision_before": revision_before,
            "catalog_revision_after": revision_after,
            "catalog_revision_increment": visible_removed,
            "observation_sequence_increments": host_increments,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_auxiliary_contract_row(
    conn: sqlite3.Connection,
    filename: str,
    table: str,
    primary_key: list[str],
    approved: dict[str, Any],
) -> None:
    column_names = ordered_columns(conn, table)
    where = " AND ".join(f"{quote_ident(name)}=?" for name in primary_key)
    locator_values = [approved[name] for name in primary_key]
    row = conn.execute(
        f"SELECT * FROM {quote_ident(table)} WHERE {where}",
        locator_values,
    ).fetchone()
    if row is None or sqlite_row_sha256(column_names, row) != approved["row_sha256"]:
        raise RuntimeError(
            "Approved auxiliary thread row identity changed before deletion: "
            f"{filename}:{table}"
        )
    cur = conn.execute(
        f"DELETE FROM {quote_ident(table)} WHERE {where}",
        locator_values,
    )
    if cur.rowcount != 1:
        raise RuntimeError(
            "Approved auxiliary thread row changed during deletion: "
            f"{filename}:{table}"
        )


def apply_target_auxiliary_databases(
    plan: Plan,
    mutation_observer: Any | None = None,
) -> dict[str, Any]:
    current_discovered, current_discovery_issues = (
        discover_auxiliary_thread_database_inventory(plan.codex_home)
    )
    if current_discovery_issues:
        raise RuntimeError(
            "Auxiliary database discovery changed after approval: "
            + " | ".join(current_discovery_issues)
        )
    current_presence = sorted(current_discovered)
    if current_presence != plan.auxiliary_thread_databases_present:
        raise RuntimeError(
            "Auxiliary desktop thread database presence changed after approval"
        )
    if set(plan.auxiliary_thread_database_plans) != set(current_presence):
        raise RuntimeError("Approved auxiliary thread database plan is incomplete")

    current_assessment = auxiliary_thread_database_assessment(
        plan.codex_home,
        plan.target_ids,
        "quick_check",
    )

    removed_by_database: dict[str, int] = {}
    checks: dict[str, str] = {}
    database_results: dict[str, dict[str, Any]] = {}
    for filename in current_presence:
        table, column, max_user_version = current_discovered[filename]
        approved_plan = plan.auxiliary_thread_database_plans[filename]
        current_plan = current_assessment["database_plans"].get(filename, {})
        if approved_plan.get("status") != "enabled":
            reason = "; ".join(
                str(item) for item in approved_plan.get("reasons", [])
            ) or "The database was preserved by the approved plan."
            database_results[filename] = {
                "status": "skipped_safely",
                "reason": reason,
                "mutation_started": False,
            }
            checks[filename] = current_assessment["checks"].get(filename, "unsafe")
            continue
        if current_plan.get("status") != "enabled":
            reason = "; ".join(
                str(item) for item in current_plan.get("reasons", [])
            ) or "The database no longer matches its safe mutation anchors."
            database_results[filename] = {
                "status": "skipped_safely",
                "reason": reason,
                "mutation_started": False,
            }
            checks[filename] = current_assessment["checks"].get(filename, "unsafe")
            continue
        expected = plan.auxiliary_thread_contracts.get(filename)
        if not isinstance(expected, dict) or current_plan.get(
            "preserved_contract"
        ) != expected:
            database_results[filename] = {
                "status": "skipped_safely",
                "reason": "The database schema or target rows changed after approval.",
                "mutation_started": False,
            }
            checks[filename] = current_assessment["checks"].get(filename, "unsafe")
            continue
        expected_metadata = {
            "table": table,
            "thread_column": column,
            "max_user_version": max_user_version,
            "primary_key": THREAD_AUXILIARY_PRIMARY_KEYS[table],
        }
        if any(expected.get(key) != value for key, value in expected_metadata.items()):
            raise RuntimeError(
                f"Auxiliary thread database metadata changed after approval: {filename}"
            )
        path = plan.codex_home / "sqlite" / filename
        require_managed_sqlite(plan.codex_home, path)
        approved_rows = expected.get("rows", [])
        if not approved_rows:
            removed_by_database[filename] = 0
            checks[filename] = database_check(path, "integrity_check")
            if checks[filename] != "ok":
                raise RuntimeError(
                    "Auxiliary thread database integrity_check failed: "
                    f"{filename}: {checks[filename]}"
                )
            database_results[filename] = {
                "status": "completed",
                "rows_removed": 0,
                "mutation_started": False,
            }
            continue
        conn = connect_rw(path)
        if conn is None:
            raise RuntimeError(
                f"Auxiliary thread database disappeared before deletion: {filename}"
            )
        try:
            conn.execute("BEGIN IMMEDIATE")
            issues, _compatibility = auxiliary_thread_runtime_issues(
                conn,
                filename,
                table,
                column,
                plan.target_ids,
            )
            if issues:
                raise RuntimeError(
                    f"Auxiliary thread database changed before deletion: {filename}: "
                    + " | ".join(issues)
                )
            if int(conn.execute("PRAGMA user_version").fetchone()[0]) != expected.get(
                "user_version"
            ):
                raise RuntimeError(
                    f"Auxiliary thread database user_version changed: {filename}"
                )
            if auxiliary_thread_schema_signature(conn) != expected.get(
                "schema_signature"
            ):
                raise RuntimeError(
                    f"Auxiliary thread database schema changed: {filename}"
                )
            current_rows = auxiliary_thread_row_contracts(
                conn,
                table,
                column,
                plan.target_ids,
            )
            if current_rows != approved_rows:
                raise RuntimeError(
                    f"Target auxiliary thread rows changed after approval: {filename}"
                )
            require_desktop_offline(plan)
            notify_mutation(mutation_observer, COMPONENT_AUXILIARY)
            primary_key = THREAD_AUXILIARY_PRIMARY_KEYS[table]
            for contract in approved_rows:
                delete_auxiliary_contract_row(
                    conn,
                    filename,
                    table,
                    primary_key,
                    contract,
                )
            if auxiliary_thread_row_contracts(
                conn,
                table,
                column,
                plan.target_ids,
            ):
                raise RuntimeError(
                    f"Auxiliary target rows remain before commit: {filename}"
                )
            require_desktop_offline(plan)
            conn.commit()
            removed_by_database[filename] = len(approved_rows)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        checks[filename] = database_check(path, "integrity_check")
        if checks[filename] != "ok":
            raise RuntimeError(
                f"Auxiliary thread database integrity_check failed after deletion: "
                f"{filename}: {checks[filename]}"
            )
        database_results[filename] = {
            "status": "completed",
            "rows_removed": len(approved_rows),
            "mutation_started": True,
        }
    verification_plans: dict[str, dict[str, Any]] = {}
    for filename, current_plan in current_assessment["database_plans"].items():
        verification_plan = dict(current_plan)
        if database_results.get(filename, {}).get("status") == "skipped_safely":
            verification_plan["status"] = "skipped"
        verification_plans[filename] = verification_plan
    return {
        "databases_present": current_presence,
        "rows_removed": removed_by_database,
        "integrity_checks": checks,
        "database_results": database_results,
        "verification_plans": verification_plans,
    }


def delete_log_rows_on_conn(
    conn: sqlite3.Connection,
    target_ids: list[str],
) -> int:
    if not target_ids:
        return 0
    cur = conn.execute(
        f"DELETE FROM logs WHERE thread_id IN ({placeholders(target_ids)})",
        target_ids,
    )
    return cur.rowcount


def delete_approved_log_rows_on_conn(
    conn: sqlite3.Connection,
    approved_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    column_names = ordered_columns(conn, "logs")
    id_position = column_names.index("id")
    thread_position = column_names.index("thread_id")
    approved_contracts: dict[int, tuple[str, str]] = {}
    for entry in approved_entries:
        sid = str(entry.get("id", "")).lower()
        for contract in entry.get("row_contracts", []):
            row_id = int(contract["row_id"])
            identity = (sid, str(contract["row_sha256"]))
            if row_id in approved_contracts and approved_contracts[row_id] != identity:
                raise RuntimeError(
                    "Approved log snapshot contains conflicting row identities."
                )
            approved_contracts[row_id] = identity

    deletable: list[int] = []
    already_absent = 0
    identity_changed = 0
    for row_id, (sid, expected_sha256) in sorted(approved_contracts.items()):
        row = conn.execute("SELECT * FROM logs WHERE id=?", (row_id,)).fetchone()
        if row is None:
            already_absent += 1
            continue
        actual_thread_id = row[thread_position]
        actual_sha256 = sqlite_row_sha256(column_names, row)
        if (
            actual_thread_id is not None
            and str(actual_thread_id).lower() == sid
            and actual_sha256 == expected_sha256
        ):
            deletable.append(int(row[id_position]))
        else:
            identity_changed += 1
    removed = 0
    for offset in range(0, len(deletable), 500):
        batch = deletable[offset : offset + 500]
        cur = conn.execute(
            f"DELETE FROM logs WHERE id IN ({placeholders([str(item) for item in batch])})",
            batch,
        )
        removed += cur.rowcount
    if deletable:
        if removed != len(deletable):
            raise RuntimeError("Approved log rows changed during deletion.")
    return {
        "removed": removed,
        "already_absent": already_absent,
        "identity_changed_retained": identity_changed,
        "approved_row_ids": sorted(approved_contracts),
    }


def apply_target_paginated_history(
    plan: Plan,
    mutation_observer: Any | None = None,
) -> dict[str, Any]:
    approved_contract = plan.paginated_history_contract
    approved_rows = approved_contract.get("rows", {})
    approved_total = sum(
        len(rows) for rows in approved_rows.values() if isinstance(rows, list)
    )
    if not approved_total:
        return {
            "database_present": plan.paginated_history_database_path is not None,
            "rows_removed": {
                table: 0 for table in PAGINATED_HISTORY_PRIMARY_KEYS
            },
            "already_absent": 0,
            "integrity_check": (
                "missing"
                if plan.paginated_history_database_path is None
                else "not_requested"
            ),
        }

    expected_path = plan.paginated_history_database_path
    current_path = discovered_database_path(
        discover_paginated_history_database(plan.codex_home)
    )
    if expected_path is None or current_path != expected_path:
        raise RuntimeError(
            "The selected paginated history database changed after approval"
        )
    require_managed_sqlite(plan.codex_home, expected_path)
    conn = connect_rw(expected_path)
    if conn is None:
        raise RuntimeError(
            "The selected paginated history database disappeared before deletion"
        )
    try:
        conn.execute("BEGIN IMMEDIATE")
        issues, compatibility = paginated_history_runtime_issues(
            conn,
            expected_path.name,
            plan.target_ids,
        )
        if issues:
            raise RuntimeError(
                "Paginated history schema changed before deletion: "
                + " | ".join(issues)
            )
        current_signature = sqlite_schema_signature(conn, include_indexes=True)
        if current_signature != approved_contract.get("schema_signature"):
            raise RuntimeError(
                "Paginated history schema identity changed after approval"
            )
        if paginated_prewrite_comparison_contract(
            compatibility.get("mutation_effect_assessment", {})
        ) != paginated_prewrite_comparison_contract(
            approved_contract.get("mutation_effect_assessment", {})
        ):
            raise RuntimeError(
                "Paginated history mutation effect envelope changed after approval"
            )
        current_rows = paginated_history_row_contracts(conn, plan.target_ids)
        already_absent = 0
        for table, primary_key in PAGINATED_HISTORY_PRIMARY_KEYS.items():
            approved_table_rows = approved_rows.get(table, [])
            current_table_rows = current_rows.get(table, [])
            approved_by_key = {
                tuple(row.get(name) for name in primary_key): row
                for row in approved_table_rows
            }
            current_by_key = {
                tuple(row.get(name) for name in primary_key): row
                for row in current_table_rows
            }
            if len(approved_by_key) != len(approved_table_rows):
                raise RuntimeError(
                    f"Approved paginated history contract contains duplicate {table} keys"
                )
            if len(current_by_key) != len(current_table_rows):
                raise RuntimeError(
                    f"Current paginated history contains duplicate {table} keys"
                )
            unexpected_keys = sorted(set(current_by_key) - set(approved_by_key))
            if unexpected_keys:
                raise RuntimeError(
                    f"Unapproved paginated history rows appeared in {table}"
                )
            changed_keys = [
                key
                for key, row in current_by_key.items()
                if row.get("row_sha256")
                != approved_by_key[key].get("row_sha256")
            ]
            if changed_keys:
                raise RuntimeError(
                    f"Approved paginated history rows changed in {table}"
                )
            already_absent += len(set(approved_by_key) - set(current_by_key))

        rows_removed: dict[str, int] = {}
        expected_current_total = sum(
            len(rows) for rows in current_rows.values()
        )
        if expected_current_total:
            notify_mutation(mutation_observer, COMPONENT_PAGINATED_HISTORY)
        rows_removed = delete_paginated_history_rows_on_conn(
            conn,
            plan.target_ids,
        )
        for table in [
            "thread_items",
            "thread_turns",
            "thread_history_projection_state",
        ]:
            expected_count = len(current_rows.get(table, []))
            if rows_removed[table] != expected_count:
                raise RuntimeError(
                    f"Paginated history rows changed during deletion in {table}"
                )
        integrity_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        if integrity_check != "ok":
            raise RuntimeError(
                "Paginated history quick_check failed after deletion: "
                + integrity_check
            )
        conn.commit()
        return {
            "database_present": True,
            "rows_removed": rows_removed,
            "already_absent": already_absent,
            "integrity_check": integrity_check,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_log_rows(
    codex_home: Path,
    target_ids: list[str],
    mutation_observer: Any | None = None,
    logs_path: Path | None = None,
) -> int:
    discovered_logs_path = discovered_database_path(discover_logs_database(codex_home))
    if logs_path is not None and discovered_logs_path != logs_path:
        raise RuntimeError(
            "The selected logs database changed after the approved report"
        )
    logs_path = logs_path or discovered_logs_path
    if logs_path is None:
        return 0
    require_managed_sqlite(codex_home, logs_path)
    conn = connect_rw(logs_path)
    if conn is None:
        return 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        issues = logs_runtime_mutation_issues(conn, target_ids) + reference_format_issues(
            conn,
            [("logs", "thread_id")],
            logs_path.name,
        )
        if issues:
            raise RuntimeError(
                "Logs schema or references changed before deletion: "
                + " | ".join(issues)
            )
        if count_in_table(conn, "logs", "thread_id", target_ids):
            notify_mutation(mutation_observer, COMPONENT_LOGS)
        removed = delete_log_rows_on_conn(conn, target_ids)
        conn.commit()
        return removed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def validate_path_contracts(
    paths: Iterable[Path],
    expected_contracts: dict[str, dict[str, Any]],
) -> None:
    for path in paths:
        expected = expected_contracts.get(str(path))
        if expected is None:
            raise RuntimeError(
                f"Artifact was not present in the approved identity contract: {path}"
            )
        if not path_is_present(path):
            raise RuntimeError(f"Approved artifact disappeared before deletion: {path}")
        if path_contract_entry(path) != expected:
            raise RuntimeError(
                f"Artifact identity or contents changed after approval: {path}"
            )


def atomic_rename_noreplace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    result: int | None = None
    if sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        renamex_np = libc.renamex_np
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        result = int(renamex_np(source_bytes, destination_bytes, 0x00000004))
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        renameat2 = libc.renameat2
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = int(
            renameat2(
                -100,
                source_bytes,
                -100,
                destination_bytes,
                0x00000001,
            )
        )
    if result is not None:
        if result == 0:
            return
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            str(destination),
        )

    source_info = source.lstat()
    if not stat.S_ISREG(source_info.st_mode):
        raise OSError(
            errno.ENOTSUP,
            "Atomic no-replace rename is unavailable for directories",
            str(destination),
        )
    os.link(source, destination, follow_symlinks=False)
    source.unlink()


def atomic_swap_paths(first: Path, second: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    first_bytes = os.fsencode(first)
    second_bytes = os.fsencode(second)
    result: int | None = None
    if sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        renamex_np = libc.renamex_np
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        result = int(renamex_np(first_bytes, second_bytes, 0x00000002))
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        renameat2 = libc.renameat2
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = int(renameat2(-100, first_bytes, -100, second_bytes, 0x00000002))
    if result is None:
        raise OSError(
            errno.ENOTSUP,
            "Atomic path exchange is unavailable on this platform",
            str(second),
        )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), str(second))


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def copy_extended_attributes(source: Path, destination: Path) -> None:
    python_xattr_functions = [
        getattr(os, function_name, None)
        for function_name in ["listxattr", "getxattr", "setxattr"]
    ]
    if all(callable(function) for function in python_xattr_functions):
        listxattr, getxattr, setxattr = python_xattr_functions
        for attribute in listxattr(source, follow_symlinks=False):
            setxattr(
                destination,
                attribute,
                getxattr(source, attribute, follow_symlinks=False),
                follow_symlinks=False,
            )
        return
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        if not hasattr(libc, "copyfile"):
            raise OSError(
                errno.ENOTSUP,
                "Unable to preserve managed global-state extended attributes",
                str(source),
            )
        copyfile = libc.copyfile
        copyfile.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        copyfile.restype = ctypes.c_int
        copyfile_metadata_nofollow = (
            (1 << 0) | (1 << 1) | (1 << 2) | (1 << 18) | (1 << 19)
        )
        if (
            copyfile(
                os.fsencode(source),
                os.fsencode(destination),
                None,
                copyfile_metadata_nofollow,
            )
            != 0
        ):
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number), str(source))
        return
    raise OSError(
        errno.ENOTSUP,
        "Unable to preserve managed global-state extended attributes",
        str(source),
    )


def create_peer_file(
    source: Path,
    content: bytes,
    mutation_observer: Any | None = None,
) -> Path:
    source_info = source.lstat()
    if not stat.S_ISREG(source_info.st_mode) or source_info.st_nlink != 1:
        raise RuntimeError(f"Managed global state source is unsafe: {source}")
    notify_mutation(mutation_observer, COMPONENT_GLOBAL_STATE)
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{source.name}.delete-session-",
        dir=source.parent,
    )
    candidate = Path(raw_path)
    try:
        os.fchmod(descriptor, stat.S_IMODE(source_info.st_mode))
        try:
            os.fchown(descriptor, source_info.st_uid, source_info.st_gid)
        except PermissionError:
            current = os.fstat(descriptor)
            if (current.st_uid, current.st_gid) != (
                source_info.st_uid,
                source_info.st_gid,
            ):
                raise
        written = 0
        while written < len(content):
            count = os.write(descriptor, content[written:])
            if count <= 0:
                raise OSError(errno.EIO, "Short write while updating global state")
            written += count
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        candidate.unlink(missing_ok=True)
        raise
    os.close(descriptor)
    try:
        copy_extended_attributes(source, candidate)
        check_fd = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(check_fd)
        finally:
            os.close(check_fd)
        return candidate
    except Exception:
        candidate.unlink(missing_ok=True)
        raise


def remove_global_state_contracts(
    data: dict[str, Any],
    approved_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    for contract in approved_refs:
        kind = contract["kind"]
        container_path = tuple(str(part) for part in contract["container_path"])
        if kind == "map_key":
            container = json_path_value(data, container_path)
            key = str(contract["key"])
            if (
                not isinstance(container, dict)
                or key not in container
                or json_value_sha256(container[key]) != contract["value_sha256"]
            ):
                raise RuntimeError(
                    "Approved global state map reference changed before deletion at "
                    + json_path_label((*container_path, key))
                )
            del container[key]
        elif kind == "array_value":
            container = json_path_value(data, container_path)
            value = str(contract["value"])
            expected_count = int(contract["count"])
            if (
                not isinstance(container, list)
                or container.count(value) != expected_count
            ):
                raise RuntimeError(
                    "Approved global state list reference changed before deletion at "
                    + json_path_label(container_path)
                )
            container[:] = [item for item in container if item != value]
        elif kind == "scalar_value":
            if not container_path:
                raise RuntimeError("Invalid approved global state scalar path")
            parent = json_path_value(data, container_path[:-1])
            key = container_path[-1]
            if not isinstance(parent, dict) or parent.get(key) != contract["value"]:
                raise RuntimeError(
                    "Approved global state scalar reference changed before deletion at "
                    + json_path_label(container_path)
                )
            del parent[key]
        elif kind == "voice_selector":
            if not container_path:
                raise RuntimeError("Invalid approved global state voice selector path")
            parent = json_path_value(data, container_path[:-1])
            key = container_path[-1]
            current = parent.get(key) if isinstance(parent, dict) else None
            if (
                not isinstance(parent, dict)
                or not isinstance(current, dict)
                or current.get("conversationId") != contract["conversation_id"]
                or json_value_sha256(current) != contract["value_sha256"]
            ):
                raise RuntimeError(
                    "Approved global state voice selector changed before deletion at "
                    + json_path_label(container_path)
                )
            parent[key] = None
        else:
            raise RuntimeError(f"Unknown approved global state reference kind: {kind}")
    return data


def encode_global_state(data: dict[str, Any], previous_raw: bytes) -> bytes:
    encoded = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return encoded + (b"\n" if previous_raw.endswith(b"\n") else b"")


def global_state_file_identity(path: Path) -> tuple[int, int, int, int]:
    info = path.lstat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def stage_global_state_file(
    codex_home: Path,
    path: Path,
    target_ids: list[str],
    approved_refs: list[dict[str, Any]],
    mutation_observer: Any | None = None,
    pre_mutation_check: Any | None = None,
) -> dict[str, Any]:
    require_managed_file(codex_home, path)
    source_identity = global_state_file_identity(path)
    source_data, source_bytes = load_global_state_file(path)
    current_refs, _mentions, issues, _warnings = global_state_snapshot(
        source_data,
        target_ids,
    )
    if issues or current_refs != approved_refs:
        raise RuntimeError(
            "Target-owned global state references changed after approval"
            + (": " + " | ".join(issues) if issues else "")
        )
    cleaned_data = remove_global_state_contracts(
        json.loads(json.dumps(source_data, ensure_ascii=False)),
        approved_refs,
    )
    (
        remaining_refs,
        _remaining_mentions,
        remaining_issues,
        _remaining_warnings,
    ) = global_state_snapshot(cleaned_data, target_ids)
    if remaining_refs or remaining_issues:
        raise RuntimeError(
            "Global state transformation did not remove only the approved references"
        )
    cleaned_bytes = encode_global_state(cleaned_data, source_bytes)
    if pre_mutation_check is not None:
        pre_mutation_check()
    candidate = create_peer_file(path, cleaned_bytes, mutation_observer)
    candidate_info = candidate.lstat()
    return {
        "path": path,
        "approved_count": len(approved_refs),
        "source_bytes": source_bytes,
        "source_identity": source_identity,
        "cleaned_bytes": cleaned_bytes,
        "candidate": candidate,
        "staged_identity": (candidate_info.st_dev, candidate_info.st_ino),
        "published": False,
    }


def rollback_global_state_stages(
    stages: list[dict[str, Any]],
) -> list[str]:
    recovery_paths: list[str] = []
    for stage in reversed(stages):
        path = Path(stage["path"])
        candidate = Path(stage["candidate"])
        if stage.get("committed", False):
            if path_is_present(candidate):
                recovery_paths.append(str(candidate))
            continue
        if not stage.get("published", False):
            candidate.unlink(missing_ok=True)
            continue
        try:
            current = path.lstat()
            staged_identity = tuple(stage["staged_identity"])
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or (current.st_dev, current.st_ino) != staged_identity
                or path.read_bytes() != stage["cleaned_bytes"]
            ):
                recovery_paths.append(str(candidate))
                continue
            atomic_swap_paths(candidate, path)
            stage["published"] = False
            fsync_directory(path.parent)
            if candidate.read_bytes() != stage["cleaned_bytes"]:
                recovery_paths.append(str(candidate))
                continue
            candidate.unlink(missing_ok=True)
        except OSError:
            recovery_paths.append(str(candidate))
    return sorted(dict.fromkeys(recovery_paths))


def apply_target_global_state(
    plan: Plan,
    mutation_observer: Any | None = None,
) -> dict[str, Any]:
    require_desktop_offline(plan)
    stages: list[dict[str, Any]] = []
    removed_by_file = {filename: 0 for filename in GLOBAL_STATE_FILENAMES}
    try:
        (
            current_refs,
            current_presence,
            _mentions,
            issues,
            _warnings,
        ) = inspect_global_state_files(plan.codex_home, plan.target_ids)
        if issues:
            raise RuntimeError(
                "Global state safety changed before deletion: " + " | ".join(issues)
            )
        if current_presence != plan.global_state_files_present:
            raise RuntimeError(
                "Managed global state file presence changed after approval"
            )
        if current_refs != plan.global_state_refs:
            raise RuntimeError(
                "Target-owned global state references changed after approval"
            )
        for filename in [
            ".codex-global-state.json.bak",
            ".codex-global-state.json",
        ]:
            approved = plan.global_state_refs.get(filename, [])
            if not approved:
                continue
            stages.append(
                stage_global_state_file(
                    plan.codex_home,
                    plan.codex_home / filename,
                    plan.target_ids,
                    approved,
                    mutation_observer,
                    lambda: require_desktop_offline(plan),
                )
            )
        require_desktop_offline(plan)
        for stage in stages:
            path = Path(stage["path"])
            if (
                global_state_file_identity(path) != tuple(stage["source_identity"])
                or path.read_bytes() != stage["source_bytes"]
            ):
                raise RuntimeError(
                    f"Managed global state changed during staging: {path}"
                )
        for stage in stages:
            path = Path(stage["path"])
            candidate = Path(stage["candidate"])
            atomic_swap_paths(candidate, path)
            stage["published"] = True
            if candidate.read_bytes() != stage["source_bytes"]:
                raise RuntimeError(
                    f"Managed global state publish collision detected: {path}"
                )
            removed_by_file[path.name] = int(stage["approved_count"])
            fsync_directory(path.parent)
        require_desktop_offline(plan)
        (
            verified_refs,
            verified_presence,
            _verified_mentions,
            verified_issues,
            _verified_warnings,
        ) = inspect_global_state_files(plan.codex_home, plan.target_ids)
        if (
            verified_issues
            or verified_presence != plan.global_state_files_present
            or any(verified_refs.values())
        ):
            raise RuntimeError(
                "Target-owned global state references remain after pair publish"
            )
        for stage in stages:
            stage["published"] = False
            stage["committed"] = True
        cleanup_failures: list[str] = []
        for stage in stages:
            candidate = Path(stage["candidate"])
            try:
                candidate.unlink()
            except OSError:
                cleanup_failures.append(str(candidate))
        if cleanup_failures:
            raise RuntimeError(
                "Global state pair committed, but displaced originals could not be "
                "removed: " + ", ".join(cleanup_failures)
            )
        fsync_directory(plan.codex_home)
        return {
            "removed_by_file": removed_by_file,
            "removed": sum(removed_by_file.values()),
        }
    except Exception as exc:
        recovery_paths = rollback_global_state_stages(stages)
        recovery_note = (
            "; preserved recovery files: " + ", ".join(recovery_paths)
            if recovery_paths
            else ""
        )
        raise RuntimeError(
            f"Global state pair update failed: {exc}{recovery_note}"
        ) from exc


def remove_paths(
    codex_home: Path,
    paths: Iterable[Path],
    allowed_roots: Iterable[Path],
    expected_contracts: dict[str, dict[str, Any]] | None = None,
    retained_status: dict[str, list[str]] | None = None,
    mutation_observer: Any | None = None,
    mutation_component: str = COMPONENT_ROLLOUTS,
) -> int:
    roots = list(allowed_roots)
    for root in roots:
        issue = managed_root_issue(codex_home, root)
        if issue is not None:
            raise RuntimeError(issue)
    removed = 0
    quarantine_roots: dict[Path, Path] = {}
    quarantine_counter = 0

    def quarantine_root(root: Path) -> Path:
        existing = quarantine_roots.get(root)
        if existing is not None:
            return existing
        created = Path(tempfile.mkdtemp(prefix=".codex-delete-", dir=str(root)))
        quarantine_roots[root] = created
        return created

    def quarantine_destination(root: Path, source: Path) -> Path:
        nonlocal quarantine_counter
        quarantine_counter += 1
        return quarantine_root(root) / f"{quarantine_counter}-{source.name}"

    def restore_or_raise(quarantined: Path, original: Path) -> None:
        try:
            atomic_rename_noreplace(quarantined, original)
            return
        except OSError as exc:
            raise RuntimeError(
                "An artifact changed during deletion; both the new path and the retained "
                f"approved object were preserved. Retained object: {quarantined}"
            ) from exc

    def regular_file_contract_matches(
        candidate: Path,
        contract: dict[str, Any],
    ) -> bool:
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(info.st_mode):
            return False
        for key, actual in [
            ("device", info.st_dev),
            ("inode", info.st_ino),
            ("mode", info.st_mode),
            ("size", info.st_size),
            ("mtime_ns", info.st_mtime_ns),
        ]:
            if contract.get(key) != actual:
                return False
        expected_digest = contract.get("content_sha256")
        return (
            isinstance(expected_digest, str)
            and file_content_sha256(
                candidate,
                info,
            )
            == expected_digest
        )

    def remove_regular_file(
        source: Path,
        contract: dict[str, Any],
        root: Path,
    ) -> str:
        notify_mutation(mutation_observer, mutation_component)
        destination = quarantine_destination(root, source)
        try:
            os.rename(source, destination)
        except FileNotFoundError:
            return "already_absent"
        if not regular_file_contract_matches(destination, contract):
            restore_or_raise(destination, source)
            return "identity_changed_retained"
        destination.unlink()
        return "removed"

    def remove_empty_directory(
        source: Path,
        contract: dict[str, Any],
        root: Path,
    ) -> str:
        notify_mutation(mutation_observer, mutation_component)
        destination = quarantine_destination(root, source)
        try:
            os.rename(source, destination)
        except FileNotFoundError:
            return "already_absent"
        info = destination.lstat()
        identity_matches = (
            stat.S_ISDIR(info.st_mode)
            and contract.get("device") == info.st_dev
            and contract.get("inode") == info.st_ino
            and contract.get("mode") == info.st_mode
        )
        if not identity_matches:
            restore_or_raise(destination, source)
            return "identity_changed_retained"
        if next(destination.iterdir(), None) is not None:
            restore_or_raise(destination, source)
            return "retained_with_late_children"
        destination.rmdir()
        return "removed"

    def record_retained(status: str, path: Path) -> None:
        if retained_status is None:
            raise RuntimeError(
                f"Approved target artifact changed during deletion and was preserved: {path}"
            )
        retained_status.setdefault(status, []).append(str(path))

    try:
        for path in paths:
            expected = (
                expected_contracts.get(str(path))
                if expected_contracts is not None
                else None
            )
            if expected_contracts is not None:
                if retained_status is None:
                    validate_path_contracts([path], expected_contracts)
                elif not path_is_present(path):
                    record_retained("already_absent", path)
                    continue
                elif expected is None or path_contract_entry(path) != expected:
                    record_retained("identity_changed_retained", path)
                    continue
            elif not path_is_present(path):
                continue
            matching_root = next(
                (root for root in roots if path_within_root_issue(path, root) is None),
                None,
            )
            if matching_root is None:
                raise RuntimeError(
                    f"Refusing to remove path outside approved roots: {path}"
                )
            if path.is_symlink():
                raise RuntimeError(f"Refusing to remove symbolic-link artifact: {path}")
            contract = expected or path_contract_entry(path)
            if contract.get("type") == "file":
                file_status = remove_regular_file(path, contract, matching_root)
                if file_status == "removed":
                    removed += 1
                else:
                    record_retained(file_status, path)
                continue
            if contract.get("type") != "directory":
                raise RuntimeError(f"Unsupported approved artifact type: {path}")
            tree_entries = contract.get("tree_entries")
            if not isinstance(tree_entries, list):
                raise RuntimeError(
                    f"Approved directory contract has no leaf inventory: {path}"
                )
            file_entries: list[tuple[Path, dict[str, Any]]] = []
            directory_entries: list[tuple[Path, dict[str, Any]]] = []
            for entry in tree_entries:
                if not isinstance(entry, dict):
                    raise RuntimeError(
                        f"Approved directory contract is malformed: {path}"
                    )
                relative = Path(str(entry.get("relative_path", "")))
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or not relative.parts
                ):
                    raise RuntimeError(
                        f"Approved directory contract contains an unsafe path: {relative}"
                    )
                item = path / relative
                if path_within_root_issue(item, path) is not None:
                    raise RuntimeError(
                        f"Approved directory entry escaped its root: {item}"
                    )
                mode = int(entry.get("mode", 0))
                if stat.S_ISREG(mode):
                    file_entries.append((item, entry))
                elif stat.S_ISDIR(mode):
                    directory_entries.append((item, entry))
                else:
                    raise RuntimeError(
                        f"Unsupported approved directory entry type: {item}"
                    )
            directory_statuses: set[str] = set()
            for item, entry in sorted(
                file_entries,
                key=lambda pair: len(pair[0].parts),
                reverse=True,
            ):
                item_status = remove_regular_file(item, entry, matching_root)
                if item_status != "removed":
                    directory_statuses.add(item_status)
            root_status = "already_absent"
            for item, entry in sorted(
                directory_entries + [(path, contract)],
                key=lambda pair: len(pair[0].parts),
                reverse=True,
            ):
                item_status = remove_empty_directory(item, entry, matching_root)
                if item == path:
                    root_status = item_status
                elif item_status != "removed":
                    directory_statuses.add(item_status)
            if root_status == "removed":
                removed += 1
                continue
            if "identity_changed_retained" in directory_statuses:
                root_status = "identity_changed_retained"
            elif "retained_with_late_children" in directory_statuses:
                root_status = "retained_with_late_children"
            record_retained(root_status, path)
        return removed
    finally:
        for root in quarantine_roots.values():
            try:
                root.rmdir()
            except OSError:
                pass


def integrity(path: Path) -> str:
    require_managed_sqlite(path.parent, path)
    return database_check(path, "integrity_check")


def expected_preserved_artifact_contracts(
    codex_home: Path,
    retained: Iterable[dict[str, Any]],
) -> dict[str, dict[str, dict[str, Any]]]:
    roots = {
        COMPONENT_ROLLOUTS: codex_home / "sessions",
        COMPONENT_SNAPSHOTS: codex_home / "shell_snapshots",
        COMPONENT_GENERATED: codex_home / "generated_images",
    }
    allowed_statuses = {
        "retained_unapproved_addition",
        "identity_changed_retained",
        "retained_with_late_children",
        "retained_untrusted",
    }
    expected = {component: {} for component in roots}
    for item in retained:
        if not isinstance(item, dict) or item.get("status") not in allowed_statuses:
            continue
        component = str(item.get("component", ""))
        root = roots.get(component)
        contract = item.get("preserved_contract")
        path = Path(str(item.get("object_id", "")))
        if (
            root is None
            or not isinstance(contract, dict)
            or not path.is_absolute()
            or ".." in path.parts
            or not (path == root or path.is_relative_to(root))
            or contract.get("path") != str(path)
        ):
            continue
        expected[component][str(path)] = contract
    return expected


def state_target_preservation_snapshot(
    codex_home: Path,
    target_ids: list[str],
    rollout_paths: list[str],
) -> dict[str, Any]:
    state_path = discovered_database_path(discover_state_database(codex_home))
    if state_path is None:
        return {"database_present": False, "target_rows": {}, "rollout_rows": []}
    managed_issues = managed_sqlite_issues(codex_home, state_path)
    if managed_issues:
        unsafe_paths: dict[str, dict[str, int]] = {}
        for candidate in sqlite_family_paths(state_path):
            if not path_is_present(candidate):
                continue
            info = candidate.lstat()
            unsafe_paths[str(candidate)] = {
                "device": int(info.st_dev),
                "inode": int(info.st_ino),
                "mode": int(info.st_mode),
                "links": int(info.st_nlink),
                "size": int(info.st_size),
                "modified_ns": int(info.st_mtime_ns),
            }
        return {
            "database_present": path_is_present(state_path),
            "unsafe_path_identities": unsafe_paths,
        }
    state = connect_ro(state_path)
    if state is None:
        return {"database_present": False, "target_rows": {}, "rollout_rows": []}
    try:
        target_rows: dict[str, list[str]] = {}
        for table, column in STATE_REFERENCE_LOCATIONS:
            for sid in target_ids:
                target_rows[f"{table}.{column}:{sid}"] = reference_row_digests(
                    state,
                    table,
                    column,
                    sid,
                )
        return {
            "database_present": True,
            "target_rows": target_rows,
            "rollout_rows": rollout_migration_skipped_rows_for_paths(
                state,
                rollout_paths,
            ),
        }
    finally:
        state.close()


def nofollow_path_identities(paths: Iterable[Path]) -> dict[str, dict[str, int]]:
    identities: dict[str, dict[str, int]] = {}
    for path in paths:
        if not path_is_present(path):
            continue
        info = path.lstat()
        identities[str(path)] = {
            "device": int(info.st_dev),
            "inode": int(info.st_ino),
            "mode": int(info.st_mode),
            "links": int(info.st_nlink),
            "size": int(info.st_size),
            "modified_ns": int(info.st_mtime_ns),
        }
    return identities


def stable_sqlite_database_identity(path: Path) -> dict[str, Any]:
    """Return the stable identity of the selected SQLite database file.

    SQLite may create, remove, resize, or touch `-wal` and `-shm` sidecars during
    ordinary reads, checkpoints, and Desktop shutdown.  Those coordination files
    are runtime evidence, not deletion authority.  The canonical main-file path
    and replacement-sensitive inode identity stay frozen while schema anchors and
    exact target-row contracts protect the data selected for mutation.
    """

    if not path_is_present(path):
        return {"path": str(path), "present": False}
    info = path.lstat()
    return {
        "path": str(path),
        "present": True,
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "mode": int(info.st_mode),
        "links": int(info.st_nlink),
    }


def preserved_component_snapshot(
    codex_home: Path,
    kind: str,
    target_ids: list[str],
    rollout_paths: list[str],
) -> Any:
    target_set = set(target_ids)
    if kind == "state_and_index":
        return state_target_preservation_snapshot(
            codex_home,
            target_ids,
            rollout_paths,
        )
    if kind == "session_index":
        path = codex_home / "session_index.jsonl"
        issue = managed_file_issue(codex_home, path)
        if issue is not None:
            return {
                "present": path_is_present(path),
                "unsafe_path_identities": nofollow_path_identities([path]),
            }
        present = path_is_present(path)
        index_issues = session_index_issues(path)
        if present and index_issues:
            return {
                "present": True,
                "issues": index_issues,
                "raw_file_contract": path_contract_entry(path),
            }
        return {
            "present": present,
            "rows": session_index_entries_for_ids(path, target_set),
        }
    if kind == "logs":
        path = discovered_database_path(discover_logs_database(codex_home))
        if path is None:
            return {"present": False, "rows": []}
        if managed_sqlite_issues(codex_home, path):
            return {
                "present": path_is_present(path),
                "unsafe_path_identities": nofollow_path_identities(
                    sqlite_family_paths(path)
                ),
            }
        logs = connect_ro(path)
        try:
            return {
                "present": logs is not None,
                "rows": logs_entries_for_ids(logs, target_set),
            }
        finally:
            if logs is not None:
                logs.close()
    if kind == "desktop_catalog":
        path, discovery_issues = discover_desktop_catalog_path(codex_home)
        if discovery_issues:
            candidates = sqlite_directory_database_candidates(codex_home)
            return {
                "issues": discovery_issues,
                "unsafe_path_identities": nofollow_path_identities(candidates),
            }
        if path is None:
            return {"present": False, "path": "", "rows": {}}
        require_managed_sqlite(codex_home, path)
        catalog = connect_ro(path)
        try:
            return {
                "present": catalog is not None,
                "path": str(path),
                "schema_signature": desktop_catalog_schema_signature(catalog),
                "user_version": desktop_catalog_user_version(catalog),
                "rows": desktop_catalog_row_contracts(catalog, target_ids),
            }
        finally:
            if catalog is not None:
                catalog.close()
    if kind == "auxiliary_thread_databases":
        (
            _counts,
            contracts,
            issues,
            _checks,
            presence,
        ) = auxiliary_thread_database_snapshot(codex_home, target_ids)
        return {"presence": presence, "contracts": contracts, "issues": issues}
    if kind == "paginated_history":
        assessment = paginated_history_database_assessment(
            codex_home,
            target_ids,
        )
        return {
            "path": assessment.get("path", ""),
            "discovery": assessment.get("discovery", {}),
            "check": assessment.get("check", "missing"),
            "database_plan": assessment.get("database_plan", {}),
            "contract": assessment.get("contract", {}),
        }
    if kind == "global_state":
        refs, presence, _mentions, issues, _warnings = inspect_global_state_files(
            codex_home,
            target_ids,
        )
        return {
            "presence": presence,
            "refs": refs,
            "issues": issues,
            "path_identities": nofollow_path_identities(
                [codex_home / filename for filename in GLOBAL_STATE_FILENAMES]
            ),
        }
    raise RuntimeError(f"Unknown expected-preserved contract kind: {kind}")


def expected_preserved_component_contracts(
    plan: Plan,
    target_ids: list[str],
    skipped_components: set[str],
) -> list[dict[str, Any]]:
    rollout_paths = sorted(
        {
            info.rollout_path
            for sid, info in plan.threads.items()
            if sid in set(target_ids) and info.rollout_path
        }
    )
    kinds: list[tuple[str, str]] = []
    if COMPONENT_CORE in skipped_components:
        kinds.extend(
            [
                (COMPONENT_CORE, "state_and_index"),
                (COMPONENT_CORE, "session_index"),
            ]
        )
    if COMPONENT_LOGS in skipped_components and plan.include_logs:
        kinds.append((COMPONENT_LOGS, "logs"))
    for component in (
        COMPONENT_CATALOG,
        COMPONENT_AUXILIARY,
        COMPONENT_GLOBAL_STATE,
        COMPONENT_PAGINATED_HISTORY,
    ):
        if component in skipped_components:
            kinds.append((component, component))

    contracts: list[dict[str, Any]] = []
    for component, kind in kinds:
        record: dict[str, Any] = {
            "component": component,
            "object_id": kind,
            "kind": kind,
            "target_ids": sorted(target_ids),
            "rollout_paths": rollout_paths,
        }
        try:
            record["expected"] = preserved_component_snapshot(
                plan.codex_home,
                kind,
                sorted(target_ids),
                rollout_paths,
            )
        except (OSError, RuntimeError, sqlite3.Error, UnicodeError) as exc:
            record["capture_error"] = str(exc)
        contracts.append(record)
    return contracts


def verify_expected_preserved_component_contracts(
    codex_home: Path,
    contracts: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str]]:
    present: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []
    errors: list[str] = []
    for record in contracts:
        component = str(record.get("component", ""))
        object_id = str(record.get("object_id", ""))
        summary = {"component": component, "object_id": object_id}
        capture_error = record.get("capture_error")
        if capture_error is not None or "expected" not in record:
            reason = f"Preserved contract could not be frozen: {capture_error}"
            missing.append({**summary, "reason": reason})
            errors.append(f"{component}:{object_id}: {reason}")
            continue
        try:
            current = preserved_component_snapshot(
                codex_home,
                str(record.get("kind", "")),
                [str(sid) for sid in record.get("target_ids", [])],
                [str(path) for path in record.get("rollout_paths", [])],
            )
        except (OSError, RuntimeError, sqlite3.Error, UnicodeError) as exc:
            reason = f"Preserved object could not be inspected: {exc}"
            missing.append({**summary, "reason": reason})
            errors.append(f"{component}:{object_id}: {reason}")
            continue
        if current == record.get("expected"):
            present.append(summary)
            continue
        reason = "Expected-preserved object was removed or changed during apply."
        missing.append({**summary, "reason": reason})
        errors.append(f"{component}:{object_id}: {reason}")
    return present, missing, errors


def verify(
    codex_home: Path,
    target_ids: list[str],
    include_logs: bool,
    state_db_was_present: bool,
    logs_db_was_present: bool,
    protected_thread_ids: set[str] | None = None,
    target_rollout_paths: Iterable[str] | None = None,
    desktop_catalog_was_present: bool = False,
    desktop_catalog_expected_path: Path | None = None,
    global_state_files_expected: dict[str, bool] | None = None,
    auxiliary_databases_expected: list[str] | None = None,
    skipped_components: set[str] | None = None,
    expected_preserved_artifacts: dict[
        str, dict[str, dict[str, Any]]
    ] | None = None,
    expected_preserved_contracts: list[dict[str, Any]] | None = None,
    state_database_expected_path: Path | None = None,
    logs_database_expected_path: Path | None = None,
    auxiliary_database_plans_expected: dict[str, dict[str, Any]] | None = None,
    paginated_history_expected_path: Path | None = None,
    paginated_history_contract_expected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    skipped_components = skipped_components or set()
    expected_preserved_artifacts = expected_preserved_artifacts or {}
    expected_preserved_contracts = expected_preserved_contracts or []
    auxiliary_database_plans_expected = auxiliary_database_plans_expected or {}
    paginated_history_contract_expected = (
        paginated_history_contract_expected or {}
    )
    (
        expected_preserved_present,
        expected_preserved_missing,
        expected_preserved_errors,
    ) = verify_expected_preserved_component_contracts(
        codex_home,
        expected_preserved_contracts,
    )
    boundary_findings = storage_boundary_findings(codex_home, include_logs)
    active_boundary_issues = [
        finding["reason"]
        for finding in boundary_findings
        if finding["component"] not in skipped_components
    ]
    if active_boundary_issues:
        return {
            "verification_ok": False,
            "verification_errors": active_boundary_issues
            + expected_preserved_errors,
            "residual_counts": {},
            "remaining_rollout_files": [],
            "remaining_shell_snapshots": [],
            "remaining_generated_artifacts": [],
            "missing_protected_threads": sorted(protected_thread_ids or set()),
            "state_integrity": "unsafe",
            "logs_integrity": "unsafe" if include_logs else "skipped",
            "desktop_catalog_integrity": "unsafe",
            "desktop_catalog_revision": None,
            "auxiliary_thread_database_checks": {},
            "auxiliary_thread_databases_present": [],
            "global_state_files_present": {},
            "planned_deleted_remaining": {},
            "expected_preserved_present": expected_preserved_present,
            "expected_preserved_missing": expected_preserved_missing,
            "unexpected_remaining": [],
            "unexpected_non_target_removed": sorted(protected_thread_ids or set()),
            "integrity_checks": {},
            "offline_verification_ok": False,
            "historical_snapshot_ok": True,
        }

    state: sqlite3.Connection | None = None
    logs: sqlite3.Connection | None = None
    desktop_catalog: sqlite3.Connection | None = None
    try:
        current_state_path = discovered_database_path(
            discover_state_database(codex_home)
        )
        current_logs_path = discovered_database_path(
            discover_logs_database(codex_home)
        )
        state_path = state_database_expected_path or current_state_path
        logs_path = logs_database_expected_path or current_logs_path
        state = (
            None
            if state_path is None or managed_sqlite_issues(codex_home, state_path)
            else connect_ro(state_path)
        )
        logs = (
            connect_ro(logs_path)
            if include_logs
            and COMPONENT_LOGS not in skipped_components
            and logs_path is not None
            else None
        )
        current_catalog_path, catalog_discovery_issues = discover_desktop_catalog_path(
            codex_home
        )
        catalog_presence_matches = (
            True
            if COMPONENT_CATALOG in skipped_components
            else desktop_catalog_was_present == (current_catalog_path is not None)
        )
        catalog_path_matches = (
            current_catalog_path == desktop_catalog_expected_path
            if desktop_catalog_was_present
            else current_catalog_path is None
        )
        desktop_catalog = (
            connect_ro(current_catalog_path)
            if current_catalog_path is not None and not catalog_discovery_issues
            and COMPONENT_CATALOG not in skipped_components
            else None
        )
        verification_errors = expected_preserved_errors + (
            []
            if COMPONENT_CORE in skipped_components
            else state_runtime_mutation_issues(state, target_ids)
        )
        if include_logs and COMPONENT_LOGS not in skipped_components:
            verification_errors.extend(logs_schema_issues(logs))
        if COMPONENT_CATALOG not in skipped_components:
            verification_errors.extend(catalog_discovery_issues)
        if COMPONENT_CATALOG not in skipped_components and (
            not catalog_presence_matches or not catalog_path_matches
        ):
            verification_errors.append(
                "Desktop catalog database presence or selected path changed after approval."
            )
        if COMPONENT_CATALOG not in skipped_components:
            verification_errors.extend(
                desktop_catalog_runtime_mutation_issues(
                    desktop_catalog,
                    target_ids,
                )
            )
            verification_errors.extend(
                reference_format_issues(
                    desktop_catalog,
                    DESKTOP_CATALOG_CANONICAL_UUID_REFERENCES,
                    "sqlite/codex desktop catalog database",
                )
            )
        verified_rollout_paths = {
            str(path) for path in (target_rollout_paths or []) if path
        }
        catalog_contracts = desktop_catalog_row_contracts(desktop_catalog, target_ids)
        desktop_catalog_revision_after = desktop_catalog_revision(desktop_catalog)
        current_state_thread_ids = state_thread_ids(state)
        if COMPONENT_CORE not in skipped_components:
            verification_errors.extend(
                rollout_migration_skipped_path_issues(
                    state,
                    set(target_ids),
                    verified_rollout_paths,
                )
            )
        residual = {
            "state_threads": 0
            if COMPONENT_CORE in skipped_components
            else count_in_table(state, "threads", "id", target_ids),
            "state_thread_spawn_edges": 0
            if COMPONENT_CORE in skipped_components
            else count_edges(state, target_ids),
            "state_thread_dynamic_tools": count_in_table(
                state, "thread_dynamic_tools", "thread_id", target_ids
            )
            if COMPONENT_CORE not in skipped_components
            else 0,
            "state_thread_goals": count_in_table(
                state, "thread_goals", "thread_id", target_ids
            )
            if COMPONENT_CORE not in skipped_components
            else 0,
            "state_stage1_outputs": count_in_table(
                state, "stage1_outputs", "thread_id", target_ids
            )
            if COMPONENT_CORE not in skipped_components
            else 0,
            "state_agent_job_items_assigned": count_in_table(
                state, "agent_job_items", "assigned_thread_id", target_ids
            )
            if COMPONENT_CORE not in skipped_components
            else 0,
            "session_index_rows": 0
            if COMPONENT_CORE in skipped_components
            else count_session_index(
                codex_home / "session_index.jsonl", set(target_ids)
            ),
            "logs_rows": count_in_table(logs, "logs", "thread_id", target_ids)
            if include_logs and COMPONENT_LOGS not in skipped_components
            else 0,
            "state_rollout_migration_skipped_rollouts": len(
                rollout_migration_skipped_rows_for_paths(
                    state,
                    verified_rollout_paths,
                )
            )
            if COMPONENT_CORE not in skipped_components
            else 0,
            "desktop_catalog_rows": 0
            if COMPONENT_CATALOG in skipped_components
            else len(
                catalog_contracts.get("local_thread_catalog", [])
            ),
            "desktop_timeline_rows": 0
            if COMPONENT_CATALOG in skipped_components
            else len(
                catalog_contracts.get("thread_timeline_ledger", [])
            ),
            "desktop_automation_run_rows": 0
            if COMPONENT_CATALOG in skipped_components
            else len(
                catalog_contracts.get("automation_runs", [])
            ),
            "desktop_inbox_rows": 0
            if COMPONENT_CATALOG in skipped_components
            else desktop_catalog_unsupported_target_counts(
                desktop_catalog, target_ids
            ).get("inbox_items", 0),
        }
    finally:
        if desktop_catalog is not None:
            desktop_catalog.close()
        if logs is not None:
            logs.close()
        if state is not None:
            state.close()

    if COMPONENT_AUXILIARY in skipped_components:
        auxiliary_counts = {}
        auxiliary_contracts = {}
        auxiliary_checks = {}
        auxiliary_presence = list(auxiliary_databases_expected or [])
    else:
        auxiliary_assessment = auxiliary_thread_database_assessment(
            codex_home,
            target_ids,
            "integrity_check",
        )
        auxiliary_counts = auxiliary_assessment["counts"]
        auxiliary_contracts = auxiliary_assessment["contracts"]
        auxiliary_checks = auxiliary_assessment["checks"]
        auxiliary_presence = auxiliary_assessment["presence"]
        current_auxiliary_plans = auxiliary_assessment["database_plans"]
        if not auxiliary_database_plans_expected:
            verification_errors.extend(auxiliary_assessment["issues"])
        else:
            for filename, approved_database_plan in sorted(
                auxiliary_database_plans_expected.items()
            ):
                current_database_plan = current_auxiliary_plans.get(filename)
                summary = {
                    "component": COMPONENT_AUXILIARY,
                    "object_id": filename,
                }
                if current_database_plan is None:
                    reason = "Expected auxiliary database disappeared during apply."
                    expected_preserved_missing.append({**summary, "reason": reason})
                    verification_errors.append(reason)
                    continue
                if approved_database_plan.get("status") == "skipped":
                    if current_database_plan.get("preserved_contract") == (
                        approved_database_plan.get("preserved_contract")
                    ):
                        expected_preserved_present.append(summary)
                    else:
                        reason = (
                            "Expected-preserved auxiliary database changed during apply."
                        )
                        expected_preserved_missing.append(
                            {**summary, "reason": reason}
                        )
                        verification_errors.append(f"{filename}: {reason}")
                elif current_database_plan.get("status") != "enabled":
                    verification_errors.extend(
                        str(item)
                        for item in current_database_plan.get("reasons", [])
                    )
    if (
        COMPONENT_AUXILIARY not in skipped_components
        and auxiliary_databases_expected is not None
        and auxiliary_presence != auxiliary_databases_expected
    ):
        verification_errors.append(
            "Auxiliary desktop thread database presence changed after approval."
        )
    residual["desktop_auxiliary_thread_rows"] = sum(
        count
        for filename, count in auxiliary_counts.items()
        if not auxiliary_database_plans_expected
        or auxiliary_database_plans_expected.get(filename, {}).get("status")
        != "skipped"
    )
    if COMPONENT_PAGINATED_HISTORY in skipped_components:
        paginated_history_counts = {
            table: 0 for table in PAGINATED_HISTORY_PRIMARY_KEYS
        }
        paginated_history_check = "skipped"
        paginated_history_path = paginated_history_expected_path
        paginated_history_contract_after: dict[str, Any] = {}
    else:
        paginated_assessment = paginated_history_database_assessment(
            codex_home,
            target_ids,
            "integrity_check",
        )
        paginated_history_counts = dict(
            paginated_assessment.get("counts", {})
        )
        paginated_history_check = str(
            paginated_assessment.get("check", "missing")
        )
        path_value = str(paginated_assessment.get("path", ""))
        paginated_history_path = Path(path_value) if path_value else None
        paginated_history_contract_after = dict(
            paginated_assessment.get("contract", {})
        )
        current_plan = paginated_assessment.get("database_plan", {})
        if paginated_history_path != paginated_history_expected_path:
            verification_errors.append(
                "The authoritative paginated history database path changed during deletion."
            )
        if paginated_history_expected_path is not None and current_plan.get(
            "status"
        ) != "enabled":
            verification_errors.extend(
                str(reason) for reason in current_plan.get("reasons", [])
            )
        expected_signature = paginated_history_contract_expected.get(
            "schema_signature"
        )
        if expected_signature and paginated_history_contract_after.get(
            "schema_signature"
        ) != expected_signature:
            verification_errors.append(
                "Paginated history schema identity changed during deletion."
            )
    residual["paginated_history_projection_rows"] = (
        paginated_history_counts.get("thread_history_projection_state", 0)
    )
    residual["paginated_history_turn_rows"] = paginated_history_counts.get(
        "thread_turns", 0
    )
    residual["paginated_history_item_rows"] = paginated_history_counts.get(
        "thread_items", 0
    )
    if COMPONENT_GLOBAL_STATE in skipped_components:
        global_state_refs = {filename: [] for filename in GLOBAL_STATE_FILENAMES}
        global_state_presence = dict(global_state_files_expected or {})
        global_state_issues = []
        global_state_warnings = []
    else:
        (
            global_state_refs,
            global_state_presence,
            _global_state_mentions,
            global_state_issues,
            global_state_warnings,
        ) = inspect_global_state_files(codex_home, target_ids)
    verification_errors.extend(global_state_issues)
    if (
        COMPONENT_GLOBAL_STATE not in skipped_components
        and global_state_files_expected is not None
        and global_state_presence != global_state_files_expected
    ):
        verification_errors.append(
            "Managed global state file presence changed after approval."
        )
    residual["global_state_structural_refs"] = sum(
        len(entries) for entries in global_state_refs.values()
    )

    preservation_errors: list[str] = []
    for component, contracts in expected_preserved_artifacts.items():
        if component not in {
            COMPONENT_ROLLOUTS,
            COMPONENT_SNAPSHOTS,
            COMPONENT_GENERATED,
        } or not isinstance(contracts, dict):
            preservation_errors.append(
                f"Invalid expected-preserved artifact component: {component}"
            )
            continue
        for path_text, expected_contract in contracts.items():
            path = Path(path_text)
            try:
                matches = (
                    path_is_present(path)
                    and isinstance(expected_contract, dict)
                    and path_contract_entry(path) == expected_contract
                )
            except (OSError, RuntimeError, UnicodeError):
                matches = False
            summary = {"component": component, "object_id": path_text}
            if matches:
                expected_preserved_present.append(summary)
            else:
                reason = "An expected-preserved artifact was removed or changed during apply."
                expected_preserved_missing.append({**summary, "reason": reason})
                preservation_errors.append(f"{reason} {path_text}")
    verification_errors.extend(preservation_errors)

    unsafe_artifacts: list[str] = []
    target_set = set(target_ids)
    expected_rollouts = set(
        expected_preserved_artifacts.get(COMPONENT_ROLLOUTS, {})
    )
    expected_snapshots = set(
        expected_preserved_artifacts.get(COMPONENT_SNAPSHOTS, {})
    )
    expected_generated = set(
        expected_preserved_artifacts.get(COMPONENT_GENERATED, {})
    )
    remaining_rollouts = [] if COMPONENT_ROLLOUTS in skipped_components else sorted(
        str(path)
        for path in paths_for_ids(
            rollout_paths_by_session(codex_home, unsafe_artifacts),
            target_set,
        )
        if path_is_present(path) and str(path) not in expected_rollouts
    )
    remaining_snapshots = [] if COMPONENT_SNAPSHOTS in skipped_components else sorted(
        str(path)
        for path in paths_for_ids(
            shell_snapshot_paths_by_session(codex_home, unsafe_artifacts),
            target_set,
        )
        if path_is_present(path) and str(path) not in expected_snapshots
    )
    remaining_generated = [] if COMPONENT_GENERATED in skipped_components else sorted(
        str(path)
        for path in paths_for_ids(
            generated_paths_by_session(codex_home, unsafe_artifacts),
            target_set,
        )
        if path_is_present(path) and str(path) not in expected_generated
    )
    artifact_warnings = sorted(dict.fromkeys(unsafe_artifacts))
    missing_protected_threads = (
        []
        if state is None and COMPONENT_CORE in skipped_components
        else sorted((protected_thread_ids or set()) - current_state_thread_ids)
    )
    state_integrity = (
        "skipped"
        if COMPONENT_CORE in skipped_components
        else integrity(state_path) if state_path is not None else "missing"
    )
    logs_integrity = (
        integrity(logs_path)
        if include_logs
        and COMPONENT_LOGS not in skipped_components
        and logs_path is not None
        else "skipped"
    )
    desktop_catalog_integrity = (
        "skipped"
        if COMPONENT_CATALOG in skipped_components
        else database_check(current_catalog_path, "integrity_check")
        if current_catalog_path is not None
        else "missing"
    )
    state_integrity_ok = (
        True
        if COMPONENT_CORE in skipped_components
        else
        state_integrity == "ok"
        if state_db_was_present
        else state_integrity == "missing"
    )
    logs_integrity_ok = (
        True
        if not include_logs or COMPONENT_LOGS in skipped_components
        else (
            logs_integrity == "ok"
            if logs_db_was_present
            else logs_integrity == "missing"
        )
    )
    desktop_catalog_integrity_ok = (
        True
        if COMPONENT_CATALOG in skipped_components
        else
        desktop_catalog_integrity == "ok"
        if desktop_catalog_was_present
        else desktop_catalog_integrity == "missing"
    )
    paginated_history_integrity_ok = (
        True
        if COMPONENT_PAGINATED_HISTORY in skipped_components
        else paginated_history_check == "ok"
        if paginated_history_expected_path is not None
        else paginated_history_check == "missing"
    )
    state_path_matches = (
        COMPONENT_CORE in skipped_components
        or current_state_path == state_database_expected_path
    )
    logs_path_matches = (
        not include_logs
        or COMPONENT_LOGS in skipped_components
        or current_logs_path == logs_database_expected_path
    )
    if not state_path_matches:
        verification_errors.append(
            "The authoritative state database path changed during deletion."
        )
    if not logs_path_matches:
        verification_errors.append(
            "The authoritative logs database path changed during deletion."
        )
    verification_ok = (
        all(value == 0 for value in residual.values())
        and not remaining_rollouts
        and not remaining_snapshots
        and not remaining_generated
        and state_integrity_ok
        and logs_integrity_ok
        and state_path_matches
        and logs_path_matches
        and desktop_catalog_integrity_ok
        and paginated_history_integrity_ok
        and not verification_errors
        and not missing_protected_threads
    )
    planned_deleted_remaining: dict[str, Any] = {
        key: value for key, value in residual.items() if value
    }
    if remaining_rollouts:
        planned_deleted_remaining["rollout_files"] = remaining_rollouts
    if remaining_snapshots:
        planned_deleted_remaining["shell_snapshots"] = remaining_snapshots
    if remaining_generated:
        planned_deleted_remaining["generated_artifacts"] = remaining_generated
    integrity_checks = {
        "state": state_integrity,
        "logs": logs_integrity,
        "desktop_catalog": desktop_catalog_integrity,
        "auxiliary_thread_databases": auxiliary_checks,
        "paginated_history": paginated_history_check,
    }

    return {
        "verification_ok": verification_ok,
        "verification_errors": sorted(dict.fromkeys(verification_errors)),
        "residual_counts": residual,
        "remaining_rollout_files": sorted(dict.fromkeys(remaining_rollouts)),
        "remaining_shell_snapshots": sorted(dict.fromkeys(remaining_snapshots)),
        "remaining_generated_artifacts": remaining_generated,
        "missing_protected_threads": missing_protected_threads,
        "state_integrity": state_integrity,
        "logs_integrity": logs_integrity,
        "desktop_catalog_integrity": desktop_catalog_integrity,
        "paginated_history_integrity": paginated_history_check,
        "paginated_history_database_path": (
            str(paginated_history_path)
            if paginated_history_path is not None
            else ""
        ),
        "desktop_catalog_revision": desktop_catalog_revision_after,
        "auxiliary_thread_database_checks": auxiliary_checks,
        "auxiliary_thread_contracts": auxiliary_contracts,
        "auxiliary_thread_databases_present": auxiliary_presence,
        "global_state_files_present": global_state_presence,
        "global_state_warnings": global_state_warnings,
        "artifact_warnings": artifact_warnings,
        "expected_preserved_artifacts": {
            component: sorted(contracts)
            for component, contracts in expected_preserved_artifacts.items()
            if contracts
        },
        "planned_deleted_remaining": planned_deleted_remaining,
        "expected_preserved_present": sorted(
            expected_preserved_present,
            key=lambda item: (item.get("component", ""), item.get("object_id", "")),
        ),
        "expected_preserved_missing": sorted(
            expected_preserved_missing,
            key=lambda item: (item.get("component", ""), item.get("object_id", "")),
        ),
        "unexpected_remaining": artifact_warnings,
        "unexpected_non_target_removed": missing_protected_threads,
        "integrity_checks": integrity_checks,
        "offline_verification_ok": True,
        "historical_snapshot_ok": True,
    }


HISTORICAL_SCOPE_KEYS = (
    "session_index_rows_without_state",
    "rollout_files_without_state",
    "shell_snapshots_without_state",
    "generated_artifacts_without_state",
    "logs_rows_without_state",
    "state_orphan_references",
    "state_threads_missing_rollout_file",
)


def historical_scope(residuals: dict[str, Any]) -> dict[str, Any]:
    return {
        key: residuals.get(key, [] if key != "state_orphan_references" else {})
        for key in HISTORICAL_SCOPE_KEYS
    }


def approved_historical_snapshot(
    plan: Plan,
    apply_missing_rollout_threads: bool,
    force_open: bool = False,
) -> dict[str, Any]:
    residuals = plan.historical_residuals
    snapshot = json.loads(
        json.dumps(
            {
                "scanned": bool(residuals.get("scanned", False)),
                **historical_scope(residuals),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    if not apply_missing_rollout_threads:
        snapshot["state_threads_missing_rollout_file"] = []
        return snapshot

    snapshot["state_threads_missing_rollout_file"] = [
        entry
        for entry in snapshot.get("state_threads_missing_rollout_file", [])
        if not entry.get("current_session")
        and (force_open or not entry.get("open_or_unknown"))
    ]
    missing_ids = entry_ids(snapshot["state_threads_missing_rollout_file"])
    snapshot["session_index_rows_without_state"].extend(
        session_index_entries_for_ids(
            plan.codex_home / "session_index.jsonl",
            missing_ids,
        )
    )
    logs = (
        connect_ro(plan.logs_database_path)
        if plan.include_logs and plan.logs_database_path is not None
        else None
    )
    try:
        snapshot["logs_rows_without_state"].extend(
            logs_entries_for_ids(logs, missing_ids)
        )
    finally:
        if logs is not None:
            logs.close()
    return snapshot


def entries_for_ids(
    entries: Iterable[dict[str, Any]], approved_ids: set[str]
) -> list[dict[str, Any]]:
    return [
        entry for entry in entries if str(entry.get("id", "")).lower() in approved_ids
    ]


def orphan_references_for_ids(
    references: dict[str, list[dict[str, Any]]], approved_ids: set[str]
) -> dict[str, list[dict[str, Any]]]:
    filtered: dict[str, list[dict[str, Any]]] = {}
    for table, entries in references.items():
        if table == "thread_spawn_edges":
            selected = [
                entry
                for entry in entries
                if str(entry.get("parent_thread_id", "")).lower() in approved_ids
                or str(entry.get("child_thread_id", "")).lower() in approved_ids
                or bool(
                    {str(sid).lower() for sid in entry.get("missing_thread_ids", [])}
                    & approved_ids
                )
            ]
        else:
            selected = entries_for_ids(entries, approved_ids)
        if selected:
            filtered[table] = selected
    return filtered


def cleanup_historical_residuals(
    codex_home: Path,
    residuals: dict[str, Any],
    include_logs: bool,
    excluded_ids: set[str],
    apply_missing_rollout_threads: bool,
    state_db_was_present: bool,
    logs_db_was_present: bool,
    expected_rollout_migration_state_rows: list[dict[str, Any]] | None = None,
    state_database_path: Path | None = None,
    logs_database_path: Path | None = None,
    mutation_observer: Any | None = None,
) -> dict[str, Any]:
    if not residuals.get("scanned", False):
        return {"applied": False, "reason": "historical scan was skipped"}

    historical_notified = False

    def notify_historical_mutation() -> None:
        nonlocal historical_notified
        if historical_notified:
            return
        historical_notified = True
        notify_mutation(mutation_observer, COMPONENT_HISTORICAL)

    fresh_residuals = scan_historical_residuals(codex_home, excluded_ids, include_logs)
    if not fresh_residuals.get("scanned", False):
        raise RuntimeError(
            "Historical cleanup became unavailable; retry from a new report."
        )

    discovered_state_path = discovered_database_path(
        discover_state_database(codex_home)
    )
    if state_database_path is not None and discovered_state_path != state_database_path:
        raise RuntimeError(
            "The selected state database changed after the approved report"
        )
    discovered_logs_path = discovered_database_path(
        discover_logs_database(codex_home)
    )
    if (
        include_logs
        and logs_database_path is not None
        and discovered_logs_path != logs_database_path
    ):
        raise RuntimeError(
            "The selected logs database changed after the approved report"
        )

    approved_index_entries = residuals.get("session_index_rows_without_state", [])
    approved_log_entries = residuals.get("logs_rows_without_state", [])
    approved_orphans = residuals.get("state_orphan_references", {})
    approved_missing_entries = residuals.get("state_threads_missing_rollout_file", [])
    if not isinstance(approved_index_entries, list):
        approved_index_entries = []
    if not isinstance(approved_log_entries, list):
        approved_log_entries = []
    if not isinstance(approved_orphans, dict):
        approved_orphans = {}
    if not isinstance(approved_missing_entries, list):
        approved_missing_entries = []
    if not apply_missing_rollout_threads:
        approved_missing_entries = []

    index_ids = entry_ids(approved_index_entries)
    log_ids = entry_ids(approved_log_entries)
    approved_orphan_ids = orphan_reference_ids(approved_orphans)
    missing_rollout_ids = (
        entry_ids(approved_missing_entries) if apply_missing_rollout_threads else set()
    )

    path_entries: list[dict[str, Any]] = []
    for key in [
        "rollout_files_without_state",
        "shell_snapshots_without_state",
        "generated_artifacts_without_state",
    ]:
        entries = residuals.get(key, [])
        if isinstance(entries, list):
            path_entries.extend(entries)

    if missing_rollout_ids:
        path_entries.extend(approved_missing_entries)

    approved_paths = unique_paths(paths_from_entries(path_entries))
    approved_path_contracts = path_contracts_from_entries(path_entries)
    excluded_lower = {sid.lower() for sid in excluded_ids}
    index_ids -= excluded_lower
    log_ids -= excluded_lower
    approved_orphan_ids -= excluded_lower
    missing_rollout_ids -= excluded_lower

    ordinary_path_ids: set[str] = set()
    for key in [
        "rollout_files_without_state",
        "shell_snapshots_without_state",
        "generated_artifacts_without_state",
    ]:
        entries = residuals.get(key, [])
        if isinstance(entries, list):
            ordinary_path_ids.update(entry_ids(entries))
    ordinary_no_state_ids = (
        index_ids | log_ids | ordinary_path_ids | approved_orphan_ids
    ) - missing_rollout_ids

    state_path = state_database_path or discovered_state_path
    if state_path is None:
        raise RuntimeError("The selected state database is unavailable")
    require_managed_sqlite(codex_home, state_path)
    state_conn = connect_rw(state_path)
    if state_conn is None:
        raise RuntimeError("The selected state database disappeared before historical cleanup")
    logs_conn: sqlite3.Connection | None = None
    try:
        state_conn.execute("BEGIN IMMEDIATE")
        state_issues = state_runtime_mutation_issues(
            state_conn,
            ordinary_no_state_ids | missing_rollout_ids | approved_orphan_ids,
        )
        if state_issues:
            raise RuntimeError(
                "State schema changed before historical cleanup: "
                + " | ".join(state_issues)
            )
        canonical_issues = reference_format_issues(
            state_conn,
            STATE_REFERENCE_LOCATIONS,
            state_path.name,
        ) + state_rollout_path_issues(state_conn, codex_home)
        if canonical_issues:
            raise RuntimeError(
                "State references changed before historical cleanup: "
                + " | ".join(canonical_issues)
            )

        locked_state_ids = state_thread_ids(state_conn)
        unsafe_artifacts: list[str] = []
        locked_rollouts = rollout_paths_by_session(
            codex_home,
            unsafe_artifacts,
            authoritative_session_ids=locked_state_ids,
        )
        locked_snapshots = shell_snapshot_paths_by_session(
            codex_home,
            unsafe_artifacts,
            authoritative_session_ids=locked_state_ids,
        )
        locked_generated = generated_paths_by_session(
            codex_home,
            unsafe_artifacts,
            authoritative_session_ids=locked_state_ids,
        )
        if unsafe_artifacts:
            raise RuntimeError(
                "Artifact safety changed before historical cleanup: "
                + " | ".join(sorted(dict.fromkeys(unsafe_artifacts)))
            )

        revived_ids = sorted(ordinary_no_state_ids & locked_state_ids)
        if revived_ids:
            raise RuntimeError(
                "Historical residual IDs became live state threads before cleanup: "
                + ", ".join(revived_ids)
            )

        locked_index_issues = session_index_issues(codex_home / "session_index.jsonl")
        if locked_index_issues:
            raise RuntimeError(
                "session_index.jsonl changed before historical cleanup: "
                + " | ".join(locked_index_issues)
            )
        locked_orphans = orphan_reference_entries(
            state_conn,
            locked_state_ids,
            excluded_lower,
        )
        deletable_orphan_ids: set[str] = set()
        orphan_already_absent: list[str] = []
        orphan_identity_changed: list[str] = []
        for sid in sorted(approved_orphan_ids):
            approved_for_id = orphan_references_for_ids(approved_orphans, {sid})
            locked_for_id = orphan_references_for_ids(locked_orphans, {sid})
            if locked_for_id == approved_for_id:
                deletable_orphan_ids.add(sid)
            elif not locked_for_id:
                orphan_already_absent.append(sid)
            else:
                orphan_identity_changed.append(sid)

        locked_missing = missing_rollout_entries(
            state_conn,
            codex_home,
            excluded_lower,
            locked_rollouts,
            locked_snapshots,
            locked_generated,
        )
        approved_locked_missing = entries_for_ids(
            locked_missing,
            missing_rollout_ids,
        )
        if approved_locked_missing != approved_missing_entries:
            raise RuntimeError(
                "Missing-rollout thread scope changed or edge status changed before cleanup"
            )

        log_cleanup = {
            "removed": 0,
            "already_absent": sum(
                len(entry.get("row_contracts", [])) for entry in approved_log_entries
            ),
            "identity_changed_retained": 0,
            "approved_row_ids": [],
        }
        if include_logs:
            logs_path = logs_database_path or discovered_logs_path
            if logs_path is None:
                raise RuntimeError("The selected logs database is unavailable")
            require_managed_sqlite(codex_home, logs_path)
            logs_conn = connect_rw(logs_path)
            if logs_db_was_present and logs_conn is None:
                raise RuntimeError(
                    "The selected logs database disappeared before historical cleanup"
                )
            if logs_conn is not None:
                logs_conn.execute("BEGIN IMMEDIATE")
                log_issues = logs_runtime_mutation_issues(
                    logs_conn,
                    log_ids,
                ) + reference_format_issues(
                    logs_conn,
                    [("logs", "thread_id")],
                    logs_path.name,
                )
                if log_issues:
                    raise RuntimeError(
                        "Logs schema or references changed before historical cleanup: "
                        + " | ".join(log_issues)
                    )
                if entries_for_ids(approved_log_entries, log_ids):
                    notify_historical_mutation()
                log_cleanup = delete_approved_log_rows_on_conn(
                    logs_conn,
                    entries_for_ids(approved_log_entries, log_ids),
                )

        paths: list[Path] = []
        path_already_absent: list[str] = []
        path_identity_changed: list[str] = []
        for path in approved_paths:
            expected_contract = approved_path_contracts.get(str(path))
            if not path_is_present(path):
                path_already_absent.append(str(path))
            elif (
                expected_contract is None
                or path_contract_entry(path) != expected_contract
            ):
                path_identity_changed.append(str(path))
            else:
                paths.append(path)

        approved_missing_skipped_rows = [
            row
            for entry in approved_missing_entries
            for row in entry.get("rollout_migration_skipped_rows", [])
        ]
        if (
            approved_missing_skipped_rows
            or deletable_orphan_ids
            or missing_rollout_ids
            or entries_for_ids(approved_index_entries, index_ids)
            or paths
        ):
            notify_historical_mutation()
        skipped_removed = delete_rollout_migration_skipped_rows_on_conn(
            state_conn,
            approved_missing_skipped_rows,
        )
        state_ids = sorted(deletable_orphan_ids | missing_rollout_ids)
        state_deleted = (
            delete_state_rows_on_conn(state_conn, state_ids) if state_ids else {}
        )
        state_deleted["rollout_migration_skipped_rollouts"] = skipped_removed
        index_cleanup = rewrite_approved_session_index_rows(
            codex_home,
            entries_for_ids(approved_index_entries, index_ids),
        )
        late_path_status: dict[str, list[str]] = {}
        paths_removed = remove_paths(
            codex_home,
            paths,
            [
                codex_home / "sessions",
                codex_home / "shell_snapshots",
                codex_home / "generated_images",
            ],
            approved_path_contracts,
            late_path_status,
        )
        path_already_absent = sorted(
            dict.fromkeys(
                path_already_absent + late_path_status.get("already_absent", [])
            )
        )
        path_identity_changed = sorted(
            dict.fromkeys(
                path_identity_changed
                + late_path_status.get("identity_changed_retained", [])
            )
        )
        paths_with_late_children = sorted(
            dict.fromkeys(late_path_status.get("retained_with_late_children", []))
        )

        if missing_rollout_ids and count_in_table(
            state_conn,
            "threads",
            "id",
            sorted(missing_rollout_ids),
        ):
            raise RuntimeError("Approved missing-rollout threads remain after cleanup.")
        approved_missing_rollout_paths = {
            str(entry.get("rollout_path", ""))
            for entry in approved_missing_entries
            if entry.get("rollout_path")
        }
        if rollout_migration_skipped_rows_for_paths(
            state_conn,
            approved_missing_rollout_paths,
        ):
            raise RuntimeError(
                "Approved missing-rollout migration skipped rows remain after cleanup."
            )
        remaining_orphans = orphan_references_for_ids(
            orphan_reference_entries(
                state_conn,
                state_thread_ids(state_conn),
                excluded_lower,
            ),
            deletable_orphan_ids,
        )
        if remaining_orphans:
            raise RuntimeError("Approved orphan references remain after cleanup.")

        if logs_conn is not None:
            logs_conn.commit()
        state_conn.commit()
    except Exception:
        if logs_conn is not None:
            logs_conn.rollback()
        state_conn.rollback()
        raise
    finally:
        if logs_conn is not None:
            logs_conn.close()
        state_conn.close()
    result: dict[str, Any] = {
        "applied": True,
        "state_deleted": state_deleted,
        "session_index_removed": index_cleanup["removed"],
        "logs_removed": log_cleanup["removed"],
        "paths_removed": paths_removed,
        "state_cleanup_ids": state_ids,
        "session_index_cleanup_ids": sorted(index_ids),
        "logs_cleanup_ids": sorted(log_ids),
        "path_cleanup_targets": [str(path) for path in paths],
        "missing_rollout_threads_applied": apply_missing_rollout_threads,
        "approved_snapshot": {
            "session_index": index_cleanup,
            "logs": log_cleanup,
            "paths": {
                "removed": paths_removed,
                "already_absent": path_already_absent,
                "identity_changed_retained": path_identity_changed,
                "retained_with_late_children": paths_with_late_children,
            },
            "orphan_references": {
                "deleted_ids": sorted(deletable_orphan_ids),
                "already_absent_ids": orphan_already_absent,
                "identity_changed_retained_ids": orphan_identity_changed,
            },
        },
    }
    post_scan = scan_historical_residuals(
        codex_home,
        excluded_ids,
        include_logs,
    )
    state_integrity = integrity(state_path)
    verified_logs_path = logs_database_path or discovered_database_path(
        discover_logs_database(codex_home)
    )
    logs_integrity = (
        integrity(verified_logs_path)
        if include_logs and verified_logs_path is not None
        else "missing"
        if include_logs
        else "skipped"
    )
    state_integrity_ok = (
        state_integrity == "ok"
        if state_db_was_present
        else state_integrity == "missing"
    )
    logs_integrity_ok = (
        True
        if not include_logs
        else (
            logs_integrity == "ok"
            if logs_db_was_present
            else logs_integrity == "missing"
        )
    )
    cleanup_ok = state_integrity_ok and logs_integrity_ok
    result["verification"] = {
        "cleanup_ok": cleanup_ok,
        "historical_residuals": post_scan,
        "state_integrity": state_integrity,
        "logs_integrity": logs_integrity,
        "approved_remaining": [],
        "retained_unapproved": post_scan.get("summary", {}),
    }
    return result


def global_state_ref_matches_targets(
    contract: dict[str, Any], target_ids: set[str]
) -> bool:
    encoded = json.dumps(contract, ensure_ascii=False, sort_keys=True).lower()
    return any(sid in encoded for sid in target_ids)


def plan_desktop_mutation_components(plan: Plan) -> set[str]:
    components: set[str] = set()
    if component_plan_enabled(plan, COMPONENT_PAGINATED_HISTORY) and any(
        plan.paginated_history_rows.values()
    ):
        components.add(COMPONENT_PAGINATED_HISTORY)
    if component_plan_enabled(plan, COMPONENT_CATALOG) and any(
        bool(rows) for rows in plan.desktop_catalog_rows.values()
    ):
        components.add(COMPONENT_CATALOG)
    if component_plan_enabled(plan, COMPONENT_AUXILIARY) and any(
        isinstance(contract, dict) and bool(contract.get("rows", []))
        for contract in plan.auxiliary_thread_contracts.values()
    ):
        components.add(COMPONENT_AUXILIARY)
    if component_plan_enabled(plan, COMPONENT_GLOBAL_STATE) and any(
        bool(refs) for refs in plan.global_state_refs.values()
    ):
        components.add(COMPONENT_GLOBAL_STATE)
    return components


def narrow_plan_for_execution(
    plan: Plan,
    execution_snapshot: dict[str, Any],
    force_open: bool,
) -> tuple[Plan, list[dict[str, Any]], list[dict[str, Any]]]:
    approved_targets = {
        str(sid).lower()
        for sid in execution_snapshot.get("executable_target_ids", [])
        if isinstance(sid, str)
    }
    approved_retained_targets = {
        str(sid).lower()
        for sid in execution_snapshot.get("retained_target_ids", [])
        if isinstance(sid, str)
    }
    approved_all_targets = approved_targets | approved_retained_targets
    current_targets = set(plan.target_ids)
    executable_targets = approved_targets & current_targets
    dynamic_warnings: list[dict[str, Any]] = []
    retained = list(execution_snapshot.get("retained_units", []))
    retained_identities = {
        (str(item.get("component", "")), str(item.get("object_id", "")))
        for item in retained
        if isinstance(item, dict)
    }
    for item in plan.retained_objects:
        identity = (
            str(item.get("component", "")),
            str(item.get("object_id", "")),
        )
        if identity in retained_identities:
            current_contract = item.get("preserved_contract")
            approved_item = next(
                (
                    existing
                    for existing in reversed(retained)
                    if isinstance(existing, dict)
                    and str(existing.get("component", "")) == identity[0]
                    and str(existing.get("object_id", "")) == identity[1]
                ),
                {},
            )
            if (
                isinstance(current_contract, dict)
                and approved_item.get("preserved_contract") != current_contract
            ):
                retained.append(
                    {
                        **item,
                        "status": "identity_changed_retained",
                        "reason": "An unsafe retained object changed after approval and "
                        "its current identity was preserved.",
                    }
                )
            continue
        retained.append(
            {
                **item,
                "status": "retained_unapproved_addition",
                "reason": "An unsafe target object appeared after approval and was preserved.",
            }
        )
        retained_identities.add(identity)

    for sid in sorted(approved_targets - current_targets):
        retained.append(
            {
                "component": COMPONENT_CORE,
                "session_id": sid,
                "object_id": sid,
                "status": "already_absent_or_no_longer_resolved",
                "reason": "The approved target is no longer in the current resolved graph.",
            }
        )
    for sid in sorted(current_targets - approved_all_targets):
        retained.append(
            {
                "component": COMPONENT_CORE,
                "session_id": sid,
                "object_id": sid,
                "status": "retained_unapproved_addition",
                "reason": "The target appeared after approval and was not added to the deletion scope.",
            }
        )

    newly_unsafe = {
        sid
        for sid in executable_targets
        if plan.target_dispositions.get(sid, {}).get("status") == "retained"
        or (
            plan.target_dispositions.get(sid, {}).get("status")
            == "requires_force_open"
            and not force_open
        )
    }
    for connected in target_connected_components(plan):
        if connected & newly_unsafe:
            newly_unsafe.update(connected & executable_targets)
    for sid in sorted(newly_unsafe):
        executable_targets.discard(sid)
        reasons = plan.target_dispositions.get(sid, {}).get("reasons", [])
        retained.append(
            {
                "component": COMPONENT_CORE,
                "session_id": sid,
                "object_id": sid,
                "status": "retained_safety_changed",
                "reason": "; ".join(str(item) for item in reasons)
                or "The target became unsafe after approval.",
            }
        )

    frozen_component_plans = execution_snapshot.get("component_plans", {})
    effective_components: dict[str, dict[str, Any]] = {}
    for component in ALL_COMPONENTS:
        frozen = frozen_component_plans.get(component, {})
        current = plan.component_plans.get(component, {})
        enabled = (
            frozen.get("status", "enabled") == "enabled"
            and current.get("status", "enabled") == "enabled"
        )
        reasons = sorted(
            dict.fromkeys(
                [str(item) for item in frozen.get("reasons", [])]
                + [str(item) for item in current.get("reasons", [])]
            )
        )
        effective_components[component] = {
            "status": "enabled" if enabled else "skipped",
            "reasons": reasons,
        }

    object_contracts = execution_snapshot.get("object_contracts", {})
    approved_paginated_targets = {
        sid
        for sid in executable_targets
        if isinstance(object_contracts.get("threads", {}).get(sid), dict)
        and object_contracts["threads"][sid].get("history_mode") == "paginated"
    }
    paginated_dependency_changed = False
    if approved_paginated_targets:
        current_paginated_contract = paginated_prewrite_comparison_contract(
            filtered_paginated_history_contract(
                plan.paginated_history_contract,
                approved_paginated_targets,
            )
        )
        approved_paginated_contract = paginated_prewrite_comparison_contract(
            filtered_paginated_history_contract(
                object_contracts.get("paginated_history_contract", {}),
                approved_paginated_targets,
            )
        )
        approved_paginated_path = object_contracts.get(
            "paginated_history_database_path", ""
        )
        current_paginated_path = (
            str(plan.paginated_history_database_path)
            if plan.paginated_history_database_path is not None
            else ""
        )
        paginated_dependency_changed = (
            current_paginated_contract != approved_paginated_contract
            or current_paginated_path != approved_paginated_path
            or paginated_prewrite_comparison_contract(
                plan.paginated_history_database_plan
            )
            != paginated_prewrite_comparison_contract(
                object_contracts.get("paginated_history_database_plan", {})
            )
        )
    if paginated_dependency_changed:
        message = (
            "Paginated history changed after approval; every dependent paginated "
            "target was retained before any target component could mutate."
        )
        effective_components[COMPONENT_PAGINATED_HISTORY] = {
            "status": "skipped",
            "reasons": [message],
        }
        for sid in sorted(approved_paginated_targets):
            executable_targets.discard(sid)
            for unit in execution_snapshot.get("executable_units", []):
                if not isinstance(unit, dict) or str(unit.get("session_id", "")) != sid:
                    continue
                retained.append(
                    {
                        **unit,
                        "status": "retained_paginated_dependency_changed",
                        "reason": message,
                    }
                )
            dynamic_warnings.append(
                safety_warning(
                    "paginated_target_dependency_changed",
                    message,
                    COMPONENT_PAGINATED_HISTORY,
                    DISPOSITION_RETAIN_TARGET_COMPONENT,
                    session_id=sid,
                )
            )
    current_thread_contract = {
        sid: {
            "rollout_path": plan.threads[sid].rollout_path,
            "history_mode": plan.threads[sid].history_mode,
        }
        for sid in sorted(executable_targets)
        if sid in plan.threads
    }
    approved_thread_contract = {
        sid: value
        for sid, value in object_contracts.get("threads", {}).items()
        if sid in executable_targets
    }
    current_edges = [
        edge
        for edge in plan.target_edge_rows
        if str(edge.get("parent_thread_id", "")).lower() in executable_targets
        or str(edge.get("child_thread_id", "")).lower() in executable_targets
    ]
    approved_edges = [
        edge
        for edge in object_contracts.get("target_edge_rows", [])
        if str(edge.get("parent_thread_id", "")).lower() in executable_targets
        or str(edge.get("child_thread_id", "")).lower() in executable_targets
    ]
    if (
        current_thread_contract != approved_thread_contract
        or current_edges != approved_edges
        or plan.preflight.get("state_mutation_effect_assessment", {})
        != object_contracts.get("state_mutation_effect_assessment", {})
    ):
        message = (
            "Target graph or thread storage changed after approval; the core state/index "
            "component was retained while independent components may continue."
        )
        effective_components[COMPONENT_CORE] = {
            "status": "skipped",
            "reasons": [message],
        }
        dynamic_warnings.append(
            safety_warning(
                "core_contract_changed",
                message,
                COMPONENT_CORE,
                DISPOSITION_SKIP_COMPONENT,
            )
        )

    index_path = plan.codex_home / "session_index.jsonl"
    current_index_contract = (
        session_index_entries_for_ids(index_path, executable_targets)
        if effective_components[COMPONENT_CORE]["status"] == "enabled"
        and managed_file_issue(plan.codex_home, index_path) is None
        and not session_index_issues(index_path)
        else []
    )
    approved_index_contract = entries_for_ids(
        object_contracts.get("session_index_rows", []),
        executable_targets,
    )
    if current_index_contract != approved_index_contract:
        message = "Approved target session-index rows changed after approval."
        effective_components[COMPONENT_CORE] = {
            "status": "skipped",
            "reasons": [message],
        }
        dynamic_warnings.append(
            safety_warning(
                "index_contract_changed",
                message,
                COMPONENT_CORE,
                DISPOSITION_SKIP_COMPONENT,
            )
        )

    logs = (
        connect_ro(plan.logs_database_path)
        if plan.include_logs
        and plan.logs_database_path is not None
        and component_plan_enabled(plan, COMPONENT_LOGS)
        and not managed_sqlite_issues(
            plan.codex_home, plan.logs_database_path
        )
        else None
    )
    try:
        current_log_contract = logs_entries_for_ids(logs, executable_targets)
    finally:
        if logs is not None:
            logs.close()
    approved_log_contract = entries_for_ids(
        object_contracts.get("log_rows", []), executable_targets
    )
    if current_log_contract != approved_log_contract:
        message = "Approved target log rows changed after approval."
        effective_components[COMPONENT_LOGS] = {
            "status": "skipped",
            "reasons": [message],
        }
        dynamic_warnings.append(
            safety_warning(
                "logs_contract_changed",
                message,
                COMPONENT_LOGS,
                DISPOSITION_SKIP_COMPONENT,
            )
        )

    artifact_components = {
        COMPONENT_ROLLOUTS,
        COMPONENT_SNAPSHOTS,
        COMPONENT_GENERATED,
    }
    approved_unit_paths_by_component = {
        component: {
            str(unit.get("object_id", ""))
            for unit in execution_snapshot.get("executable_units", [])
            if unit.get("component") == component
            and str(unit.get("session_id", "")) in executable_targets
        }
        for component in artifact_components
    }
    approved_artifact_contracts = object_contracts.get("artifact_contracts", {})

    def safe_paths(paths: list[Path], component: str) -> list[Path]:
        selected: list[Path] = []
        current_paths = {str(path): path for path in paths}
        approved_paths = approved_unit_paths_by_component.get(component, set())
        for path_text in sorted(set(current_paths) | approved_paths):
            path = current_paths.get(path_text, Path(path_text))
            expected = approved_artifact_contracts.get(path_text)
            current_contract = plan.artifact_contracts.get(path_text)
            if path_text not in approved_paths:
                retained.append(
                    {
                        "component": component,
                        "session_id": next(
                            iter(
                                {
                                    match.lower()
                                    for match in UUID_RE.findall(path_text)
                                }
                                & executable_targets
                            ),
                            "",
                        ),
                        "object_id": path_text,
                        "status": "retained_unapproved_addition",
                        "reason": "A target artifact appeared after approval and was preserved.",
                        "preserved_contract": current_contract,
                    }
                )
                continue
            if (
                expected is not None
                and path_is_present(path)
                and current_contract == expected
            ):
                selected.append(path)
                continue
            retained_item: dict[str, Any] = {
                "component": component,
                "object_id": path_text,
                "status": (
                    "already_absent"
                    if not path_is_present(path)
                    else "identity_changed_retained"
                ),
                "reason": "The approved artifact was absent or changed and was not touched.",
            }
            if path_is_present(path):
                if current_contract is None:
                    try:
                        current_contract = path_contract_entry(path)
                    except (OSError, RuntimeError, UnicodeError):
                        current_contract = None
                if current_contract is not None:
                    retained_item["preserved_contract"] = current_contract
            retained.append(retained_item)
        return selected

    selected_rollouts = safe_paths(plan.rollout_files, COMPONENT_ROLLOUTS)
    selected_snapshots = safe_paths(plan.shell_snapshots, COMPONENT_SNAPSHOTS)
    selected_generated = safe_paths(plan.generated_artifacts, COMPONENT_GENERATED)
    selected_paths = selected_rollouts + selected_snapshots + selected_generated
    selected_contracts = {
        str(path): approved_artifact_contracts[str(path)] for path in selected_paths
    }

    filtered_catalog_rows = {
        table: [
            row
            for row in rows
            if str(row.get("thread_id", "")).lower() in executable_targets
        ]
        for table, rows in plan.desktop_catalog_rows.items()
    }
    approved_catalog_rows = {
        table: [
            row
            for row in rows
            if str(row.get("thread_id", "")).lower() in executable_targets
        ]
        for table, rows in object_contracts.get("desktop_catalog_rows", {}).items()
    }
    if (
        filtered_catalog_rows != approved_catalog_rows
        or plan.desktop_catalog_schema_signature
        != object_contracts.get("desktop_catalog_schema_signature")
        or plan.desktop_catalog_user_version
        != object_contracts.get("desktop_catalog_user_version")
    ):
        message = "Desktop catalog target contracts changed after approval."
        effective_components[COMPONENT_CATALOG] = {
            "status": "skipped",
            "reasons": [message],
        }
        dynamic_warnings.append(
            safety_warning(
                "catalog_contract_changed",
                message,
                COMPONENT_CATALOG,
                DISPOSITION_SKIP_COMPONENT,
            )
        )

    filtered_auxiliary: dict[str, dict[str, Any]] = {}
    for filename, contract in plan.auxiliary_thread_contracts.items():
        filtered = json.loads(json.dumps(contract, ensure_ascii=False))
        filtered["rows"] = [
            row
            for row in contract.get("rows", [])
            if str(row.get("thread_id", "")).lower() in executable_targets
        ]
        filtered_auxiliary[filename] = filtered
    approved_auxiliary: dict[str, dict[str, Any]] = {}
    for filename, contract in object_contracts.get(
        "auxiliary_thread_contracts", {}
    ).items():
        if not isinstance(contract, dict):
            continue
        filtered = json.loads(json.dumps(contract, ensure_ascii=False))
        filtered["rows"] = [
            row
            for row in contract.get("rows", [])
            if str(row.get("thread_id", "")).lower() in executable_targets
        ]
        approved_auxiliary[filename] = filtered
    if (
        filtered_auxiliary != approved_auxiliary
        or plan.auxiliary_thread_databases_present
        != object_contracts.get("auxiliary_thread_databases_present")
    ):
        message = "Auxiliary thread database contracts changed after approval."
        effective_components[COMPONENT_AUXILIARY] = {
            "status": "skipped",
            "reasons": [message],
        }
        dynamic_warnings.append(
            safety_warning(
                "auxiliary_contract_changed",
                message,
                COMPONENT_AUXILIARY,
                DISPOSITION_SKIP_COMPONENT,
            )
        )

    filtered_paginated = paginated_prewrite_comparison_contract(
        filtered_paginated_history_contract(
            plan.paginated_history_contract,
            executable_targets,
        )
    )
    approved_paginated = paginated_prewrite_comparison_contract(
        filtered_paginated_history_contract(
            object_contracts.get("paginated_history_contract", {}),
            executable_targets,
        )
    )
    approved_paginated_path = object_contracts.get(
        "paginated_history_database_path", ""
    )
    current_paginated_path = (
        str(plan.paginated_history_database_path)
        if plan.paginated_history_database_path is not None
        else ""
    )
    active_paginated_targets = {
        sid
        for sid in executable_targets
        if isinstance(object_contracts.get("threads", {}).get(sid), dict)
        and object_contracts["threads"][sid].get("history_mode") == "paginated"
    }
    if active_paginated_targets and (
        filtered_paginated != approved_paginated
        or current_paginated_path != approved_paginated_path
        or paginated_prewrite_comparison_contract(
            plan.paginated_history_database_plan
        )
        != paginated_prewrite_comparison_contract(
            object_contracts.get("paginated_history_database_plan", {})
        )
    ):
        message = "Paginated history target contracts changed after approval."
        effective_components[COMPONENT_PAGINATED_HISTORY] = {
            "status": "skipped",
            "reasons": [message],
        }
        dynamic_warnings.append(
            safety_warning(
                "paginated_history_contract_changed",
                message,
                COMPONENT_PAGINATED_HISTORY,
                DISPOSITION_SKIP_COMPONENT,
            )
        )

    filtered_global_refs = {
        filename: [
            contract
            for contract in contracts
            if global_state_ref_matches_targets(contract, executable_targets)
        ]
        for filename, contracts in plan.global_state_refs.items()
    }
    approved_global_refs = {
        filename: [
            contract
            for contract in contracts
            if global_state_ref_matches_targets(contract, executable_targets)
        ]
        for filename, contracts in object_contracts.get("global_state_refs", {}).items()
    }
    if (
        filtered_global_refs != approved_global_refs
        or plan.global_state_files_present
        != object_contracts.get("global_state_files_present")
    ):
        message = "Managed global-state target contracts changed after approval."
        effective_components[COMPONENT_GLOBAL_STATE] = {
            "status": "skipped",
            "reasons": [message],
        }
        dynamic_warnings.append(
            safety_warning(
                "global_state_contract_changed",
                message,
                COMPONENT_GLOBAL_STATE,
                DISPOSITION_SKIP_COMPONENT,
            )
        )

    narrowed_threads = {
        sid: info for sid, info in plan.threads.items() if sid in executable_targets
    }
    narrowed_incoming = {
        sid: edges
        for sid, edges in plan.target_incoming_edges.items()
        if sid in executable_targets
    }
    narrowed_edges = [
        edge
        for edge in plan.target_edge_rows
        if str(edge.get("parent_thread_id", "")).lower() in executable_targets
        or str(edge.get("child_thread_id", "")).lower() in executable_targets
    ]
    narrowed_rollout_paths = {
        info.rollout_path for info in narrowed_threads.values() if info.rollout_path
    }
    narrowed_skipped_rows = [
        row
        for row in plan.rollout_migration_skipped_rows
        if str(row.get("rollout_path", "")) in narrowed_rollout_paths
    ]
    narrowed = replace(
        plan,
        root_ids=sorted(executable_targets),
        target_ids=sorted(executable_targets),
        threads=narrowed_threads,
        open_subagents=[
            sid for sid in plan.open_subagents if sid in executable_targets
        ],
        rollout_files=selected_rollouts,
        shell_snapshots=selected_snapshots,
        generated_artifacts=selected_generated,
        artifact_contracts=selected_contracts,
        target_incoming_edges=narrowed_incoming,
        target_edge_rows=narrowed_edges,
        include_subagents=False,
        desktop_catalog_rows=filtered_catalog_rows,
        auxiliary_thread_contracts=filtered_auxiliary,
        auxiliary_thread_rows={
            filename: len(contract.get("rows", []))
            for filename, contract in filtered_auxiliary.items()
        },
        paginated_history_contract=filtered_paginated,
        paginated_history_rows={
            table: len(rows)
            for table, rows in filtered_paginated.get("rows", {}).items()
            if isinstance(rows, list)
        },
        global_state_refs=filtered_global_refs,
        rollout_migration_skipped_rows=narrowed_skipped_rows,
        component_plans=effective_components,
    )
    return narrowed, dynamic_warnings, retained


def _apply_plan_with_global_lock_held(
    plan: Plan,
    approved_historical_residuals: dict[str, Any],
    include_logs: bool,
    scan_historical: bool,
    apply_historical_residuals: bool,
    apply_missing_rollout_threads: bool,
    approval_scope: str,
    execution_snapshot: dict[str, Any] | None = None,
    force_open: bool = False,
    mutation_observer: Any | None = None,
) -> dict[str, Any]:
    execution_snapshot = execution_snapshot or approval_execution_snapshot(
        plan, force_open
    )
    effective_plan, dynamic_warnings, retained = narrow_plan_for_execution(
        plan,
        execution_snapshot,
        force_open,
    )
    effective_scope_snapshot = approval_execution_snapshot(
        effective_plan,
        force_open,
    )
    approved_target_work_components = execution_snapshot_target_work_components(
        effective_scope_snapshot
    )
    approved_missing_rollout_ids = entry_ids(
        approved_historical_residuals.get("state_threads_missing_rollout_file", [])
    )
    for entry in plan.historical_residuals.get(
        "state_threads_missing_rollout_file", []
    ):
        if not isinstance(entry, dict):
            continue
        sid = str(entry.get("id", "")).lower()
        if sid in approved_missing_rollout_ids or not (
            entry.get("current_session") is True
            or entry.get("open_or_unknown") is True
        ):
            continue
        retained.append(
            {
                "component": COMPONENT_HISTORICAL,
                "session_id": sid,
                "object_id": str(entry.get("rollout_path", sid)),
                "status": (
                    "retained_current_session"
                    if entry.get("current_session") is True
                    else "retained_requires_force_open"
                ),
                "reason": (
                    "The current Codex session is always preserved."
                    if entry.get("current_session") is True
                    else "The open or unknown-status session was not explicitly included."
                ),
            }
        )
    historical_desktop_work = apply_historical_residuals and (
        historical_snapshot_has_approved_work(approved_historical_residuals)
    )
    if plan_desktop_mutation_components(effective_plan) or historical_desktop_work:
        owners, owner_issue = desktop_owner_processes(effective_plan.codex_home)
        if owner_issue:
            raise DesktopOfflineGate(owner_issue, "final_approve_and_launch")
        if owners:
            raise DesktopOfflineGate(
                "Codex Desktop still owns catalog or global UI state.",
                "quit_desktop",
            )
    target_ids = effective_plan.target_ids
    preserved_component_baselines = expected_preserved_component_contracts(
        effective_plan,
        target_ids,
        {
            COMPONENT_CORE,
            COMPONENT_LOGS,
            COMPONENT_CATALOG,
            COMPONENT_AUXILIARY,
            COMPONENT_GLOBAL_STATE,
            COMPONENT_PAGINATED_HISTORY,
        },
    )
    component_results: dict[str, dict[str, Any]] = {}
    deleted: list[dict[str, Any]] = []
    all_warnings = [dict(finding) for finding in plan.safety_warnings] + [
        dict(finding) for finding in dynamic_warnings
    ]
    mutation_started = False
    mutation_components: list[str] = []

    def retain_component_units(component: str, reason: str) -> None:
        for unit in execution_snapshot.get("executable_units", []):
            if unit.get("component") != component:
                continue
            sid = str(unit.get("session_id", ""))
            if sid and sid not in target_ids:
                continue
            retained.append(
                {
                    **unit,
                    "status": "retained_component_skipped",
                    "reason": reason,
                }
            )

    def run_component(
        component: str,
        action: Any,
        default: Any,
        *,
        requested: bool = True,
        gate_on_prewrite_failure: bool = False,
    ) -> Any:
        nonlocal mutation_started
        plan_entry = effective_plan.component_plans.get(
            component, {"status": "enabled", "reasons": []}
        )
        if plan_entry.get("status") != "enabled":
            reason = "; ".join(
                str(item) for item in plan_entry.get("reasons", [])
            ) or "The component is outside the safe executable set."
            component_results[component] = {
                "status": "skipped_safely",
                "mutation_started": False,
                "reason": reason,
            }
            retain_component_units(component, reason)
            return default
        if not requested:
            component_results[component] = {
                "status": "not_requested",
                "mutation_started": False,
            }
            return default

        component_started = False

        def observe_mutation(observed_component: str = component) -> None:
            nonlocal component_started, mutation_started
            if component_started:
                return
            if not mutation_started and mutation_observer is not None:
                mutation_observer(observed_component)
            component_started = True
            mutation_started = True
            mutation_components.append(component)

        try:
            value = action(observe_mutation)
        except (OSError, RuntimeError, sqlite3.Error, UnicodeError) as exc:
            if component_started:
                raise PartialMutationError(component, exc) from exc
            if gate_on_prewrite_failure:
                raise RuntimeError(
                    f"{component} prewrite contract failed: {exc}"
                ) from exc
            message = f"{component} was skipped before mutation: {exc}"
            effective_plan.component_plans[component] = {
                "status": "skipped",
                "reasons": [message],
            }
            finding = safety_warning(
                "component_prewrite_failure",
                message,
                component,
                DISPOSITION_SKIP_COMPONENT,
            )
            all_warnings.append(finding)
            component_results[component] = {
                "status": "skipped_safely",
                "mutation_started": False,
                "reason": str(exc),
            }
            retain_component_units(component, str(exc))
            return default
        component_results[component] = {
            "status": "completed",
            "mutation_started": component_started,
            "result": value,
        }
        deleted.append({"component": component, "result": value})
        return value

    approved_object_contracts = execution_snapshot.get("object_contracts", {})
    approved_target_counts = approved_object_contracts.get("target_counts", {})
    core_count_keys = {
        "state_threads",
        "state_thread_spawn_edges",
        "state_thread_dynamic_tools",
        "state_thread_goals",
        "state_stage1_outputs",
        "state_agent_job_items_assigned",
        "session_index_rows",
    }
    core_work_requested = any(
        int(approved_target_counts.get(key, 0) or 0) > 0
        for key in core_count_keys
    ) or bool(effective_plan.rollout_migration_skipped_rows)
    logs_work_requested = include_logs and int(
        approved_target_counts.get("logs_rows", 0) or 0
    ) > 0
    paginated_history_work_requested = any(
        int(approved_target_counts.get(key, 0) or 0) > 0
        for key in {
            "paginated_history_projection_rows",
            "paginated_history_turn_rows",
            "paginated_history_item_rows",
        }
    )
    historical_work_requested = apply_historical_residuals and (
        historical_snapshot_has_approved_work(approved_historical_residuals)
    )
    approved_historical_snapshot_empty = bool(
        apply_historical_residuals
        and approved_historical_residuals.get("scanned") is True
        and not historical_work_requested
    )
    empty_historical_result = {
        "applied": False,
        "approved_snapshot_empty": True,
        "reason": "The approved historical snapshot contained no objects.",
        "verification": {
            "cleanup_ok": True,
            "approved_snapshot_empty": True,
            "historical_residuals": {
                "scanned": False,
                "authoritative": False,
                "approved_snapshot_empty": True,
                "reason": (
                    "No historical rescan is required to verify an approved empty "
                    "snapshot; later objects remain outside the approved scope."
                ),
                "summary": {
                    "has_residuals": False,
                    "issue_categories": 0,
                    "total_ids": 0,
                    "total_items": 0,
                },
            },
        },
    }

    paginated_history_cleanup = run_component(
        COMPONENT_PAGINATED_HISTORY,
        lambda observer: apply_target_paginated_history(
            effective_plan,
            observer,
        ),
        {
            "database_present": bool(
                effective_plan.paginated_history_database_path
            ),
            "rows_removed": {
                table: 0 for table in PAGINATED_HISTORY_PRIMARY_KEYS
            },
            "already_absent": 0,
            "integrity_check": "not_requested",
        },
        requested=paginated_history_work_requested,
        gate_on_prewrite_failure=True,
    )

    historical_cleanup_result = run_component(
        COMPONENT_HISTORICAL,
        lambda observer: cleanup_historical_residuals(
            effective_plan.codex_home,
            approved_historical_residuals,
            include_logs,
            set(plan.target_ids),
            apply_missing_rollout_threads,
            bool(plan.preflight.get("state_db_present")),
            bool(plan.preflight.get("logs_db_present")),
            state_database_path=effective_plan.state_database_path,
            logs_database_path=effective_plan.logs_database_path,
            mutation_observer=observer,
        ),
        empty_historical_result
        if approved_historical_snapshot_empty
        else {"applied": False},
        requested=historical_work_requested,
    )
    historical_state_cleanup_ids = {
        str(sid).lower()
        for sid in historical_cleanup_result.get("state_cleanup_ids", [])
    }
    session_index_removed, state_deleted = run_component(
        COMPONENT_CORE,
        lambda observer: apply_target_index_and_state(
            effective_plan,
            historical_state_cleanup_ids,
            observer,
        ),
        (0, {}),
        requested=core_work_requested,
    )
    logs_removed = run_component(
        COMPONENT_LOGS,
        lambda observer: delete_log_rows(
            effective_plan.codex_home,
            target_ids,
            observer,
            effective_plan.logs_database_path,
        ),
        0,
        requested=logs_work_requested,
    )

    def remove_artifact_component(
        component: str,
        paths: list[Path],
        root: Path,
        observer: Any,
    ) -> dict[str, Any]:
        retained_status: dict[str, list[str]] = {}
        removed = remove_paths(
            effective_plan.codex_home,
            paths,
            [root],
            effective_plan.artifact_contracts,
            retained_status,
            observer,
            component,
        )
        for status, retained_paths in retained_status.items():
            for path in retained_paths:
                retained_item: dict[str, Any] = {
                    "component": component,
                    "object_id": path,
                    "status": status,
                    "reason": "The artifact changed or disappeared during apply.",
                }
                retained_path = Path(path)
                if status != "already_absent" and path_is_present(retained_path):
                    try:
                        retained_item["preserved_contract"] = path_contract_entry(
                            retained_path
                        )
                    except (OSError, RuntimeError, UnicodeError):
                        pass
                retained.append(retained_item)
        return {"removed": removed, "retained": retained_status}

    rollout_cleanup = run_component(
        COMPONENT_ROLLOUTS,
        lambda observer: remove_artifact_component(
            COMPONENT_ROLLOUTS,
            effective_plan.rollout_files,
            effective_plan.codex_home / "sessions",
            observer,
        ),
        {"removed": 0, "retained": {}},
        requested=bool(effective_plan.rollout_files),
    )
    snapshot_cleanup = run_component(
        COMPONENT_SNAPSHOTS,
        lambda observer: remove_artifact_component(
            COMPONENT_SNAPSHOTS,
            effective_plan.shell_snapshots,
            effective_plan.codex_home / "shell_snapshots",
            observer,
        ),
        {"removed": 0, "retained": {}},
        requested=bool(effective_plan.shell_snapshots),
    )
    generated_cleanup = run_component(
        COMPONENT_GENERATED,
        lambda observer: remove_artifact_component(
            COMPONENT_GENERATED,
            effective_plan.generated_artifacts,
            effective_plan.codex_home / "generated_images",
            observer,
        ),
        {"removed": 0, "retained": {}},
        requested=bool(effective_plan.generated_artifacts),
    )
    catalog_work_requested = any(
        bool(rows) for rows in effective_plan.desktop_catalog_rows.values()
    )
    auxiliary_work_requested = any(
        isinstance(contract, dict) and bool(contract.get("rows", []))
        for contract in effective_plan.auxiliary_thread_contracts.values()
    )
    global_state_work_requested = any(
        bool(refs) for refs in effective_plan.global_state_refs.values()
    )
    desktop_catalog_cleanup = run_component(
        COMPONENT_CATALOG,
        lambda observer: apply_target_desktop_catalog(effective_plan, observer),
        {
            "database_present": bool(effective_plan.desktop_catalog_path),
            "rows_removed": {},
            "catalog_revision_before": effective_plan.desktop_catalog_revision,
            "catalog_revision_after": effective_plan.desktop_catalog_revision,
            "catalog_revision_increment": 0,
            "observation_sequence_increments": {},
        },
        requested=catalog_work_requested,
    )
    auxiliary_thread_cleanup = run_component(
        COMPONENT_AUXILIARY,
        lambda observer: apply_target_auxiliary_databases(
            effective_plan, observer
        ),
        {
            "databases_present": effective_plan.auxiliary_thread_databases_present,
            "rows_removed": {},
            "integrity_checks": {},
            "database_results": {},
            "verification_plans": effective_plan.auxiliary_thread_database_plans,
        },
        requested=auxiliary_work_requested,
    )
    for filename, database_result in sorted(
        auxiliary_thread_cleanup.get("database_results", {}).items()
    ):
        if database_result.get("status") != "skipped_safely":
            continue
        reason = str(database_result.get("reason", "Auxiliary database preserved."))
        retained.append(
            {
                "component": COMPONENT_AUXILIARY,
                "object_id": filename,
                "status": "retained_database_skipped",
                "reason": reason,
            }
        )
        finding = safety_warning(
            "auxiliary_database_changed_preserved",
            reason,
            COMPONENT_AUXILIARY,
            DISPOSITION_RETAIN_OBJECT,
            object_id=filename,
        )
        if not any(
            item.get("code") == finding.get("code")
            and item.get("object_id") == filename
            for item in all_warnings
        ):
            all_warnings.append(finding)
    global_state_cleanup = run_component(
        COMPONENT_GLOBAL_STATE,
        lambda observer: apply_target_global_state(effective_plan, observer),
        {
            "removed_by_file": {
                filename: 0 for filename in GLOBAL_STATE_FILENAMES
            },
            "removed": 0,
        },
        requested=global_state_work_requested,
    )

    result: dict[str, Any] = {
        "approval_scope": approval_scope,
        "session_index_removed": session_index_removed,
        "state_deleted": state_deleted,
        "logs_removed": logs_removed,
        "paginated_history_cleanup": paginated_history_cleanup,
        "rollout_files_removed": rollout_cleanup["removed"],
        "shell_snapshots_removed": snapshot_cleanup["removed"],
        "generated_artifacts_removed": generated_cleanup["removed"],
        "desktop_catalog_cleanup": desktop_catalog_cleanup,
        "auxiliary_thread_cleanup": auxiliary_thread_cleanup,
        "global_state_cleanup": global_state_cleanup,
        "historical_cleanup": historical_cleanup_result,
        "component_results": component_results,
        "mutation_started": mutation_started,
        "mutation_components": mutation_components,
        "deleted": deleted,
        "retained": retained,
        "safety_warnings": all_warnings,
        "approved_execution_snapshot": execution_snapshot,
        "effective_target_ids": target_ids,
    }
    if not scan_historical:
        result["historical_residuals"] = skipped_historical_scan(
            "Historical scan was disabled by --no-historical-scan."
        )
    elif apply_historical_residuals:
        raw_historical_verification = historical_cleanup_result.get("verification")
        historical_verification: dict[str, Any] = (
            raw_historical_verification
            if isinstance(raw_historical_verification, dict)
            else {}
        )
        result["historical_residuals"] = historical_verification.get(
            "historical_residuals",
            skipped_historical_scan("Historical cleanup verification was unavailable."),
        )
    else:
        result["historical_residuals"] = scan_historical_residuals(
            plan.codex_home,
            set(target_ids),
            include_logs,
        )
    approved_missing_thread_ids = (
        entry_ids(
            approved_historical_residuals.get("state_threads_missing_rollout_file", [])
        )
        if apply_missing_rollout_threads
        else set()
    )
    skipped_components = {
        component
        for component, component_result in component_results.items()
        if component_result.get("status") == "skipped_safely"
    } | {
        component
        for component, entry in effective_plan.component_plans.items()
        if entry.get("status") == "skipped"
    }
    protected_thread_ids = plan.initial_state_thread_ids - approved_missing_thread_ids
    if COMPONENT_CORE not in skipped_components:
        protected_thread_ids -= set(target_ids)
    expected_preserved = expected_preserved_artifact_contracts(
        effective_plan.codex_home,
        retained,
    )
    expected_component_contracts = [
        contract
        for contract in preserved_component_baselines
        if contract.get("component") in skipped_components
    ]
    result["verification"] = verify(
        effective_plan.codex_home,
        target_ids,
        include_logs,
        bool(plan.preflight.get("state_db_present")),
        bool(plan.preflight.get("logs_db_present")),
        protected_thread_ids,
        [info.rollout_path for info in plan.threads.values() if info.rollout_path],
        bool(plan.preflight.get("desktop_catalog_db_present")),
        effective_plan.desktop_catalog_path,
        effective_plan.global_state_files_present,
        effective_plan.auxiliary_thread_databases_present,
        skipped_components,
        expected_preserved_artifacts=expected_preserved,
        expected_preserved_contracts=expected_component_contracts,
        state_database_expected_path=effective_plan.state_database_path,
        logs_database_expected_path=effective_plan.logs_database_path,
        auxiliary_database_plans_expected=(
            auxiliary_thread_cleanup.get(
                "verification_plans",
                effective_plan.auxiliary_thread_database_plans,
            )
        ),
        paginated_history_expected_path=(
            effective_plan.paginated_history_database_path
        ),
        paginated_history_contract_expected=(
            effective_plan.paginated_history_contract
        ),
    )
    committed_catalog_revision = result["desktop_catalog_cleanup"].get(
        "catalog_revision_after"
    )
    verified_catalog_revision = result["verification"].get("desktop_catalog_revision")
    catalog_component_completed = (
        component_results.get(COMPONENT_CATALOG, {}).get("status") == "completed"
    )
    if catalog_component_completed and isinstance(committed_catalog_revision, int) and (
        not isinstance(verified_catalog_revision, int)
        or verified_catalog_revision < committed_catalog_revision
    ):
        result["verification"]["verification_ok"] = False
        errors = result["verification"].setdefault("verification_errors", [])
        errors.append(
            "Desktop catalog revision regressed after the deletion transaction."
        )
    raw_historical_verification = historical_cleanup_result.get("verification")
    historical_verification_result: dict[str, Any] = (
        raw_historical_verification
        if isinstance(raw_historical_verification, dict)
        else {}
    )
    historical_cleanup_ok = not apply_historical_residuals or bool(
        historical_verification_result.get("cleanup_ok", False)
    )
    historical_scan_ok = (
        approved_historical_snapshot_empty
        or not scan_historical
        or not plan.historical_residuals.get("scanned", False)
        or bool(result["historical_residuals"].get("scanned", False))
    )
    result["historical_scan_ok"] = historical_scan_ok
    result["approved_historical_snapshot_empty"] = (
        approved_historical_snapshot_empty
    )
    result["verification"]["historical_snapshot_ok"] = (
        historical_cleanup_ok and historical_scan_ok
    )
    verification_ok = bool(result["verification"].get("verification_ok"))
    completed_ok = verification_ok and historical_cleanup_ok and historical_scan_ok
    accounted_target_work = any(
        component_results.get(component, {}).get("status") == "completed"
        for component in approved_target_work_components
    )
    # An empty approved historical snapshot satisfies historical verification,
    # but it is not executed work. Counting it here could turn an all-target
    # prewrite skip into a false completed_with_warnings result.
    accounted_historical_work = (
        apply_historical_residuals
        and historical_snapshot_has_approved_work(approved_historical_residuals)
        and component_results.get(COMPONENT_HISTORICAL, {}).get("status")
        == "completed"
    )
    accounted_work = (
        accounted_target_work
        or accounted_historical_work
        or any(
            str(item.get("status", "")).startswith("already_absent")
            for item in retained
            if isinstance(item, dict)
        )
    )
    for finding in all_warnings:
        finding["safe_operations_remaining"] = accounted_work
    result["safety_warnings"] = all_warnings
    has_warnings = bool(all_warnings or retained or skipped_components)
    if not completed_ok:
        outcome = "partial_possible" if mutation_started else "failed"
    elif not accounted_work:
        outcome = "no_safe_work"
    elif has_warnings:
        outcome = "completed_with_warnings"
    else:
        outcome = "completed"
    result["outcome"] = outcome
    result["completed"] = completed_ok
    result["execution_ok"] = completed_ok and outcome != "failed"
    result["success"] = outcome in {"completed", "completed_with_warnings"}
    result["next_action"] = (
        "inspect_partial"
        if outcome == "partial_possible"
        else "fix_input"
        if outcome == "failed"
        else "none"
    )
    return result


def apply_plan(
    plan: Plan,
    approved_historical_residuals: dict[str, Any],
    include_logs: bool,
    scan_historical: bool,
    apply_historical_residuals: bool,
    apply_missing_rollout_threads: bool,
    approval_scope: str,
    execution_snapshot: dict[str, Any] | None = None,
    force_open: bool = False,
    mutation_observer: Any | None = None,
) -> dict[str, Any]:
    """Apply one approved plan while serializing all mutations for Codex home."""
    frozen_snapshot = execution_snapshot or approval_execution_snapshot(
        plan,
        force_open,
    )
    # Preserve the existing zero-write Desktop gate before acquiring the directory
    # lock. The locked implementation repeats this check to close the
    # owner-state race between preview and lock acquisition.
    preview_plan, _preview_warnings, _preview_retained = narrow_plan_for_execution(
        plan,
        frozen_snapshot,
        force_open,
    )
    historical_desktop_work = apply_historical_residuals and (
        historical_snapshot_has_approved_work(approved_historical_residuals)
    )
    if plan_desktop_mutation_components(preview_plan) or historical_desktop_work:
        owners, owner_issue = desktop_owner_processes(preview_plan.codex_home)
        if owner_issue:
            raise DesktopOfflineGate(owner_issue, "final_approve_and_launch")
        if owners:
            raise DesktopOfflineGate(
                "Codex Desktop still owns catalog or global UI state.",
                "quit_desktop",
            )
    lock_fd = acquire_global_mutation_lock(plan.codex_home)
    observed_mutation = False

    def observe_locked_mutation(component: str) -> None:
        nonlocal observed_mutation
        if mutation_observer is not None:
            mutation_observer(component)
        observed_mutation = True

    try:
        try:
            return _apply_plan_with_global_lock_held(
                plan,
                approved_historical_residuals,
                include_logs,
                scan_historical,
                apply_historical_residuals,
                apply_missing_rollout_threads,
                approval_scope,
                frozen_snapshot,
                force_open,
                observe_locked_mutation,
            )
        except DesktopOfflineGate:
            raise
        except PartialMutationError as exc:
            exc.apply_result = build_partial_apply_result(
                plan,
                include_logs,
                scan_historical,
                apply_missing_rollout_threads,
                approved_historical_residuals,
                mutation_started=True,
            )
            raise
        except (OSError, RuntimeError, sqlite3.Error, UnicodeError) as exc:
            inspected = build_partial_apply_result(
                plan,
                include_logs,
                scan_historical,
                apply_missing_rollout_threads,
                approved_historical_residuals,
                mutation_started=observed_mutation,
            )
            if observed_mutation:
                wrapped = PartialMutationError("apply_verification", exc)
                wrapped.apply_result = inspected
                raise wrapped from exc
            exc.apply_result = inspected
            raise
    finally:
        release_global_mutation_lock(lock_fd)


def build_partial_apply_result(
    plan: Plan,
    include_logs: bool,
    scan_historical: bool,
    apply_missing_rollout_threads: bool,
    approved_historical_residuals: dict[str, Any] | None = None,
    mutation_started: bool = False,
) -> dict[str, Any]:
    outcome = "partial_possible" if mutation_started else "failed"
    partial: dict[str, Any] = {
        "success": False,
        "execution_ok": False,
        "completed": False,
        "outcome": outcome,
        "mutation_started": mutation_started,
        "component_results": {},
        "deleted": [],
        "retained": list(plan.retained_objects),
        "safety_warnings": list(plan.safety_warnings),
        "next_action": "inspect_partial" if mutation_started else "fix_input",
    }
    approved_historical_residuals = (
        approved_historical_residuals or plan.historical_residuals
    )
    try:
        approved_missing_thread_ids = (
            entry_ids(
                approved_historical_residuals.get(
                    "state_threads_missing_rollout_file", []
                )
            )
            if apply_missing_rollout_threads
            else set()
        )
        partial["verification"] = verify(
            plan.codex_home,
            plan.target_ids,
            include_logs,
            bool(plan.preflight.get("state_db_present")),
            bool(plan.preflight.get("logs_db_present")),
            plan.initial_state_thread_ids
            - set(plan.target_ids)
            - approved_missing_thread_ids,
            [info.rollout_path for info in plan.threads.values() if info.rollout_path],
            bool(plan.preflight.get("desktop_catalog_db_present")),
            plan.desktop_catalog_path,
            plan.global_state_files_present,
            plan.auxiliary_thread_databases_present,
        )
    except (OSError, RuntimeError, sqlite3.Error, UnicodeError) as exc:
        partial["verification"] = {
            "verification_ok": False,
            "verification_errors": [f"Unable to inspect target post-state: {exc}"],
            "residual_counts": {},
            "remaining_rollout_files": [],
            "remaining_shell_snapshots": [],
            "remaining_generated_artifacts": [],
            "missing_protected_threads": [],
            "state_integrity": "unknown",
            "logs_integrity": "unknown" if include_logs else "skipped",
            "desktop_catalog_integrity": "unknown",
            "desktop_catalog_revision": None,
            "auxiliary_thread_database_checks": {},
            "auxiliary_thread_contracts": {},
            "auxiliary_thread_databases_present": [],
            "global_state_files_present": {},
            "global_state_warnings": [],
            "artifact_warnings": [],
            "expected_preserved_artifacts": {},
            "planned_deleted_remaining": {},
            "expected_preserved_present": [],
            "expected_preserved_missing": [],
            "unexpected_remaining": [],
            "unexpected_non_target_removed": [],
            "integrity_checks": {},
            "offline_verification_ok": False,
            "historical_snapshot_ok": False,
        }
    if scan_historical:
        try:
            partial["historical_residuals"] = scan_historical_residuals(
                plan.codex_home,
                set(plan.target_ids),
                include_logs,
            )
        except (OSError, RuntimeError, sqlite3.Error, UnicodeError) as exc:
            partial["historical_residuals"] = skipped_historical_scan(
                f"Unable to inspect historical post-state: {exc}"
            )
    else:
        partial["historical_residuals"] = skipped_historical_scan(
            "Historical scan was disabled by --no-historical-scan."
        )
    return partial


def path_contract(
    paths: Iterable[Path],
    approved: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(paths, key=lambda item: str(item)):
        if not path_is_present(path):
            continue
        contract = approved.get(str(path)) if approved is not None else None
        entries.append(contract if contract is not None else path_contract_entry(path))
    return entries


def approval_preflight_contract(preflight: dict[str, Any]) -> dict[str, Any]:
    """Return the safety facts that define the approved deletion scope.

    Process identities are point-in-time observations used by the apply gate,
    not objects approved for deletion.  Keeping them in the fingerprint makes
    an otherwise unchanged frozen plan expire whenever a helper process exits
    or receives a new PID.
    """

    contract = dict(preflight)
    contract.pop("desktop_owner_processes", None)
    contract.pop("desktop_owner_detection_issue", None)
    return contract


def plan_contract(plan: Plan) -> dict[str, Any]:
    return {
        "contract_version": PLAN_CONTRACT_VERSION,
        "codex_home": str(plan.codex_home),
        "state_database_path": (
            str(plan.state_database_path) if plan.state_database_path is not None else ""
        ),
        "logs_database_path": (
            str(plan.logs_database_path) if plan.logs_database_path is not None else ""
        ),
        "paginated_history_database_path": (
            str(plan.paginated_history_database_path)
            if plan.paginated_history_database_path is not None
            else ""
        ),
        "include_subagents": plan.include_subagents,
        "include_logs": plan.include_logs,
        "scan_historical": plan.scan_historical,
        "root_ids": plan.root_ids,
        "target_ids": plan.target_ids,
        "open_subagents": plan.open_subagents,
        "target_incoming_edges": plan.target_incoming_edges,
        "target_edge_rows": plan.target_edge_rows,
        "rollout_migration_skipped_rows": plan.rollout_migration_skipped_rows,
        "desktop_catalog_path": (
            str(plan.desktop_catalog_path)
            if plan.desktop_catalog_path is not None
            else ""
        ),
        "desktop_catalog_schema_signature": plan.desktop_catalog_schema_signature,
        "desktop_catalog_user_version": plan.desktop_catalog_user_version,
        "desktop_catalog_rows": plan.desktop_catalog_rows,
        "auxiliary_thread_rows": plan.auxiliary_thread_rows,
        "auxiliary_thread_contracts": plan.auxiliary_thread_contracts,
        "auxiliary_thread_database_plans": plan.auxiliary_thread_database_plans,
        "auxiliary_thread_databases_present": (plan.auxiliary_thread_databases_present),
        "paginated_history_rows": plan.paginated_history_rows,
        "paginated_history_contract": plan.paginated_history_contract,
        "paginated_history_database_plan": plan.paginated_history_database_plan,
        "global_state_files_present": plan.global_state_files_present,
        "global_state_refs": plan.global_state_refs,
        "counts": plan.counts,
        "bytes_to_remove": plan.bytes_to_remove,
        "rollout_files": path_contract(plan.rollout_files, plan.artifact_contracts),
        "shell_snapshots": path_contract(plan.shell_snapshots, plan.artifact_contracts),
        "generated_artifacts": path_contract(
            plan.generated_artifacts, plan.artifact_contracts
        ),
        "artifact_ownership_evidence": plan.artifact_ownership_evidence,
        "unsafe_paths": sorted(plan.unsafe_paths),
        "blockers": sorted(plan.blockers),
        "safety_warnings": plan.safety_warnings,
        "component_plans": plan.component_plans,
        "target_dispositions": plan.target_dispositions,
        "executable_units": plan.executable_units,
        "retained_objects": plan.retained_objects,
        "preflight": approval_preflight_contract(plan.preflight),
        "historical_residuals": plan.historical_residuals,
        "threads": {
            sid: {
                "rollout_path": info.rollout_path,
                "edge_status": info.edge_status,
                "history_mode": info.history_mode,
            }
            for sid, info in sorted(plan.threads.items())
        },
    }


def compute_plan_fingerprint(plan: Plan) -> str:
    encoded = json.dumps(
        plan_contract(plan),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def target_plan_contract(plan: Plan) -> dict[str, Any]:
    contract = plan_contract(plan)
    contract.pop("historical_residuals", None)
    counts = dict(contract.get("counts", {}))
    counts.pop("global_state_non_owning_text_mentions", None)
    contract["counts"] = counts
    preflight = dict(contract.get("preflight", {}))
    preflight.pop("desktop_owner_processes", None)
    preflight.pop("desktop_owner_detection_issue", None)
    preflight.pop("global_state_warnings", None)
    preflight.pop("initial_state_thread_count", None)
    preflight.pop("initial_state_thread_ids_sha256", None)
    contract["preflight"] = preflight
    return contract


def target_plan_fingerprint(plan: Plan) -> str:
    encoded = json.dumps(
        target_plan_contract(plan),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def plan_fingerprint(plan: Plan) -> str:
    return plan.fingerprint or compute_plan_fingerprint(plan)


def approval_scope_key(
    apply_historical_residuals: bool,
    apply_missing_rollout_threads: bool,
    force_open: bool,
) -> str:
    if apply_missing_rollout_threads:
        base = "targets_historical_and_missing_rollout_threads"
    elif apply_historical_residuals:
        base = "targets_and_historical_residuals"
    else:
        base = "targets_only"
    return f"{base}_force_open" if force_open else base


def approval_authority_fingerprint(plan: Plan) -> str:
    authority = {
        "contract_version": APPROVAL_CONTRACT_VERSION,
        "codex_home": str(plan.codex_home),
        "root_ids": plan.root_ids,
        "include_subagents": plan.include_subagents,
        "include_logs": plan.include_logs,
        "scan_historical": plan.scan_historical,
    }
    encoded = json.dumps(
        authority,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def approval_state_target_counts(
    plan: Plan,
    target_ids: set[str],
) -> dict[str, int]:
    counts = {
        "state_threads": 0,
        "state_thread_spawn_edges": 0,
        "state_thread_dynamic_tools": 0,
        "state_thread_goals": 0,
        "state_stage1_outputs": 0,
        "state_agent_job_items_assigned": 0,
    }
    if not target_ids or not component_plan_enabled(plan, COMPONENT_CORE):
        return counts
    state_path = plan.state_database_path
    if state_path is None:
        return counts
    if managed_sqlite_issues(plan.codex_home, state_path):
        return counts
    state = connect_ro(state_path)
    try:
        ids = sorted(target_ids)
        counts.update(
            {
                "state_threads": count_in_table(state, "threads", "id", ids),
                "state_thread_spawn_edges": count_edges(state, ids),
                "state_thread_dynamic_tools": count_in_table(
                    state, "thread_dynamic_tools", "thread_id", ids
                ),
                "state_thread_goals": count_in_table(
                    state, "thread_goals", "thread_id", ids
                ),
                "state_stage1_outputs": count_in_table(
                    state, "stage1_outputs", "thread_id", ids
                ),
                "state_agent_job_items_assigned": count_in_table(
                    state, "agent_job_items", "assigned_thread_id", ids
                ),
            }
        )
    finally:
        if state is not None:
            state.close()
    return counts


def approval_execution_snapshot(plan: Plan, force_open: bool) -> dict[str, Any]:
    retained_target_ids: set[str] = set()
    retained_reasons: dict[str, list[str]] = {}
    for sid, disposition in plan.target_dispositions.items():
        status = str(disposition.get("status", "eligible"))
        if status == "retained" or (
            status == "requires_force_open" and not force_open
        ):
            retained_target_ids.add(sid)
            retained_reasons[sid] = [
                str(reason) for reason in disposition.get("reasons", [])
            ]
    executable_target_ids = sorted(set(plan.target_ids) - retained_target_ids)
    executable_units: list[dict[str, Any]] = []
    retained_units = list(plan.retained_objects)
    for unit in plan.executable_units:
        component = str(unit.get("component", ""))
        sid = str(unit.get("session_id", ""))
        component_enabled = component_plan_enabled(plan, component)
        target_enabled = not sid or sid in executable_target_ids
        if component_enabled and target_enabled:
            executable_units.append(unit)
            continue
        reason = (
            "; ".join(retained_reasons.get(sid, []))
            if sid in retained_target_ids
            else "; ".join(
                str(item)
                for item in plan.component_plans.get(component, {}).get(
                    "reasons", []
                )
            )
        )
        retained_units.append(
            {
                **unit,
                "status": "retained_by_plan",
                "reason": reason or "The unit is outside the safe executable set.",
            }
        )
    executable_target_set = set(executable_target_ids)
    logs = (
        connect_ro(plan.logs_database_path)
        if plan.include_logs
        and plan.logs_database_path is not None
        and component_plan_enabled(plan, COMPONENT_LOGS)
        else None
    )
    try:
        target_log_contracts = logs_entries_for_ids(logs, executable_target_set)
    finally:
        if logs is not None:
            logs.close()
    approved_artifact_paths = {
        str(unit.get("object_id", ""))
        for unit in executable_units
        if unit.get("component")
        in {COMPONENT_ROLLOUTS, COMPONENT_SNAPSHOTS, COMPONENT_GENERATED}
    }
    target_index_contracts = (
        session_index_entries_for_ids(
            plan.codex_home / "session_index.jsonl",
            executable_target_set,
        )
        if component_plan_enabled(plan, COMPONENT_CORE)
        else []
    )
    target_counts = approval_state_target_counts(plan, executable_target_set)
    target_counts["session_index_rows"] = sum(
        int(entry.get("rows", 0)) for entry in target_index_contracts
    )
    target_counts["logs_rows"] = sum(
        int(entry.get("rows", 0)) for entry in target_log_contracts
    )
    paginated_history_contract = filtered_paginated_history_contract(
        plan.paginated_history_contract,
        executable_target_set,
    )
    paginated_rows = paginated_history_contract.get("rows", {})
    target_counts["paginated_history_projection_rows"] = len(
        paginated_rows.get("thread_history_projection_state", [])
    )
    target_counts["paginated_history_turn_rows"] = len(
        paginated_rows.get("thread_turns", [])
    )
    target_counts["paginated_history_item_rows"] = len(
        paginated_rows.get("thread_items", [])
    )
    object_contracts = {
        "state_database_path": (
            str(plan.state_database_path) if plan.state_database_path is not None else ""
        ),
        "logs_database_path": (
            str(plan.logs_database_path) if plan.logs_database_path is not None else ""
        ),
        "paginated_history_database_path": (
            str(plan.paginated_history_database_path)
            if plan.paginated_history_database_path is not None
            else ""
        ),
        "paginated_history_contract": paginated_history_contract,
        "paginated_history_database_plan": plan.paginated_history_database_plan,
        "threads": {
            sid: {
                "rollout_path": plan.threads[sid].rollout_path,
                "history_mode": plan.threads[sid].history_mode,
            }
            for sid in executable_target_ids
            if sid in plan.threads
        },
        "target_incoming_edges": {
            sid: edges
            for sid, edges in plan.target_incoming_edges.items()
            if sid in executable_target_set
        },
        "target_edge_rows": [
            edge
            for edge in plan.target_edge_rows
            if str(edge.get("parent_thread_id", "")).lower()
            in executable_target_set
            or str(edge.get("child_thread_id", "")).lower()
            in executable_target_set
        ],
        "target_counts": target_counts,
        "state_mutation_effect_assessment": plan.preflight.get(
            "state_mutation_effect_assessment", {}
        ),
        "session_index_rows": target_index_contracts,
        "log_rows": target_log_contracts,
        "artifact_contracts": {
            path: contract
            for path, contract in plan.artifact_contracts.items()
            if path in approved_artifact_paths
        },
        "desktop_catalog_rows": {
            table: [
                row
                for row in rows
                if str(row.get("thread_id", "")).lower()
                in executable_target_set
            ]
            for table, rows in plan.desktop_catalog_rows.items()
        },
        "desktop_catalog_schema_signature": plan.desktop_catalog_schema_signature,
        "desktop_catalog_user_version": plan.desktop_catalog_user_version,
        "auxiliary_thread_contracts": {
            filename: {
                **contract,
                "rows": [
                    row
                    for row in contract.get("rows", [])
                    if str(row.get("thread_id", "")).lower()
                    in executable_target_set
                ],
            }
            for filename, contract in plan.auxiliary_thread_contracts.items()
        },
        "auxiliary_thread_database_plans": plan.auxiliary_thread_database_plans,
        "auxiliary_thread_databases_present": plan.auxiliary_thread_databases_present,
        "global_state_files_present": plan.global_state_files_present,
        "global_state_refs": {
            filename: [
                contract
                for contract in contracts
                if global_state_ref_matches_targets(
                    contract,
                    executable_target_set,
                )
            ]
            for filename, contracts in plan.global_state_refs.items()
        },
        "rollout_migration_skipped_rows": [
            row
            for row in plan.rollout_migration_skipped_rows
            if str(row.get("rollout_path", ""))
            in {
                plan.threads[sid].rollout_path
                for sid in executable_target_ids
                if sid in plan.threads and plan.threads[sid].rollout_path
            }
        ],
    }
    return {
        "executable_target_ids": executable_target_ids,
        "retained_target_ids": sorted(retained_target_ids),
        "retained_target_reasons": retained_reasons,
        "component_plans": plan.component_plans,
        "executable_units": executable_units,
        "retained_units": sorted(
            retained_units,
            key=lambda item: (
                str(item.get("component", "")),
                str(item.get("session_id", "")),
                str(item.get("object_id", "")),
            ),
        ),
        "object_contracts": object_contracts,
    }


def execution_snapshot_desktop_mutation_components(
    execution_snapshot: dict[str, Any],
) -> set[str]:
    """Return approved Desktop-owned components that contain executable objects."""
    if not isinstance(execution_snapshot, dict):
        return set()
    object_contracts = execution_snapshot.get("object_contracts", {})
    component_plans = execution_snapshot.get("component_plans", {})
    if not isinstance(object_contracts, dict) or not isinstance(component_plans, dict):
        return set()

    def enabled(component: str) -> bool:
        entry = component_plans.get(component, {})
        return isinstance(entry, dict) and entry.get("status", "enabled") == "enabled"

    components: set[str] = set()
    paginated_contract = object_contracts.get("paginated_history_contract", {})
    paginated_rows = (
        paginated_contract.get("rows", {})
        if isinstance(paginated_contract, dict)
        else {}
    )
    if (
        enabled(COMPONENT_PAGINATED_HISTORY)
        and isinstance(paginated_rows, dict)
        and any(isinstance(rows, list) and rows for rows in paginated_rows.values())
    ):
        components.add(COMPONENT_PAGINATED_HISTORY)
    catalog_rows = object_contracts.get("desktop_catalog_rows", {})
    if (
        enabled(COMPONENT_CATALOG)
        and isinstance(catalog_rows, dict)
        and any(isinstance(rows, list) and rows for rows in catalog_rows.values())
    ):
        components.add(COMPONENT_CATALOG)

    auxiliary_contracts = object_contracts.get("auxiliary_thread_contracts", {})
    if (
        enabled(COMPONENT_AUXILIARY)
        and isinstance(auxiliary_contracts, dict)
        and any(
            isinstance(contract, dict)
            and isinstance(contract.get("rows"), list)
            and contract.get("rows")
            for contract in auxiliary_contracts.values()
        )
    ):
        components.add(COMPONENT_AUXILIARY)

    global_refs = object_contracts.get("global_state_refs", {})
    if (
        enabled(COMPONENT_GLOBAL_STATE)
        and isinstance(global_refs, dict)
        and any(isinstance(refs, list) and refs for refs in global_refs.values())
    ):
        components.add(COMPONENT_GLOBAL_STATE)
    return components


def execution_snapshot_target_work_components(
    execution_snapshot: dict[str, Any],
) -> set[str]:
    """Return components with at least one frozen executable target object."""

    if not isinstance(execution_snapshot, dict):
        return set()
    object_contracts = execution_snapshot.get("object_contracts", {})
    component_plans = execution_snapshot.get("component_plans", {})
    if not isinstance(object_contracts, dict) or not isinstance(component_plans, dict):
        return set()

    def enabled(component: str) -> bool:
        entry = component_plans.get(component, {})
        return isinstance(entry, dict) and entry.get("status", "enabled") == "enabled"

    work = execution_snapshot_desktop_mutation_components(execution_snapshot)
    target_counts = object_contracts.get("target_counts", {})
    if isinstance(target_counts, dict):
        if enabled(COMPONENT_CORE) and any(
            int(target_counts.get(key, 0) or 0) > 0
            for key in {
                "state_threads",
                "state_thread_spawn_edges",
                "state_thread_dynamic_tools",
                "state_thread_goals",
                "state_stage1_outputs",
                "state_agent_job_items_assigned",
                "session_index_rows",
            }
        ):
            work.add(COMPONENT_CORE)
        if enabled(COMPONENT_LOGS) and int(target_counts.get("logs_rows", 0) or 0) > 0:
            work.add(COMPONENT_LOGS)
        if enabled(COMPONENT_PAGINATED_HISTORY) and any(
            int(target_counts.get(key, 0) or 0) > 0
            for key in {
                "paginated_history_projection_rows",
                "paginated_history_turn_rows",
                "paginated_history_item_rows",
            }
        ):
            work.add(COMPONENT_PAGINATED_HISTORY)

    artifact_contracts = object_contracts.get("artifact_contracts", {})
    if isinstance(artifact_contracts, dict) and artifact_contracts:
        approved_paths = set(artifact_contracts)
        for unit in execution_snapshot.get("executable_units", []):
            if not isinstance(unit, dict):
                continue
            component = str(unit.get("component", ""))
            if (
                component
                in {COMPONENT_ROLLOUTS, COMPONENT_SNAPSHOTS, COMPONENT_GENERATED}
                and enabled(component)
                and str(unit.get("object_id", "")) in approved_paths
            ):
                work.add(component)

    if (
        enabled(COMPONENT_CORE)
        and isinstance(object_contracts.get("rollout_migration_skipped_rows"), list)
        and object_contracts.get("rollout_migration_skipped_rows")
    ):
        work.add(COMPONENT_CORE)
    return work


def execution_snapshot_has_approved_target_work(
    execution_snapshot: dict[str, Any],
) -> bool:
    """Whether the frozen executable scope contains at least one target object."""

    return bool(execution_snapshot_target_work_components(execution_snapshot))


def historical_snapshot_has_approved_work(snapshot: dict[str, Any]) -> bool:
    if not isinstance(snapshot, dict) or snapshot.get("scanned") is not True:
        return False
    for key in HISTORICAL_SCOPE_KEYS:
        value = snapshot.get(key)
        if isinstance(value, list) and value:
            return True
        if isinstance(value, dict) and any(
            isinstance(entries, list) and entries for entries in value.values()
        ):
            return True
    return False


def compute_approval_token(
    plan: Plan,
    apply_historical_residuals: bool,
    apply_missing_rollout_threads: bool,
    force_open: bool,
) -> str:
    scope = approval_scope_key(
        apply_historical_residuals,
        apply_missing_rollout_threads,
        force_open,
    )
    payload: dict[str, Any] = {
        "approval_contract_version": APPROVAL_CONTRACT_VERSION,
        "scope": scope,
        "target_plan_fingerprint": target_plan_fingerprint(plan),
        "authority_fingerprint": approval_authority_fingerprint(plan),
        "apply_historical_residuals": apply_historical_residuals,
        "apply_missing_rollout_threads": apply_missing_rollout_threads,
        "force_open": force_open,
        "execution_snapshot": approval_execution_snapshot(plan, force_open),
    }
    if apply_historical_residuals:
        historical_snapshot = approved_historical_snapshot(
            plan,
            apply_missing_rollout_threads,
            force_open,
        )
        payload["historical_snapshot"] = historical_snapshot
    raw_payload = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    compressed = zlib.compress(raw_payload, level=9)
    encoded_payload = base64.urlsafe_b64encode(compressed).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(raw_payload).hexdigest()
    return f"v{APPROVAL_CONTRACT_VERSION}.{encoded_payload}.{digest}"


def decode_approval_token(token: str) -> dict[str, Any] | None:
    if not token or len(token) > MAX_APPROVAL_TOKEN_CHARS:
        return None
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != f"v{APPROVAL_CONTRACT_VERSION}":
        return None
    encoded_payload, expected_digest = parts[1], parts[2]
    try:
        padding = "=" * (-len(encoded_payload) % 4)
        compressed = base64.b64decode(
            encoded_payload + padding,
            altchars=b"-_",
            validate=True,
        )
        decompressor = zlib.decompressobj()
        raw_payload = decompressor.decompress(
            compressed,
            MAX_APPROVAL_PAYLOAD_BYTES + 1,
        )
        if (
            len(raw_payload) > MAX_APPROVAL_PAYLOAD_BYTES
            or decompressor.unconsumed_tail
            or decompressor.unused_data
            or not decompressor.eof
        ):
            return None
        if not hmac.compare_digest(
            hashlib.sha256(raw_payload).hexdigest(), expected_digest
        ):
            return None
        payload = json.loads(raw_payload)
    except (UnicodeError, ValueError, json.JSONDecodeError, zlib.error):
        return None
    return payload if isinstance(payload, dict) else None


def validated_approval_payload(
    plan: Plan,
    token: str,
    apply_historical_residuals: bool,
    apply_missing_rollout_threads: bool,
    force_open: bool,
) -> dict[str, Any] | None:
    payload = decode_approval_token(token)
    if payload is None:
        return None
    expected_scope = approval_scope_key(
        apply_historical_residuals,
        apply_missing_rollout_threads,
        force_open,
    )
    expected_keys = {
        "approval_contract_version",
        "scope",
        "target_plan_fingerprint",
        "authority_fingerprint",
        "apply_historical_residuals",
        "apply_missing_rollout_threads",
        "force_open",
        "execution_snapshot",
    }
    if apply_historical_residuals:
        expected_keys.add("historical_snapshot")
    if set(payload) != expected_keys:
        return None
    if (
        payload.get("approval_contract_version") != APPROVAL_CONTRACT_VERSION
        or payload.get("scope") != expected_scope
        or payload.get("authority_fingerprint")
        != approval_authority_fingerprint(plan)
        or payload.get("apply_historical_residuals") is not apply_historical_residuals
        or payload.get("apply_missing_rollout_threads")
        is not apply_missing_rollout_threads
        or payload.get("force_open") is not force_open
    ):
        return None
    execution_snapshot = payload.get("execution_snapshot")
    if not isinstance(execution_snapshot, dict):
        return None
    expected_execution_keys = {
        "executable_target_ids",
        "retained_target_ids",
        "retained_target_reasons",
        "component_plans",
        "executable_units",
        "retained_units",
        "object_contracts",
    }
    if set(execution_snapshot) != expected_execution_keys:
        return None
    if any(
        not isinstance(execution_snapshot.get(key), list)
        for key in [
            "executable_target_ids",
            "retained_target_ids",
            "executable_units",
            "retained_units",
        ]
    ):
        return None
    if not isinstance(execution_snapshot.get("retained_target_reasons"), dict):
        return None
    if not isinstance(execution_snapshot.get("component_plans"), dict):
        return None
    if not isinstance(execution_snapshot.get("object_contracts"), dict):
        return None
    executable_target_ids = execution_snapshot.get("executable_target_ids", [])
    retained_target_ids = execution_snapshot.get("retained_target_ids", [])
    if any(
        not isinstance(sid, str) or not CANONICAL_UUID_RE.fullmatch(sid)
        for sid in [*executable_target_ids, *retained_target_ids]
    ):
        return None
    executable_target_set = set(executable_target_ids)
    retained_target_set = set(retained_target_ids)
    if (
        len(executable_target_set) != len(executable_target_ids)
        or len(retained_target_set) != len(retained_target_ids)
        or executable_target_set & retained_target_set
        or set(plan.target_ids) != executable_target_set | retained_target_set
    ):
        # Recursive target membership is an authorization boundary. Object identity
        # drift is handled locally during apply, but a new or missing target requires
        # a newly reviewed approval capsule before any mutation.
        return None
    if apply_historical_residuals:
        snapshot = payload.get("historical_snapshot")
        if not isinstance(snapshot, dict) or snapshot.get("scanned") is not True:
            return None
        expected_snapshot_keys = {"scanned", *HISTORICAL_SCOPE_KEYS}
        if set(snapshot) != expected_snapshot_keys:
            return None
        if not isinstance(snapshot.get("state_orphan_references"), dict):
            return None
        if any(
            not isinstance(snapshot.get(key), list)
            for key in HISTORICAL_SCOPE_KEYS
            if key != "state_orphan_references"
        ):
            return None
    return payload


def missing_rollout_scope_entries(plan: Plan) -> list[dict[str, Any]]:
    entries = plan.historical_residuals.get("state_threads_missing_rollout_file", [])
    return entries if isinstance(entries, list) else []


def scope_open_threads(plan: Plan, apply_missing_rollout_threads: bool) -> list[str]:
    ids = set(plan.open_subagents)
    if apply_missing_rollout_threads:
        ids.update(missing_rollout_open_threads(plan))
    return sorted(ids)


def missing_rollout_open_threads(plan: Plan) -> list[str]:
    return sorted(
        str(entry["id"]).lower()
        for entry in missing_rollout_scope_entries(plan)
        if entry.get("id") and entry.get("open_or_unknown")
    )


def missing_rollout_current_sessions(plan: Plan) -> list[str]:
    return sorted(
        str(entry["id"]).lower()
        for entry in missing_rollout_scope_entries(plan)
        if entry.get("id") and entry.get("current_session")
    )


def snapshot_missing_rollout_entries(
    snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    entries = snapshot.get("state_threads_missing_rollout_file", [])
    return entries if isinstance(entries, list) else []


def snapshot_open_threads(
    plan: Plan,
    snapshot: dict[str, Any],
    apply_missing_rollout_threads: bool,
) -> list[str]:
    ids = set(plan.open_subagents)
    if apply_missing_rollout_threads:
        ids.update(
            str(entry["id"]).lower()
            for entry in snapshot_missing_rollout_entries(snapshot)
            if entry.get("id") and entry.get("open_or_unknown")
        )
    return sorted(ids)


def approval_component_status_contract(
    component_plans: dict[str, Any],
) -> dict[str, dict[str, str]]:
    """Keep mutation dispositions while excluding diagnostic prose."""

    return {
        component: {
            "status": str(
                entry.get("status", "enabled")
                if isinstance(entry, dict)
                else "enabled"
            )
        }
        for component, entry in sorted(component_plans.items())
    }


def approval_executable_unit_contract(
    units: Iterable[dict[str, Any]],
) -> list[dict[str, str]]:
    """Return the exact executable object locators without runtime messages."""

    normalized = [
        {
            "component": str(unit.get("component", "")),
            "session_id": str(unit.get("session_id", "")),
            "object_id": str(unit.get("object_id", "")),
        }
        for unit in units
        if isinstance(unit, dict)
    ]
    return sorted(
        normalized,
        key=lambda item: (
            item["component"],
            item["session_id"],
            item["object_id"],
        ),
    )


def approval_auxiliary_row_contracts(
    contracts: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Bind only target-bearing auxiliary rows and their stable locators."""

    selected: dict[str, dict[str, Any]] = {}
    for filename, raw_contract in sorted(contracts.items()):
        if not isinstance(raw_contract, dict):
            continue
        rows = raw_contract.get("rows", [])
        if not isinstance(rows, list) or not rows:
            continue
        selected[filename] = {
            "table": str(raw_contract.get("table", "")),
            "thread_column": str(raw_contract.get("thread_column", "")),
            "primary_key": list(raw_contract.get("primary_key", [])),
            "rows": rows,
        }
    return selected


def approval_paginated_row_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """Remove runtime schema and sidecar evidence from paginated authority."""

    if not isinstance(contract, dict) or not contract:
        return {}
    return {
        "path": str(contract.get("path", "")),
        "primary_keys": contract.get("primary_keys", {}),
        "rows": contract.get("rows", {}),
    }


def approval_scope_contract(
    plan: Plan,
    apply_historical_residuals: bool,
    apply_missing_rollout_threads: bool,
    force_open: bool,
) -> dict[str, Any]:
    """Return the explicit semantic authority selected by the user.

    This contract intentionally excludes process observations, SQLite sidecar
    metadata, database mtimes/sizes, schema-version diagnostics, unrelated
    extension inventories, non-owning text mentions, and other runtime evidence.
    Those facts are freshly assessed during staging and apply.  Target membership,
    exact deletable rows/files, structured target references, historical identities,
    and inclusion options remain approval boundaries.
    """

    scope = approval_scope_key(
        apply_historical_residuals,
        apply_missing_rollout_threads,
        force_open,
    )
    execution_snapshot = approval_execution_snapshot(plan, force_open)
    object_contracts = execution_snapshot.get("object_contracts", {})
    if not isinstance(object_contracts, dict):
        object_contracts = {}
    desktop_catalog_rows = object_contracts.get("desktop_catalog_rows", {})
    auxiliary_contracts = object_contracts.get(
        "auxiliary_thread_contracts", {}
    )
    semantic_objects = {
        "state_database_path": str(
            object_contracts.get("state_database_path", "")
        ),
        "logs_database_path": str(
            object_contracts.get("logs_database_path", "")
        ),
        "paginated_history_database_path": str(
            object_contracts.get("paginated_history_database_path", "")
        ),
        "desktop_catalog_path": (
            str(plan.desktop_catalog_path)
            if plan.desktop_catalog_path is not None
            else ""
        ),
        "threads": object_contracts.get("threads", {}),
        "target_incoming_edges": object_contracts.get(
            "target_incoming_edges", {}
        ),
        "target_edge_rows": object_contracts.get("target_edge_rows", []),
        "target_counts": object_contracts.get("target_counts", {}),
        "session_index_rows": object_contracts.get("session_index_rows", []),
        "log_rows": object_contracts.get("log_rows", []),
        "artifact_contracts": object_contracts.get("artifact_contracts", {}),
        "paginated_history": approval_paginated_row_contract(
            object_contracts.get("paginated_history_contract", {})
        ),
        "desktop_catalog_rows": (
            desktop_catalog_rows
            if isinstance(desktop_catalog_rows, dict)
            else {}
        ),
        "auxiliary_thread_rows": approval_auxiliary_row_contracts(
            auxiliary_contracts if isinstance(auxiliary_contracts, dict) else {}
        ),
        "global_state_refs": object_contracts.get("global_state_refs", {}),
        "rollout_migration_skipped_rows": object_contracts.get(
            "rollout_migration_skipped_rows", []
        ),
    }
    contract: dict[str, Any] = {
        "scope_contract_version": APPROVAL_SCOPE_CONTRACT_VERSION,
        "approval_contract_version": APPROVAL_CONTRACT_VERSION,
        "scope": scope,
        "codex_home": str(plan.codex_home),
        "root_ids": plan.root_ids,
        "include_subagents": plan.include_subagents,
        "include_logs": plan.include_logs,
        "scan_historical": plan.scan_historical,
        "executable_target_ids": execution_snapshot.get(
            "executable_target_ids", []
        ),
        "retained_target_ids": execution_snapshot.get("retained_target_ids", []),
        "component_statuses": approval_component_status_contract(
            execution_snapshot.get("component_plans", {})
            if isinstance(execution_snapshot.get("component_plans", {}), dict)
            else {}
        ),
        "executable_units": approval_executable_unit_contract(
            execution_snapshot.get("executable_units", [])
        ),
        "objects": semantic_objects,
    }
    if apply_historical_residuals:
        contract["historical_snapshot"] = approved_historical_snapshot(
            plan,
            apply_missing_rollout_threads,
            force_open,
        )
    return contract


def compute_approval_scope_fingerprint(
    plan: Plan,
    apply_historical_residuals: bool,
    apply_missing_rollout_threads: bool,
    force_open: bool,
) -> str:
    encoded = json.dumps(
        approval_scope_contract(
            plan,
            apply_historical_residuals,
            apply_missing_rollout_threads,
            force_open,
        ),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def approval_tokens(plan: Plan) -> dict[str, str]:
    scopes: list[tuple[bool, bool]] = [(False, False)]
    if plan.historical_residuals.get("scanned", False):
        scopes.append((True, False))
        if plan.historical_residuals.get("state_threads_missing_rollout_file"):
            scopes.append((True, True))
    tokens: dict[str, str] = {}
    for apply_historical, apply_missing in scopes:
        scope_force_options = (
            [False, True] if scope_open_threads(plan, apply_missing) else [False]
        )
        for force_open in scope_force_options:
            key = approval_scope_key(apply_historical, apply_missing, force_open)
            tokens[key] = compute_approval_token(
                plan,
                apply_historical,
                apply_missing,
                force_open,
            )
    return tokens


def approval_scope_fingerprints(plan: Plan) -> dict[str, str]:
    """Return stable fingerprints of explicit semantic approval contracts."""

    scopes: list[tuple[bool, bool]] = [(False, False)]
    if plan.historical_residuals.get("scanned", False):
        scopes.append((True, False))
        if plan.historical_residuals.get("state_threads_missing_rollout_file"):
            scopes.append((True, True))
    fingerprints: dict[str, str] = {}
    for apply_historical, apply_missing in scopes:
        scope_force_options = (
            [False, True] if scope_open_threads(plan, apply_missing) else [False]
        )
        for force_open in scope_force_options:
            key = approval_scope_key(apply_historical, apply_missing, force_open)
            fingerprints[key] = compute_approval_scope_fingerprint(
                plan,
                apply_historical,
                apply_missing,
                force_open,
            )
    return fingerprints


def approval_scope_fingerprint(
    plan: Plan,
    apply_historical_residuals: bool,
    apply_missing_rollout_threads: bool,
    force_open: bool,
) -> str:
    return compute_approval_scope_fingerprint(
        plan,
        apply_historical_residuals,
        apply_missing_rollout_threads,
        force_open,
    )


def plan_to_dict(plan: Plan) -> dict[str, Any]:
    return {
        "outcome": "report_ready",
        "success": False,
        "next_action": "choose_scope",
        "script_version": SCRIPT_VERSION,
        "plan_contract_version": PLAN_CONTRACT_VERSION,
        "approval_contract_version": APPROVAL_CONTRACT_VERSION,
        "plan_fingerprint": plan_fingerprint(plan),
        "approval_scope_fingerprints": approval_scope_fingerprints(plan),
        "approval_tokens": approval_tokens(plan),
        "scope_safety": {
            "target_open_or_unknown_sessions": plan.open_subagents,
            "missing_rollout_open_threads": missing_rollout_open_threads(plan),
            "missing_rollout_current_sessions": missing_rollout_current_sessions(plan),
        },
        "codex_home": str(plan.codex_home),
        "state_database": {
            "path": (
                str(plan.state_database_path)
                if plan.state_database_path is not None
                else ""
            ),
            "discovery": plan.preflight.get("state_database_discovery", {}),
        },
        "logs_database": {
            "path": (
                str(plan.logs_database_path)
                if plan.logs_database_path is not None
                else ""
            ),
            "discovery": plan.preflight.get("logs_database_discovery", {}),
        },
        "paginated_history_database": {
            "path": (
                str(plan.paginated_history_database_path)
                if plan.paginated_history_database_path is not None
                else ""
            ),
            "discovery": plan.preflight.get(
                "paginated_history_database_discovery", {}
            ),
            "rows": plan.paginated_history_rows,
            "contract": plan.paginated_history_contract,
            "database_plan": plan.paginated_history_database_plan,
        },
        "root_ids": plan.root_ids,
        "target_ids": plan.target_ids,
        "target_count": len(plan.target_ids),
        "open_subagents": plan.open_subagents,
        "target_incoming_edges": plan.target_incoming_edges,
        "target_edge_rows": plan.target_edge_rows,
        "rollout_migration_state_rows": plan.rollout_migration_state_rows,
        "rollout_migration_skipped_rows": plan.rollout_migration_skipped_rows,
        "desktop_catalog": {
            "path": (
                str(plan.desktop_catalog_path)
                if plan.desktop_catalog_path is not None
                else ""
            ),
            "schema_signature": plan.desktop_catalog_schema_signature,
            "user_version": plan.desktop_catalog_user_version,
            "catalog_revision": plan.desktop_catalog_revision,
            "rows": plan.desktop_catalog_rows,
        },
        "auxiliary_thread_rows": plan.auxiliary_thread_rows,
        "auxiliary_thread_contracts": plan.auxiliary_thread_contracts,
        "auxiliary_thread_database_plans": plan.auxiliary_thread_database_plans,
        "auxiliary_thread_databases_present": (plan.auxiliary_thread_databases_present),
        "global_state": {
            "files_present": plan.global_state_files_present,
            "structural_refs": plan.global_state_refs,
            "non_owning_text_mentions": plan.global_state_non_owning_mentions,
        },
        "counts": plan.counts,
        "preflight": plan.preflight,
        "blockers": plan.blockers,
        "safety_warnings": plan.safety_warnings,
        "component_plans": plan.component_plans,
        "target_dispositions": plan.target_dispositions,
        "executable_units": plan.executable_units,
        "retained_objects": plan.retained_objects,
        "bytes_to_remove": plan.bytes_to_remove,
        "mib_to_remove": round(plan.bytes_to_remove / 1024 / 1024, 1),
        "rollout_files": [str(path) for path in plan.rollout_files],
        "shell_snapshots": [str(path) for path in plan.shell_snapshots],
        "generated_artifacts": [str(path) for path in plan.generated_artifacts],
        "artifact_ownership_evidence": plan.artifact_ownership_evidence,
        "unsafe_paths": plan.unsafe_paths,
        "warnings": plan.warnings,
        "historical_residuals": plan.historical_residuals,
        "threads": {
            sid: {
                "title": info.title,
                "agent_nickname": info.agent_nickname,
                "agent_role": info.agent_role,
                "edge_status": info.edge_status,
                "rollout_path": info.rollout_path,
                "history_mode": info.history_mode,
            }
            for sid, info in plan.threads.items()
        },
    }


def print_residual_entry(label: str, entries: list[dict[str, Any]]) -> None:
    if not entries:
        return
    print(f"  {label}: {len(entries)}")
    for entry in entries:
        sid = entry.get("id", "")
        rows = entry.get("rows")
        count = entry.get("count")
        rollout_path = entry.get("rollout_path")
        if rows is not None:
            print(f"    {sid}: {rows} row(s)")
        elif count is not None:
            print(f"    {sid}: {count} item(s)")
        elif rollout_path:
            print(f"    {sid}: missing {rollout_path}")
        else:
            print(f"    {sid}: {entry}")


def print_historical_residuals(residuals: dict[str, Any]) -> None:
    if not residuals.get("scanned", False):
        print(
            f"Historical residual scan: unavailable ({residuals.get('reason', 'skipped')})"
        )
        return

    summary = residuals.get("summary", {})
    if not summary.get("has_residuals", False):
        print("Historical residual scan: none")
    else:
        print(
            "Historical residual scan: "
            f"{summary.get('total_items', 0)} residual item(s) across "
            f"{summary.get('total_ids', 0)} past session id(s)"
        )
        print_residual_entry(
            "session_index rows without state",
            residuals.get("session_index_rows_without_state", []),
        )
        print_residual_entry(
            "rollout files without state",
            residuals.get("rollout_files_without_state", []),
        )
        print_residual_entry(
            "shell snapshots without state",
            residuals.get("shell_snapshots_without_state", []),
        )
        print_residual_entry(
            "generated artifacts without state",
            residuals.get("generated_artifacts_without_state", []),
        )
        print_residual_entry(
            "logs rows without state",
            residuals.get("logs_rows_without_state", []),
        )
        print_residual_entry(
            "state threads with missing rollout file",
            residuals.get("state_threads_missing_rollout_file", []),
        )
        orphan_refs = residuals.get("state_orphan_references", {})
        if orphan_refs:
            print("  state orphan references:")
            for table, entries in orphan_refs.items():
                print(f"    {table}: {len(entries)}")

    for note in residuals.get("notes", []):
        print(f"  note: {note}")


def human_safe_result_value(value: Any) -> Any:
    """Remove machine-only approvals and digests from human-readable output."""
    if isinstance(value, dict):
        return {
            key: human_safe_result_value(item)
            for key, item in value.items()
            if key != "approved_execution_snapshot"
            and not any(
                marker in key.lower()
                for marker in ("approval_token", "capsule", "fingerprint", "digest", "sha256")
            )
        }
    if isinstance(value, list):
        return [human_safe_result_value(item) for item in value]
    return value


def print_human(plan: Plan, apply_result: dict[str, Any] | None) -> None:
    mode = "apply" if apply_result is not None else "report"
    print(f"Mode: {mode}")
    print(f"Codex home: {plan.codex_home}")
    print("Applicable approval scopes:")
    for scope in approval_tokens(plan):
        print(f"  {scope}")
    print(f"Root IDs: {', '.join(plan.root_ids)}")
    print(f"Target sessions: {len(plan.target_ids)}")
    print(f"Open/unknown target sessions: {len(plan.open_subagents)}")
    if plan.open_subagents:
        for sid in plan.open_subagents:
            print(f"  open: {sid}")
    print(f"Rollout files: {len(plan.rollout_files)}")
    print(f"Shell snapshots: {len(plan.shell_snapshots)}")
    print(f"Generated artifacts: {len(plan.generated_artifacts)}")
    print(
        f"Approx bytes to remove: {plan.bytes_to_remove} ({plan.bytes_to_remove / 1024 / 1024:.1f} MiB)"
    )
    for key in sorted(plan.counts):
        print(f"{key}: {plan.counts[key]}")
    for warning in plan.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    for finding in plan.safety_warnings:
        print(
            "SAFETY WARNING: "
            f"[{finding.get('component', 'unknown')}/"
            f"{finding.get('disposition', 'warn')}] "
            f"{finding.get('reason', finding.get('message', ''))}",
            file=sys.stderr,
        )
    for blocker in plan.blockers:
        print(f"BLOCKER: {blocker}", file=sys.stderr)
    if apply_result is not None:
        print("Apply result:")
        for key, value in apply_result.items():
            if key not in {
                "verification",
                "historical_cleanup",
                "historical_residuals",
                "approved_execution_snapshot",
            }:
                print(f"{key}: {human_safe_result_value(value)}")
        print("Verification:")
        print(
            json.dumps(
                human_safe_result_value(apply_result["verification"]),
                indent=2,
                sort_keys=True,
            )
        )
        if apply_result["historical_cleanup"].get("applied", False):
            print("Historical residual cleanup:")
            for key, value in apply_result["historical_cleanup"].items():
                if key != "verification":
                    print(f"{key}: {human_safe_result_value(value)}")
        print_historical_residuals(apply_result["historical_residuals"])
    else:
        print_historical_residuals(plan.historical_residuals)


def emit_error(
    args: argparse.Namespace,
    message: str,
    exit_code: int,
    plan: Plan | None = None,
    details: dict[str, Any] | None = None,
) -> int:
    inspected_result = (
        details.get("partial_apply_result")
        if isinstance(details, dict)
        and isinstance(details.get("partial_apply_result"), dict)
        else {}
    )
    outcome = str(
        inspected_result.get(
            "outcome",
            "plan_changed" if exit_code == EXIT_PLAN_CHANGED else "failed",
        )
    )
    mutation_started = bool(inspected_result.get("mutation_started", False))
    next_action = str(
        inspected_result.get(
            "next_action",
            "approve_and_apply" if outcome == "plan_changed" else "fix_input",
        )
    )
    if args.json:
        payload: dict[str, Any] = {
            "error": message,
            "exit_code": exit_code,
            "outcome": outcome,
            "success": False,
            "mutation_started": mutation_started,
            "next_action": next_action,
        }
        if plan is not None:
            payload["plan"] = plan_to_dict(plan)
        if details is not None:
            payload["details"] = details
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        if plan is not None:
            print_human(plan, None)
        if details is not None:
            print("Post-state inspection:", file=sys.stderr)
            print(
                json.dumps(
                    human_safe_result_value(details),
                    indent=2,
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
        print(f"ERROR: {message}", file=sys.stderr)
    return exit_code


def emit_offline_wait_gate(
    args: argparse.Namespace,
    plan: Plan,
    reason: str,
    next_action: str,
) -> int:
    apply_result = {
        "outcome": "waiting_for_manual_exit",
        "success": False,
        "execution_ok": False,
        "completed": False,
        "mutation_started": False,
        "component_results": {},
        "deleted": [],
        "retained": list(plan.retained_objects),
        "safety_warnings": list(plan.safety_warnings),
        "next_action": next_action,
        "gate": {
            "code": "desktop_offline_required",
            "reason": reason,
        },
    }
    if args.json:
        print(
            json.dumps(
                {
                    "mode": "apply_gate",
                    "plan": plan_to_dict(plan),
                    "apply_result": apply_result,
                    "exit_code": EXIT_USAGE_OR_BLOCKED,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print_human(plan, None)
        print("Apply gate:")
        print("outcome: waiting_for_manual_exit")
        print(f"next_action: {next_action}")
        print(f"reason: {reason}")
    return EXIT_USAGE_OR_BLOCKED


def emit_concurrency_gate(
    args: argparse.Namespace,
    plan: Plan,
    reason: str,
) -> int:
    apply_result = {
        "outcome": "failed",
        "success": False,
        "execution_ok": False,
        "completed": False,
        "mutation_started": False,
        "component_results": {},
        "deleted": [],
        "retained": list(plan.retained_objects),
        "safety_warnings": list(plan.safety_warnings),
        "next_action": "wait_for_receipt",
        "gate": {
            "code": "mutation_lock_busy",
            "reason": reason,
        },
    }
    if args.json:
        print(
            json.dumps(
                {
                    "mode": "apply_gate",
                    "plan": plan_to_dict(plan),
                    "apply_result": apply_result,
                    "exit_code": EXIT_USAGE_OR_BLOCKED,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print_human(plan, None)
        print("Apply gate:")
        print("outcome: failed")
        print("next_action: wait_for_receipt")
        print(f"reason: {reason}")
    return EXIT_USAGE_OR_BLOCKED


def main() -> int:
    args = parse_args()
    if args.apply_historical_residuals and not args.apply:
        return emit_error(
            args,
            "--apply-historical-residuals requires --apply",
            EXIT_USAGE_OR_BLOCKED,
        )
    if args.apply_historical_residuals and args.no_historical_scan:
        return emit_error(
            args,
            "--apply-historical-residuals cannot be used with --no-historical-scan",
            EXIT_USAGE_OR_BLOCKED,
        )
    if args.apply_missing_rollout_threads and not args.apply_historical_residuals:
        return emit_error(
            args,
            "--apply-missing-rollout-threads requires --apply-historical-residuals",
            EXIT_USAGE_OR_BLOCKED,
        )
    if args.confirm_plan and not args.apply:
        return emit_error(
            args,
            "--confirm-plan is only valid with --apply",
            EXIT_USAGE_OR_BLOCKED,
        )

    try:
        root_ids = normalize_ids(args.session_ids)
    except ValueError as exc:
        return emit_error(args, str(exc), EXIT_USAGE_OR_BLOCKED)

    codex_home = Path(args.codex_home).expanduser().resolve()
    try:
        plan = make_plan(
            codex_home=codex_home,
            root_ids=root_ids,
            include_subagents=not args.no_subagents,
            include_logs=not args.no_logs,
            scan_historical=not args.no_historical_scan,
        )
    except (OSError, RuntimeError, sqlite3.Error, UnicodeError) as exc:
        return emit_error(
            args,
            f"Unable to build a safe deletion plan: {exc}",
            EXIT_USAGE_OR_BLOCKED,
        )

    if not args.apply:
        if args.json:
            print(
                json.dumps(
                    {"mode": "report", "plan": plan_to_dict(plan)},
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print_human(plan, None)
        return 0

    approval_scope = approval_scope_key(
        args.apply_historical_residuals,
        args.apply_missing_rollout_threads,
        args.force_open,
    )
    approval_payload = validated_approval_payload(
        plan,
        args.confirm_plan or "",
        args.apply_historical_residuals,
        args.apply_missing_rollout_threads,
        args.force_open,
    )
    if approval_payload is None:
        return emit_error(
            args,
            "The apply command is missing the approved scope token, requests a different safety "
            "scope, or the deletion plan changed. Review this new report and obtain approval again.",
            EXIT_PLAN_CHANGED,
            plan,
        )
    approved_historical_residuals = approval_payload.get("historical_snapshot", {})
    if not isinstance(approved_historical_residuals, dict):
        approved_historical_residuals = {}

    execution_snapshot = approval_payload.get("execution_snapshot", {})
    approved_offline_components = execution_snapshot_desktop_mutation_components(
        execution_snapshot
    )
    if args.apply_historical_residuals and historical_snapshot_has_approved_work(
        approved_historical_residuals
    ):
        approved_offline_components.add(COMPONENT_HISTORICAL)
    if approved_offline_components:
        owners, owner_issue = desktop_owner_processes(plan.codex_home)
        if owner_issue:
            return emit_offline_wait_gate(
                args,
                plan,
                owner_issue,
                "final_approve_and_launch",
            )
        if owners:
            return emit_offline_wait_gate(
                args,
                plan,
                "Codex Desktop still owns catalog or global UI state.",
                "quit_desktop",
            )

    approved_missing_ids = entry_ids(
        snapshot_missing_rollout_entries(approved_historical_residuals)
    )
    if args.apply_historical_residuals and not plan.historical_residuals.get(
        "scanned", False
    ):
        return emit_error(
            args,
            "Historical cleanup is unavailable because the authoritative state scan did not complete.",
            EXIT_USAGE_OR_BLOCKED,
            plan,
        )
    if args.apply_missing_rollout_threads:
        current_approved_missing = entries_for_ids(
            plan.historical_residuals.get("state_threads_missing_rollout_file", []),
            approved_missing_ids,
        )
        if current_approved_missing != snapshot_missing_rollout_entries(
            approved_historical_residuals
        ):
            return emit_error(
                args,
                "The approved dangling-thread scope changed. Review the new report and "
                "obtain approval again.",
                EXIT_PLAN_CHANGED,
                plan,
            )
    try:
        apply_result = apply_plan(
            plan,
            approved_historical_residuals=approved_historical_residuals,
            include_logs=not args.no_logs,
            scan_historical=not args.no_historical_scan,
            apply_historical_residuals=args.apply_historical_residuals,
            apply_missing_rollout_threads=args.apply_missing_rollout_threads,
            approval_scope=approval_scope,
            execution_snapshot=execution_snapshot,
            force_open=args.force_open,
        )
    except DesktopOfflineGate as gate:
        return emit_offline_wait_gate(
            args,
            plan,
            gate.reason,
            gate.next_action,
        )
    except MutationConcurrencyGate as gate:
        return emit_concurrency_gate(args, plan, gate.reason)
    except PartialMutationError as exc:
        partial_apply_result = getattr(exc, "apply_result", None)
        if not isinstance(partial_apply_result, dict):
            partial_apply_result = build_partial_apply_result(
                plan,
                approved_historical_residuals=approved_historical_residuals,
                include_logs=not args.no_logs,
                scan_historical=not args.no_historical_scan,
                apply_missing_rollout_threads=args.apply_missing_rollout_threads,
                mutation_started=True,
            )
        return emit_error(
            args,
            f"Apply stopped after a mutation began; inspect before retrying: {exc}",
            EXIT_VERIFICATION_FAILED,
            plan,
            {"partial_apply_result": partial_apply_result},
        )
    except (OSError, RuntimeError, sqlite3.Error, UnicodeError) as exc:
        failed_apply_result = getattr(exc, "apply_result", None)
        if not isinstance(failed_apply_result, dict):
            failed_apply_result = build_partial_apply_result(
                plan,
                approved_historical_residuals=approved_historical_residuals,
                include_logs=not args.no_logs,
                scan_historical=not args.no_historical_scan,
                apply_missing_rollout_threads=args.apply_missing_rollout_threads,
                mutation_started=False,
            )
        return emit_error(
            args,
            f"Apply failed before any managed mutation was observed: {exc}",
            EXIT_VERIFICATION_FAILED,
            plan,
            {"partial_apply_result": failed_apply_result},
        )

    if args.json:
        print(
            json.dumps(
                {
                    "mode": "apply",
                    "plan": plan_to_dict(plan),
                    "apply_result": apply_result,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print_human(plan, apply_result)
    return 0 if apply_result.get("execution_ok", False) else EXIT_VERIFICATION_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
