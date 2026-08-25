# Agent-Optimized Memory Architecture

Read this file when designing, extending, scaling, or auditing the layered memory system. Ordinary retrieval and small record updates do not need it.

## Functional layers

The filesystem is a context repository with distinct memory functions:

| Function | Canonical records | Why it exists |
| --- | --- | --- |
| Discovery | `PROJECTS.md`, `project.md` | Locate the correct project and authoritative sources without scanning everything. |
| Working memory | `current.md` | Keep the active goal, hard constraints, and immediate continuation small. |
| Routing | `index.md`, `anchors.md` | Find relevant detail using stable semantic names. |
| Semantic memory | decisions, issues, conflicts | Preserve what is believed, chosen, unresolved, or contradictory. |
| Procedural memory | lessons | Reuse scoped prevention and verification behavior. |
| Episodic memory | tasks, handoffs, archive | Preserve what happened and where continuation boundaries occurred. |
| Provenance and commit | source references, patches, snapshots | Make claims traceable and updates recoverable. |
| Search projection | lexical, vector, or graph index | Accelerate candidate discovery without replacing canonical records. |

Directory names, IDs, anchors, and one-line summaries are retrieval affordances. Keep them stable and descriptive because an agent often sees these before it sees record bodies.

## Operating loop

```text
discover -> orient -> recall -> act -> verify -> reflect -> checkpoint -> compress
```

- Discover selects one project and memory root.
- Orient reads current state and the minimum stable map.
- Recall loads task records and applicable lessons.
- Act changes the authorized workspace, not memory by implication.
- Verify gathers observable evidence.
- Reflect records only durable conclusions, mistakes, or lesson outcomes.
- Checkpoint makes continuation resumable when needed.
- Compress moves historical detail downward while preserving reachability.

Each transition can be read-only. A context write requires the authorization described in `SKILL.md`.

## Trust and freshness

Use this evidence order when records disagree:

1. applicable system, developer, user, and repository instructions;
2. directly inspected current source or external state;
3. committed records with durable source references and matching snapshots;
4. archived episodes and consumed handoffs;
5. summaries, search projections, and low-confidence unsourced notes.

Recency alone does not prove truth. Preserve superseded decisions and temporal history, but mark current validity explicitly. A search result is never stronger than the canonical record it points to.

## Retrieval design

Use constrained, explainable retrieval:

1. project namespace filter;
2. record status and type filter;
3. exact ID, anchor, filename, and symptom matches;
4. lexical or optional semantic similarity;
5. reranking by scope, evidence quality, and temporal validity;
6. agent reads selected canonical records and explains applicability.

Keep relevance and severity separate. Limit returned candidates, but disclose possibly relevant unread records in `CoverageReport`. Record the projection snapshot so stale indexes are detectable.

## Scaling path

Adopt infrastructure only when the simpler layer is measurably insufficient:

1. Markdown, `index.md`, anchors, and ordinary file search.
2. `recall_context.py` for deterministic ranked candidate discovery.
3. Project-scoped full-text or embedding search when lexical recall misses paraphrases at meaningful scale.
4. A temporal context graph when relationship traversal, fact invalidation, and cross-project historical queries justify a database and ingestion pipeline.

Do not require an embedding service or graph database for a small repository. The file system remains portable, reviewable, versionable, and usable when optional services are offline.

## Health signals

Audit behavior rather than file count. Useful signals include:

- handoff resume success and stale-checkpoint frequency;
- percentage of indexed paths that resolve;
- lesson precision: applied, irrelevant, and contradicted outcomes;
- repeated mistakes that lacked a retrieved lesson;
- unresolved conflicts hidden from `current.md`;
- context loaded per task versus files actually used;
- projection snapshot lag;
- archive records that no active anchor can reach.

If a metric is not collected reliably, treat it as unknown rather than manufacturing a score.

## Design lineage

- [Letta MemFS](https://github.com/letta-ai/letta-docs-md/blob/main/concepts/memfs/index.md): git-backed Markdown memory, a small always-visible tree, and deeper files loaded on demand.
- [LangGraph persistence](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/persistence.mdx): separation of thread checkpoints from long-term stores.
- [Graphiti](https://github.com/getzep/graphiti): episodic provenance, temporal validity, contradiction history, and hybrid graph retrieval.
- [Mem0](https://github.com/mem0ai/mem0): scoped search, metadata filters, semantic retrieval, and explainable reranking.
