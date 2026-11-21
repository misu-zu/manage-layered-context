# Handoff Checkpoint Protocol

Read this reference when another agent, branch, session, or context window must continue. A checkpoint is a durable continuation contract bound to committed memory, not a transcript summary and not new authorization.

## Choose a profile

Use the safest automatic default:

- `lite`: the same agent resumes in a later session, the state is simple, risk is ordinary, and no complex artifact acceptance is needed.
- `full`: a different agent, branch transfer, high-risk operation, complex artifacts, disputed assumptions, or explicit request.

An explicit profile overrides the automatic choice. A v2 checkpoint with no `profile` is `full`.

Lite contains exactly the operational resume set:

```python
HandoffCheckpoint(
    profile="lite",
    checkpoint_id="HANDOFF-20260830-013000-parser",
    task_id="TASK-20260830-001",
    status="ready",
    objective="Finish parser regression verification.",
    state_summary="Recovery branch is implemented; full suite remains.",
    completed=["Targeted parser tests passed."],
    remaining=["Run the full parser suite."],
    next_action="Run the full parser suite from the project root.",
    changed_files=["src/parser.py", "tests/test_parser.py"],
    verification=["targeted parser tests: 12 passed"],
    blockers=[],
    source_snapshot_id="SNAP-20260830-012800",
    checkpoint_snapshot_id="SNAP-20260830-012900",
)
```

Full adds context ID, actor/receiver, creation time, task-base/source/created/last-updated snapshots, predecessor, acceptance criteria, artifacts/deletions, assumptions, risks, anchors, lessons/checks, pending patches, sources, and consumption provenance. Inspect the exact conditional schema with `memoryctl.py schema HandoffCheckpoint --json` and use `assets/memory-template/handoffs/_template.md`.

## When to create

Create at a stable restart boundary when control transfers, material unfinished work pauses or blocks, compression would discard operational detail, or risky work reaches a verified checkpoint. Skip trivial completed work with no continuation value. Never checkpoint mid-side-effect when no safe restart exists; first stabilize or set `blocked` with the exact recovery constraint.

## Sender

1. Read current/index, active task, relevant lessons, and predecessor.
2. Reinspect observable workspace state; separate verified completion from attempts and assumptions.
3. Run required checks or state exactly why one could not run.
4. Create the selected profile through the normal transaction. `source_snapshot_id` is the state inspected by the sender; the checkpoint snapshot is the committed target.
5. Set one executable `next_action`. Supporting work stays in `remaining`.
6. Add the checkpoint to an index and the active task; update `latest_handoff` when it is the active continuation.
7. Commit before transfer. Conversation-only drafts are not handoffs.

Use `ready` only when internally consistent and independently verifiable. Use `blocked` for an unresolved dependency and `draft` while authoring. Full ready/accepted checkpoints must validate artifacts and changed files fail-closed. Lite ready/accepted checkpoints validate `changed_files`.

Never include credentials, hidden reasoning, an unfiltered transcript, or claims supported only by the sender’s chat context.

## Receiver

1. Read checkpoint, current, and root index; then the minimum task, topic shard, lesson, and source records needed.
2. Compare the checkpoint snapshot with the current boundary:
   - equal → validate normally;
   - current newer → checkpoint is stale evidence; re-evaluate anchors and record `rebased` if still usable;
   - current older, mixed, or ambiguous → stop acceptance and enter conflict/recovery.
3. Validate changed files/artifacts and rerun critical checks when evidence is missing, stale, environment-dependent, or high risk.
4. Reassess lesson applicability independently.
5. Record accepted/rebased/rejected/consumed through an authorized transaction. Preserve rejected and superseded checkpoints as evidence.

For Lite, acceptance or consumption may piggyback on the next already-authorized memory update. Do not create a standalone write solely to mark consumption when no write is otherwise authorized.

## States

```text
draft -> ready -> accepted -> consumed -> archived
   |        |         |
   |        |         +-> superseded
   |        +-> rejected
   +-> blocked -> ready | superseded
```

Only a committed transaction changes status. A stale checkpoint remains provenance, not current state.

## Snapshot meanings

Lite uses `source_snapshot_id` and `checkpoint_snapshot_id`. Full preserves four distinct roles so creation does not make itself stale:

- `task_base_snapshot_id`: where the task began;
- `source_snapshot_id`: committed state inspected before authoring;
- `created_snapshot_id`: immutable checkpoint creation commit;
- `last_updated_snapshot_id`: latest commit that changed checkpoint content/status.

## Design lineage

- [LangGraph checkpoint protocol](https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint/README.md): scoped checkpoints, pending writes, and resumability.
- [OpenAI Agents SDK handoffs](https://github.com/openai/openai-agents-python/blob/main/docs/handoffs.md): typed handoff metadata and deliberate history filtering.
- [A2A task model](https://github.com/a2aproject/A2A/blob/main/specification/a2a.proto): task lifecycle, context IDs, and artifacts.
