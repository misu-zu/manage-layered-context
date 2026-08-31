# LESSON-NNNN: Topic

```python
Lesson(
    id="LESSON-NNNN",
    anchors=[
        "lesson:LESSON-NNNN",
        "risk:<failure-pattern>",
        "operation:<operation>",
    ],
    title="<short reusable guidance>",
    fact_ids=[],
    status="candidate | active | deprecated",
    scope="<where this applies and important exclusions>",
    trigger_signals=[],
    anti_pattern="<observable behavior that led to failure>",
    prevention="<specific preventive action>",
    required_checks=[],
    severity="low | medium | high | critical",
    confidence="low | medium | high",
    supporting_evidence=[],
    contradicting_evidence=[],
    outcome_notes=[],
    review_when=None,
    supersedes=[],
)
```

Keep a one-off or weakly supported lesson `candidate`. Record evidence and scope; do not convert an anecdote into a universal rule.
