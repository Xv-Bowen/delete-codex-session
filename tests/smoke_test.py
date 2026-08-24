#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import fcntl
import io
import json
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType


if not __debug__:
    raise RuntimeError("smoke tests require Python assertions; do not run with -O")


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "delete_codex_session.py"
OFFLINE_HELPER = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "delete_codex_session_offline_helper.py"
)
TARGET_PARENT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
TARGET_CLOSED_CHILD = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
TARGET_OPEN_CHILD = "cccccccc-cccc-cccc-cccc-cccccccccccc"
TARGET_GRANDCHILD = "dddddddd-dddd-dddd-dddd-dddddddddddd"
KEEP_SESSION = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
OLD_FILE_ONLY = "ffffffff-ffff-ffff-ffff-ffffffffffff"
OLD_ORPHAN_REF = "77777777-7777-7777-7777-777777777777"
OLD_MISSING_ROLLOUT = "99999999-9999-9999-9999-999999999999"
NEW_CHILD = "12121212-1212-1212-1212-121212121212"
PINNED_SECTION = "13131313-1313-1313-1313-131313131313"


def temporary_directory() -> tempfile.TemporaryDirectory[str]:
    configured = os.environ.get("CODEX_SESSION_TEST_TMPDIR")
    root = (
        Path(configured).resolve()
        if configured
        else Path(tempfile.gettempdir()).resolve()
    )
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=root)


def scope_token(
    plan: dict[str, object],
    *,
    historical: bool = False,
    missing_rollout: bool = False,
    force_open: bool = False,
) -> str:
    if missing_rollout:
        key = "targets_historical_and_missing_rollout_threads"
    elif historical:
        key = "targets_and_historical_residuals"
    else:
        key = "targets_only"
    if force_open:
        key += "_force_open"
    tokens = plan["approval_tokens"]
    assert isinstance(tokens, dict)
    token = tokens[key]
    assert isinstance(token, str)
    return token


def safety_findings(plan: object) -> list[dict[str, object]]:
    if isinstance(plan, dict):
        blockers = plan.get("blockers", [])
        findings = plan.get("safety_warnings", [])
    else:
        blockers = getattr(plan, "blockers", [])
        findings = getattr(plan, "safety_warnings", [])
    assert blockers == [], f"legacy global blockers remain: {blockers!r}"
    assert isinstance(findings, list)
    assert all(isinstance(item, dict) for item in findings)
    return findings


def assert_safety_warning(plan: object, expected_text: str) -> dict[str, object]:
    expected = expected_text.lower()
    for finding in safety_findings(plan):
        message = str(finding.get("reason", finding.get("message", "")))
        if expected in message.lower():
            return finding
    raise AssertionError(
        f"missing safety warning containing {expected_text!r}: "
        f"{safety_findings(plan)!r}"
    )


def retained_session_ids(result: dict[str, object]) -> set[str]:
    retained = result.get("retained", [])
    assert isinstance(retained, list)
    return {
        str(item["session_id"])
        for item in retained
        if isinstance(item, dict) and item.get("session_id")
    }


def assert_controlled_warning_first_result(
    result: dict[str, object],
) -> None:
    outcome = result.get("outcome")
    assert outcome in {"completed_with_warnings", "no_safe_work"}
    assert result.get("success") is (outcome == "completed_with_warnings")
    if outcome == "no_safe_work":
        assert result.get("mutation_started") is False
    assert result.get("next_action")


STABLE_NEXT_ACTIONS = {
    "choose_scope",
    "approve_and_apply",
    "final_approve_and_launch",
    "launch_ghostty",
    "quit_desktop",
    "wait_for_receipt",
    "relaunch_same_job",
    "restage",
    "reopen_desktop",
    "reopen_and_verify",
    "retry_cleanup",
    "inspect_partial",
    "fix_input",
    "none",
}


STABLE_VERIFICATION_FIELDS = {
    "planned_deleted_remaining",
    "expected_preserved_present",
    "expected_preserved_missing",
    "unexpected_remaining",
    "unexpected_non_target_removed",
    "integrity_checks",
    "offline_verification_ok",
    "historical_snapshot_ok",
}


def assert_report_ready(plan: dict[str, object]) -> None:
    assert plan["outcome"] == "report_ready"
    assert plan["next_action"] in STABLE_NEXT_ACTIONS


def init_state(path: Path, codex_home: Path) -> None:
    conn = sqlite3.connect(path)
    with conn:
        conn.executescript(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                position INTEGER NOT NULL,
                created_at_ms INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL
            );
            CREATE TABLE project_roots (
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                path TEXT NOT NULL,
                PRIMARY KEY (project_id, position)
            );
            CREATE TABLE project_idempotency_keys (
                key TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL
            );
            CREATE TABLE thread_sections (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                source TEXT NOT NULL,
                model_provider TEXT NOT NULL,
                cwd TEXT NOT NULL,
                title TEXT NOT NULL,
                sandbox_policy TEXT NOT NULL,
                approval_mode TEXT NOT NULL,
                tokens_used INTEGER NOT NULL DEFAULT 0,
                has_user_event INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0,
                archived_at INTEGER,
                git_sha TEXT,
                git_branch TEXT,
                git_origin_url TEXT,
                cli_version TEXT NOT NULL DEFAULT '',
                first_user_message TEXT NOT NULL DEFAULT '',
                agent_nickname TEXT,
                agent_role TEXT,
                memory_mode TEXT NOT NULL DEFAULT 'enabled',
                model TEXT,
                reasoning_effort TEXT,
                agent_path TEXT,
                created_at_ms INTEGER,
                updated_at_ms INTEGER,
                thread_section_id TEXT
                    REFERENCES thread_sections(id) ON DELETE SET NULL,
                project_id TEXT REFERENCES projects(id) ON DELETE SET NULL
            );
            CREATE TABLE thread_spawn_edges (
                parent_thread_id TEXT NOT NULL,
                child_thread_id TEXT NOT NULL PRIMARY KEY,
                status TEXT NOT NULL
            );
            CREATE TABLE thread_dynamic_tools (
                thread_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                input_schema TEXT NOT NULL,
                defer_loading INTEGER NOT NULL DEFAULT 0,
                namespace TEXT,
                PRIMARY KEY(thread_id, position)
            );
            CREATE TABLE thread_goals (
                thread_id TEXT PRIMARY KEY NOT NULL,
                goal_id TEXT NOT NULL,
                objective TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE stage1_outputs (
                thread_id TEXT PRIMARY KEY,
                source_updated_at INTEGER NOT NULL,
                raw_memory TEXT NOT NULL,
                rollout_summary TEXT NOT NULL,
                generated_at INTEGER NOT NULL
            );
            CREATE TABLE agent_job_items (
                job_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                row_index INTEGER NOT NULL,
                row_json TEXT NOT NULL,
                status TEXT NOT NULL,
                assigned_thread_id TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY(job_id, item_id)
            );
            """
        )
        conn.execute(
            "INSERT INTO thread_sections (id, name) VALUES (?, 'Pinned')",
            (PINNED_SECTION,),
        )
        for sid, title in [
            (TARGET_PARENT, "parent"),
            (TARGET_CLOSED_CHILD, "child closed"),
            (TARGET_OPEN_CHILD, "child open"),
            (TARGET_GRANDCHILD, "grandchild"),
            (KEEP_SESSION, "keep"),
        ]:
            rollout = (
                codex_home / "sessions" / "2026" / "05" / "03" / f"rollout-{sid}.jsonl"
            )
            rollout.parent.mkdir(parents=True, exist_ok=True)
            rollout.write_text(f'{{"id":"{sid}"}}\n', encoding="utf-8")
            conn.execute(
                """
                INSERT INTO threads
                (id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
                 sandbox_policy, approval_mode, created_at_ms, updated_at_ms, thread_section_id)
                VALUES (?, ?, 1, 1, 'test', 'openai', '/tmp', ?, '{}', 'never', 1000, 1000, ?)
                """,
                (sid, str(rollout), title, PINNED_SECTION),
            )
        conn.executemany(
            "INSERT INTO thread_spawn_edges VALUES (?, ?, ?)",
            [
                (TARGET_PARENT, TARGET_CLOSED_CHILD, "closed"),
                (TARGET_PARENT, TARGET_OPEN_CHILD, "open"),
                (TARGET_CLOSED_CHILD, TARGET_GRANDCHILD, "closed"),
                (KEEP_SESSION, OLD_ORPHAN_REF, "closed"),
            ],
        )
        conn.execute(
            "INSERT INTO thread_dynamic_tools VALUES (?, 0, 'tool', 'desc', '{}', 0, 'ns')",
            (TARGET_PARENT,),
        )
        conn.execute(
            "INSERT INTO thread_goals VALUES (?, 'goal', 'objective', 'active')",
            (TARGET_CLOSED_CHILD,),
        )
        conn.execute(
            "INSERT INTO stage1_outputs VALUES (?, 1, 'raw', 'summary', 1)",
            (TARGET_GRANDCHILD,),
        )
        conn.execute(
            """
            INSERT INTO agent_job_items
            (job_id, item_id, row_index, row_json, status, assigned_thread_id, created_at, updated_at)
            VALUES ('job', 'item', 0, '{}', 'done', ?, 1, 1)
            """,
            (TARGET_OPEN_CHILD,),
        )
        missing_rollout = (
            codex_home
            / "sessions"
            / "2026"
            / "05"
            / "01"
            / f"rollout-{OLD_MISSING_ROLLOUT}.jsonl"
        )
        conn.execute(
            """
            INSERT INTO threads
            (id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
             sandbox_policy, approval_mode, created_at_ms, updated_at_ms, thread_section_id)
            VALUES (?, ?, 1, 1, 'test', 'openai', '/tmp', 'old missing rollout', '{}', 'never', 1000, 1000, ?)
            """,
            (OLD_MISSING_ROLLOUT, str(missing_rollout), PINNED_SECTION),
        )
        conn.execute(
            "INSERT INTO thread_goals VALUES (?, 'old-goal', 'old objective', 'active')",
            (OLD_ORPHAN_REF,),
        )
    conn.close()


def init_rollout_migration_tables(path: Path) -> None:
    conn = sqlite3.connect(path)
    with conn:
        conn.executescript(
            """
            CREATE TABLE rollout_migration_state (
                migration_id TEXT PRIMARY KEY,
                last_checked_thread_created_at INTEGER,
                last_checked_thread_id TEXT,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE rollout_migration_skipped_rollouts (
                migration_id TEXT NOT NULL,
                rollout_path TEXT NOT NULL,
                rollout_size_bytes INTEGER NOT NULL,
                rollout_modified_at_ns INTEGER NOT NULL,
                skip_reason TEXT NOT NULL,
                skipped_at INTEGER NOT NULL,
                PRIMARY KEY (migration_id, rollout_path)
            );
            """
        )
    conn.close()


def init_logs(path: Path) -> None:
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(
            """
            CREATE TABLE logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                ts_nanos INTEGER NOT NULL,
                level TEXT NOT NULL,
                target TEXT NOT NULL,
                feedback_log_body TEXT,
                module_path TEXT,
                file TEXT,
                line INTEGER,
                thread_id TEXT,
                process_uuid TEXT,
                estimated_bytes INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        for sid in [
            TARGET_PARENT,
            TARGET_CLOSED_CHILD,
            TARGET_OPEN_CHILD,
            TARGET_GRANDCHILD,
            KEEP_SESSION,
            OLD_FILE_ONLY,
            OLD_MISSING_ROLLOUT,
        ]:
            conn.execute(
                "INSERT INTO logs (ts, ts_nanos, level, target, thread_id) VALUES (1, 0, 'INFO', 'test', ?)",
                (sid,),
            )
    conn.close()


def init_paginated_history(
    path: Path,
    thread_ids: list[str],
) -> None:
    conn = sqlite3.connect(path)
    with conn:
        conn.executescript(
            """
            CREATE TABLE _sqlx_migrations (
                version BIGINT PRIMARY KEY,
                description TEXT NOT NULL,
                installed_on TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                success BOOLEAN NOT NULL,
                checksum BLOB NOT NULL,
                execution_time BIGINT NOT NULL
            );
            CREATE TABLE thread_history_projection_state (
                thread_id TEXT PRIMARY KEY,
                next_rollout_byte_offset INTEGER NOT NULL,
                next_rollout_ordinal INTEGER NOT NULL
            );
            CREATE TABLE thread_turns (
                thread_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                rollout_ordinal INTEGER NOT NULL,
                status TEXT NOT NULL,
                error_json TEXT,
                started_at INTEGER,
                completed_at INTEGER,
                duration_ms INTEGER,
                first_user_item_id TEXT,
                final_agent_item_id TEXT,
                rollout_byte_offset INTEGER,
                rollout_end_ordinal INTEGER,
                rollout_end_byte_offset INTEGER,
                PRIMARY KEY (thread_id, turn_id)
            );
            CREATE TABLE thread_items (
                thread_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                rollout_ordinal INTEGER NOT NULL,
                created_at_ms INTEGER NOT NULL,
                item_json TEXT NOT NULL,
                item_type TEXT NOT NULL DEFAULT '',
                updated_at_ordinal INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (thread_id, turn_id, item_id)
            );
            CREATE UNIQUE INDEX idx_thread_items_page
                ON thread_items(thread_id, rollout_ordinal);
            CREATE UNIQUE INDEX idx_thread_turns_page
                ON thread_turns(thread_id, rollout_ordinal);
            """
        )
        conn.executemany(
            "INSERT INTO _sqlx_migrations "
            "(version, description, success, checksum, execution_time) "
            "VALUES (?, ?, 1, X'00', 1)",
            [(1, "thread history"), (2, "thread items item type")],
        )
        for position, sid in enumerate(thread_ids, start=1):
            turn_id = f"turn-{position}"
            conn.execute(
                "INSERT INTO thread_history_projection_state VALUES (?, ?, ?)",
                (sid, 100 + position, 10 + position),
            )
            conn.execute(
                "INSERT INTO thread_turns "
                "(thread_id, turn_id, rollout_ordinal, status) "
                "VALUES (?, ?, ?, 'completed')",
                (sid, turn_id, position),
            )
            conn.executemany(
                "INSERT INTO thread_items "
                "(thread_id, turn_id, item_id, rollout_ordinal, created_at_ms, "
                "item_json, item_type, updated_at_ordinal) "
                "VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                [
                    (
                        sid,
                        turn_id,
                        f"item-{position}-1",
                        position * 10,
                        json.dumps({"type": "userMessage", "text": sid}),
                        "userMessage",
                        position * 10,
                    ),
                    (
                        sid,
                        turn_id,
                        f"item-{position}-2",
                        position * 10 + 1,
                        json.dumps({"type": "agentMessage", "text": "ok"}),
                        "agentMessage",
                        position * 10 + 1,
                    ),
                ],
            )
    conn.close()


def init_desktop_ui_metadata(
    codex_home: Path,
    target_id: str,
) -> tuple[Path, list[str]]:
    sqlite_root = codex_home / "sqlite"
    sqlite_root.mkdir()
    catalog_path = sqlite_root / "codex-dev.db"
    conn = sqlite3.connect(catalog_path)
    with conn:
        conn.executescript(
            """
            CREATE TABLE local_thread_catalog (
                host_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                display_title TEXT NOT NULL,
                source_created_at REAL NOT NULL,
                source_updated_at REAL NOT NULL,
                cwd TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                source_detail TEXT,
                model_provider TEXT NOT NULL,
                git_branch TEXT,
                observation_sequence INTEGER NOT NULL,
                missing_candidate INTEGER NOT NULL DEFAULT 0
                    CHECK (missing_candidate IN (0, 1)),
                thread_source TEXT,
                source_recency_at REAL NOT NULL DEFAULT 0,
                pending_observed_title INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (host_id, thread_id)
            );
            CREATE TABLE local_thread_catalog_hosts (
                host_id TEXT PRIMARY KEY,
                host_kind TEXT NOT NULL
            );
            CREATE TABLE local_thread_catalog_metadata (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                catalog_revision INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE local_thread_catalog_sync_state (
                host_id TEXT PRIMARY KEY,
                watermark_updated_at REAL,
                initial_build_complete INTEGER NOT NULL DEFAULT 0,
                observation_sequence INTEGER NOT NULL DEFAULT 0,
                last_full_reconciled_at INTEGER
            );
            CREATE TABLE thread_timeline_ledger (
                host_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                record_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (host_id, thread_id, sequence),
                UNIQUE (host_id, thread_id, record_id)
            ) WITHOUT ROWID;
            CREATE TABLE automation_runs (
                thread_id TEXT PRIMARY KEY,
                automation_id TEXT NOT NULL,
                status TEXT NOT NULL,
                read_at INTEGER,
                thread_title TEXT,
                source_cwd TEXT,
                inbox_title TEXT,
                inbox_summary TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                archived_user_message TEXT,
                archived_assistant_message TEXT,
                archived_reason TEXT
            );
            """
        )
        conn.execute("PRAGMA user_version=32")
        conn.execute("INSERT INTO local_thread_catalog_hosts VALUES ('local', 'local')")
        conn.execute("INSERT INTO local_thread_catalog_metadata VALUES (1, 41)")
        conn.execute(
            "INSERT INTO local_thread_catalog_sync_state VALUES "
            "('local', 123.0, 1, 124, 122000)"
        )
        conn.executemany(
            """
            INSERT INTO local_thread_catalog
            (host_id, thread_id, display_title, source_created_at,
             source_updated_at, cwd, source_kind, source_detail,
             model_provider, git_branch, observation_sequence,
             missing_candidate, thread_source, source_recency_at,
             pending_observed_title)
            VALUES ('local', ?, ?, 1.0, 2.0, '/tmp', 'vscode', NULL,
                    'openai', NULL, ?, 0, NULL, 2.0, 1)
            """,
            [
                (target_id, "realtime voice", 49),
                (KEEP_SESSION, "keep desktop row", 50),
            ],
        )
        conn.executemany(
            "INSERT INTO thread_timeline_ledger VALUES " "('local', ?, ?, ?, ?)",
            [
                (
                    target_id,
                    1,
                    "target-session:started",
                    json.dumps({"kind": "started"}),
                ),
                (
                    target_id,
                    2,
                    "target-session:ended",
                    json.dumps({"kind": "ended"}),
                ),
                (
                    KEEP_SESSION,
                    1,
                    "keep-session:started",
                    json.dumps({"kind": "keep"}),
                ),
            ],
        )
        conn.execute(
            "INSERT INTO automation_runs "
            "(thread_id, automation_id, status, read_at, thread_title, "
            "source_cwd, inbox_title, inbox_summary, created_at, updated_at, "
            "archived_user_message, archived_assistant_message, archived_reason) "
            "VALUES ('pending:job-1', 'automation-keep', 'pending', NULL, "
            "'keep pending run', '/tmp', NULL, NULL, 100, 101, NULL, NULL, NULL)"
        )
    conn.close()

    prompt_mentions = [
        f"delete {target_id}",
        target_id,
        f"codex://threads/{target_id}",
        f"why is {target_id} still visible",
        f"verify target {target_id} again",
    ]
    global_state = {
        "electron-persisted-atom-state": {
            "heartbeat-thread-permissions-by-id": {
                target_id: {"approvalPolicy": "never"},
                KEEP_SESSION: {"approvalPolicy": "never"},
            },
            "prompt-history": {KEEP_SESSION: prompt_mentions},
            "thread-descriptions-v1": {
                target_id: {"title": "realtime voice"},
                KEEP_SESSION: {"title": "keep"},
            },
            f"thread-reference-capability:{target_id}": True,
            f"thread-reference-capability:{KEEP_SESSION}": True,
        },
        "projectless-thread-ids": [target_id, KEEP_SESSION],
        "thread-projectless-output-directories": {
            target_id: "/tmp/realtime-voice/outputs",
            KEEP_SESSION: "/tmp/keep/outputs",
        },
        "unrelated-setting": {"sentinel": "preserve-me"},
    }
    encoded = json.dumps(
        global_state,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    for filename in [
        ".codex-global-state.json",
        ".codex-global-state.json.bak",
    ]:
        (codex_home / filename).write_text(encoded, encoding="utf-8")
    return catalog_path, prompt_mentions


def init_auxiliary_thread_databases(
    codex_home: Path,
    target_ids: list[str],
) -> dict[str, Path]:
    assert len(target_ids) >= 2
    sqlite_root = codex_home / "sqlite"
    sqlite_root.mkdir(exist_ok=True)

    history_path = sqlite_root / "codex-history-snapshots-dev.db"
    conn = sqlite3.connect(history_path)
    with conn:
        conn.executescript(
            """
            CREATE TABLE app_server_history_snapshots (
                principal_key TEXT NOT NULL,
                host_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                accessed_at INTEGER NOT NULL,
                payload_bytes INTEGER NOT NULL CHECK (payload_bytes >= 0),
                payload_json TEXT NOT NULL,
                PRIMARY KEY (principal_key, host_id, thread_id)
            );
            CREATE INDEX app_server_history_snapshots_access_idx
                ON app_server_history_snapshots (accessed_at);
            PRAGMA user_version=2;
            """
        )
        history_rows = [
            ("principal-target-1", "local-a", target_ids[0], 101, {"target": 1}),
            ("principal-target-2", "local-b", target_ids[1], 102, {"target": 2}),
            ("principal-keep", "local", KEEP_SESSION, 103, {"keep": True}),
        ]
        for principal_key, host_id, thread_id, accessed_at, payload in history_rows:
            payload_json = json.dumps(payload, separators=(",", ":"))
            conn.execute(
                "INSERT INTO app_server_history_snapshots VALUES (?, ?, ?, ?, ?, ?)",
                (
                    principal_key,
                    host_id,
                    thread_id,
                    accessed_at,
                    len(payload_json.encode("utf-8")),
                    payload_json,
                ),
            )
    conn.close()

    summaries_path = sqlite_root / "codex-thread-summaries-dev.db"
    conn = sqlite3.connect(summaries_path)
    with conn:
        conn.executescript(
            """
            CREATE TABLE thread_turn_summaries (
                principal_key TEXT NOT NULL CHECK (length(principal_key) > 0),
                host_key TEXT NOT NULL CHECK (length(host_key) > 0),
                thread_id TEXT NOT NULL CHECK (length(thread_id) > 0),
                summary TEXT NOT NULL CHECK (length(summary) BETWEEN 1 AND 280),
                compact_summary TEXT CHECK (
                    compact_summary IS NULL OR length(compact_summary) BETWEEN 1 AND 60
                ),
                compact_summary_turn_key TEXT CHECK (
                    compact_summary_turn_key IS NULL OR
                    length(compact_summary_turn_key) > 0
                ),
                revision INTEGER NOT NULL CHECK (revision >= 0),
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (principal_key, host_key, thread_id)
            );
            PRAGMA user_version=2;
            """
        )
        conn.executemany(
            "INSERT INTO thread_turn_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "principal-target-1",
                    "local-a",
                    target_ids[0],
                    "target one summary",
                    "target one",
                    "turn-1",
                    1,
                    201,
                ),
                (
                    "principal-target-2",
                    "local-b",
                    target_ids[1],
                    "target two summary",
                    None,
                    None,
                    2,
                    202,
                ),
                (
                    "principal-keep",
                    "local",
                    KEEP_SESSION,
                    "keep summary",
                    "keep",
                    "keep-turn",
                    3,
                    203,
                ),
            ],
        )
    conn.close()
    return {
        history_path.name: history_path,
        summaries_path.name: summaries_path,
    }


def init_migrated_summary_database(
    codex_home: Path,
    target_ids: list[str],
) -> Path:
    assert len(target_ids) >= 2
    sqlite_root = codex_home / "sqlite"
    sqlite_root.mkdir(exist_ok=True)
    path = sqlite_root / "codex-thread-summaries-dev.db"
    conn = sqlite3.connect(path)
    with conn:
        conn.executescript(
            """
            CREATE TABLE thread_turn_summaries (
                principal_key TEXT NOT NULL CHECK (length(principal_key) > 0),
                host_key TEXT NOT NULL CHECK (length(host_key) > 0),
                thread_id TEXT NOT NULL CHECK (length(thread_id) > 0),
                summary TEXT NOT NULL CHECK (length(summary) BETWEEN 1 AND 280),
                revision INTEGER NOT NULL CHECK (revision >= 0),
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (principal_key, host_key, thread_id)
            );
            ALTER TABLE thread_turn_summaries ADD COLUMN compact_summary TEXT CHECK (
                compact_summary IS NULL OR length(compact_summary) BETWEEN 1 AND 60
            );
            ALTER TABLE thread_turn_summaries ADD COLUMN compact_summary_turn_key TEXT CHECK (
                compact_summary_turn_key IS NULL OR
                length(compact_summary_turn_key) > 0
            );
            PRAGMA user_version=2;
            """
        )
        conn.executemany(
            "INSERT INTO thread_turn_summaries "
            "(principal_key, host_key, thread_id, summary, revision, updated_at, "
            "compact_summary, compact_summary_turn_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "principal-target-1",
                    "local-a",
                    target_ids[0],
                    "migrated target one summary",
                    1,
                    301,
                    "migrated one",
                    "turn-1",
                ),
                (
                    "principal-target-2",
                    "local-b",
                    target_ids[1],
                    "migrated target two summary",
                    2,
                    302,
                    None,
                    None,
                ),
                (
                    "principal-keep",
                    "local",
                    KEEP_SESSION,
                    "migrated keep summary",
                    3,
                    303,
                    "keep",
                    "keep-turn",
                ),
            ],
        )
    conn.close()
    return path


def set_voice_selector(
    codex_home: Path,
    selector: object,
    filenames: list[str] | None = None,
) -> None:
    selected_filenames = filenames or [
        ".codex-global-state.json",
        ".codex-global-state.json.bak",
    ]
    for filename in selected_filenames:
        path = codex_home / filename
        data = json.loads(path.read_text(encoding="utf-8"))
        atom = data["electron-persisted-atom-state"]
        atom["realtime-voice-most-recent-thread"] = json.loads(json.dumps(selector))
        path.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )


def snapshot_tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def snapshot_managed_bytes(root: Path) -> dict[str, bytes]:
    snapshot = snapshot_tree_bytes(root)
    snapshot.pop(".delete-codex-session.mutation.lock", None)
    return snapshot


def run_cmd(
    *args: str,
    expect: int = 0,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    if env is not None:
        command_env.update(env)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=command_env,
    )
    if proc.returncode != expect:
        raise AssertionError(
            f"expected {expect}, got {proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    return proc


def exercise(
    apply_historical_residuals: bool,
    apply_missing_rollout_threads: bool = False,
) -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")

        index = codex_home / "session_index.jsonl"
        ids = [
            TARGET_PARENT,
            TARGET_CLOSED_CHILD,
            TARGET_OPEN_CHILD,
            TARGET_GRANDCHILD,
            KEEP_SESSION,
            OLD_FILE_ONLY,
            OLD_MISSING_ROLLOUT,
        ]
        index.write_text(
            "".join(json.dumps({"id": sid, "thread_name": sid}) + "\n" for sid in ids),
            encoding="utf-8",
        )

        shell = codex_home / "shell_snapshots"
        shell.mkdir()
        (shell / f"{TARGET_PARENT}.1.sh").write_text("pwd\n", encoding="utf-8")
        (shell / f"{OLD_FILE_ONLY}.1.sh").write_text("old\n", encoding="utf-8")
        (shell / f"{OLD_MISSING_ROLLOUT}.1.sh").write_text(
            "old missing\n", encoding="utf-8"
        )

        generated = codex_home / "generated_images" / TARGET_OPEN_CHILD
        generated.mkdir(parents=True)
        (generated / "image.png").write_bytes(b"img")
        old_generated = codex_home / "generated_images" / OLD_FILE_ONLY
        old_generated.mkdir(parents=True)
        (old_generated / "image.png").write_bytes(b"old")
        missing_generated = codex_home / "generated_images" / OLD_MISSING_ROLLOUT
        missing_generated.mkdir(parents=True)
        (missing_generated / "image.png").write_bytes(b"missing")
        old_rollout = (
            codex_home
            / "sessions"
            / "2026"
            / "05"
            / "01"
            / f"rollout-{OLD_FILE_ONLY}.jsonl"
        )
        old_rollout.parent.mkdir(parents=True, exist_ok=True)
        old_rollout.write_text(f'{{"id":"{OLD_FILE_ONLY}"}}\n', encoding="utf-8")

        report = run_cmd(
            TARGET_PARENT,
            "--codex-home",
            str(codex_home),
            "--json",
        )
        payload = json.loads(report.stdout)
        assert payload["mode"] == "report"
        plan = payload["plan"]
        fingerprint = plan["plan_fingerprint"]
        assert len(fingerprint) == 64
        assert plan["plan_contract_version"] == 13
        assert plan["approval_contract_version"] == 4
        assert len(plan["target_edge_rows"]) == 3
        assert set(plan["scope_safety"]["target_open_or_unknown_sessions"]) == {
            TARGET_PARENT,
            TARGET_OPEN_CHILD,
        }
        assert scope_token(plan, force_open=True) != fingerprint
        assert plan["blockers"] == []
        assert plan["script_version"] == "3.4"
        assert scope_token(plan).startswith("v4.")
        assert plan["preflight"]["state_quick_check"] == "ok"
        assert plan["preflight"]["state_schema_issues"] == []
        assert plan["target_count"] == 4
        assert plan["counts"]["logs_rows"] == 4
        historical = plan["historical_residuals"]
        assert historical["summary"]["has_residuals"] is True
        assert historical["session_index_rows_without_state"][0]["id"] == OLD_FILE_ONLY
        assert historical["rollout_files_without_state"][0]["id"] == OLD_FILE_ONLY
        assert historical["shell_snapshots_without_state"][0]["id"] == OLD_FILE_ONLY
        assert historical["generated_artifacts_without_state"][0]["id"] == OLD_FILE_ONLY
        assert historical["logs_rows_without_state"][0]["id"] == OLD_FILE_ONLY
        assert (
            historical["state_threads_missing_rollout_file"][0]["id"]
            == OLD_MISSING_ROLLOUT
        )
        assert OLD_ORPHAN_REF in {
            item["id"] for item in historical["state_orphan_references"]["thread_goals"]
        }
        assert (codex_home / "sessions").exists()

        run_cmd(
            TARGET_PARENT,
            "--codex-home",
            str(codex_home),
            "--apply",
            "--json",
            expect=3,
        )

        warning_first = run_cmd(
            TARGET_PARENT,
            "--codex-home",
            str(codex_home),
            "--apply",
            "--confirm-plan",
            scope_token(plan),
            "--json",
        )
        warning_first_result = json.loads(warning_first.stdout)["apply_result"]
        assert warning_first_result["outcome"] == "no_safe_work"
        assert warning_first_result["success"] is False
        assert warning_first_result["mutation_started"] is False
        assert set(plan["target_ids"]) <= retained_session_ids(warning_first_result)

        run_cmd(
            TARGET_PARENT,
            "--codex-home",
            str(codex_home),
            "--apply",
            "--confirm-plan",
            scope_token(plan),
            "--force-open",
            expect=3,
        )

        approved_token = scope_token(
            plan,
            historical=apply_historical_residuals,
            missing_rollout=apply_missing_rollout_threads,
            force_open=True,
        )
        apply_args = [
            TARGET_PARENT,
            "--codex-home",
            str(codex_home),
            "--apply",
            "--confirm-plan",
            approved_token,
            "--force-open",
            "--json",
        ]
        if apply_historical_residuals:
            apply_args.append("--apply-historical-residuals")
        if apply_missing_rollout_threads:
            apply_args.append("--apply-missing-rollout-threads")
        applied = run_cmd(*apply_args)
        applied_payload = json.loads(applied.stdout)
        assert applied_payload["apply_result"]["success"] is True
        expected_scope = (
            "targets_historical_and_missing_rollout_threads"
            if apply_missing_rollout_threads
            else (
                "targets_and_historical_residuals"
                if apply_historical_residuals
                else "targets_only"
            )
        )
        assert applied_payload["apply_result"]["approval_scope"] == (
            expected_scope + "_force_open"
        )
        verification = applied_payload["apply_result"]["verification"]
        assert verification["verification_ok"] is True
        assert set(verification["residual_counts"]) == {
            "state_threads",
            "state_thread_spawn_edges",
            "state_thread_dynamic_tools",
            "state_thread_goals",
            "state_stage1_outputs",
            "state_agent_job_items_assigned",
            "state_rollout_migration_skipped_rollouts",
            "session_index_rows",
            "logs_rows",
            "paginated_history_projection_rows",
            "paginated_history_turn_rows",
            "paginated_history_item_rows",
            "desktop_catalog_rows",
            "desktop_timeline_rows",
            "desktop_automation_run_rows",
            "desktop_inbox_rows",
            "desktop_auxiliary_thread_rows",
            "global_state_structural_refs",
        }
        assert all(value == 0 for value in verification["residual_counts"].values())
        assert verification["remaining_rollout_files"] == []
        assert verification["remaining_shell_snapshots"] == []
        assert verification["remaining_generated_artifacts"] == []
        assert verification["state_integrity"] == "ok"
        assert verification["logs_integrity"] == "ok"
        assert (
            applied_payload["apply_result"]["historical_cleanup"]["applied"]
            is apply_historical_residuals
        )
        if apply_historical_residuals:
            assert (
                applied_payload["apply_result"]["historical_cleanup"]["verification"][
                    "cleanup_ok"
                ]
                is True
            )
        assert (
            applied_payload["apply_result"]["historical_residuals"]["summary"][
                "has_residuals"
            ]
            is not apply_missing_rollout_threads
        )

        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            target_ids = [
                TARGET_PARENT,
                TARGET_CLOSED_CHILD,
                TARGET_OPEN_CHILD,
                TARGET_GRANDCHILD,
            ]
            marks = ",".join("?" for _ in target_ids)
            expected_threads = 1 if apply_missing_rollout_threads else 2
            assert (
                conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
                == expected_threads
            )
            assert conn.execute("SELECT id, name FROM thread_sections").fetchall() == [
                (PINNED_SECTION, "Pinned")
            ]
            remaining_ids = {row[0] for row in conn.execute("SELECT id FROM threads")}
            assert KEEP_SESSION in remaining_ids
            assert (
                OLD_MISSING_ROLLOUT in remaining_ids
            ) is not apply_missing_rollout_threads
            assert (
                conn.execute(
                    "SELECT assigned_thread_id FROM agent_job_items"
                ).fetchone()[0]
                is None
            )
            for table, column in [
                ("threads", "id"),
                ("thread_dynamic_tools", "thread_id"),
                ("thread_goals", "thread_id"),
                ("stage1_outputs", "thread_id"),
                ("agent_job_items", "assigned_thread_id"),
            ]:
                assert (
                    conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({marks})",
                        target_ids,
                    ).fetchone()[0]
                    == 0
                )
            assert (
                conn.execute(
                    f"SELECT COUNT(*) FROM thread_spawn_edges "
                    f"WHERE parent_thread_id IN ({marks}) OR child_thread_id IN ({marks})",
                    target_ids + target_ids,
                ).fetchone()[0]
                == 0
            )
            orphan_goal_count = conn.execute(
                "SELECT COUNT(*) FROM thread_goals WHERE thread_id=?",
                (OLD_ORPHAN_REF,),
            ).fetchone()[0]
            assert (orphan_goal_count == 0) is apply_historical_residuals
        finally:
            conn.close()
        conn = sqlite3.connect(codex_home / "logs_2.sqlite")
        try:
            marks = ",".join("?" for _ in target_ids)
            assert (
                conn.execute(
                    f"SELECT COUNT(*) FROM logs WHERE thread_id IN ({marks})",
                    target_ids,
                ).fetchone()[0]
                == 0
            )
            old_log_count = conn.execute(
                "SELECT COUNT(*) FROM logs WHERE thread_id=?",
                (OLD_FILE_ONLY,),
            ).fetchone()[0]
            missing_log_count = conn.execute(
                "SELECT COUNT(*) FROM logs WHERE thread_id=?",
                (OLD_MISSING_ROLLOUT,),
            ).fetchone()[0]
            assert (old_log_count == 0) is apply_historical_residuals
            assert (missing_log_count == 0) is apply_missing_rollout_threads
        finally:
            conn.close()
        index_text = index.read_text(encoding="utf-8")
        assert KEEP_SESSION in index_text
        assert TARGET_PARENT not in index_text
        assert not generated.exists()
        assert not any(
            sid in path.name
            for path in (codex_home / "sessions").rglob("*.jsonl")
            for sid in target_ids
        )
        assert not (shell / f"{TARGET_PARENT}.1.sh").exists()
        assert (OLD_FILE_ONLY in index_text) is not apply_historical_residuals
        assert (OLD_MISSING_ROLLOUT in index_text) is not apply_missing_rollout_threads
        assert old_rollout.exists() is not apply_historical_residuals
        assert old_generated.exists() is not apply_historical_residuals
        assert (
            shell / f"{OLD_FILE_ONLY}.1.sh"
        ).exists() is not apply_historical_residuals
        assert missing_generated.exists() is not apply_missing_rollout_threads


def exercise_batch_and_id_validation() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")

        report = run_cmd(
            TARGET_PARENT.upper(),
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        assert plan["root_ids"] == [TARGET_PARENT, KEEP_SESSION]
        assert plan["target_count"] == 6
        assert plan["script_version"] == "3.4"

        invalid = run_cmd(
            "aaaaaaaa----aaaaaaaa",
            "--codex-home",
            str(codex_home),
            "--json",
            expect=2,
        )
        assert "suspicious session" in json.loads(invalid.stdout)["error"]


def exercise_human_report_hides_approval_capsules() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")

        json_report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(json_report.stdout)["plan"]
        assert_report_ready(plan)
        assert plan["approval_tokens"]

        human_report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
        )
        rendered = human_report.stdout + "\n" + human_report.stderr
        assert "Applicable approval scopes:" in rendered
        for scope in plan["approval_tokens"]:
            assert f"  {scope}\n" in rendered
        assert "v4." not in rendered
        assert plan["plan_fingerprint"] not in rendered
        assert "plan_fingerprint" not in rendered.lower()
        assert "approval_tokens" not in rendered.lower()
        assert "digest" not in rendered.lower()


def exercise_late_target_artifact_is_retained() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        index = codex_home / "session_index.jsonl"
        index.write_text(json.dumps({"id": KEEP_SESSION}) + "\n", encoding="utf-8")

        report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        approved_token = scope_token(plan)

        shell = codex_home / "shell_snapshots"
        shell.mkdir()
        new_snapshot = shell / f"{KEEP_SESSION}.after-report.sh"
        new_snapshot.write_text("pwd\n", encoding="utf-8")

        applied = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--apply",
            "--confirm-plan",
            approved_token,
            "--json",
        )
        result = json.loads(applied.stdout)["apply_result"]
        assert result["outcome"] == "completed_with_warnings"
        assert result["success"] is True
        assert result["mutation_started"] is True
        assert any(
            item.get("object_id") == str(new_snapshot)
            for item in result["retained"]
            if isinstance(item, dict)
        )
        assert new_snapshot.exists()
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM threads WHERE id=?", (KEEP_SESSION,)
                ).fetchone()[0]
                == 0
            )
        finally:
            conn.close()


def exercise_desktop_ui_metadata_only_cleanup() -> None:
    target_id = NEW_CHILD
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        catalog_path, prompt_mentions = init_desktop_ui_metadata(
            codex_home,
            target_id,
        )
        global_state_paths = [
            codex_home / ".codex-global-state.json",
            codex_home / ".codex-global-state.json.bak",
        ]
        xattr_snapshots: dict[Path, bytes] = {}
        if sys.platform == "darwin":
            xattr_tool = Path("/usr/bin/xattr")
            assert xattr_tool.is_file()
            for path, value in zip(
                global_state_paths,
                ["main-xattr-value", "backup-xattr-value"],
                strict=True,
            ):
                subprocess.run(
                    [
                        str(xattr_tool),
                        "-w",
                        "com.openai.codex.delete-session-test",
                        value,
                        str(path),
                    ],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                xattr_snapshots[path] = subprocess.run(
                    [
                        str(xattr_tool),
                        "-p",
                        "com.openai.codex.delete-session-test",
                        str(path),
                    ],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ).stdout

        conn = sqlite3.connect(catalog_path)
        try:
            keep_catalog_before = conn.execute(
                "SELECT * FROM local_thread_catalog WHERE thread_id=?",
                (KEEP_SESSION,),
            ).fetchone()
            keep_timeline_before = conn.execute(
                "SELECT * FROM thread_timeline_ledger WHERE thread_id=?",
                (KEEP_SESSION,),
            ).fetchone()
            hosts_before = conn.execute(
                "SELECT * FROM local_thread_catalog_hosts"
            ).fetchall()
            keep_automation_before = conn.execute(
                "SELECT * FROM automation_runs WHERE thread_id='pending:job-1'"
            ).fetchone()
            assert keep_automation_before is not None
        finally:
            conn.close()

        module = load_deleter_module()
        original_desktop_owner_processes = module.desktop_owner_processes
        module.desktop_owner_processes = lambda _codex_home: ([], "")
        try:
            module_plan = module.make_plan(
                codex_home,
                [target_id],
                True,
                True,
                False,
            )
            plan = module.plan_to_dict(module_plan)
            result = module.apply_plan(
                module_plan,
                module_plan.historical_residuals,
                True,
                False,
                False,
                False,
                "targets_only",
            )
        finally:
            module.desktop_owner_processes = original_desktop_owner_processes
        assert plan["blockers"] == []
        assert plan["target_count"] == 1
        assert plan["counts"]["state_threads"] == 0
        assert plan["counts"]["session_index_rows"] == 0
        assert plan["counts"]["logs_rows"] == 0
        assert plan["counts"]["rollout_files"] == 0
        assert plan["counts"]["shell_snapshots"] == 0
        assert plan["counts"]["generated_artifacts"] == 0
        assert plan["counts"]["desktop_catalog_rows"] == 1
        assert plan["counts"]["desktop_timeline_rows"] == 2
        assert plan["counts"]["desktop_automation_run_rows"] == 0
        assert plan["counts"]["desktop_inbox_rows"] == 0
        assert plan["counts"]["global_state_structural_refs"] == 10
        assert plan["counts"]["global_state_non_owning_text_mentions"] == 10
        assert plan["preflight"]["target_evidence_items"] == 13
        assert plan["preflight"]["desktop_catalog_quick_check"] == "ok"
        assert plan["desktop_catalog"]["catalog_revision"] == 41
        assert len(plan["desktop_catalog"]["rows"]["local_thread_catalog"]) == 1
        assert len(plan["desktop_catalog"]["rows"]["thread_timeline_ledger"]) == 2
        for filename in [
            ".codex-global-state.json",
            ".codex-global-state.json.bak",
        ]:
            assert len(plan["global_state"]["structural_refs"][filename]) == 5
            assert len(plan["global_state"]["non_owning_text_mentions"][filename]) == 5

        assert result["success"] is True
        desktop_cleanup = result["desktop_catalog_cleanup"]
        assert desktop_cleanup["rows_removed"] == {
            "automation_runs": 0,
            "local_thread_catalog": 1,
            "thread_timeline_ledger": 2,
        }
        assert desktop_cleanup["catalog_revision_before"] == 41
        assert desktop_cleanup["catalog_revision_after"] == 42
        assert desktop_cleanup["catalog_revision_increment"] == 1
        assert desktop_cleanup["observation_sequence_increments"] == {"local": 1}
        global_cleanup = result["global_state_cleanup"]
        assert global_cleanup["removed"] == 10
        assert global_cleanup["removed_by_file"] == {
            ".codex-global-state.json": 5,
            ".codex-global-state.json.bak": 5,
        }
        verification = result["verification"]
        assert verification["verification_ok"] is True
        assert verification["desktop_catalog_integrity"] == "ok"
        assert verification["desktop_catalog_revision"] == 42
        assert verification["residual_counts"]["desktop_catalog_rows"] == 0
        assert verification["residual_counts"]["desktop_timeline_rows"] == 0
        assert verification["residual_counts"]["global_state_structural_refs"] == 0

        conn = sqlite3.connect(catalog_path)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM local_thread_catalog WHERE thread_id=?",
                (target_id,),
            ).fetchone() == (0,)
            assert conn.execute(
                "SELECT COUNT(*) FROM thread_timeline_ledger WHERE thread_id=?",
                (target_id,),
            ).fetchone() == (0,)
            assert (
                conn.execute(
                    "SELECT * FROM local_thread_catalog WHERE thread_id=?",
                    (KEEP_SESSION,),
                ).fetchone()
                == keep_catalog_before
            )
            assert (
                conn.execute(
                    "SELECT * FROM thread_timeline_ledger WHERE thread_id=?",
                    (KEEP_SESSION,),
                ).fetchone()
                == keep_timeline_before
            )
            assert (
                conn.execute("SELECT * FROM local_thread_catalog_hosts").fetchall()
                == hosts_before
            )
            assert (
                conn.execute(
                    "SELECT * FROM automation_runs WHERE thread_id='pending:job-1'"
                ).fetchone()
                == keep_automation_before
            )
            assert conn.execute(
                "SELECT catalog_revision FROM local_thread_catalog_metadata WHERE id=1"
            ).fetchone() == (42,)
            assert conn.execute(
                "SELECT watermark_updated_at, initial_build_complete, "
                "observation_sequence, last_full_reconciled_at "
                "FROM local_thread_catalog_sync_state WHERE host_id='local'"
            ).fetchone() == (123.0, 1, 125, 122000)
            assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)
        finally:
            conn.close()

        for filename in [
            ".codex-global-state.json",
            ".codex-global-state.json.bak",
        ]:
            data = json.loads((codex_home / filename).read_text(encoding="utf-8"))
            atom = data["electron-persisted-atom-state"]
            assert target_id not in atom["heartbeat-thread-permissions-by-id"]
            assert target_id not in atom["thread-descriptions-v1"]
            assert f"thread-reference-capability:{target_id}" not in atom
            assert target_id not in data["projectless-thread-ids"]
            assert target_id not in data["thread-projectless-output-directories"]
            assert atom["prompt-history"][KEEP_SESSION] == prompt_mentions
            assert KEEP_SESSION in atom["heartbeat-thread-permissions-by-id"]
            assert KEEP_SESSION in atom["thread-descriptions-v1"]
            assert f"thread-reference-capability:{KEEP_SESSION}" in atom
            assert KEEP_SESSION in data["projectless-thread-ids"]
            assert KEEP_SESSION in data["thread-projectless-output-directories"]
            assert data["unrelated-setting"] == {"sentinel": "preserve-me"}
        for path, before in xattr_snapshots.items():
            after = subprocess.run(
                [
                    "/usr/bin/xattr",
                    "-p",
                    "com.openai.codex.delete-session-test",
                    str(path),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout
            assert after == before
        assert not list(codex_home.glob(".*.delete-session-*"))


def exercise_desktop_catalog_stale_plan_rejection() -> None:
    target_id = NEW_CHILD
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        catalog_path, _prompt_mentions = init_desktop_ui_metadata(
            codex_home,
            target_id,
        )
        global_state_paths = [
            codex_home / ".codex-global-state.json",
            codex_home / ".codex-global-state.json.bak",
        ]
        global_state_before = {
            path.name: path.read_bytes() for path in global_state_paths
        }
        state_conn = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            state_rows_before = state_conn.execute(
                "SELECT COUNT(*) FROM threads"
            ).fetchone()
        finally:
            state_conn.close()

        module = load_deleter_module()
        original_desktop_owner_processes = module.desktop_owner_processes
        module.desktop_owner_processes = lambda _codex_home: ([], "")
        try:
            module_plan = module.make_plan(
                codex_home,
                [target_id],
                True,
                True,
                False,
            )
            plan = module.plan_to_dict(module_plan)
            assert plan["blockers"] == []

            conn = sqlite3.connect(catalog_path)
            with conn:
                conn.execute(
                    "UPDATE local_thread_catalog SET display_title=? "
                    "WHERE host_id='local' AND thread_id=?",
                    ("changed after report", target_id),
                )
            conn.close()

            result = module.apply_plan(
                module_plan,
                module_plan.historical_residuals,
                True,
                False,
                False,
                False,
                "targets_only",
            )
        finally:
            module.desktop_owner_processes = original_desktop_owner_processes
        assert result["outcome"] == "completed_with_warnings"
        assert result["success"] is True
        assert result["component_results"]["desktop_catalog"]["status"] == (
            "skipped_safely"
        )
        assert result["component_results"]["global_state"]["status"] == "completed"

        conn = sqlite3.connect(catalog_path)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM local_thread_catalog"
            ).fetchone() == (2,)
            assert conn.execute(
                "SELECT COUNT(*) FROM thread_timeline_ledger"
            ).fetchone() == (3,)
            assert conn.execute(
                "SELECT display_title FROM local_thread_catalog "
                "WHERE host_id='local' AND thread_id=?",
                (target_id,),
            ).fetchone() == ("changed after report",)
            assert conn.execute(
                "SELECT COUNT(*) FROM local_thread_catalog WHERE thread_id=?",
                (KEEP_SESSION,),
            ).fetchone() == (1,)
            assert conn.execute(
                "SELECT COUNT(*) FROM thread_timeline_ledger WHERE thread_id=?",
                (target_id,),
            ).fetchone() == (2,)
            assert conn.execute(
                "SELECT COUNT(*) FROM thread_timeline_ledger WHERE thread_id=?",
                (KEEP_SESSION,),
            ).fetchone() == (1,)
            assert conn.execute(
                "SELECT catalog_revision FROM local_thread_catalog_metadata WHERE id=1"
            ).fetchone() == (41,)
            assert conn.execute(
                "SELECT observation_sequence FROM local_thread_catalog_sync_state "
                "WHERE host_id='local'"
            ).fetchone() == (124,)
        finally:
            conn.close()
        assert any(
            path.read_bytes() != global_state_before[path.name]
            for path in global_state_paths
        )
        state_conn = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            assert (
                state_conn.execute("SELECT COUNT(*) FROM threads").fetchone()
                == state_rows_before
            )
        finally:
            state_conn.close()


def exercise_live_desktop_owner_blocks_apply() -> None:
    target_id = NEW_CHILD
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        state_path = codex_home / "state_5.sqlite"
        init_state(state_path, codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        catalog_path, _prompt_mentions = init_desktop_ui_metadata(
            codex_home,
            target_id,
        )
        global_state_paths = [
            codex_home / ".codex-global-state.json",
            codex_home / ".codex-global-state.json.bak",
        ]
        owner_env = {"CODEX_HOME": str(codex_home)}

        report = run_cmd(
            target_id,
            "--no-historical-scan",
            "--json",
            env=owner_env,
        )
        plan = json.loads(report.stdout)["plan"]
        assert plan["blockers"] == []
        assert plan["preflight"]["desktop_offline_required"] is True
        if not plan["preflight"]["desktop_owner_processes"]:
            return

        canonical_paths = [state_path, catalog_path, *global_state_paths]
        canonical_before = {path: path.read_bytes() for path in canonical_paths}

        rejected = run_cmd(
            target_id,
            "--no-historical-scan",
            "--apply",
            "--confirm-plan",
            scope_token(plan),
            "--json",
            env=owner_env,
            expect=2,
        )
        payload = json.loads(rejected.stdout)
        gate_result = payload["apply_result"]
        assert payload["exit_code"] == 2
        assert gate_result["outcome"] == "waiting_for_manual_exit"
        assert gate_result["success"] is False
        assert gate_result["mutation_started"] is False
        assert gate_result["next_action"] == "quit_desktop"
        assert gate_result["gate"]["code"] == "desktop_offline_required"
        for path, before in canonical_before.items():
            assert path.read_bytes() == before

        conn = sqlite3.connect(catalog_path)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM local_thread_catalog WHERE thread_id=?",
                (target_id,),
            ).fetchone() == (1,)
            assert conn.execute(
                "SELECT COUNT(*) FROM thread_timeline_ledger WHERE thread_id=?",
                (target_id,),
            ).fetchone() == (2,)
            assert conn.execute(
                "SELECT COUNT(*) FROM automation_runs "
                "WHERE thread_id='pending:job-1'"
            ).fetchone() == (1,)
            assert conn.execute(
                "SELECT catalog_revision FROM local_thread_catalog_metadata WHERE id=1"
            ).fetchone() == (41,)
            assert conn.execute(
                "SELECT observation_sequence FROM local_thread_catalog_sync_state "
                "WHERE host_id='local'"
            ).fetchone() == (124,)
        finally:
            conn.close()


def exercise_auxiliary_thread_database_cleanup() -> None:
    target_ids = [TARGET_CLOSED_CHILD, TARGET_GRANDCHILD]
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        auxiliary_paths = init_auxiliary_thread_databases(codex_home, target_ids)

        module = load_deleter_module()
        keep_rows_before: dict[str, tuple[object, ...]] = {}
        schema_signatures_before: dict[str, str] = {}
        for filename, path in auxiliary_paths.items():
            table = (
                "app_server_history_snapshots"
                if filename.startswith("codex-history-snapshots")
                else "thread_turn_summaries"
            )
            conn = sqlite3.connect(path)
            try:
                keep_row = conn.execute(
                    f"SELECT * FROM {table} WHERE thread_id=?",
                    (KEEP_SESSION,),
                ).fetchone()
                assert keep_row is not None
                keep_rows_before[filename] = keep_row
                schema_signatures_before[filename] = module.sqlite_schema_signature(
                    conn
                )
            finally:
                conn.close()

        original_desktop_owner_processes = module.desktop_owner_processes
        module.desktop_owner_processes = lambda _codex_home: ([], "")
        try:
            module_plan = module.make_plan(
                codex_home,
                [TARGET_CLOSED_CHILD],
                True,
                True,
                False,
            )
            plan = module.plan_to_dict(module_plan)
            result = module.apply_plan(
                module_plan,
                module_plan.historical_residuals,
                True,
                False,
                False,
                False,
                "targets_only",
            )
        finally:
            module.desktop_owner_processes = original_desktop_owner_processes

        assert plan["blockers"] == []
        assert plan["target_ids"] == target_ids
        assert plan["counts"]["desktop_auxiliary_thread_rows"] == 4
        assert plan["preflight"]["desktop_offline_required"] is True
        assert plan["auxiliary_thread_rows"] == {
            "codex-history-snapshots-dev.db": 2,
            "codex-thread-summaries-dev.db": 2,
        }
        for filename, contract in plan["auxiliary_thread_contracts"].items():
            assert contract["user_version"] == 2
            assert len(contract["schema_signature"]) == 64
            assert len(contract["rows"]) == 2
            for row in contract["rows"]:
                assert len(row["row_sha256"]) == 64
                assert row["thread_id"] in target_ids

        assert result["success"] is True
        auxiliary_cleanup = result["auxiliary_thread_cleanup"]
        assert {
            key: auxiliary_cleanup[key]
            for key in ["databases_present", "rows_removed", "integrity_checks"]
        } == {
            "databases_present": sorted(auxiliary_paths),
            "rows_removed": {
                "codex-history-snapshots-dev.db": 2,
                "codex-thread-summaries-dev.db": 2,
            },
            "integrity_checks": {
                "codex-history-snapshots-dev.db": "ok",
                "codex-thread-summaries-dev.db": "ok",
            },
        }
        assert all(
            entry["status"] == "completed"
            for entry in auxiliary_cleanup["database_results"].values()
        )
        verification = result["verification"]
        assert verification["verification_ok"] is True
        assert verification["residual_counts"]["desktop_auxiliary_thread_rows"] == 0
        assert verification["auxiliary_thread_database_checks"] == {
            "codex-history-snapshots-dev.db": "ok",
            "codex-thread-summaries-dev.db": "ok",
        }
        for contract in verification["auxiliary_thread_contracts"].values():
            assert contract["rows"] == []

        for filename, path in auxiliary_paths.items():
            table = (
                "app_server_history_snapshots"
                if filename.startswith("codex-history-snapshots")
                else "thread_turn_summaries"
            )
            conn = sqlite3.connect(path)
            try:
                marks = ",".join("?" for _ in target_ids)
                assert conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE thread_id IN ({marks})",
                    target_ids,
                ).fetchone() == (0,)
                assert (
                    conn.execute(
                        f"SELECT * FROM {table} WHERE thread_id=?",
                        (KEEP_SESSION,),
                    ).fetchone()
                    == keep_rows_before[filename]
                )
                assert conn.execute("PRAGMA user_version").fetchone() == (2,)
                assert (
                    module.sqlite_schema_signature(conn)
                    == schema_signatures_before[filename]
                )
                assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
                if table == "app_server_history_snapshots":
                    index_names = {
                        str(row[1])
                        for row in conn.execute(f"PRAGMA index_list({table})")
                    }
                    assert "app_server_history_snapshots_access_idx" in index_names
            finally:
                conn.close()


def exercise_migrated_summary_database_cleanup() -> None:
    target_ids = [TARGET_CLOSED_CHILD, TARGET_GRANDCHILD]
    expected_sql_hash = (
        "2b3807b7976cdd356c4ec88e73ab4a58217c274543d0facbc7213da932aec98b"
    )
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        summaries_path = init_migrated_summary_database(codex_home, target_ids)

        module = load_deleter_module()
        conn = sqlite3.connect(summaries_path)
        try:
            keep_row_before = conn.execute(
                "SELECT * FROM thread_turn_summaries WHERE thread_id=?",
                (KEEP_SESSION,),
            ).fetchone()
            assert keep_row_before is not None
            column_order_before = [
                str(row[1])
                for row in conn.execute("PRAGMA table_xinfo(thread_turn_summaries)")
            ]
            assert column_order_before == [
                "principal_key",
                "host_key",
                "thread_id",
                "summary",
                "revision",
                "updated_at",
                "compact_summary",
                "compact_summary_turn_key",
            ]
            table_sql = conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='thread_turn_summaries'"
            ).fetchone()
            assert table_sql is not None
            assert module.normalized_sql_hash(str(table_sql[0])) == expected_sql_hash
        finally:
            conn.close()

        original_desktop_owner_processes = module.desktop_owner_processes
        module.desktop_owner_processes = lambda _codex_home: ([], "")
        try:
            module_plan = module.make_plan(
                codex_home,
                [TARGET_CLOSED_CHILD],
                True,
                True,
                False,
            )
            plan = module.plan_to_dict(module_plan)
            result = module.apply_plan(
                module_plan,
                module_plan.historical_residuals,
                True,
                False,
                False,
                False,
                "targets_only",
            )
        finally:
            module.desktop_owner_processes = original_desktop_owner_processes

        assert plan["blockers"] == []
        assert plan["counts"]["desktop_auxiliary_thread_rows"] == 2
        assert plan["auxiliary_thread_rows"] == {"codex-thread-summaries-dev.db": 2}
        contract = plan["auxiliary_thread_contracts"]["codex-thread-summaries-dev.db"]
        assert contract["user_version"] == 2
        assert len(contract["rows"]) == 2
        assert result["success"] is True
        assert result["auxiliary_thread_cleanup"]["rows_removed"] == {
            "codex-thread-summaries-dev.db": 2
        }
        assert result["verification"]["verification_ok"] is True
        assert (
            result["verification"]["residual_counts"]["desktop_auxiliary_thread_rows"]
            == 0
        )

        conn = sqlite3.connect(summaries_path)
        try:
            marks = ",".join("?" for _ in target_ids)
            assert conn.execute(
                "SELECT COUNT(*) FROM thread_turn_summaries "
                f"WHERE thread_id IN ({marks})",
                target_ids,
            ).fetchone() == (0,)
            assert (
                conn.execute(
                    "SELECT * FROM thread_turn_summaries WHERE thread_id=?",
                    (KEEP_SESSION,),
                ).fetchone()
                == keep_row_before
            )
            assert [
                str(row[1])
                for row in conn.execute("PRAGMA table_xinfo(thread_turn_summaries)")
            ] == column_order_before
            table_sql = conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='thread_turn_summaries'"
            ).fetchone()
            assert table_sql is not None
            assert module.normalized_sql_hash(str(table_sql[0])) == expected_sql_hash
            assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        finally:
            conn.close()


def exercise_auxiliary_thread_row_stale_plan_rejection() -> None:
    target_ids = [TARGET_CLOSED_CHILD, TARGET_GRANDCHILD]
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        auxiliary_paths = init_auxiliary_thread_databases(codex_home, target_ids)

        module = load_deleter_module()
        original_desktop_owner_processes = module.desktop_owner_processes
        module.desktop_owner_processes = lambda _codex_home: ([], "")
        try:
            module_plan = module.make_plan(
                codex_home,
                [TARGET_CLOSED_CHILD],
                True,
                True,
                False,
            )
            plan = module.plan_to_dict(module_plan)
            assert plan["blockers"] == []

            history_path = auxiliary_paths["codex-history-snapshots-dev.db"]
            changed_payload = json.dumps(
                {"changed": "after-report"}, separators=(",", ":")
            )
            conn = sqlite3.connect(history_path)
            with conn:
                conn.execute(
                    "UPDATE app_server_history_snapshots "
                    "SET payload_json=?, payload_bytes=? WHERE thread_id=?",
                    (
                        changed_payload,
                        len(changed_payload.encode("utf-8")),
                        TARGET_CLOSED_CHILD,
                    ),
                )
            conn.close()
            canonical_after_change = snapshot_tree_bytes(codex_home)

            result = module.apply_plan(
                module_plan,
                module_plan.historical_residuals,
                True,
                False,
                False,
                False,
                "targets_only",
            )
        finally:
            module.desktop_owner_processes = original_desktop_owner_processes
        assert result["outcome"] == "completed_with_warnings"
        assert result["success"] is True
        assert result["component_results"]["auxiliary_thread_databases"]["status"] == (
            "completed"
        )
        database_results = result["auxiliary_thread_cleanup"]["database_results"]
        assert database_results["codex-history-snapshots-dev.db"]["status"] == (
            "skipped_safely"
        )
        assert database_results["codex-thread-summaries-dev.db"]["status"] == (
            "completed"
        )
        assert snapshot_tree_bytes(codex_home) != canonical_after_change

        conn = sqlite3.connect(history_path)
        try:
            assert conn.execute(
                "SELECT payload_json FROM app_server_history_snapshots "
                "WHERE thread_id=?",
                (TARGET_CLOSED_CHILD,),
            ).fetchone() == (changed_payload,)
        finally:
            conn.close()
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM threads WHERE id IN (?, ?)",
                target_ids,
            ).fetchone() == (0,)
        finally:
            conn.close()


def exercise_auxiliary_unknown_schema_blockers() -> None:
    target_ids = [TARGET_CLOSED_CHILD, TARGET_GRANDCHILD]
    module = load_deleter_module()
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        auxiliary_paths = init_auxiliary_thread_databases(codex_home, target_ids)
        history_path = auxiliary_paths["codex-history-snapshots-dev.db"]
        conn = sqlite3.connect(history_path)
        with conn:
            conn.execute("PRAGMA user_version=99")
            conn.execute(
                "ALTER TABLE app_server_history_snapshots "
                "ADD COLUMN future_note TEXT"
            )
            conn.execute(
                "CREATE TABLE future_auxiliary_state "
                "(note TEXT NOT NULL DEFAULT '')"
            )
            conn.execute(
                "CREATE VIEW future_auxiliary_view AS "
                "SELECT note FROM future_auxiliary_state"
            )
        conn.close()

        assessment = module.auxiliary_thread_database_assessment(
            codex_home, target_ids
        )
        assert assessment["issues"] == []
        history_plan = assessment["database_plans"][
            "codex-history-snapshots-dev.db"
        ]
        assert history_plan["status"] == "enabled"
        assert history_plan["compatibility"]["newer_user_version_accepted"] is True
        assert "future_auxiliary_state" in history_plan["compatibility"][
            "unknown_tables"
        ]

        original_desktop_owner_processes = module.desktop_owner_processes
        module.desktop_owner_processes = lambda _codex_home: ([], "")
        try:
            plan = module.make_plan(
                codex_home,
                [TARGET_CLOSED_CHILD],
                True,
                True,
                False,
            )
            assert plan.blockers == []
            result = module.apply_plan(
                plan,
                plan.historical_residuals,
                True,
                False,
                False,
                False,
                "targets_only",
            )
        finally:
            module.desktop_owner_processes = original_desktop_owner_processes
        assert result["success"] is True
        assert result["verification"]["verification_ok"] is True
        assert all(
            entry["status"] == "completed"
            for entry in result["auxiliary_thread_cleanup"][
                "database_results"
            ].values()
        )

    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        auxiliary_paths = init_auxiliary_thread_databases(codex_home, target_ids)
        history_path = auxiliary_paths["codex-history-snapshots-dev.db"]
        summaries_path = auxiliary_paths["codex-thread-summaries-dev.db"]
        conn = sqlite3.connect(history_path)
        with conn:
            conn.execute(
                "CREATE TABLE future_auxiliary_links "
                "(thread_id TEXT NOT NULL, note TEXT)"
            )
            conn.execute(
                "INSERT INTO future_auxiliary_links VALUES (?, 'preserve')",
                (TARGET_CLOSED_CHILD,),
            )
        conn.close()

        plan = module.make_plan(
            codex_home,
            [TARGET_CLOSED_CHILD],
            True,
            True,
            False,
        )
        assert plan.auxiliary_thread_database_plans[
            "codex-history-snapshots-dev.db"
        ]["status"] == "skipped"
        assert plan.auxiliary_thread_database_plans[
            "codex-thread-summaries-dev.db"
        ]["status"] == "enabled"
        assert_safety_warning(plan, "future_auxiliary_links.thread_id")

        original_desktop_owner_processes = module.desktop_owner_processes
        module.desktop_owner_processes = lambda _codex_home: ([], "")
        try:
            result = module.apply_plan(
                plan,
                plan.historical_residuals,
                True,
                False,
                False,
                False,
                "targets_only",
            )
        finally:
            module.desktop_owner_processes = original_desktop_owner_processes
        assert result["outcome"] == "completed_with_warnings"
        assert result["verification"]["verification_ok"] is True
        database_results = result["auxiliary_thread_cleanup"]["database_results"]
        assert database_results["codex-history-snapshots-dev.db"]["status"] == (
            "skipped_safely"
        )
        assert database_results["codex-thread-summaries-dev.db"]["status"] == (
            "completed"
        )
        conn = sqlite3.connect(history_path)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM app_server_history_snapshots "
                "WHERE thread_id IN (?, ?)",
                target_ids,
            ).fetchone() == (2,)
        finally:
            conn.close()
        conn = sqlite3.connect(summaries_path)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM thread_turn_summaries "
                "WHERE thread_id IN (?, ?)",
                target_ids,
            ).fetchone() == (0,)
        finally:
            conn.close()


def exercise_voice_selector_object_cleanup() -> None:
    target_id = NEW_CHILD
    selector = {
        "conversationId": target_id,
        "hostId": "local",
        "version": 27,
    }
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        _catalog_path, prompt_mentions = init_desktop_ui_metadata(
            codex_home,
            target_id,
        )
        set_voice_selector(codex_home, selector)

        module = load_deleter_module()
        original_desktop_owner_processes = module.desktop_owner_processes
        module.desktop_owner_processes = lambda _codex_home: ([], "")
        try:
            module_plan = module.make_plan(
                codex_home,
                [target_id],
                True,
                True,
                False,
            )
            plan = module.plan_to_dict(module_plan)
            result = module.apply_plan(
                module_plan,
                module_plan.historical_residuals,
                True,
                False,
                False,
                False,
                "targets_only",
            )
        finally:
            module.desktop_owner_processes = original_desktop_owner_processes

        assert plan["blockers"] == []
        assert plan["counts"]["global_state_structural_refs"] == 12
        for filename in [
            ".codex-global-state.json",
            ".codex-global-state.json.bak",
        ]:
            voice_contracts = [
                contract
                for contract in plan["global_state"]["structural_refs"][filename]
                if contract["kind"] == "voice_selector"
            ]
            assert voice_contracts == [
                {
                    "kind": "voice_selector",
                    "container_path": [
                        "electron-persisted-atom-state",
                        "realtime-voice-most-recent-thread",
                    ],
                    "conversation_id": target_id,
                    "value_sha256": module.json_value_sha256(selector),
                }
            ]

        assert result["success"] is True
        assert result["global_state_cleanup"]["removed"] == 12
        assert result["verification"]["verification_ok"] is True
        assert (
            result["verification"]["residual_counts"]["global_state_structural_refs"]
            == 0
        )
        for filename in [
            ".codex-global-state.json",
            ".codex-global-state.json.bak",
        ]:
            data = json.loads((codex_home / filename).read_text(encoding="utf-8"))
            atom = data["electron-persisted-atom-state"]
            assert atom["realtime-voice-most-recent-thread"] is None
            assert atom["prompt-history"][KEEP_SESSION] == prompt_mentions
            assert KEEP_SESSION in atom["heartbeat-thread-permissions-by-id"]
            assert KEEP_SESSION in atom["thread-descriptions-v1"]
            assert f"thread-reference-capability:{KEEP_SESSION}" in atom
            assert KEEP_SESSION in data["projectless-thread-ids"]
            assert KEEP_SESSION in data["thread-projectless-output-directories"]
            assert data["unrelated-setting"] == {"sentinel": "preserve-me"}


def exercise_voice_selector_nonstandard_shape_cleanup() -> None:
    target_id = TARGET_CLOSED_CHILD
    module = load_deleter_module()
    invalid_selectors = [
        {
            "conversationId": target_id,
            "hostId": "local",
            "version": 27,
            "future": True,
        },
        {"conversationId": target_id, "hostId": "", "version": 27},
        {"conversationId": target_id, "hostId": "local", "version": True},
        {"conversationId": target_id, "hostId": "local", "version": -1},
        {
            "conversationId": target_id.upper(),
            "hostId": "local",
            "version": 27,
        },
    ]
    for selector in invalid_selectors:
        refs, _mentions, issues, warnings = module.global_state_snapshot(
            {
                "electron-persisted-atom-state": {
                    "realtime-voice-most-recent-thread": selector,
                }
            },
            [target_id],
        )
        assert any(contract["kind"] == "voice_selector" for contract in refs)
        assert issues == []
        assert any(
            "voice selector has a nonstandard shape" in item for item in warnings
        )

    refs, _mentions, issues, warnings = module.global_state_snapshot(
        {
            "electron-persisted-atom-state": {
                "realtime-voice-most-recent-thread": target_id.upper(),
            }
        },
        [target_id],
    )
    assert any(contract["kind"] == "scalar_value" for contract in refs)
    assert issues == []
    assert any("non-canonical session ID" in item for item in warnings)

    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        init_desktop_ui_metadata(codex_home, target_id)
        set_voice_selector(
            codex_home,
            invalid_selectors[-1],
            [".codex-global-state.json"],
        )
        set_voice_selector(
            codex_home,
            target_id.upper(),
            [".codex-global-state.json.bak"],
        )

        original_desktop_owner_processes = module.desktop_owner_processes
        module.desktop_owner_processes = lambda _codex_home: ([], "")
        try:
            module_plan = module.make_plan(
                codex_home,
                [target_id],
                True,
                True,
                False,
            )
            plan = module.plan_to_dict(module_plan)
            assert plan["preflight"]["global_state_issues"] == []
            assert plan["blockers"] == []
            assert any(
                "voice selector has a nonstandard shape" in warning
                for warning in plan["preflight"]["global_state_warnings"]
            )
            assert any(
                "non-canonical session ID" in warning
                for warning in plan["preflight"]["global_state_warnings"]
            )
            result = module.apply_plan(
                module_plan,
                module_plan.historical_residuals,
                True,
                False,
                False,
                False,
                "targets_only",
            )
        finally:
            module.desktop_owner_processes = original_desktop_owner_processes

        assert result["success"] is True
        for filename in [
            ".codex-global-state.json",
            ".codex-global-state.json.bak",
        ]:
            data = json.loads((codex_home / filename).read_text(encoding="utf-8"))
            assert (
                data["electron-persisted-atom-state"].get(
                    "realtime-voice-most-recent-thread"
                )
                is None
            )


def exercise_voice_selector_stale_plan_rejection() -> None:
    target_id = NEW_CHILD
    selector = {
        "conversationId": target_id,
        "hostId": "local",
        "version": 27,
    }
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        catalog_path, _prompt_mentions = init_desktop_ui_metadata(
            codex_home,
            target_id,
        )
        set_voice_selector(codex_home, selector)

        module = load_deleter_module()
        original_desktop_owner_processes = module.desktop_owner_processes
        module.desktop_owner_processes = lambda _codex_home: ([], "")
        try:
            module_plan = module.make_plan(
                codex_home,
                [target_id],
                True,
                True,
                False,
            )
            plan = module.plan_to_dict(module_plan)
            assert plan["blockers"] == []

            changed_selector = dict(selector)
            changed_selector["hostId"] = "remote-after-report"
            set_voice_selector(
                codex_home,
                changed_selector,
                [".codex-global-state.json"],
            )
            canonical_after_change = snapshot_tree_bytes(codex_home)

            result = module.apply_plan(
                module_plan,
                module_plan.historical_residuals,
                True,
                False,
                False,
                False,
                "targets_only",
            )
        finally:
            module.desktop_owner_processes = original_desktop_owner_processes
        assert result["outcome"] == "completed_with_warnings"
        assert result["success"] is True
        assert result["component_results"]["global_state"]["status"] == (
            "skipped_safely"
        )
        assert snapshot_tree_bytes(codex_home) != canonical_after_change

        data = json.loads(
            (codex_home / ".codex-global-state.json").read_text(encoding="utf-8")
        )
        assert (
            data["electron-persisted-atom-state"]["realtime-voice-most-recent-thread"]
            == changed_selector
        )
        conn = sqlite3.connect(catalog_path)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM local_thread_catalog WHERE thread_id=?",
                (target_id,),
            ).fetchone() == (0,)
        finally:
            conn.close()


def exercise_discovered_exact_uuid_mapping_cleanup() -> None:
    target_id = TARGET_CLOSED_CHILD
    binding_key = "client-new-thread:6b6bb59f-ee4f-46d0-9e45-aac3e046f1c3"
    keep_key = "client-new-thread:keep"
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        init_desktop_ui_metadata(codex_home, target_id)
        for filename in [
            ".codex-global-state.json",
            ".codex-global-state.json.bak",
        ]:
            path = codex_home / filename
            data = json.loads(path.read_text(encoding="utf-8"))
            atom = data["electron-persisted-atom-state"]
            atom["client-thread-bindings-v1"] = {
                binding_key: target_id,
                keep_key: KEEP_SESSION,
            }
            atom["future-thread-object"] = {
                "conversationId": target_id,
                "label": "preserve-unknown-object",
            }
            path.write_text(json.dumps(data), encoding="utf-8")

        module = load_deleter_module()
        original_desktop_owner_processes = module.desktop_owner_processes
        module.desktop_owner_processes = lambda _codex_home: ([], "")
        try:
            module_plan = module.make_plan(
                codex_home,
                [target_id],
                True,
                True,
                False,
            )
            plan = module.plan_to_dict(module_plan)
            assert plan["blockers"] == []
            assert plan["preflight"]["global_state_issues"] == []
            assert any(
                "future-thread-object/conversationId" in warning
                for warning in plan["preflight"]["global_state_warnings"]
            )
            for filename in [
                ".codex-global-state.json",
                ".codex-global-state.json.bak",
            ]:
                discovered = [
                    contract
                    for contract in plan["global_state"]["structural_refs"][filename]
                    if contract.get("discovered") == "exact_uuid_map_value"
                ]
                assert discovered == [
                    {
                        "kind": "map_key",
                        "container_path": [
                            "electron-persisted-atom-state",
                            "client-thread-bindings-v1",
                        ],
                        "key": binding_key,
                        "target_session_id": target_id,
                        "value_sha256": module.json_value_sha256(target_id),
                        "discovered": "exact_uuid_map_value",
                    }
                ]
            result = module.apply_plan(
                module_plan,
                module_plan.historical_residuals,
                True,
                False,
                False,
                False,
                "targets_only",
            )
        finally:
            module.desktop_owner_processes = original_desktop_owner_processes

        assert result["success"] is True
        assert result["verification"]["verification_ok"] is True
        assert any(
            "future-thread-object/conversationId" in warning.get("reason", "")
            for warning in result["safety_warnings"]
        )
        for filename in [
            ".codex-global-state.json",
            ".codex-global-state.json.bak",
        ]:
            data = json.loads((codex_home / filename).read_text(encoding="utf-8"))
            atom = data["electron-persisted-atom-state"]
            bindings = atom["client-thread-bindings-v1"]
            assert binding_key not in bindings
            assert bindings[keep_key] == KEEP_SESSION
            assert atom["future-thread-object"] == {
                "conversationId": target_id,
                "label": "preserve-unknown-object",
            }


def exercise_historical_fail_closed_without_state() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        rollout = codex_home / "sessions" / f"rollout-{TARGET_PARENT}.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text("{}\n", encoding="utf-8")
        index = codex_home / "session_index.jsonl"
        index.write_text(json.dumps({"id": TARGET_PARENT}) + "\n", encoding="utf-8")

        report = run_cmd(
            TARGET_PARENT,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        assert plan["blockers"] == []
        assert plan["historical_residuals"]["scanned"] is False

        run_cmd(
            TARGET_PARENT,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--apply",
            "--confirm-plan",
            scope_token(plan),
            "--apply-historical-residuals",
            "--json",
            expect=3,
        )
        assert rollout.exists()
        assert TARGET_PARENT in index.read_text(encoding="utf-8")


def exercise_unrelated_schema_extension_is_compatible() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            conn.execute("CREATE TABLE future_state (note TEXT NOT NULL)")
            conn.execute("INSERT INTO future_state VALUES ('unrelated extension')")
            conn.commit()
        finally:
            conn.close()

        report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        compatibility = plan["preflight"]["state_schema_compatibility"]
        assert "future_state" in compatibility["unknown_tables"]
        assert compatibility["target_reference_hits"] == []
        assert compatibility["scan_complete"] is True
        assert plan["preflight"]["state_authoritative"] is True
        assert plan["historical_residuals"]["scanned"] is True
        assert plan["component_plans"]["state_and_index"]["status"] == "enabled"
        applied = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--apply",
            "--confirm-plan",
            scope_token(plan),
            "--json",
        )
        result = json.loads(applied.stdout)["apply_result"]
        assert result["success"] is True
        assert result["verification"]["verification_ok"] is True
        assert result["component_results"]["state_and_index"]["status"] == "completed"
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM threads WHERE id=?", (KEEP_SESSION,)
            ).fetchone() == (0,)
            assert conn.execute("SELECT note FROM future_state").fetchone() == (
                "unrelated extension",
            )
        finally:
            conn.close()


def exercise_unknown_target_reference_preserves_state_component() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        target_rollout = (
            codex_home
            / "sessions"
            / "2026"
            / "05"
            / "03"
            / f"rollout-{KEEP_SESSION}.jsonl"
        )
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            conn.execute("CREATE TABLE future_state (note TEXT NOT NULL)")
            conn.execute(
                "INSERT INTO future_state VALUES (?)",
                (json.dumps({"conversation_id": KEEP_SESSION}),),
            )
            conn.commit()
        finally:
            conn.close()

        report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        compatibility = plan["preflight"]["state_schema_compatibility"]
        assert compatibility["protected_ids"] == [KEEP_SESSION]
        assert compatibility["target_reference_hits"] == [
            {"table": "future_state", "column": "note", "ids": [KEEP_SESSION]}
        ]
        assert_safety_warning(plan, "extension contains target session references")
        assert plan["component_plans"]["state_and_index"]["status"] == "skipped"
        assert plan["component_plans"]["rollout_artifacts"]["status"] == "enabled"

        applied = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--apply",
            "--confirm-plan",
            scope_token(plan),
            "--json",
        )
        result = json.loads(applied.stdout)["apply_result"]
        assert_controlled_warning_first_result(result)
        assert result["component_results"]["state_and_index"]["status"] == (
            "skipped_safely"
        )
        assert not target_rollout.exists()
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM threads WHERE id=?", (KEEP_SESSION,)
            ).fetchone() == (1,)
            assert KEEP_SESSION in conn.execute(
                "SELECT note FROM future_state"
            ).fetchone()[0]
        finally:
            conn.close()


def exercise_logs_schema_extensions_are_isolated() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        conn = sqlite3.connect(codex_home / "logs_2.sqlite")
        with conn:
            conn.execute("CREATE TABLE future_logs (note TEXT NOT NULL)")
            conn.execute("INSERT INTO future_logs VALUES ('keep extension')")
        conn.close()

        report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        compatibility = plan["preflight"]["logs_schema_compatibility"]
        assert compatibility["unknown_tables"] == ["future_logs"]
        assert compatibility["target_reference_hits"] == []
        assert plan["component_plans"]["logs"]["status"] == "enabled"
        applied = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--apply",
            "--confirm-plan",
            scope_token(plan),
            "--json",
        )
        result = json.loads(applied.stdout)["apply_result"]
        assert result["success"] is True
        assert result["verification"]["verification_ok"] is True
        conn = sqlite3.connect(codex_home / "logs_2.sqlite")
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM logs WHERE thread_id=?", (KEEP_SESSION,)
            ).fetchone() == (0,)
            assert conn.execute("SELECT note FROM future_logs").fetchone() == (
                "keep extension",
            )
        finally:
            conn.close()

    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        conn = sqlite3.connect(codex_home / "logs_2.sqlite")
        with conn:
            conn.execute("CREATE TABLE future_logs (payload TEXT NOT NULL)")
            conn.execute(
                "INSERT INTO future_logs VALUES (?)",
                (json.dumps({"conversation_id": KEEP_SESSION}),),
            )
        conn.close()

        report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        assert plan["component_plans"]["logs"]["status"] == "skipped"
        assert plan["component_plans"]["state_and_index"]["status"] == "enabled"
        assert any(
            warning["code"] == "logs_schema_extension_target_reference"
            for warning in plan["safety_warnings"]
        )
        applied = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--apply",
            "--confirm-plan",
            scope_token(plan),
            "--json",
        )
        result = json.loads(applied.stdout)["apply_result"]
        assert result["outcome"] == "completed_with_warnings"
        assert result["verification"]["verification_ok"] is True
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM threads WHERE id=?", (KEEP_SESSION,)
            ).fetchone() == (0,)
        finally:
            conn.close()
        conn = sqlite3.connect(codex_home / "logs_2.sqlite")
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM logs WHERE thread_id=?", (KEEP_SESSION,)
            ).fetchone() == (1,)
            assert KEEP_SESSION in conn.execute(
                "SELECT payload FROM future_logs"
            ).fetchone()[0]
        finally:
            conn.close()


def exercise_unknown_json_reference_protects_historical_residual() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        orphan_rollout = (
            codex_home / "sessions" / "future" / f"rollout-{OLD_FILE_ONLY}.jsonl"
        )
        orphan_rollout.parent.mkdir(parents=True)
        orphan_rollout.write_text("{}\n", encoding="utf-8")
        (codex_home / "session_index.jsonl").write_text(
            json.dumps({"id": OLD_FILE_ONLY}) + "\n",
            encoding="utf-8",
        )
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            conn.execute("CREATE TABLE future_state (payload_json TEXT NOT NULL)")
            conn.execute(
                "INSERT INTO future_state VALUES (?)",
                (json.dumps({"nested": {"thread_uuid": OLD_FILE_ONLY}}),),
            )
            conn.commit()
        finally:
            conn.close()

        report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        historical = plan["historical_residuals"]
        assert historical["scanned"] is True
        assert OLD_FILE_ONLY in historical["extension_protected_ids"]
        assert OLD_FILE_ONLY in historical["schema_compatibility"]["protected_ids"]
        assert all(
            entry["id"] != OLD_FILE_ONLY
            for entry in historical["rollout_files_without_state"]
        )
        assert all(
            entry["id"] != OLD_FILE_ONLY
            for entry in historical["session_index_rows_without_state"]
        )
        assert orphan_rollout.exists()


def exercise_recent_log_only_ids_are_transient() -> None:
    recent_id = "14141414-1414-1414-1414-141414141414"
    old_id = "15151515-1515-1515-1515-151515151515"
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        now = int(time.time())
        conn = sqlite3.connect(codex_home / "logs_2.sqlite")
        try:
            conn.execute(
                "INSERT INTO logs (ts, ts_nanos, level, target, thread_id) "
                "VALUES (?, 0, 'INFO', 'old-worker', ?)",
                (now - 3601, old_id),
            )
            conn.commit()
        finally:
            conn.close()

        first_report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        first_plan = json.loads(first_report.stdout)["plan"]
        first_historical = first_plan["historical_residuals"]
        assert any(
            entry["id"] == old_id
            for entry in first_historical["logs_rows_without_state"]
        )

        conn = sqlite3.connect(codex_home / "logs_2.sqlite")
        try:
            conn.execute(
                "INSERT INTO logs (ts, ts_nanos, level, target, thread_id) "
                "VALUES (?, 0, 'INFO', 'recent-worker', ?)",
                (now, recent_id),
            )
            conn.commit()
        finally:
            conn.close()

        second_report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        second_plan = json.loads(second_report.stdout)["plan"]
        second_historical = second_plan["historical_residuals"]
        assert all(
            entry["id"] != recent_id
            for entry in second_historical["logs_rows_without_state"]
        )
        assert recent_id not in json.dumps(second_historical, sort_keys=True)
        assert any(
            "Recent log-only worker IDs are treated as transient" in note
            for note in second_historical["notes"]
        )
        assert first_plan["plan_fingerprint"] == second_plan["plan_fingerprint"]


def exercise_thread_sections_schema_blocker() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            conn.execute("ALTER TABLE thread_sections RENAME COLUMN name TO label")
            conn.commit()
        finally:
            conn.close()

        report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        assert_safety_warning(
            plan,
            "State table 'thread_sections' is missing required column(s): name.",
        )
        assert_safety_warning(
            plan,
            "--no-subagents disables recursive graph expansion but does not bypass schema safety",
        )

        no_subagents_report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        no_subagents_plan = json.loads(no_subagents_report.stdout)["plan"]
        assert_safety_warning(
            no_subagents_plan,
            "State table 'thread_sections' is missing required column(s): name.",
        )
        assert not any(
            "Cannot resolve recursive subagents" in str(item.get("message", ""))
            for item in safety_findings(no_subagents_plan)
        )
        applied = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--apply",
            "--confirm-plan",
            scope_token(no_subagents_plan),
            "--json",
        )
        result = json.loads(applied.stdout)["apply_result"]
        assert_controlled_warning_first_result(result)
        assert result["component_results"]["state_and_index"]["status"] == (
            "skipped_safely"
        )

        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM threads WHERE id=?", (KEEP_SESSION,)
                ).fetchone()[0]
                == 1
            )
        finally:
            conn.close()


def exercise_uuid_ancestor_regression() -> None:
    with temporary_directory() as tmp:
        uuid_ancestor = Path(tmp) / OLD_FILE_ONLY
        codex_home = uuid_ancestor / ".codex"
        codex_home.mkdir(parents=True)
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")

        report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        assert plan["blockers"] == []
        assert plan["counts"]["rollout_files"] == 1
        rollout_residual_ids = {
            entry["id"]
            for entry in plan["historical_residuals"]["rollout_files_without_state"]
        }
        assert OLD_FILE_ONLY not in rollout_residual_ids


def exercise_ambiguous_artifact_owner_blocker() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        ambiguous = (
            codex_home
            / "sessions"
            / "2026"
            / "05"
            / "03"
            / f"rollout-{OLD_FILE_ONLY}-{KEEP_SESSION}.jsonl"
        )
        ambiguous.write_text("keep-owned sentinel\n", encoding="utf-8")

        report = run_cmd(
            OLD_FILE_ONLY,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        assert_safety_warning(plan, "ambiguous ownership")
        applied = run_cmd(
            OLD_FILE_ONLY,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--apply",
            "--confirm-plan",
            scope_token(plan),
            "--json",
        )
        assert_controlled_warning_first_result(
            json.loads(applied.stdout)["apply_result"]
        )
        assert ambiguous.read_text(encoding="utf-8") == "keep-owned sentinel\n"


def exercise_ambiguous_generated_owner_blocker() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        generated_dir = codex_home / "generated_images" / OLD_FILE_ONLY
        generated_dir.mkdir(parents=True)
        ambiguous = generated_dir / f"image-{KEEP_SESSION}.png"
        ambiguous.write_bytes(b"keep-owned generated sentinel")

        report = run_cmd(
            OLD_FILE_ONLY,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        assert_safety_warning(plan, "ambiguous ownership")
        assert str(generated_dir) not in plan["generated_artifacts"]
        applied = run_cmd(
            OLD_FILE_ONLY,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--apply",
            "--confirm-plan",
            scope_token(plan),
            "--json",
        )
        assert_controlled_warning_first_result(
            json.loads(applied.stdout)["apply_result"]
        )
        assert ambiguous.read_bytes() == b"keep-owned generated sentinel"


def exercise_artifact_content_contract() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        generated_dir = codex_home / "generated_images" / TARGET_CLOSED_CHILD
        generated_dir.mkdir(parents=True)
        image = generated_dir / "image.png"
        image.write_bytes(b"before")
        module = load_deleter_module()
        plan = module.make_plan(
            codex_home,
            [TARGET_CLOSED_CHILD],
            False,
            False,
            False,
        )
        assert plan.generated_artifacts == [generated_dir]
        original_file = image.stat()
        original_directory = generated_dir.stat()

        image.write_bytes(b"after!")
        os.utime(
            image,
            ns=(original_file.st_atime_ns, original_file.st_mtime_ns),
        )
        os.utime(
            generated_dir,
            ns=(original_directory.st_atime_ns, original_directory.st_mtime_ns),
        )
        try:
            module.validate_path_contracts(
                plan.generated_artifacts,
                plan.artifact_contracts,
            )
        except RuntimeError as exc:
            assert "contents changed" in str(exc)
        else:
            raise AssertionError("same-size artifact content replacement was accepted")


def exercise_direct_open_root() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")

        report = run_cmd(
            TARGET_OPEN_CHILD,
            "--codex-home",
            str(codex_home),
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        assert plan["target_count"] == 1
        assert plan["open_subagents"] == [TARGET_OPEN_CHILD]
        retained = run_cmd(
            TARGET_OPEN_CHILD,
            "--codex-home",
            str(codex_home),
            "--apply",
            "--confirm-plan",
            scope_token(plan),
            "--json",
        )
        retained_result = json.loads(retained.stdout)["apply_result"]
        assert retained_result["outcome"] == "no_safe_work"
        assert retained_result["success"] is False
        assert retained_result["mutation_started"] is False
        assert TARGET_OPEN_CHILD in retained_session_ids(retained_result)
        applied = run_cmd(
            TARGET_OPEN_CHILD,
            "--codex-home",
            str(codex_home),
            "--apply",
            "--confirm-plan",
            scope_token(plan, force_open=True),
            "--force-open",
            "--json",
        )
        assert json.loads(applied.stdout)["apply_result"]["success"] is True


def exercise_symlink_blockers() -> None:
    for kind in ["state", "logs", "index", "sessions", "state_wal"]:
        with temporary_directory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            external = root / "external"
            codex_home.mkdir()
            external.mkdir()
            init_state(codex_home / "state_5.sqlite", codex_home)
            init_logs(codex_home / "logs_2.sqlite")
            index = codex_home / "session_index.jsonl"
            index.write_text(json.dumps({"id": KEEP_SESSION}) + "\n", encoding="utf-8")

            external_target: Path
            if kind == "state":
                external_target = external / "state.sqlite"
                (codex_home / "state_5.sqlite").replace(external_target)
                (codex_home / "state_5.sqlite").symlink_to(external_target)
            elif kind == "logs":
                external_target = external / "logs.sqlite"
                (codex_home / "logs_2.sqlite").replace(external_target)
                (codex_home / "logs_2.sqlite").symlink_to(external_target)
            elif kind == "index":
                external_target = external / "session_index.jsonl"
                index.replace(external_target)
                index.symlink_to(external_target)
            elif kind == "sessions":
                external_target = external / "sessions"
                (codex_home / "sessions").replace(external_target)
                (codex_home / "sessions").symlink_to(
                    external_target,
                    target_is_directory=True,
                )
            else:
                external_target = external / "wal-sentinel"
                external_target.write_text("sentinel", encoding="utf-8")
                (codex_home / "state_5.sqlite-wal").symlink_to(external_target)

            before = external_target.read_bytes() if external_target.is_file() else None
            report = run_cmd(
                KEEP_SESSION,
                "--codex-home",
                str(codex_home),
                "--no-subagents",
                "--json",
            )
            plan = json.loads(report.stdout)["plan"]
            assert_safety_warning(plan, "symbolic link")
            applied = run_cmd(
                KEEP_SESSION,
                "--codex-home",
                str(codex_home),
                "--no-subagents",
                "--apply",
                "--confirm-plan",
                scope_token(plan),
                "--json",
            )
            assert_controlled_warning_first_result(
                json.loads(applied.stdout)["apply_result"]
            )
            assert external_target.exists()
            if before is not None:
                assert external_target.read_bytes() == before
            else:
                assert (
                    external_target
                    / "2026"
                    / "05"
                    / "03"
                    / f"rollout-{KEEP_SESSION}.jsonl"
                ).exists()


def exercise_index_nonobject_json() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        index = codex_home / "session_index.jsonl"
        preserved = '[]\nnull\n"text"\n'
        index.write_text(
            preserved + json.dumps({"id": KEEP_SESSION}) + "\n",
            encoding="utf-8",
        )

        report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        assert plan["counts"]["session_index_rows"] == 1
        applied = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--apply",
            "--confirm-plan",
            scope_token(plan),
            "--json",
        )
        assert json.loads(applied.stdout)["apply_result"]["success"] is True
        index_lines = index.read_text(encoding="utf-8").splitlines()
        assert index_lines[:3] == ["[]", "null", '"text"']
        assert index_lines[3].strip() == ""


def exercise_noncanonical_blocker() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        with conn:
            conn.execute(
                "UPDATE threads SET id=? WHERE id=?",
                (KEEP_SESSION.upper(), KEEP_SESSION),
            )
        conn.close()
        conn = sqlite3.connect(codex_home / "logs_2.sqlite")
        with conn:
            conn.execute(
                "UPDATE logs SET thread_id=? WHERE thread_id=?",
                (KEEP_SESSION.upper(), KEEP_SESSION),
            )
        conn.close()

        report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        findings = plan["preflight"]["noncanonical_target_references"]
        assert {item["database"] for item in findings} == {
            "state_5.sqlite",
            "logs_2.sqlite",
        }
        assert_safety_warning(plan, "non-canonical")
        applied = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--apply",
            "--confirm-plan",
            scope_token(plan),
            "--json",
        )
        assert_controlled_warning_first_result(
            json.loads(applied.stdout)["apply_result"]
        )


def load_deleter_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("delete_codex_session_v21", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_offline_helper_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "delete_codex_session_offline_helper_test",
        OFFLINE_HELPER,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def create_offline_helper_test_job(
    helper: ModuleType,
    root: Path,
    *,
    job_id: str,
) -> tuple[Path, str]:
    codex_home = root / ".codex"
    codex_home.mkdir(mode=0o700)
    token = f"v3.synthetic_{job_id}." + "a" * 64
    request = helper.build_request(
        codex_home=codex_home,
        session_ids=[TARGET_PARENT],
        approval_token=token,
        options={
            "include_subagents": True,
            "include_logs": True,
            "scan_historical": False,
            "apply_historical_residuals": False,
            "apply_missing_rollout_threads": False,
            "force_open": False,
        },
        timing={
            "launch_delay_seconds": 0.0,
            "quit_timeout_seconds": 5.0,
            "offline_stability_seconds": 0.5,
            "poll_interval_seconds": 0.05,
            "restart_timeout_seconds": 1.0,
        },
        restart_requested=False,
        expires_in_seconds=900,
        job_id=job_id,
        exit_mode=helper.EXIT_MODE_MANUAL_GHOSTTY,
    )
    job_dir, _initial = helper.create_job(request, root / "jobs")
    helper.ReceiptWriter(job_dir).update(
        phase="terminal_launch_submitted",
        final_approval_recorded=True,
        terminal_launch_attempts=1,
        terminal_launch_submitted_at_epoch_ms=helper.now_epoch_ms(),
    )
    return job_dir, token


def fake_offline_core(token: str) -> ModuleType:
    core = ModuleType("fake_offline_core_success")
    core.parse_args = lambda: None

    def apply_plan(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "outcome": "completed",
            "next_action": "reopen_and_verify",
            "success": True,
            "execution_ok": True,
            "mutation_started": True,
            "component_results": {
                "state_and_index": {
                    "status": "completed",
                    "mutation_started": True,
                }
            },
            "verification": {"verification_ok": True},
            "diagnostic": f"redact this capsule: {token}",
        }

    def main() -> int:
        core.parse_args()
        apply_result = core.apply_plan()
        print(
            json.dumps(
                {
                    "mode": "apply",
                    "plan": {
                        "script_version": "test",
                        "plan_contract_version": 13,
                        "approval_contract_version": 4,
                        "plan_fingerprint": "f" * 64,
                        "root_ids": [TARGET_PARENT],
                        "target_ids": [TARGET_PARENT],
                        "blockers": [],
                        "counts": {"state_threads": 1},
                        "approval_tokens": {"targets_only": token},
                    },
                    "apply_result": apply_result,
                }
            )
        )
        return 0

    core.apply_plan = apply_plan
    core.main = main
    return core


def read_offline_receipt(job_dir: Path) -> dict[str, object]:
    value = json.loads((job_dir / "receipt.json").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def run_helper_captured(
    helper: ModuleType,
    args: object,
    *,
    entrypoint: str,
) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = getattr(helper, entrypoint)(args)
    return int(result), stdout.getvalue(), stderr.getvalue()


def fake_staged_core(token: str, *, historical: bool) -> ModuleType:
    core = ModuleType("fake_staged_core")
    scope = "targets_and_historical_residuals" if historical else "targets_only"
    historical_residuals = {
        "scanned": True,
        "summary": {
            "total_items": 17 if historical else 0,
            "total_ids": 3 if historical else 0,
            "has_residuals": historical,
        },
        "state_threads_missing_rollout_file": [],
    }
    plan = type(
        "FakeStagedPlan",
        (),
        {
            "historical_residuals": historical_residuals,
            "root_ids": [TARGET_PARENT],
            "target_ids": [TARGET_PARENT],
            "counts": {"state_threads": 1, "historical_total_items": 17},
            "bytes_to_remove": 4096,
            "open_subagents": [],
            "preflight": {"desktop_offline_required": True},
            "warnings": [],
            "blockers": [],
            "unsafe_paths": [],
        },
    )()
    core.make_plan = lambda **_kwargs: plan
    core.approval_scope_key = lambda *_args: scope
    core.approval_tokens = lambda _plan: {scope: token}
    core.validated_approval_payload = lambda *_args: {
        "scope": scope,
        "historical_snapshot": historical_residuals,
    }
    core.plan_fingerprint = lambda _plan: "1" * 64
    core.target_plan_fingerprint = lambda _plan: "2" * 64
    core.approval_scope_fingerprint = lambda *_args: "1" * 64
    core.missing_rollout_open_threads = lambda _plan: []
    core.missing_rollout_current_sessions = lambda _plan: []
    return core


def stage_synthetic_handoff(
    helper: ModuleType,
    root: Path,
    token: str,
    *,
    historical: bool,
) -> tuple[Path, dict[str, object], str, str]:
    codex_home = root / ".codex"
    codex_home.mkdir(mode=0o700)
    helper.safe_interpreter_bytes = (
        lambda _path: b"approved synthetic interpreter"
    )
    core = fake_staged_core(token, historical=historical)
    helper.load_verified_core_module = lambda _path, _source: core
    helper.preflight_approved_request = lambda _request: {
        "validated": True,
        "target_plan_fingerprint": "2" * 64,
        "target_ids": [TARGET_PARENT],
        "target_count": 1,
        "desktop_offline_required": True,
        "blocker_count": 0,
        "checked_at_epoch_ms": helper.now_epoch_ms(),
    }
    argv = [
        "stage",
        TARGET_PARENT,
        "--codex-home",
        str(codex_home),
        "--job-root",
        str(root / "jobs"),
        "--confirm-plan-fingerprint",
        "1" * 64,
    ]
    if historical:
        argv.append("--apply-historical-residuals")
    args = helper.parse_args(argv)
    exit_code, stdout, stderr = run_helper_captured(
        helper,
        args,
        entrypoint="stage_handoff",
    )
    assert exit_code == helper.EXIT_OK
    payload = json.loads(stdout)
    assert isinstance(payload, dict)
    return Path(str(payload["job_dir"])), payload, stdout, stderr


def launch_staged_job_synthetically(helper: ModuleType, job_dir: Path) -> None:
    helper.ghostty_launch_arguments = lambda *_args, **_kwargs: [
        "/usr/bin/osascript",
        "--job-dir",
        str(job_dir),
    ]
    launches: list[Path] = []

    def successful_launch(
        launched_job: Path,
        _interpreter_path: str,
    ) -> tuple[bool, str, str]:
        launches.append(launched_job)
        return True, helper.DEFAULT_GHOSTTY_BUNDLE_ID, ""

    helper.launch_ghostty_worker = successful_launch
    args = helper.parse_args(["launch-ghostty", "--job-dir", str(job_dir)])
    exit_code, _stdout, stderr = run_helper_captured(
        helper,
        args,
        entrypoint="launch_staged_ghostty",
    )
    assert exit_code == helper.EXIT_OK
    assert stderr == ""
    assert launches == [job_dir]


def exercise_staged_long_capsule_transport_contract() -> None:
    helper = load_offline_helper_module()
    token = "v3." + "A" * 65_000 + "." + "b" * 64
    assert len(token) > 60_000
    with temporary_directory() as tmp:
        job_dir, staged, stage_stdout, stage_stderr = stage_synthetic_handoff(
            helper,
            Path(tmp),
            token,
            historical=True,
        )
        request_path = job_dir / helper.REQUEST_FILENAME
        receipt_path = job_dir / helper.RECEIPT_FILENAME
        request_text = request_path.read_text(encoding="utf-8")
        receipt_text = receipt_path.read_text(encoding="utf-8")
        assert request_text.count(token) == 1
        assert token not in stage_stdout
        assert token not in stage_stderr
        assert token not in receipt_text
        assert "approval_tokens" not in stage_stdout
        assert job_dir.stat().st_mode & 0o777 == 0o700
        assert request_path.stat().st_mode & 0o777 == 0o600
        assert request_path.stat().st_nlink == 1
        assert receipt_path.stat().st_mode & 0o777 == 0o600
        staged_plan = staged["staged_plan"]
        assert isinstance(staged_plan, dict)
        assert staged_plan["approval_scope"] == "targets_and_historical_residuals"
        assert staged["awaiting_final_approval"] is False
        assert staged["single_approval_bound"] is True
        assert staged["next_action"] == "launch_ghostty"
        assert json.loads(receipt_text)["final_approval_recorded"] is True
        assert "approval_capsule_chars" not in staged_plan
        assert "approval_capsule_sha256" not in staged_plan

        parsed_request = json.loads(request_text)
        assert parsed_request["options"]["apply_historical_residuals"] is True
        assert parsed_request["options"]["apply_missing_rollout_threads"] is False
        assert parsed_request["exit_mode"] == helper.EXIT_MODE_MANUAL_GHOSTTY
        assert parsed_request["restart"]["requested"] is False
        assert parsed_request["schema_version"] == 6
        assert parsed_request["protocol_version"] == 6
        assert parsed_request["cleanup_job_ids"] == []

        fake_app = Path(tmp) / "Ghostty.app"
        helper.validated_ghostty_app = lambda _app=helper.DEFAULT_GHOSTTY_APP: fake_app
        helper.validated_osascript = lambda _path=helper.DEFAULT_OSASCRIPT: Path(
            "/usr/bin/osascript"
        )
        helper.safe_interpreter_bytes = lambda _path: b"approved interpreter"
        captured_calls: list[tuple[list[str], dict[str, object]]] = []
        original_run = helper.subprocess.run

        def capture_launch(
            arguments: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            captured_calls.append((list(arguments), dict(kwargs)))
            return subprocess.CompletedProcess(arguments, 0, "", "")

        helper.subprocess.run = capture_launch
        try:
            launch_args = helper.parse_args(
                ["launch-ghostty", "--job-dir", str(job_dir)]
            )
            exit_code, launch_stdout, launch_stderr = run_helper_captured(
                helper,
                launch_args,
                entrypoint="launch_staged_ghostty",
            )
        finally:
            helper.subprocess.run = original_run
        assert exit_code == helper.EXIT_OK
        assert len(captured_calls) == 1
        arguments, kwargs = captured_calls[0]
        serialized_boundary = repr((arguments, kwargs))
        assert token not in serialized_boundary
        assert TARGET_PARENT not in serialized_boundary
        assert "--apply-historical-residuals" not in serialized_boundary
        assert arguments[0] == "/usr/bin/osascript"
        assert arguments[-3] == "--"
        assert 'application id "com.mitchellh.ghostty"' in arguments[2]
        assert "new surface configuration" in arguments[2]
        assert "wait after command" in arguments[2]
        assert "open -na" not in serialized_boundary
        assert "ghostty-worker" in arguments[-1]
        assert "--job-dir" in arguments[-1]
        assert arguments[-2] == str(job_dir.parent)
        assert str(job_dir) in arguments[-1]
        assert "env" not in kwargs
        assert token not in os.environ.values()
        assert token not in sys.argv
        assert token not in launch_stdout
        assert token not in launch_stderr
        assert token not in receipt_path.read_text(encoding="utf-8")
        assert token in request_path.read_text(encoding="utf-8")


def exercise_stage_rejects_changed_report_before_job_creation() -> None:
    helper = load_offline_helper_module()
    token = "v3.synthetic_changed_report." + "9" * 64
    with temporary_directory() as tmp:
        root = Path(tmp)
        codex_home = root / ".codex"
        codex_home.mkdir(mode=0o700)
        core = fake_staged_core(token, historical=False)
        helper.load_verified_core_module = lambda _path, _source: core
        args = helper.parse_args(
            [
                "stage",
                TARGET_PARENT,
                "--codex-home",
                str(codex_home),
                "--job-root",
                str(root / "jobs"),
                "--confirm-plan-fingerprint",
                "8" * 64,
            ]
        )
        exit_code, stdout, stderr = run_helper_captured(
            helper,
            args,
            entrypoint="stage_handoff",
        )
        assert exit_code == helper.EXIT_PLAN_CHANGED
        assert stderr == ""
        payload = json.loads(stdout)
        assert payload["outcome"] == "plan_changed"
        assert payload["next_action"] == "restage"
        assert payload["mutation_started"] is False
        assert payload["staged"] is False
        assert not (root / "jobs").exists()
        assert token not in stdout


def exercise_stage_accepts_paginated_sidecar_churn() -> None:
    helper = load_offline_helper_module()
    deleter = load_deleter_module()
    with temporary_directory() as tmp:
        root = Path(tmp)
        codex_home = root / ".codex"
        codex_home.mkdir(mode=0o700)
        state_path = codex_home / "state_5.sqlite"
        history_path = codex_home / "thread_history_1.sqlite"
        init_state(state_path, codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        init_paginated_history(history_path, [KEEP_SESSION])
        conn = sqlite3.connect(state_path)
        with conn:
            conn.execute(
                "ALTER TABLE threads ADD COLUMN history_mode TEXT NOT NULL "
                "DEFAULT 'legacy'"
            )
            conn.execute(
                "UPDATE threads SET history_mode='paginated' WHERE id=?",
                (KEEP_SESSION,),
            )
        conn.close()

        wal_conn = sqlite3.connect(history_path)
        assert wal_conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        wal_conn.execute("PRAGMA wal_autocheckpoint=0")
        wal_conn.execute(
            "UPDATE thread_items SET updated_at_ordinal=updated_at_ordinal "
            "WHERE thread_id=?",
            (KEEP_SESSION,),
        )
        wal_conn.commit()
        approved_plan = deleter.make_plan(
            codex_home,
            [KEEP_SESSION],
            False,
            True,
            True,
        )
        approved_fingerprint = deleter.approval_scope_fingerprint(
            approved_plan,
            False,
            False,
            False,
        )
        wal_conn.execute(
            "CREATE TABLE future_runtime_metadata "
            "(id INTEGER PRIMARY KEY, note TEXT)"
        )
        wal_conn.execute(
            "INSERT INTO future_runtime_metadata (note) VALUES (?)",
            ("preserve compatible extension",),
        )
        wal_conn.commit()
        shm_path = Path(str(history_path) + "-shm")
        assert shm_path.is_file()
        shm_stat = shm_path.stat()
        os.utime(
            shm_path,
            ns=(shm_stat.st_atime_ns, shm_stat.st_mtime_ns + 2_000_000),
        )
        wal_conn.close()

        args = helper.parse_args(
            [
                "stage",
                KEEP_SESSION,
                "--codex-home",
                str(codex_home),
                "--job-root",
                str(root / "jobs"),
                "--no-subagents",
                "--confirm-plan-fingerprint",
                approved_fingerprint,
            ]
        )
        exit_code, stdout, stderr = run_helper_captured(
            helper,
            args,
            entrypoint="stage_handoff",
        )
        assert exit_code == helper.EXIT_OK
        assert stderr == ""
        payload = json.loads(stdout)
        assert payload["staged"] is True
        assert payload["mutation_started"] is False
        assert payload["next_action"] == "launch_ghostty"
        job_dir = Path(payload["job_dir"])
        assert job_dir.is_dir()
        receipt = read_offline_receipt(job_dir)
        assert receipt["phase"] == "staged_waiting_for_ghostty_launch"
        assert receipt["final_approval_recorded"] is True

        launch_staged_job_synthetically(helper, job_dir)
        runtime_conn = sqlite3.connect(history_path)
        runtime_conn.execute("PRAGMA wal_autocheckpoint=0")
        runtime_conn.execute(
            "UPDATE _sqlx_migrations SET execution_time=execution_time "
            "WHERE version=1"
        )
        runtime_conn.commit()
        runtime_shm = Path(str(history_path) + "-shm")
        assert runtime_shm.is_file()
        runtime_stat = runtime_shm.stat()
        os.utime(
            runtime_shm,
            ns=(runtime_stat.st_atime_ns, runtime_stat.st_mtime_ns + 3_000_000),
        )
        runtime_conn.close()

        original_loader = helper.load_verified_core_module

        def load_core_with_offline_owner(
            path: Path,
            source: bytes,
        ) -> ModuleType:
            core = original_loader(path, source)
            core.desktop_owner_processes = lambda _bundle_id: ([], "")
            return core

        helper.load_verified_core_module = load_core_with_offline_owner
        helper.desktop_owner_processes = lambda _bundle_id: ([], "")
        helper.wait_for_desktop_offline = lambda *_args, **_kwargs: {
            "offline": True,
            "samples": 3,
            "stability_seconds": 0.5,
            "last_owners": [],
        }
        assert helper.run_worker(job_dir) == helper.EXIT_OK
        completed = read_offline_receipt(job_dir)
        assert completed["phase"] == "complete"
        assert completed["permanent_deletion_complete"] is True
        assert completed["verification_ok"] is True
        conn = sqlite3.connect(history_path)
        try:
            for table in [
                "thread_history_projection_state",
                "thread_turns",
                "thread_items",
            ]:
                assert conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE thread_id=?",
                    (KEEP_SESSION,),
                ).fetchone() == (0,)
            assert conn.execute(
                "SELECT note FROM future_runtime_metadata"
            ).fetchall() == [("preserve compatible extension",)]
        finally:
            conn.close()


def exercise_ghostty_tcc_denial_is_safe_and_retryable() -> None:
    helper = load_offline_helper_module()
    token = "v3.synthetic_ghostty_tcc_denial." + "7" * 64
    with temporary_directory() as tmp:
        job_dir, _staged, _stdout, _stderr = stage_synthetic_handoff(
            helper,
            Path(tmp),
            token,
            historical=False,
        )

        def deny_automation(
            _job_dir: Path,
            _interpreter_path: str,
        ) -> object:
            return helper.GhosttyLaunchResult(
                False,
                helper.DEFAULT_GHOSTTY_BUNDLE_ID,
                "Not authorized to send Apple events. (-1743)",
                "confirmed_not_submitted",
            )

        helper.launch_ghostty_worker = deny_automation
        launch_args = helper.parse_args(
            ["launch-ghostty", "--job-dir", str(job_dir)]
        )
        exit_code, stdout, stderr = run_helper_captured(
            helper,
            launch_args,
            entrypoint="launch_staged_ghostty",
        )
        assert exit_code == helper.EXIT_BLOCKED
        assert stderr == ""
        payload = json.loads(stdout)
        assert payload["launch_outcome"] == "confirmed_not_submitted"
        assert payload["next_action"] == "relaunch_same_job"
        assert payload["retryable_now"] is True
        assert payload["launched"] is False
        receipt = read_offline_receipt(job_dir)
        assert receipt["phase"] == "terminal_launch_failed_before_worker"
        assert receipt["mutation_started"] is False
        assert receipt.get("request_consumed") is not True
        assert (job_dir / helper.REQUEST_FILENAME).exists()


def exercise_staged_scope_and_launch_retry_contract() -> None:
    helper = load_offline_helper_module()
    token = "v3.synthetic_staged_retry." + "c" * 64
    with temporary_directory() as tmp:
        job_dir, _staged, _stdout, _stderr = stage_synthetic_handoff(
            helper,
            Path(tmp),
            token,
            historical=False,
        )
        launch_args = helper.parse_args(["launch-ghostty", "--job-dir", str(job_dir)])
        launch_calls: list[str] = []

        lock_fd = helper.acquire_launch_lock(job_dir)
        try:
            try:
                helper.launch_staged_ghostty(launch_args)
            except helper.HelperError as exc:
                assert "already being launched" in str(exc)
            else:
                raise AssertionError("concurrent Ghostty launch was accepted")
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        assert launch_calls == []

        def launch_with_unconfirmed_outcome(
            _job_dir: Path,
            _interpreter_path: str,
        ) -> tuple[bool, str, str]:
            launch_calls.append("attempt")
            return False, helper.DEFAULT_GHOSTTY_BUNDLE_ID, "injected failure"

        helper.launch_ghostty_worker = launch_with_unconfirmed_outcome
        first_code, first_stdout, _first_stderr = run_helper_captured(
            helper,
            launch_args,
            entrypoint="launch_staged_ghostty",
        )
        assert first_code == helper.EXIT_BLOCKED
        first_output = json.loads(first_stdout)
        assert first_output["launch_outcome"] == "unknown"
        assert first_output["outcome"] == "waiting_for_manual_exit"
        assert first_output["next_action"] == "wait_for_receipt"
        assert first_output["retryable_now"] is False
        assert (job_dir / helper.REQUEST_FILENAME).exists()
        first_receipt = read_offline_receipt(job_dir)
        assert first_receipt["phase"] == "launch_unknown"
        assert first_receipt["next_action"] == "wait_for_receipt"
        assert first_receipt["mutation_started"] is False
        assert first_receipt["terminal_launch_attempts"] == 1

        try:
            helper.launch_staged_ghostty(launch_args)
        except helper.HelperError as exc:
            assert "outcome is not yet known" in str(exc)
        else:
            raise AssertionError("an unconfirmed Ghostty launch was submitted again")
        assert len(launch_calls) == 1
        assert token not in (job_dir / helper.RECEIPT_FILENAME).read_text(
            encoding="utf-8"
        )

        progressed_root = Path(tmp) / "progressed"
        progressed_root.mkdir()
        progressed_job, _payload, _out, _err = stage_synthetic_handoff(
            helper,
            progressed_root,
            token,
            historical=False,
        )
        progressed_args = helper.parse_args(
            ["launch-ghostty", "--job-dir", str(progressed_job)]
        )

        def launch_false_after_worker_progress(
            _job_dir: Path,
            _interpreter_path: str,
        ) -> tuple[bool, str, str]:
            helper.safely_remove_unconsumed_request(progressed_job)
            helper.ReceiptWriter(progressed_job).update(
                phase="mutation_started",
                mutation_started=True,
                request_consumed=True,
            )
            return False, helper.DEFAULT_GHOSTTY_BUNDLE_ID, "open returned false"

        helper.launch_ghostty_worker = launch_false_after_worker_progress
        progressed_code, progressed_stdout, _progressed_stderr = run_helper_captured(
            helper,
            progressed_args,
            entrypoint="launch_staged_ghostty",
        )
        assert progressed_code == helper.EXIT_PARTIAL_OR_VERIFY_FAILED
        progressed_output = json.loads(progressed_stdout)
        assert progressed_output["launch_outcome"] == "worker_progressed"
        assert progressed_output["outcome"] == "partial_possible"
        assert progressed_output["next_action"] == "inspect_partial"
        progressed_receipt = read_offline_receipt(progressed_job)
        assert progressed_receipt["phase"] == "mutation_started"
        assert progressed_receipt["mutation_started"] is True
        assert progressed_receipt["request_consumed"] is True
        assert not (progressed_job / helper.REQUEST_FILENAME).exists()


def exercise_staged_cancel_revokes_capsule() -> None:
    helper = load_offline_helper_module()
    token = "v3.synthetic_staged_cancel." + "d" * 64
    with temporary_directory() as tmp:
        job_dir, _payload, _stdout, _stderr = stage_synthetic_handoff(
            helper,
            Path(tmp),
            token,
            historical=True,
        )
        cancel_args = helper.parse_args(["cancel-staged", "--job-dir", str(job_dir)])
        exit_code, stdout, stderr = run_helper_captured(
            helper,
            cancel_args,
            entrypoint="cancel_staged_handoff",
        )
        assert exit_code == helper.EXIT_OK
        assert json.loads(stdout)["cancelled"] is True
        assert stderr == ""
        assert not (job_dir / helper.REQUEST_FILENAME).exists()
        receipt = read_offline_receipt(job_dir)
        assert receipt["phase"] == "cancelled_before_mutation"
        assert receipt["terminal"] is True
        assert receipt["staged_capsule_revoked"] is True
        assert receipt["mutation_started"] is False
        assert receipt["permanent_deletion_complete"] is False
        assert token not in (job_dir / helper.RECEIPT_FILENAME).read_text(
            encoding="utf-8"
        )

        second_code, _second_stdout, _second_stderr = run_helper_captured(
            helper,
            cancel_args,
            entrypoint="cancel_staged_handoff",
        )
        assert second_code == helper.EXIT_OK
        launch_args = helper.parse_args(["launch-ghostty", "--job-dir", str(job_dir)])
        try:
            helper.launch_staged_ghostty(launch_args)
        except helper.HelperError as exc:
            assert "terminal state" in str(exc)
        else:
            raise AssertionError("a cancelled staged capsule was launched")


def exercise_staged_job_cannot_bypass_ghostty_launch_gate() -> None:
    helper = load_offline_helper_module()
    token = "v3.synthetic_unapproved_stage." + "e" * 64
    with temporary_directory() as tmp:
        job_dir, _payload, _stdout, _stderr = stage_synthetic_handoff(
            helper,
            Path(tmp),
            token,
            historical=False,
        )
        request_path = job_dir / helper.REQUEST_FILENAME
        receipt_path = job_dir / helper.RECEIPT_FILENAME
        request_before = request_path.read_bytes()
        receipt_before = receipt_path.read_bytes()
        try:
            helper.run_worker(job_dir)
        except helper.HelperError as exc:
            assert "Ghostty launch gate" in str(exc)
        else:
            raise AssertionError("an unapproved staged job reached the worker")
        assert request_path.read_bytes() == request_before
        assert receipt_path.read_bytes() == receipt_before
        assert read_offline_receipt(job_dir)["mutation_started"] is False

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            terminal_exit = helper.run_ghostty_worker(job_dir)
        assert terminal_exit == helper.EXIT_BLOCKED
        assert "未能启动" in stdout.getvalue()
        assert token not in stdout.getvalue()
        assert token not in stderr.getvalue()
        assert request_path.read_bytes() == request_before
        assert receipt_path.read_bytes() == receipt_before


def exercise_staged_manual_only_success() -> None:
    helper = load_offline_helper_module()
    assert not hasattr(helper, "request_graceful_quit")
    assert not hasattr(helper, "restart_desktop")
    assert not hasattr(helper, "launch_worker")
    assert not hasattr(helper, "launch_agent_payload")
    with contextlib.redirect_stderr(io.StringIO()):
        for removed_command in [
            "prepare",
            "prepare-terminal",
            "worker",
            "launch-terminal",
            "terminal-worker",
        ]:
            try:
                helper.parse_args([removed_command])
            except SystemExit:
                pass
            else:
                raise AssertionError(
                    f"removed command remains public: {removed_command}"
                )
        for forbidden_argv in [
            ["stage", TARGET_PARENT, "--approval-token-stdin"],
            ["stage", TARGET_PARENT, "--restart"],
            [
                "launch-ghostty",
                "--job-dir",
                "/tmp/" + "a" * 32,
                "--apply-historical-residuals",
            ],
        ]:
            try:
                helper.parse_args(forbidden_argv)
            except SystemExit:
                pass
            else:
                raise AssertionError(
                    f"manual staged CLI accepted forbidden options: {forbidden_argv}"
                )

    with temporary_directory() as tmp:
        job_dir, token = create_offline_helper_test_job(
            helper,
            Path(tmp),
            job_id="d" * 32,
        )
        owner = {
            "pid": 876,
            "uid": os.geteuid(),
            "executable": "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT",
            "bundle_path": "/Applications/ChatGPT.app",
            "bundle_id": helper.DEFAULT_BUNDLE_ID,
        }
        owner_checks = 0
        offline_stable = False

        def owners_then_offline(
            _bundle_id: str,
        ) -> tuple[list[dict[str, object]], str]:
            nonlocal owner_checks
            owner_checks += 1
            return ([owner], "") if owner_checks == 1 else ([], "")

        def wait_for_offline(
            _bundle_id: str,
            _timeout_seconds: float,
            stability_seconds: float,
            _poll_interval_seconds: float,
        ) -> dict[str, object]:
            nonlocal offline_stable
            assert stability_seconds == 0.5
            offline_stable = True
            return {
                "offline": True,
                "samples": 4,
                "stability_seconds": stability_seconds,
                "last_owners": [],
            }

        core = fake_offline_core(token)
        original_apply = core.apply_plan

        def apply_only_after_offline(*args: object, **kwargs: object) -> object:
            assert offline_stable is True
            return original_apply(*args, **kwargs)

        core.apply_plan = apply_only_after_offline
        helper.preflight_approved_request = lambda _request: {
            "validated": True,
            "desktop_offline_required": True,
        }
        helper.desktop_owner_processes = owners_then_offline
        helper.wait_for_desktop_offline = wait_for_offline
        helper.verify_source_contract = lambda _request: b"verified fake core"
        helper.load_verified_core_module = lambda _path, _source: core

        assert helper.run_worker(job_dir) == helper.EXIT_OK
        receipt = read_offline_receipt(job_dir)
        assert receipt["phase"] == "complete"
        assert receipt["desktop_exit_mode"] == helper.EXIT_MODE_MANUAL_GHOSTTY
        assert receipt["graceful_quit_requested"] is False
        assert receipt["automatic_restart_requested"] is False
        assert receipt["mutation_started"] is True
        assert receipt["permanent_deletion_complete"] is True
        assert receipt["restart"] == {
            "requested": False,
            "attempted": False,
            "success": None,
            "manual_reopen_required": True,
        }
        assert not (job_dir / helper.REQUEST_FILENAME).exists()
        assert token not in (job_dir / helper.RECEIPT_FILENAME).read_text(
            encoding="utf-8"
        )


def exercise_staged_manual_offline_timeout_is_retryable() -> None:
    helper = load_offline_helper_module()
    with temporary_directory() as tmp:
        job_dir, token = create_offline_helper_test_job(
            helper,
            Path(tmp),
            job_id="3" * 32,
        )
        owner = {
            "pid": 321,
            "uid": os.geteuid(),
            "executable": "/Applications/Codex.app/Contents/MacOS/Codex",
            "bundle_path": "/Applications/Codex.app",
            "bundle_id": helper.DEFAULT_BUNDLE_ID,
        }
        helper.preflight_approved_request = lambda _request: {
            "validated": True,
            "desktop_offline_required": True,
        }
        helper.desktop_owner_processes = lambda _bundle_id: ([owner], "")
        helper.wait_for_desktop_offline = lambda *_args, **_kwargs: {
            "offline": False,
            "samples": 20,
            "stability_seconds": 0.0,
            "last_owners": [owner],
        }

        def unexpected_core(*_args: object, **_kwargs: object) -> ModuleType:
            raise AssertionError("manual offline timeout invoked the deletion core")

        helper.load_verified_core_module = unexpected_core
        assert helper.run_worker(job_dir) == helper.EXIT_BLOCKED
        receipt = read_offline_receipt(job_dir)
        assert receipt["phase"] == "manual_offline_wait_failed"
        assert receipt["terminal"] is False
        assert receipt["retryable"] is True
        assert receipt["mutation_started"] is False
        assert receipt["request_consumed"] is False
        assert receipt["permanent_deletion_complete"] is False
        assert (job_dir / helper.REQUEST_FILENAME).exists()
        assert token not in (job_dir / helper.RECEIPT_FILENAME).read_text(
            encoding="utf-8"
        )

        helper.ghostty_launch_arguments = lambda *_args, **_kwargs: [
            "/usr/bin/osascript",
            "--job-dir",
            str(job_dir),
        ]
        relaunches: list[Path] = []

        def successful_relaunch(
            retried_job: Path,
            _interpreter_path: str,
        ) -> tuple[bool, str, str]:
            relaunches.append(retried_job)
            return True, helper.DEFAULT_GHOSTTY_BUNDLE_ID, ""

        helper.launch_ghostty_worker = successful_relaunch
        launch_args = helper.parse_args(["launch-ghostty", "--job-dir", str(job_dir)])
        launch_code, _launch_stdout, _launch_stderr = run_helper_captured(
            helper,
            launch_args,
            entrypoint="launch_staged_ghostty",
        )
        assert launch_code == helper.EXIT_OK
        assert relaunches == [job_dir]
        relaunched_receipt = read_offline_receipt(job_dir)
        assert relaunched_receipt["phase"] == "terminal_launch_submitted"
        assert relaunched_receipt["terminal_launch_attempts"] == 2


def exercise_staged_late_target_artifact_is_retained() -> None:
    helper = load_offline_helper_module()
    with temporary_directory() as tmp:
        root = Path(tmp)
        codex_home = root / ".codex"
        codex_home.mkdir()
        state_path = codex_home / "state_5.sqlite"
        init_state(state_path, codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        init_desktop_ui_metadata(codex_home, TARGET_CLOSED_CHILD)
        deleter = load_deleter_module()
        approved_plan = deleter.make_plan(
            codex_home,
            [TARGET_CLOSED_CHILD],
            False,
            True,
            True,
        )
        stage_args = helper.parse_args(
            [
                "stage",
                TARGET_CLOSED_CHILD,
                "--codex-home",
                str(codex_home),
                "--job-root",
                str(root / "jobs"),
                "--no-subagents",
                "--confirm-plan-fingerprint",
                deleter.approval_scope_fingerprint(
                    approved_plan,
                    False,
                    False,
                    False,
                ),
            ]
        )
        exit_code, stdout, _stderr = run_helper_captured(
            helper,
            stage_args,
            entrypoint="stage_handoff",
        )
        assert exit_code == helper.EXIT_OK
        job_dir = Path(json.loads(stdout)["job_dir"])
        launch_staged_job_synthetically(helper, job_dir)

        snapshots = codex_home / "shell_snapshots"
        snapshots.mkdir()
        late_snapshot = snapshots / f"{TARGET_CLOSED_CHILD}.after-stage.sh"
        late_snapshot.write_text(
            "changed-after-stage\n",
            encoding="utf-8",
        )
        helper.desktop_owner_processes = lambda _bundle_id: ([], "")
        helper.wait_for_desktop_offline = lambda *_args, **_kwargs: {
            "offline": True,
            "samples": 3,
            "stability_seconds": 0.5,
            "last_owners": [],
        }
        assert helper.run_worker(job_dir) == helper.EXIT_OK
        receipt = read_offline_receipt(job_dir)
        assert receipt["phase"] == "complete"
        assert receipt["terminal"] is True
        assert receipt["outcome"] == "completed_with_warnings"
        assert receipt["success"] is True
        assert receipt["mutation_started"] is True
        assert receipt["permanent_deletion_complete"] is True
        assert late_snapshot.exists()
        conn = sqlite3.connect(state_path)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM threads WHERE id=?",
                (TARGET_CLOSED_CHILD,),
            ).fetchone() == (0,)
        finally:
            conn.close()
        assert not (job_dir / helper.REQUEST_FILENAME).exists()


def exercise_staged_historical_additions_do_not_expand_scope() -> None:
    helper = load_offline_helper_module()
    with temporary_directory() as tmp:
        root = Path(tmp)
        codex_home = root / ".codex"
        codex_home.mkdir()
        state_path = codex_home / "state_5.sqlite"
        logs_path = codex_home / "logs_2.sqlite"
        init_state(state_path, codex_home)
        init_logs(logs_path)
        init_desktop_ui_metadata(codex_home, TARGET_CLOSED_CHILD)
        snapshots = codex_home / "shell_snapshots"
        snapshots.mkdir()
        approved_snapshot = snapshots / f"{OLD_FILE_ONLY}.approved.sh"
        approved_snapshot.write_text("approved\n", encoding="utf-8")
        deleter = load_deleter_module()
        approved_plan = deleter.make_plan(
            codex_home,
            [TARGET_CLOSED_CHILD],
            False,
            True,
            True,
        )

        stage_args = helper.parse_args(
            [
                "stage",
                TARGET_CLOSED_CHILD,
                "--codex-home",
                str(codex_home),
                "--job-root",
                str(root / "jobs"),
                "--no-subagents",
                "--apply-historical-residuals",
                "--confirm-plan-fingerprint",
                deleter.approval_scope_fingerprint(
                    approved_plan,
                    True,
                    False,
                    False,
                ),
            ]
        )
        exit_code, stdout, _stderr = run_helper_captured(
            helper,
            stage_args,
            entrypoint="stage_handoff",
        )
        assert exit_code == helper.EXIT_OK
        staged = json.loads(stdout)
        job_dir = Path(staged["job_dir"])
        request_path = job_dir / helper.REQUEST_FILENAME
        frozen_request = request_path.read_bytes()
        assert (
            staged["staged_plan"]["approval_scope"]
            == "targets_and_historical_residuals"
        )
        launch_staged_job_synthetically(helper, job_dir)

        later_snapshot = snapshots / f"{NEW_CHILD}.after-stage.sh"
        later_snapshot.write_text("later\n", encoding="utf-8")
        conn = sqlite3.connect(logs_path)
        with conn:
            later_log_id = conn.execute(
                "INSERT INTO logs (ts, ts_nanos, level, target, thread_id) "
                "VALUES (2, 0, 'INFO', 'after-stage', ?)",
                (NEW_CHILD,),
            ).lastrowid
        conn.close()
        assert request_path.read_bytes() == frozen_request

        original_loader = helper.load_verified_core_module

        def load_core_with_offline_owner(
            path: Path,
            source: bytes,
        ) -> ModuleType:
            core = original_loader(path, source)
            core.desktop_owner_processes = lambda _bundle_id: ([], "")
            return core

        helper.load_verified_core_module = load_core_with_offline_owner
        helper.desktop_owner_processes = lambda _bundle_id: ([], "")
        helper.wait_for_desktop_offline = lambda *_args, **_kwargs: {
            "offline": True,
            "samples": 3,
            "stability_seconds": 0.5,
            "last_owners": [],
        }
        assert helper.run_worker(job_dir) == helper.EXIT_OK
        receipt = read_offline_receipt(job_dir)
        assert receipt["phase"] == "complete"
        assert receipt["mutation_started"] is True
        assert receipt["permanent_deletion_complete"] is True
        assert not approved_snapshot.exists()
        assert later_snapshot.read_text(encoding="utf-8") == "later\n"
        conn = sqlite3.connect(logs_path)
        try:
            assert conn.execute(
                "SELECT target FROM logs WHERE id=?",
                (later_log_id,),
            ).fetchone() == ("after-stage",)
        finally:
            conn.close()
        conn = sqlite3.connect(state_path)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM threads WHERE id=?",
                (TARGET_CLOSED_CHILD,),
            ).fetchone() == (0,)
            assert conn.execute(
                "SELECT COUNT(*) FROM threads WHERE id=?",
                (KEEP_SESSION,),
            ).fetchone() == (1,)
        finally:
            conn.close()
        assert not request_path.exists()


def exercise_offline_helper_request_hash_mismatch() -> None:
    helper = load_offline_helper_module()
    with temporary_directory() as tmp:
        job_dir, _token = create_offline_helper_test_job(
            helper,
            Path(tmp),
            job_id="0" * 32,
        )
        receipt_before = read_offline_receipt(job_dir)
        request_path = job_dir / "request.json"
        request_path.write_bytes(request_path.read_bytes() + b" ")
        assert helper.run_worker(job_dir) == 2
        receipt_after = read_offline_receipt(job_dir)
        assert not request_path.exists()
        assert receipt_after["request_sha256"] == receipt_before["request_sha256"]
        assert receipt_after["mutation_started"] is False
        assert receipt_after["phase"] == "blocked_before_mutation"
        assert receipt_after["errors"][-1]["code"] == "RequestError"


def exercise_offline_helper_source_drift() -> None:
    helper = load_offline_helper_module()
    with temporary_directory() as tmp:
        job_dir, token = create_offline_helper_test_job(
            helper,
            Path(tmp),
            job_id="9" * 32,
        )
        request_path = job_dir / "request.json"

        def injected_source_drift(_request: dict[str, object]) -> bytes:
            raise helper.SourceChangedError("injected approved-source drift")

        def unexpected_owner_check(
            _bundle_id: str,
        ) -> tuple[list[dict[str, object]], str]:
            raise AssertionError("source drift reached Desktop owner inspection")

        helper.verify_source_contract = injected_source_drift
        helper.desktop_owner_processes = unexpected_owner_check
        assert helper.run_worker(job_dir) == helper.EXIT_PLAN_CHANGED
        receipt_path = job_dir / "receipt.json"
        receipt = read_offline_receipt(job_dir)
        assert receipt["phase"] == "plan_changed"
        assert receipt["terminal"] is True
        assert receipt["mutation_started"] is False
        assert receipt["deletion_success"] is False
        assert receipt["verification_ok"] is False
        assert receipt["permanent_deletion_complete"] is False
        assert receipt["partial_possible"] is False
        assert any(error["code"] == "SourceChangedError" for error in receipt["errors"])
        assert token not in receipt_path.read_text(encoding="utf-8")
        assert not request_path.exists()


def exercise_global_state_pair_second_publish_failure() -> None:
    target_id = NEW_CHILD
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        state_path = codex_home / "state_5.sqlite"
        init_state(state_path, codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        catalog_path, _prompt_mentions = init_desktop_ui_metadata(
            codex_home,
            target_id,
        )
        global_state_paths = [
            codex_home / ".codex-global-state.json",
            codex_home / ".codex-global-state.json.bak",
        ]
        canonical_paths = [state_path, catalog_path, *global_state_paths]
        canonical_before = {path: path.read_bytes() for path in canonical_paths}

        module = load_deleter_module()
        original_desktop_owner_processes = module.desktop_owner_processes
        module.desktop_owner_processes = lambda _codex_home: ([], "")
        plan = module.make_plan(
            codex_home,
            [target_id],
            True,
            True,
            False,
        )
        assert plan.blockers == []
        assert {
            filename: len(refs) for filename, refs in plan.global_state_refs.items()
        } == {
            ".codex-global-state.json": 5,
            ".codex-global-state.json.bak": 5,
        }

        original_atomic_swap_paths = module.atomic_swap_paths
        swap_calls = 0

        def fail_second_swap(first: Path, second: Path) -> None:
            nonlocal swap_calls
            swap_calls += 1
            if swap_calls == 2:
                raise OSError("injected second global-state exchange failure")
            original_atomic_swap_paths(first, second)

        module.atomic_swap_paths = fail_second_swap
        try:
            try:
                module.apply_target_global_state(plan)
            except RuntimeError as exc:
                assert "Global state pair update failed" in str(exc)
                assert "injected second global-state exchange failure" in str(exc)
            else:
                raise AssertionError("injected second pair publish failure was ignored")
        finally:
            module.atomic_swap_paths = original_atomic_swap_paths
            module.desktop_owner_processes = original_desktop_owner_processes

        assert swap_calls == 3
        for path, before in canonical_before.items():
            assert path.read_bytes() == before
        refs, presence, _mentions, issues, _warnings = (
            module.inspect_global_state_files(
                codex_home,
                [target_id],
            )
        )
        assert issues == []
        assert presence == plan.global_state_files_present
        assert refs == plan.global_state_refs

        conn = sqlite3.connect(catalog_path)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM local_thread_catalog WHERE thread_id=?",
                (target_id,),
            ).fetchone() == (1,)
            assert conn.execute(
                "SELECT COUNT(*) FROM thread_timeline_ledger WHERE thread_id=?",
                (target_id,),
            ).fetchone() == (2,)
            assert conn.execute(
                "SELECT COUNT(*) FROM automation_runs "
                "WHERE thread_id='pending:job-1'"
            ).fetchone() == (1,)
            assert conn.execute(
                "SELECT catalog_revision FROM local_thread_catalog_metadata WHERE id=1"
            ).fetchone() == (41,)
            assert conn.execute(
                "SELECT observation_sequence FROM local_thread_catalog_sync_state "
                "WHERE host_id='local'"
            ).fetchone() == (124,)
        finally:
            conn.close()
        assert not list(codex_home.glob(".*.delete-session-*"))


def exercise_global_state_rollback_preserves_same_inode_collision() -> None:
    target_id = NEW_CHILD
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        state_path = codex_home / "state_5.sqlite"
        init_state(state_path, codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        catalog_path, _prompt_mentions = init_desktop_ui_metadata(
            codex_home,
            target_id,
        )
        main_path = codex_home / ".codex-global-state.json"
        backup_path = codex_home / ".codex-global-state.json.bak"
        canonical_before = {
            state_path: state_path.read_bytes(),
            catalog_path: catalog_path.read_bytes(),
            main_path: main_path.read_bytes(),
            backup_path: backup_path.read_bytes(),
        }

        module = load_deleter_module()
        original_desktop_owner_processes = module.desktop_owner_processes
        module.desktop_owner_processes = lambda _codex_home: ([], "")
        plan = module.make_plan(
            codex_home,
            [target_id],
            True,
            True,
            False,
        )
        assert plan.blockers == []

        original_atomic_swap_paths = module.atomic_swap_paths
        swap_calls = 0
        published_path: Path | None = None
        published_identity: tuple[int, int] | None = None
        collision_bytes = b'{"external":"same-inode-collision"}'

        def collide_before_second_swap(first: Path, second: Path) -> None:
            nonlocal published_identity, published_path, swap_calls
            swap_calls += 1
            if swap_calls == 1:
                original_atomic_swap_paths(first, second)
                published_path = second
                info = second.lstat()
                published_identity = (info.st_dev, info.st_ino)
                return
            assert swap_calls == 2
            assert published_path is not None
            assert published_identity is not None
            published_path.write_bytes(collision_bytes)
            current = published_path.lstat()
            assert (current.st_dev, current.st_ino) == published_identity
            raise OSError("injected failure after same-inode external write")

        module.atomic_swap_paths = collide_before_second_swap
        try:
            try:
                module.apply_target_global_state(plan)
            except RuntimeError as exc:
                error_text = str(exc)
                assert "Global state pair update failed" in error_text
                assert "preserved recovery files" in error_text
            else:
                raise AssertionError("same-inode publish collision was ignored")
        finally:
            module.atomic_swap_paths = original_atomic_swap_paths
            module.desktop_owner_processes = original_desktop_owner_processes

        assert swap_calls == 2
        assert published_path == backup_path
        current = backup_path.lstat()
        assert (current.st_dev, current.st_ino) == published_identity
        assert backup_path.read_bytes() == collision_bytes
        assert main_path.read_bytes() == canonical_before[main_path]
        assert state_path.read_bytes() == canonical_before[state_path]
        assert catalog_path.read_bytes() == canonical_before[catalog_path]

        recovery_paths = list(codex_home.glob(".*.delete-session-*"))
        assert len(recovery_paths) == 1
        assert recovery_paths[0].read_bytes() == canonical_before[backup_path]


def exercise_target_graph_recheck() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        module = load_deleter_module()
        plan = module.make_plan(
            codex_home,
            [TARGET_PARENT],
            True,
            False,
            False,
        )
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        with conn:
            conn.execute(
                "INSERT INTO thread_spawn_edges VALUES (?, ?, 'closed')",
                (TARGET_PARENT, NEW_CHILD),
            )
        conn.close()

        try:
            module.apply_target_index_and_state(plan)
        except RuntimeError as exc:
            assert "graph" in str(exc)
        else:
            raise AssertionError("target graph change was not rejected")

        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM threads WHERE id=?",
                    (TARGET_PARENT,),
                ).fetchone()[0]
                == 1
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM thread_spawn_edges WHERE child_thread_id=?",
                    (NEW_CHILD,),
                ).fetchone()[0]
                == 1
            )
        finally:
            conn.close()


def exercise_no_subagents_outgoing_edge_recheck() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        module = load_deleter_module()
        plan = module.make_plan(
            codex_home,
            [TARGET_CLOSED_CHILD],
            False,
            False,
            False,
        )
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        with conn:
            conn.execute(
                "INSERT INTO thread_spawn_edges VALUES (?, ?, 'closed')",
                (TARGET_CLOSED_CHILD, KEEP_SESSION),
            )
        conn.close()

        try:
            module.apply_target_index_and_state(plan)
        except RuntimeError as exc:
            assert "touching edges" in str(exc)
        else:
            raise AssertionError("new target outgoing edge was not rejected")

        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM threads WHERE id=?",
                    (TARGET_CLOSED_CHILD,),
                ).fetchone()[0]
                == 1
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM thread_spawn_edges "
                    "WHERE parent_thread_id=? AND child_thread_id=?",
                    (TARGET_CLOSED_CHILD, KEEP_SESSION),
                ).fetchone()[0]
                == 1
            )
        finally:
            conn.close()


def exercise_no_subagents_no_logs() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        report = run_cmd(
            TARGET_CLOSED_CHILD,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--no-logs",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        assert plan["target_count"] == 1
        assert plan["open_subagents"] == []
        applied = run_cmd(
            TARGET_CLOSED_CHILD,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--no-logs",
            "--apply",
            "--confirm-plan",
            scope_token(plan),
            "--json",
        )
        assert json.loads(applied.stdout)["apply_result"]["success"] is True
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM threads WHERE id=?",
                    (TARGET_PARENT,),
                ).fetchone()[0]
                == 1
            )
        finally:
            conn.close()
        conn = sqlite3.connect(codex_home / "logs_2.sqlite")
        try:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM logs WHERE thread_id=?",
                    (TARGET_CLOSED_CHILD,),
                ).fetchone()[0]
                == 1
            )
        finally:
            conn.close()


def exercise_warning_first_target_retention_continues_independent_closed() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        state_path = codex_home / "state_5.sqlite"
        init_state(state_path, codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        conn = sqlite3.connect(state_path)
        with conn:
            conn.execute(
                "DELETE FROM thread_spawn_edges WHERE parent_thread_id=?",
                (KEEP_SESSION,),
            )
        conn.close()

        report = run_cmd(
            TARGET_PARENT,
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        assert_safety_warning(plan, "Open or unknown-status session")
        applied = run_cmd(
            TARGET_PARENT,
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--apply",
            "--confirm-plan",
            scope_token(plan),
            "--json",
        )
        result = json.loads(applied.stdout)["apply_result"]
        assert result["outcome"] == "completed_with_warnings"
        assert result["success"] is True
        assert result["mutation_started"] is True
        assert result["effective_target_ids"] == [KEEP_SESSION]
        assert set(plan["target_ids"]) - {KEEP_SESSION} <= retained_session_ids(result)

        conn = sqlite3.connect(state_path)
        try:
            remaining = {row[0] for row in conn.execute("SELECT id FROM threads")}
        finally:
            conn.close()
        assert KEEP_SESSION not in remaining
        assert {
            TARGET_PARENT,
            TARGET_CLOSED_CHILD,
            TARGET_OPEN_CHILD,
            TARGET_GRANDCHILD,
        } <= remaining

    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        state_path = codex_home / "state_5.sqlite"
        init_state(state_path, codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        command_env = {"CODEX_THREAD_ID": KEEP_SESSION}

        report = run_cmd(
            KEEP_SESSION,
            TARGET_CLOSED_CHILD,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
            env=command_env,
        )
        plan = json.loads(report.stdout)["plan"]
        assert_safety_warning(plan, "current Codex session")
        applied = run_cmd(
            KEEP_SESSION,
            TARGET_CLOSED_CHILD,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--apply",
            "--confirm-plan",
            scope_token(plan),
            "--json",
            env=command_env,
        )
        result = json.loads(applied.stdout)["apply_result"]
        assert result["outcome"] == "completed_with_warnings"
        assert result["success"] is True
        assert result["mutation_started"] is True
        assert result["effective_target_ids"] == [TARGET_CLOSED_CHILD]
        assert KEEP_SESSION in retained_session_ids(result)

        conn = sqlite3.connect(state_path)
        try:
            remaining = {row[0] for row in conn.execute("SELECT id FROM threads")}
        finally:
            conn.close()
        assert KEEP_SESSION in remaining
        assert TARGET_CLOSED_CHILD not in remaining


def exercise_current_session_retained_no_safe_work() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        state_path = codex_home / "state_5.sqlite"
        init_state(state_path, codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        command_env = {"CODEX_THREAD_ID": KEEP_SESSION}
        report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
            env=command_env,
        )
        plan = json.loads(report.stdout)["plan"]
        assert plan["preflight"]["current_session_is_target"] is True
        assert_safety_warning(plan, "current Codex session")
        applied = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--apply",
            "--confirm-plan",
            scope_token(plan),
            "--json",
            env=command_env,
        )
        result = json.loads(applied.stdout)["apply_result"]
        assert result["outcome"] == "no_safe_work"
        assert result["success"] is False
        assert result["mutation_started"] is False
        assert result["effective_target_ids"] == []
        assert KEEP_SESSION in retained_session_ids(result)
        conn = sqlite3.connect(state_path)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM threads WHERE id=?", (KEEP_SESSION,)
            ).fetchone() == (1,)
        finally:
            conn.close()


def exercise_trigger_blocker() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        with conn:
            conn.execute(
                """
                CREATE TRIGGER wipe_unapproved_threads
                AFTER DELETE ON threads
                BEGIN
                    DELETE FROM threads WHERE id != OLD.id;
                END
                """
            )
        before_count = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        conn.close()

        report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        assert_safety_warning(plan, "trigger")
        applied = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--apply",
            "--confirm-plan",
            scope_token(plan),
            "--json",
        )
        assert_controlled_warning_first_result(
            json.loads(applied.stdout)["apply_result"]
        )
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            assert (
                conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
                == before_count
            )
        finally:
            conn.close()


def exercise_alternate_database_blocker() -> None:
    module = load_deleter_module()
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        state_path = codex_home / "state_6.sqlite"
        logs_path = codex_home / "logs_3.sqlite"
        init_state(state_path, codex_home)
        init_logs(logs_path)

        plan = module.make_plan(
            codex_home,
            [KEEP_SESSION],
            False,
            True,
            False,
        )
        rendered = module.plan_to_dict(plan)
        assert rendered["state_database"]["path"] == str(state_path)
        assert rendered["logs_database"]["path"] == str(logs_path)
        assert plan.component_plans["state_and_index"]["status"] == "enabled"
        assert plan.component_plans["logs"]["status"] == "enabled"

        original_desktop_owner_processes = module.desktop_owner_processes
        module.desktop_owner_processes = lambda _codex_home: ([], "")
        try:
            result = module.apply_plan(
                plan,
                plan.historical_residuals,
                True,
                False,
                False,
                False,
                "targets_only",
            )
        finally:
            module.desktop_owner_processes = original_desktop_owner_processes
        assert result["success"] is True
        assert result["verification"]["verification_ok"] is True
        assert not (codex_home / "state_5.sqlite").exists()
        assert not (codex_home / "logs_2.sqlite").exists()

    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        conn = sqlite3.connect(codex_home / "state_6.sqlite")
        conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()

        report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        discovery = plan["preflight"]["state_database_discovery"]
        assert discovery["ambiguous"] is True
        assert len(discovery["candidates"]) == 2
        assert plan["component_plans"]["state_and_index"]["status"] == "skipped"
        assert plan["component_plans"]["historical"]["status"] == "skipped"
        assert plan["component_plans"]["logs"]["status"] == "enabled"
        assert any(
            warning["code"] == "state_database_discovery_failed"
            for warning in plan["safety_warnings"]
        )


def exercise_warning_first_component_skip_continues() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        index = codex_home / "session_index.jsonl"
        index.write_bytes(b"\xff\xfe\n")

        report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        assert plan["preflight"]["session_index_issues"]
        warning = assert_safety_warning(plan, "UTF-8")
        assert warning["component"] == "state_and_index"
        applied = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--apply",
            "--confirm-plan",
            scope_token(plan),
            "--json",
        )
        result = json.loads(applied.stdout)["apply_result"]
        assert result["outcome"] == "completed_with_warnings"
        assert result["success"] is True
        assert result["mutation_started"] is True
        assert result["component_results"]["state_and_index"]["status"] == (
            "skipped_safely"
        )
        assert result["component_results"]["logs"]["status"] == "completed"
        assert result["component_results"]["rollout_artifacts"]["status"] == (
            "completed"
        )
        assert "Traceback" not in applied.stderr
        assert index.read_bytes() == b"\xff\xfe\n"
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM threads WHERE id=?", (KEEP_SESSION,)
            ).fetchone() == (1,)
        finally:
            conn.close()
        conn = sqlite3.connect(codex_home / "logs_2.sqlite")
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM logs WHERE thread_id=?", (KEEP_SESSION,)
            ).fetchone() == (0,)
        finally:
            conn.close()
        assert not any(
            KEEP_SESSION in path.name
            for path in (codex_home / "sessions").rglob("*.jsonl")
        )


def exercise_partial_result_survives_invalid_utf8() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        module = load_deleter_module()
        plan = module.make_plan(
            codex_home,
            [TARGET_CLOSED_CHILD],
            False,
            True,
            True,
        )
        (codex_home / "session_index.jsonl").write_bytes(b"\xff\xfe\n")

        partial = module.build_partial_apply_result(
            plan,
            True,
            True,
            False,
        )
        assert partial["success"] is False
        assert partial["verification"]["verification_ok"] is False
        assert STABLE_VERIFICATION_FIELDS <= set(partial["verification"])
        assert any(
            "Unable to inspect target post-state" in error
            for error in partial["verification"]["verification_errors"]
        )
        assert partial["historical_residuals"]["scanned"] is False


def exercise_malformed_json_index_blocker() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        (codex_home / "session_index.jsonl").write_text('{"id":\n', encoding="utf-8")

        report = run_cmd(
            TARGET_PARENT,
            "--codex-home",
            str(codex_home),
            "--json",
        )
        payload = json.loads(report.stdout)
        assert_safety_warning(payload["plan"], "session_index.jsonl")


def exercise_sqlite_hardlink_blocker() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        state_path = codex_home / "state_5.sqlite"
        init_state(state_path, codex_home)
        external_link = Path(tmp) / "state-hardlink.sqlite"
        os.link(state_path, external_link)

        report = run_cmd(
            TARGET_PARENT,
            "--codex-home",
            str(codex_home),
            "--json",
        )
        payload = json.loads(report.stdout)
        assert_safety_warning(payload["plan"], "multiple hard links")
        assert external_link.exists()


def exercise_rollout_path_directory_blocker() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        directory_path = (
            codex_home
            / "sessions"
            / "2026"
            / "05"
            / "03"
            / f"directory-{TARGET_PARENT}.jsonl"
        )
        directory_path.mkdir()
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        with conn:
            conn.execute(
                "UPDATE threads SET rollout_path=? WHERE id=?",
                (str(directory_path), TARGET_PARENT),
            )
        conn.close()

        report = run_cmd(
            TARGET_PARENT,
            "--codex-home",
            str(codex_home),
            "--json",
        )
        payload = json.loads(report.stdout)
        assert_safety_warning(payload["plan"], "rollout_path")


def exercise_open_missing_rollout_scope() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        with conn:
            conn.execute(
                "INSERT INTO thread_spawn_edges VALUES (?, ?, 'open')",
                (KEEP_SESSION, OLD_MISSING_ROLLOUT),
            )
        conn.close()

        report = run_cmd(
            TARGET_CLOSED_CHILD,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        missing_entries = plan["historical_residuals"][
            "state_threads_missing_rollout_file"
        ]
        missing_entry = next(
            entry for entry in missing_entries if entry["id"] == OLD_MISSING_ROLLOUT
        )
        assert missing_entry["open_or_unknown"] is True
        assert (
            OLD_MISSING_ROLLOUT in plan["scope_safety"]["missing_rollout_open_threads"]
        )
        assert_safety_warning(plan, "Open or unknown-status session")
        applied = run_cmd(
            TARGET_CLOSED_CHILD,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--apply",
            "--apply-historical-residuals",
            "--apply-missing-rollout-threads",
            "--confirm-plan",
            scope_token(plan, historical=True, missing_rollout=True),
            "--json",
        )
        result = json.loads(applied.stdout)["apply_result"]
        assert result["outcome"] == "completed_with_warnings"
        assert result["success"] is True
        assert OLD_MISSING_ROLLOUT in retained_session_ids(result)
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM threads WHERE id=?",
                (OLD_MISSING_ROLLOUT,),
            ).fetchone() == (1,)
        finally:
            conn.close()


def exercise_current_missing_rollout_blocker() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        command_env = {"CODEX_THREAD_ID": OLD_MISSING_ROLLOUT}
        report = run_cmd(
            TARGET_CLOSED_CHILD,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
            env=command_env,
        )
        plan = json.loads(report.stdout)["plan"]
        assert plan["scope_safety"]["missing_rollout_current_sessions"] == [
            OLD_MISSING_ROLLOUT
        ]
        assert_safety_warning(plan, "current Codex session")
        assert any(
            key.startswith("targets_historical_and_missing_rollout_threads")
            for key in plan["approval_tokens"]
        )
        run_cmd(
            TARGET_CLOSED_CHILD,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--apply",
            "--apply-historical-residuals",
            "--apply-missing-rollout-threads",
            "--confirm-plan",
            scope_token(plan),
            "--json",
            expect=3,
            env=command_env,
        )
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM threads WHERE id=?",
                    (OLD_MISSING_ROLLOUT,),
                ).fetchone()[0]
                == 1
            )
        finally:
            conn.close()


def exercise_rollout_owner_mismatch_blocker() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        parent_rollout = (
            codex_home
            / "sessions"
            / "2026"
            / "05"
            / "03"
            / f"rollout-{TARGET_PARENT}.jsonl"
        )
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        with conn:
            conn.execute(
                "UPDATE threads SET rollout_path=? WHERE id=?",
                (str(parent_rollout), KEEP_SESSION),
            )
        conn.close()

        report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        assert_safety_warning(plan, "artifact paths")
        applied = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--apply",
            "--confirm-plan",
            scope_token(plan),
            "--json",
        )
        assert_controlled_warning_first_result(
            json.loads(applied.stdout)["apply_result"]
        )
        assert parent_rollout.exists()


def exercise_multiple_incoming_edges() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        with conn:
            conn.executescript(
                """
                ALTER TABLE thread_spawn_edges RENAME TO old_spawn_edges;
                CREATE TABLE thread_spawn_edges (
                    parent_thread_id TEXT NOT NULL,
                    child_thread_id TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                INSERT INTO thread_spawn_edges SELECT * FROM old_spawn_edges;
                DROP TABLE old_spawn_edges;
                """
            )
            conn.execute(
                "INSERT INTO thread_spawn_edges VALUES (?, ?, 'completed')",
                (KEEP_SESSION, TARGET_OPEN_CHILD),
            )
        conn.close()

        report = run_cmd(
            TARGET_OPEN_CHILD,
            "--codex-home",
            str(codex_home),
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        assert plan["open_subagents"] == [TARGET_OPEN_CHILD]
        assert len(plan["target_incoming_edges"][TARGET_OPEN_CHILD]) == 2


def exercise_special_character_codex_home() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / "special ?#%25" / ".codex"
        codex_home.mkdir(parents=True)
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        assert plan["blockers"] == []
        assert plan["counts"]["state_threads"] == 1


def exercise_historical_revive_race() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        old_rollout = (
            codex_home
            / "sessions"
            / "2026"
            / "05"
            / "01"
            / f"rollout-{OLD_FILE_ONLY}.jsonl"
        )
        old_rollout.parent.mkdir(parents=True, exist_ok=True)
        old_rollout.write_text("{}\n", encoding="utf-8")
        module = load_deleter_module()
        plan = module.make_plan(
            codex_home,
            [KEEP_SESSION],
            False,
            False,
            True,
        )
        original_scan = module.scan_historical_residuals
        injected = False

        def racing_scan(*args: object, **kwargs: object) -> dict[str, object]:
            nonlocal injected
            result = original_scan(*args, **kwargs)
            if not injected:
                injected = True
                conn = sqlite3.connect(codex_home / "state_5.sqlite")
                with conn:
                    conn.execute(
                        """
                        INSERT INTO threads
                        (id, rollout_path, created_at, updated_at, source, model_provider,
                         cwd, title, sandbox_policy, approval_mode, created_at_ms,
                         updated_at_ms)
                        VALUES (?, ?, 1, 1, 'test', 'openai', '/tmp', 'revived',
                                '{}', 'never', 1000, 1000)
                        """,
                        (OLD_FILE_ONLY, str(old_rollout)),
                    )
                conn.close()
            return result

        setattr(module, "scan_historical_residuals", racing_scan)
        try:
            module.cleanup_historical_residuals(
                codex_home,
                plan.historical_residuals,
                False,
                {KEEP_SESSION},
                False,
                True,
                False,
            )
        except RuntimeError as exc:
            assert "became live" in str(exc)
        else:
            raise AssertionError("revived historical session was not rejected")
        assert old_rollout.exists()
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM threads WHERE id=?",
                    (OLD_FILE_ONLY,),
                ).fetchone()[0]
                == 1
            )
        finally:
            conn.close()


def exercise_missing_rollout_outgoing_edge_recheck() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        module = load_deleter_module()
        plan = module.make_plan(
            codex_home,
            [TARGET_CLOSED_CHILD],
            False,
            False,
            True,
        )
        missing_entries = plan.historical_residuals[
            "state_threads_missing_rollout_file"
        ]
        missing_entry = next(
            entry for entry in missing_entries if entry["id"] == OLD_MISSING_ROLLOUT
        )
        assert missing_entry["touching_edges"] == []

        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        with conn:
            conn.execute(
                "INSERT INTO thread_spawn_edges VALUES (?, ?, 'open')",
                (OLD_MISSING_ROLLOUT, KEEP_SESSION),
            )
        conn.close()

        try:
            module.cleanup_historical_residuals(
                codex_home,
                plan.historical_residuals,
                False,
                {TARGET_CLOSED_CHILD},
                True,
                True,
                False,
            )
        except RuntimeError as exc:
            assert "scope changed" in str(exc)
        else:
            raise AssertionError("new dangling-thread outgoing edge was not rejected")

        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM threads WHERE id=?",
                    (OLD_MISSING_ROLLOUT,),
                ).fetchone()[0]
                == 1
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM thread_spawn_edges "
                    "WHERE parent_thread_id=? AND child_thread_id=?",
                    (OLD_MISSING_ROLLOUT, KEEP_SESSION),
                ).fetchone()[0]
                == 1
            )
        finally:
            conn.close()


def exercise_historical_logs_scope_race() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        module = load_deleter_module()
        plan = module.make_plan(
            codex_home,
            [KEEP_SESSION],
            False,
            True,
            True,
        )
        original_scan = module.scan_historical_residuals
        injected = False

        def racing_scan(*args: object, **kwargs: object) -> dict[str, object]:
            nonlocal injected
            result = original_scan(*args, **kwargs)
            if not injected:
                injected = True
                conn = sqlite3.connect(codex_home / "logs_2.sqlite")
                with conn:
                    conn.execute(
                        "INSERT INTO logs "
                        "(ts, ts_nanos, level, target, thread_id) "
                        "VALUES (1, 0, 'INFO', 'race', ?)",
                        (NEW_CHILD,),
                    )
                conn.close()
            return result

        setattr(module, "scan_historical_residuals", racing_scan)
        result = module.cleanup_historical_residuals(
            codex_home,
            plan.historical_residuals,
            True,
            {KEEP_SESSION},
            False,
            True,
            True,
        )
        assert result["verification"]["cleanup_ok"] is True

        conn = sqlite3.connect(codex_home / "logs_2.sqlite")
        try:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM logs WHERE thread_id=?",
                    (NEW_CHILD,),
                ).fetchone()[0]
                == 1
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM logs WHERE thread_id=?",
                    (OLD_FILE_ONLY,),
                ).fetchone()[0]
                == 0
            )
        finally:
            conn.close()
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM threads WHERE id=?",
                    (KEEP_SESSION,),
                ).fetchone()[0]
                == 1
            )
        finally:
            conn.close()


def exercise_no_historical_scan_apply() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--no-historical-scan",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        assert plan["historical_residuals"]["scanned"] is False
        applied = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--no-historical-scan",
            "--apply",
            "--confirm-plan",
            scope_token(plan),
            "--json",
        )
        apply_result = json.loads(applied.stdout)["apply_result"]
        assert apply_result["success"] is True
        assert apply_result["historical_scan_ok"] is True
        assert apply_result["historical_residuals"]["scanned"] is False


def exercise_rollout_migration_schema_support() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        state_path = codex_home / "state_5.sqlite"
        init_state(state_path, codex_home)
        init_rollout_migration_tables(state_path)
        init_logs(codex_home / "logs_2.sqlite")

        conn = sqlite3.connect(state_path)
        with conn:
            target_rollout = conn.execute(
                "SELECT rollout_path FROM threads WHERE id=?", (KEEP_SESSION,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO rollout_migration_state VALUES (?, ?, ?, ?)",
                ("startup", 1000, KEEP_SESSION, 2000),
            )
            conn.executemany(
                "INSERT INTO rollout_migration_skipped_rollouts VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("startup", target_rollout, 10, 20, "target", 30),
                    ("startup", "2026/05/03/unrelated.jsonl", 11, 21, "keep", 31),
                ],
            )
        cursor_before = conn.execute(
            "SELECT * FROM rollout_migration_state ORDER BY migration_id"
        ).fetchall()
        skipped_before = conn.execute(
            "SELECT * FROM rollout_migration_skipped_rollouts "
            "ORDER BY migration_id, rollout_path"
        ).fetchall()
        expected_skipped_after = [
            row for row in skipped_before if row[1] != target_rollout
        ]
        conn.close()

        report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--no-historical-scan",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        assert plan["blockers"] == []
        assert plan["preflight"]["state_schema_issues"] == []
        assert plan["preflight"]["state_reference_issues"] == []
        assert plan["counts"]["state_rollout_migration_skipped_rollouts"] == 1
        assert len(plan["rollout_migration_skipped_rows"]) == 1

        applied = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--no-historical-scan",
            "--apply",
            "--confirm-plan",
            scope_token(plan),
            "--json",
        )
        assert json.loads(applied.stdout)["apply_result"]["success"] is True

        conn = sqlite3.connect(state_path)
        try:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM threads WHERE id=?", (KEEP_SESSION,)
                ).fetchone()[0]
                == 0
            )
            assert (
                conn.execute(
                    "SELECT * FROM rollout_migration_state ORDER BY migration_id"
                ).fetchall()
                == cursor_before
            )
            assert (
                conn.execute(
                    "SELECT * FROM rollout_migration_skipped_rollouts "
                    "ORDER BY migration_id, rollout_path"
                ).fetchall()
                == expected_skipped_after
            )
        finally:
            conn.close()


def exercise_rollout_migration_cursor_blockers() -> None:
    cases = [
        (1000, KEEP_SESSION.upper(), "non-canonical"),
        (1000, None, "incomplete last-checked"),
    ]
    for created_at, thread_id, expected_issue in cases:
        with temporary_directory() as tmp:
            codex_home = Path(tmp) / ".codex"
            codex_home.mkdir()
            state_path = codex_home / "state_5.sqlite"
            init_state(state_path, codex_home)
            init_rollout_migration_tables(state_path)
            init_logs(codex_home / "logs_2.sqlite")
            conn = sqlite3.connect(state_path)
            with conn:
                conn.execute(
                    "INSERT INTO rollout_migration_state VALUES (?, ?, ?, ?)",
                    ("startup", created_at, thread_id, 2000),
                )
            conn.close()

            report = run_cmd(
                KEEP_SESSION,
                "--codex-home",
                str(codex_home),
                "--no-subagents",
                "--no-historical-scan",
                "--json",
            )
            plan = json.loads(report.stdout)["plan"]
            assert_safety_warning(plan, expected_issue)
            applied = run_cmd(
                KEEP_SESSION,
                "--codex-home",
                str(codex_home),
                "--no-subagents",
                "--no-historical-scan",
                "--apply",
                "--confirm-plan",
                scope_token(plan),
                "--json",
            )
            assert_controlled_warning_first_result(
                json.loads(applied.stdout)["apply_result"]
            )


def exercise_rollout_migration_schema_drift_blocker() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        state_path = codex_home / "state_5.sqlite"
        init_state(state_path, codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        conn = sqlite3.connect(state_path)
        with conn:
            conn.executescript(
                """
                CREATE TABLE rollout_migration_state (
                    migration_id TEXT PRIMARY KEY,
                    last_checked_thread_created_at INTEGER,
                    last_checked_thread_id TEXT
                );
                CREATE TABLE rollout_migration_skipped_rollouts (
                    migration_id TEXT NOT NULL,
                    rollout_path TEXT NOT NULL,
                    rollout_size_bytes INTEGER NOT NULL,
                    rollout_modified_at_ns INTEGER NOT NULL,
                    skipped_at INTEGER NOT NULL,
                    PRIMARY KEY (migration_id, rollout_path)
                );
                """
            )
        conn.close()

        report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--no-historical-scan",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        schema_issues = plan["preflight"]["state_schema_issues"]
        assert any("updated_at" in issue for issue in schema_issues)
        assert any("skip_reason" in issue for issue in schema_issues)


def exercise_rollout_migration_future_reference_blocker() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        state_path = codex_home / "state_5.sqlite"
        init_state(state_path, codex_home)
        init_rollout_migration_tables(state_path)
        init_logs(codex_home / "logs_2.sqlite")
        conn = sqlite3.connect(state_path)
        with conn:
            conn.execute(
                "ALTER TABLE rollout_migration_state ADD COLUMN future_thread_id TEXT"
            )
        conn.close()

        report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--no-historical-scan",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        assert any(
            "rollout_migration_state" in issue and "future_thread_id" in issue
            for issue in plan["preflight"]["state_schema_issues"]
        )


def exercise_rollout_migration_cursor_race_is_preserved() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        state_path = codex_home / "state_5.sqlite"
        init_state(state_path, codex_home)
        init_rollout_migration_tables(state_path)
        init_logs(codex_home / "logs_2.sqlite")
        conn = sqlite3.connect(state_path)
        with conn:
            conn.execute(
                "INSERT INTO rollout_migration_state VALUES (?, ?, ?, ?)",
                ("startup", 1000, KEEP_SESSION, 2000),
            )
        conn.close()

        report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--no-historical-scan",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        conn = sqlite3.connect(state_path)
        with conn:
            conn.execute("UPDATE rollout_migration_state SET updated_at=updated_at+1")
        conn.close()

        applied = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--no-historical-scan",
            "--apply",
            "--confirm-plan",
            scope_token(plan),
            "--json",
        )
        result = json.loads(applied.stdout)["apply_result"]
        assert result["outcome"] in {"completed", "completed_with_warnings"}
        assert result["success"] is True
        assert result["mutation_started"] is True
        conn = sqlite3.connect(state_path)
        try:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM threads WHERE id=?",
                    (KEEP_SESSION,),
                ).fetchone()[0]
                == 0
            )
            assert conn.execute(
                "SELECT updated_at FROM rollout_migration_state"
            ).fetchone() == (2001,)
        finally:
            conn.close()


def exercise_rollout_migration_missing_thread_cleanup() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        state_path = codex_home / "state_5.sqlite"
        init_state(state_path, codex_home)
        init_rollout_migration_tables(state_path)
        init_logs(codex_home / "logs_2.sqlite")
        conn = sqlite3.connect(state_path)
        with conn:
            missing_rollout = conn.execute(
                "SELECT rollout_path FROM threads WHERE id=?",
                (OLD_MISSING_ROLLOUT,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO rollout_migration_state VALUES (?, ?, ?, ?)",
                ("startup", 1000, KEEP_SESSION, 2000),
            )
            conn.execute(
                "INSERT INTO rollout_migration_skipped_rollouts "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("startup", missing_rollout, 10, 20, "missing", 30),
            )
        cursor_before = conn.execute("SELECT * FROM rollout_migration_state").fetchall()
        conn.close()

        report = run_cmd(
            TARGET_CLOSED_CHILD,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        missing_entry = next(
            entry
            for entry in plan["historical_residuals"][
                "state_threads_missing_rollout_file"
            ]
            if entry["id"] == OLD_MISSING_ROLLOUT
        )
        assert len(missing_entry["rollout_migration_skipped_rows"]) == 1
        applied = run_cmd(
            TARGET_CLOSED_CHILD,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--apply",
            "--apply-historical-residuals",
            "--apply-missing-rollout-threads",
            "--confirm-plan",
            scope_token(plan, historical=True, missing_rollout=True),
            "--json",
        )
        result = json.loads(applied.stdout)["apply_result"]
        assert result["success"] is True
        assert (
            result["historical_cleanup"]["state_deleted"][
                "rollout_migration_skipped_rollouts"
            ]
            == 1
        )
        conn = sqlite3.connect(state_path)
        try:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM rollout_migration_skipped_rollouts"
                ).fetchone()[0]
                == 0
            )
            assert (
                conn.execute("SELECT * FROM rollout_migration_state").fetchall()
                == cursor_before
            )
        finally:
            conn.close()


def exercise_rollout_migration_target_path_blocker() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        state_path = codex_home / "state_5.sqlite"
        init_state(state_path, codex_home)
        init_rollout_migration_tables(state_path)
        init_logs(codex_home / "logs_2.sqlite")
        conn = sqlite3.connect(state_path)
        with conn:
            target_rollout = conn.execute(
                "SELECT rollout_path FROM threads WHERE id=?",
                (KEEP_SESSION,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO rollout_migration_skipped_rollouts "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("startup", target_rollout + ".retry", 10, 20, "ambiguous", 30),
            )
        conn.close()

        report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        assert_safety_warning(plan, "target-like path")


def exercise_paginated_history_support_and_guards() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        state_path = codex_home / "state_5.sqlite"
        init_state(state_path, codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        history_path = codex_home / "thread_history_1.sqlite"
        init_paginated_history(history_path, [KEEP_SESSION, TARGET_PARENT])
        conn = sqlite3.connect(state_path)
        with conn:
            conn.execute(
                "ALTER TABLE threads ADD COLUMN history_mode TEXT NOT NULL "
                "DEFAULT 'legacy'"
            )
            conn.execute(
                "UPDATE threads SET history_mode='paginated' WHERE id=?",
                (KEEP_SESSION,),
            )
        conn.close()
        conn = sqlite3.connect(history_path)
        with conn:
            conn.execute(
                "ALTER TABLE thread_items ADD COLUMN additive_payload TEXT"
            )
            conn.execute(
                "CREATE TABLE future_metadata (id INTEGER PRIMARY KEY, note TEXT)"
            )
            conn.execute(
                "INSERT INTO future_metadata (note) VALUES ('unrelated extension')"
            )
        conn.close()

        module = load_deleter_module()
        module.desktop_owner_processes = lambda _codex_home: ([], "")
        wal_conn = sqlite3.connect(history_path)
        assert wal_conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        wal_conn.execute("PRAGMA wal_autocheckpoint=0")
        wal_conn.execute(
            "UPDATE future_metadata SET note=note WHERE id=1"
        )
        wal_conn.commit()
        report_plan = module.make_plan(
            codex_home,
            [KEEP_SESSION],
            False,
            True,
            True,
        )
        scope_before = module.approval_scope_fingerprint(
            report_plan,
            False,
            False,
            False,
        )
        shm_path = Path(str(history_path) + "-shm")
        assert shm_path.is_file()
        shm_stat = shm_path.stat()
        os.utime(
            shm_path,
            ns=(shm_stat.st_atime_ns, shm_stat.st_mtime_ns + 1_000_000),
        )
        touched_sidecar_plan = module.make_plan(
            codex_home,
            [KEEP_SESSION],
            False,
            True,
            True,
        )
        assert module.approval_scope_fingerprint(
            touched_sidecar_plan,
            False,
            False,
            False,
        ) == scope_before
        preserved_contract = touched_sidecar_plan.paginated_history_database_plan[
            "preserved_contract"
        ]
        assert preserved_contract["database_identity"]["path"] == str(history_path)
        assert "path_identities" not in preserved_contract
        wal_conn.close()
        module_plan = module.make_plan(
            codex_home,
            [KEEP_SESSION],
            False,
            True,
            True,
        )
        assert module.approval_scope_fingerprint(
            module_plan,
            False,
            False,
            False,
        ) == scope_before
        plan = module.plan_to_dict(module_plan)
        assert plan["target_dispositions"][KEEP_SESSION]["status"] == "eligible"
        assert plan["component_plans"]["paginated_history"]["status"] == "enabled"
        assert plan["counts"]["paginated_history_projection_rows"] == 1
        assert plan["counts"]["paginated_history_turn_rows"] == 1
        assert plan["counts"]["paginated_history_item_rows"] == 2
        assert plan["historical_residuals"]["authoritative"] is True
        compatibility = plan["preflight"][
            "paginated_history_schema_compatibility"
        ]
        assert compatibility["unknown_tables"] == ["future_metadata"]
        assert compatibility["target_reference_hits"] == []

        result = module.apply_plan(
            module_plan,
            module_plan.historical_residuals,
            True,
            True,
            False,
            False,
            "targets_only",
        )
        assert result["success"] is True
        assert result["verification"]["verification_ok"] is True
        assert result["paginated_history_cleanup"]["rows_removed"] == {
            "thread_history_projection_state": 1,
            "thread_items": 2,
            "thread_turns": 1,
        }
        conn = sqlite3.connect(history_path)
        try:
            for table in [
                "thread_history_projection_state",
                "thread_turns",
                "thread_items",
            ]:
                assert conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE thread_id=?",
                    (KEEP_SESSION,),
                ).fetchone() == (0,)
                assert conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE thread_id=?",
                    (TARGET_PARENT,),
                ).fetchone()[0] > 0
            assert conn.execute(
                "SELECT note FROM future_metadata"
            ).fetchall() == [("unrelated extension",)]
        finally:
            conn.close()

    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        state_path = codex_home / "state_5.sqlite"
        init_state(state_path, codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        history_path = codex_home / "thread_history_1.sqlite"
        init_paginated_history(history_path, [KEEP_SESSION])
        conn = sqlite3.connect(state_path)
        with conn:
            conn.execute(
                "ALTER TABLE threads ADD COLUMN history_mode TEXT NOT NULL "
                "DEFAULT 'legacy'"
            )
            conn.execute(
                "UPDATE threads SET history_mode='paginated' WHERE id=?",
                (KEEP_SESSION,),
            )
        conn.close()
        conn = sqlite3.connect(history_path)
        with conn:
            conn.execute(
                "CREATE TABLE future_history (thread_id TEXT PRIMARY KEY)"
            )
            conn.execute(
                "INSERT INTO future_history VALUES (?)",
                (KEEP_SESSION,),
            )
        conn.close()
        report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        assert (
            plan["component_plans"]["paginated_history"]["status"]
            == "skipped"
        )
        assert plan["target_dispositions"][KEEP_SESSION]["status"] == "retained"
        assert_safety_warning(plan, "future_history.thread_id")

    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        state_path = codex_home / "state_5.sqlite"
        init_state(state_path, codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        history_path = codex_home / "thread_history_1.sqlite"
        init_paginated_history(history_path, [KEEP_SESSION])
        conn = sqlite3.connect(state_path)
        with conn:
            conn.execute(
                "ALTER TABLE threads ADD COLUMN history_mode TEXT NOT NULL "
                "DEFAULT 'legacy'"
            )
            conn.execute(
                "UPDATE threads SET history_mode='paginated' WHERE id=?",
                (KEEP_SESSION,),
            )
        conn.close()
        module = load_deleter_module()
        module.desktop_owner_processes = lambda _codex_home: ([], "")
        module_plan = module.make_plan(
            codex_home,
            [KEEP_SESSION],
            False,
            True,
            False,
        )
        frozen_snapshot = module.approval_execution_snapshot(
            module_plan,
            False,
        )
        approved_scope_fingerprint = module.approval_scope_fingerprint(
            module_plan,
            False,
            False,
            False,
        )
        conn = sqlite3.connect(history_path)
        with conn:
            conn.execute(
                "UPDATE thread_items SET item_json='{}' "
                "WHERE thread_id=? AND item_id='item-1-1'",
                (KEEP_SESSION,),
            )
        conn.close()
        before = snapshot_managed_bytes(codex_home)
        current_plan = module.make_plan(
            codex_home,
            [KEEP_SESSION],
            False,
            True,
            False,
        )
        assert module.approval_scope_fingerprint(
            current_plan,
            False,
            False,
            False,
        ) != approved_scope_fingerprint
        narrowed, warnings, retained = module.narrow_plan_for_execution(
            current_plan,
            frozen_snapshot,
            False,
        )
        assert narrowed.target_ids == []
        assert any(
            warning.get("code") == "paginated_target_dependency_changed"
            and warning.get("affected_ids") == [KEEP_SESSION]
            for warning in warnings
        )
        assert {
            item.get("component")
            for item in retained
            if item.get("session_id") == KEEP_SESSION
            and item.get("status") == "retained_paginated_dependency_changed"
        } >= {
            "state_and_index",
            "logs",
            "paginated_history",
        }
        narrowed_result = module.apply_plan(
            current_plan,
            module_plan.historical_residuals,
            True,
            False,
            False,
            False,
            "targets_only",
            execution_snapshot=frozen_snapshot,
        )
        assert narrowed_result["outcome"] == "no_safe_work"
        assert narrowed_result["mutation_started"] is False
        assert snapshot_managed_bytes(codex_home) == before
        try:
            module.apply_plan(
                module_plan,
                module_plan.historical_residuals,
                True,
                False,
                False,
                False,
                "targets_only",
            )
        except RuntimeError as exc:
            assert "paginated_history prewrite contract failed" in str(exc)
        else:
            raise AssertionError("stale paginated history contract was applied")
        assert snapshot_managed_bytes(codex_home) == before


def exercise_historical_target_edge_contract() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        state_path = codex_home / "state_5.sqlite"
        init_state(state_path, codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        conn = sqlite3.connect(state_path)
        with conn:
            conn.execute(
                "INSERT INTO thread_spawn_edges VALUES (?, ?, 'closed')",
                (NEW_CHILD, KEEP_SESSION),
            )
        conn.close()

        report = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        applied = run_cmd(
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--apply",
            "--apply-historical-residuals",
            "--confirm-plan",
            scope_token(plan, historical=True),
            "--json",
        )
        result = json.loads(applied.stdout)["apply_result"]
        assert result["success"] is True
        assert set(result["historical_cleanup"]["state_cleanup_ids"]) >= {
            OLD_ORPHAN_REF,
            NEW_CHILD,
        }
        conn = sqlite3.connect(state_path)
        try:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM threads WHERE id=?",
                    (KEEP_SESSION,),
                ).fetchone()[0]
                == 0
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM thread_spawn_edges "
                    "WHERE parent_thread_id=? OR child_thread_id=?",
                    (KEEP_SESSION, KEEP_SESSION),
                ).fetchone()[0]
                == 0
            )
        finally:
            conn.close()


def exercise_late_artifact_and_index_additions() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        module = load_deleter_module()

        generated_root = codex_home / "generated_images"
        generated_root.mkdir()
        approved_directory = generated_root / OLD_FILE_ONLY
        approved_directory.mkdir()
        approved_leaf = approved_directory / "approved.txt"
        approved_leaf.write_text("approved\n", encoding="utf-8")
        directory_contract = module.path_contract_entry(approved_directory)
        original_rename = module.os.rename
        injected_directory_child = False

        def racing_directory_rename(source: object, destination: object) -> None:
            nonlocal injected_directory_child
            if Path(source) == approved_directory and not injected_directory_child:
                injected_directory_child = True
                (approved_directory / "late.txt").write_text("late\n", encoding="utf-8")
            original_rename(source, destination)

        setattr(module.os, "rename", racing_directory_rename)
        directory_status: dict[str, list[str]] = {}
        try:
            module.remove_paths(
                codex_home,
                [approved_directory],
                [generated_root],
                {str(approved_directory): directory_contract},
                directory_status,
            )
        finally:
            setattr(module.os, "rename", original_rename)
        assert not approved_leaf.exists()
        assert (approved_directory / "late.txt").read_text(encoding="utf-8") == "late\n"
        assert directory_status["retained_with_late_children"] == [
            str(approved_directory)
        ]

        snapshots_root = codex_home / "shell_snapshots"
        snapshots_root.mkdir()
        approved_file = snapshots_root / f"{OLD_FILE_ONLY}.approved.sh"
        approved_file.write_text("approved\n", encoding="utf-8")
        file_contract = module.path_contract_entry(approved_file)
        injected_replacement = False

        def racing_file_rename(source: object, destination: object) -> None:
            nonlocal injected_replacement
            if Path(source) == approved_file and not injected_replacement:
                injected_replacement = True
                approved_file.write_text("replacement\n", encoding="utf-8")
            original_rename(source, destination)

        setattr(module.os, "rename", racing_file_rename)
        file_status: dict[str, list[str]] = {}
        try:
            module.remove_paths(
                codex_home,
                [approved_file],
                [snapshots_root],
                {str(approved_file): file_contract},
                file_status,
            )
        finally:
            setattr(module.os, "rename", original_rename)
        assert approved_file.read_text(encoding="utf-8") == "replacement\n"
        assert file_status["identity_changed_retained"] == [str(approved_file)]

        collision_file = snapshots_root / f"{OLD_FILE_ONLY}.collision.sh"
        collision_file.write_text("approved-collision\n", encoding="utf-8")
        collision_contract = module.path_contract_entry(collision_file)
        original_atomic_restore = module.atomic_rename_noreplace
        injected_collision_change = False
        injected_competing_object = False

        def racing_collision_rename(source: object, destination: object) -> None:
            nonlocal injected_collision_change
            if Path(source) == collision_file and not injected_collision_change:
                injected_collision_change = True
                collision_file.write_text("changed-in-window\n", encoding="utf-8")
            original_rename(source, destination)

        def racing_atomic_restore(source: Path, destination: Path) -> None:
            nonlocal injected_competing_object
            if destination == collision_file and not injected_competing_object:
                injected_competing_object = True
                collision_file.write_text("late-new-object\n", encoding="utf-8")
            original_atomic_restore(source, destination)

        setattr(module.os, "rename", racing_collision_rename)
        setattr(module, "atomic_rename_noreplace", racing_atomic_restore)
        try:
            try:
                module.remove_paths(
                    codex_home,
                    [collision_file],
                    [snapshots_root],
                    {str(collision_file): collision_contract},
                    {},
                )
            except RuntimeError as exc:
                assert "both the new path" in str(exc)
            else:
                raise AssertionError("competing restore path was overwritten")
        finally:
            setattr(module.os, "rename", original_rename)
            setattr(module, "atomic_rename_noreplace", original_atomic_restore)
        assert collision_file.read_text(encoding="utf-8") == "late-new-object\n"
        quarantined_collision_files = list(
            snapshots_root.glob(f".codex-delete-*/*{collision_file.name}")
        )
        assert len(quarantined_collision_files) == 1
        assert (
            quarantined_collision_files[0].read_text(encoding="utf-8")
            == "changed-in-window\n"
        )

        index_path = codex_home / "session_index.jsonl"
        index_path.write_text(
            json.dumps({"id": OLD_FILE_ONLY, "marker": "approved"}) + "\n",
            encoding="utf-8",
        )
        approved_entries = module.session_index_residual_entries(
            index_path,
            set(),
            set(),
        )
        original_pwrite = module.os.pwrite
        injected_index_row = False

        def racing_pwrite(fd: int, data: bytes, offset: int) -> int:
            nonlocal injected_index_row
            if not injected_index_row:
                injected_index_row = True
                with index_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"id": NEW_CHILD, "marker": "late"}) + "\n")
            return original_pwrite(fd, data, offset)

        setattr(module.os, "pwrite", racing_pwrite)
        try:
            cleanup = module.rewrite_approved_session_index_rows(
                codex_home,
                approved_entries,
            )
        finally:
            setattr(module.os, "pwrite", original_pwrite)
        assert cleanup["removed"] == 1
        index_text = index_path.read_text(encoding="utf-8")
        assert '"marker": "approved"' not in index_text
        assert '"marker": "late"' in index_text

        index_path.write_text(
            json.dumps({"id": OLD_FILE_ONLY, "marker": "short-write"}) + "\n",
            encoding="utf-8",
        )
        short_write_entries = module.session_index_residual_entries(
            index_path,
            set(),
            set(),
        )

        def zero_length_pwrite(fd: int, data: bytes, offset: int) -> int:
            return 0

        setattr(module.os, "pwrite", zero_length_pwrite)
        try:
            try:
                module.rewrite_approved_session_index_rows(
                    codex_home,
                    short_write_entries,
                )
            except OSError as exc:
                assert "Short write" in str(exc)
            else:
                raise AssertionError("short session-index write was accepted")
        finally:
            setattr(module.os, "pwrite", original_pwrite)
        assert '"marker": "short-write"' in index_path.read_text(encoding="utf-8")


def exercise_late_historical_replacement_reporting() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        snapshots_root = codex_home / "shell_snapshots"
        snapshots_root.mkdir()
        approved_file = snapshots_root / f"{OLD_FILE_ONLY}.approved.sh"
        approved_file.write_text("approved\n", encoding="utf-8")
        module = load_deleter_module()
        plan = module.make_plan(
            codex_home,
            [KEEP_SESSION],
            False,
            False,
            True,
        )
        original_rename = module.os.rename
        injected = False

        def racing_rename(source: object, destination: object) -> None:
            nonlocal injected
            if Path(source) == approved_file and not injected:
                injected = True
                approved_file.write_text("replacement\n", encoding="utf-8")
            original_rename(source, destination)

        setattr(module.os, "rename", racing_rename)
        try:
            result = module.cleanup_historical_residuals(
                codex_home,
                plan.historical_residuals,
                False,
                {KEEP_SESSION},
                False,
                True,
                False,
                plan.rollout_migration_state_rows,
            )
        finally:
            setattr(module.os, "rename", original_rename)
        assert result["verification"]["cleanup_ok"] is True
        assert result["approved_snapshot"]["paths"]["identity_changed_retained"] == [
            str(approved_file)
        ]
        assert result["verification"]["approved_remaining"] == []
        assert approved_file.read_text(encoding="utf-8") == "replacement\n"


def exercise_additive_historical_snapshot_apply() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        state_path = codex_home / "state_5.sqlite"
        logs_path = codex_home / "logs_2.sqlite"
        init_state(state_path, codex_home)
        init_logs(logs_path)

        index_path = codex_home / "session_index.jsonl"
        index_path.write_text(
            json.dumps({"id": TARGET_CLOSED_CHILD, "marker": "target"})
            + "\n"
            + json.dumps({"id": OLD_FILE_ONLY, "marker": "approved-delete"})
            + "\n"
            + json.dumps({"id": OLD_FILE_ONLY, "marker": "approved-change"})
            + "\n",
            encoding="utf-8",
        )
        snapshots = codex_home / "shell_snapshots"
        snapshots.mkdir()
        approved_snapshot = snapshots / f"{OLD_FILE_ONLY}.approved.sh"
        approved_snapshot.write_text("approved\n", encoding="utf-8")
        absent_snapshot = snapshots / f"{OLD_FILE_ONLY}.absent.sh"
        absent_snapshot.write_text("absent\n", encoding="utf-8")
        changed_snapshot = snapshots / f"{OLD_FILE_ONLY}.changed.sh"
        changed_snapshot.write_text("before\n", encoding="utf-8")

        conn = sqlite3.connect(logs_path)
        with conn:
            conn.execute(
                "INSERT INTO logs (ts, ts_nanos, level, target, thread_id) "
                "VALUES (1, 0, 'INFO', 'approved-second-row', ?)",
                (OLD_FILE_ONLY,),
            )
        conn.close()

        report = run_cmd(
            TARGET_CLOSED_CHILD,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        token = scope_token(plan, historical=True)
        approved_log_row_ids = {
            contract["row_id"]
            for entry in plan["historical_residuals"]["logs_rows_without_state"]
            if entry["id"] == OLD_FILE_ONLY
            for contract in entry["row_contracts"]
        }
        assert len(approved_log_row_ids) == 2

        tampered_token = token[:-1] + ("0" if token[-1] != "0" else "1")
        run_cmd(
            TARGET_CLOSED_CHILD,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--apply",
            "--apply-historical-residuals",
            "--confirm-plan",
            tampered_token,
            expect=3,
        )
        assert approved_snapshot.exists()
        conn = sqlite3.connect(state_path)
        try:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM threads WHERE id=?",
                    (TARGET_CLOSED_CHILD,),
                ).fetchone()[0]
                == 1
            )
        finally:
            conn.close()

        conn = sqlite3.connect(logs_path)
        with conn:
            changed_log_row_id = max(approved_log_row_ids)
            conn.execute(
                "UPDATE logs SET target='changed-after-report' WHERE id=?",
                (changed_log_row_id,),
            )
            old_id_new_row = conn.execute(
                "INSERT INTO logs (ts, ts_nanos, level, target, thread_id) "
                "VALUES (2, 0, 'INFO', 'after-report', ?)",
                (OLD_FILE_ONLY,),
            ).lastrowid
            new_id_row = conn.execute(
                "INSERT INTO logs (ts, ts_nanos, level, target, thread_id) "
                "VALUES (2, 0, 'INFO', 'after-report', ?)",
                (NEW_CHILD,),
            ).lastrowid
        conn.close()
        index_lines = index_path.read_text(encoding="utf-8").splitlines()
        index_lines[2] = json.dumps(
            {"id": OLD_FILE_ONLY, "marker": "changed-after-report"}
        )
        index_path.write_text("\n".join(index_lines) + "\n", encoding="utf-8")
        with index_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"id": OLD_FILE_ONLY, "marker": "after-report"}) + "\n"
            )
            handle.write(json.dumps({"id": NEW_CHILD, "marker": "after-report"}) + "\n")
        absent_snapshot.unlink()
        changed_snapshot.write_text("changed-after-report\n", encoding="utf-8")
        new_snapshot = snapshots / f"{OLD_FILE_ONLY}.after-report.sh"
        new_snapshot.write_text("after-report\n", encoding="utf-8")

        applied = run_cmd(
            TARGET_CLOSED_CHILD,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--apply",
            "--apply-historical-residuals",
            "--confirm-plan",
            token,
            "--json",
        )
        apply_result = json.loads(applied.stdout)["apply_result"]
        assert apply_result["success"] is True
        assert apply_result["historical_cleanup"]["verification"]["cleanup_ok"] is True
        assert apply_result["historical_residuals"]["summary"]["has_residuals"] is True
        approved_result = apply_result["historical_cleanup"]["approved_snapshot"]
        assert approved_result["logs"]["identity_changed_retained"] == 1
        assert approved_result["session_index"]["identity_changed_retained"] == 1
        assert str(absent_snapshot) in approved_result["paths"]["already_absent"]
        assert (
            str(changed_snapshot)
            in approved_result["paths"]["identity_changed_retained"]
        )

        conn = sqlite3.connect(logs_path)
        try:
            remaining_ids = {
                row[0]
                for row in conn.execute(
                    "SELECT id FROM logs WHERE id IN (?, ?)",
                    (old_id_new_row, new_id_row),
                )
            }
            assert remaining_ids == {old_id_new_row, new_id_row}
            assert conn.execute(
                "SELECT target FROM logs WHERE id=?",
                (changed_log_row_id,),
            ).fetchone() == ("changed-after-report",)
            deleted_log_row_ids = approved_log_row_ids - {changed_log_row_id}
            marks = ",".join("?" for _ in deleted_log_row_ids)
            assert not {
                row[0]
                for row in conn.execute(
                    f"SELECT id FROM logs WHERE id IN ({marks})",
                    sorted(deleted_log_row_ids),
                )
            }
        finally:
            conn.close()

        index_text = index_path.read_text(encoding="utf-8")
        assert '"marker": "approved-delete"' not in index_text
        assert '"marker": "changed-after-report"' in index_text
        assert index_text.count('"marker": "after-report"') == 2
        assert not approved_snapshot.exists()
        assert not absent_snapshot.exists()
        assert changed_snapshot.read_text(encoding="utf-8") == "changed-after-report\n"
        assert new_snapshot.exists()


def exercise_noncanonical_spawn_child_is_locally_retained() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        state_path = codex_home / "state_5.sqlite"
        init_state(state_path, codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        conn = sqlite3.connect(state_path)
        with conn:
            conn.execute(
                "DELETE FROM thread_spawn_edges WHERE parent_thread_id IN (?, ?)",
                (TARGET_PARENT, KEEP_SESSION),
            )
            conn.execute(
                "INSERT INTO thread_spawn_edges VALUES (?, ?, 'closed')",
                (TARGET_PARENT, TARGET_CLOSED_CHILD.upper()),
            )
        conn.close()

        report = run_cmd(
            TARGET_PARENT,
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        assert_report_ready(plan)
        assert_safety_warning(plan, "non-canonical session reference")

        applied = run_cmd(
            TARGET_PARENT,
            KEEP_SESSION,
            "--codex-home",
            str(codex_home),
            "--apply",
            "--confirm-plan",
            scope_token(plan),
            "--json",
        )
        result = json.loads(applied.stdout)["apply_result"]
        assert result["outcome"] in {"completed_with_warnings", "no_safe_work"}
        assert result["mutation_started"] is (
            result["outcome"] == "completed_with_warnings"
        )
        conn = sqlite3.connect(state_path)
        try:
            remaining = {row[0] for row in conn.execute("SELECT id FROM threads")}
        finally:
            conn.close()
        assert TARGET_PARENT in remaining
        if result["outcome"] == "completed_with_warnings":
            assert result["success"] is True
            assert any(
                entry.get("status") == "completed"
                and entry.get("mutation_started") is True
                for entry in result["component_results"].values()
            )
        else:
            assert result["success"] is False
            assert KEEP_SESSION in remaining


def exercise_all_dynamic_prewrite_skips_are_no_safe_work() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        init_desktop_ui_metadata(codex_home, TARGET_CLOSED_CHILD)
        init_auxiliary_thread_databases(
            codex_home, [TARGET_CLOSED_CHILD, TARGET_GRANDCHILD]
        )
        (codex_home / "session_index.jsonl").write_text(
            json.dumps({"id": TARGET_CLOSED_CHILD}) + "\n", encoding="utf-8"
        )
        module = load_deleter_module()
        module.desktop_owner_processes = lambda _codex_home: ([], "")
        plan = module.make_plan(
            codex_home, [TARGET_CLOSED_CHILD], False, True, False
        )
        before = snapshot_managed_bytes(codex_home)

        def fail_prewrite(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("injected final prewrite failure")

        module.apply_target_index_and_state = fail_prewrite
        module.delete_log_rows = fail_prewrite
        module.remove_paths = fail_prewrite
        module.apply_target_desktop_catalog = fail_prewrite
        module.apply_target_auxiliary_databases = fail_prewrite
        module.apply_target_global_state = fail_prewrite
        result = module.apply_plan(
            plan,
            plan.historical_residuals,
            True,
            False,
            False,
            False,
            "targets_only",
        )
        assert result["outcome"] == "no_safe_work"
        assert result["success"] is False
        assert result["mutation_started"] is False
        assert result["next_action"] != "inspect_partial"
        assert result["verification"]["verification_ok"] is True
        assert snapshot_managed_bytes(codex_home) == before
        requested = [
            entry
            for entry in result["component_results"].values()
            if entry["status"] != "not_requested"
        ]
        assert requested
        assert all(entry["status"] == "skipped_safely" for entry in requested)


def exercise_zero_write_prewrite_exception_is_not_partial() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        module = load_deleter_module()
        plan = module.make_plan(
            codex_home,
            [TARGET_CLOSED_CHILD],
            False,
            False,
            False,
        )
        for component, entry in plan.component_plans.items():
            if component != module.COMPONENT_CORE:
                entry["status"] = "skipped"
                entry["reasons"] = ["test isolates the core prewrite path"]
        before = snapshot_managed_bytes(codex_home)

        def fail_before_observer(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("ordinary prewrite exception")

        module.apply_target_index_and_state = fail_before_observer
        result = module.apply_plan(
            plan,
            plan.historical_residuals,
            False,
            False,
            False,
            False,
            "targets_only",
        )
        assert result["component_results"][module.COMPONENT_CORE] == {
            "status": "skipped_safely",
            "mutation_started": False,
            "reason": "ordinary prewrite exception",
        }
        assert result["mutation_started"] is False
        assert result["outcome"] == "no_safe_work"
        assert result["success"] is False
        assert result["next_action"] != "inspect_partial"
        assert snapshot_managed_bytes(codex_home) == before


def exercise_desktop_component_observers_follow_final_prewrite_checks() -> None:
    def expect_no_observer(
        setup: object,
        invoke: object,
        inject: object,
    ) -> None:
        with temporary_directory() as tmp:
            codex_home = Path(tmp) / ".codex"
            codex_home.mkdir()
            module, plan = setup(codex_home)
            module.desktop_owner_processes = lambda _codex_home: ([], "")
            inject(module, plan)
            before = snapshot_tree_bytes(codex_home)
            events: list[str] = []
            try:
                invoke(module, plan, events.append)
            except RuntimeError as exc:
                assert "injected" in str(exc) or "changed" in str(exc)
            else:
                raise AssertionError("injected final prewrite failure was ignored")
            assert events == []
            assert snapshot_tree_bytes(codex_home) == before

    def catalog_setup(codex_home: Path) -> tuple[ModuleType, object]:
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        init_desktop_ui_metadata(codex_home, NEW_CHILD)
        module = load_deleter_module()
        module.desktop_owner_processes = lambda _codex_home: ([], "")
        return module, module.make_plan(codex_home, [NEW_CHILD], True, True, False)

    def invalidate_catalog_sync_state(_module: ModuleType, plan: object) -> None:
        catalog_path = Path(plan.desktop_catalog_path)
        conn = sqlite3.connect(catalog_path)
        with conn:
            conn.execute(
                "UPDATE local_thread_catalog_sync_state "
                "SET observation_sequence=-1 WHERE host_id='local'"
            )
        conn.close()

    expect_no_observer(
        catalog_setup,
        lambda module, plan, observer: module.apply_target_desktop_catalog(
            plan, observer
        ),
        invalidate_catalog_sync_state,
    )

    def auxiliary_setup(codex_home: Path) -> tuple[ModuleType, object]:
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_auxiliary_thread_databases(
            codex_home, [TARGET_CLOSED_CHILD, TARGET_GRANDCHILD]
        )
        module = load_deleter_module()
        return module, module.make_plan(
            codex_home, [TARGET_CLOSED_CHILD], False, False, False
        )

    expect_no_observer(
        auxiliary_setup,
        lambda module, plan, observer: module.apply_target_auxiliary_databases(
            plan, observer
        ),
        lambda module, _plan: setattr(
            module,
            "require_desktop_offline",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("injected final auxiliary prewrite failure")
            ),
        ),
    )

    def global_setup(codex_home: Path) -> tuple[ModuleType, object]:
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        init_desktop_ui_metadata(codex_home, NEW_CHILD)
        module = load_deleter_module()
        module.desktop_owner_processes = lambda _codex_home: ([], "")
        return module, module.make_plan(codex_home, [NEW_CHILD], True, True, False)

    expect_no_observer(
        global_setup,
        lambda module, plan, observer: module.apply_target_global_state(
            plan, observer
        ),
        lambda module, _plan: setattr(
            module,
            "remove_global_state_contracts",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("injected final global prewrite failure")
            ),
        ),
    )


def exercise_expected_preserved_component_contracts() -> None:
    expected_markers = {
        "state_and_index",
        "session_index",
        "logs",
        "desktop_catalog",
        "auxiliary_thread_databases",
        "global_state",
    }

    def run_case(change_preserved: bool) -> dict[str, object]:
        tmp = temporary_directory()
        codex_home = Path(tmp.name) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        catalog_path, _ = init_desktop_ui_metadata(
            codex_home, TARGET_CLOSED_CHILD
        )
        auxiliary_paths = init_auxiliary_thread_databases(
            codex_home, [TARGET_CLOSED_CHILD, TARGET_GRANDCHILD]
        )
        index_path = codex_home / "session_index.jsonl"
        index_path.write_text(
            json.dumps({"id": TARGET_CLOSED_CHILD, "marker": "approved"}) + "\n",
            encoding="utf-8",
        )
        module = load_deleter_module()
        module.desktop_owner_processes = lambda _codex_home: ([], "")
        plan = module.make_plan(
            codex_home, [TARGET_CLOSED_CHILD], False, True, False
        )
        for entry in plan.component_plans.values():
            entry["status"] = "skipped"
            entry["reasons"] = ["expected-preserved audit"]

        original_verify = module.verify
        if change_preserved:
            changed = False

            def mutate_then_verify(*args: object, **kwargs: object) -> object:
                nonlocal changed
                if not changed:
                    changed = True
                    conn = sqlite3.connect(codex_home / "state_5.sqlite")
                    with conn:
                        conn.execute(
                            "UPDATE threads SET title='changed' WHERE id=?",
                            (TARGET_CLOSED_CHILD,),
                        )
                    conn.close()
                    index_path.write_text(
                        json.dumps(
                            {"id": TARGET_CLOSED_CHILD, "marker": "changed"}
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    conn = sqlite3.connect(codex_home / "logs_2.sqlite")
                    with conn:
                        conn.execute(
                            "UPDATE logs SET target='changed' WHERE thread_id=?",
                            (TARGET_CLOSED_CHILD,),
                        )
                    conn.close()
                    conn = sqlite3.connect(catalog_path)
                    with conn:
                        conn.execute(
                            "UPDATE local_thread_catalog SET display_title='changed' "
                            "WHERE thread_id=?",
                            (TARGET_CLOSED_CHILD,),
                        )
                    conn.close()
                    auxiliary = next(iter(auxiliary_paths.values()))
                    auxiliary.rename(auxiliary.with_suffix(".preserved-changed"))
                    global_path = codex_home / ".codex-global-state.json"
                    global_path.write_text('{"changed":true}', encoding="utf-8")
                return original_verify(*args, **kwargs)

            module.verify = mutate_then_verify
        try:
            result = module.apply_plan(
                plan,
                plan.historical_residuals,
                True,
                False,
                False,
                False,
                "targets_only",
            )
        finally:
            tmp.cleanup()
        return result

    unchanged = run_case(False)
    assert unchanged["verification"]["verification_ok"] is True
    assert unchanged["verification"]["expected_preserved_missing"] == []
    present_text = json.dumps(
        unchanged["verification"]["expected_preserved_present"], sort_keys=True
    )
    assert all(marker in present_text for marker in expected_markers)

    changed = run_case(True)
    assert changed["verification"]["verification_ok"] is False
    missing_text = json.dumps(
        changed["verification"]["expected_preserved_missing"], sort_keys=True
    )
    assert all(marker in missing_text for marker in expected_markers)

    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        index_path = codex_home / "session_index.jsonl"
        index_path.write_text(
            json.dumps({"id": TARGET_CLOSED_CHILD, "marker": "approved"}) + "\n",
            encoding="utf-8",
        )
        module = load_deleter_module()
        module.desktop_owner_processes = lambda _codex_home: ([], "")
        plan = module.make_plan(
            codex_home, [TARGET_CLOSED_CHILD], False, True, False
        )
        plan.component_plans[module.COMPONENT_CORE] = {
            "status": "skipped",
            "reasons": ["expected-preserved cross-component baseline audit"],
        }
        original_delete_log_rows = module.delete_log_rows

        def corrupt_skipped_core_then_delete_logs(
            *args: object, **kwargs: object
        ) -> object:
            conn = sqlite3.connect(codex_home / "state_5.sqlite")
            with conn:
                conn.execute(
                    "UPDATE threads SET title='changed by another component' "
                    "WHERE id=?",
                    (TARGET_CLOSED_CHILD,),
                )
            conn.close()
            index_path.write_text(
                json.dumps(
                    {"id": TARGET_CLOSED_CHILD, "marker": "changed by logs"}
                )
                + "\n",
                encoding="utf-8",
            )
            return original_delete_log_rows(*args, **kwargs)

        module.delete_log_rows = corrupt_skipped_core_then_delete_logs
        cross_component_change = module.apply_plan(
            plan,
            plan.historical_residuals,
            True,
            False,
            False,
            False,
            "targets_only",
        )
        assert cross_component_change["verification"]["verification_ok"] is False
        cross_component_missing = {
            item["object_id"]
            for item in cross_component_change["verification"][
                "expected_preserved_missing"
            ]
        }
        assert {"state_and_index", "session_index"} <= cross_component_missing


def exercise_global_mutation_lock_contention_gate() -> None:
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        report = run_cmd(
            TARGET_CLOSED_CHILD,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        module = load_deleter_module()
        lock_fd = module.acquire_global_mutation_lock(codex_home)
        try:
            before = snapshot_managed_bytes(codex_home)
            rejected = run_cmd(
                TARGET_CLOSED_CHILD,
                "--codex-home",
                str(codex_home),
                "--no-subagents",
                "--apply",
                "--confirm-plan",
                scope_token(plan),
                "--json",
                expect=2,
            )
        finally:
            module.release_global_mutation_lock(lock_fd)
        payload = json.loads(rejected.stdout)
        gate = payload["apply_result"]
        assert gate["outcome"] == "failed"
        assert gate["success"] is False
        assert gate["mutation_started"] is False
        assert gate["next_action"] == "wait_for_receipt"
        assert gate["gate"]["code"] == "mutation_lock_busy"
        assert snapshot_managed_bytes(codex_home) == before


def exercise_stable_error_human_and_verification_contracts() -> None:
    module = load_deleter_module()
    args = type("JsonArgs", (), {"json": True})()
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        assert module.emit_error(args, "injected input failure", 2) == 2
    error_payload = json.loads(stdout.getvalue())
    assert error_payload["outcome"] == "failed"
    assert error_payload["success"] is False
    assert error_payload["mutation_started"] is False
    assert error_payload["next_action"] == "fix_input"

    human_args = type("HumanArgs", (), {"json": False})()
    human_stderr = io.StringIO()
    with contextlib.redirect_stderr(human_stderr):
        assert module.emit_error(
            human_args,
            "injected apply failure",
            4,
            details={
                "plan_fingerprint": "f" * 64,
                "partial_apply_result": {
                    "outcome": "partial_possible",
                    "mutation_started": True,
                    "next_action": "inspect_partial",
                    "approval_token": "v4.synthetic." + "a" * 64,
                    "approved_execution_snapshot": {"digest": "d" * 64},
                },
            },
        ) == 4
    human_error = human_stderr.getvalue().lower()
    for forbidden in [
        "v4.",
        "approval_token",
        "approved_execution_snapshot",
        "plan_fingerprint",
        "digest",
    ]:
        assert forbidden not in human_error

    stable_verification_fields = STABLE_VERIFICATION_FIELDS
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        report = run_cmd(
            TARGET_CLOSED_CHILD,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        applied = run_cmd(
            TARGET_CLOSED_CHILD,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--apply",
            "--confirm-plan",
            scope_token(plan),
            "--json",
        )
        verification = json.loads(applied.stdout)["apply_result"]["verification"]
        assert stable_verification_fields <= set(verification)

    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        report = run_cmd(
            TARGET_CLOSED_CHILD,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--json",
        )
        plan = json.loads(report.stdout)["plan"]
        human = run_cmd(
            TARGET_CLOSED_CHILD,
            "--codex-home",
            str(codex_home),
            "--no-subagents",
            "--apply",
            "--confirm-plan",
            scope_token(plan),
        )
        rendered = (human.stdout + "\n" + human.stderr).lower()
        for forbidden in [
            "v4.",
            "approved_execution_snapshot",
            "plan_fingerprint",
            "row_sha256",
            "content_sha256",
            "digest",
        ]:
            assert forbidden not in rendered


def create_terminal_metadata_job(
    helper: ModuleType,
    job_root: Path,
    codex_home: Path,
    job_id: str,
    *,
    cleanup_job_ids: list[str],
    success: bool,
) -> Path:
    request = helper.build_request(
        codex_home=codex_home,
        session_ids=[TARGET_PARENT],
        approval_token="v4.synthetic." + "a" * 64,
        options={
            "include_subagents": True,
            "include_logs": True,
            "scan_historical": True,
            "apply_historical_residuals": False,
            "apply_missing_rollout_threads": False,
            "force_open": False,
        },
        timing={
            "launch_delay_seconds": 0.0,
            "quit_timeout_seconds": 5.0,
            "offline_stability_seconds": 0.5,
            "poll_interval_seconds": 0.05,
            "restart_timeout_seconds": 1.0,
        },
        restart_requested=False,
        expires_in_seconds=900,
        job_id=job_id,
        cleanup_job_ids=cleanup_job_ids,
        exit_mode=helper.EXIT_MODE_MANUAL_GHOSTTY,
    )
    job_dir, _initial = helper.create_job(request, job_root)
    helper.safely_remove_unconsumed_request(job_dir)
    writer = helper.ReceiptWriter(job_dir)
    if success:
        writer.update(
            phase="complete",
            outcome="completed",
            next_action="reopen_desktop",
            terminal=True,
            mutation_started=True,
            success=True,
            deletion_success=True,
            verification_ok=True,
            permanent_deletion_complete=True,
            partial_possible=False,
            safe_to_reopen=True,
        )
    else:
        writer.update(
            phase="blocked_before_mutation",
            outcome="plan_changed",
            next_action="restage",
            terminal=True,
            mutation_started=False,
            success=False,
            deletion_success=False,
            verification_ok=False,
            permanent_deletion_complete=False,
            partial_possible=False,
            safe_to_reopen=True,
        )
    return job_dir


def exercise_offline_receipt_lifecycle_cleanup() -> None:
    helper = load_offline_helper_module()
    with temporary_directory() as tmp:
        root = Path(tmp)
        codex_home = root / ".codex"
        codex_home.mkdir(mode=0o700)
        job_root = root / "jobs"
        first = create_terminal_metadata_job(
            helper,
            job_root,
            codex_home,
            "1" * 32,
            cleanup_job_ids=[],
            success=False,
        )
        second = create_terminal_metadata_job(
            helper,
            job_root,
            codex_home,
            "2" * 32,
            cleanup_job_ids=[first.name],
            success=False,
        )
        assert helper.validated_cleanup_lineage(
            job_root,
            [str(second)],
            codex_home,
        ) == [second.name, first.name]
        current = create_terminal_metadata_job(
            helper,
            job_root,
            codex_home,
            "3" * 32,
            cleanup_job_ids=[second.name, first.name],
            success=True,
        )
        receipt = read_offline_receipt(current)
        result = helper.cleanup_verified_job_chain(current, receipt)
        assert result["cleanup_complete"] is True
        assert result["cleaned_job_count"] == 3
        assert not first.exists()
        assert not second.exists()
        assert not current.exists()
        assert job_root.is_dir()

        pending = create_terminal_metadata_job(
            helper,
            job_root,
            codex_home,
            "4" * 32,
            cleanup_job_ids=[],
            success=True,
        )
        unexpected = pending / "unexpected.txt"
        unexpected.write_text("preserve", encoding="utf-8")
        helper.ReceiptWriter(pending).update(
            core={"result": {"large_diagnostic": "x" * 4096}}
        )
        receipt_before = (pending / helper.RECEIPT_FILENAME).read_bytes()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            assert helper.read_status(pending) == helper.EXIT_OK
        compact = json.loads(stdout.getvalue())
        assert "core" not in compact
        assert compact["permanent_deletion_complete"] is True
        assert (pending / helper.RECEIPT_FILENAME).read_bytes() == receipt_before

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            assert helper.read_status(pending, full=True) == helper.EXIT_OK
        assert "large_diagnostic" in stdout.getvalue()

        result = helper.cleanup_verified_job_chain(
            pending,
            read_offline_receipt(pending),
        )
        assert result["cleanup_pending"] is True
        assert pending.exists()
        retained = read_offline_receipt(pending)
        assert retained["cleanup_pending"] is True
        assert retained["next_action"] == "retry_cleanup"
        unexpected.unlink()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            assert helper.cleanup_completed_handoff(pending) == helper.EXIT_OK
        assert not pending.exists()


def empty_historical_snapshot(module: ModuleType) -> dict[str, object]:
    snapshot: dict[str, object] = {"scanned": True}
    for key in module.HISTORICAL_SCOPE_KEYS:
        snapshot[key] = {} if key == "state_orphan_references" else []
    return snapshot


def exercise_empty_approved_historical_snapshot_is_satisfied() -> None:
    module = load_deleter_module()
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        plan = module.make_plan(
            codex_home=codex_home,
            root_ids=[TARGET_GRANDCHILD],
            include_subagents=False,
            include_logs=True,
            scan_historical=True,
        )
        assert plan.counts["state_threads"] == 1
        result = module.apply_plan(
            plan,
            empty_historical_snapshot(module),
            include_logs=True,
            scan_historical=True,
            apply_historical_residuals=True,
            apply_missing_rollout_threads=False,
            approval_scope="targets_and_historical_residuals",
            execution_snapshot=module.approval_execution_snapshot(plan, False),
            force_open=False,
        )
        assert result["success"] is True
        assert result["outcome"] == "completed"
        assert result["historical_cleanup"]["applied"] is False
        assert result["historical_cleanup"]["approved_snapshot_empty"] is True
        assert result["approved_historical_snapshot_empty"] is True
        assert result["historical_scan_ok"] is True
        assert result["verification"]["historical_snapshot_ok"] is True
        assert result["historical_residuals"]["approved_snapshot_empty"] is True


def mark_empty_historical_partial_job(helper: ModuleType, job_dir: Path) -> None:
    receipt = read_offline_receipt(job_dir)
    request = dict(receipt["request"])
    options = dict(request["options"])
    options["apply_historical_residuals"] = True
    request["options"] = options
    verification = {
        "verification_ok": True,
        "historical_snapshot_ok": False,
        "planned_deleted_remaining": {},
        "expected_preserved_missing": [],
        "unexpected_remaining": [],
        "unexpected_non_target_removed": [],
        "offline_verification_ok": True,
    }
    helper.ReceiptWriter(job_dir).update(
        phase="partial_or_verification_failed",
        outcome="partial_possible",
        next_action="inspect_partial",
        terminal=True,
        mutation_started=True,
        success=False,
        deletion_success=False,
        verification_ok=True,
        permanent_deletion_complete=False,
        partial_possible=True,
        safe_to_reopen=False,
        retryable=False,
        helper_exit_code=helper.EXIT_PARTIAL_OR_VERIFY_FAILED,
        request=request,
        request_integrity_verified=True,
        plan_revalidated=True,
        owner_reappeared=False,
        errors=[],
        staged_plan={
            "historical_residuals": {
                "scanned": True,
                "total_ids": 0,
                "total_items": 0,
                "has_residuals": False,
            }
        },
        core={
            "exit_code": helper.EXIT_PARTIAL_OR_VERIFY_FAILED,
            "result": {
                "apply_result": {
                    "outcome": "partial_possible",
                    "mutation_started": True,
                    "historical_scan_ok": False,
                    "historical_cleanup": {"applied": False},
                    "component_results": {
                        "historical": {
                            "status": "not_requested",
                            "mutation_started": False,
                        }
                    },
                    "approved_execution_snapshot": {
                        "object_contracts": {
                            "state_database_path": "",
                            "logs_database_path": "",
                        }
                    },
                    "verification": verification,
                }
            },
        },
    )


def exercise_job_root_audit_and_empty_history_recovery() -> None:
    helper = load_offline_helper_module()
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir(mode=0o700)
        job_root = codex_home / helper.DEFAULT_JOB_ROOT_NAME
        success = create_terminal_metadata_job(
            helper,
            job_root,
            codex_home,
            "5" * 32,
            cleanup_job_ids=[],
            success=True,
        )
        failed = create_terminal_metadata_job(
            helper,
            job_root,
            codex_home,
            "6" * 32,
            cleanup_job_ids=[],
            success=False,
        )
        recoverable = create_terminal_metadata_job(
            helper,
            job_root,
            codex_home,
            "7" * 32,
            cleanup_job_ids=[],
            success=False,
        )
        mark_empty_historical_partial_job(helper, recoverable)
        (job_root / ".DS_Store").write_bytes(b"finder")
        (job_root / "unrecognized").write_text("preserve", encoding="utf-8")
        unsafe_target = Path(tmp) / "unsafe-target"
        unsafe_target.mkdir()
        unsafe_link = job_root / ("8" * 32)
        unsafe_link.symlink_to(unsafe_target, target_is_directory=True)

        audit = helper.audit_job_root(codex_home)
        assert audit["ignored_entries"] == [".DS_Store"]
        assert audit["unrecognized_entries"] == [
            {"name": "unrecognized", "disposition": "preserve_unrecognized"}
        ]
        classifications = {
            item["job_id"]: item["classification"] for item in audit["jobs"]
        }
        assert classifications[success.name] == "verified_success_cleanup_ready"
        assert classifications[failed.name] == "terminal_failure_supersedable"
        assert (
            classifications[recoverable.name]
            == "recoverable_empty_historical_snapshot"
        )
        assert classifications[unsafe_link.name] == "unsafe_preserved"

        lock_path = success / helper.LOCK_FILENAME
        helper.atomic_write_bytes(lock_path, b"")
        lock_fd = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            active = helper.audit_job_root(codex_home)
            active_classes = {
                item["job_id"]: item["classification"] for item in active["jobs"]
            }
            assert active_classes[success.name] == "active"
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            assert (
                helper.recover_empty_historical_handoff(recoverable)
                == helper.EXIT_OK
            )
        recovered = json.loads(stdout.getvalue())
        assert recovered["recovered"] is True
        assert recovered["verification_ok"] is True
        assert recovered["cleanup_complete"] is True
        assert not recoverable.exists()
        assert success.exists()
        assert failed.exists()
        assert unsafe_link.is_symlink()


def exercise_scope_fingerprint_stability() -> None:
    module = load_deleter_module()
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        original_time = module.time.time
        try:
            module.time.time = lambda: 1_900_000_000.0
            plan = module.make_plan(
                codex_home,
                [TARGET_CLOSED_CHILD],
                False,
                True,
                True,
            )
            module.time.time = lambda: 1_900_086_400.0
            later_report = module.make_plan(
                codex_home,
                [TARGET_CLOSED_CHILD],
                False,
                True,
                True,
            )
        finally:
            module.time.time = original_time
        target_before = module.approval_scope_fingerprint(
            plan,
            False,
            False,
            False,
        )
        historical_before = module.approval_scope_fingerprint(
            plan,
            True,
            False,
            False,
        )
        assert (
            module.approval_scope_fingerprint(
                later_report,
                False,
                False,
                False,
            )
            == target_before
        )
        assert (
            module.approval_scope_fingerprint(
                later_report,
                True,
                False,
                False,
            )
            == historical_before
        )
        plan.preflight["report_generated_at_epoch_ms"] = 1_900_000_000_000
        assert (
            module.approval_scope_fingerprint(plan, False, False, False)
            == target_before
        )
        plan.preflight["report_generated_at_epoch_ms"] = 1_900_086_400_000
        assert (
            module.approval_scope_fingerprint(plan, False, False, False)
            == target_before
        )
        plan.preflight["desktop_owner_processes"] = [
            {"pid": 99999, "executable": "/Applications/Codex.app/test"}
        ]
        plan.preflight["state_schema_compatibility"] = {
            "available": True,
            "unknown_tables": ["runtime_only_diagnostic"],
            "protected_ids": [],
            "target_reference_hits": [],
            "scan_complete": True,
        }
        plan.global_state_non_owning_mentions["runtime.json"] = [
            {"path": ["prompt-history"], "id": TARGET_CLOSED_CHILD}
        ]
        plan.historical_residuals.setdefault("logs_rows_without_state", []).append(
            {"id": OLD_FILE_ONLY, "rows": 1}
        )
        assert (
            module.approval_scope_fingerprint(plan, False, False, False)
            == target_before
        )
        assert (
            module.approval_scope_fingerprint(plan, True, False, False)
            != historical_before
        )


def exercise_semantic_scope_change_matrix() -> None:
    module = load_deleter_module()
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        state_path = codex_home / "state_5.sqlite"
        logs_path = codex_home / "logs_2.sqlite"
        init_state(state_path, codex_home)
        init_logs(logs_path)
        original = module.make_plan(
            codex_home,
            [TARGET_CLOSED_CHILD],
            False,
            True,
            True,
        )
        target_fingerprint = module.approval_scope_fingerprint(
            original,
            False,
            False,
            False,
        )

        conn = sqlite3.connect(state_path)
        with conn:
            conn.execute(
                "CREATE TABLE future_unrelated_metadata "
                "(id INTEGER PRIMARY KEY, note TEXT)"
            )
            conn.execute(
                "INSERT INTO future_unrelated_metadata (note) VALUES (?)",
                ("compatible additive extension",),
            )
        conn.close()
        compatible_extension = module.make_plan(
            codex_home,
            [TARGET_CLOSED_CHILD],
            False,
            True,
            True,
        )
        assert "future_unrelated_metadata" in compatible_extension.preflight[
            "state_schema_compatibility"
        ]["unknown_tables"]
        assert module.approval_scope_fingerprint(
            compatible_extension,
            False,
            False,
            False,
        ) == target_fingerprint

        conn = sqlite3.connect(logs_path)
        with conn:
            conn.execute(
                "INSERT INTO logs "
                "(ts, ts_nanos, level, target, thread_id) "
                "VALUES (2, 0, 'INFO', 'later-target-row', ?)",
                (TARGET_CLOSED_CHILD,),
            )
        conn.close()
        target_log_added = module.make_plan(
            codex_home,
            [TARGET_CLOSED_CHILD],
            False,
            True,
            True,
        )
        changed_log_fingerprint = module.approval_scope_fingerprint(
            target_log_added,
            False,
            False,
            False,
        )
        assert changed_log_fingerprint != target_fingerprint

        conn = sqlite3.connect(state_path)
        with conn:
            conn.execute("CREATE TABLE future_target_refs (thread_id TEXT)")
            conn.execute(
                "INSERT INTO future_target_refs VALUES (?)",
                (TARGET_CLOSED_CHILD,),
            )
        conn.close()
        protected_target = module.make_plan(
            codex_home,
            [TARGET_CLOSED_CHILD],
            False,
            True,
            True,
        )
        assert protected_target.preflight["state_schema_compatibility"][
            "target_reference_hits"
        ]
        assert module.approval_scope_fingerprint(
            protected_target,
            False,
            False,
            False,
        ) != changed_log_fingerprint

    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        state_path = codex_home / "state_5.sqlite"
        init_state(state_path, codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        graph_before = module.make_plan(
            codex_home,
            [TARGET_CLOSED_CHILD],
            True,
            True,
            True,
        )
        graph_fingerprint = module.approval_scope_fingerprint(
            graph_before,
            False,
            False,
            False,
        )
        conn = sqlite3.connect(state_path)
        with conn:
            conn.execute(
                "INSERT INTO thread_spawn_edges VALUES (?, ?, 'closed')",
                (TARGET_CLOSED_CHILD, KEEP_SESSION),
            )
        conn.close()
        graph_after = module.make_plan(
            codex_home,
            [TARGET_CLOSED_CHILD],
            True,
            True,
            True,
        )
        assert KEEP_SESSION in graph_after.target_ids
        assert module.approval_scope_fingerprint(
            graph_after,
            False,
            False,
            False,
        ) != graph_fingerprint


def exercise_structural_database_filename_discovery() -> None:
    module = load_deleter_module()
    with temporary_directory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        init_state(codex_home / "state_5.sqlite", codex_home)
        init_logs(codex_home / "logs_2.sqlite")
        catalog_path, _mentions = init_desktop_ui_metadata(
            codex_home,
            TARGET_CLOSED_CHILD,
        )
        renamed_catalog = catalog_path.with_name("desktop-catalog-v99.sqlite")
        catalog_path.rename(renamed_catalog)
        auxiliary = init_auxiliary_thread_databases(
            codex_home,
            [TARGET_CLOSED_CHILD, TARGET_GRANDCHILD],
        )
        renamed_auxiliary: list[Path] = []
        for index, path in enumerate(auxiliary.values(), start=1):
            renamed = path.with_name(f"future-thread-metadata-{index}.sqlite")
            path.rename(renamed)
            renamed_auxiliary.append(renamed)

        original_owner_check = module.desktop_owner_processes
        module.desktop_owner_processes = lambda _codex_home: ([], "")
        try:
            plan = module.make_plan(
                codex_home,
                [TARGET_CLOSED_CHILD],
                False,
                True,
                False,
            )
            report = module.plan_to_dict(plan)
            result = module.apply_plan(
                plan,
                plan.historical_residuals,
                True,
                False,
                False,
                False,
                "targets_only",
            )
        finally:
            module.desktop_owner_processes = original_owner_check
        assert report["desktop_catalog"]["path"] == str(renamed_catalog)
        assert set(report["auxiliary_thread_databases_present"]) == {
            path.name for path in renamed_auxiliary
        }
        assert report["blockers"] == []
        assert result["success"] is True
        assert result["verification"]["verification_ok"] is True
        conn = sqlite3.connect(renamed_catalog)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM local_thread_catalog WHERE thread_id=?",
                (TARGET_CLOSED_CHILD,),
            ).fetchone() == (0,)
        finally:
            conn.close()
        for path in renamed_auxiliary:
            conn = sqlite3.connect(path)
            try:
                table = (
                    "app_server_history_snapshots"
                    if path.name.endswith("1.sqlite")
                    else "thread_turn_summaries"
                )
                assert conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE thread_id=?",
                    (TARGET_CLOSED_CHILD,),
                ).fetchone() == (0,)
            finally:
                conn.close()


def main() -> None:
    run_cmd(TARGET_PARENT, "--apply-historical-residuals", expect=2)
    run_cmd(
        TARGET_PARENT,
        "--apply",
        "--apply-missing-rollout-threads",
        expect=2,
    )
    exercise(apply_historical_residuals=False)
    exercise(apply_historical_residuals=True)
    exercise(
        apply_historical_residuals=True,
        apply_missing_rollout_threads=True,
    )
    exercise_batch_and_id_validation()
    exercise_human_report_hides_approval_capsules()
    exercise_late_target_artifact_is_retained()
    exercise_desktop_ui_metadata_only_cleanup()
    exercise_desktop_catalog_stale_plan_rejection()
    exercise_live_desktop_owner_blocks_apply()
    exercise_auxiliary_thread_database_cleanup()
    exercise_migrated_summary_database_cleanup()
    exercise_auxiliary_thread_row_stale_plan_rejection()
    exercise_auxiliary_unknown_schema_blockers()
    exercise_voice_selector_object_cleanup()
    exercise_voice_selector_nonstandard_shape_cleanup()
    exercise_voice_selector_stale_plan_rejection()
    exercise_discovered_exact_uuid_mapping_cleanup()
    exercise_scope_fingerprint_stability()
    exercise_semantic_scope_change_matrix()
    exercise_structural_database_filename_discovery()
    exercise_staged_long_capsule_transport_contract()
    exercise_stage_rejects_changed_report_before_job_creation()
    exercise_stage_accepts_paginated_sidecar_churn()
    exercise_ghostty_tcc_denial_is_safe_and_retryable()
    exercise_staged_scope_and_launch_retry_contract()
    exercise_staged_cancel_revokes_capsule()
    exercise_staged_job_cannot_bypass_ghostty_launch_gate()
    exercise_staged_manual_only_success()
    exercise_staged_manual_offline_timeout_is_retryable()
    exercise_staged_late_target_artifact_is_retained()
    exercise_staged_historical_additions_do_not_expand_scope()
    exercise_offline_receipt_lifecycle_cleanup()
    exercise_empty_approved_historical_snapshot_is_satisfied()
    exercise_job_root_audit_and_empty_history_recovery()
    exercise_offline_helper_request_hash_mismatch()
    exercise_offline_helper_source_drift()
    exercise_global_state_pair_second_publish_failure()
    exercise_global_state_rollback_preserves_same_inode_collision()
    exercise_historical_fail_closed_without_state()
    exercise_unrelated_schema_extension_is_compatible()
    exercise_unknown_target_reference_preserves_state_component()
    exercise_logs_schema_extensions_are_isolated()
    exercise_unknown_json_reference_protects_historical_residual()
    exercise_recent_log_only_ids_are_transient()
    exercise_thread_sections_schema_blocker()
    exercise_uuid_ancestor_regression()
    exercise_ambiguous_artifact_owner_blocker()
    exercise_ambiguous_generated_owner_blocker()
    exercise_artifact_content_contract()
    exercise_direct_open_root()
    exercise_symlink_blockers()
    exercise_index_nonobject_json()
    exercise_noncanonical_blocker()
    exercise_target_graph_recheck()
    exercise_no_subagents_outgoing_edge_recheck()
    exercise_no_subagents_no_logs()
    exercise_warning_first_target_retention_continues_independent_closed()
    exercise_current_session_retained_no_safe_work()
    exercise_trigger_blocker()
    exercise_alternate_database_blocker()
    exercise_warning_first_component_skip_continues()
    exercise_partial_result_survives_invalid_utf8()
    exercise_malformed_json_index_blocker()
    exercise_sqlite_hardlink_blocker()
    exercise_rollout_path_directory_blocker()
    exercise_open_missing_rollout_scope()
    exercise_current_missing_rollout_blocker()
    exercise_rollout_owner_mismatch_blocker()
    exercise_multiple_incoming_edges()
    exercise_special_character_codex_home()
    exercise_historical_revive_race()
    exercise_missing_rollout_outgoing_edge_recheck()
    exercise_historical_logs_scope_race()
    exercise_no_historical_scan_apply()
    exercise_rollout_migration_schema_support()
    exercise_rollout_migration_cursor_blockers()
    exercise_rollout_migration_schema_drift_blocker()
    exercise_rollout_migration_future_reference_blocker()
    exercise_rollout_migration_cursor_race_is_preserved()
    exercise_rollout_migration_missing_thread_cleanup()
    exercise_rollout_migration_target_path_blocker()
    exercise_paginated_history_support_and_guards()
    exercise_historical_target_edge_contract()
    exercise_late_artifact_and_index_additions()
    exercise_late_historical_replacement_reporting()
    exercise_additive_historical_snapshot_apply()
    exercise_noncanonical_spawn_child_is_locally_retained()
    exercise_all_dynamic_prewrite_skips_are_no_safe_work()
    exercise_zero_write_prewrite_exception_is_not_partial()
    exercise_desktop_component_observers_follow_final_prewrite_checks()
    exercise_expected_preserved_component_contracts()
    exercise_global_mutation_lock_contention_gate()
    exercise_stable_error_human_and_verification_contracts()

    print("smoke test passed")


if __name__ == "__main__":
    main()
