# HANDOFF-YYYYMMDD-HHMMSS-slug

```python
HandoffCheckpoint(
    checkpoint_id="HANDOFF-YYYYMMDD-HHMMSS-slug",
    context_id="context:<workstream>",
    task_id="TASK-YYYYMMDD-NNN",
    status="draft | ready | blocked | accepted | consumed | rejected | superseded",
    created_at="YYYY-MM-DDTHH:MM:SS+TZ",
    from_actor="<sender role or agent>",
    intended_receiver=None,
    task_base_snapshot_id="SNAP-YYYYMMDD-HHMMSS",
    source_snapshot_id="SNAP-YYYYMMDD-HHMMSS",
    created_snapshot_id="SNAP-YYYYMMDD-HHMMSS",
    last_updated_snapshot_id="SNAP-YYYYMMDD-HHMMSS",
    predecessor_checkpoint_id=None,
    objective="<bounded continuation objective>",
    acceptance_criteria=[],
    state_summary="<concise observable state>",
    completed=[],
    remaining=[],
    next_action=None,
    artifacts=[],
    changed_files=[],
    deleted_files=[],
    verification=[],
    assumptions=[],
    risks=[],
    blockers=[],
    relevant_anchors=[],
    lesson_refs=[],
    required_checks=[],
    pending_patches=[],
    source_refs=[],
    consumed_by=None,
)
```

Use `ready` only when the checkpoint is committed, internally consistent, and independently verifiable.
