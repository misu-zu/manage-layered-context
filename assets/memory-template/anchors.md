# Anchor Registry

```python
AnchorRegistry(
    aliases={
        # "concept:cognitive-load": ["認知負荷"],
        # "risk:incomplete-visual-qa": ["全ページ未確認"],
    },
    deprecated={
        # "concept:old-name": "concept:new-name",
    },
)
```

Use lowercase semantic anchors with a namespace. Preserve aliases instead of renaming anchors silently.
