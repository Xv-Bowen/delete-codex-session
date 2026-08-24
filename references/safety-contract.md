# Safety Contract

Use this contract for every report, direct apply, staged apply, and verification.

## Contents

- [Local degradation](#local-degradation)
- [Private-job reconciliation](#private-job-reconciliation)
- [Safety warnings versus gates](#safety-warnings-versus-gates)
- [Scope and approval contract](#scope-and-approval-contract)
- [Runtime schema compatibility](#runtime-schema-compatibility)
- [Component rules](#component-rules)
- [Historical cleanup](#historical-cleanup)
- [Verification contract](#verification-contract)
- [Private handoff lifecycle](#private-handoff-lifecycle)
- [Managed state boundary](#managed-state-boundary)

## Local degradation

Represent every safety diagnosis as a structured safety warning. A warning never suppresses independent safe work.

Each warning should identify:

- a stable code
- the affected object, component, target, or connected target group
- the reason it cannot be changed safely
- the disposition: `skip_and_preserve`, `scope_downgraded`, or `requires_explicit_include`
- whether independent safe operations remain
- an actionable retry hint when one exists

Build deletion as target → component → object operations. Propagate a skip only through real dependencies:

- An unsafe leaf path skips only that leaf.
- An unsafe index or logs database skips only that component.
- An unsupported auxiliary database skips that database, not every auxiliary database.
- An unsafe global-state file skips the global-state pair because the pair is published atomically.
- An unsafe core state database skips state mutation and graph-dependent expansion; continue exact-ID operations that do not depend on that graph.
- A current invoking session is preserved. Continue unrelated targets.
- An open or unknown-status session is preserved unless its inclusion was explicitly approved. Continue approved closed targets.
- A changed report-time object is preserved rather than chased or overwritten. Continue unchanged approved objects.

If no safe authorized operation remains, return `no_safe_work`; do not claim that deletion completed.

## Private-job reconciliation

Every skill invocation begins with the helper's bounded read-only `audit-jobs` inventory of the exact `<codex-home>/.session-deletion-jobs` root. Inspect only immediate canonical job-ID children. Never follow symbolic links, traverse unrecognized entries, infer cleanup candidates from age, or broadly delete the root.

Classifications narrow the permitted action:

- A verified-success receipt may undergo strict `cleanup-completed` metadata finalization; this cannot rerun deletion.
- The only partial receipt eligible for automatic recovery is the engine-recognized empty-historical-snapshot anomaly. `recover-empty-historical` must recheck the exact Codex-home boundary, inactive locks, receipt identity, original zero-object historical scope, successful target verification, healthy integrity checks, unchanged selected state/log databases, and fresh target absence before it may mark success and invoke strict cleanup.
- Later historical residuals are outside an originally empty approved snapshot. Preserve and report them for a new scope decision.
- A terminal failure may enter a later successful cleanup lineage only through its exact validated job directory.
- Active, pending, genuinely partial, malformed, cross-home, unsupported, linked, or unexpected-entry jobs remain preserved.

A read-only request never authorizes job removal. During an authorized deletion or cleanup invocation, include exact pre-existing cleanup/recovery actions in the same combined approval unless the retained successful receipt already grants strict success cleanup without a second deletion approval.

## Safety warnings versus gates

Keep these conditions outside the safety-warning collection:

- malformed or suspicious user input: validation error
- absent, invalid, mismatched, or widened approval: authorization gate
- target membership or approved scope changed: plan-consistency gate
- another worker owns the mutation lock: concurrency gate
- Desktop is still running when offline ownership is required: offline-wait state
- request or bundled source integrity changed: integrity gate
- mutation began but completion cannot be verified: `partial_possible`

Every gate must preserve data, state whether mutation started, and provide one `next_action`. A gate may pause the affected operation, but it must not be reported as a safety warning.

## Scope and approval contract

The approval capsule carries both the semantic approval-scope contract and a complete execution snapshot, but they have different jobs. The scope contract binds the exact approved delete set, retained target set, exclusions, open-session inclusion, historical snapshot, and selected object locators/identities. The execution snapshot records current runtime evidence for safe staging and apply. Neither is an operating-system authorization boundary.

Never expose capsules to the user. Never decode, edit, derive, reconstruct, substitute, or use a broader capsule. Later objects never enter the approved scope automatically.

Ask for one combined approval after the report. The same response must select the applicable scope and authorize permanent deletion without backup, selected exclusions and inclusions, creation of the private offline job when needed, freezing the unchanged report plan, Ghostty launch, the user's manual Desktop quit/reopen, and strict automatic cleanup of verified-success private metadata. Do not split those items into separate conversational approvals.

Display one complete combined-approval response for every applicable scope before accepting a selection; never show only the recommended scope or make the user assemble a shared suffix. An immediate scope-specific reply such as `我批准范围2` accepts the complete corresponding response displayed in the preceding report and needs no second prompt, but only while that exact scope is applicable and its frozen plan is unchanged. A reply without a scope is ambiguous when multiple scopes were displayed. A missing template, unavailable scope, or plan change cannot be cured by interpreting shorthand broadly; present fresh complete responses and obtain one new combined approval.

Bind offline staging to the selected semantic approval-scope fingerprint internally. Construct it from an explicit versioned allowlist, never by hashing the complete report, capsule, discovery inventory, or SQLite file family. Include selected roots/options, recursive membership, executable and retained target IDs, component status, exact approved locators/row identities, selected database paths and stable main-file identities, target-affecting protected references, and the exact selected historical snapshot. Exclude process observations, preflight diagnostics, non-owning mentions, unrelated compatible schema inventory, SQLite `-wal`/`-shm` files, and volatile size/mtime metadata. Target-only scopes also exclude unselected historical diagnostics; selected historical scopes bind exact historical IDs and identities.

A matching stage records that irreversible approval is already present, captures a fresh full execution snapshot, and may proceed directly to Ghostty launch. Object-level identity drift is preserved and reported. A change that alters target membership, recursively resolved subagents, approved object locators/identities, selected target rows, target-affecting protected references, selected historical objects, or selected scope invalidates authority: do not create or launch a mutable job, and require one fresh combined approval. Runtime-only drift is handled by the fresh snapshot or the last-moment prewrite guard without expanding authority.

The macOS Automation/TCC prompt that may appear when controlling Ghostty is separate operating-system consent. It does not expand deletion scope and cannot replace the combined deletion approval.

## Runtime schema compatibility

Judge evolving SQLite schemas by the structures used for the requested operation, not by a whole-database table-name or version whitelist. A higher `user_version`, unrelated table, additive non-reference field, index, or read-only view is compatible when required anchor tables, locator columns, primary keys, reference formats, integrity checks, and mutation-affecting triggers remain valid.

Discover state, logs, and paginated-history database families structurally. Paginated history is compatible only when `thread_history_projection_state`, `thread_turns`, and `thread_items` retain their required locator columns and primary keys; nested UUIDs in item payloads are evidence only and never row-deletion authority. Also inspect safe `.db` and `.sqlite` files in the managed SQLite directory for Desktop-catalog and auxiliary-table role anchors instead of requiring product filenames. Select a sole safe role member even when its name or suffix is new; when several files expose the same role anchor, preserve that database family as ambiguous. Freeze the selected path and stable main-file identity in the semantic scope, and freeze the full discovery inventory in the execution snapshot and verification. Never use `-wal` or `-shm` lifecycle metadata as database identity. A later selected path or main-file identity change cannot be followed automatically.

Inspect every column of an unknown table and every newly discovered thread/session/conversation-shaped reference column in a known table for candidate UUIDs. The assessment may only narrow deletion:

- Unknown storage with no candidate UUID is a compatible extension and remains untouched.
- A candidate UUID found in unknown storage is added to the protected set. Unknown rows are never deleted.
- If a requested target is referenced by an extension, preserve only the affected state, logs, Desktop, or individual auxiliary database and continue independent safe components.
- If bounded extension inspection cannot complete, protect all affected candidates. Do not infer absence from an incomplete scan.
- A modified anchor primary key, missing required column, mutation-affecting trigger, failed integrity check, unreadable storage boundary, or incompatible canonical reference remains a gate or component-level safety warning.

For historical classification, compute liveness as the union of canonical state-thread IDs, runtime-protected extension IDs, and recent canonical IDs that exist only in logs. Only a candidate absent from that union and every other authoritative evidence source may enter the historical residual scope. The recent log-only exception is bounded: any non-log evidence disables it, and old log-only IDs remain ordinary residuals.

The active model may explain the assessment, recognize evidence-backed additive compatibility, and recommend an applicable scope. This authority is deliberately independent of a particular model name or release. It must not invent SQL for unknown objects, manually override protected IDs, or treat semantic guesses as proof. Truly opaque or structurally incompatible future storage can still require preservation; compatibility is evidence-based, not a promise to mutate every future format.

## Component rules

- Resolve recursive subagents only from an audited state schema. If unavailable, warn that recursive expansion was skipped and operate only on independently proven exact IDs.
- Include exact matching logs by default. Skip an unsafe logs store without suppressing other components.
- For paginated targets, freeze every owned projection/turn/item row by primary key and row hash. Treat these rows and the other owned components for a target as one dependency: if the selected paginated database, anchor, protected-reference assessment, or target-row contract changes, retain that whole target before any of its components mutate. Independent targets may continue. At apply time revalidate the fresh execution snapshot, selected main-file identity, schema signature, target-reference inventory, and row contracts before the first dependent mutation; delete only unchanged approved rows and accept already-absent rows.
- Evaluate every discovered auxiliary database separately. Freeze its anchor assessment, compatibility evidence, target-row identities, and expected-preserved contract. A skipped auxiliary database must not suppress mutations in a different compatible auxiliary database.
- Preserve malformed or invalid-UTF-8 index content and skip index mutation; continue other components.
- Preserve incompatible SQLite anchors, mutation-affecting triggers, target-bearing unknown storage, ambiguous generations, and unreadable objects. Accept structurally compatible extensions and preserve their rows unchanged.
- Preserve symlinks, multiply linked managed files, paths escaping the selected Codex home, and artifacts with ambiguous multi-session ownership. Never follow them.
- Bind approved regular files by device, inode, type, mtime, size, and SHA-256. Isolate exact leaves atomically and remove directories only when empty.
- Preserve prompt-history text as non-owning evidence. Remove only recognized or unambiguously discovered exact structured references.
- Treat a nonstandard realtime-voice selector as a warning; clear it only when its complete value is unambiguously target-owned.
- Treat `rollout_migration_state.last_checked_thread_id` as a non-owning cursor. Never clear it with a session.
- Delete retry-ledger rows only when their exact rollout path is approved.
- Preserve JSONL byte offsets by replacing approved index rows with equal-length whitespace.
- Stage both global-state files before publishing either; preserve metadata and roll back the pair when safe ownership of canonical paths remains provable.

## Historical cleanup

Keep historical cleanup separate from target deletion. Scan only when authoritative state is trustworthy enough to classify residuals.

Ordinary residuals include exact orphan index rows, rollout files, snapshots, generated artifacts, log rows, and state references without a matching state thread. State threads missing rollout files are a separate scope.

Freeze exact report-time historical identities and the runtime extension assessment. Delete unchanged approved objects; treat already-absent objects as satisfied; preserve identity-changed replacements and all later additions. If a residual becomes a live state thread or gains an unknown-schema reference, preserve it and report the changed classification. Exclude point-in-time Desktop owner process observations from the approval fingerprint; they are rechecked as an apply gate and do not define the deletion set.

An authoritative approved historical snapshot with no object identities is satisfied without invoking historical mutation. Its verification is independent of later unapproved residuals and must not become `partial_possible` merely because a new global historical scan is unavailable after target deletion.

## Verification contract

Verification succeeds with warnings when all of the following hold:

- planned safe deletions are absent
- expected-preserved objects match the warning contract
- unexpected residuals are empty
- protected non-target removals are empty
- supported database integrity checks are healthy
- catalog/global-state presence still matches the approved contract
- every selected state/log path still matches its approved structural discovery
- every skipped auxiliary database matches its expected-preserved per-database contract

Expected preserved objects are not verification errors. Unexpected disappearance, unexpected mutation, or an unaccounted residual is an error; after mutation begins it produces `partial_possible`.

Offline verification is complete before Desktop reopens. A post-reopen report is optional and starts a new read-only assessment; it is not required to validate an already verified offline result.

## Private handoff lifecycle

The `0600` request and receipt are cross-process recovery material while Desktop is closed. They are transient operational state, not a permanent audit archive.

- Retain the receipt after failure, partial completion, or a strict-cleanup anomaly so diagnosis and recovery remain possible.
- A restaged successor may explicitly inherit a bounded chain of terminal failed job IDs. Validate every predecessor under the same private job root and Codex-home boundary; never discover deletion candidates by broad directory age or globbing.
- After `complete` plus deletion and verification success, print the Ghostty result first, then remove failed predecessors and the current job.
- Cleanup accepts only same-user `0700` real job directories containing the single-link `0600` receipt and optional inactive worker/launch locks. The approval request must already be absent.
- A symbolic link, unexpected entry, active lock, owner/mode mismatch, unsupported receipt, cross-home lineage, or concurrent change stops cleanup. Retain the current success receipt with `cleanup_pending`; do not recursively force removal.
- `cleanup-completed` may retry only this strict metadata cleanup. It never reruns deletion and does not require a second deletion approval.

## Managed state boundary

Mutation is limited to the selected Codex home:

- `sessions/**/*.jsonl`
- `shell_snapshots/<thread-id>.*.sh`
- `generated_images/**/<thread-id>*`
- `session_index.jsonl`
- structurally selected `state_*.sqlite`, `logs_*.sqlite`, `thread_history_*.sqlite`, Desktop catalog, history-snapshot, and thread-summary databases, regardless of safe managed filename
- `.codex-global-state.json` and `.codex-global-state.json.bak`
- `.session-deletion-jobs/<job-id>/`

Never remove project directories, worktrees, credentials, unrelated configuration, skills, plugins, pets, or installation files.
