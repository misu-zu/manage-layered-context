# Memory Schema

Read this file before creating or modifying memory records. Keep records concise and omit optional fields only when they are genuinely inapplicable. The Python-like blocks are schema declarations, not executable code. In copied templates, replace alternatives such as `"open | blocked"` with exactly one allowed value.

The project catalog, project map, lessons, mistake digests, and handoffs are v2 additions. A memory root created by an earlier version may omit those files and the corresponding `ProjectState`, `TaskRecord`, `ArchiveEntry`, or `ContextPatch` fields. Treat absent v2 fields as empty or `None` until an authorized patch upgrades the root; do not rewrite an otherwise valid root merely to normalize formatting.

## Levels and status

```python
ImportanceLevel = Literal[
    "L0_invariant",
    "L0_project_map",
    "L1_active_decision",
    "L1_active_lesson",
    "L2_active_issue",
    "L2_active_handoff",
    "L3_working_note",
    "L4_historical_detail",
    "L5_scratch",
]

DetailLevel = Literal["index", "summary", "detail", "source"]
RecordStatus = Literal["active", "archived", "deprecated", "scratch"]
RecordMode = Literal["minimal", "standard", "detailed", "forensic"]
```

## Source references

```python
@dataclass
class SourceRef:
    ref_type: Literal[
        "project",
        "task",
        "handoff",
        "lesson",
        "mistake",
        "patch",
        "archive",
        "conversation",
        "file",
        "commit",
    ]
    ref_id: str
    note: str | None = None
```

Prefer stable file paths, task IDs, patch IDs, commit IDs, or user-provided conversation labels. Mark a claim with no durable source as `confidence="low"`.

Never put credentials, tokens, private keys, recovery codes, raw personal data, or unrelated sensitive information in any record, source note, patch, staging file, backup, or archive. Use a role or non-identifying alias instead of personal data. When a sensitive source is necessary, store only a redacted summary and a pointer to an approved secure location; never copy the sensitive value into memory. Assume the memory root may be version-controlled.

## Workspace catalog and project map

```python
@dataclass
class ProjectPointer:
    project_id: str
    name: str
    purpose: str
    root: str
    memory_root: str
    status: Literal["active", "paused", "archived"]
    aliases: list[str]

@dataclass
class ProjectCatalog:
    catalog_version: int
    projects: list[ProjectPointer]

@dataclass
class ProjectMap:
    project_id: str
    name: str
    purpose: str
    project_root: str
    memory_root: str
    boundaries: list[str]
    components: dict[str, str]
    entry_points: list[str]
    authoritative_sources: list[str]
    generated_or_disposable: list[str]
    navigation_notes: list[str]
```

Paths in a workspace `PROJECTS.md` are relative to the catalog file. In `memory/project.md`, resolve `project_root` relative to the directory containing `project.md`, then resolve `memory_root`, component paths, entry points, and other project paths relative to that project root unless explicitly marked absolute. For the conventional `<project>/memory/project.md` layout, use `project_root=".."` and `memory_root="memory"`. The catalog is a discovery map, not a status dashboard: do not duplicate goals, decisions, snapshots, handoff detail, or other volatile state there.

### Upgrade an earlier memory root

Upgrade only through an authorized context patch:

1. Require the existing `current.md` and `index.md` snapshots to match.
2. Add `project.md`, `lessons/`, and `handoffs/` from the v2 templates without overwriting records.
3. Add `active_lessons=[]` and `latest_handoff=None` to `ProjectState` in the staged target.
4. Do not bulk-rewrite old task, archive, or patch records merely to add empty v2 fields; add those fields when each record is next changed.
5. Add a workspace `PROJECTS.md` pointer only when multi-project discovery is useful or explicitly requested.
6. Validate the staged and committed roots with `scripts/validate_memory.py --strict-v2`.

## Active state and retrieval index

```python
@dataclass
class ProjectState:
    snapshot_id: str
    project_identity: str
    record_mode: RecordMode
    active_goal: str | None
    invariants: list[str]
    active_decisions: list[str]
    active_lessons: list[str]
    open_issues: list[str]
    unresolved_conflicts: list[str]
    active_tasks: list[str]
    latest_handoff: str | None
    pending_patches: list[str]
    next_actions: list[str]
    last_updated: str

@dataclass
class IndexEntry:
    anchors: list[str]
    summary: str
    path: str
    importance: ImportanceLevel
    status: RecordStatus
    related: list[str]
    record_snapshot_id: str

@dataclass
class MemoryIndex:
    snapshot_id: str
    entries: list[IndexEntry]

@dataclass
class AnchorRegistry:
    aliases: dict[str, list[str]]
    deprecated: dict[str, str]
```

`ProjectState` lists anchors or IDs instead of duplicating record detail. `IndexEntry.record_snapshot_id` identifies the committed snapshot in which the linked record was last changed. Update it whenever the linked record changes; older values are valid for unchanged records.

## Decision

```python
@dataclass
class Decision:
    id: str
    anchors: list[str]
    topic: str
    choice: str
    status: Literal["tentative", "active", "deprecated"]
    confidence: Literal["low", "medium", "high"]
    scope: str
    rationale: str
    alternatives: list[str]
    source_refs: list[SourceRef]
    review_when: str | None
    supersedes: list[str]
```

Create a decision only for an explicit choice. Repeated discussion is not agreement.

## Open issue

```python
@dataclass
class OpenIssue:
    id: str
    anchors: list[str]
    topic: str
    status: Literal["open", "blocked", "partially_resolved", "resolved"]
    question: str
    current_understanding: str
    next_action: str | None
    source_refs: list[SourceRef]
    resolved_by: str | None
```

Keep an issue visible until an explicit resolution source exists.

## Conflict

```python
@dataclass
class Conflict:
    id: str
    anchors: list[str]
    topic: str
    competing_positions: list[str]
    reason: str
    resolution_status: Literal["unresolved", "resolved"]
    next_action: str | None
    source_refs: list[SourceRef]
    resolved_by: str | None
```

Visible conflict is safer than an unsupported merge.

## Mistake digests and reusable lessons

```python
@dataclass
class MistakeDigest:
    id: str
    kind: Literal[
        "wrong_assumption",
        "omission",
        "tool_misuse",
        "verification_gap",
        "regression",
        "coordination_gap",
        "unknown",
    ]
    signal: str
    impact: str
    cause: str | None
    prevention: str
    applies_when: list[str]
    source_refs: list[SourceRef]
    promoted_to: str | None

@dataclass
class Lesson:
    id: str
    anchors: list[str]
    title: str
    status: Literal["candidate", "active", "deprecated"]
    scope: str
    trigger_signals: list[str]
    anti_pattern: str
    prevention: str
    required_checks: list[str]
    severity: Literal["low", "medium", "high", "critical"]
    confidence: Literal["low", "medium", "high"]
    supporting_evidence: list[SourceRef]
    contradicting_evidence: list[SourceRef]
    outcome_notes: list[str]
    review_when: str | None
    supersedes: list[str]
```

A `MistakeDigest` records one observed episode compactly. `cause=None` is better than an invented cause. A `Lesson` is reusable procedural memory. A `candidate` may inform judgment but cannot impose a required check. Promote it to `active` only with an explicit user decision, a reproducible failure and verified prevention, or converging evidence from more than one episode. Severity affects the consequence of an applicable lesson; it must not inflate retrieval relevance.

## Task records

```python
@dataclass
class TaskRecord:
    id: str
    anchors: list[str]
    objective: str
    status: Literal["active", "completed", "blocked", "archived"]
    base_snapshot_id: str
    last_updated_snapshot_id: str
    created_at: str
    updated_at: str
    relevant_anchors: list[str]
    lesson_refs: list[str]
    required_checks: list[str]
    handoff_refs: list[str]
    outcomes: list[str]
    remaining_work: list[str]
    source_refs: list[SourceRef]
```

`base_snapshot_id` records where the task began. `last_updated_snapshot_id` records the latest committed memory snapshot reflected by the task record.

## Handoff checkpoints

```python
@dataclass
class HandoffCheckpoint:
    checkpoint_id: str
    context_id: str
    task_id: str
    status: Literal[
        "draft",
        "ready",
        "blocked",
        "accepted",
        "consumed",
        "rejected",
        "superseded",
    ]
    created_at: str
    from_actor: str
    intended_receiver: str | None
    task_base_snapshot_id: str
    source_snapshot_id: str
    created_snapshot_id: str
    last_updated_snapshot_id: str
    predecessor_checkpoint_id: str | None
    objective: str
    acceptance_criteria: list[str]
    state_summary: str
    completed: list[str]
    remaining: list[str]
    next_action: str | None
    artifacts: list[str]
    changed_files: list[str]
    deleted_files: list[str]
    verification: list[str]
    assumptions: list[str]
    risks: list[str]
    blockers: list[str]
    relevant_anchors: list[str]
    lesson_refs: list[str]
    required_checks: list[str]
    pending_patches: list[str]
    source_refs: list[SourceRef]
    consumed_by: str | None
```

`context_id` groups related tasks or handoffs; `checkpoint_id` identifies one resumable state. `task_base_snapshot_id` is where the handed-off task began. `source_snapshot_id` is the committed state the sender inspected before preparing the handoff patch. `created_snapshot_id` is that patch's committed target and never changes. `last_updated_snapshot_id` advances only when a later patch changes the checkpoint status or content. `artifacts` and `changed_files` list paths that must exist at a ready checkpoint; put intentionally removed paths in `deleted_files`. A ready checkpoint must not claim unverified work as complete. A receiver compares current state with `last_updated_snapshot_id` and validates artifacts before setting `accepted` or `consumed`; see `handoff-protocol.md`.

## Archive records

```python
@dataclass
class ArchiveRecord:
    id: str
    anchors: list[str]
    archived_at: str
    summary: str
    affected_records: list[str]
    details: list[str]
    mistakes: list[MistakeDigest]
    source_refs: list[SourceRef]

@dataclass
class ArchiveEntry:
    id: str
    anchors: list[str]
    summary: str
    path: str
    affected_records: list[str]
    mistake_refs: list[str]
    source_refs: list[SourceRef]

@dataclass
class ArchiveManifest:
    entries: list[ArchiveEntry]
```

Archive entries remain reachable through at least one index anchor.

## Patch

```python
@dataclass
class ContextPatch:
    patch_id: str
    status: Literal[
        "proposed",
        "applying",
        "merged",
        "rejected",
        "superseded",
        "recovery_required",
    ]
    created_at: str
    base_snapshot_id: str | None
    target_snapshot_id: str | None
    affected_anchors: list[str]
    target_files: list[str]
    add_or_update_decisions: list[str]
    add_or_update_issues: list[str]
    add_or_update_conflicts: list[str]
    add_or_update_lessons: list[str]
    add_or_update_handoffs: list[str]
    project_map_updates: list[str]
    archive_targets: list[str]
    source_refs: list[SourceRef]
    assumptions: list[str]
    merge_started_at: str | None
    merge_completed_at: str | None
    merge_notes: str | None
```

## Merge transaction metadata

Store the transaction manifest at `memory/.staging/<patch-id>/manifest.md` while a merge is in progress.

```python
@dataclass
class TransactionFile:
    path: str
    operation: Literal["create", "update", "archive"]
    before_sha256: str | None
    staged_sha256: str
    backup_path: str | None

@dataclass
class MergeTransaction:
    patch_id: str
    status: Literal["prepared", "applying", "committed", "recovery_required"]
    owner: str
    base_snapshot_id: str
    target_snapshot_id: str
    started_at: str
    files: list[TransactionFile]

@dataclass
class UpdateLock:
    patch_id: str
    owner: str
    base_snapshot_id: str
    target_snapshot_id: str
    acquired_at: str
```

## Operation reports

```python
@dataclass
class CoverageReport:
    loaded_files: list[str]
    not_loaded_but_possibly_relevant: list[str]
    completeness: Literal["low", "medium", "high"]
    risk_notes: list[str]

@dataclass
class LessonRecallReport:
    query_signals: list[str]
    candidates_considered: list[str]
    applied_lessons: list[str]
    rejected_candidates: list[str]
    required_checks: list[str]

@dataclass
class MemoryChangeReport:
    created: list[str]
    updated: list[str]
    archived: list[str]
    pending_conflicts: list[str]
    new_snapshot_id: str

@dataclass
class HandoffReport:
    checkpoint: str | None
    disposition: Literal[
        "created",
        "accepted",
        "rebased",
        "rejected",
        "consumed",
        "none",
    ]
    snapshot_id: str
    next_action: str | None
```

Use `new_snapshot_id="unchanged"` when no transaction committed.

## Identifiers and anchors

Use stable searchable identifiers:

```text
ADR-0001
ISSUE-0001
CONFLICT-0001
LESSON-0001
TASK-YYYYMMDD-001
HANDOFF-YYYYMMDD-HHMMSS-slug
MISTAKE-YYYYMMDD-NNN
PATCH-YYYYMMDD-HHMMSS-slug
ARCHIVE-YYYYMMDD-slug

concept:evaluation-window
component:context-loader
decision:ADR-0001
issue:ISSUE-0001
conflict:CONFLICT-0001
lesson:LESSON-0001
risk:destructive-path-resolution
symptom:snapshot-mismatch
task:TASK-YYYYMMDD-001
handoff:HANDOFF-YYYYMMDD-HHMMSS-slug
mistake:MISTAKE-YYYYMMDD-001
project:project-slug
source:path-or-conversation-label
```

Prefer semantic anchors that survive wording changes. Add a Japanese alias when it narrows meaning or preserves a project term.

## Snapshot rules

Use an ISO-like sortable ID such as `SNAP-20260620-153045`. Each target ID must be unique and sort after its base; append a short suffix when needed. A proposed or staged patch does not change the committed snapshot.

Treat matching `snapshot_id` values in `current.md` and `index.md` as the commit boundary. If they differ, do not trust the memory as fully committed and do not begin another merge.

`project.md`, lesson records, and handoff records participate in a project memory transaction when changed, but they do not define the commit boundary. A workspace-level `PROJECTS.md` is outside an individual project transaction and must contain only validated, low-volatility pointers.

For every merge:

1. Create `.update-lock` atomically with create-new or equivalent exclusive semantics. If it already exists, do not overwrite or remove it; leave the patch proposed and report contention.
2. While holding the lock, re-read `current.md` and `index.md`. Require their snapshot IDs to match each other and the patch's `base_snapshot_id`. If not, release only the lock created by this operation and rebase or leave the patch pending.
3. Assign a unique `target_snapshot_id`. Build staged new files and backups under `.staging/<patch-id>/`, then record relative paths and SHA-256 hashes in `manifest.md`.
4. Validate the entire staged set before changing committed files. Set the patch to `applying` and record `merge_started_at`.
5. Replace affected detail records first, then `index.md`, and `current.md` last. Writing `current.md` last is the commit marker.
6. Re-read all target files. Require `current.md` and `index.md` to equal the target snapshot and require every file hash to match the transaction manifest.
7. Mark the patch `merged`, record `merge_completed_at`, then remove its staging directory and the lock created by this operation.

For interruption recovery, never start a new merge while a lock, staging manifest, or `applying` patch exists:

- If both control files equal the target snapshot and all hashes match, finalize the patch as merged and clean up the transaction.
- If both control files equal the base snapshot and all committed hashes match the recorded pre-merge state, restore or discard staged work only after revalidation, return the patch to proposed, and clean up the transaction.
- If snapshots or hashes are mixed, missing, or ambiguous, set the patch to `recovery_required`, retain the lock and staging evidence, create or update a conflict record, and request review. Never guess, silently roll forward, or delete recovery evidence.
