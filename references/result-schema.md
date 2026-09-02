# Result and Summary Contract

Use this vocabulary for machine results and user-facing summaries.

## Contents

- [Outcomes](#outcomes)
- [Next actions](#next-actions)
- [Safety-warning record](#safety-warning-record)
- [Schema compatibility record](#schema-compatibility-record)
- [Plan summary](#plan-summary)
- [Apply summary](#apply-summary)
- [Verification fields](#verification-fields)
- [Recovery summary](#recovery-summary)

## Outcomes

- `report_ready`: the exact plan is ready for scope selection or approval.
- `completed`: all approved safe operations completed and no objects were preserved by warnings.
- `completed_with_warnings`: all approved safe operations completed; warned objects were skipped and preserved as planned.
- `no_safe_work`: the plan ran, but every requested mutation was skipped or lacked authority; do not claim deletion.
- `waiting_for_manual_exit`: offline work is staged and waiting for the user to quit Desktop.
- `plan_changed`: approved authority no longer matches the mutation contract; no broader work is authorized.
- `partial_possible`: mutation started but completion or verification is uncertain.
- `failed`: a validation, integrity, or internal execution error prevented a trustworthy result.

Use `success: true` only for `completed` and `completed_with_warnings`. A report or waiting state is not deletion success.

## Next actions

Return exactly one `next_action`:

- `choose_scope`
- `approve_and_apply`
- `final_approve_and_launch` (legacy core-report alias for the one combined approval; never create a second prompt)
- `launch_ghostty`
- `quit_desktop`
- `wait_for_receipt`
- `relaunch_same_job`
- `restage`
- `reopen_desktop`
- `retry_cleanup`
- `reopen_and_verify` (legacy receipt alias)
- `inspect_partial`
- `fix_input`
- `none`

The next action must be executable and must agree with whether mutation started, whether a private request remains, and whether Desktop must remain offline.

## Safety-warning record

Each warning should contain:

```json
{
  "code": "stable_machine_code",
  "scope": "object|component|target|connected_targets",
  "affected_ids": ["session-id"],
  "affected_objects": ["managed-object"],
  "reason": "why mutation is unsafe or unsupported",
  "disposition": "skip_and_preserve|scope_downgraded|requires_explicit_include",
  "safe_operations_remaining": true,
  "retry_hint": "optional actionable remediation"
}
```

Do not put authorization, concurrency, waiting, or post-mutation uncertainty into this array. Represent them through `outcome`, gate details, and `next_action`.

## Schema compatibility record

Expose runtime assessment under `preflight.state_schema_compatibility`, `preflight.logs_schema_compatibility`, `preflight.paginated_history_schema_compatibility`, `preflight.desktop_catalog_schema_compatibility`, each entry in `auxiliary_thread_database_plans`, and historical `schema_compatibility` / `logs_schema_compatibility`:

```json
{
  "available": true,
  "unknown_tables": ["projects"],
  "candidate_reference_locations": [],
  "protected_ids": [],
  "target_reference_hits": [],
  "ambiguous_candidate_ids": [],
  "scan_complete": true,
  "scan_limit_bytes": 67108864,
  "scanned_bytes": 0,
  "user_version": 32,
  "newer_user_version_accepted": true,
  "mutation_effect_assessment": {
    "status": "target_only",
    "quick_check": "ok",
    "effects": [],
    "outside_scope_change_count": 0,
    "remaining_target_references": [],
    "triggers": [],
    "foreign_keys": []
  }
}
```

Fields that do not apply to a database may be absent. Unknown tables, target hits, triggers, foreign keys, and newer versions are evidence rather than automatic blockers. `target_only` means the fixed built-in mutation removed the target references without escaping scope. `target_residual`, `outside_scope`, or `indeterminate` preserves the affected dependency. A non-empty historical `protected_ids` still narrows historical cleanup.

The plan also exposes `state_database`, `logs_database`, `paginated_history_database_plan`, `preflight.state_mutation_effect_assessment`, and `artifact_ownership_evidence`. The paginated contract freezes direct target rows plus row-key/content digests for shadow-observed side effects; do not show hashes in the human report. Ownership evidence records the authoritative owner, filename UUID hints, and decision basis. Desktop catalog and auxiliary roles are discovered from safe managed `.db`/`.sqlite` files by table anchors, not required filenames. `auxiliary_thread_database_plans` remains keyed by the discovered filename and records `status: enabled|skipped`, reasons, anchors, compatibility evidence, and the frozen `preserved_contract`. Apply results include `paginated_history_cleanup` and per-database `database_results`; one `skipped_safely` entry may coexist with `completed` siblings and still produce `completed_with_warnings` when verification succeeds.

## Plan summary

Lead with one sentence:

> 可安全删除 X/Y 个会话（Z MiB）；N 个会话或对象将跳过并保留；需要/不需要离线执行。

Then show only:

1. requested roots and recursively resolved target count
2. planned safe deletion totals by component
3. preserved totals grouped by warning code
4. historical residual totals as a separate scope
5. the private-job audit: cleanup-ready success, narrowly recoverable anomaly, supersedable failure, active or preserved jobs, and ignored root metadata
6. material schema extensions, protected IDs, or incompatible anchors
7. selected exclusions or open-session inclusion
8. every applicable scope, with the locally preferred recommendation first
9. one complete, copyable combined-approval response for every applicable scope

Collapse long UUID lists by root and child count. Expand only warning-affected IDs by default. Never show capsules, token keys, or secret digests.

Use scope 2 as this installed skill's default recommendation whenever authoritative historical scanning makes it applicable, including when it currently adds zero IDs or items; state that zero delta explicitly. User exclusions or unavailable/untrustworthy historical scanning override that preference in favor of scope 1. Scope 3 is recommended only for an explicit broadest-cleanup or missing-rollout request. Always display the full approval response for every applicable scope, not only the recommendation, and repeat the complete common authorization terms inside each response so the user can copy one response without assembling fragments.

An immediately following `我批准范围N` binds to the corresponding complete response just displayed and counts as the one combined approval when that scope is applicable and the frozen report is unchanged. Do not request the long response again. A scope-less approval is ambiguous when several scopes were offered, and any plan change requires newly rendered complete responses.

Do not enumerate recent log-only transient worker IDs. A stable note may explain that they were excluded from the frozen historical scope. The report contains one internal `approval_scope_fingerprints` entry per applicable scope. Engine 4.0 / approval-scope contract 2 derives it from an explicit semantic allowlist rather than the complete capsule or runtime report. Target-only fingerprints exclude unselected historical totals; all scopes exclude point-in-time owner observations, non-owning mentions, compatible unrelated schema inventory, volatile file metadata, and SQLite sidecars. Selected target identities, authoritative ownership, target-contained effect contracts, and selected historical identities remain bound. Never expose these fingerprints to the user.

For an unchanged approved offline plan, staging has no second approval state: return `launch_ghostty` and launch it under the already-bound combined approval. Use `restage` only when the plan changed and a fresh approval is required.

The private-job audit uses audit contract version 1 and classifies exact immediate job children as `verified_success_cleanup_ready`, `recoverable_empty_historical_snapshot`, `recoverable_skipped_historical_component`, `terminal_failure_supersedable`, `retryable_pre_mutation`, `restage_required_pre_mutation`, `partial_possible_preserved`, `active`, `pending_preserved`, or `unsafe_preserved`. Finder metadata such as `.DS_Store` is ignored. Unrecognized root entries are reported and preserved. A read-only invocation reports these classes but does not itself authorize removal; when the user is already approving deletion or cleanup, include every exact cleanup, recovery, or predecessor-link action in that same combined approval.

## Apply summary

Report:

- `outcome` and `next_action`
- deleted sessions, rows, files, and bytes
- preserved sessions and objects grouped by warning
- approved objects already absent
- identity-changed approved objects retained
- later unapproved objects retained
- unexpected residuals or removals
- integrity and offline receipt verification
- temporary handoff metadata state: automatically cleaned, retained for failure diagnosis, or `cleanup_pending`
- pre-existing private jobs reconciled, preserved, or awaiting authorization

Say “已完成但有保留项” for `completed_with_warnings`, not “部分失败”. Reserve “可能部分修改” for `partial_possible` only.

## Verification fields

Verification should distinguish:

- `planned_deleted_remaining`
- `expected_preserved_present`
- `expected_preserved_missing`
- `unexpected_remaining`
- `unexpected_non_target_removed`
- `integrity_checks`
- `offline_verification_ok`
- `historical_snapshot_ok`

`residual_counts` also reports `paginated_history_projection_rows`, `paginated_history_turn_rows`, and `paginated_history_item_rows`. Verification checks the selected paginated main-file identity, integrity, schema signature, and frozen target-row contract before declaring success. Sidecar lifecycle is not database identity.

Expected-preserved objects do not make verification fail. `verification_ok` requires no planned safe deletions remaining, no unexpected residuals or removals, healthy required integrity checks, and successful offline verification when required.

The offline request/receipt protocol uses schema version 6. A verified successful receipt is normally transient: Ghostty prints the result, then the helper strictly removes the current job, its explicit pre-mutation failed-predecessor chain, and any exact recovery predecessor recorded separately in `recovery_job_ids`. A recovery predecessor is revalidated at cleanup and cannot be smuggled through ordinary `cleanup_job_ids` supersession. `status` returns a compact, read-only projection by default; `--full` returns the retained diagnostic receipt. Compact status includes phase, outcome, next action, mutation/verification flags, cleanup state, selected session IDs, diagnostic counts, and the receipt path.

Core engine 4.0 records `approved_historical_snapshot_empty: true` when an authoritative selected historical snapshot contains zero IDs and zero items. That selected scope is satisfied without historical mutation or a new global historical rescan. Later residual IDs or items are reported as unapproved and preserved, not treated as failure. A retained receipt from the older false-`partial_possible` behavior may be reclassified only as `recoverable_empty_historical_snapshot`; recovery must freshly verify target absence and integrity before strict metadata cleanup.

`recoverable_skipped_historical_component` is the non-empty component recovery classification. It means target deletion and verification succeeded, the historical component was skipped before its first write, and no component reported failure. A fresh combined approval and semantic fingerprint must cover the current exact historical identities. Staging returns `stage_historical_component_recovery`, requires a historical-only execution plan with zero target counts, and records the exact predecessor in `recovery_job_ids`. Any target/snapshot drift, historical mutation, verification discrepancy, owner reappearance, or integrity failure remains `partial_possible_preserved`.

## Recovery summary

For `plan_changed`, state that mutation did not start and show the changed semantic scope before requesting fresh approval. Do not call process churn, compatible additive schema, or SQLite sidecar lifecycle a scope change.

For `partial_possible`, keep Desktop offline and list four groups: confirmed removed, confirmed preserved, uncertain, and recovery files. Use `next_action: inspect_partial` unless the audit returns the supported skipped-historical classification; only then may a fresh approved historical-only stage use `stage_historical_component_recovery`. Never imply that a generic retry is safe.

For `cleanup_pending`, state clearly that deletion and verification already succeeded and Desktop may reopen. Show the exact retained job path and use `next_action: retry_cleanup`. `cleanup-completed` retries only the strict private-metadata cleanup; it must not repeat deletion. When cleanup succeeds, the receipt path ceases to exist by design and no post-reopen verification is mandatory.
