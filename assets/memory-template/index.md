# Active Memory Index

```python
ActiveMemoryIndex(
    snapshot_id="SNAP-YYYYMMDD-HHMMSS",
    entries=[
        # Active decision, issue, conflict, task, lesson, or handoff entries only.
    ],
    routes=[
        IndexRoute(
            route_id="route:general",
            anchor_prefixes=["component:", "concept:", "operation:", "risk:"],
            path_glob="indexes/general*.md",
            summary="General bounded topic route; split semantically as history grows.",
            status_scope=["active", "archived", "deprecated", "scratch"],
        ),
    ],
)
```

Keep this root below 1,500 target / 2,000 hard o200k tokens. Historical records belong in topic shards or the archive manifest.
