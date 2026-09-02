# Runtime Schema Compatibility

Read this reference when a report contains a newer database version, unknown schema objects, protected extension IDs, or schema-related warnings.

## Decision model

Use five evidence layers:

1. **Family and role discovery** — inventory every `state_*.sqlite`, `logs_*.sqlite`, or `thread_history_*.sqlite` candidate, plus safe `.db`/`.sqlite` files under the managed SQLite directory. Select state/log/paginated-history generations and Desktop/auxiliary roles only from structurally unambiguous anchor matches; do not require a preferred product filename.
2. **Anchor contract** — the exact tables, locator columns, primary keys, integrity checks, canonical references, and mutation behavior the selected component needs.
3. **Extension inventory** — unknown tables plus newly introduced thread/session/conversation-shaped columns in known tables.
4. **Ownership evidence** — authoritative state references and structured thread/session columns; names and embedded UUIDs are hints, not verdicts.
5. **Mutation effect envelope** — when extensions, triggers, or foreign keys touch target storage, run the exact built-in mutation on an in-memory clone and diff every row before and after.

Version numbers, filenames, trigger names, and SQL wording are discovery signals, not deletion authority. Known anchors establish a database role and exact row locators. A newer or renamed database is compatible when its anchors hold and the shadow envelope is target-contained. Unknown rows may be removed only indirectly by already-present schema behavior reached from the fixed built-in mutation; the model never creates selectors or SQL for them.

Keep schema evidence in three layers:

1. The semantic approval scope binds only authority-changing facts: selected database path and stable main-file identity, selected target row keys/hashes, target-affecting protected references, and the chosen target/historical set.
2. Staging captures a fresh execution snapshot with complete discovery, schema signatures, integrity evidence, and current row contracts.
3. The apply transaction repeats component-local prewrite checks before mutation.

The stable SQLite identity is the canonical main file's path, device, inode, type, and link count. Never include the main file's size/mtime or `-wal`/`-shm` existence, size, mtime, or inode in approval authority. Read-only connections can create, resize, touch, checkpoint, or remove sidecars. Such lifecycle churn is runtime evidence, not a scope change.

## Runtime classifications

- `not_required`: no schema dependency extends the direct built-in mutation.
- `target_only`: shadow execution is integral, every changed row is target-owned, no new reference is introduced, and every runtime-discovered target reference is gone. Freeze and continue.
- `target_residual`: execution stays bounded but leaves a target reference in discovered storage. Preserve the affected dependency.
- `outside_scope`: a row without target ownership changes, a row is introduced, or integrity fails. Preserve the affected dependency.
- `indeterminate`: the clone, bounded scan, schema action, or integrity check cannot complete. Preserve the affected dependency.
- `incompatible_anchor`: a required locator, key, canonical identity, or database boundary changed, so the fixed mutation is no longer well-defined.

Apply these classifications per database, not merely per database class. In particular, one preserved history-snapshot or thread-summary database does not block compatible siblings. Report-time compatible databases may still be downgraded individually if their row or schema identity changes before apply.

If one safe file exposes multiple auxiliary roles, or multiple files expose the same Desktop/auxiliary role, classify discovery as ambiguous and preserve it. A renamed anchor cannot be recovered by semantic guessing because the deterministic executor no longer has an approved locator.

## Paginated history contract

Recognize paginated history from the structural trio `thread_history_projection_state`, `thread_turns`, and `thread_items`. Require their canonical thread locator, composite primary keys where applicable, healthy integrity, canonical target IDs, and a complete bounded inspection. Triggers and foreign keys are allowed when shadow execution proves their effects are target-contained. An unknown target-bearing table is also compatible when the fixed deletion reaches it through that existing schema behavior and leaves no target reference.

Freeze each direct target row by primary key/full-row hash and every shadow-observed side effect by row-key/content digest. Apply recomputes the effect envelope inside the prewrite boundary and requires it to match. Rows newly added or changed after staging retain the dependent target; already-absent approved direct rows are satisfied. UUIDs nested in ordinary payloads are evidence only and never additional selectors.

## Historical residual rule

Build candidate IDs from index rows, rollout files, shell snapshots, generated artifacts, logs, and known structured references. Then compute:

```text
live_or_protected = canonical_state_thread_ids ∪ extension_protected_ids ∪ recent_log_only_ids
historical_residual = candidate_id ∉ live_or_protected ∧ candidate_id not explicitly excluded
```

If the bounded extension scan is incomplete, place every affected candidate in `extension_protected_ids`. This can retain extra data but cannot cause a false deletion.

`recent_log_only_ids` is a bounded transient-activity guard for canonical IDs found only in current log rows. Any rollout, index, snapshot, generated artifact, state reference, or canonical state row disables the exception. After the time window expires, a log-only ID is classified as an ordinary historical residual. The transient IDs are not serialized into the frozen plan, so inspection cannot continuously expand its own scope.

## Artifact ownership

For a state thread, its exact `threads.rollout_path` is the primary ownership assertion. Validate that the path stays inside the managed sessions root, is a regular JSONL file, and is not claimed by another live thread. UUIDs in the relative filename are weak hints. Several UUID hints are acceptable when exactly one live authoritative state thread matches; record `artifact_ownership_evidence`. Repeat this same authoritative-owner resolution after the state transaction lock is acquired, rather than falling back to filename-only ambiguity during historical cleanup. Preserve the object when two live threads claim it, ownership is otherwise unresolved, or path safety fails.

## Agent judgment boundary

Use the structured assessment to explain what changed, decide whether remaining work is useful, recognize evidence-backed target-contained evolution, and recommend scope. This applies to the active capable model and is not tied to a model name or version. Do not:

- compose mutation SQL for unknown tables;
- infer ownership from naming intuition when authoritative evidence conflicts;
- interpret a random UUID mention as a new deletion selector;
- bypass an incompatible anchor, escaped effect, or incomplete integrity check.

The runtime assessment is designed to absorb ordinary additive migrations without requiring a Skill update. Encrypted, compressed, unreadable, structurally ambiguous, or fundamentally redefined task storage may still require preservation; the agent may explain and isolate it but may not improvise destructive support.

Keep mutation mechanics deterministic while leaving evidence interpretation to the model: it cannot add selectors, weaken exact identities, or waive an incomplete effect scan. This lets capable models reason about compatible evolution without turning a long prohibition list into the decision-maker.
