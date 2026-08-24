# Offline Ghostty Handoff

Use this workflow when `desktop_offline_required` is true and Desktop is running, or when inspecting an existing staged job. The helper never quits or reopens Desktop automatically.

Treat a selected paginated-history database as Desktop-owned state. Stage and mutate it only through this offline workflow when Desktop ownership is present, and complete its row-contract, schema, integrity, and residual verification before telling the user to reopen Desktop.

Use Ghostty 1.3.0 or later through its native AppleScript `surface configuration` API. Create one new Ghostty window with the private worker command, the private job root as its initial working directory, a private environment, and `wait after command`. Keeping the working directory outside the current job child allows verified-success metadata to be removed safely. Do not use `open -na ... -e`, `.command` files, pasted input, or macOS built-in Terminal; those paths can create an extra default window, trigger Ghostty's external-command confirmation, or show a red abnormal-exit result.

macOS may show its own Automation/TCC prompt the first time Codex controls Ghostty. This is an operating-system permission and is separate from deletion approval. If it is denied, record that launch was not submitted and stop without mutation. Do not fall back to another terminal.

Before reporting, staging, launching, retrying, or inspecting any deletion, run the bounded read-only private-job audit:

```bash
python3 scripts/delete_codex_session_offline_helper.py audit-jobs \
  --codex-home <codex-home>
```

The audit inspects only the exact job root and its immediate canonical job-ID children. It never follows symbolic links, infers cleanup from age, or broadly deletes the root. It reports verified successes ready for strict cleanup, the one narrowly recoverable empty-historical anomaly, supersedable terminal failures, retryable pre-mutation jobs, active work, genuine partial states, and unsafe or unrecognized entries. The audit itself does not mutate or grant authority.

## Single approval model

The report-time approval is one combined authorization. The same user response selects the exact deletion scope and authorizes permanent deletion, creation of the private staged request, Ghostty launch, and the user's manual Desktop quit/reopen. It also binds exclusions, recursive subagents, logs, open/unknown sessions, historical residuals, preserved warnings, and later-unapproved objects.

Staging reruns the report and compares the selected semantic approval-scope fingerprint with the approved report. The fingerprint comes from a versioned explicit allowlist, not the complete capsule: it binds roots/options, recursive membership, executable and retained targets, exact approved object locators/identities, selected database paths with stable main-file identities, target-affecting protected references, and the selected historical snapshot. It excludes process observations, diagnostics, compatible unrelated schema inventory, volatile size/mtime metadata, and SQLite `-wal`/`-shm` lifecycle. Only an exact semantic-scope match may write the private `0600` request.

After that match, staging records final approval as already bound and captures a fresh complete execution snapshot for the worker. This snapshot may legitimately differ from report-time runtime evidence. Staging must not introduce another conversational prompt merely because a compatible additive schema object appeared, a process exited, or SQLite created/removed/touched a sidecar.

If recursive target membership, an approved object locator/identity, a selected target row, a target-affecting protected reference, selected historical snapshot, or selected scope changed, return `plan_changed` with `next_action: restage`, show the semantic delta, and obtain one fresh combined approval. Unselected diagnostics and runtime-only drift do not require another approval. No mutation or Ghostty launch may occur under stale authority.

Between staging and mutation, the worker rechecks the staged execution snapshot transactionally. A new target row, changed selected row, incompatible anchor, new target-bearing extension, or selected main-file replacement retains the whole affected paginated target before any of its target components mutate; independent targets may continue. This is a preservation decision under existing authority, not a reason to widen the delete set.

Do not expose or transport the approval capsule through argv, stdin, environment variables, logs, receipts, or terminal text. The selected-scope fingerprint is machine consistency data: pass it only to the local staging helper and never show it to the user.

## Commands

Stage the selected scope:

```bash
python3 scripts/delete_codex_session_offline_helper.py stage \
  <session-id> [<session-id> ...] [selected-scope-options] \
  --confirm-plan-fingerprint <approved-scope-fingerprint> \
  [--supersedes-job-dir <terminal-failed-job-dir> ...]
```

Immediately after a successful stage under the same combined approval, launch only by private job path:

```bash
python3 scripts/delete_codex_session_offline_helper.py launch-ghostty \
  --job-dir <job-dir>
```

Inspect progress or the retained Ghostty receipt. Status is compact and read-only by default:

```bash
python3 scripts/delete_codex_session_offline_helper.py status \
  --job-dir <job-dir>
```

Add `--full` only when retained failure details are needed. Retry strict cleanup for a verified success whose metadata could not be removed:

```bash
python3 scripts/delete_codex_session_offline_helper.py cleanup-completed \
  --job-dir <job-dir>
```

Recover the recognized older false-partial state caused only by an approved empty historical snapshot:

```bash
python3 scripts/delete_codex_session_offline_helper.py recover-empty-historical \
  --job-dir <job-dir>
```

This recovery command performs no new session or historical deletion. It may finalize only when the exact private directory is inactive, the retained receipt matches the narrow engine anomaly, all original target components were already verified absent, required integrity checks were healthy, selected database identities remain unchanged, and a fresh read-only report still finds no target evidence. Historical objects discovered later were never in the approved empty snapshot and remain preserved.

Revoke an unstarted staged request when approval is declined:

```bash
python3 scripts/delete_codex_session_offline_helper.py cancel-staged \
  --job-dir <job-dir>
```

## State machine

Interpret the receipt, not Terminal window visibility:

1. `staged_waiting_for_ghostty_launch`: approved private request exists; Ghostty launch has not been submitted.
2. `terminal_launch_submitted`: Ghostty launch submission succeeded, but the worker result is unknown. This legacy field name remains an internal receipt compatibility detail.
3. `waiting_for_manual_exit`: request remains unconsumed and no mutation has started.
4. `applying`: Desktop stayed absent for the stability interval and mutation started.
5. `verifying`: deletion ran and offline verification is in progress.
6. `complete`: deletion and required verification succeeded; manual reopen is allowed and strict metadata cleanup begins after Ghostty prints the result.
7. `plan_changed`: no mutation; stage a fresh plan.
8. `partial_possible`: mutation may be incomplete; keep Desktop offline and inspect the receipt.

Ghostty should present four human phases: waiting for Desktop exit, deleting safe items, verifying, and safe to reopen. It must also show preserved warning items separately from failures. The worker itself exits normally after printing its result so Ghostty does not paint an application-level red failure. Before cleanup, the receipt is the machine source of truth; after verified-success cleanup, Ghostty's displayed result and removal of the exact job directory are the terminal state. Ghostty's `wait after command` keeps the finished surface available; the app may render this as “process completed” rather than literally closing the window on a keypress.

## Relaunch and retry

Use exactly one `next_action`:

- `quit_desktop`: Ghostty is waiting; the user must fully exit Desktop.
- `relaunch_same_job`: a prior worker reached a retryable pre-mutation waiting state, the request remains unconsumed, and no worker is active.
- `wait_for_receipt`: launch submission outcome is unknown; do not resubmit.
- `restage`: source, target membership, selected scope, interpreter, request, or approval contract changed before mutation.
- `inspect_partial`: mutation started and completion is uncertain; do not retry automatically.
- `reopen_desktop`: the worker completed deletion and offline verification; the user may reopen Desktop without another mandatory scan.
- `retry_cleanup`: deletion is already verified, but strict private-metadata cleanup remains pending; reopening Desktop is still safe.
- `reopen_and_verify`: legacy receipt alias only.
- `none`: cancellation or post-reopen verification is complete.

Closing Ghostty while it is safely waiting may leave the request reusable. Relaunch only when the receipt proves no mutation, no active worker, and an unconsumed valid request. If launch submission outcome is unknown, inspect status rather than submitting again.

An offline timeout is retryable when the request is unconsumed and mutation did not start. Expired or cancelled jobs require a fresh stage. Never reuse a consumed request.

## Completion and recovery

A successful `/usr/bin/osascript` call means only that Ghostty launch was submitted. Report deletion success only after Ghostty or a retained receipt reports `complete`, deletion success, and verification success.

After verified success, the helper prints the final result and strictly removes the current job plus its explicit failed-predecessor chain. It accepts only same-user private real directories containing the known receipt/lock files; a request, symbolic link, unfamiliar entry, active lock, boundary mismatch, or permission anomaly stops cleanup. In that case the current success receipt remains with `cleanup_pending`, and `cleanup-completed` may retry the exact metadata cleanup later. Never recursively force an uncertain directory.

Failure, partial completion, cancellation requiring diagnosis, or a cleanup anomaly retains the receipt. A successor stage may use `--supersedes-job-dir` for a terminal failed job; the helper validates and carries forward its bounded predecessor IDs. When the successor finally verifies success, all still-present members of that chain are cleaned before the current job. Missing predecessor directories count as already cleaned.

After verified success, the user manually reopens Desktop. Do not require a post-reopen report merely to reconfirm the completed deletion. Run another report only when the user asks for a new residual scan, UI state still looks wrong, or cleanup remains pending.

If `partial_possible`, keep Desktop offline. Report removed, preserved, uncertain, and recovery-file objects; run read-only status or verification first; obtain fresh authority before any new mutation.

Run `audit-jobs` again on every later skill invocation. Strictly clean a verified success only with authority, offer `recover-empty-historical` only for its exact recognized anomaly, attach a terminal failure only to an explicitly approved successor lineage, and preserve active, pending, genuinely partial, malformed, linked, boundary-violating, or otherwise uncertain directories. Keep the job-root directory itself; only exact verified job children may be removed.
