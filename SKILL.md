---
name: delete-codex-session
description: Report and permanently delete one or more Codex Desktop/CLI sessions by thread or session ID, including recursive subagents and related files, databases, UI state, and optional historical residuals. Use for delete, purge, remove, or cleanup requests. Judge evolving storage from authoritative ownership evidence and shadow-executed mutation effects, continue independent safe work, obtain one combined scope-and-deletion approval, and use a staged Ghostty handoff when Desktop must be offline.
---

# Delete Codex Session

Use the bundled scripts for all discovery and mutation. Never reconstruct deletion commands manually because one session can span files, SQLite databases, Desktop metadata, and global UI state.

Run commands from this skill directory. Prefer one batch invocation when the user provides multiple IDs. Run from a session other than every requested target.

## Required references

- Read [references/safety-contract.md](references/safety-contract.md) before interpreting diagnostics or applying a plan.
- Read [references/result-schema.md](references/result-schema.md) before presenting a report, approval request, result, or recovery status.
- Read [references/offline-handoff.md](references/offline-handoff.md) on every invocation because the mandatory private-job audit may discover staged, successful, failed, or recoverable offline work.
- Read [references/runtime-schema-compatibility.md](references/runtime-schema-compatibility.md) when a database has a newer version, unknown tables/columns, runtime-protected IDs, or a schema-related warning.

## Core behavior

- Treat diagnostics as evidence to interpret, not keywords that automatically forbid deletion. Decide from authoritative ownership, exact target contracts, shadow mutation effects, and the real dependency graph.
- Skip and preserve the warned object, component, target, or connected target group. Continue every independent operation that remains safe and authorized.
- Never describe an expected preserved object as an apply failure. Report it under `completed_with_warnings` or `no_safe_work`.
- Keep validation errors, missing authority, concurrent mutation ownership, plan/approval conflicts, and post-mutation uncertainty as gates with an explicit `next_action`; they are not ordinary safety warnings.
- Never delete the invoking session. Preserve it with a safety warning and continue independent targets.
- Never broaden approval to later objects, newly discovered targets, or a wider historical scope.
- Keep three contracts separate. The approval-scope contract is an explicit semantic allowlist of the user's authority; the staged execution snapshot is fresh runtime evidence; transactional prewrite checks are the last mutation guard. Never hash a whole report, capsule, SQLite family, or diagnostic blob as approval authority.
- Discover `state_*.sqlite`, `logs_*.sqlite`, `thread_history_*.sqlite`, Desktop catalog databases, and auxiliary thread databases by required structural anchors at runtime. Paginated history requires the canonical projection/turn/item anchors and exact thread locator columns. Desktop and auxiliary roles may be selected from any safe `.db` or `.sqlite` file under the managed SQLite directory; filenames are diagnostic labels, not mutation authority. If multiple files expose the same role anchor, preserve that family as ambiguous; never guess by version or preferred filename.
- Do not treat a higher SQLite `user_version`, unrelated new table, additive field, index, or read-only view as unsafe by itself. Require the stable anchor contracts used by the deletion, inspect schema extensions at runtime, and freeze the complete assessment into the plan.
- Unknown schema objects, triggers, foreign keys, newer versions, and extra filename UUIDs are not unsafe by presence alone. The engine runs the exact deterministic mutation against an in-memory database clone and freezes a row-level effect envelope. Accept an extension when the shadow result is integral, changes only target-owned rows, and removes every target reference it is responsible for. Preserve only the affected dependency when effects escape scope, target references remain, or inspection is incomplete.
- Treat state `threads.rollout_path` as authoritative artifact ownership. UUIDs embedded in a filename are hints. A path with several UUID hints is usable when exactly one live state thread claims it; preserve it only when authoritative owners conflict or no unique owner can be proven.
- Assess auxiliary databases independently. One incompatible or target-bearing auxiliary database is preserved by its own frozen contract while compatible auxiliary databases continue.
- Let the currently selected capable model interpret the full compatibility, ownership, and mutation-effect evidence. Do not encode brittle product-version guesses or long lists of forbidden shapes into the prompt. The model may accept evidence-backed target-contained evolution and recommend scope, but it never supplies mutation SQL, adds selectors, waives integrity/identity failures, or treats confidence as evidence when the effect envelope is indeterminate.
- Use Ghostty 1.3.0 or later for an offline handoff, through its native AppleScript `surface configuration` API. Create exactly one configured Ghostty window with the private worker command and `wait after command`; do not use `open -na ... -e`, `.command` files, pasted input, or macOS built-in Terminal.
- Treat the one-time macOS Automation/TCC prompt for controlling Ghostty as an operating-system permission, not another deletion approval. Never promise that Codex can suppress it. If native Ghostty automation is unavailable or denied, stop safely instead of falling back to a different terminal.
- Treat Desktop offline status as an ownership question, not an application-bundle census. The main application and its embedded Codex session backend are mutable-state owners. Crash reporters, renderers, network/storage services, plugin hosts, and other auxiliary processes do not block merely because their executable lives inside the app bundle; require affirmative state-ownership evidence before treating an auxiliary as an owner.

## 0. Audit private jobs every time

Before the session report or any recovery action, always run the bounded read-only job-root audit:

```bash
python3 scripts/delete_codex_session_offline_helper.py audit-jobs \
  --codex-home <codex-home>
```

Inspect every exact job classification and handle it by contract:

- `verified_success_cleanup_ready`: run `cleanup-completed` as routine strict metadata finalization during a deletion or cleanup invocation. It never reruns deletion and needs no second deletion approval when the retained receipt already records the approved cleanup lifecycle.
- `recoverable_empty_historical_snapshot`: include the exact recovery job in the current combined approval, then run `recover-empty-historical`. This command may finalize only when the original approved historical snapshot was empty, every target component already verified absent, integrity is healthy, and a fresh read-only report still finds no target evidence. Later historical objects remain unapproved and preserved.
- `recoverable_skipped_historical_component`: the target components and integrity checks are already verified, while a non-empty approved historical component was skipped before its own first write. Obtain a fresh exact historical scope in the current combined approval and stage it with `--recovers-partial-job-dir <exact-job-dir>`. The fresh plan must contain no target work; ordinary supersession is forbidden.
- `terminal_failure_supersedable`: preserve it until a newly approved offline job can name its exact directory with `--supersedes-job-dir`; a later verified success then removes the bounded failed chain.
- `retryable_pre_mutation`: relaunch only when the receipt still proves an unconsumed valid request, no worker is active, and the current request is to continue that operation.
- `restage_required_pre_mutation`: the inactive worker made no mutation, but its request expired or its source contract changed; revoke the old private request and include a fresh stage in the current combined approval.
- `partial_possible_preserved`: inspect `status --full`; never delete, retry, or supersede it unless a supported narrow recovery classification is produced.
- `active` or `pending_preserved`: do not interfere; report the current action. Do not call an unlocked waiting receipt active: status and audit must use the exact worker/launch locks plus request validity to distinguish a live wait from a dead pre-mutation worker.
- `unsafe_preserved` or unrecognized entries: preserve and report the exact safety reason. Never recurse into, repair, or broadly remove them.

Ignore root-level `.DS_Store` as Finder metadata, not as a deletion job. Keep the private job-root directory itself even when no jobs remain. A purely read-only user request authorizes the audit but not metadata cleanup. For deletion or cleanup requests, fold every exact audit action requiring current authority into the same combined approval; do not create a separate approval round.

## 1. Report

Run a machine-readable report before every deletion:

```bash
python3 scripts/delete_codex_session.py <session-id> [<session-id> ...] --json
```

Read the full plan, including target roots, resolved subagents and touching spawn edges, exact object counts, byte size, historical scan, safety warnings, preserved objects, applicable scopes, `outcome`, and `next_action`.

When present, also read the selected `state_database`, `logs_database`, and `paginated_history_database_plan`, `state_mutation_effect_assessment`, paginated `mutation_effect_assessment`, `artifact_ownership_evidence`, schema-compatibility records, frozen target-row contracts, auxiliary database plans, historical protected IDs, scan completeness, and unresolved target-reference hits. Explain the effect boundary and ownership basis without dumping row hashes or internal capsules.

Summarize the result in this order:

1. safely deletable session count and size
2. preserved/skipped session and object count with warning reasons
3. whether Desktop must be offline
4. private-job audit counts and the exact automatic cleanup, recovery, predecessor-link, or preservation actions
5. every applicable deletion scope, with the locally preferred scope first
6. one complete, copyable combined-approval response for every applicable scope

Keep ordinary historical residuals separate from target-session objects. Keep state threads missing rollout files separate from ordinary historical residuals. If historical scanning did not run, explain why and do not offer historical cleanup.

Treat a recent canonical ID that exists only in the structurally selected `logs_*.sqlite` database as transient worker activity, not as a frozen historical residual. Do not expose those internal IDs. Once the activity is outside the bounded protection window, or the ID has any non-log artifact/reference, classify it normally.

Do not expose approval capsules, token keys, digests, fingerprints, or raw internal credentials to the user. They are machine consistency data, not user-facing approval text.

## 2. Select scope and approve once

Offer only applicable scopes:

1. requested targets only
2. targets plus ordinary historical residuals
3. targets, ordinary historical residuals, and reported state threads missing rollout files

Fold explicitly requested exclusions and inclusion of open/unknown sessions into the selected scope. Do not create extra approval rounds for `--force-open`, `--no-subagents`, or `--no-logs`; list them clearly in the same approval summary.

This installed skill has a local historical-cleanup preference. Recommend scope 2 and present it first whenever its historical scan is authoritative and scope 2 is applicable, even when the frozen report currently contains zero ordinary residuals. State explicitly when scope 2 adds zero IDs or items. Prefer scope 1 when the user excluded historical cleanup, scope 2 is unavailable, or the historical scan is not authoritative. Do not recommend scope 3 merely because it is available; recommend it only when the user requested missing-rollout thread cleanup or clearly requested the broadest reported cleanup.

After the report, provide a separate complete, copyable approval response for every applicable scope, ordered with the recommendation first. Never provide a response template only for the recommended scope, and never require the user to assemble a scope label with a shared authorization suffix. Each complete response must both select its scope and authorize all normal execution steps. Every response must state:

- deletion is permanent and creates no backup or archive
- exact requested targets, recursively resolved subagents, historical scope, exclusions, logs, and open/unknown-session inclusion
- every warned or later unapproved object will be preserved
- creation of the private staged job under `<codex-home>/.session-deletion-jobs/<job-id>/` when offline work is required
- permission to freeze the unchanged reported plan, launch one configured Ghostty window, and have the user manually quit and later reopen Desktop
- permission to keep private request/receipt metadata only while needed, retain it after failure for recovery, and automatically remove the verified-success job plus its explicitly linked failed predecessor chain
- exact pre-existing job directories, if any, selected for strict success cleanup, supported empty-historical recovery, skipped-historical component recovery, or bounded predecessor linking; every other audited entry remains preserved

Do not ask separately for scope selection, staging, the private job path, final deletion, Ghostty launch, or successful metadata cleanup. Bind the response internally to the selected report-time semantic approval-scope fingerprint and never expose it. The fingerprint is built from an explicit allowlist: selected roots and options, recursive target membership, executable and retained targets, exact approved object locators and identities, authoritative ownership decisions, selected database paths with stable main-file identity, frozen target-contained mutation-effect contracts, unresolved protected references, and the exact selected historical snapshot. Unselected historical totals, process IDs, unrelated compatible schema inventory, SQLite `-wal`/`-shm` state, and other non-authority observations must not invalidate an unchanged approval. Exact selected targets, object identities, effect contracts, exclusions, and selected historical identities remain authority boundaries. A later macOS Automation/TCC prompt is separate system consent and may still appear once.

An immediate, unambiguous reply such as `我批准范围2` is the user's acceptance of the complete scope-2 response displayed in the preceding report. Treat it as the one combined approval and continue without asking the user to repeat the long text, provided that the corresponding full response was displayed, the scope is applicable, and its frozen plan is unchanged. A bare `批准` is ambiguous when multiple scopes were offered. If the requested scope was not displayed or the plan changed, stop and present the new complete responses instead of inferring authority.

For a direct apply, use that response immediately. For an offline handoff, rerun and stage the exact approved semantic scope with its scope-specific internal fingerprint, capture a fresh complete execution snapshot, then launch Ghostty without returning for another conversational approval. Compatible additive schema changes, process churn, and SQLite sidecar creation/removal may change that execution snapshot without changing approval. If a failed terminal job is being replaced, pass its exact private job directory with `--supersedes-job-dir`; the helper validates and carries forward the bounded predecessor chain. Use `--recovers-partial-job-dir` only for the audit-recognized skipped-historical classification: staging freshly proves target absence, zero target work, matching options and targets, healthy prior verification, a non-empty exact historical snapshot, and a historical-only plan. If recursive membership, an approved locator/identity, a protected target reference, the selected historical snapshot, or the selected scope changed, do not mutate or launch; show the delta and obtain one fresh combined approval for the new plan.

An approved historical scope whose frozen snapshot contains zero IDs and zero items is already satisfied. Do not run historical mutation merely to prove emptiness, do not require a post-mutation global historical rescan, and do not classify later unapproved residuals as failure. The engine records `approved_historical_snapshot_empty` and verifies the target components normally.

## 3. Apply directly

Use the same IDs and report options, and pass the internally selected exact-scope capsule verbatim:

```bash
python3 scripts/delete_codex_session.py <session-id> [<session-id> ...] \
  --apply --confirm-plan <selected-scope-capsule> --json
```

Add only options included in the final approved scope:

```bash
--apply-historical-residuals
--apply-historical-residuals --apply-missing-rollout-threads
--force-open
```

Repeat `--codex-home`, `--no-subagents`, `--no-logs`, and `--no-historical-scan` exactly when applicable. Never decode, edit, derive, reconstruct, substitute, or widen a capsule.

An approval/plan conflict means semantic authority no longer matches the requested mutation. Do not substitute another capsule. Report `plan_changed` with `next_action: restage` or rerun the report and obtain approval for the changed plan. Runtime-only drift is not an approval conflict; let the fresh execution snapshot and component-local prewrite checks accept it or preserve only the affected dependency.

## 4. Apply offline

When Desktop owns state that must be changed, do not attempt a live apply. A non-empty approved historical component changes state/log databases and therefore always uses the offline path. Follow [references/offline-handoff.md](references/offline-handoff.md): after the one combined approval, stage the exact selected scope with its approved scope fingerprint, launch one Ghostty window by private job path, and let the user manually quit and later reopen Desktop.

Keep `job_dir` while the worker is pending or failed. A successful Ghostty launch only means submission succeeded. `status` is concise and read-only by default; use `--full` only for retained failure diagnosis. After Ghostty displays a verified successful result, the helper removes the request, receipt, current job directory, and explicitly linked failed predecessor chain. The missing receipt is then expected; do not recreate it or require a post-reopen report. If strict cleanup is blocked, retain the success receipt with `cleanup_pending` and retry only `cleanup-completed`, never the deletion.

## 5. Verify and report

Treat an operation as complete only when mutation and verification agree. Verify that:

- every planned safe object is gone
- every warned object is present or otherwise accounted for as expected-preserved
- no protected non-target object disappeared
- exact structured references and supported database rows selected for deletion are gone
- required SQLite integrity checks remain healthy
- approved historical snapshot processing is complete when selected
- offline verification succeeded when offline execution was required; a success receipt may already have been automatically removed

Report `outcome`, `next_action`, deleted counts and bytes, preserved counts grouped by warning, unexpected residuals, later unapproved objects, temporary-metadata cleanup state, and Desktop reopen guidance. A verified Ghostty result uses `reopen_desktop`; do not require another scan merely to prove the already verified deletion. Use the outcome meanings and concise summary format in [references/result-schema.md](references/result-schema.md).

If mutation began and verification failed, report `partial_possible`, keep Desktop offline, preserve recovery material, and use `next_action: inspect_partial`. Never retry mutation automatically.

## Touched state

The scripts may update only the selected Codex home: session rollouts, shell snapshots, generated artifacts, `session_index.jsonl`, supported state/log/paginated-history/Desktop/auxiliary SQLite databases, the two global-state files, and private `.session-deletion-jobs/<job-id>/` requests and receipts. Verified-success private job directories are transient and are removed only through the strict metadata-cleanup contract.

They do not remove project directories, worktrees, credentials, unrelated configuration, skills, plugins, pets, or Codex installation files.
