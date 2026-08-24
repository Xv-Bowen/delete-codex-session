# Runtime Schema Compatibility

Read this reference when a report contains a newer database version, unknown schema objects, protected extension IDs, or schema-related warnings.

## Decision model

Use four evidence layers:

1. **Family and role discovery** — inventory every `state_*.sqlite`, `logs_*.sqlite`, or `thread_history_*.sqlite` candidate, plus safe `.db`/`.sqlite` files under the managed SQLite directory. Select state/log/paginated-history generations and Desktop/auxiliary roles only from structurally unambiguous anchor matches; do not require a preferred product filename.
2. **Anchor contract** — the exact tables, locator columns, primary keys, integrity checks, canonical references, and mutation behavior the selected component needs.
3. **Extension inventory** — unknown tables plus newly introduced thread/session/conversation-shaped columns in known tables.
4. **Candidate protection** — canonical UUIDs found in extension values, including nested JSON/text and uncompressed byte values.

Version numbers and filenames are discovery signals, not deletion authority. Known table/column/primary-key anchors establish a database role and the exact row locator; they do not authorize unknown tables. A newer version or renamed safe database is compatible when the anchor contract still holds. An extension never enters a delete set automatically.

Keep schema evidence in three layers:

1. The semantic approval scope binds only authority-changing facts: selected database path and stable main-file identity, selected target row keys/hashes, target-affecting protected references, and the chosen target/historical set.
2. Staging captures a fresh execution snapshot with complete discovery, schema signatures, integrity evidence, and current row contracts.
3. The apply transaction repeats component-local prewrite checks before mutation.

The stable SQLite identity is the canonical main file's path, device, inode, type, and link count. Never include the main file's size/mtime or `-wal`/`-shm` existence, size, mtime, or inode in approval authority. Read-only connections can create, resize, touch, checkpoint, or remove sidecars. Such lifecycle churn is runtime evidence, not a scope change.

## Runtime classifications

- `compatible_extension`: anchor contract holds and the extension contains no candidate UUID. Leave it untouched and continue.
- `protected_reference`: extension contains a candidate UUID. Add the UUID to the protected set; never delete the unknown row.
- `affected_component_preserved`: an approved target is protected or extension inspection is incomplete. Preserve that database component and continue independent work.
- `incompatible_anchor`: a required primary key or column changed, a relevant trigger can alter known thread storage, integrity failed, or canonical identity is ambiguous. Gate or skip only the dependent component.

Apply these classifications per database, not merely per database class. In particular, one preserved history-snapshot or thread-summary database does not block compatible siblings. Report-time compatible databases may still be downgraded individually if their row or schema identity changes before apply.

If one safe file exposes multiple auxiliary roles, or multiple files expose the same Desktop/auxiliary role, classify discovery as ambiguous and preserve it. A database whose known anchor table is renamed into an unknown table cannot be mutated merely by semantic guessing; inspect it as unknown storage, protect candidate UUIDs, and continue independent components.

## Paginated history contract

Recognize paginated history from the structural trio `thread_history_projection_state`, `thread_turns`, and `thread_items`. Require their canonical thread locator, composite primary keys where applicable, healthy integrity, canonical target IDs, no mutation-affecting triggers, and a complete extension-reference scan. Additive columns, indexes, migrations, views, and unrelated tables are compatible when they do not reference an approved target and the anchors remain intact.

Freeze each target-owned row as its table, complete primary key, and full-row hash. A compatible unrelated table, column, index, view, migration row, or sidecar lifecycle may appear after approval and staging can capture it without fresh user authority when it neither adds a target reference nor changes selected rows or anchors. Apply rechecks the selected path, stable main-file identity, fresh schema/extension inventory, and exact row contracts in one prewrite transaction. Rows newly added or changed after staging retain the whole dependent target before any target component mutates; already-absent approved rows are satisfied. Delete only by the approved primary keys. UUIDs nested in `item_json` or other payloads do not confer ownership and are never additional delete selectors.

## Historical residual rule

Build candidate IDs from index rows, rollout files, shell snapshots, generated artifacts, logs, and known structured references. Then compute:

```text
live_or_protected = canonical_state_thread_ids ∪ extension_protected_ids ∪ recent_log_only_ids
historical_residual = candidate_id ∉ live_or_protected ∧ candidate_id not explicitly excluded
```

If the bounded extension scan is incomplete, place every affected candidate in `extension_protected_ids`. This can retain extra data but cannot cause a false deletion.

`recent_log_only_ids` is a bounded transient-activity guard for canonical IDs found only in current log rows. Any rollout, index, snapshot, generated artifact, state reference, or canonical state row disables the exception. After the time window expires, a log-only ID is classified as an ordinary historical residual. The transient IDs are not serialized into the frozen plan, so inspection cannot continuously expand its own scope.

## Agent judgment boundary

Use the structured assessment to explain what changed, decide whether remaining safe work is useful, recognize ordinary additive compatibility, and recommend an applicable user scope. This judgment boundary applies to the active capable model and is not tied to a hard-coded model family or version. Do not:

- compose mutation SQL for unknown tables;
- remove an ID from the protected set based on naming intuition;
- interpret a random UUID mention as permission to delete that row;
- bypass an incompatible anchor or incomplete integrity check.

The runtime assessment is designed to absorb ordinary additive migrations without requiring a Skill update. Encrypted, compressed, unreadable, structurally ambiguous, or fundamentally redefined task storage may still require preservation; the agent may explain and isolate it but may not improvise destructive support.

Use model judgment for classification and explanation, regardless of model family or release. Keep mutation mechanics deterministic: the model cannot add selectors, weaken exact identities, waive an incomplete scan, or reclassify a target-bearing unknown object as deletable. This division lets future capable models reason about compatible evolution without turning confidence into deletion authority.
