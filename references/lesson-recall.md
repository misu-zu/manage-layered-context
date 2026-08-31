# Lesson Capture and Recall

Read this file before recording a mistake, promoting a reusable lesson, or using past failure knowledge to constrain a new task. The goal is to convert observable experience into scoped prevention without accumulating folklore.

## Two-stage memory

Use two different records:

1. `MistakeDigest` is an episodic note stored with the relevant archive record. It says what was observed and how recurrence might be prevented.
2. `Lesson` is reusable procedural memory in `memory/lessons/`. It is retrieved across similar tasks and may contribute preflight checks.

This separation keeps archives cheap while preventing a single noisy episode from becoming a permanent rule.

## Minimal archive syntax

```python
MistakeDigest(
    id="MISTAKE-20260826-001",
    kind="verification_gap",
    signal="Generated document opened, but page 3 overflow was not rendered for inspection.",
    impact="The delivered layout clipped a table row.",
    cause="Visual verification covered only the first two pages.",
    prevention="Render and inspect every page after the final content change.",
    applies_when=["operation:document-generation", "artifact:multi-page"],
    source_refs=[SourceRef(ref_type="archive", ref_id="ARCHIVE-20260826-report")],
    promoted_to=None,
)
```

Keep `signal`, `impact`, and `prevention` observable and compact. Set `cause=None` when evidence does not establish a cause. Do not store blame, private personal detail, raw hidden reasoning, or a long failure transcript.

Record a digest only when it adds future diagnostic value. Ordinary experimentation, harmless dead ends, and errors already enforced by deterministic tooling usually do not need durable memory.

## Promote, revise, and deprecate lessons

Create a candidate lesson when the prevention can plausibly transfer beyond one archived episode. Keep it `candidate` until at least one of these supports activation:

- the user explicitly adopts it as a project rule;
- the failure is reproducible and the proposed check demonstrably prevents it;
- independent episodes provide converging evidence;
- a success/failure comparison isolates the useful behavioral difference.

An active lesson needs scoped trigger signals, a specific prevention action, and evidence. Candidate lessons can be considered during retrieval but cannot impose required checks. Add contradicting evidence when the lesson causes harm, does not generalize, or matches false positives. Edit, narrow, or deprecate it instead of silently ignoring repeated contradictions.

When several tasks support or contradict the same lesson, update the existing record. Do not create near-duplicate lessons for wording variants; use anchors and aliases.

## Preflight recall

Run lesson preflight for mutating, repeated, risky, or previously failed work.

1. Build a task fingerprint from:
   - selected project and task type;
   - operation such as migrate, delete, render, merge, publish, or hand off;
   - affected components and artifacts;
   - tools or environments involved;
   - exact error text or observable symptom fragments;
   - relevant constraints, acceptance criteria, and invariants.
2. Scope to the selected project before scoring similarity.
3. Search exact semantic anchors, IDs, error signatures, filenames, and aliases first.
4. Use lexical ranking next. When available, run:

   ```text
   python <skill-root>/scripts/memoryctl.py recall <memory-root> --query "<task fingerprint>" --kinds lessons,archive --top 3
   ```

   The helper returns a ranked candidate upper bound and matching signals; it does not load or validate their claims. Open rank 1 and assess applicability before opening the next candidate. Expand with `--expand` (configured limit 8) only when the first three, checked sequentially, do not provide adequate coverage.
5. Use optional semantic or hybrid search only as a candidate generator. Namespace it by project and retain source paths and score explanations.
6. Read the highest-value candidate, judge applicability using scope, status, trigger match, temporal validity, evidence quality, and contradictions, then stop or continue one at a time.
7. Produce `LessonRecallReport`. Add only applicable active-lesson checks to the task plan and eventual handoff.

Reject a candidate explicitly when it is outside scope, deprecated, weakly supported, contradicted, stale, or merely similar in wording. Severity determines the cost of ignoring an applicable lesson; it does not make an irrelevant lesson more relevant.

## Automatic avoidance policy

Apply this gate after reading a lesson:

```text
candidate or deprecated -> advisory only; never mandatory
active + weak applicability -> mention or reject with reason
active + clear applicability + low/medium severity -> add a recommended check
active + clear applicability + high/critical severity -> add a required check before the risky action
```

If a required check cannot run, do not silently proceed. Expose the gap as a risk, blocker, or explicit user decision according to task scope and severity.

At completion, record an outcome only when it improves the lesson's evidence:

- `helped`: the check detected or prevented the failure;
- `irrelevant`: it matched but did not apply, suggesting narrower triggers;
- `contradicted`: following it worsened or failed to improve the result;
- `not_evaluated`: no evidence either way; usually omit.

Never claim causal success merely because a task happened to pass.

## Search projections

Markdown records and source references are canonical. Full-text databases, embeddings, vector indexes, and context graphs are rebuildable projections. Every projection record must retain project namespace, source path, record ID, status, and the committed snapshot used to build it. A stale or unavailable projection lowers retrieval confidence but must not make canonical memory inaccessible.

Prefer this escalation path:

1. `current.md`, `index.md`, anchors, and direct file search;
2. deterministic lexical ranking with `recall_context.py`;
3. optional project-scoped semantic or hybrid search;
4. a temporal graph only when cross-record relationships and validity history justify its operational cost.

## Design lineage

- [Reflexion](https://github.com/noahshinn/reflexion): short verbal repair guidance persisted across attempts.
- [ExpeL](https://github.com/LeapLabTHU/ExpeL): insight extraction from successful and failed experiences, with task-similarity retrieval.
- [Voyager](https://github.com/MineDojo/Voyager): a retrieved skill library combined with execution feedback and self-verification.
- [Mem0 search](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/search.mdx): scoped hybrid retrieval, thresholds, reranking, and explainable scores.
