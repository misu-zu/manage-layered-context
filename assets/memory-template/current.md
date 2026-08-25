# Current Project Memory

```python
ProjectState(
    snapshot_id="SNAP-YYYYMMDD-HHMMSS",
    project_identity="<project name and purpose>",
    record_mode="standard",
    active_goal="<current goal>",
    invariants=[
        # "<hard constraint or forbidden action>",
    ],
    active_decisions=[
        # "decision:ADR-0001",
    ],
    active_lessons=[
        # "lesson:LESSON-0001",
    ],
    open_issues=[
        # "issue:ISSUE-0001",
    ],
    unresolved_conflicts=[],
    active_tasks=[],
    latest_handoff=None,
    pending_patches=[],
    next_actions=[],
    last_updated="YYYY-MM-DDTHH:MM:SS+TZ",
)
```

Keep this dashboard small. Link records by anchor; do not duplicate their full detail here.
