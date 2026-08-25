# ARCHIVE-YYYYMMDD-slug

```python
ArchiveRecord(
    id="ARCHIVE-YYYYMMDD-slug",
    anchors=["archive:ARCHIVE-YYYYMMDD-slug"],
    archived_at="YYYY-MM-DDTHH:MM:SS+TZ",
    summary="<what happened and why this detail moved downward>",
    affected_records=[],
    details=[],
    mistakes=[
        # MistakeDigest(
        #     id="MISTAKE-YYYYMMDD-001",
        #     kind="wrong_assumption | omission | tool_misuse | verification_gap | regression | coordination_gap | unknown",
        #     signal="<observable failure signal>",
        #     impact="<observable consequence>",
        #     cause=None,
        #     prevention="<specific recurrence check>",
        #     applies_when=[],
        #     source_refs=[],
        #     promoted_to=None,
        # ),
    ],
    source_refs=[],
)
```

Keep failure notes concise and evidence-backed. Promote only reusable guidance into `lessons/`.
