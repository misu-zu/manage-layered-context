# Project Map

```python
ProjectMap(
    project_id="project:<stable-slug>",
    name="<project name>",
    purpose="<one-sentence purpose>",
    project_root="..",
    memory_root="memory",
    boundaries=[],
    components={
        # "component:<name>": "<relative path or concise role>",
    },
    entry_points=[],
    authoritative_sources=[],
    generated_or_disposable=[],
    navigation_notes=[],
)
```

Keep this map stable and navigational. Put active goals and task state in `current.md`.
