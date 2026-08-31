# HANDOFF-YYYYMMDD-HHMMSS-slug

```python
HandoffCheckpoint(
    profile="lite",
    checkpoint_id="HANDOFF-YYYYMMDD-HHMMSS-slug",
    task_id="TASK-YYYYMMDD-NNN",
    fact_ids=[],
    status="draft | ready | blocked | accepted | consumed | rejected | superseded",
    objective="<bounded continuation objective>",
    state_summary="<concise observable state>",
    completed=[],
    remaining=[],
    next_action=None,
    changed_files=[],
    verification=[],
    blockers=[],
    source_snapshot_id="SNAP-YYYYMMDD-HHMMSS",
    checkpoint_snapshot_id="SNAP-YYYYMMDD-HHMMSS",
)
```

Use Lite only for a simple later-session resume by the same agent. Use the full template for another agent, branch, high risk, or complex artifacts.
