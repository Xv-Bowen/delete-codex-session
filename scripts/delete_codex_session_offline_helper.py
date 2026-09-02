#!/usr/bin/env python3
"""Visible, manual-offline macOS handoff for a Codex session deletion.

This module deliberately does not implement deletion itself.  After one user
approval selects the scope and authorizes deletion, ``stage`` verifies that the
selected approval-scope fingerprint is unchanged and freezes that scope in a
private job directory.  ``launch-ghostty`` then opens one visible Ghostty worker without a
second user-approval round.  The user manually quits Codex Desktop, the worker
waits for a stable offline interval, and only then invokes the sibling
``delete_codex_session.py`` entry point in-process.

The approval capsule is generated and retained only in a 0600 request file. It
is never placed in argv, stdin, the environment, a receipt, stdout, or stderr.
The Ghostty worker consumes and durably unlinks that file before acting.  This
helper never asks Desktop to quit and never restarts it.  After verified success
it strictly removes the transient receipt and any explicitly linked failed-job
chain; failures retain private recovery metadata.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import os
import plistlib
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


PROTOCOL_VERSION = 6
REQUEST_SCHEMA_VERSION = 6
RECEIPT_SCHEMA_VERSION = 6
JOB_AUDIT_CONTRACT_VERSION = 1
DEFAULT_BUNDLE_ID = "com.openai.codex"
DEFAULT_GHOSTTY_APP = Path("/Applications/Ghostty.app")
DEFAULT_GHOSTTY_BUNDLE_ID = "com.mitchellh.ghostty"
MINIMUM_GHOSTTY_VERSION = (1, 3, 0)
DEFAULT_OSASCRIPT = Path("/usr/bin/osascript")
EXIT_MODE_MANUAL_GHOSTTY = "manual_ghostty"
SUPPORTED_EXIT_MODES = {EXIT_MODE_MANUAL_GHOSTTY}
DEFAULT_JOB_ROOT_NAME = ".session-deletion-jobs"
REQUEST_FILENAME = "request.json"
RECEIPT_FILENAME = "receipt.json"
LOCK_FILENAME = "worker.lock"
LAUNCH_LOCK_FILENAME = "launch.lock"
CORE_SCRIPT_FILENAME = "delete_codex_session.py"
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_CAPTURE_BYTES = 32 * 1024 * 1024
MAX_RECEIPT_BYTES = 48 * 1024 * 1024
MAX_TOKEN_CHARS = 1024 * 1024
MAX_CLEANUP_JOB_IDS = 64
IGNORED_JOB_ROOT_ENTRIES = {".DS_Store"}
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-" r"[0-9a-f]{4}-[0-9a-f]{12}$"
)
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
TOKEN_RE = re.compile(r"^v[0-9]+\.[A-Za-z0-9_-]+\.[0-9a-f]{64}$")
BUNDLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{1,254}$")

EXIT_OK = 0
EXIT_BLOCKED = 2
EXIT_PLAN_CHANGED = 3
EXIT_PARTIAL_OR_VERIFY_FAILED = 4

OUTCOME_STAGED = "report_ready"
OUTCOME_ROUTE_DIRECT = "report_ready"
OUTCOME_WAITING_OFFLINE = "waiting_for_manual_exit"
OUTCOME_RETRYABLE_WARNING = "waiting_for_manual_exit"
OUTCOME_LAUNCH_UNKNOWN = "waiting_for_manual_exit"
OUTCOME_COMPLETE = "completed"
OUTCOME_COMPLETE_WITH_WARNINGS = "completed_with_warnings"
OUTCOME_NO_SAFE_WORK = "no_safe_work"
OUTCOME_RESTAGE_REQUIRED = "plan_changed"
OUTCOME_PARTIAL_POSSIBLE = "partial_possible"
OUTCOME_CANCELLED = "no_safe_work"
OUTCOME_FAILED = "failed"

NEXT_ROUTE_TO_DIRECT_APPLY = "approve_and_apply"
NEXT_LAUNCH_GHOSTTY = "launch_ghostty"
NEXT_QUIT_AND_WAIT = "quit_desktop"
NEXT_RETRY_LAUNCH = "relaunch_same_job"
NEXT_INSPECT_NO_RELAUNCH = "wait_for_receipt"
NEXT_RESTAGE = "restage"
NEXT_KEEP_CLOSED = "inspect_partial"
NEXT_REOPEN = "reopen_desktop"
NEXT_REOPEN_AND_VERIFY = "reopen_and_verify"
NEXT_RETRY_CLEANUP = "retry_cleanup"
NEXT_CHOOSE_SCOPE = "choose_scope"
NEXT_FIX_INPUT = "fix_input"
NEXT_NONE = "none"

ALLOWED_OUTCOMES = {
    "report_ready",
    "completed",
    "completed_with_warnings",
    "no_safe_work",
    "waiting_for_manual_exit",
    "plan_changed",
    "partial_possible",
    "failed",
}
ALLOWED_NEXT_ACTIONS = {
    NEXT_CHOOSE_SCOPE,
    NEXT_ROUTE_TO_DIRECT_APPLY,
    NEXT_LAUNCH_GHOSTTY,
    NEXT_QUIT_AND_WAIT,
    NEXT_INSPECT_NO_RELAUNCH,
    NEXT_RETRY_LAUNCH,
    NEXT_RESTAGE,
    NEXT_REOPEN,
    NEXT_REOPEN_AND_VERIFY,
    NEXT_RETRY_CLEANUP,
    NEXT_KEEP_CLOSED,
    NEXT_FIX_INPUT,
    NEXT_NONE,
}

RETRYABLE_PREMUTATION_PHASES = {
    "reading_staged_request",
    "request_validated_pending_manual_exit",
    "launch_delay",
    "waiting_for_manual_exit",
    "waiting_for_offline",
    "offline_observed",
    "manual_offline_wait_failed",
    "terminal_launch_failed_before_worker",
}

REQUEST_KEYS = {
    "schema_version",
    "protocol_version",
    "job_id",
    "created_at_epoch_ms",
    "expires_at_epoch_ms",
    "codex_home",
    "session_ids",
    "options",
    "timing",
    "restart",
    "exit_mode",
    "source",
    "approval_token",
    "cleanup_job_ids",
}
OPTIONS_KEYS = {
    "include_subagents",
    "include_logs",
    "scan_historical",
    "apply_historical_residuals",
    "apply_missing_rollout_threads",
    "force_open",
}
TIMING_KEYS = {
    "launch_delay_seconds",
    "quit_timeout_seconds",
    "offline_stability_seconds",
    "poll_interval_seconds",
    "restart_timeout_seconds",
}
RESTART_KEYS = {"requested", "bundle_id"}
SOURCE_KEYS = {
    "core_path",
    "core_sha256",
    "helper_sha256",
    "interpreter_path",
    "interpreter_sha256",
    "interpreter_version",
}


class HelperError(RuntimeError):
    """A safely reportable handoff failure."""


class RequestError(HelperError):
    """The private request is malformed or unsafe."""


class ExpiredRequestError(RequestError):
    """The approved private request expired before mutation."""


class SourceChangedError(HelperError):
    """Approved helper/core source changed before execution."""


class PlanChangedError(HelperError):
    """The approval capsule no longer matches the current read-only plan."""


class DesktopOfflineError(HelperError):
    """Codex Desktop did not become or remain safely offline."""


@dataclass(frozen=True)
class CoreInvocationResult:
    exit_code: int
    payload: dict[str, Any] | None
    stdout_sha256: str
    stderr: str
    output_valid: bool
    owner_reappeared: bool
    owner_monitor_issue: str
    pre_apply_owner_blocked: bool


@dataclass(frozen=True)
class GhosttyLaunchResult:
    launched: bool
    label: str
    error: str
    submission_status: str


def now_epoch_ms() -> int:
    return time.time_ns() // 1_000_000


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def secure_directory(path: Path, *, create: bool = False) -> None:
    if create:
        try:
            path.mkdir(mode=0o700, parents=False, exist_ok=False)
        except FileExistsError:
            pass
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise HelperError(f"Private directory does not exist: {path}") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise HelperError(
            f"Private directory must be a same-user, non-shared real directory: {path}"
        )


def owned_real_directory(path: Path) -> None:
    """Validate a containing directory without requiring private permissions."""

    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise HelperError(f"Containing directory does not exist: {path}") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise HelperError(
            f"Containing directory is not a same-user real directory: {path}"
        )


def secure_regular_file(path: Path, *, expected_mode: int = 0o600) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise HelperError(f"Required private file does not exist: {path}") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != expected_mode
    ):
        raise HelperError(
            f"Private file must be a same-user, single-link {expected_mode:04o} file: {path}"
        )
    return info


def atomic_write_bytes(path: Path, content: bytes, mode: int = 0o600) -> None:
    secure_directory(path.parent)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        info = temporary.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != mode
        ):
            raise HelperError(f"Unsafe staged private file: {temporary}")
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode(
            "utf-8"
        )
        + b"\n"
    )
    atomic_write_bytes(path, encoded)


def read_private_json(
    path: Path, *, maximum: int = MAX_REQUEST_BYTES
) -> dict[str, Any]:
    secure_regular_file(path)
    raw = path.read_bytes()
    if len(raw) > maximum:
        raise HelperError(f"Private JSON file is too large: {path}")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise HelperError(f"Private JSON file is invalid: {path}") from exc
    if not isinstance(value, dict):
        raise HelperError(f"Private JSON root must be an object: {path}")
    return value


def create_exclusive_private_file(path: Path, content: bytes) -> None:
    secure_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        offset = 0
        while offset < len(content):
            written = os.write(fd, content[offset:])
            if written <= 0:
                raise HelperError(f"Unable to finish writing private file: {path}")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    secure_regular_file(path)
    fsync_directory(path.parent)


def read_private_bytes(path: Path, *, maximum: int = MAX_REQUEST_BYTES) -> bytes:
    """Read one private file through a stable, no-follow descriptor."""

    secure_directory(path.parent)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > maximum
        ):
            raise RequestError("The private file failed its safety contract.")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 128 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > maximum:
            raise RequestError("The private file exceeds the supported size.")
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise RequestError("The private file identity changed while reading.")
        return raw
    finally:
        os.close(fd)


def consume_private_request(path: Path) -> bytes:
    """Read and durably unlink the only on-disk approval capsule copy."""

    raw = read_private_bytes(path)
    path.unlink()
    fsync_directory(path.parent)
    return raw


def bounded_number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RequestError(f"{name} must be a number.")
    converted = float(value)
    if not minimum <= converted <= maximum:
        raise RequestError(f"{name} is outside the supported range.")
    return converted


def expiration_seconds(value: str) -> int:
    try:
        converted = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expiration must be an integer") from exc
    if not 300 <= converted <= 86_400:
        raise argparse.ArgumentTypeError("expiration must be between 300 and 86400")
    return converted


def strict_bool_map(value: Any, keys: set[str], name: str) -> dict[str, bool]:
    if not isinstance(value, dict) or set(value) != keys:
        raise RequestError(f"{name} has an unsupported schema.")
    if any(type(value[key]) is not bool for key in keys):
        raise RequestError(f"Every {name} value must be boolean.")
    return {key: bool(value[key]) for key in sorted(keys)}


def public_json_value(value: Any) -> Any:
    """Return a receipt-safe representation of optional newer core fields."""

    if isinstance(value, dict):
        return {str(key): public_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [public_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return {
            str(key): public_json_value(item)
            for key, item in attributes.items()
            if not str(key).startswith("_")
        }
    return str(value)


def public_redacted_value(value: Any) -> Any:
    """Remove private consistency fingerprints from user-facing output only."""

    converted = public_json_value(value)
    if isinstance(converted, dict):
        redacted: dict[str, Any] = {}
        for key, item in converted.items():
            lowered = str(key).lower()
            if (
                lowered.endswith("_fingerprint")
                or lowered.endswith("_sha256")
                or lowered == "digest"
                or lowered.endswith("_digest")
            ):
                continue
            redacted[str(key)] = public_redacted_value(item)
        return redacted
    if isinstance(converted, list):
        return [public_redacted_value(item) for item in converted]
    return converted


def public_json_dumps(value: Any, **kwargs: Any) -> str:
    return json.dumps(public_redacted_value(value), **kwargs)


def plan_field(plan: Any, name: str, default: Any) -> Any:
    if isinstance(plan, dict):
        return plan.get(name, default)
    return getattr(plan, name, default)


def plan_safety_warnings(plan: Any) -> list[dict[str, Any]]:
    """Normalize warning-first plans while retaining compatibility with blockers."""

    normalized: list[dict[str, Any]] = []

    def add(raw: Any, *, origin: str, severity: str, disposition: str) -> None:
        entries = raw if isinstance(raw, list) else ([] if raw is None else [raw])
        for entry in entries:
            if isinstance(entry, dict):
                warning = public_json_value(entry)
                warning.setdefault("code", origin)
                warning.setdefault("severity", severity)
                warning.setdefault("disposition", disposition)
                if "message" not in warning:
                    warning["message"] = str(
                        warning.get("detail", warning.get("reason", warning["code"]))
                    )
            else:
                warning = {
                    "code": origin,
                    "severity": severity,
                    "disposition": disposition,
                    "message": str(entry),
                }
            normalized.append(warning)

    add(
        plan_field(plan, "safety_warnings", []),
        origin="safety_warning",
        severity="warning",
        disposition="continue_or_retain_as_reported",
    )
    add(
        plan_field(plan, "warnings", []),
        origin="legacy_warning",
        severity="warning",
        disposition="continue_with_warning",
    )
    add(
        plan_field(plan, "blockers", []),
        origin="legacy_safety_gate",
        severity="critical",
        disposition="retain_affected_scope",
    )
    add(
        plan_field(plan, "unsafe_paths", []),
        origin="unsafe_managed_path",
        severity="critical",
        disposition="retain_affected_scope",
    )
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for warning in normalized:
        identity = json.dumps(warning, ensure_ascii=False, sort_keys=True)
        if identity not in seen:
            seen.add(identity)
            unique.append(warning)
    return unique


def _component_entries(raw: Any) -> list[dict[str, Any]]:
    value = public_json_value(raw)
    if isinstance(value, dict):
        entries: list[dict[str, Any]] = []
        for key, item in value.items():
            if isinstance(item, dict):
                entry = dict(item)
                entry.setdefault("component_id", str(key))
            else:
                entry = {"component_id": str(key), "value": item}
            entries.append(entry)
        return entries
    if isinstance(value, list):
        return [item if isinstance(item, dict) else {"value": item} for item in value]
    return []


def plan_component_summary(
    plan: Any, execution_snapshot: dict[str, Any] | None = None
) -> dict[str, Any]:
    target_ids = [str(item) for item in plan_field(plan, "target_ids", [])]
    snapshot = execution_snapshot if isinstance(execution_snapshot, dict) else {}
    components = _component_entries(
        snapshot.get("component_plans", plan_field(plan, "component_plans", []))
    )
    if snapshot:
        executable_ids = {
            str(item) for item in snapshot.get("executable_target_ids", [])
        }
        retained_ids = {str(item) for item in snapshot.get("retained_target_ids", [])}
    else:
        executable_ids = set()
        retained_ids = set()
    explicit_executable = plan_field(plan, "executable_target_ids", None)
    explicit_retained = plan_field(plan, "retained_target_ids", None)
    if not snapshot:
        if isinstance(explicit_executable, (list, tuple, set)):
            executable_ids.update(str(item) for item in explicit_executable)
        if isinstance(explicit_retained, (list, tuple, set)):
            retained_ids.update(str(item) for item in explicit_retained)
        dispositions = plan_field(plan, "target_dispositions", {})
        if isinstance(dispositions, dict):
            for sid, disposition_value in dispositions.items():
                disposition = (
                    disposition_value if isinstance(disposition_value, dict) else {}
                )
                status = str(disposition.get("status", "eligible"))
                if status == "eligible":
                    executable_ids.add(str(sid))
                elif status in {"retained", "requires_force_open"}:
                    retained_ids.add(str(sid))
    executable_components = 0
    retained_components = 0
    for component in components:
        component_ids = {
            str(item)
            for key in ("target_ids", "session_ids", "member_ids")
            for item in (
                component.get(key, [])
                if isinstance(component.get(key, []), list)
                else []
            )
        }
        disposition = str(
            component.get(
                "status",
                component.get("disposition", component.get("outcome", "")),
            )
        ).lower()
        executable = component.get("execution_ok", component.get("executable"))
        if executable is True or disposition in {
            "enabled",
            "execute",
            "executable",
            "ready",
            "continue",
        }:
            executable_components += 1
            executable_ids.update(component_ids)
        else:
            retained_components += 1
            retained_ids.update(component_ids)
    if not components and explicit_executable is None and explicit_retained is None:
        legacy_unsafe = bool(
            plan_field(plan, "blockers", []) or plan_field(plan, "unsafe_paths", [])
        )
        if legacy_unsafe:
            retained_ids.update(target_ids)
        else:
            executable_ids.update(target_ids)
    retained_ids.update(set(target_ids) - executable_ids)
    executable_ids.difference_update(retained_ids)
    retained_objects = public_json_value(
        snapshot.get("retained_units", plan_field(plan, "retained_objects", []))
    )
    executable_units = public_json_value(
        snapshot.get("executable_units", plan_field(plan, "executable_units", []))
    )
    return {
        "requested_target_count": len(target_ids),
        "executable_target_count": len(executable_ids),
        "retained_target_count": len(retained_ids),
        "executable_target_ids": sorted(executable_ids),
        "retained_target_ids": sorted(retained_ids),
        "component_count": len(components),
        "executable_component_count": executable_components,
        "retained_component_count": retained_components,
        "component_plans": components,
        "retained_objects": retained_objects,
        "executable_unit_count": (
            len(executable_units) if isinstance(executable_units, list) else 0
        ),
        "retained_object_count": (
            len(retained_objects) if isinstance(retained_objects, list) else 0
        ),
    }


def plan_execution_summary(
    plan: Any, execution_snapshot: dict[str, Any] | None = None
) -> dict[str, Any]:
    components = plan_component_summary(plan, execution_snapshot)
    return {
        "executable": {
            "target_count": components["executable_target_count"],
            "target_ids": components["executable_target_ids"],
            "component_count": components["executable_component_count"],
            "unit_count": components["executable_unit_count"],
        },
        "retained": {
            "target_count": components["retained_target_count"],
            "target_ids": components["retained_target_ids"],
            "component_count": components["retained_component_count"],
            "objects": components["retained_objects"],
            "object_count": components["retained_object_count"],
        },
    }


def approved_desktop_mutation_components(
    core: types.ModuleType,
    plan: Any,
    execution_snapshot: dict[str, Any] | None,
    historical_snapshot: dict[str, Any] | None = None,
) -> list[str]:
    """Return Desktop-owned components in the approved executable snapshot."""

    snapshot = execution_snapshot if isinstance(execution_snapshot, dict) else {}
    detector = getattr(core, "execution_snapshot_desktop_mutation_components", None)
    if callable(detector):
        detected = detector(snapshot)
        if detected is None:
            return []
        if not isinstance(detected, (list, tuple, set)):
            raise HelperError(
                "The core returned an unsupported approved Desktop component set."
            )
        components = {str(item) for item in detected if str(item)}
        historical_detector = getattr(
            core, "historical_snapshot_has_approved_work", None
        )
        if callable(historical_detector) and historical_detector(
            historical_snapshot or {}
        ):
            components.add("historical")
        return sorted(components)

    preflight = plan_field(plan, "preflight", {})
    if isinstance(preflight, dict) and preflight.get("desktop_offline_required"):
        return ["legacy_plan_preflight"]
    return []


def validate_request(value: Any, job_dir: Path) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != REQUEST_KEYS:
        raise RequestError("The handoff request has an unsupported schema.")
    if (
        value.get("schema_version") != REQUEST_SCHEMA_VERSION
        or value.get("protocol_version") != PROTOCOL_VERSION
    ):
        raise RequestError("The handoff request version is unsupported.")
    job_id = value.get("job_id")
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise RequestError("The handoff job ID is invalid.")
    if job_dir.name != job_id:
        raise RequestError("The request job ID does not match its private directory.")
    cleanup_job_ids = value.get("cleanup_job_ids")
    if (
        not isinstance(cleanup_job_ids, list)
        or len(cleanup_job_ids) > MAX_CLEANUP_JOB_IDS
        or any(
            not isinstance(item, str) or not JOB_ID_RE.fullmatch(item)
            for item in cleanup_job_ids
        )
        or cleanup_job_ids != list(dict.fromkeys(cleanup_job_ids))
        or job_id in cleanup_job_ids
    ):
        raise RequestError("cleanup_job_ids must be a bounded unique job-ID list.")
    created = value.get("created_at_epoch_ms")
    expires = value.get("expires_at_epoch_ms")
    if (
        isinstance(created, bool)
        or not isinstance(created, int)
        or isinstance(expires, bool)
        or not isinstance(expires, int)
        or expires <= created
        or expires - created > 86_400_000
    ):
        raise RequestError("The handoff request lifetime is invalid.")
    codex_home_raw = value.get("codex_home")
    if not isinstance(codex_home_raw, str) or not codex_home_raw:
        raise RequestError("codex_home is invalid.")
    codex_home = Path(codex_home_raw)
    if not codex_home.is_absolute() or str(codex_home.resolve()) != codex_home_raw:
        raise RequestError("codex_home must be an absolute canonical path.")
    session_ids = value.get("session_ids")
    if (
        not isinstance(session_ids, list)
        or not session_ids
        or len(session_ids) > 256
        or any(
            not isinstance(item, str) or not UUID_RE.fullmatch(item)
            for item in session_ids
        )
        or session_ids != sorted(dict.fromkeys(session_ids))
    ):
        raise RequestError("session_ids must be unique, sorted canonical UUIDs.")

    options = strict_bool_map(value.get("options"), OPTIONS_KEYS, "options")
    if (
        options["apply_missing_rollout_threads"]
        and not options["apply_historical_residuals"]
    ):
        raise RequestError(
            "apply_missing_rollout_threads requires apply_historical_residuals."
        )
    if options["apply_historical_residuals"] and not options["scan_historical"]:
        raise RequestError("Historical cleanup requires a historical scan.")

    timing_raw = value.get("timing")
    if not isinstance(timing_raw, dict) or set(timing_raw) != TIMING_KEYS:
        raise RequestError("timing has an unsupported schema.")
    timing = {
        "launch_delay_seconds": bounded_number(
            timing_raw["launch_delay_seconds"], "launch_delay_seconds", 0, 30
        ),
        "quit_timeout_seconds": bounded_number(
            timing_raw["quit_timeout_seconds"], "quit_timeout_seconds", 5, 3600
        ),
        "offline_stability_seconds": bounded_number(
            timing_raw["offline_stability_seconds"],
            "offline_stability_seconds",
            0.5,
            30,
        ),
        "poll_interval_seconds": bounded_number(
            timing_raw["poll_interval_seconds"], "poll_interval_seconds", 0.05, 5
        ),
        "restart_timeout_seconds": bounded_number(
            timing_raw["restart_timeout_seconds"], "restart_timeout_seconds", 1, 120
        ),
    }

    restart_raw = value.get("restart")
    if not isinstance(restart_raw, dict) or set(restart_raw) != RESTART_KEYS:
        raise RequestError("restart has an unsupported schema.")
    if type(restart_raw.get("requested")) is not bool:
        raise RequestError("restart.requested must be boolean.")
    bundle_id = restart_raw.get("bundle_id")
    if (
        not isinstance(bundle_id, str)
        or not BUNDLE_ID_RE.fullmatch(bundle_id)
        or bundle_id != DEFAULT_BUNDLE_ID
    ):
        raise RequestError("The requested Desktop bundle identifier is unsupported.")
    if restart_raw["requested"]:
        raise RequestError("This manual handoff never restarts Desktop.")

    exit_mode = value.get("exit_mode")
    if exit_mode != EXIT_MODE_MANUAL_GHOSTTY:
        raise RequestError("Only the visible manual Ghostty exit mode is supported.")

    source_raw = value.get("source")
    if not isinstance(source_raw, dict) or set(source_raw) != SOURCE_KEYS:
        raise RequestError("source has an unsupported schema.")
    core_path_raw = source_raw.get("core_path")
    core_sha = source_raw.get("core_sha256")
    helper_sha = source_raw.get("helper_sha256")
    interpreter_path_raw = source_raw.get("interpreter_path")
    interpreter_sha = source_raw.get("interpreter_sha256")
    interpreter_version_raw = source_raw.get("interpreter_version")
    expected_core = (Path(__file__).resolve().parent / CORE_SCRIPT_FILENAME).resolve()
    if (
        not isinstance(core_path_raw, str)
        or Path(core_path_raw) != expected_core
        or not isinstance(core_sha, str)
        or not re.fullmatch(r"[0-9a-f]{64}", core_sha)
        or not isinstance(helper_sha, str)
        or not re.fullmatch(r"[0-9a-f]{64}", helper_sha)
        or not isinstance(interpreter_path_raw, str)
        or not Path(interpreter_path_raw).is_absolute()
        or str(Path(interpreter_path_raw).resolve()) != interpreter_path_raw
        or not isinstance(interpreter_sha, str)
        or not re.fullmatch(r"[0-9a-f]{64}", interpreter_sha)
        or not isinstance(interpreter_version_raw, list)
        or len(interpreter_version_raw) != 3
        or any(
            isinstance(item, bool) or not isinstance(item, int)
            for item in interpreter_version_raw
        )
        or tuple(interpreter_version_raw[:2]) < (3, 10)
    ):
        raise RequestError("The approved source contract is invalid.")

    token = value.get("approval_token")
    if (
        not isinstance(token, str)
        or len(token) > MAX_TOKEN_CHARS
        or not TOKEN_RE.fullmatch(token)
    ):
        raise RequestError("The approval capsule is malformed.")

    validated = dict(value)
    validated["options"] = options
    validated["timing"] = timing
    validated["restart"] = {
        "requested": bool(restart_raw["requested"]),
        "bundle_id": bundle_id,
    }
    validated["exit_mode"] = exit_mode
    validated["source"] = {
        "core_path": str(expected_core),
        "core_sha256": core_sha,
        "helper_sha256": helper_sha,
        "interpreter_path": interpreter_path_raw,
        "interpreter_sha256": interpreter_sha,
        "interpreter_version": list(interpreter_version_raw),
    }
    validated["cleanup_job_ids"] = list(cleanup_job_ids)
    return validated


def safe_source_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SourceChangedError(
            f"Unable to open approved source file: {path}"
        ) from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
        ):
            raise SourceChangedError(f"Unsafe approved source file: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 256 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise SourceChangedError(
                f"Approved source identity changed while reading: {path}"
            )
        return b"".join(chunks)
    finally:
        os.close(fd)


def safe_interpreter_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SourceChangedError(
            f"Unable to open the approved Python interpreter: {path}"
        ) from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {0, os.geteuid()}
            or info.st_nlink < 1
            or not info.st_mode & 0o111
            or info.st_mode & 0o022
        ):
            raise SourceChangedError(
                f"The approved Python interpreter is not a safe executable: {path}"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 256 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise SourceChangedError(
                f"The approved Python interpreter changed while reading: {path}"
            )
        return b"".join(chunks)
    finally:
        os.close(fd)


def current_interpreter_contract() -> dict[str, Any]:
    version = [sys.version_info.major, sys.version_info.minor, sys.version_info.micro]
    if tuple(version[:2]) < (3, 10):
        raise SourceChangedError(
            "The offline helper requires Python 3.10 or newer to run the deletion engine."
        )
    path = Path(sys.executable).resolve()
    content = safe_interpreter_bytes(path)
    return {
        "interpreter_path": str(path),
        "interpreter_sha256": sha256_bytes(content),
        "interpreter_version": version,
    }


def verify_interpreter_contract(request: dict[str, Any]) -> None:
    approved = request["source"]
    current = current_interpreter_contract()
    for key in [
        "interpreter_path",
        "interpreter_sha256",
        "interpreter_version",
    ]:
        if current[key] != approved[key]:
            raise SourceChangedError(
                "The Python interpreter changed after the handoff was prepared."
            )


def verify_source_contract(request: dict[str, Any]) -> bytes:
    source = request["source"]
    verify_interpreter_contract(request)
    helper_bytes = safe_source_bytes(Path(__file__).resolve())
    if sha256_bytes(helper_bytes) != source["helper_sha256"]:
        raise SourceChangedError(
            "The offline helper changed after the handoff was prepared."
        )
    core_bytes = safe_source_bytes(Path(source["core_path"]))
    if sha256_bytes(core_bytes) != source["core_sha256"]:
        raise SourceChangedError("The deletion engine changed after approval.")
    return core_bytes


def load_verified_core_module(path: Path, source: bytes) -> types.ModuleType:
    module_name = f"_delete_codex_session_offline_{uuid.uuid4().hex}"
    module = types.ModuleType(module_name)
    module.__file__ = str(path)
    module.__package__ = ""
    sys.modules[module_name] = module
    try:
        code = compile(source, str(path), "exec")
        exec(code, module.__dict__)  # noqa: S102 - exact sibling bytes are SHA-bound
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def preflight_approved_request(request: dict[str, Any]) -> dict[str, Any]:
    """Rebuild and validate the approved plan without mutating or quitting Desktop."""

    core_source = verify_source_contract(request)
    core = load_verified_core_module(Path(request["source"]["core_path"]), core_source)
    try:
        options = request["options"]
        plan = core.make_plan(
            codex_home=Path(request["codex_home"]),
            root_ids=request["session_ids"],
            include_subagents=options["include_subagents"],
            include_logs=options["include_logs"],
            scan_historical=options["scan_historical"],
        )
        approval_payload = core.validated_approval_payload(
            plan,
            request["approval_token"],
            options["apply_historical_residuals"],
            options["apply_missing_rollout_threads"],
            options["force_open"],
        )
        if approval_payload is None:
            raise PlanChangedError(
                "The approval capsule no longer matches the current deletion plan."
            )
        safety_warnings = plan_safety_warnings(plan)
        execution_snapshot = approval_payload.get("execution_snapshot", {})
        if not isinstance(execution_snapshot, dict):
            execution_snapshot = {}
        component_summary = plan_component_summary(plan, execution_snapshot)
        execution_summary = plan_execution_summary(plan, execution_snapshot)
        historical_snapshot = approval_payload.get("historical_snapshot", {})
        if not isinstance(historical_snapshot, dict):
            historical_snapshot = {}
        desktop_mutation_components = approved_desktop_mutation_components(
            core, plan, execution_snapshot, historical_snapshot
        )
        desktop_offline_required = bool(desktop_mutation_components)
        current_missing_rollout_sessions = (
            core.missing_rollout_current_sessions(plan)
            if options["apply_missing_rollout_threads"]
            else []
        )
        if current_missing_rollout_sessions:
            safety_warnings.append(
                {
                    "code": "current_session_preserved",
                    "scope": "target",
                    "affected_ids": sorted(
                        str(item) for item in current_missing_rollout_sessions
                    ),
                    "affected_objects": [],
                    "reason": (
                        "The invoking session is protected and remains outside the "
                        "safe executable subset."
                    ),
                    "disposition": "skip_and_preserve",
                    "safe_operations_remaining": bool(
                        execution_summary["executable"]["target_count"]
                        or execution_summary["executable"]["unit_count"]
                    ),
                }
            )
        return {
            "validated": True,
            "target_plan_fingerprint": core.target_plan_fingerprint(plan),
            "target_ids": list(plan.target_ids),
            "target_count": len(plan.target_ids),
            "desktop_offline_required": desktop_offline_required,
            "desktop_mutation_components": desktop_mutation_components,
            "outcome": (
                OUTCOME_WAITING_OFFLINE
                if desktop_offline_required
                else OUTCOME_ROUTE_DIRECT
            ),
            "next_action": (
                NEXT_QUIT_AND_WAIT
                if desktop_offline_required
                else NEXT_ROUTE_TO_DIRECT_APPLY
            ),
            "safety_warnings": safety_warnings,
            "component_summary": component_summary,
            "execution_summary": execution_summary,
            "warning_count": len(safety_warnings),
            "checked_at_epoch_ms": now_epoch_ms(),
        }
    finally:
        sys.modules.pop(core.__name__, None)


def outer_app_bundle(executable: str) -> Path | None:
    path = Path(executable)
    candidates = [item for item in (path, *path.parents) if item.suffix == ".app"]
    return candidates[-1] if candidates else None


def desktop_process_role(
    bundle: Path,
    executable: Path,
    bundle_executable: Any,
) -> str:
    """Classify whether a bundle process can own mutable Codex session state."""

    if (
        isinstance(bundle_executable, str)
        and executable == bundle / "Contents" / "MacOS" / bundle_executable
    ):
        return "main_application"
    if executable == bundle / "Contents" / "Resources" / "codex":
        return "session_backend"
    return "auxiliary"


def desktop_owner_processes(
    bundle_id: str = DEFAULT_BUNDLE_ID,
) -> tuple[list[dict[str, Any]], str]:
    """Return same-user processes with evidence of mutable Desktop-state ownership.

    Merely running from the application bundle is not ownership evidence. Crash
    reporters, renderers, network services, plugin hosts, and other auxiliaries may
    legitimately outlive the main application and do not own the approved session
    catalog. The main application and the embedded Codex session backend do.
    """

    try:
        completed = subprocess.run(  # noqa: S603 - absolute system binary, fixed argv
            ["/bin/ps", "-axo", "pid=,uid=,comm="],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"Unable to inspect Codex Desktop processes: {exc}"
    owners: list[dict[str, Any]] = []
    issues: list[str] = []
    for raw_line in completed.stdout.splitlines():
        parts = raw_line.strip().split(maxsplit=2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pid, uid, executable = int(parts[0]), int(parts[1]), parts[2]
        if uid != os.geteuid():
            continue
        bundle = outer_app_bundle(executable)
        if bundle is None:
            continue
        plist_path = bundle / "Contents" / "Info.plist"
        try:
            with plist_path.open("rb") as stream:
                info = plistlib.load(stream)
            actual_bundle_id = info.get("CFBundleIdentifier")
            bundle_executable = info.get("CFBundleExecutable")
        except (OSError, plistlib.InvalidFileException):
            if bundle.name in {"ChatGPT.app", "Codex.app"}:
                issues.append(f"Unable to validate Desktop bundle for PID {pid}.")
            continue
        if actual_bundle_id == bundle_id:
            executable_path = Path(executable)
            process_role = desktop_process_role(
                bundle,
                executable_path,
                bundle_executable,
            )
            if process_role == "auxiliary":
                continue
            owners.append(
                {
                    "pid": pid,
                    "uid": uid,
                    "executable": executable,
                    "bundle_path": str(bundle),
                    "bundle_id": actual_bundle_id,
                    "process_role": process_role,
                    "is_main_application": process_role == "main_application",
                }
            )
        elif bundle.name in {"ChatGPT.app", "Codex.app"}:
            issues.append(
                f"Unexpected bundle identifier for a Desktop-like process PID {pid}."
            )
    if issues:
        return owners, " | ".join(sorted(dict.fromkeys(issues)))
    return owners, ""


def wait_for_desktop_offline(
    bundle_id: str,
    timeout_seconds: float,
    stability_seconds: float,
    poll_interval_seconds: float,
    sample_callback: Callable[[list[dict[str, Any]]], None] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    zero_since: float | None = None
    last_owners: list[dict[str, Any]] = []
    last_detection_issue = ""
    detection_issue_samples = 0
    samples = 0
    while True:
        owners, issue = desktop_owner_processes(bundle_id)
        samples += 1
        last_owners = owners
        if sample_callback is not None:
            sample_callback(owners)
        now = time.monotonic()
        if issue:
            detection_issue_samples += 1
            last_detection_issue = issue
            zero_since = None
        elif owners:
            zero_since = None
        elif zero_since is None:
            zero_since = now
        elif now - zero_since >= stability_seconds:
            return {
                "offline": True,
                "samples": samples,
                "stability_seconds": stability_seconds,
                "last_owners": [],
                "detection_issue_samples": detection_issue_samples,
                "last_detection_issue": last_detection_issue,
            }
        if now >= deadline:
            return {
                "offline": False,
                "samples": samples,
                "stability_seconds": 0.0 if zero_since is None else now - zero_since,
                "last_owners": last_owners,
                "detection_issue_samples": detection_issue_samples,
                "last_detection_issue": last_detection_issue,
            }
        time.sleep(poll_interval_seconds)


class ReceiptWriter:
    def __init__(self, job_dir: Path, initial: dict[str, Any] | None = None) -> None:
        self.path = job_dir / RECEIPT_FILENAME
        self._lock = threading.Lock()
        if initial is not None:
            self._payload = dict(initial)
        elif self.path.exists():
            self._payload = read_private_json(self.path, maximum=MAX_RECEIPT_BYTES)
        else:
            self._payload = {
                "schema_version": RECEIPT_SCHEMA_VERSION,
                "protocol_version": PROTOCOL_VERSION,
                "job_id": job_dir.name,
                "phase": "worker_starting",
                "outcome": OUTCOME_RETRYABLE_WARNING,
                "next_action": NEXT_INSPECT_NO_RELAUNCH,
                "terminal": False,
                "mutation_started": False,
                "success": False,
                "deletion_success": False,
                "verification_ok": False,
                "partial_possible": False,
                "errors": [],
            }
        self.write()

    @property
    def payload(self) -> dict[str, Any]:
        return self._payload

    def write(self) -> None:
        atomic_write_json(self.path, self._payload)

    def update(self, **values: Any) -> None:
        self.transition(**values)

    def transition(self, *, clear: Iterable[str] = (), **values: Any) -> None:
        with self._lock:
            for key in clear:
                self._payload.pop(key, None)
            self._payload.update(values)
            self._payload["updated_at_epoch_ms"] = now_epoch_ms()
            self.write()

    def add_error(self, phase: str, code: str, message: str) -> None:
        with self._lock:
            errors = list(self._payload.get("errors", []))
            errors.append(
                {
                    "phase": phase,
                    "code": code,
                    "message": message[:4000],
                }
            )
            self._payload["errors"] = errors
            self._payload["updated_at_epoch_ms"] = now_epoch_ms()
            self.write()

    def add_warning(
        self,
        phase: str,
        code: str,
        message: str,
        *,
        disposition: str = "continue_with_warning",
    ) -> None:
        with self._lock:
            warnings = list(self._payload.get("safety_warnings", []))
            warnings.append(
                {
                    "phase": phase,
                    "code": code,
                    "severity": "warning",
                    "disposition": disposition,
                    "message": message[:4000],
                }
            )
            self._payload["safety_warnings"] = warnings[-200:]
            self._payload["updated_at_epoch_ms"] = now_epoch_ms()
            self.write()

    def add_notice(
        self,
        phase: str,
        code: str,
        message: str,
        *,
        disposition: str,
    ) -> None:
        """Record an operational gate/wait notice outside safety warnings."""

        with self._lock:
            notices = list(self._payload.get("operational_notices", []))
            notices.append(
                {
                    "phase": phase,
                    "code": code,
                    "disposition": disposition,
                    "message": message[:4000],
                }
            )
            self._payload["operational_notices"] = notices[-200:]
            self._payload["updated_at_epoch_ms"] = now_epoch_ms()
            self.write()


def public_request_metadata(request: dict[str, Any]) -> dict[str, Any]:
    options = request["options"]
    if options["apply_missing_rollout_threads"]:
        scope = "targets_historical_and_missing_rollout_threads"
    elif options["apply_historical_residuals"]:
        scope = "targets_and_historical_residuals"
    else:
        scope = "targets_only"
    if options["force_open"]:
        scope += "_force_open"
    return {
        "codex_home": request["codex_home"],
        "session_ids": request["session_ids"],
        "approval_scope": scope,
        "options": options,
        "exit_mode": request["exit_mode"],
        "restart_requested": request["restart"]["requested"],
        "bundle_id": request["restart"]["bundle_id"],
        "created_at_epoch_ms": request["created_at_epoch_ms"],
        "expires_at_epoch_ms": request["expires_at_epoch_ms"],
        "cleanup_job_ids": list(request.get("cleanup_job_ids", [])),
        "source": {
            "core_path": request["source"]["core_path"],
            "core_sha256": request["source"]["core_sha256"],
            "helper_sha256": request["source"]["helper_sha256"],
            "interpreter_path": request["source"]["interpreter_path"],
            "interpreter_sha256": request["source"]["interpreter_sha256"],
            "interpreter_version": request["source"]["interpreter_version"],
        },
    }


def sanitize_text(value: str, token: str) -> str:
    sanitized = value.replace(token, "<approval-capsule-redacted>") if token else value
    if len(sanitized.encode("utf-8", errors="replace")) > 16 * 1024:
        sanitized = sanitized[: 16 * 1024] + "…<truncated>"
    return sanitized


def sanitize_value(value: Any, token: str) -> Any:
    if isinstance(value, dict):
        return {
            key: sanitize_value(item, token)
            for key, item in value.items()
            if key not in {"approval_tokens", "approval_token", "confirm_plan"}
        }
    if isinstance(value, list):
        return [sanitize_value(item, token) for item in value]
    if isinstance(value, str):
        return sanitize_text(value, token)
    return value


def summarize_core_payload(
    payload: dict[str, Any] | None, token: str
) -> dict[str, Any] | None:
    if payload is None:
        return None
    summary: dict[str, Any] = {}
    for key in ["mode", "error", "exit_code"]:
        if key in payload:
            summary[key] = sanitize_value(payload[key], token)
    plan = payload.get("plan")
    if isinstance(plan, dict):
        summary["plan"] = sanitize_value(
            {
                key: plan[key]
                for key in [
                    "script_version",
                    "plan_contract_version",
                    "approval_contract_version",
                    "plan_fingerprint",
                    "root_ids",
                    "target_ids",
                    "safety_warnings",
                    "component_plans",
                    "retained_objects",
                    "counts",
                ]
                if key in plan
            },
            token,
        )
    if "apply_result" in payload:
        summary["apply_result"] = sanitize_value(payload["apply_result"], token)
    details = payload.get("details")
    if isinstance(details, dict) and "partial_apply_result" in details:
        summary["partial_apply_result"] = sanitize_value(
            details["partial_apply_result"], token
        )
    return summary


def core_args_namespace(request: dict[str, Any], token: str) -> argparse.Namespace:
    options = request["options"]
    return argparse.Namespace(
        session_ids=request["session_ids"],
        codex_home=request["codex_home"],
        apply=True,
        confirm_plan=token,
        no_subagents=not options["include_subagents"],
        no_logs=not options["include_logs"],
        force_open=options["force_open"],
        no_historical_scan=not options["scan_historical"],
        apply_historical_residuals=options["apply_historical_residuals"],
        apply_missing_rollout_threads=options["apply_missing_rollout_threads"],
        json=True,
    )


def invoke_core_main(
    core: types.ModuleType,
    request: dict[str, Any],
    token: str,
    writer: ReceiptWriter,
) -> CoreInvocationResult:
    original_parse_args = core.parse_args
    original_apply_plan = core.apply_plan
    original_core_owner_detector = getattr(core, "desktop_owner_processes", None)
    pre_apply_owner_blocked = False
    owner_reappeared = threading.Event()
    owner_monitor_issue: list[str] = []
    stop_monitor = threading.Event()
    monitor_thread: threading.Thread | None = None
    owner_samples: list[dict[str, Any]] = []
    mutation_observer_setter: Callable[..., Any] | None = None
    mutation_observer_unsubscribe: Callable[[], Any] | None = None
    mutation_observer_clear_with_none = False

    def mutation_observer(*observer_args: Any, **observer_kwargs: Any) -> None:
        if writer.payload.get("mutation_started") is True:
            return
        event = str(
            observer_kwargs.get(
                "event",
                observer_kwargs.get(
                    "phase",
                    observer_args[0] if observer_args else "mutation_started",
                ),
            )
        ).lower()
        explicitly_started = observer_kwargs.get("mutation_started") is True
        observed_component = str(
            observer_kwargs.get(
                "component", observer_args[0] if observer_args else "unknown"
            )
        )
        if observer_kwargs.get("mutation_started") is not False and (
            explicitly_started
            or bool(observer_args)
            or bool(observer_kwargs)
            or event
            in {
                "mutation_started",
                "first_write",
                "before_first_write",
                "write_started",
            }
        ):
            writer.transition(
                clear=("retryable", "completed_at_epoch_ms"),
                phase="mutation_started",
                outcome=OUTCOME_PARTIAL_POSSIBLE,
                next_action=NEXT_KEEP_CLOSED,
                mutation_started=True,
                partial_possible=True,
                mutation_started_at_epoch_ms=now_epoch_ms(),
                mutation_component=observed_component,
            )

    def monitor() -> None:
        while not stop_monitor.wait(request["timing"]["poll_interval_seconds"]):
            owners, issue = desktop_owner_processes(request["restart"]["bundle_id"])
            if issue:
                owner_monitor_issue.append(issue)
                owner_reappeared.set()
                return
            if owners:
                owner_samples.extend(owners[:20])
                owner_reappeared.set()
                return

    def verified_core_owner_detector(
        _codex_home: Any,
    ) -> tuple[list[dict[str, Any]], str]:
        owners, issue = desktop_owner_processes(request["restart"]["bundle_id"])
        if issue:
            owner_monitor_issue.append(issue)
            owner_reappeared.set()
        if owners:
            owner_samples.extend(owners[:20])
            owner_reappeared.set()
        return owners, issue

    def wrapped_apply_plan(*args: Any, **kwargs: Any) -> Any:
        nonlocal pre_apply_owner_blocked, monitor_thread
        owners, issue = desktop_owner_processes(request["restart"]["bundle_id"])
        if issue or owners:
            pre_apply_owner_blocked = True
            raise DesktopOfflineError(
                issue or "Codex Desktop restarted immediately before mutation."
            )
        monitor_thread = threading.Thread(
            target=monitor,
            name="codex-desktop-owner-monitor",
            daemon=True,
        )
        monitor_thread.start()
        try:
            code = getattr(original_apply_plan, "__code__", None)
            parameter_names = (
                code.co_varnames[: code.co_argcount + code.co_kwonlyargcount]
                if code is not None
                else ()
            )
            if (
                "mutation_observer" not in kwargs
                and "mutation_observer" in parameter_names
            ):
                kwargs["mutation_observer"] = mutation_observer
            return original_apply_plan(*args, **kwargs)
        finally:
            stop_monitor.set()
            if monitor_thread is not None:
                monitor_thread.join(timeout=2)

    core.parse_args = lambda: core_args_namespace(request, token)
    core.apply_plan = wrapped_apply_plan
    if callable(original_core_owner_detector):
        core.desktop_owner_processes = verified_core_owner_detector
    for observer_name in ("set_mutation_observer", "register_mutation_observer"):
        candidate = getattr(core, observer_name, None)
        if not callable(candidate):
            continue
        try:
            possible_unsubscribe = candidate(mutation_observer)
            mutation_observer_setter = candidate
            mutation_observer_clear_with_none = observer_name == "set_mutation_observer"
            if callable(possible_unsubscribe):
                mutation_observer_unsubscribe = possible_unsubscribe
        except Exception as exc:
            writer.add_notice(
                "core_setup",
                "mutation_observer_unavailable",
                f"The core mutation observer could not be registered: {exc}",
                disposition="use_core_result_marker",
            )
        break
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            raw_exit_code = core.main()
        exit_code = int(raw_exit_code)
    except SystemExit as exc:
        exit_code = int(exc.code) if isinstance(exc.code, int) else EXIT_BLOCKED
    finally:
        core.parse_args = original_parse_args
        core.apply_plan = original_apply_plan
        if callable(original_core_owner_detector):
            core.desktop_owner_processes = original_core_owner_detector
        if mutation_observer_unsubscribe is not None:
            try:
                mutation_observer_unsubscribe()
            except Exception:
                pass
        elif mutation_observer_setter is not None and mutation_observer_clear_with_none:
            try:
                mutation_observer_setter(None)
            except Exception:
                pass
        stop_monitor.set()
        if monitor_thread is not None and monitor_thread.is_alive():
            monitor_thread.join(timeout=2)
    final_owners, final_owner_issue = desktop_owner_processes(
        request["restart"]["bundle_id"]
    )
    if final_owner_issue:
        owner_monitor_issue.append(final_owner_issue)
        owner_reappeared.set()
    if final_owners:
        owner_samples.extend(final_owners[:20])
        owner_reappeared.set()
    stdout_value = stdout.getvalue()
    stderr_value = sanitize_text(stderr.getvalue(), token)
    output_valid = False
    payload: dict[str, Any] | None = None
    if len(stdout_value.encode("utf-8", errors="replace")) <= MAX_CAPTURE_BYTES:
        try:
            decoded = json.loads(stdout_value)
            if isinstance(decoded, dict):
                payload = decoded
                output_valid = True
        except (UnicodeError, json.JSONDecodeError):
            pass
    if owner_samples:
        writer.update(owner_reappeared_sample=owner_samples[:20])
    return CoreInvocationResult(
        exit_code=exit_code,
        payload=payload,
        stdout_sha256=sha256_bytes(stdout_value.encode("utf-8", errors="replace")),
        stderr=stderr_value,
        output_valid=output_valid,
        owner_reappeared=owner_reappeared.is_set(),
        owner_monitor_issue=" | ".join(sorted(dict.fromkeys(owner_monitor_issue))),
        pre_apply_owner_blocked=pre_apply_owner_blocked,
    )


def core_apply_result(result: CoreInvocationResult) -> dict[str, Any]:
    if not result.output_valid or result.payload is None:
        return {}
    value = result.payload.get("apply_result")
    return value if isinstance(value, dict) else {}


def core_result_mutation_started(result: CoreInvocationResult) -> bool:
    candidates: list[Any] = []
    apply_result = core_apply_result(result)
    candidates.extend(
        [
            apply_result.get("mutation_started"),
            apply_result.get("partial_possible"),
        ]
    )
    execution = apply_result.get("execution")
    if isinstance(execution, dict):
        candidates.append(execution.get("mutation_started"))
    component_results = apply_result.get("component_results")
    if isinstance(component_results, dict):
        component_values = list(component_results.values())
    elif isinstance(component_results, list):
        component_values = component_results
    else:
        component_values = []
    for component in component_values:
        if isinstance(component, dict):
            candidates.extend(
                [component.get("mutation_started"), component.get("committed")]
            )
    if result.payload is not None:
        details = result.payload.get("details")
        if isinstance(details, dict):
            partial = details.get("partial_apply_result")
            if isinstance(partial, dict):
                candidates.extend(
                    [partial.get("mutation_started"), partial.get("partial_possible")]
                )
    return any(value is True for value in candidates)


def core_result_component_summary(result: CoreInvocationResult) -> dict[str, Any]:
    apply_result = core_apply_result(result)
    raw = apply_result.get("component_results", [])
    public = public_json_value(raw)
    values = list(public.values()) if isinstance(public, dict) else public
    if not isinstance(values, list):
        values = []
    executed = 0
    retained = 0
    failed = 0
    for component in values:
        if not isinstance(component, dict):
            continue
        outcome = str(component.get("outcome", component.get("status", ""))).lower()
        if component.get("execution_ok") is True or outcome in {
            "complete",
            "completed",
            "completed_with_warnings",
            "executed",
        }:
            executed += 1
        elif outcome in {"failed", "partial", "partial_possible"}:
            failed += 1
        elif outcome in {"skipped_safely", "not_requested", "retained"}:
            retained += 1
        else:
            retained += 1
    return {
        "component_count": len(values),
        "executed_component_count": executed,
        "retained_component_count": retained,
        "failed_component_count": failed,
        "component_results": public,
    }


def core_result_status(result: CoreInvocationResult) -> dict[str, Any]:
    apply_result = core_apply_result(result)
    verification = apply_result.get("verification")
    verification_ok = isinstance(verification, dict) and bool(
        verification.get("verification_ok")
    )
    core_outcome = str(apply_result.get("outcome", "")).strip()
    safe_outcomes = {OUTCOME_COMPLETE, OUTCOME_COMPLETE_WITH_WARNINGS}
    execution_ok = apply_result.get("execution_ok") is True
    legacy_success = apply_result.get("success") is True
    safe_completion = (
        result.exit_code == 0
        and result.output_valid
        and verification_ok
        and (
            core_outcome in safe_outcomes
            or not core_outcome
            and (execution_ok or legacy_success)
        )
    )
    controlled_no_safe_work = (
        result.exit_code == 0
        and result.output_valid
        and verification_ok
        and core_outcome == OUTCOME_NO_SAFE_WORK
        and not core_result_mutation_started(result)
    )
    warnings: list[Any] = []
    for source in [
        apply_result.get("safety_warnings"),
        result.payload.get("plan", {}).get("safety_warnings")
        if isinstance(result.payload, dict)
        and isinstance(result.payload.get("plan"), dict)
        else None,
    ]:
        if isinstance(source, list):
            warnings.extend(public_json_value(source))
    component_summary = core_result_component_summary(result)
    completed_with_warnings = bool(warnings)
    if safe_completion:
        outcome = (
            OUTCOME_COMPLETE_WITH_WARNINGS
            if core_outcome == OUTCOME_COMPLETE_WITH_WARNINGS
            or completed_with_warnings
            else OUTCOME_COMPLETE
        )
        next_action = NEXT_REOPEN
    elif controlled_no_safe_work:
        outcome = OUTCOME_NO_SAFE_WORK
        next_action = NEXT_REOPEN
    elif core_result_mutation_started(result):
        outcome = OUTCOME_PARTIAL_POSSIBLE
        next_action = NEXT_KEEP_CLOSED
    elif core_outcome == "plan_changed" or result.exit_code == EXIT_PLAN_CHANGED:
        outcome = OUTCOME_RESTAGE_REQUIRED
        next_action = NEXT_RESTAGE
    else:
        outcome = OUTCOME_FAILED
        next_action = NEXT_FIX_INPUT
    return {
        "safe_completion": safe_completion,
        "controlled_no_safe_work": controlled_no_safe_work,
        "verification_ok": verification_ok,
        "mutation_started": core_result_mutation_started(result),
        "outcome": outcome,
        "next_action": next_action,
        "safety_warnings": warnings,
        "component_summary": component_summary,
    }


def core_result_is_success(result: CoreInvocationResult) -> tuple[bool, bool]:
    status = core_result_status(result)
    return bool(status["safe_completion"]), bool(status["verification_ok"])


def acquire_worker_lock(job_dir: Path) -> int:
    path = job_dir / LOCK_FILENAME
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    os.fchmod(fd, 0o600)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
    ):
        os.close(fd)
        raise HelperError("The worker lock file is unsafe.")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise HelperError("This handoff job is already running.") from exc
    return fd


def terminal_receipt_exit_code(receipt: dict[str, Any]) -> int:
    recorded = receipt.get("helper_exit_code")
    if isinstance(recorded, int) and recorded in {
        EXIT_OK,
        EXIT_BLOCKED,
        EXIT_PLAN_CHANGED,
        EXIT_PARTIAL_OR_VERIFY_FAILED,
    }:
        return recorded
    phase = receipt.get("phase")
    if receipt.get("deletion_success") is True and phase == "complete":
        return EXIT_OK
    if phase == "plan_changed":
        return EXIT_PLAN_CHANGED
    if phase in {"partial_or_verification_failed"}:
        return EXIT_PARTIAL_OR_VERIFY_FAILED
    if phase == "engine_failed_before_mutation":
        return EXIT_BLOCKED
    return EXIT_BLOCKED


def receipt_with_stable_action(receipt: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(receipt)
    phase = str(enriched.get("phase", ""))
    raw_outcome = enriched.get("outcome")
    mutation_started = enriched.get("mutation_started") is True
    request_consumed = enriched.get("request_consumed") is True
    safe_outcomes = {OUTCOME_COMPLETE, OUTCOME_COMPLETE_WITH_WARNINGS}

    if phase == "complete":
        if (
            raw_outcome in safe_outcomes
            and enriched.get("deletion_success") is True
            and enriched.get("verification_ok") is True
        ):
            outcome = raw_outcome
        elif (
            raw_outcome == OUTCOME_NO_SAFE_WORK
            and enriched.get("verification_ok") is True
            and not mutation_started
        ):
            outcome = OUTCOME_NO_SAFE_WORK
        elif (
            enriched.get("deletion_success") is True
            and enriched.get("verification_ok") is True
        ):
            outcome = (
                OUTCOME_COMPLETE_WITH_WARNINGS
                if enriched.get("safety_warnings")
                else OUTCOME_COMPLETE
            )
        else:
            outcome = OUTCOME_PARTIAL_POSSIBLE if mutation_started else OUTCOME_FAILED
    elif phase == "partial_or_verification_failed" or mutation_started:
        outcome = OUTCOME_PARTIAL_POSSIBLE
    elif phase in {"prepared", "staged_waiting_for_ghostty_launch"}:
        outcome = OUTCOME_STAGED
    elif phase == "route_to_direct_apply":
        outcome = OUTCOME_ROUTE_DIRECT
    elif phase in {
        "worker_starting",
        "terminal_launch_submitted",
        "launch_unknown",
        "reading_staged_request",
        "request_validated_pending_manual_exit",
        "launch_delay",
        "waiting_for_manual_exit",
        "waiting_for_offline",
        "offline_observed",
        "request_consumed_offline",
        "revalidating_approval",
        "core_finished",
        "manual_offline_wait_failed",
        "terminal_launch_failed_before_worker",
    }:
        outcome = OUTCOME_WAITING_OFFLINE
    elif phase in {"plan_changed", "expired_before_mutation"}:
        outcome = OUTCOME_RESTAGE_REQUIRED
    elif phase == "cancelled_before_mutation":
        outcome = OUTCOME_CANCELLED
    elif isinstance(raw_outcome, str) and raw_outcome in ALLOWED_OUTCOMES:
        outcome = raw_outcome
    else:
        outcome = OUTCOME_FAILED

    if outcome == OUTCOME_PARTIAL_POSSIBLE:
        next_action = NEXT_KEEP_CLOSED
    elif phase == "cancelled_before_mutation":
        next_action = NEXT_NONE
    elif phase in {"prepared", "staged_waiting_for_ghostty_launch"}:
        next_action = NEXT_LAUNCH_GHOSTTY
    elif phase == "route_to_direct_apply":
        next_action = NEXT_ROUTE_TO_DIRECT_APPLY
    elif phase == "launch_unknown" or phase == "worker_starting":
        next_action = NEXT_INSPECT_NO_RELAUNCH
    elif phase in {
        "manual_offline_wait_failed",
        "terminal_launch_failed_before_worker",
    } and not request_consumed:
        next_action = NEXT_RETRY_LAUNCH
    elif phase in {
        "request_consumed_offline",
        "revalidating_approval",
        "core_finished",
        "offline_observed",
    } or request_consumed:
        next_action = NEXT_INSPECT_NO_RELAUNCH
    elif outcome == OUTCOME_WAITING_OFFLINE:
        next_action = NEXT_QUIT_AND_WAIT
    elif enriched.get("cleanup_pending") is True:
        next_action = NEXT_RETRY_CLEANUP
    elif outcome in safe_outcomes | {OUTCOME_NO_SAFE_WORK} and phase == "complete":
        next_action = NEXT_REOPEN
    elif outcome == OUTCOME_RESTAGE_REQUIRED:
        next_action = NEXT_RESTAGE
    elif outcome == OUTCOME_FAILED:
        next_action = NEXT_FIX_INPUT
    elif outcome == OUTCOME_STAGED:
        next_action = NEXT_CHOOSE_SCOPE
    else:
        next_action = NEXT_NONE

    verified_success = (
        phase == "complete"
        and outcome in safe_outcomes
        and enriched.get("deletion_success") is True
        and enriched.get("verification_ok") is True
    )
    controlled_no_safe_work = (
        phase == "complete"
        and outcome == OUTCOME_NO_SAFE_WORK
        and enriched.get("verification_ok") is True
        and not mutation_started
    )
    enriched["outcome"] = outcome
    enriched["next_action"] = next_action
    enriched["success"] = verified_success
    enriched["deletion_success"] = verified_success
    enriched["partial_possible"] = outcome == OUTCOME_PARTIAL_POSSIBLE
    enriched["retryable"] = next_action == NEXT_RETRY_LAUNCH
    enriched["safe_to_reopen"] = (
        verified_success
        or controlled_no_safe_work
        or (
            not mutation_started
            and (
                outcome in {OUTCOME_STAGED, OUTCOME_RESTAGE_REQUIRED, OUTCOME_FAILED}
                or phase
                in {
                    "route_to_direct_apply",
                    "cancelled_before_mutation",
                    "manual_offline_wait_failed",
                    "terminal_launch_failed_before_worker",
                }
            )
        )
    )
    if outcome == OUTCOME_NO_SAFE_WORK:
        enriched["permanent_deletion_complete"] = False
    if outcome == OUTCOME_PARTIAL_POSSIBLE:
        enriched["retryable"] = False
    return enriched


def run_worker(job_dir: Path) -> int:
    job_dir = job_dir.resolve()
    secure_directory(job_dir)
    if not JOB_ID_RE.fullmatch(job_dir.name):
        raise HelperError("Invalid private job directory name.")
    lock_fd = acquire_worker_lock(job_dir)
    try:
        existing_receipt = read_private_json(
            job_dir / RECEIPT_FILENAME, maximum=MAX_RECEIPT_BYTES
        )
    except Exception:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        raise
    if (
        existing_receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION
        or existing_receipt.get("protocol_version") != PROTOCOL_VERSION
        or existing_receipt.get("job_id") != job_dir.name
    ):
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        raise HelperError(
            "This handoff uses a legacy or unsupported receipt; stage a new job."
        )
    if existing_receipt.get("terminal") is True:
        exit_code = terminal_receipt_exit_code(existing_receipt)
        try:
            if (job_dir / REQUEST_FILENAME).exists():
                safely_remove_unconsumed_request(job_dir)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        return exit_code
    approved_phases = {
        "terminal_launch_submitted",
        "terminal_launch_failed_before_worker",
        "launch_unknown",
        "reading_staged_request",
        "request_validated_pending_manual_exit",
        "launch_delay",
        "waiting_for_manual_exit",
        "waiting_for_offline",
        "offline_observed",
        "manual_offline_wait_failed",
    }
    launch_attempts = existing_receipt.get("terminal_launch_attempts")
    if (
        existing_receipt.get("final_approval_recorded") is not True
        or isinstance(launch_attempts, bool)
        or not isinstance(launch_attempts, int)
        or launch_attempts < 1
        or existing_receipt.get("phase") not in approved_phases
    ):
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        raise HelperError(
            "This staged job has not passed the bound approval and Ghostty launch gate."
        )
    expected_request_sha256 = existing_receipt.get("request_sha256")
    if not isinstance(expected_request_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_request_sha256
    ):
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        raise RequestError("The prepared request receipt lacks a valid content hash.")
    writer = ReceiptWriter(job_dir)
    request: dict[str, Any] | None = None
    core: types.ModuleType | None = None
    token = ""
    try:
        writer.update(
            phase="reading_staged_request",
            outcome=OUTCOME_WAITING_OFFLINE,
            next_action=NEXT_QUIT_AND_WAIT,
            worker_pid=os.getpid(),
            request_consumed=False,
        )
        raw = read_private_bytes(job_dir / REQUEST_FILENAME)
        actual_request_sha256 = sha256_bytes(raw)
        if actual_request_sha256 != expected_request_sha256:
            raise RequestError(
                "The private request content changed after the handoff was prepared."
            )
        try:
            decoded = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RequestError("The staged handoff request is invalid JSON.") from exc
        request = validate_request(decoded, job_dir)
        token = request["approval_token"]
        writer.update(
            phase="request_validated_pending_manual_exit",
            outcome=OUTCOME_WAITING_OFFLINE,
            next_action=NEXT_QUIT_AND_WAIT,
            request=public_request_metadata(request),
            request_integrity_verified=True,
        )
        if now_epoch_ms() >= request["expires_at_epoch_ms"]:
            raise ExpiredRequestError(
                "The approved handoff expired before Desktop shutdown."
            )
        verify_source_contract(request)
        writer.update(
            phase="launch_delay",
            outcome=OUTCOME_WAITING_OFFLINE,
            next_action=NEXT_QUIT_AND_WAIT,
            source_verified_before_manual_exit=True,
        )
        raw = b""
        decoded = None
        time.sleep(request["timing"]["launch_delay_seconds"])
        if now_epoch_ms() >= request["expires_at_epoch_ms"]:
            raise ExpiredRequestError(
                "The approved handoff expired before Desktop shutdown."
            )

        owners, owner_issue = desktop_owner_processes(request["restart"]["bundle_id"])
        if owner_issue:
            writer.add_notice(
                "waiting_for_manual_exit",
                "desktop_owner_detection_retry",
                owner_issue,
                disposition="wait_for_verified_offline_sample",
            )
        writer.update(
            phase="waiting_for_manual_exit" if owners else "waiting_for_offline",
            outcome=OUTCOME_WAITING_OFFLINE,
            next_action=NEXT_QUIT_AND_WAIT,
            initial_owner_count=len(owners),
            initial_owners=owners[:50],
            desktop_exit_mode=request["exit_mode"],
            graceful_quit_requested=False,
            automatic_restart_requested=False,
        )

        last_progress_signature: tuple[tuple[int, str], ...] | None = None

        def show_offline_progress(current_owners: list[dict[str, Any]]) -> None:
            nonlocal last_progress_signature
            signature = tuple(
                sorted(
                    (
                        int(owner.get("pid", 0) or 0),
                        str(owner.get("process_role", "state_owner")),
                    )
                    for owner in current_owners
                )
            )
            if signature == last_progress_signature:
                return
            last_progress_signature = signature
            if current_owners:
                roles = ", ".join(
                    sorted(
                        {
                            str(owner.get("process_role", "state_owner"))
                            for owner in current_owners
                        }
                    )
                )
                print(
                    "仍在等待状态所有者退出："
                    f"{len(current_owners)} 个（{roles}）。",
                    flush=True,
                )
            else:
                print(
                    "[2/4] 已检测到桌面状态所有者离线，正在确认稳定状态。",
                    flush=True,
                )

        offline = wait_for_desktop_offline(
            request["restart"]["bundle_id"],
            request["timing"]["quit_timeout_seconds"],
            request["timing"]["offline_stability_seconds"],
            request["timing"]["poll_interval_seconds"],
            sample_callback=show_offline_progress,
        )
        if offline.get("detection_issue_samples"):
            writer.add_notice(
                "waiting_for_offline",
                "desktop_owner_detection_recovered",
                str(
                    offline.get("last_detection_issue")
                    or "Desktop owner detection recovered after transient failures."
                ),
                disposition="continued_after_stable_verified_offline_samples",
            )
        writer.update(
            phase="offline_observed",
            outcome=OUTCOME_WAITING_OFFLINE,
            next_action=NEXT_QUIT_AND_WAIT,
            desktop_offline=offline,
        )
        if not offline["offline"]:
            raise DesktopOfflineError(
                "Codex Desktop did not fully exit before the approved timeout."
            )
        if now_epoch_ms() >= request["expires_at_epoch_ms"]:
            raise ExpiredRequestError(
                "The approved handoff expired before plan revalidation."
            )
        owners, owner_issue = desktop_owner_processes(request["restart"]["bundle_id"])
        if owner_issue or owners:
            raise DesktopOfflineError(
                owner_issue or "Codex Desktop reappeared before capsule consumption."
            )

        raw = consume_private_request(job_dir / REQUEST_FILENAME)
        if sha256_bytes(raw) != expected_request_sha256:
            raise RequestError(
                "The private request content changed before offline execution."
            )
        try:
            decoded = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RequestError("The consumed handoff request is invalid JSON.") from exc
        request = validate_request(decoded, job_dir)
        token = request["approval_token"]
        writer.update(
            phase="request_consumed_offline",
            outcome=OUTCOME_WAITING_OFFLINE,
            next_action=NEXT_INSPECT_NO_RELAUNCH,
            request_consumed=True,
            request_consumed_at_epoch_ms=now_epoch_ms(),
        )
        raw = b""
        decoded = None

        core_source = verify_source_contract(request)
        core = load_verified_core_module(
            Path(request["source"]["core_path"]), core_source
        )
        writer.update(
            phase="revalidating_approval",
            outcome=OUTCOME_WAITING_OFFLINE,
            next_action=NEXT_INSPECT_NO_RELAUNCH,
            plan_revalidated=False,
        )
        print(
            "[3/4] 离线状态稳定且私有请求已核验，正在删除批准对象并完成验证。",
            flush=True,
        )
        result = invoke_core_main(core, request, token, writer)
        summary = summarize_core_payload(result.payload, token)
        result_status = core_result_status(result)
        success = bool(result_status["safe_completion"])
        controlled_no_safe_work = bool(
            result_status["controlled_no_safe_work"]
        )
        verification_ok = bool(result_status["verification_ok"])
        mutation_started = bool(
            writer.payload.get("mutation_started")
            or result_status["mutation_started"]
        )
        if mutation_started and not writer.payload.get("mutation_started"):
            writer.transition(
                clear=("retryable", "completed_at_epoch_ms"),
                mutation_started=True,
                partial_possible=True,
                mutation_started_at_epoch_ms=now_epoch_ms(),
            )
        receipt_warnings = list(writer.payload.get("safety_warnings", []))
        receipt_warnings.extend(result_status["safety_warnings"])
        component_summary = result_status["component_summary"]
        if result.owner_reappeared:
            success = False
            controlled_no_safe_work = False
            verification_ok = False
            writer.add_error(
                "apply",
                "desktop_owner_reappeared",
                result.owner_monitor_issue
                or "Codex Desktop reappeared while the deletion engine was running.",
            )
        writer.update(
            phase="core_finished",
            outcome=(
                result_status["outcome"]
                if success or controlled_no_safe_work
                else OUTCOME_PARTIAL_POSSIBLE
                if mutation_started
                else OUTCOME_RESTAGE_REQUIRED
            ),
            next_action=(
                result_status["next_action"]
                if success or controlled_no_safe_work
                else NEXT_KEEP_CLOSED
                if mutation_started
                else NEXT_RESTAGE
            ),
            core={
                "exit_code": result.exit_code,
                "output_valid": result.output_valid,
                "stdout_sha256": result.stdout_sha256,
                "stderr": result.stderr,
                "result": summary,
            },
            plan_revalidated=result.output_valid
            and result.exit_code not in {EXIT_PLAN_CHANGED},
            success=success,
            deletion_success=success,
            verification_ok=verification_ok,
            partial_possible=mutation_started and not success,
            owner_reappeared=result.owner_reappeared,
            safety_warnings=receipt_warnings[-200:],
            component_summary=component_summary,
            safe_to_reopen=False,
        )

        if success:
            safe_outcome = str(result_status["outcome"])
            if safe_outcome not in {
                OUTCOME_COMPLETE,
                OUTCOME_COMPLETE_WITH_WARNINGS,
            }:
                safe_outcome = (
                    OUTCOME_COMPLETE_WITH_WARNINGS
                    if receipt_warnings
                    else OUTCOME_COMPLETE
                )
            writer.transition(
                clear=("retryable", "manual_offline_retry_at_epoch_ms"),
                phase="complete",
                outcome=safe_outcome,
                next_action=NEXT_REOPEN,
                terminal=True,
                helper_exit_code=EXIT_OK,
                success=True,
                deletion_success=True,
                verification_ok=True,
                permanent_deletion_complete=True,
                partial_possible=False,
                safe_to_reopen=True,
                restart={
                    "requested": False,
                    "attempted": False,
                    "success": None,
                    "manual_reopen_required": True,
                },
                completed_at_epoch_ms=now_epoch_ms(),
            )
            return EXIT_OK

        if controlled_no_safe_work:
            writer.transition(
                clear=(
                    "retryable",
                    "manual_offline_retry_at_epoch_ms",
                    "completed_at_epoch_ms",
                ),
                phase="complete",
                outcome=OUTCOME_NO_SAFE_WORK,
                next_action=NEXT_REOPEN,
                terminal=True,
                helper_exit_code=EXIT_OK,
                success=False,
                deletion_success=False,
                verification_ok=True,
                permanent_deletion_complete=False,
                partial_possible=False,
                mutation_started=False,
                safe_to_reopen=True,
                restart={
                    "requested": False,
                    "attempted": False,
                    "success": None,
                    "manual_reopen_required": True,
                },
                completed_at_epoch_ms=now_epoch_ms(),
            )
            return EXIT_OK

        if not mutation_started:
            if result.exit_code == EXIT_PLAN_CHANGED:
                helper_exit = EXIT_PLAN_CHANGED
                status = "plan_changed"
            elif result.pre_apply_owner_blocked or result.exit_code == EXIT_BLOCKED:
                helper_exit = EXIT_BLOCKED
                status = "blocked_before_mutation"
            else:
                helper_exit = EXIT_BLOCKED
                status = "engine_failed_before_mutation"
            writer.update(
                phase=status,
                outcome=OUTCOME_RESTAGE_REQUIRED,
                next_action=NEXT_RESTAGE,
                terminal=True,
                helper_exit_code=helper_exit,
                success=False,
                deletion_success=False,
                permanent_deletion_complete=False,
                partial_possible=False,
                safe_to_reopen=True,
                completed_at_epoch_ms=now_epoch_ms(),
            )
            return helper_exit

        writer.update(
            phase="partial_or_verification_failed",
            outcome=OUTCOME_PARTIAL_POSSIBLE,
            next_action=NEXT_KEEP_CLOSED,
            terminal=True,
            helper_exit_code=EXIT_PARTIAL_OR_VERIFY_FAILED,
            success=False,
            deletion_success=False,
            permanent_deletion_complete=False,
            partial_possible=True,
            safe_to_reopen=False,
            completed_at_epoch_ms=now_epoch_ms(),
        )
        return EXIT_PARTIAL_OR_VERIFY_FAILED
    except ExpiredRequestError as exc:
        mutation_started = bool(writer.payload.get("mutation_started"))
        writer.add_notice(
            writer.payload.get("phase", "worker"),
            "approved_request_expired",
            sanitize_text(str(exc), token),
            disposition="restage_and_reapprove",
        )
        helper_exit = (
            EXIT_PARTIAL_OR_VERIFY_FAILED if mutation_started else EXIT_PLAN_CHANGED
        )
        writer.transition(
            clear=("retryable",),
            phase=(
                "partial_or_verification_failed"
                if mutation_started
                else "expired_before_mutation"
            ),
            outcome=(
                OUTCOME_PARTIAL_POSSIBLE
                if mutation_started
                else OUTCOME_RESTAGE_REQUIRED
            ),
            next_action=NEXT_KEEP_CLOSED if mutation_started else NEXT_RESTAGE,
            terminal=True,
            helper_exit_code=helper_exit,
            staged_capsule_revoked=True,
            deletion_success=False,
            verification_ok=False,
            permanent_deletion_complete=False,
            partial_possible=mutation_started,
            safe_to_reopen=not mutation_started,
            completed_at_epoch_ms=now_epoch_ms(),
        )
        return helper_exit
    except (ExpiredRequestError, PlanChangedError, SourceChangedError) as exc:
        mutation_started = bool(writer.payload.get("mutation_started"))
        writer.add_error(
            writer.payload.get("phase", "worker"),
            type(exc).__name__,
            sanitize_text(str(exc), token),
        )
        helper_exit = (
            EXIT_PARTIAL_OR_VERIFY_FAILED if mutation_started else EXIT_PLAN_CHANGED
        )
        writer.update(
            phase="partial_or_verification_failed"
            if mutation_started
            else "plan_changed",
            outcome=(
                OUTCOME_PARTIAL_POSSIBLE
                if mutation_started
                else OUTCOME_RESTAGE_REQUIRED
            ),
            next_action=NEXT_KEEP_CLOSED if mutation_started else NEXT_RESTAGE,
            terminal=True,
            helper_exit_code=helper_exit,
            deletion_success=False,
            verification_ok=False,
            permanent_deletion_complete=False,
            partial_possible=mutation_started,
            safe_to_reopen=not mutation_started,
            completed_at_epoch_ms=now_epoch_ms(),
        )
        return helper_exit
    except DesktopOfflineError as exc:
        writer.add_notice(
            writer.payload.get("phase", "waiting_for_manual_exit"),
            "desktop_offline_not_yet_verified",
            sanitize_text(str(exc), token),
            disposition="retry_manual_offline_wait",
        )
        writer.transition(
            clear=("completed_at_epoch_ms",),
            phase="manual_offline_wait_failed",
            outcome=OUTCOME_RETRYABLE_WARNING,
            next_action=NEXT_RETRY_LAUNCH,
            terminal=False,
            helper_exit_code=EXIT_BLOCKED,
            deletion_success=False,
            verification_ok=False,
            permanent_deletion_complete=False,
            partial_possible=False,
            retryable=True,
            safe_to_reopen=True,
            manual_offline_retry_at_epoch_ms=now_epoch_ms(),
        )
        return EXIT_BLOCKED
    except Exception as exc:
        mutation_started = bool(writer.payload.get("mutation_started"))
        writer.add_error(
            writer.payload.get("phase", "worker"),
            type(exc).__name__,
            sanitize_text(str(exc), token),
        )
        writer.update(
            phase="partial_or_verification_failed"
            if mutation_started
            else "blocked_before_mutation",
            outcome=(
                OUTCOME_PARTIAL_POSSIBLE
                if mutation_started
                else OUTCOME_RESTAGE_REQUIRED
            ),
            next_action=NEXT_KEEP_CLOSED if mutation_started else NEXT_RESTAGE,
            terminal=True,
            helper_exit_code=(
                EXIT_PARTIAL_OR_VERIFY_FAILED if mutation_started else EXIT_BLOCKED
            ),
            deletion_success=False,
            verification_ok=False,
            permanent_deletion_complete=False,
            partial_possible=mutation_started,
            safe_to_reopen=not mutation_started,
            completed_at_epoch_ms=now_epoch_ms(),
        )
        return EXIT_PARTIAL_OR_VERIFY_FAILED if mutation_started else EXIT_BLOCKED
    finally:
        token = ""
        if core is not None:
            sys.modules.pop(core.__name__, None)
        if request is not None:
            request["approval_token"] = ""
        if writer.payload.get("terminal") is True:
            request_path = job_dir / REQUEST_FILENAME
            if request_path.exists():
                try:
                    safely_remove_unconsumed_request(job_dir)
                except Exception as cleanup_exc:
                    writer.add_error(
                        "terminal_cleanup",
                        type(cleanup_exc).__name__,
                        str(cleanup_exc),
                    )
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def validate_session_inputs(
    codex_home: Path,
    session_ids: Iterable[str],
) -> tuple[Path, list[str]]:
    resolved_home = codex_home.expanduser().resolve()
    if not resolved_home.is_dir():
        raise HelperError(f"Codex home does not exist: {resolved_home}")
    normalized: list[str] = []
    for raw in session_ids:
        candidate = raw.lower()
        if not UUID_RE.fullmatch(candidate):
            raise HelperError(f"Invalid canonical session ID: {raw}")
        normalized.append(candidate)
    normalized = sorted(dict.fromkeys(normalized))
    if not normalized or len(normalized) > 256:
        raise HelperError("One to 256 session IDs are required.")
    return resolved_home, normalized


def build_request(
    *,
    codex_home: Path,
    session_ids: list[str],
    approval_token: str,
    options: dict[str, bool],
    timing: dict[str, float],
    restart_requested: bool = False,
    expires_in_seconds: int,
    job_id: str,
    cleanup_job_ids: list[str] | None = None,
    exit_mode: str = EXIT_MODE_MANUAL_GHOSTTY,
) -> dict[str, Any]:
    if restart_requested or exit_mode != EXIT_MODE_MANUAL_GHOSTTY:
        raise HelperError("Only the manual, no-restart Ghostty handoff is supported.")
    created = now_epoch_ms()
    helper_path = Path(__file__).resolve()
    core_path = helper_path.parent / CORE_SCRIPT_FILENAME
    interpreter = current_interpreter_contract()
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "job_id": job_id,
        "cleanup_job_ids": list(cleanup_job_ids or []),
        "created_at_epoch_ms": created,
        "expires_at_epoch_ms": created + expires_in_seconds * 1000,
        "codex_home": str(codex_home.expanduser().resolve()),
        "session_ids": session_ids,
        "options": options,
        "timing": timing,
        "restart": {"requested": restart_requested, "bundle_id": DEFAULT_BUNDLE_ID},
        "exit_mode": exit_mode,
        "source": {
            "core_path": str(core_path.resolve()),
            "core_sha256": sha256_bytes(safe_source_bytes(core_path)),
            "helper_sha256": sha256_bytes(safe_source_bytes(helper_path)),
            **interpreter,
        },
        "approval_token": approval_token,
    }


def create_job(request: dict[str, Any], job_root: Path) -> tuple[Path, dict[str, Any]]:
    job_root = job_root.expanduser().resolve()
    if not job_root.exists():
        parent = job_root.parent
        owned_real_directory(parent)
        job_root.mkdir(mode=0o700)
        fsync_directory(parent)
    secure_directory(job_root)
    job_dir = job_root / request["job_id"]
    job_dir.mkdir(mode=0o700)
    secure_directory(job_dir)
    request_path = job_dir / REQUEST_FILENAME
    try:
        request_bytes = (
            json.dumps(
                request, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
            + b"\n"
        )
        if len(request_bytes) > MAX_REQUEST_BYTES:
            raise HelperError("The private request exceeds the supported size.")
        create_exclusive_private_file(request_path, request_bytes)
        initial = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "job_id": request["job_id"],
            "phase": "prepared",
            "outcome": OUTCOME_STAGED,
            "next_action": NEXT_LAUNCH_GHOSTTY,
            "terminal": False,
            "mutation_started": False,
            "success": False,
            "deletion_success": False,
            "verification_ok": False,
            "permanent_deletion_complete": False,
            "partial_possible": False,
            "request": public_request_metadata(request),
            "request_sha256": sha256_bytes(request_bytes),
            "created_at_epoch_ms": request["created_at_epoch_ms"],
            "updated_at_epoch_ms": now_epoch_ms(),
            "errors": [],
        }
        ReceiptWriter(job_dir, initial)
        return job_dir, initial
    except Exception:
        if request_path.exists():
            safely_remove_unconsumed_request(job_dir)
        raise


def ghostty_version_tuple(value: Any) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise HelperError("Ghostty has no valid bundle version metadata.")
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:[-+].*)?", value)
    if match is None:
        raise HelperError("Ghostty's bundle version is unsupported.")
    return tuple(int(part) for part in match.groups())


def validated_ghostty_app(app_path: Path = DEFAULT_GHOSTTY_APP) -> Path:
    """Return an audited Ghostty bundle with the required AppleScript API."""

    if not app_path.is_absolute() or app_path.resolve() != app_path:
        raise HelperError("Ghostty must use its canonical application path.")
    try:
        app_info = app_path.lstat()
    except FileNotFoundError as exc:
        raise HelperError("Ghostty is missing from /Applications/Ghostty.app.") from exc
    if not stat.S_ISDIR(app_info.st_mode):
        raise HelperError("The configured Ghostty application is not a real bundle.")
    plist_path = app_path / "Contents" / "Info.plist"
    try:
        plist_info = plist_path.lstat()
        if not stat.S_ISREG(plist_info.st_mode):
            raise HelperError("Ghostty Info.plist is not a regular file.")
        with plist_path.open("rb") as stream:
            metadata = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise HelperError("Unable to validate the installed Ghostty bundle.") from exc
    if metadata.get("CFBundleIdentifier") != DEFAULT_GHOSTTY_BUNDLE_ID:
        raise HelperError("The installed application is not the expected Ghostty bundle.")
    version = ghostty_version_tuple(metadata.get("CFBundleShortVersionString"))
    if version < MINIMUM_GHOSTTY_VERSION:
        minimum = ".".join(str(part) for part in MINIMUM_GHOSTTY_VERSION)
        raise HelperError(f"Ghostty {minimum} or newer is required for AppleScript.")
    executable_name = metadata.get("CFBundleExecutable")
    if not isinstance(executable_name, str) or not executable_name:
        raise HelperError("Ghostty has no valid bundle executable metadata.")
    executable = app_path / "Contents" / "MacOS" / executable_name
    try:
        executable_info = executable.lstat()
    except FileNotFoundError as exc:
        raise HelperError("Ghostty's bundle executable is missing.") from exc
    if not stat.S_ISREG(executable_info.st_mode) or not os.access(executable, os.X_OK):
        raise HelperError("Ghostty's bundle executable is not runnable.")
    scripting_definition = app_path / "Contents" / "Resources" / "Ghostty.sdef"
    try:
        definition_info = scripting_definition.lstat()
        if not stat.S_ISREG(definition_info.st_mode):
            raise HelperError("Ghostty's AppleScript dictionary is not a regular file.")
        definition = scripting_definition.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise HelperError("Unable to validate Ghostty's AppleScript dictionary.") from exc
    required_markers = (
        'command name="new surface configuration"',
        'command name="new window"',
        'property name="command"',
        'property name="wait after command"',
    )
    if any(marker not in definition for marker in required_markers):
        raise HelperError("Ghostty's AppleScript API lacks required surface controls.")
    return app_path


def validated_osascript(path: Path = DEFAULT_OSASCRIPT) -> Path:
    """Return the fixed system AppleScript interpreter when it is executable."""

    if not path.is_absolute() or path.resolve() != path:
        raise HelperError("osascript must use its canonical system path.")
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise HelperError("The system osascript executable is missing.") from exc
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        raise HelperError("The system osascript executable is not runnable.")
    return path


def ghostty_launch_arguments(
    job_dir: Path,
    interpreter_path: str,
    app_path: Path = DEFAULT_GHOSTTY_APP,
) -> list[str]:
    validated_ghostty_app(app_path)
    osascript = validated_osascript()
    resolved_job_dir = job_dir.resolve()
    secure_directory(resolved_job_dir)
    if not JOB_ID_RE.fullmatch(resolved_job_dir.name):
        raise HelperError("Invalid private job directory name.")
    resolved_interpreter = Path(interpreter_path).resolve()
    if (
        not Path(interpreter_path).is_absolute()
        or str(resolved_interpreter) != interpreter_path
    ):
        raise SourceChangedError(
            "The approved Python interpreter path is not canonical."
        )
    safe_interpreter_bytes(resolved_interpreter)
    worker_invocation = shlex.join(
        [
            interpreter_path,
            str(Path(__file__).resolve()),
            "ghostty-worker",
            "--job-dir",
            str(resolved_job_dir),
        ]
    )
    shell_script = (
        "unset HISTFILE; "
        + worker_invocation
        + "; worker_status=$?; "
        + 'if [ "$worker_status" -ne 0 ]; then '
        + "printf '\\n离线工作进程退出码：%s；请以任务回执为准。\\n' "
        + '"$worker_status"; fi; exit 0'
    )
    worker_command = shlex.join(["/bin/zsh", "-f", "-c", shell_script])
    script = """on run argv
    if (count of argv) is not 2 then error "Expected a job directory and worker command."
    set workerDirectory to item 1 of argv
    set workerCommand to item 2 of argv
    tell application id "com.mitchellh.ghostty"
        set workerConfiguration to new surface configuration
        set initial working directory of workerConfiguration to workerDirectory
        set command of workerConfiguration to workerCommand
        set environment variables of workerConfiguration to {"HISTFILE=/dev/null"}
        set wait after command of workerConfiguration to true
        new window with configuration workerConfiguration
    end tell
end run"""
    return [
        str(osascript),
        "-e",
        script,
        "--",
        str(resolved_job_dir.parent),
        worker_command,
    ]


def launch_ghostty_worker(
    job_dir: Path,
    interpreter_path: str,
    app_path: Path = DEFAULT_GHOSTTY_APP,
) -> GhosttyLaunchResult:
    arguments = ghostty_launch_arguments(job_dir, interpreter_path, app_path)
    try:
        completed = subprocess.run(  # noqa: S603 - fixed osascript and audited Ghostty
            arguments,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        return GhosttyLaunchResult(
            False,
            DEFAULT_GHOSTTY_BUNDLE_ID,
            str(exc),
            "unknown",
        )
    except OSError as exc:
        return GhosttyLaunchResult(
            False,
            DEFAULT_GHOSTTY_BUNDLE_ID,
            str(exc),
            "confirmed_not_submitted",
        )
    except subprocess.SubprocessError as exc:
        return GhosttyLaunchResult(
            False,
            DEFAULT_GHOSTTY_BUNDLE_ID,
            str(exc),
            "unknown",
        )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit {completed.returncode}"
        denied = "-1743" in detail or "not authorized" in detail.lower()
        return GhosttyLaunchResult(
            False,
            DEFAULT_GHOSTTY_BUNDLE_ID,
            detail[:2000],
            "confirmed_not_submitted" if denied else "unknown",
        )
    return GhosttyLaunchResult(
        True,
        DEFAULT_GHOSTTY_BUNDLE_ID,
        "",
        "submitted",
    )


def normalize_ghostty_launch_result(value: Any) -> GhosttyLaunchResult:
    if isinstance(value, GhosttyLaunchResult):
        return value
    if isinstance(value, tuple) and len(value) == 3:
        launched, label, error = value
        return GhosttyLaunchResult(
            bool(launched),
            str(label),
            str(error),
            "submitted" if launched else "unknown",
        )
    raise HelperError("The Ghostty launcher returned an unsupported result.")


def safely_remove_unconsumed_request(job_dir: Path) -> None:
    path = job_dir / REQUEST_FILENAME
    if not path.exists():
        return
    secure_regular_file(path)
    path.unlink()
    fsync_directory(job_dir)


def cleanup_job_ids_from_receipt(receipt: dict[str, Any]) -> list[str]:
    request = receipt.get("request", {})
    raw = request.get("cleanup_job_ids", []) if isinstance(request, dict) else []
    if raw is None:
        raw = []
    if (
        not isinstance(raw, list)
        or len(raw) > MAX_CLEANUP_JOB_IDS
        or any(not isinstance(item, str) or not JOB_ID_RE.fullmatch(item) for item in raw)
        or raw != list(dict.fromkeys(raw))
    ):
        raise HelperError("The receipt contains an invalid cleanup lineage.")
    return list(raw)


def recovery_job_ids_from_receipt(receipt: dict[str, Any]) -> list[str]:
    raw = receipt.get("recovery_job_ids", [])
    if raw is None:
        raw = []
    lineage = cleanup_job_ids_from_receipt(receipt)
    if (
        not isinstance(raw, list)
        or len(raw) > 1
        or any(not isinstance(item, str) or not JOB_ID_RE.fullmatch(item) for item in raw)
        or raw != list(dict.fromkeys(raw))
        or any(item not in lineage for item in raw)
    ):
        raise HelperError("The receipt contains an invalid recovery lineage.")
    return list(raw)


def acquire_existing_cleanup_lock(path: Path) -> int:
    secure_regular_file(path)
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise HelperError(f"Unsafe cleanup lock file: {path}")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise HelperError(f"A cleanup job is still active: {path.parent.name}") from exc
    except Exception:
        os.close(fd)
        raise
    return fd


def verified_success_receipt(receipt: dict[str, Any]) -> bool:
    return bool(
        receipt.get("phase") == "complete"
        and receipt.get("deletion_success") is True
        and receipt.get("verification_ok") is True
        and receipt.get("permanent_deletion_complete") is True
    )


TARGET_ABSENCE_COUNT_KEYS = {
    "desktop_automation_run_rows",
    "desktop_auxiliary_thread_rows",
    "desktop_catalog_rows",
    "desktop_inbox_rows",
    "desktop_timeline_rows",
    "generated_artifacts",
    "global_state_structural_refs",
    "logs_rows",
    "paginated_history_item_rows",
    "paginated_history_projection_rows",
    "paginated_history_turn_rows",
    "rollout_files",
    "session_index_rows",
    "shell_snapshots",
    "state_agent_job_items_assigned",
    "state_rollout_migration_skipped_rollouts",
    "state_stage1_outputs",
    "state_thread_dynamic_tools",
    "state_thread_goals",
    "state_thread_spawn_edges",
    "state_threads",
}


def empty_historical_recovery_candidate(receipt: dict[str, Any]) -> bool:
    """Whether a retained partial receipt matches the known empty-snapshot anomaly."""

    if (
        receipt.get("terminal") is not True
        or receipt.get("mutation_started") is not True
        or receipt.get("phase") != "partial_or_verification_failed"
        or receipt.get("outcome") != OUTCOME_PARTIAL_POSSIBLE
        or receipt.get("owner_reappeared") is True
        or receipt.get("request_integrity_verified") is not True
        or receipt.get("plan_revalidated") is not True
    ):
        return False
    request = receipt.get("request", {})
    options = request.get("options", {}) if isinstance(request, dict) else {}
    staged = receipt.get("staged_plan", {})
    historical = staged.get("historical_residuals", {}) if isinstance(staged, dict) else {}
    if (
        not isinstance(options, dict)
        or options.get("apply_historical_residuals") is not True
        or not isinstance(historical, dict)
        or historical.get("scanned") is not True
        or int(historical.get("total_ids", -1)) != 0
        or int(historical.get("total_items", -1)) != 0
        or historical.get("has_residuals") is True
    ):
        return False
    core = receipt.get("core", {})
    core_result = core.get("result", {}) if isinstance(core, dict) else {}
    apply_result = (
        core_result.get("apply_result", {})
        if isinstance(core_result, dict)
        else {}
    )
    verification = (
        apply_result.get("verification", {})
        if isinstance(apply_result, dict)
        else {}
    )
    historical_cleanup = (
        apply_result.get("historical_cleanup", {})
        if isinstance(apply_result, dict)
        else {}
    )
    historical_component = (
        apply_result.get("component_results", {}).get("historical", {})
        if isinstance(apply_result.get("component_results", {}), dict)
        else {}
    )
    return bool(
        isinstance(apply_result, dict)
        and apply_result.get("outcome") == OUTCOME_PARTIAL_POSSIBLE
        and apply_result.get("mutation_started") is True
        and apply_result.get("historical_scan_ok") is False
        and isinstance(verification, dict)
        and verification.get("verification_ok") is True
        and verification.get("historical_snapshot_ok") is False
        and not verification.get("planned_deleted_remaining")
        and not verification.get("expected_preserved_missing")
        and not verification.get("unexpected_remaining")
        and not verification.get("unexpected_non_target_removed")
        and verification.get("offline_verification_ok") is True
        and isinstance(historical_cleanup, dict)
        and historical_cleanup.get("applied") is False
        and isinstance(historical_component, dict)
        and historical_component.get("status") == "not_requested"
        and not receipt.get("errors")
    )


def recoverable_skipped_historical_component(receipt: dict[str, Any]) -> bool:
    """Whether only approved historical cleanup remains after verified target deletion."""

    if (
        receipt.get("terminal") is not True
        or receipt.get("mutation_started") is not True
        or receipt.get("phase") != "partial_or_verification_failed"
        or receipt.get("outcome") != OUTCOME_PARTIAL_POSSIBLE
        or receipt.get("owner_reappeared") is True
        or receipt.get("request_integrity_verified") is not True
        or receipt.get("plan_revalidated") is not True
        or receipt.get("request_consumed") is not True
        or receipt.get("verification_ok") is not True
        or receipt.get("deletion_success") is True
        or receipt.get("permanent_deletion_complete") is True
        or receipt.get("errors")
    ):
        return False

    request = receipt.get("request", {})
    options = request.get("options", {}) if isinstance(request, dict) else {}
    staged = receipt.get("staged_plan", {})
    historical = staged.get("historical_residuals", {}) if isinstance(staged, dict) else {}
    if (
        not isinstance(options, dict)
        or options.get("apply_historical_residuals") is not True
        or options.get("apply_missing_rollout_threads") is True
        or not isinstance(historical, dict)
        or historical.get("scanned") is not True
        or int(historical.get("total_ids", 0) or 0) <= 0
        or int(historical.get("total_items", 0) or 0) <= 0
        or historical.get("has_residuals") is not True
    ):
        return False

    core_receipt = receipt.get("core", {})
    core_result = (
        core_receipt.get("result", {}) if isinstance(core_receipt, dict) else {}
    )
    apply_result = (
        core_result.get("apply_result", {}) if isinstance(core_result, dict) else {}
    )
    verification = (
        apply_result.get("verification", {})
        if isinstance(apply_result, dict)
        else {}
    )
    historical_cleanup = (
        apply_result.get("historical_cleanup", {})
        if isinstance(apply_result, dict)
        else {}
    )
    component_results = (
        apply_result.get("component_results", {})
        if isinstance(apply_result, dict)
        else {}
    )
    historical_component = (
        component_results.get("historical", {})
        if isinstance(component_results, dict)
        else {}
    )
    component_summary = receipt.get("component_summary", {})
    residual_counts = (
        verification.get("residual_counts", {})
        if isinstance(verification, dict)
        else {}
    )
    integrity_checks = (
        verification.get("integrity_checks", {})
        if isinstance(verification, dict)
        else {}
    )
    auxiliary_checks = (
        integrity_checks.get("auxiliary_thread_databases", {})
        if isinstance(integrity_checks, dict)
        else {}
    )
    required_integrity = [
        integrity_checks.get(key) if isinstance(integrity_checks, dict) else None
        for key in ["state", "logs", "desktop_catalog", "paginated_history"]
    ]
    if (
        not isinstance(apply_result, dict)
        or apply_result.get("outcome") != OUTCOME_PARTIAL_POSSIBLE
        or apply_result.get("mutation_started") is not True
        or apply_result.get("historical_scan_ok") is not False
        or not isinstance(verification, dict)
        or verification.get("verification_ok") is not True
        or verification.get("historical_snapshot_ok") is not False
        or verification.get("offline_verification_ok") is not True
        or verification.get("verification_errors")
        or verification.get("planned_deleted_remaining")
        or verification.get("expected_preserved_missing")
        or verification.get("unexpected_remaining")
        or verification.get("unexpected_non_target_removed")
        or verification.get("remaining_rollout_files")
        or verification.get("remaining_shell_snapshots")
        or verification.get("remaining_generated_artifacts")
        or not isinstance(residual_counts, dict)
        or any(int(value or 0) != 0 for value in residual_counts.values())
        or any(value != "ok" for value in required_integrity)
        or not isinstance(auxiliary_checks, dict)
        or any(value != "ok" for value in auxiliary_checks.values())
        or not isinstance(historical_cleanup, dict)
        or historical_cleanup.get("applied") is not False
        or not isinstance(historical_component, dict)
        or historical_component.get("status") != "skipped_safely"
        or historical_component.get("mutation_started") is not False
        or not isinstance(component_summary, dict)
        or int(component_summary.get("failed_component_count", -1)) != 0
        or not isinstance(component_results, dict)
        or any(
            isinstance(result, dict) and result.get("status") == "failed"
            for result in component_results.values()
        )
    ):
        return False
    return True


def inactive_job_locks(job_dir: Path) -> tuple[bool, str]:
    descriptors: list[int] = []
    try:
        for name in [LOCK_FILENAME, LAUNCH_LOCK_FILENAME]:
            path = job_dir / name
            if path.exists():
                descriptors.append(acquire_existing_cleanup_lock(path))
        return True, ""
    except HelperError as exc:
        return False, str(exc)
    finally:
        for fd in descriptors:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def inactive_premutation_assessment(
    job_dir: Path,
    receipt: dict[str, Any],
    *,
    locks_inactive: bool | None = None,
) -> dict[str, Any]:
    """Derive a safe action for an inactive worker before request consumption."""

    phase = str(receipt.get("phase", ""))
    if (
        receipt.get("terminal") is True
        or receipt.get("mutation_started") is True
        or receipt.get("request_consumed") is True
        or phase not in RETRYABLE_PREMUTATION_PHASES
    ):
        return {"eligible": False}
    if locks_inactive is None:
        locks_inactive, lock_reason = inactive_job_locks(job_dir)
    else:
        lock_reason = ""
    if not locks_inactive:
        return {
            "eligible": True,
            "retryable": False,
            "next_action": NEXT_QUIT_AND_WAIT,
            "reason": lock_reason or "The offline worker is still active.",
        }
    try:
        raw = read_private_bytes(job_dir / REQUEST_FILENAME)
        expected_sha = receipt.get("request_sha256")
        if not isinstance(expected_sha, str) or sha256_bytes(raw) != expected_sha:
            raise RequestError("The unconsumed request no longer matches its receipt.")
        try:
            decoded = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RequestError("The unconsumed request is invalid JSON.") from exc
        request = validate_request(decoded, job_dir)
        if now_epoch_ms() >= request["expires_at_epoch_ms"]:
            return {
                "eligible": True,
                "retryable": False,
                "next_action": NEXT_RESTAGE,
                "reason": "The approved request expired before mutation.",
            }
        verify_source_contract(request)
    except (OSError, HelperError, UnicodeError, json.JSONDecodeError) as exc:
        return {
            "eligible": True,
            "retryable": False,
            "next_action": NEXT_RESTAGE,
            "reason": str(exc),
        }
    finally:
        if "request" in locals():
            request["approval_token"] = ""
    return {
        "eligible": True,
        "retryable": True,
        "next_action": NEXT_RETRY_LAUNCH,
        "reason": (
            "The prior worker is inactive; the request is unconsumed, valid, "
            "unexpired, and source-bound."
        ),
    }


def receipt_with_runtime_action(
    job_dir: Path,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    """Add read-only worker-liveness evidence to the stable receipt action."""

    enriched = receipt_with_stable_action(receipt)
    assessment = inactive_premutation_assessment(job_dir, receipt)
    if assessment.get("eligible") is not True:
        return enriched
    enriched["worker_active"] = assessment.get("retryable") is False and assessment.get(
        "next_action"
    ) == NEXT_QUIT_AND_WAIT
    enriched["liveness_reason"] = assessment.get("reason", "")
    if assessment.get("retryable") is True:
        enriched["outcome"] = OUTCOME_RETRYABLE_WARNING
        enriched["next_action"] = NEXT_RETRY_LAUNCH
        enriched["retryable"] = True
        enriched["safe_to_reopen"] = True
    elif assessment.get("next_action") == NEXT_RESTAGE:
        enriched["outcome"] = OUTCOME_RESTAGE_REQUIRED
        enriched["next_action"] = NEXT_RESTAGE
        enriched["retryable"] = False
        enriched["safe_to_reopen"] = True
    return enriched


def classify_job_directory(job_dir: Path, codex_home: Path) -> dict[str, Any]:
    """Classify one exact private job child without mutating it."""

    result: dict[str, Any] = {
        "job_id": job_dir.name,
        "job_dir": str(job_dir),
        "classification": "unsafe_preserved",
        "recommended_action": "inspect",
    }
    try:
        if not JOB_ID_RE.fullmatch(job_dir.name):
            raise HelperError("The entry name is not a canonical job ID.")
        secure_directory(job_dir)
        entries = list(job_dir.iterdir())
        allowed_names = {
            REQUEST_FILENAME,
            RECEIPT_FILENAME,
            LOCK_FILENAME,
            LAUNCH_LOCK_FILENAME,
        }
        unexpected = sorted(
            entry.name for entry in entries if entry.name not in allowed_names
        )
        if unexpected:
            raise HelperError(
                "The job contains unexpected entries: " + ", ".join(unexpected)
            )
        receipt = read_private_json(
            job_dir / RECEIPT_FILENAME,
            maximum=MAX_RECEIPT_BYTES,
        )
        if (
            receipt.get("schema_version") not in {5, RECEIPT_SCHEMA_VERSION}
            or receipt.get("protocol_version") not in {5, PROTOCOL_VERSION}
            or receipt.get("job_id") != job_dir.name
        ):
            raise HelperError("The job receipt version or identity is unsupported.")
        request = receipt.get("request", {})
        if (
            not isinstance(request, dict)
            or request.get("codex_home") != str(codex_home)
        ):
            raise HelperError("The job belongs to a different Codex home.")
        inactive, lock_reason = inactive_job_locks(job_dir)
        enriched = receipt_with_runtime_action(job_dir, receipt)
        result.update(
            {
                "schema_version": receipt.get("schema_version"),
                "protocol_version": receipt.get("protocol_version"),
                "phase": enriched.get("phase", "unknown"),
                "outcome": enriched.get("outcome", OUTCOME_FAILED),
                "next_action": enriched.get("next_action", NEXT_FIX_INPUT),
                "terminal": bool(enriched.get("terminal")),
                "mutation_started": bool(enriched.get("mutation_started")),
                "session_ids": list(request.get("session_ids", [])),
                "receipt_path": str(job_dir / RECEIPT_FILENAME),
            }
        )
        if not inactive:
            result.update(
                classification="active",
                recommended_action="leave_running",
                reason=lock_reason,
            )
        elif verified_success_receipt(enriched):
            result.update(
                classification="verified_success_cleanup_ready",
                recommended_action="cleanup_completed",
                reason="Deletion and verification succeeded; only private metadata remains.",
            )
        elif empty_historical_recovery_candidate(enriched):
            result.update(
                classification="recoverable_empty_historical_snapshot",
                recommended_action="recover_empty_historical",
                reason=(
                    "Target deletion verified; only an approved empty historical "
                    "snapshot was misclassified as unverifiable."
                ),
            )
        elif recoverable_skipped_historical_component(enriched):
            result.update(
                classification="recoverable_skipped_historical_component",
                recommended_action="stage_historical_component_recovery",
                reason=(
                    "Target deletion and integrity checks verified; the approved "
                    "historical component alone was skipped before mutation."
                ),
            )
        elif enriched.get("outcome") == OUTCOME_PARTIAL_POSSIBLE or enriched.get(
            "mutation_started"
        ) is True:
            result.update(
                classification="partial_possible_preserved",
                recommended_action="inspect_partial",
                reason="Mutation started without a narrowly provable successful result.",
            )
        elif enriched.get("retryable") is True:
            result.update(
                classification="retryable_pre_mutation",
                recommended_action="relaunch_same_job",
                reason=str(
                    enriched.get(
                        "liveness_reason",
                        "The request is unconsumed and no mutation started.",
                    )
                ),
            )
        elif (
            enriched.get("terminal") is not True
            and enriched.get("next_action") == NEXT_RESTAGE
            and enriched.get("safe_to_reopen") is True
        ):
            result.update(
                classification="restage_required_pre_mutation",
                recommended_action="cancel_and_restage_after_approval",
                reason=str(
                    enriched.get(
                        "liveness_reason",
                        "The inactive pre-mutation job can no longer be relaunched.",
                    )
                ),
            )
        elif enriched.get("terminal") is True:
            result.update(
                classification="terminal_failure_supersedable",
                recommended_action="supersede_on_next_approved_job",
                reason="The terminal failure may be linked to a later successful cleanup chain.",
            )
        else:
            result.update(
                classification="pending_preserved",
                recommended_action=str(enriched.get("next_action", NEXT_INSPECT_NO_RELAUNCH)),
                reason="The job is incomplete and requires status-aware handling.",
            )
    except (OSError, HelperError, UnicodeError, json.JSONDecodeError) as exc:
        result["reason"] = str(exc)
    return result


def audit_job_root(codex_home: Path) -> dict[str, Any]:
    """Return a bounded, read-only inventory of the private job root."""

    codex_home = codex_home.expanduser().resolve()
    owned_real_directory(codex_home)
    job_root = codex_home / DEFAULT_JOB_ROOT_NAME
    if not job_root.exists():
        return {
            "audit_contract_version": JOB_AUDIT_CONTRACT_VERSION,
            "codex_home": str(codex_home),
            "job_root": str(job_root),
            "job_root_present": False,
            "jobs": [],
            "ignored_entries": [],
            "unrecognized_entries": [],
            "summary": {"job_count": 0, "classifications": {}},
        }
    secure_directory(job_root)
    jobs: list[dict[str, Any]] = []
    ignored: list[str] = []
    unrecognized: list[dict[str, str]] = []
    for entry in sorted(job_root.iterdir(), key=lambda item: item.name):
        if entry.name in IGNORED_JOB_ROOT_ENTRIES:
            ignored.append(entry.name)
            continue
        if not JOB_ID_RE.fullmatch(entry.name):
            unrecognized.append(
                {
                    "name": entry.name,
                    "disposition": "preserve_unrecognized",
                }
            )
            continue
        jobs.append(classify_job_directory(entry, codex_home))
    classifications: dict[str, int] = {}
    for job in jobs:
        classification = str(job.get("classification", "unsafe_preserved"))
        classifications[classification] = classifications.get(classification, 0) + 1
    return {
        "audit_contract_version": JOB_AUDIT_CONTRACT_VERSION,
        "codex_home": str(codex_home),
        "job_root": str(job_root),
        "job_root_present": True,
        "jobs": jobs,
        "ignored_entries": ignored,
        "unrecognized_entries": unrecognized,
        "summary": {
            "job_count": len(jobs),
            "classifications": dict(sorted(classifications.items())),
            "ignored_entry_count": len(ignored),
            "unrecognized_entry_count": len(unrecognized),
        },
    }


def cleanup_verified_job_chain(
    job_dir: Path,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    """Remove verified-success metadata and its exact failed predecessor chain."""

    job_dir = job_dir.expanduser().resolve()
    secure_directory(job_dir)
    if not verified_success_receipt(receipt):
        raise HelperError("Only a verified successful job may finalize its cleanup chain.")
    job_root = job_dir.parent
    secure_directory(job_root)
    current_job_id = job_dir.name
    if not JOB_ID_RE.fullmatch(current_job_id):
        raise HelperError("The successful job directory has an invalid ID.")
    lineage = cleanup_job_ids_from_receipt(receipt)
    recovery_ids = set(recovery_job_ids_from_receipt(receipt))
    ordered_ids = [*lineage, current_job_id]
    if len(ordered_ids) != len(set(ordered_ids)):
        raise HelperError("The cleanup lineage contains a cycle.")
    request_metadata = receipt.get("request", {})
    codex_home = (
        request_metadata.get("codex_home")
        if isinstance(request_metadata, dict)
        else None
    )
    if not isinstance(codex_home, str) or not codex_home:
        raise HelperError("The successful receipt lacks its Codex-home boundary.")

    allowed_names = {RECEIPT_FILENAME, LOCK_FILENAME, LAUNCH_LOCK_FILENAME}
    locks: dict[str, list[int]] = {}
    existing_ids: list[str] = []
    try:
        for job_id in ordered_ids:
            candidate = job_root / job_id
            if not candidate.exists():
                if job_id == current_job_id:
                    raise HelperError("The successful job directory disappeared.")
                continue
            if candidate.parent != job_root:
                raise HelperError("A cleanup candidate escaped the private job root.")
            secure_directory(candidate)
            candidate_receipt = read_private_json(
                candidate / RECEIPT_FILENAME,
                maximum=MAX_RECEIPT_BYTES,
            )
            if (
                candidate_receipt.get("schema_version")
                not in {5, RECEIPT_SCHEMA_VERSION}
                or candidate_receipt.get("protocol_version")
                not in {5, PROTOCOL_VERSION}
                or candidate_receipt.get("job_id") != job_id
            ):
                raise HelperError(f"Unsupported cleanup receipt: {job_id}")
            metadata = candidate_receipt.get("request", {})
            if not isinstance(metadata, dict) or metadata.get("codex_home") != codex_home:
                raise HelperError(f"Cleanup job crosses a Codex-home boundary: {job_id}")
            if job_id == current_job_id:
                if not verified_success_receipt(candidate_receipt):
                    raise HelperError("The current success receipt changed before cleanup.")
            elif job_id in recovery_ids:
                if not recoverable_skipped_historical_component(candidate_receipt):
                    raise HelperError(
                        f"Cleanup recovery predecessor changed or is ineligible: {job_id}"
                    )
            elif (
                candidate_receipt.get("terminal") is not True
                or candidate_receipt.get("mutation_started") is True
                or candidate_receipt.get("outcome") == OUTCOME_PARTIAL_POSSIBLE
                or candidate_receipt.get("deletion_success") is True
                or candidate_receipt.get("permanent_deletion_complete") is True
            ):
                raise HelperError(f"Cleanup predecessor is not a terminal failure: {job_id}")
            entries = list(candidate.iterdir())
            unexpected = sorted(entry.name for entry in entries if entry.name not in allowed_names)
            if unexpected:
                raise HelperError(
                    f"Cleanup job {job_id} contains unexpected entries: "
                    + ", ".join(unexpected)
                )
            if candidate / REQUEST_FILENAME in entries:
                raise HelperError(f"Cleanup job still contains an approval request: {job_id}")
            secure_regular_file(candidate / RECEIPT_FILENAME)
            locks[job_id] = []
            for lock_name in [LOCK_FILENAME, LAUNCH_LOCK_FILENAME]:
                lock_path = candidate / lock_name
                if lock_path.exists():
                    locks[job_id].append(acquire_existing_cleanup_lock(lock_path))
            existing_ids.append(job_id)

        cleaned: list[str] = []
        for job_id in existing_ids:
            candidate = job_root / job_id
            current_entries = list(candidate.iterdir())
            unexpected = sorted(
                entry.name for entry in current_entries if entry.name not in allowed_names
            )
            if unexpected:
                raise HelperError(
                    f"Cleanup job {job_id} changed during finalization: "
                    + ", ".join(unexpected)
                )
            for name in [LOCK_FILENAME, LAUNCH_LOCK_FILENAME, RECEIPT_FILENAME]:
                path = candidate / name
                if path.exists():
                    secure_regular_file(path)
                    path.unlink()
            os.rmdir(candidate)
            fsync_directory(job_root)
            cleaned.append(job_id)
        return {
            "cleanup_complete": True,
            "cleanup_pending": False,
            "cleaned_job_ids": cleaned,
            "cleaned_job_count": len(cleaned),
        }
    except Exception as exc:
        remaining = [job_id for job_id in ordered_ids if (job_root / job_id).exists()]
        if job_dir.exists() and (job_dir / RECEIPT_FILENAME).exists():
            try:
                writer = ReceiptWriter(job_dir)
                writer.update(
                    cleanup_complete=False,
                    cleanup_pending=True,
                    cleanup_pending_job_ids=remaining,
                    cleanup_errors=[
                        {
                            "code": type(exc).__name__,
                            "message": str(exc)[:4000],
                        }
                    ],
                    next_action=NEXT_RETRY_CLEANUP,
                )
            except Exception:
                pass
        return {
            "cleanup_complete": False,
            "cleanup_pending": True,
            "cleaned_job_ids": [
                job_id for job_id in ordered_ids if not (job_root / job_id).exists()
            ],
            "pending_job_ids": remaining,
            "error": str(exc),
        }
    finally:
        for descriptors in locks.values():
            for fd in descriptors:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)


def request_options_from_args(args: argparse.Namespace) -> dict[str, bool]:
    if args.apply_missing_rollout_threads and not args.apply_historical_residuals:
        raise HelperError(
            "--apply-missing-rollout-threads requires --apply-historical-residuals."
        )
    if args.apply_historical_residuals and args.no_historical_scan:
        raise HelperError(
            "--apply-historical-residuals cannot be used with --no-historical-scan."
        )
    return {
        "include_subagents": not args.no_subagents,
        "include_logs": not args.no_logs,
        "scan_historical": not args.no_historical_scan,
        "apply_historical_residuals": args.apply_historical_residuals,
        "apply_missing_rollout_threads": args.apply_missing_rollout_threads,
        "force_open": args.force_open,
    }


def request_timing_from_args(args: argparse.Namespace) -> dict[str, float]:
    return {
        "launch_delay_seconds": float(args.launch_delay),
        "quit_timeout_seconds": float(args.quit_timeout),
        "offline_stability_seconds": float(args.offline_stability),
        "poll_interval_seconds": float(args.poll_interval),
        "restart_timeout_seconds": 1.0,
    }


def staged_plan_summary(
    core: types.ModuleType,
    plan: Any,
    approval_scope: str,
    execution_snapshot: dict[str, Any] | None = None,
    historical_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    historical = plan.historical_residuals
    historical_summary = historical.get("summary", {})
    if not isinstance(historical_summary, dict):
        historical_summary = {}
    safety_warnings = plan_safety_warnings(plan)
    component_summary = plan_component_summary(plan, execution_snapshot)
    execution_summary = plan_execution_summary(plan, execution_snapshot)
    desktop_mutation_components = approved_desktop_mutation_components(
        core, plan, execution_snapshot, historical_snapshot
    )
    desktop_offline_required = bool(desktop_mutation_components)
    return {
        "approval_scope": approval_scope,
        "plan_fingerprint": core.plan_fingerprint(plan),
        "target_plan_fingerprint": core.target_plan_fingerprint(plan),
        "root_ids": list(plan.root_ids),
        "target_ids": list(plan.target_ids),
        "target_count": len(plan.target_ids),
        "counts": dict(plan.counts),
        "bytes_to_remove": int(plan.bytes_to_remove),
        "mib_to_remove": round(plan.bytes_to_remove / 1024 / 1024, 1),
        "historical_residuals": {
            "scanned": bool(historical.get("scanned", False)),
            "total_items": int(historical_summary.get("total_items", 0)),
            "total_ids": int(historical_summary.get("total_ids", 0)),
            "has_residuals": bool(historical_summary.get("has_residuals", False)),
            "dangling_state_threads": len(
                historical.get("state_threads_missing_rollout_file", [])
            ),
        },
        "scope_safety": {
            "target_open_or_unknown_sessions": list(plan.open_subagents),
            "missing_rollout_open_threads": core.missing_rollout_open_threads(plan),
            "missing_rollout_current_sessions": core.missing_rollout_current_sessions(
                plan
            ),
        },
        "desktop_offline_required": desktop_offline_required,
        "desktop_mutation_components": desktop_mutation_components,
        "outcome": (
            OUTCOME_STAGED
            if desktop_offline_required
            else OUTCOME_ROUTE_DIRECT
        ),
        "next_action": (
            NEXT_LAUNCH_GHOSTTY
            if desktop_offline_required
            else NEXT_ROUTE_TO_DIRECT_APPLY
        ),
        "safety_warnings": safety_warnings,
        "warning_count": len(safety_warnings),
        "component_summary": component_summary,
        "execution_summary": execution_summary,
    }


def validated_cleanup_lineage(
    job_root: Path,
    superseded_job_dirs: Iterable[str],
    codex_home: Path,
) -> list[str]:
    """Freeze a bounded chain of failed jobs that success may later remove."""

    requested = [str(item) for item in superseded_job_dirs if str(item)]
    if not requested:
        return []
    secure_directory(job_root)
    pending = list(requested)
    accepted: list[str] = []
    seen: set[str] = set()
    while pending:
        raw = Path(pending.pop(0)).expanduser()
        if not raw.is_absolute():
            raise HelperError("A superseded job directory must be absolute.")
        resolved = raw.resolve()
        if resolved != raw or resolved.parent != job_root:
            raise HelperError(
                "A superseded job must be an exact real child of the selected job root."
            )
        secure_directory(resolved)
        job_id = resolved.name
        if not JOB_ID_RE.fullmatch(job_id):
            raise HelperError("A superseded job directory has an invalid ID.")
        if job_id in seen:
            continue
        if len(seen) >= MAX_CLEANUP_JOB_IDS:
            raise HelperError("The superseded job chain exceeds the supported limit.")
        for lock_name in [LOCK_FILENAME, LAUNCH_LOCK_FILENAME]:
            lock_path = resolved / lock_name
            if not lock_path.exists():
                continue
            lock_fd = acquire_existing_cleanup_lock(lock_path)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        receipt = read_private_json(
            resolved / RECEIPT_FILENAME,
            maximum=MAX_RECEIPT_BYTES,
        )
        if (
            receipt.get("schema_version") not in {5, RECEIPT_SCHEMA_VERSION}
            or receipt.get("protocol_version") not in {5, PROTOCOL_VERSION}
            or receipt.get("job_id") != job_id
        ):
            raise HelperError("A superseded job has an unsupported receipt.")
        request_metadata = receipt.get("request", {})
        if (
            not isinstance(request_metadata, dict)
            or request_metadata.get("codex_home") != str(codex_home)
        ):
            raise HelperError("A superseded job belongs to a different Codex home.")
        if receipt.get("terminal") is not True:
            raise HelperError(
                "A retryable non-terminal job must be relaunched instead of superseded."
            )
        if (
            receipt.get("mutation_started") is True
            or receipt.get("outcome") == OUTCOME_PARTIAL_POSSIBLE
        ):
            raise HelperError(
                "A partially mutated job requires an explicitly validated recovery path."
            )
        if (
            receipt.get("permanent_deletion_complete") is True
            or receipt.get("deletion_success") is True
        ):
            raise HelperError(
                "A verified successful job is not a failed predecessor; finalize it separately."
            )
        seen.add(job_id)
        accepted.append(job_id)
        inherited = request_metadata.get("cleanup_job_ids", [])
        if inherited is None:
            inherited = []
        if not isinstance(inherited, list) or any(
            not isinstance(item, str) or not JOB_ID_RE.fullmatch(item)
            for item in inherited
        ):
            raise HelperError("A superseded job contains an invalid cleanup lineage.")
        for inherited_id in inherited:
            if inherited_id not in seen:
                inherited_dir = job_root / inherited_id
                if inherited_dir.exists():
                    pending.append(str(inherited_dir))
    return accepted


def validated_historical_recovery_lineage(
    job_root: Path,
    recovery_job_dirs: Iterable[str],
    codex_home: Path,
    core: types.ModuleType,
    plan: Any,
    execution_snapshot: dict[str, Any],
    historical_snapshot: dict[str, Any],
    options: dict[str, bool],
) -> tuple[list[str], list[str]]:
    """Validate one partial receipt whose only unfinished work is historical."""

    requested = [str(item) for item in recovery_job_dirs if str(item)]
    if not requested:
        return [], []
    if len(requested) != 1:
        raise HelperError("Exactly one partial historical recovery job is supported.")
    secure_directory(job_root)
    raw = Path(requested[0]).expanduser()
    if not raw.is_absolute():
        raise HelperError("A recovery job directory must be absolute.")
    resolved = raw.resolve()
    if resolved != raw or resolved.parent != job_root:
        raise HelperError(
            "A recovery job must be an exact real child of the selected job root."
        )
    secure_directory(resolved)
    if not JOB_ID_RE.fullmatch(resolved.name):
        raise HelperError("The recovery job directory has an invalid ID.")
    inactive, reason = inactive_job_locks(resolved)
    if not inactive:
        raise HelperError(reason or "The recovery job is still active.")
    receipt = read_private_json(
        resolved / RECEIPT_FILENAME,
        maximum=MAX_RECEIPT_BYTES,
    )
    if (
        receipt.get("schema_version") not in {5, RECEIPT_SCHEMA_VERSION}
        or receipt.get("protocol_version") not in {5, PROTOCOL_VERSION}
        or receipt.get("job_id") != resolved.name
    ):
        raise HelperError("The recovery job has an unsupported receipt.")
    if not recoverable_skipped_historical_component(receipt):
        raise HelperError(
            "The partial job is not an exact skipped-historical recovery candidate."
        )
    metadata = receipt.get("request", {})
    prior_options = metadata.get("options", {}) if isinstance(metadata, dict) else {}
    current_ids = sorted(str(item).lower() for item in plan.root_ids)
    prior_ids = metadata.get("session_ids", []) if isinstance(metadata, dict) else []
    if (
        not isinstance(metadata, dict)
        or metadata.get("codex_home") != str(codex_home)
        or prior_ids != current_ids
        or prior_options != options
    ):
        raise HelperError(
            "The recovery scope differs from the retained partial job."
        )
    fresh_target_absence_evidence(receipt, resolved)
    detector = getattr(core, "execution_snapshot_target_work_components", None)
    if not callable(detector) or detector(execution_snapshot):
        raise HelperError(
            "The fresh recovery plan contains target work and cannot be historical-only."
        )
    nonzero_target_counts = {
        key: int(plan.counts.get(key, 0) or 0)
        for key in TARGET_ABSENCE_COUNT_KEYS
        if int(plan.counts.get(key, 0) or 0) != 0
    }
    if nonzero_target_counts:
        raise HelperError("The fresh recovery plan still contains target-owned objects.")
    component_plans = getattr(plan, "component_plans", {})
    if not isinstance(component_plans, dict):
        raise HelperError("The fresh recovery plan lacks component boundaries.")
    for component, component_plan in component_plans.items():
        status = (
            component_plan.get("status", "enabled")
            if isinstance(component_plan, dict)
            else "enabled"
        )
        if component == "historical":
            if status != "enabled":
                raise HelperError("The historical recovery component is unavailable.")
        elif status == "enabled":
            raise HelperError(
                "The fresh recovery plan enables a non-historical component."
            )
    historical_detector = getattr(core, "historical_snapshot_has_approved_work", None)
    if not callable(historical_detector) or not historical_detector(historical_snapshot):
        raise HelperError("The fresh approved historical snapshot contains no work.")
    inherited = metadata.get("cleanup_job_ids", [])
    if inherited is None:
        inherited = []
    if not isinstance(inherited, list) or any(
        not isinstance(item, str) or not JOB_ID_RE.fullmatch(item)
        for item in inherited
    ):
        raise HelperError("The recovery job contains an invalid inherited lineage.")
    inherited_dirs = [str(job_root / job_id) for job_id in inherited]
    inherited_ids = validated_cleanup_lineage(
        job_root,
        inherited_dirs,
        codex_home,
    )
    return [resolved.name], inherited_ids


def stage_handoff(args: argparse.Namespace) -> int:
    """Freeze one already-approved report scope without launching or mutating."""

    codex_home, session_ids = validate_session_inputs(
        Path(args.codex_home), args.session_ids
    )
    options = request_options_from_args(args)
    timing = request_timing_from_args(args)
    helper_path = Path(__file__).resolve()
    core_path = helper_path.parent / CORE_SCRIPT_FILENAME
    core_source = safe_source_bytes(core_path)
    core = load_verified_core_module(core_path, core_source)
    request: dict[str, Any] | None = None
    try:
        plan = core.make_plan(
            codex_home=codex_home,
            root_ids=session_ids,
            include_subagents=options["include_subagents"],
            include_logs=options["include_logs"],
            scan_historical=options["scan_historical"],
        )
        scope = core.approval_scope_key(
            options["apply_historical_residuals"],
            options["apply_missing_rollout_threads"],
            options["force_open"],
        )
        token = core.approval_tokens(plan).get(scope, "")
        if not token:
            raise HelperError(f"The selected approval scope is not applicable: {scope}")
        selected_approval_payload = core.validated_approval_payload(
            plan,
            token,
            options["apply_historical_residuals"],
            options["apply_missing_rollout_threads"],
            options["force_open"],
        )
        if selected_approval_payload is None:
            raise HelperError("The generated approval capsule failed self-validation.")
        execution_snapshot = selected_approval_payload.get("execution_snapshot", {})
        if not isinstance(execution_snapshot, dict):
            execution_snapshot = {}
        historical_snapshot = selected_approval_payload.get(
            "historical_snapshot", {}
        )
        if not isinstance(historical_snapshot, dict):
            historical_snapshot = {}
        summary = staged_plan_summary(
            core,
            plan,
            scope,
            execution_snapshot,
            historical_snapshot,
        )
        confirmed_fingerprint = str(args.confirm_plan_fingerprint).lower()
        selected_scope_fingerprint = core.approval_scope_fingerprint(
            plan,
            options["apply_historical_residuals"],
            options["apply_missing_rollout_threads"],
            options["force_open"],
        )
        summary["approval_scope_fingerprint"] = selected_scope_fingerprint
        if (
            not re.fullmatch(r"[0-9a-f]{64}", confirmed_fingerprint)
            or confirmed_fingerprint != selected_scope_fingerprint
        ):
            print(
                public_json_dumps(
                    {
                        "staged": False,
                        "launched": False,
                        "mutation_started": False,
                        "outcome": OUTCOME_RESTAGE_REQUIRED,
                        "next_action": NEXT_RESTAGE,
                        "reason": (
                            "The report changed before the approved scope could be "
                            "frozen; no private job was created."
                        ),
                        "current_plan": summary,
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return EXIT_PLAN_CHANGED
        job_root = (
            Path(args.job_root).expanduser().resolve()
            if args.job_root
            else codex_home / DEFAULT_JOB_ROOT_NAME
        )
        superseded_job_ids = validated_cleanup_lineage(
            job_root,
            args.supersedes_job_dir,
            codex_home,
        )
        recovery_job_ids, inherited_recovery_job_ids = (
            validated_historical_recovery_lineage(
                job_root,
                getattr(args, "recovers_partial_job_dir", []),
                codex_home,
                core,
                plan,
                execution_snapshot,
                historical_snapshot,
                options,
            )
        )
        cleanup_job_ids = list(
            dict.fromkeys(
                [
                    *recovery_job_ids,
                    *inherited_recovery_job_ids,
                    *superseded_job_ids,
                ]
            )
        )
        job_id = uuid.uuid4().hex
        request = build_request(
            codex_home=codex_home,
            session_ids=session_ids,
            approval_token=token,
            options=options,
            timing=timing,
            restart_requested=False,
            expires_in_seconds=args.expires_in,
            job_id=job_id,
            cleanup_job_ids=cleanup_job_ids,
            exit_mode=EXIT_MODE_MANUAL_GHOSTTY,
        )
        online_preflight = preflight_approved_request(request)
        if now_epoch_ms() >= request["expires_at_epoch_ms"]:
            raise HelperError("The staged handoff expired during read-only preflight.")
        if online_preflight.get("desktop_offline_required") is not True:
            print(
                public_json_dumps(
                    {
                        "staged": False,
                        "launched": False,
                        "mutation_started": False,
                        "awaiting_final_approval": False,
                        "outcome": OUTCOME_ROUTE_DIRECT,
                        "next_action": NEXT_ROUTE_TO_DIRECT_APPLY,
                        "staged_plan": summary,
                        "online_preflight": online_preflight,
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return EXIT_OK
        staged_online_preflight = {
            **online_preflight,
            "outcome": OUTCOME_STAGED,
            "next_action": NEXT_LAUNCH_GHOSTTY,
        }
        job_dir, _initial = create_job(request, job_root)
        writer = ReceiptWriter(job_dir)
        writer.update(
            phase="staged_waiting_for_ghostty_launch",
            outcome=(
                OUTCOME_STAGED
                if summary["desktop_offline_required"]
                else OUTCOME_ROUTE_DIRECT
            ),
            next_action=(
                NEXT_LAUNCH_GHOSTTY
                if summary["desktop_offline_required"]
                else NEXT_ROUTE_TO_DIRECT_APPLY
            ),
            staged=True,
            final_approval_recorded=True,
            final_approval_recorded_at_epoch_ms=now_epoch_ms(),
            online_preflight=staged_online_preflight,
            staged_plan=summary,
            safety_warnings=summary["safety_warnings"],
            component_summary=summary["component_summary"],
            execution_summary=summary["execution_summary"],
            recovery_job_ids=recovery_job_ids,
        )
        public_result = {
            "staged": True,
            "launched": False,
            "mutation_started": False,
            "awaiting_final_approval": False,
            "single_approval_bound": True,
            "outcome": (
                OUTCOME_STAGED
                if summary["desktop_offline_required"]
                else OUTCOME_ROUTE_DIRECT
            ),
            "next_action": (
                NEXT_LAUNCH_GHOSTTY
                if summary["desktop_offline_required"]
                else NEXT_ROUTE_TO_DIRECT_APPLY
            ),
            "job_id": job_id,
            "job_dir": str(job_dir),
            "receipt_path": str(job_dir / RECEIPT_FILENAME),
            "expires_at_epoch_ms": request["expires_at_epoch_ms"],
            "staged_plan": summary,
            "safety_warnings": summary["safety_warnings"],
            "component_summary": summary["component_summary"],
            "execution_summary": summary["execution_summary"],
            "recovery_job_ids": recovery_job_ids,
        }
        print(
            public_json_dumps(
                public_result, ensure_ascii=False, indent=2, sort_keys=True
            )
        )
        return EXIT_OK
    finally:
        token = ""
        if request is not None:
            request["approval_token"] = ""
        sys.modules.pop(core.__name__, None)


def worker_lock_active(job_dir: Path) -> bool:
    path = job_dir / LOCK_FILENAME
    if not path.exists():
        return False
    secure_regular_file(path)
    fd = os.open(path, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def acquire_launch_lock(job_dir: Path) -> int:
    path = job_dir / LAUNCH_LOCK_FILENAME
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    os.fchmod(fd, 0o600)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
    ):
        os.close(fd)
        raise HelperError("The launch lock file is unsafe.")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise HelperError("This staged handoff is already being launched.") from exc
    return fd


def launch_staged_ghostty(args: argparse.Namespace) -> int:
    """Open Ghostty for one scope bound to the user's single approval."""

    job_dir = Path(args.job_dir).expanduser().resolve()
    secure_directory(job_dir)
    if not JOB_ID_RE.fullmatch(job_dir.name):
        raise HelperError("Invalid private job directory name.")
    lock_fd = acquire_launch_lock(job_dir)
    request: dict[str, Any] | None = None
    try:
        receipt = read_private_json(
            job_dir / RECEIPT_FILENAME, maximum=MAX_RECEIPT_BYTES
        )
        if (
            receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION
            or receipt.get("protocol_version") != PROTOCOL_VERSION
            or receipt.get("job_id") != job_dir.name
        ):
            raise HelperError("This staged handoff uses an unsupported receipt.")
        if receipt.get("terminal") is True:
            raise HelperError(
                "This staged handoff has already reached a terminal state."
            )
        if receipt.get("mutation_started") is True:
            raise HelperError("A started deletion cannot be launched again.")
        if worker_lock_active(job_dir):
            raise HelperError("A Ghostty worker for this handoff is already active.")
        if receipt.get("final_approval_recorded") is not True:
            raise HelperError("This staged handoff lacks the bound user approval.")
        phase = str(receipt.get("phase", ""))
        if phase in {"terminal_launch_submitted", "launch_unknown"}:
            raise HelperError(
                "The previous Ghostty launch outcome is not yet known; inspect the "
                "receipt and do not submit a second window."
            )
        elif phase not in {
            "staged_waiting_for_ghostty_launch",
            "terminal_launch_failed_before_worker",
            "reading_staged_request",
            "request_validated_pending_manual_exit",
            "launch_delay",
            "waiting_for_manual_exit",
            "waiting_for_offline",
            "offline_observed",
            "manual_offline_wait_failed",
        }:
            raise HelperError(f"This staged handoff cannot launch from phase: {phase}")

        raw = read_private_bytes(job_dir / REQUEST_FILENAME)
        expected_sha = receipt.get("request_sha256")
        if not isinstance(expected_sha, str) or sha256_bytes(raw) != expected_sha:
            raise RequestError("The staged request no longer matches its receipt.")
        try:
            decoded = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RequestError("The staged request is invalid JSON.") from exc
        request = validate_request(decoded, job_dir)
        if now_epoch_ms() >= request["expires_at_epoch_ms"]:
            safely_remove_unconsumed_request(job_dir)
            expired_writer = ReceiptWriter(job_dir)
            expired_writer.add_notice(
                "launch_preflight",
                "approved_request_expired",
                "The staged approval expired before mutation.",
                disposition="restage_and_reapprove",
            )
            expired_writer.transition(
                clear=("retryable",),
                phase="expired_before_mutation",
                outcome=OUTCOME_RESTAGE_REQUIRED,
                next_action=NEXT_RESTAGE,
                terminal=True,
                helper_exit_code=EXIT_PLAN_CHANGED,
                staged_capsule_revoked=True,
                deletion_success=False,
                verification_ok=False,
                permanent_deletion_complete=False,
                partial_possible=False,
                safe_to_reopen=True,
                completed_at_epoch_ms=now_epoch_ms(),
            )
            raise ExpiredRequestError(
                "The staged approval has expired; create a new stage."
            )
        try:
            online_preflight = preflight_approved_request(request)
        except (PlanChangedError, SourceChangedError) as exc:
            safely_remove_unconsumed_request(job_dir)
            stale_writer = ReceiptWriter(job_dir)
            stale_writer.add_error(
                "launch_preflight",
                type(exc).__name__,
                str(exc),
            )
            stale_writer.update(
                phase="plan_changed",
                outcome=OUTCOME_RESTAGE_REQUIRED,
                next_action=NEXT_RESTAGE,
                terminal=True,
                helper_exit_code=EXIT_PLAN_CHANGED,
                deletion_success=False,
                verification_ok=False,
                permanent_deletion_complete=False,
                partial_possible=False,
                safe_to_reopen=True,
                completed_at_epoch_ms=now_epoch_ms(),
            )
            raise
        if online_preflight.get("desktop_offline_required") is not True:
            safely_remove_unconsumed_request(job_dir)
            route_writer = ReceiptWriter(job_dir)
            route_writer.transition(
                clear=("retryable",),
                phase="route_to_direct_apply",
                outcome=OUTCOME_ROUTE_DIRECT,
                next_action=NEXT_ROUTE_TO_DIRECT_APPLY,
                terminal=True,
                helper_exit_code=EXIT_OK,
                staged_capsule_revoked=True,
                deletion_success=False,
                verification_ok=False,
                permanent_deletion_complete=False,
                partial_possible=False,
                safe_to_reopen=True,
                completed_at_epoch_ms=now_epoch_ms(),
            )
            print(
                public_json_dumps(
                    {
                        "accepted": False,
                        "launched": False,
                        "outcome": OUTCOME_ROUTE_DIRECT,
                        "next_action": NEXT_ROUTE_TO_DIRECT_APPLY,
                        "job_id": job_dir.name,
                        "receipt_path": str(job_dir / RECEIPT_FILENAME),
                        "online_preflight": online_preflight,
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return EXIT_OK
        ghostty_launch_arguments(
            job_dir,
            request["source"]["interpreter_path"],
        )
        writer = ReceiptWriter(job_dir)
        attempts = int(receipt.get("terminal_launch_attempts", 0)) + 1
        writer.transition(
            clear=("retryable", "completed_at_epoch_ms", "manual_offline_retry_at_epoch_ms"),
            phase="terminal_launch_submitted",
            outcome=OUTCOME_WAITING_OFFLINE,
            next_action=NEXT_QUIT_AND_WAIT,
            final_approval_recorded=True,
            final_approval_recorded_at_epoch_ms=now_epoch_ms(),
            terminal_launch_attempts=attempts,
            terminal_launch_submitted_at_epoch_ms=now_epoch_ms(),
            online_preflight=online_preflight,
        )
        launch_result = normalize_ghostty_launch_result(
            launch_ghostty_worker(job_dir, request["source"]["interpreter_path"])
        )
        if not launch_result.launched:
            latest = read_private_json(
                job_dir / RECEIPT_FILENAME, maximum=MAX_RECEIPT_BYTES
            )
            active_worker = worker_lock_active(job_dir)
            latest_phase = str(latest.get("phase", ""))
            latest_mutation = latest.get("mutation_started") is True
            latest_consumed = latest.get("request_consumed") is True
            if latest.get("terminal") is True:
                terminal_code = terminal_receipt_exit_code(latest)
                print(
                    public_json_dumps(
                        receipt_with_stable_action(latest),
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
                return terminal_code
            if latest_mutation or latest_consumed:
                print(
                    public_json_dumps(
                        {
                            "accepted": True,
                            "launched": True,
                            "launch_outcome": "worker_progressed",
                            "outcome": OUTCOME_PARTIAL_POSSIBLE,
                            "next_action": NEXT_KEEP_CLOSED,
                            "retryable_now": False,
                            "mutation_started": latest_mutation,
                            "request_consumed": latest_consumed,
                            "job_id": job_dir.name,
                            "job_dir": str(job_dir),
                            "receipt_path": str(job_dir / RECEIPT_FILENAME),
                            "error": launch_result.error,
                        },
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
                return EXIT_PARTIAL_OR_VERIFY_FAILED
            if active_worker or latest_phase not in {"terminal_launch_submitted"}:
                retryable = latest_phase in {
                    "reading_staged_request",
                    "request_validated_pending_manual_exit",
                    "launch_delay",
                    "waiting_for_manual_exit",
                    "waiting_for_offline",
                    "offline_observed",
                    "manual_offline_wait_failed",
                }
                print(
                    public_json_dumps(
                        {
                            "accepted": True,
                            "launched": True,
                            "launch_outcome": "worker_progressed",
                            "outcome": (
                                OUTCOME_RETRYABLE_WARNING
                                if retryable and not active_worker
                                else OUTCOME_WAITING_OFFLINE
                            ),
                            "next_action": (
                                NEXT_RETRY_LAUNCH
                                if retryable and not active_worker
                                else NEXT_QUIT_AND_WAIT
                            ),
                            "retryable_now": retryable and not active_worker,
                            "job_id": job_dir.name,
                            "job_dir": str(job_dir),
                            "receipt_path": str(job_dir / RECEIPT_FILENAME),
                            "error": launch_result.error,
                        },
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
                return EXIT_BLOCKED if retryable and not active_worker else EXIT_OK
            try:
                transition_guard_fd = acquire_worker_lock(job_dir)
            except HelperError:
                print(
                    public_json_dumps(
                        {
                            "accepted": True,
                            "launched": True,
                            "launch_outcome": "worker_progressed",
                            "outcome": OUTCOME_WAITING_OFFLINE,
                            "next_action": NEXT_QUIT_AND_WAIT,
                            "retryable_now": False,
                            "job_id": job_dir.name,
                            "job_dir": str(job_dir),
                            "receipt_path": str(job_dir / RECEIPT_FILENAME),
                            "error": launch_result.error,
                        },
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
                return EXIT_OK
            try:
                guarded_receipt = read_private_json(
                    job_dir / RECEIPT_FILENAME, maximum=MAX_RECEIPT_BYTES
                )
                if guarded_receipt.get("phase") != "terminal_launch_submitted":
                    guarded_status = receipt_with_stable_action(guarded_receipt)
                    print(
                        public_json_dumps(
                            guarded_status,
                            ensure_ascii=False,
                            indent=2,
                            sort_keys=True,
                        )
                    )
                    if guarded_receipt.get("mutation_started") is True or guarded_receipt.get(
                        "request_consumed"
                    ) is True:
                        return EXIT_PARTIAL_OR_VERIFY_FAILED
                    return EXIT_BLOCKED
                failure_writer = ReceiptWriter(job_dir)
                confirmed_failure = (
                    launch_result.submission_status == "confirmed_not_submitted"
                )
                failure_writer.transition(
                    phase=(
                        "terminal_launch_failed_before_worker"
                        if confirmed_failure
                        else "launch_unknown"
                    ),
                    outcome=(
                        OUTCOME_RETRYABLE_WARNING
                        if confirmed_failure
                        else OUTCOME_LAUNCH_UNKNOWN
                    ),
                    next_action=(
                        NEXT_RETRY_LAUNCH
                        if confirmed_failure
                        else NEXT_INSPECT_NO_RELAUNCH
                    ),
                    retryable=confirmed_failure,
                    safe_to_reopen=confirmed_failure,
                    terminal_launch_error=launch_result.error,
                )
            finally:
                try:
                    fcntl.flock(transition_guard_fd, fcntl.LOCK_UN)
                finally:
                    os.close(transition_guard_fd)
            print(
                public_json_dumps(
                    {
                        "accepted": False,
                        "launched": False,
                        "launch_outcome": (
                            "confirmed_not_submitted"
                            if confirmed_failure
                            else "unknown"
                        ),
                        "outcome": (
                            OUTCOME_RETRYABLE_WARNING
                            if confirmed_failure
                            else OUTCOME_LAUNCH_UNKNOWN
                        ),
                        "next_action": (
                            NEXT_RETRY_LAUNCH
                            if confirmed_failure
                            else NEXT_INSPECT_NO_RELAUNCH
                        ),
                        "retryable_now": confirmed_failure,
                        "job_id": job_dir.name,
                        "job_dir": str(job_dir),
                        "receipt_path": str(job_dir / RECEIPT_FILENAME),
                        "error": launch_result.error,
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return EXIT_BLOCKED
        print(
            public_json_dumps(
                {
                    "accepted": True,
                    "launched": True,
                    "outcome": OUTCOME_WAITING_OFFLINE,
                    "next_action": NEXT_QUIT_AND_WAIT,
                    "job_id": job_dir.name,
                    "job_dir": str(job_dir),
                    "receipt_path": str(job_dir / RECEIPT_FILENAME),
                    "launch_label": launch_result.label,
                    "exit_mode": EXIT_MODE_MANUAL_GHOSTTY,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return EXIT_OK
    finally:
        if request is not None:
            request["approval_token"] = ""
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def cancel_staged_handoff(args: argparse.Namespace) -> int:
    """Revoke an unstarted staged capsule without touching session data."""

    job_dir = Path(args.job_dir).expanduser().resolve()
    secure_directory(job_dir)
    if not JOB_ID_RE.fullmatch(job_dir.name):
        raise HelperError("Invalid private job directory name.")
    launch_lock_fd = acquire_launch_lock(job_dir)
    worker_lock_fd: int | None = None
    try:
        worker_lock_fd = acquire_worker_lock(job_dir)
        receipt = read_private_json(
            job_dir / RECEIPT_FILENAME, maximum=MAX_RECEIPT_BYTES
        )
        if (
            receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION
            or receipt.get("protocol_version") != PROTOCOL_VERSION
            or receipt.get("job_id") != job_dir.name
        ):
            raise HelperError("This staged handoff uses an unsupported receipt.")
        if receipt.get("terminal") is True:
            print(
                public_json_dumps(
                    receipt_with_stable_action(receipt),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            if receipt.get("phase") == "cancelled_before_mutation":
                return EXIT_OK
            return terminal_receipt_exit_code(receipt)
        if receipt.get("mutation_started") is True:
            raise HelperError("A started handoff cannot be cancelled.")
        if (job_dir / REQUEST_FILENAME).exists():
            safely_remove_unconsumed_request(job_dir)
        writer = ReceiptWriter(job_dir)
        writer.update(
            phase="cancelled_before_mutation",
            outcome=OUTCOME_CANCELLED,
            next_action=NEXT_NONE,
            terminal=True,
            helper_exit_code=EXIT_BLOCKED,
            staged_capsule_revoked=True,
            deletion_success=False,
            verification_ok=False,
            permanent_deletion_complete=False,
            partial_possible=False,
            safe_to_reopen=True,
            completed_at_epoch_ms=now_epoch_ms(),
        )
        print(
            public_json_dumps(
                {
                    "cancelled": True,
                    "mutation_started": False,
                    "job_id": job_dir.name,
                    "receipt_path": str(job_dir / RECEIPT_FILENAME),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return EXIT_OK
    finally:
        if worker_lock_fd is not None:
            try:
                fcntl.flock(worker_lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(worker_lock_fd)
        try:
            fcntl.flock(launch_lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(launch_lock_fd)


def run_ghostty_worker(job_dir: Path) -> int:
    """Run the manual handoff visibly and leave a human-readable final state."""

    print("Codex 会话删除已在 Ghostty 中准备就绪。", flush=True)
    print(
        "[1/4] 等待退出：请现在手动完全退出 ChatGPT/Codex Desktop；"
        "本助手不会主动退出或重启它。",
        flush=True,
    )
    print("检测到应用稳定离线后，才会开始删除与完整性验证。", flush=True)
    print(f"任务目录：{job_dir}", flush=True)
    try:
        exit_code = run_worker(job_dir)
    except Exception as exc:
        exit_code = EXIT_BLOCKED
        print(f"\n离线助手未能启动：{exc}", flush=True)
    else:
        try:
            receipt = read_private_json(
                job_dir.resolve() / RECEIPT_FILENAME,
                maximum=MAX_RECEIPT_BYTES,
            )
        except Exception as exc:
            print(f"\n结果回执无法读取：{exc}", flush=True)
        else:
            receipt = receipt_with_stable_action(receipt)
            print("\n—— 最终结果 ——", flush=True)
            if receipt.get("safe_to_reopen") is True and receipt.get("phase") == "complete":
                if receipt.get("outcome") == OUTCOME_NO_SAFE_WORK:
                    print(
                        "[4/4] 未执行任何安全删除；已确认状态未被修改。"
                        "你现在可以手动重新打开 Codex。"
                    )
                elif receipt.get("outcome") == OUTCOME_COMPLETE_WITH_WARNINGS:
                    print(
                        "[4/4] 安全执行和离线验证已完成，但保留了警告项。"
                        "你现在可以手动重新打开 Codex。"
                    )
                elif receipt.get("outcome") == OUTCOME_COMPLETE:
                    print(
                        "[4/4] 删除和离线验证均已成功。"
                        "你现在可以手动重新打开 Codex。"
                    )
                else:
                    print("已安全结束且未确认删除成功；请按回执指引继续。")
            elif exit_code == EXIT_PLAN_CHANGED:
                print("批准后的计划已变化，未开始删除；请回到 Codex 重新报告和批准。")
            elif exit_code == EXIT_PARTIAL_OR_VERIFY_FAILED:
                print(
                    "删除可能只完成了一部分。请保持 Codex 关闭并检查回执，勿直接重试。"
                )
            else:
                print("当前安全警告需要后续操作，尚未确认永久删除成功。")
            print(f"阶段：{receipt.get('phase', 'unknown')}")
            print(f"结果：{receipt.get('outcome', 'unknown')}")
            print(f"下一步：{receipt.get('next_action', NEXT_RESTAGE)}")
            print(f"是否开始修改：{bool(receipt.get('mutation_started'))}")
            print(f"删除验证成功：{bool(receipt.get('permanent_deletion_complete'))}")
            if verified_success_receipt(receipt):
                cleanup = cleanup_verified_job_chain(job_dir, receipt)
                if cleanup.get("cleanup_complete") is True:
                    print(
                        "临时请求、回执和失败任务链已自动清理"
                        f"（{cleanup.get('cleaned_job_count', 0)} 个任务目录）。",
                        flush=True,
                    )
                else:
                    print(
                        "删除已经验证成功，但临时任务资料尚未完全清理："
                        f"{cleanup.get('error', '未知清理错误')}",
                        flush=True,
                    )
                    print(f"待清理回执：{job_dir.resolve() / RECEIPT_FILENAME}", flush=True)
            else:
                print(f"回执：{job_dir.resolve() / RECEIPT_FILENAME}", flush=True)
    print("\n任务进程已结束；按任意键关闭此 Ghostty 窗口。", flush=True)
    return exit_code


def compact_receipt_status(job_dir: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    enriched = receipt_with_runtime_action(job_dir, receipt)
    request = enriched.get("request", {})
    errors = enriched.get("errors", [])
    warnings = enriched.get("safety_warnings", [])
    notices = enriched.get("operational_notices", [])
    return {
        "job_id": enriched.get("job_id", job_dir.name),
        "phase": enriched.get("phase", "unknown"),
        "outcome": enriched.get("outcome", OUTCOME_FAILED),
        "next_action": enriched.get("next_action", NEXT_RESTAGE),
        "terminal": bool(enriched.get("terminal")),
        "mutation_started": bool(enriched.get("mutation_started")),
        "permanent_deletion_complete": bool(
            enriched.get("permanent_deletion_complete")
        ),
        "verification_ok": bool(enriched.get("verification_ok")),
        "safe_to_reopen": bool(enriched.get("safe_to_reopen")),
        "worker_active": enriched.get("worker_active"),
        "liveness_reason": enriched.get("liveness_reason"),
        "cleanup_pending": bool(enriched.get("cleanup_pending")),
        "cleanup_pending_job_ids": list(
            enriched.get("cleanup_pending_job_ids", [])
            if isinstance(enriched.get("cleanup_pending_job_ids", []), list)
            else []
        ),
        "request": {
            "approval_scope": request.get("approval_scope", "")
            if isinstance(request, dict)
            else "",
            "session_ids": request.get("session_ids", [])
            if isinstance(request, dict)
            else [],
            "cleanup_job_ids": request.get("cleanup_job_ids", [])
            if isinstance(request, dict)
            else [],
        },
        "diagnostics": {
            "error_count": len(errors) if isinstance(errors, list) else 0,
            "warning_count": len(warnings) if isinstance(warnings, list) else 0,
            "notice_count": len(notices) if isinstance(notices, list) else 0,
            "latest_error": errors[-1] if isinstance(errors, list) and errors else None,
            "latest_warning": (
                warnings[-1] if isinstance(warnings, list) and warnings else None
            ),
        },
        "receipt_path": str(job_dir / RECEIPT_FILENAME),
        "updated_at_epoch_ms": enriched.get("updated_at_epoch_ms"),
    }


def read_status(job_dir: Path, *, full: bool = False) -> int:
    job_dir = job_dir.expanduser().resolve()
    secure_directory(job_dir)
    payload = read_private_json(job_dir / RECEIPT_FILENAME, maximum=MAX_RECEIPT_BYTES)
    payload = (
        receipt_with_runtime_action(job_dir, payload)
        if full
        else compact_receipt_status(job_dir, payload)
    )
    print(public_json_dumps(payload, indent=2, sort_keys=True))
    return EXIT_OK


def cleanup_completed_handoff(job_dir: Path) -> int:
    job_dir = job_dir.expanduser().resolve()
    secure_directory(job_dir)
    receipt = read_private_json(job_dir / RECEIPT_FILENAME, maximum=MAX_RECEIPT_BYTES)
    result = cleanup_verified_job_chain(job_dir, receipt)
    print(public_json_dumps(result, indent=2, sort_keys=True))
    return EXIT_OK if result.get("cleanup_complete") is True else EXIT_BLOCKED


def fresh_target_absence_evidence(
    receipt: dict[str, Any],
    job_dir: Path,
) -> dict[str, Any]:
    """Recheck target absence with the current read-only engine."""

    request = receipt.get("request", {})
    if not isinstance(request, dict):
        raise HelperError("The recovery receipt lacks request metadata.")
    codex_home_raw = request.get("codex_home")
    session_ids_raw = request.get("session_ids", [])
    options = request.get("options", {})
    if (
        not isinstance(codex_home_raw, str)
        or not isinstance(session_ids_raw, list)
        or not isinstance(options, dict)
    ):
        raise HelperError("The recovery receipt has malformed request metadata.")
    codex_home, session_ids = validate_session_inputs(
        Path(codex_home_raw),
        session_ids_raw,
    )
    expected_root = codex_home / DEFAULT_JOB_ROOT_NAME
    if job_dir.parent != expected_root:
        raise HelperError("The recovery job is outside its exact private job root.")
    core_path = Path(__file__).resolve().parent / CORE_SCRIPT_FILENAME
    core_source = safe_source_bytes(core_path)
    core = load_verified_core_module(core_path, core_source)
    plan = core.make_plan(
        codex_home=codex_home,
        root_ids=session_ids,
        include_subagents=bool(options.get("include_subagents", True)),
        include_logs=bool(options.get("include_logs", True)),
        scan_historical=bool(options.get("scan_historical", True)),
    )
    core_receipt = receipt.get("core", {})
    core_result = (
        core_receipt.get("result", {}) if isinstance(core_receipt, dict) else {}
    )
    apply_result = (
        core_result.get("apply_result", {})
        if isinstance(core_result, dict)
        else {}
    )
    approved_snapshot = (
        apply_result.get("approved_execution_snapshot", {})
        if isinstance(apply_result, dict)
        else {}
    )
    object_contracts = (
        approved_snapshot.get("object_contracts", {})
        if isinstance(approved_snapshot, dict)
        else {}
    )
    expected_state_path = str(object_contracts.get("state_database_path", ""))
    expected_logs_path = str(object_contracts.get("logs_database_path", ""))
    if expected_state_path and str(plan.state_database_path or "") != expected_state_path:
        raise HelperError("The selected state database changed before recovery verification.")
    if expected_logs_path and str(plan.logs_database_path or "") != expected_logs_path:
        raise HelperError("The selected logs database changed before recovery verification.")
    if plan.blockers:
        raise HelperError(
            "Fresh target-absence verification has blockers: "
            + " | ".join(str(item) for item in plan.blockers)
        )
    if plan.preflight.get("current_session_is_target") is True:
        raise HelperError("A recovery target is the current invoking session.")
    if int(plan.preflight.get("target_evidence_items", 0) or 0) != 0:
        raise HelperError("Fresh target evidence remains after the retained partial job.")
    nonzero_counts = {
        key: int(plan.counts.get(key, 0) or 0)
        for key in TARGET_ABSENCE_COUNT_KEYS
        if int(plan.counts.get(key, 0) or 0) != 0
    }
    if nonzero_counts:
        raise HelperError(
            "Fresh target-owned counts remain: "
            + ", ".join(f"{key}={value}" for key, value in sorted(nonzero_counts.items()))
        )
    if plan.unsafe_paths:
        raise HelperError("Unsafe target paths prevent recovery finalization.")
    preflight = plan.preflight
    for present_key, check_key, label in [
        ("state_db_present", "state_quick_check", "state"),
        ("logs_db_present", "logs_quick_check", "logs"),
        ("desktop_catalog_db_present", "desktop_catalog_quick_check", "desktop catalog"),
    ]:
        if preflight.get(present_key) and preflight.get(check_key) != "ok":
            raise HelperError(f"Fresh {label} integrity verification is not healthy.")
    for issues_key in [
        "managed_path_issues",
        "session_index_issues",
        "state_schema_issues",
        "logs_schema_issues",
        "desktop_catalog_schema_issues",
        "global_state_issues",
        "target_graph_issues",
    ]:
        if preflight.get(issues_key):
            raise HelperError(
                f"Fresh target-absence verification found {issues_key}."
            )
    historical = plan.historical_residuals
    historical_summary = (
        historical.get("summary", {}) if isinstance(historical, dict) else {}
    )
    return {
        "checked_at_epoch_ms": now_epoch_ms(),
        "engine_script_version": getattr(core, "SCRIPT_VERSION", "unknown"),
        "target_ids": session_ids,
        "target_evidence_items": 0,
        "target_counts": {key: 0 for key in sorted(TARGET_ABSENCE_COUNT_KEYS)},
        "integrity_checks": {
            "state": preflight.get("state_quick_check", "missing"),
            "logs": preflight.get("logs_quick_check", "missing"),
            "desktop_catalog": preflight.get(
                "desktop_catalog_quick_check", "missing"
            ),
        },
        "later_historical_residuals": {
            "scanned": bool(historical.get("scanned", False))
            if isinstance(historical, dict)
            else False,
            "authoritative": bool(historical.get("authoritative", False))
            if isinstance(historical, dict)
            else False,
            "total_ids": int(historical_summary.get("total_ids", 0) or 0),
            "total_items": int(historical_summary.get("total_items", 0) or 0),
            "disposition": "later_unapproved_preserved",
        },
    }


def recover_empty_historical_handoff(job_dir: Path) -> int:
    """Finalize only the narrowly proven empty-historical-snapshot anomaly."""

    job_dir = job_dir.expanduser().resolve()
    secure_directory(job_dir)
    if not JOB_ID_RE.fullmatch(job_dir.name):
        raise HelperError("Invalid private recovery job directory name.")
    receipt = read_private_json(job_dir / RECEIPT_FILENAME, maximum=MAX_RECEIPT_BYTES)
    if (
        receipt.get("schema_version") not in {5, RECEIPT_SCHEMA_VERSION}
        or receipt.get("protocol_version") not in {5, PROTOCOL_VERSION}
        or receipt.get("job_id") != job_dir.name
    ):
        raise HelperError("The recovery receipt version or identity is unsupported.")
    inactive, reason = inactive_job_locks(job_dir)
    if not inactive:
        raise HelperError(reason)
    enriched = receipt_with_stable_action(receipt)
    if not empty_historical_recovery_candidate(enriched):
        raise HelperError(
            "The retained job is not the narrowly supported empty-historical anomaly."
        )
    evidence = fresh_target_absence_evidence(enriched, job_dir)
    outcome = (
        OUTCOME_COMPLETE_WITH_WARNINGS
        if enriched.get("safety_warnings")
        else OUTCOME_COMPLETE
    )
    writer = ReceiptWriter(job_dir)
    writer.update(
        phase="complete",
        outcome=outcome,
        next_action=NEXT_REOPEN,
        terminal=True,
        success=True,
        deletion_success=True,
        verification_ok=True,
        permanent_deletion_complete=True,
        partial_possible=False,
        retryable=False,
        safe_to_reopen=True,
        helper_exit_code=EXIT_OK,
        recovered_empty_historical_snapshot=True,
        recovery_verification=evidence,
        recovered_at_epoch_ms=now_epoch_ms(),
    )
    cleanup = cleanup_verified_job_chain(job_dir, writer.payload)
    result = {
        "recovered": True,
        "outcome": outcome,
        "next_action": NEXT_REOPEN,
        "verification_ok": True,
        "later_historical_residuals": evidence["later_historical_residuals"],
        **cleanup,
    }
    print(public_json_dumps(result, indent=2, sort_keys=True))
    return EXIT_OK if cleanup.get("cleanup_complete") is True else EXIT_BLOCKED


def audit_jobs_command(codex_home: Path) -> int:
    result = audit_job_root(codex_home)
    print(public_json_dumps(result, indent=2, sort_keys=True))
    return EXIT_OK


def self_check() -> int:
    version = [sys.version_info.major, sys.version_info.minor, sys.version_info.micro]
    checks: dict[str, Any] = {
        "platform": sys.platform,
        "python": sys.executable,
        "python_version": version,
        "helper_path": str(Path(__file__).resolve()),
        "core_path": str(Path(__file__).resolve().parent / CORE_SCRIPT_FILENAME),
    }
    try:
        ghostty_app = validated_ghostty_app()
        with (ghostty_app / "Contents" / "Info.plist").open("rb") as stream:
            ghostty_metadata = plistlib.load(stream)
        checks["ghostty_app"] = str(ghostty_app)
        checks["ghostty_version"] = ghostty_metadata.get(
            "CFBundleShortVersionString", ""
        )
        checks["ghostty_applescript_available"] = True
    except (HelperError, OSError, plistlib.InvalidFileException) as exc:
        checks["ghostty_applescript_available"] = False
        checks["ghostty_error"] = str(exc)
    try:
        checks["osascript_path"] = str(validated_osascript())
        checks["osascript_available"] = True
    except HelperError as exc:
        checks["osascript_available"] = False
        checks["osascript_error"] = str(exc)
    try:
        helper_bytes = safe_source_bytes(Path(__file__).resolve())
        core_bytes = safe_source_bytes(Path(checks["core_path"]))
        checks["helper_sha256"] = sha256_bytes(helper_bytes)
        checks["core_sha256"] = sha256_bytes(core_bytes)
        checks["source_files_safe"] = True
    except (OSError, HelperError) as exc:
        checks["source_files_safe"] = False
        checks["source_error"] = str(exc)
    try:
        checks["interpreter_contract"] = current_interpreter_contract()
        checks["interpreter_supported"] = True
    except (OSError, HelperError) as exc:
        checks["interpreter_supported"] = False
        checks["interpreter_error"] = str(exc)
    checks["manual_ghostty_handoff_available"] = bool(
        sys.platform == "darwin"
        and checks.get("ghostty_applescript_available")
        and checks.get("osascript_available")
        and checks.get("source_files_safe")
        and checks.get("interpreter_supported")
    )
    checks["automatic_quit_supported"] = False
    checks["automatic_restart_supported"] = False
    checks["ok"] = checks["manual_ghostty_handoff_available"]
    print(public_json_dumps(checks, indent=2, sort_keys=True))
    return EXIT_OK if checks["ok"] else EXIT_BLOCKED


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely hand an approved Codex deletion to an offline worker."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_stage_arguments(
        command_parser: argparse.ArgumentParser,
    ) -> None:
        command_parser.add_argument("session_ids", nargs="+")
        command_parser.add_argument(
            "--codex-home", default=os.environ.get("CODEX_HOME", "~/.codex")
        )
        command_parser.add_argument("--job-root")
        command_parser.add_argument("--no-subagents", action="store_true")
        command_parser.add_argument("--no-logs", action="store_true")
        command_parser.add_argument("--force-open", action="store_true")
        command_parser.add_argument("--no-historical-scan", action="store_true")
        command_parser.add_argument("--apply-historical-residuals", action="store_true")
        command_parser.add_argument(
            "--apply-missing-rollout-threads", action="store_true"
        )
        command_parser.add_argument(
            "--expires-in", type=expiration_seconds, default=86_400
        )
        command_parser.add_argument("--launch-delay", type=float, default=0.0)
        command_parser.add_argument("--quit-timeout", type=float, default=1200.0)
        command_parser.add_argument("--offline-stability", type=float, default=2.0)
        command_parser.add_argument("--poll-interval", type=float, default=0.25)
        command_parser.add_argument(
            "--confirm-plan-fingerprint",
            required=True,
            help="Bind staging to the unchanged selected approval scope.",
        )
        command_parser.add_argument(
            "--supersedes-job-dir",
            action="append",
            default=[],
            help=(
                "Carry one terminal failed job into the bounded cleanup chain; "
                "repeat for multiple predecessors."
            ),
        )
        command_parser.add_argument(
            "--recovers-partial-job-dir",
            action="append",
            default=[],
            help=(
                "Link one strictly verified partial job whose target deletion "
                "succeeded and whose historical component alone remains."
            ),
        )

    stage = subparsers.add_parser(
        "stage",
        help="Freeze one approved, unchanged deletion scope without mutating.",
    )
    add_stage_arguments(stage)

    launch_ghostty = subparsers.add_parser(
        "launch-ghostty",
        help="Open Ghostty for an approved and fingerprint-bound staged scope.",
    )
    launch_ghostty.add_argument("--job-dir", required=True)

    cancel_staged = subparsers.add_parser(
        "cancel-staged",
        help="Revoke an unstarted staged capsule without deleting session data.",
    )
    cancel_staged.add_argument("--job-dir", required=True)

    ghostty_worker = subparsers.add_parser("ghostty-worker")
    ghostty_worker.add_argument("--job-dir", required=True)

    status = subparsers.add_parser("status")
    status.add_argument("--job-dir", required=True)
    status.add_argument(
        "--full",
        action="store_true",
        help="Print the complete retained diagnostic receipt instead of a compact status.",
    )

    cleanup_completed = subparsers.add_parser(
        "cleanup-completed",
        help="Retry strict metadata cleanup for one verified successful job.",
    )
    cleanup_completed.add_argument("--job-dir", required=True)

    audit_jobs = subparsers.add_parser(
        "audit-jobs",
        help="Read-only classification of every exact private deletion job child.",
    )
    audit_jobs.add_argument(
        "--codex-home", default=os.environ.get("CODEX_HOME", "~/.codex")
    )

    recover_empty_historical = subparsers.add_parser(
        "recover-empty-historical",
        help=(
            "Reverify and strictly clean one retained job whose only failure was "
            "an approved empty historical snapshot."
        ),
    )
    recover_empty_historical.add_argument("--job-dir", required=True)

    subparsers.add_parser("self-check")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "stage":
            return stage_handoff(args)
        if args.command == "launch-ghostty":
            return launch_staged_ghostty(args)
        if args.command == "cancel-staged":
            return cancel_staged_handoff(args)
        if args.command == "ghostty-worker":
            return run_ghostty_worker(Path(args.job_dir))
        if args.command == "status":
            return read_status(Path(args.job_dir), full=args.full)
        if args.command == "cleanup-completed":
            return cleanup_completed_handoff(Path(args.job_dir))
        if args.command == "audit-jobs":
            return audit_jobs_command(Path(args.codex_home))
        if args.command == "recover-empty-historical":
            return recover_empty_historical_handoff(Path(args.job_dir))
        if args.command == "self-check":
            return self_check()
        raise HelperError(f"Unsupported command: {args.command}")
    except (PlanChangedError, SourceChangedError) as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "exit_code": EXIT_PLAN_CHANGED,
                    "outcome": OUTCOME_RESTAGE_REQUIRED,
                    "next_action": NEXT_RESTAGE,
                    "success": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return EXIT_PLAN_CHANGED
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "exit_code": EXIT_BLOCKED,
                    "outcome": OUTCOME_FAILED,
                    "next_action": NEXT_FIX_INPUT,
                    "success": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return EXIT_BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
