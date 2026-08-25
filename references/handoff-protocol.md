# Handoff Checkpoint Protocol

Read this file when work must continue in another agent, branch, session, or context window. A handoff checkpoint is a durable continuation contract bound to a committed memory snapshot. It is not a transcript summary and does not itself authorize new work.

## When to create one

Create a checkpoint when at least one condition applies:

- control is intentionally transferring to another agent or branch;
- material work remains when the current run pauses or blocks;
- context compression would otherwise discard operational state;
- a long or risky operation reaches a verified restart boundary;
- branch results need acceptance before merge.

Do not create one for a trivial completed task with no continuation value. Do not checkpoint in the middle of an irreversible side effect when no safe restart boundary exists; first reach a stable state or record the checkpoint as `blocked` with the exact recovery constraint.

## Sender protocol

1. Read `current.md`, `index.md`, the active task, relevant lessons, and any predecessor checkpoint.
2. Re-read observable workspace state. Separate completed and verified work from attempted or assumed work.
3. Run the task's required checks, or state precisely which check could not run and why.
4. Create a `HandoffCheckpoint` through a context patch. Use the patch base as `source_snapshot_id`, its target as both `created_snapshot_id` and `last_updated_snapshot_id`, and preserve the task's original base as `task_base_snapshot_id`.
5. Set one next executable action. Supporting steps may remain in `remaining`, but `next_action` must be concrete enough to start without reconstructing the conversation.
6. Add the checkpoint to `index.md`, the task's `handoff_refs`, and `current.md.latest_handoff` when it is the active continuation point.
7. Commit the patch transaction before transferring control. A draft in conversation is not a durable checkpoint.

Use `status="ready"` only when the checkpoint is internally consistent and the receiver can validate it. Use `status="blocked"` when continuation depends on an unresolved blocker. Keep incomplete authoring as `draft` and do not present it as handed off.

## Readiness checklist

A ready or blocked checkpoint must make all of these explicit:

- stable `checkpoint_id`, `context_id`, and `task_id`;
- task base, sender-inspected source, checkpoint creation, and last-updated snapshot IDs;
- objective and acceptance criteria;
- concise current state;
- verified completed work and genuinely remaining work;
- next executable action, or `None` only when blocked or complete;
- artifact and changed-file paths that exist at the checkpoint, plus intentionally removed paths in `deleted_files`;
- verification commands or checks and their observed outcomes;
- assumptions, risks, and blockers;
- relevant anchors, applied lessons, and required checks;
- pending context patches or external state not represented by committed files;
- durable source references.

Do not include credentials, hidden reasoning, an unfiltered transcript dump, or claims that exist only in the sender's conversational context.

## Receiver protocol

1. Read the checkpoint first, then `current.md` and `index.md`. Read `project.md`, the task, lessons, and source records needed to validate it.
2. Compare `last_updated_snapshot_id` with the current commit boundary. Preserve `created_snapshot_id` as immutable provenance.
   - Equal: continue validation.
   - Current is newer: treat the checkpoint as stale, re-evaluate affected anchors, and record disposition `rebased` if it remains usable.
   - Current is older, mixed, or ambiguous: stop acceptance and create or update a conflict or recovery record.
3. Validate that referenced artifacts and changed files exist in the stated condition. Re-run critical checks when the evidence is missing, stale, environment-dependent, or high risk.
4. Confirm lesson applicability independently. A lesson reference is not proof that its guard still fits.
5. Set `accepted` through a patch before relying on the checkpoint for a long continuation. Set `consumed` only after its continuation state has been incorporated into the current task or a successor checkpoint.
6. If rejecting it, record the concrete mismatch and next action. Preserve the checkpoint as evidence; do not rewrite it into apparent agreement.

## State transitions

```text
draft -> ready -> accepted -> consumed -> archived
   |        |         |
   |        |         +-> superseded
   |        +-> rejected
   +-> blocked -> ready | superseded
```

Only a committed patch changes checkpoint status. Preserve predecessors so a continuation chain can be audited without relying on chat history.

## Relationship to other records

- `TaskRecord` owns the bounded objective and accumulated outcomes.
- `HandoffCheckpoint` owns one resumable continuation boundary.
- `ContextPatch` proposes and transactionally commits memory changes.
- `ArchiveEntry` preserves consumed historical checkpoints and any mistake digests.

A branch result normally proposes a patch. Add a checkpoint when another actor must continue or accept that result. Neither record makes branch conclusions authoritative until current memory validates and merges them.

## Compact example

```python
HandoffCheckpoint(
    checkpoint_id="HANDOFF-20260826-153000-parser-repair",
    context_id="context:parser-repair",
    task_id="TASK-20260826-001",
    status="ready",
    created_at="2026-08-26T15:30:00+09:00",
    from_actor="implementation-agent",
    intended_receiver="review-agent",
    task_base_snapshot_id="SNAP-20260826-140000",
    source_snapshot_id="SNAP-20260826-152800",
    created_snapshot_id="SNAP-20260826-152900",
    last_updated_snapshot_id="SNAP-20260826-152900",
    predecessor_checkpoint_id=None,
    objective="Repair parser recovery without changing accepted syntax.",
    acceptance_criteria=["targeted tests pass", "legacy fixture remains unchanged"],
    state_summary="Recovery branch implemented; broader regression suite remains.",
    completed=["Added recovery branch", "Ran targeted fixture tests: pass"],
    remaining=["Run full parser suite", "Review error-location behavior"],
    next_action="Run the full parser suite from the project root.",
    artifacts=[],
    changed_files=["src/parser.py", "tests/test_recovery.py"],
    deleted_files=[],
    verification=["targeted recovery tests: 12 passed"],
    assumptions=["accepted syntax is frozen by ADR-0012"],
    risks=["error offsets may differ for nested input"],
    blockers=[],
    relevant_anchors=["component:parser", "decision:ADR-0012"],
    lesson_refs=["lesson:LESSON-0004"],
    required_checks=["compare legacy fixture bytes"],
    pending_patches=[],
    source_refs=[SourceRef(ref_type="file", ref_id="tests/test_recovery.py")],
    consumed_by=None,
)
```

## Design lineage

- [LangGraph checkpoint protocol](https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint/README.md): thread-scoped state, checkpoint IDs, pending writes, and resumability.
- [OpenAI Agents SDK handoffs](https://github.com/openai/openai-agents-python/blob/main/docs/handoffs.md): typed handoff metadata and deliberate history filtering.
- [A2A task model](https://github.com/a2aproject/A2A/blob/main/specification/a2a.proto): context IDs, task lifecycle state, artifacts, and history.
