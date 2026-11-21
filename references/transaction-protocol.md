# Transaction, Recovery, and Migration Protocol

Use this reference for manual staging, mode changes, index reorganization, recovery, merge, or migration. All modes use the same safety core; cost controls only retained evidence and permitted scope.

## Common core

1. Confirm write authorization and run relevant lesson preflight.
2. Require `current.md` and root `index.md` to share the patch base snapshot.
3. Create `.update-lock` exclusively. Never overwrite or remove another owner’s lock.
4. Re-read the committed boundary while holding the lock.
5. Stage every target under `.staging/<patch-id>/files/`; record before hashes and target snapshot in `manifest.json`.
6. Build an effective staged root and validate its schemas, paths, reachability, handoffs, configuration, and budgets before touching committed files.
7. Commit in this order: detail/patch → topic shard → root `index.md` → `current.md`. `current.md` is last.
8. Re-read the boundary and verify every committed SHA-256 against the staged manifest.
9. Finalize retained evidence required by the mode, then remove only this transaction’s staging and lock.

Use sortable, unique targets after the base. Never merge against a changed base. Never put secrets or raw personal data into staging, backups, or audit evidence.

## Modes

| Mode | Intended scope | Durable evidence | Forbidden/required |
| --- | --- | --- | --- |
| `minimal` | one canonical detail plus routing/current | no generated patch | no delete, archive, migration, merge, or index reorganization |
| `standard` | ordinary multi-record updates | compact `ContextPatch` | common core still mandatory |
| `detailed` | archive, delete, index reorganization, branch merge | patch plus retained hash/recovery manifest | required for these operations |
| `forensic` | v2→v3, irreversible, security-sensitive | detailed evidence plus retained backups/audit | required for these operations |

Do not downgrade mode merely to avoid evidence or validation. Do not use forensic for every trivial write; backup retention has cost and may increase sensitive-data exposure.

## Unified CLI

For a new root or a single authored record, use the high-level commands.

```sh
python scripts/memoryctl.py init project/memory --name "Export service" --purpose "Publish verified exports"
python scripts/memoryctl.py record put project/memory draft.md --path decisions/ADR-0001-format.md
```

Supply one complete structured Markdown record. The default destination is `<kind>/<id>.md`. Specify `--path` when updating an existing filename with a suffix. The command updates the entry, relevant current-state link, snapshots and patch together. A handoff also links its indexed task when present. An index summary may be supplied with `--summary`. The configured mode applies unless `--mode` is supplied. Use detailed mode to move an existing root entry to a historical shard. Input drafts belong outside the canonical root.

Begin a transaction with every path that will change, including both control files:

```text
python <skill-root>/scripts/memoryctl.py txn begin <memory-root> \
  --patch-id PATCH-20260830-010000-alpha \
  --mode standard --operation update \
  --target-snapshot SNAP-20260830-010000 \
  --file decisions/ADR-0001-alpha.md --file index.md --file current.md
```

Use `--auto-target` in place of `--target-snapshot` to allocate the next valid UTC snapshot and set both staged control versions. To extend a prepared patch, use `txn add <root> --patch-id <id> --file <path>`. The mode limits apply to the complete file list. The begin command returns the staging directory. Edit only its listed files, then:

```text
python <skill-root>/scripts/memoryctl.py txn commit <memory-root> --patch-id PATCH-...
```

For an authorized delete in detailed/forensic mode, list the record with `--delete-file` and stage updated control/index files with `--file`. Both modes retain the deleted bytes under `.transactions/<patch-id>/deleted/<original-path>` and verify their before hash. To restore a record, stage those bytes and its references in a new transaction. The file list and index changes also enforce the minimum mode for deletes, archive changes and index moves. It never treats an omitted staged file as an implicit delete.

Abort only a still-prepared transaction whose committed files match all before hashes:

```text
python <skill-root>/scripts/memoryctl.py txn abort <memory-root> --patch-id PATCH-...
```

For an interruption, use `txn recover`. Recovery is deterministic:

- both controls equal target and all committed hashes equal staged hashes → finalize committed;
- both controls equal base and every committed file equals its before state → abort cleanly;
- any mixed, missing, or ambiguous snapshot/hash state → set `recovery_required`, retain lock/staging evidence, and request review.

The lock is published atomically. A short OS lock on `.txn-guard` serializes each CLI operation and releases on process exit. The file can remain between writes. Recovery can clear an interrupted preparation at a verified clean base. Incomplete older locks without ownership evidence require inspection.

Never guess, silently roll forward, or delete mixed recovery evidence. A new transaction cannot begin while recovery evidence remains.

## Explicit v2 migration

V2 remains readable and writable. Migration never occurs during validation, retrieval, or an ordinary update. Preview first:

```text
python <skill-root>/scripts/memoryctl.py migrate <memory-root> \
  --target-snapshot SNAP-20260830-020000 --json
```

Preview changes no files. Apply requires forensic mode and an explicit confirmation token:

```text
python <skill-root>/scripts/memoryctl.py migrate <memory-root> \
  --target-snapshot SNAP-20260830-020000 --mode forensic \
  --apply --confirm APPLY_V3_MIGRATION
```

Migration adds `config.json`, converts `MemoryIndex` to `ActiveMemoryIndex`, keeps active records at the root, groups historical records into bounded semantic/time shards, advances the commit boundary, validates the staged v3 root, and retains forensic evidence. It also adds required first-class identifier fields only when absent: `fact_ids=[]` does not invent facts, `OpenIssue.contradiction_id` defaults to `None`, and a migrated `Conflict` uses its existing record ID as the fallback contradiction identifier. Preserve prose outside the structured record block and do not bulk-rewrite unchanged canonical records merely to normalize style.

## Branch results and deletion

Treat branch output as a proposed patch, not authoritative memory. Re-evaluate stale anchors, assumptions, and lessons. Create a conflict when conclusions depend on incompatible premises.

Archive before deleting recoverable historical detail. Keep unresolved issues, conflicts, active lessons, unconsumed handoffs, source references, and review conditions reachable. A delete operation requires at least detailed mode and must prove the exact relative path remains inside the intended root.
