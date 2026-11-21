# Retrieval, Routing, and Context Budgets

Read this reference when selecting v3 topic shards, changing index layout, investigating recall gaps, or enforcing context limits.

## Progressive path

Use this read path:

```text
PROJECTS.md when needed
  -> current.md
  -> root index.md
  -> recall across the project (or follow a topic shard manually)
  -> rank up to 3 candidates
  -> open rank 1, assess, then continue one at a time if needed
  -> raise the candidate limit to 8 only if the first 3 are insufficient
```

`project.md` is conditional orientation, not a mandatory read. `anchors.md` is conditional alias resolution. Do not read every topic shard merely because a root route contains a broad glob. Prefer route summaries and anchor prefixes that let an agent choose a single semantic family.

The helper reads canonical records on disk, including archive-manifest-only records. It uses all topic routes and entry summaries as ranking signals. Only the bounded candidate list enters the agent context.

```text
python <skill-root>/scripts/memoryctl.py recall <memory-root> \
  --query "<project + operation + component + symptoms + constraints>" --json
```

The configured default is a candidate limit of 3. It is not a number of files to load. Open the highest-ranked canonical record, assess whether the query and required evidence are covered, and stop immediately when sufficient. Continue with ranks 2 and 3 one at a time only while a named gap remains. Use `--expand` for a candidate limit of 8 only after all first-stage candidates prove insufficient. Candidate score and route match are explanations, not truth.

## Task fingerprint

Include the selected project, operation, affected components/artifacts, exact error fragments, acceptance criteria, and hard constraints. For risky work also include likely lesson signals such as `operation:migrate`, `risk:destructive-path-resolution`, tool name, platform, and artifact type.

Use `--kinds` for a record family and `--status` for a record or index status. The CLI applies lexical matching after Unicode normalization, drops English function words, gives route matches a bounded bonus, and downranks deprecated and superseded records. Ties prefer the more recent indexed snapshot. An empty result sets `no_confident_match`. Check scope, evidence and contradictions in the source record. Each candidate includes its canonical status and indexed snapshot when available.

`coverage` describes the machine scan. File warnings, a live lock, staged work, or a mixed commit boundary lower it. Unusable routes and shards appear in `unsearched_routes` and `unsearched_shards`. There can still be relevant material outside the selected project or kinds.

If an indexed path is missing, a projection is stale, sources disagree, or a likely shard remains unread, lower completeness and name the gap.

## Root-index bound

Root `index.md` contains only active decision/issue/conflict/task/lesson/handoff entries and a bounded set of `IndexRoute` pointers. Historical records move to topic shards or `archive/manifest.md`. Do not retain one root entry per historical task.

Primary routing anchor precedence is:

```text
component > concept > operation > risk > record type
```

Keep aliases in `anchors.md`; do not duplicate entries for wording variants. A path occurs in one active or topic index. Topic index `snapshot_id` means “last changed at,” so unchanged shards do not require rewriting on every transaction.

Split in this order:

1. semantic component or concept;
2. record family such as tasks, lessons, or archive;
3. year when a shard approaches its target;
4. month when a yearly shard approaches its hard limit.

Keep one root route per bounded family, using a glob such as `indexes/tasks-*.md`. If route metadata itself approaches the root target, merge overly narrow route families before adding infrastructure.

## Budgets

V3 defaults:

| File | Target | Hard |
| --- | ---: | ---: |
| `current.md` | 450 | 600 |
| root `index.md` | 1,500 | 2,000 |
| each `indexes/*.md` | 1,500 | 2,000 |

Measure with `o200k_base`:

```text
python <skill-root>/scripts/memoryctl.py budget <memory-root> --require-exact --json
```

CI and release evaluation require exact `tiktoken` counts. Runtime may use the configured conservative fallback when `tiktoken` is unavailable; it must report `exact=false` and the estimation method. Exact v3 hard-limit violations are errors. Estimated violations are warnings. A transaction checks the budgets of changed files, or every budget when config.json changes. Standalone validation always checks the full root. In v2, the same condition is a warning until explicit migration. Target overflow is always a prompt to compress or shard, not proof of corruption.

Keep `current.md` to active goal, hard invariants, active IDs, blockers/conflicts, latest continuation, and immediate actions. Move rationale and evidence to canonical records. Keep index summaries to one discriminating line.

## Reporting

Normal high-completeness read:

```text
Coverage: high; loaded current.md, index.md, indexes/parser.md, and 2 records; memory unchanged; no material unread risk.
```

Use the full `CoverageReport` only when completeness is low/medium, a conflict or stale source exists, a write/handoff occurred, or the user requests detail. For a clean normal read, do not emit repetitive multi-line metadata that becomes future conversation overhead.

## Escalation

Stay with Markdown and deterministic lexical routing until measured recall failures justify more complexity. Add project-scoped semantic search only when paraphrase misses are material. Add a temporal graph only when relationship traversal and validity history produce measurable gains. Every projection retains project, source path, record ID, status, and snapshot; canonical Markdown remains available when projections are offline.
