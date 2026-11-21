# Memory Record Schema

Read this reference before creating or changing canonical records. The exact v3 contract is machine-enforced; inspect it instead of guessing:

```text
python <skill-root>/scripts/memoryctl.py schema <RecordName> --json
python <skill-root>/scripts/memoryctl.py validate <memory-root> --strict-v3
```

## Safe structured syntax

Canonical records use one Python-fenced, keyword-only schema call. The syntax is data, never executable Python. The validator parses it with `ast` and rejects:

- positional or duplicate arguments;
- unknown calls or fields;
- attributes, imports, comprehensions, operators other than signed integers, keyword unpacking, and executable expressions;
- wrong scalar/list/dictionary/nested-record types;
- invalid enum values and missing required fields.

Allowed values are strings, integers, booleans, `None`, lists, string-key dictionaries, and approved nested records such as `SourceRef`, `IndexEntry`, `IndexRoute`, and `MistakeDigest`. Keep one top-level record per Markdown file. Templates contain placeholders and are not valid committed records until every placeholder and enum alternative is replaced.

## Versions and configuration

A root without `config.json` is v2. Read and write it compatibly; `--strict-v2` keeps the established project, lesson, handoff, snapshot, path, and artifact checks. Do not normalize or migrate it implicitly.

V3 requires this exact configuration shape:

```json
{
  "schema_version": 3,
  "record_mode": "standard",
  "budgets": {
    "current": {"target": 450, "hard": 600},
    "root_index": {"target": 1500, "hard": 2000},
    "topic_index": {"target": 1500, "hard": 2000}
  },
  "tokenizer": {
    "encoding": "o200k_base",
    "fallback": "max_utf8_bytes_or_chars_div_3"
  },
  "retrieval": {"normal_top_k": 3, "expanded_top_k": 8}
}
```

Unknown or missing configuration fields fail validation. CI and release checks require exact `tiktoken/o200k_base`; runtime may use the named conservative estimate and must label it `estimated`.

## Common records

Use stable IDs and semantic anchors. Exact fields, requiredness, nested types, and enums come from `memoryctl schema`.

- `ProjectState`: the bounded `current.md` dashboard and commit marker. It links active IDs; it does not duplicate detail.
- `ProjectMap`: stable project boundaries, roots, components, entry points, and authoritative sources.
- `ActiveMemoryIndex`: v3 root active entries plus `IndexRoute` pointers. V2 uses `MemoryIndex`.
- `TopicIndex`: bounded entries for one topic or time shard plus an optional archive-manifest route. Its `snapshot_id` is the last snapshot that changed that shard; it may be older than the commit boundary but never newer.
- `AnchorRegistry`: aliases and explicit deprecations.
- `Decision`, `OpenIssue`, `Conflict`: explicit choices, unresolved questions, and incompatible positions.
- `MistakeDigest`: one observed episode in an archive record. `cause=None` is preferable to an invented cause.
- `Lesson`: scoped reusable prevention. Only an applicable `active` lesson can produce a required check.
- `TaskRecord`: bounded objective, task-base snapshot, latest reflected snapshot, outcomes, remaining work, lessons, and handoffs.
- `HandoffCheckpoint`: conditional `lite` or `full` continuation contract; see `handoff-protocol.md`.
- `ArchiveRecord` and `ArchiveManifest`: recoverable history and its reachable pointer.
- `ContextPatch`: durable change proposal/result. Minimal mode intentionally omits it.
- `SourceRef`: stable provenance such as project, task, handoff, lesson, mistake, patch, archive, conversation label, file, or commit.

Every v3 `Decision`, `OpenIssue`, `Conflict`, `Lesson`, `TaskRecord`, `HandoffCheckpoint`, and `ArchiveRecord` includes first-class `fact_ids`. Store only stable exact identifiers there; keep explanation in the record's prose fields. Every v3 `OpenIssue` also includes `contradiction_id=None` or an exact identifier, while every `Conflict` includes a non-null exact `contradiction_id`. The shared parser keeps these fields optional so untouched v2 roots remain readable, but strict-v3 validation requires their presence, rejects blanks and duplicate fact IDs, and migration adds them without inventing facts. A migrated `Conflict` uses its existing record ID as the fallback contradiction identifier; migrated fact lists remain empty until evidence-backed curation.

Never place credentials, tokens, keys, recovery codes, raw personal data, or unrelated sensitive material in records, source notes, patches, staging, backups, or archives. If a sensitive source matters, store only a redacted summary and an approved secure-location pointer.

## V3 active and topic indexes

```python
ActiveMemoryIndex(
    snapshot_id="SNAP-20260830-010000",
    entries=[
        IndexEntry(
            anchors=["component:parser", "decision:ADR-0001"],
            summary="Parser syntax is frozen by ADR-0001.",
            path="decisions/ADR-0001-parser-syntax.md",
            importance="L1_active_decision",
            status="active",
            related=[],
            record_snapshot_id="SNAP-20260830-005900",
        ),
    ],
    routes=[
        IndexRoute(
            route_id="route:parser-history",
            anchor_prefixes=["component:parser", "concept:syntax"],
            path_glob="indexes/parser-*.md",
            summary="Parser decisions, failures, and historical tasks.",
            status_scope=["active", "archived", "deprecated"],
        ),
    ],
)
```

Root entries must be active. A path appears in either the root entries or one topic shard, not both. Every canonical decision, issue, conflict, lesson, task, handoff, and archive record must be reachable from an index or archive manifest. Route globs are relative, stay under `indexes/`, and cannot escape the memory root.

Choose an entry's primary routing anchor in this order: `component:` → `concept:` → `operation:` → `risk:` → first stable record-type anchor. Use semantic topic shards first. When one topic remains too large, split historical shards by year, then month; keep the root route as one bounded glob such as `indexes/tasks-*.md`.

V3 checks unique canonical IDs, an ID or ID-plus-slug filename, index status, and resolvable references from current.md. Every unresolved conflict must appear there. Snapshot IDs must contain a real date and time. A one-day allowance accommodates legacy local timestamps and clock skew. Indexed versions cannot exceed the committed boundary.

Index status is `scratch` for candidate lessons, tentative decisions or draft handoffs, `deprecated` for deprecated or superseded records, and `archived` for completed, resolved, consumed, rejected and archived records. Open and actionable states map to `active`. Credential detection covers common key formats and private-key headers, including Markdown prose. Source review is still necessary for personal data and credentials in other forms.

## Status and evidence rules

- Discussion becomes a `Decision` only after an explicit choice source exists.
- Do not put a fact ID in `choice`, a decision ID in `fact_ids`, or explanatory prose in `contradiction_id`; typed identifiers are separate retrieval and evaluation channels.
- Keep an issue visible until a durable resolution exists.
- Expose incompatible evidence as `Conflict`; do not silently choose a side.
- A one-off or weak failure remains `MistakeDigest` or `Lesson(status="candidate")`.
- Preserve superseded records, mark their current status, and link replacements.
- `record_snapshot_id` identifies the last snapshot that changed the linked record, not necessarily the current commit boundary.

Use IDs such as `ADR-0001`, `ISSUE-0001`, `CONFLICT-0001`, `LESSON-0001`, `TASK-YYYYMMDD-NNN`, `HANDOFF-YYYYMMDD-HHMMSS-slug`, `MISTAKE-YYYYMMDD-NNN`, `PATCH-YYYYMMDD-HHMMSS-slug`, and `ARCHIVE-YYYYMMDD-slug`.

## Snapshots and reports

`current.md` and root `index.md` must share one sortable `SNAP-YYYYMMDD-HHMMSS[-suffix]`; together they define the commit boundary. A target sorts after its base. Topic and record snapshot fields may remain older when unchanged. Never trust a mixed control boundary or start a second transaction while `.update-lock`, `.staging`, or recovery-required evidence exists.

Normal high-completeness reads use a one-line coverage summary. Detailed forms are reserved for incomplete/conflicting reads, writes, handoffs, or explicit requests:

```python
CoverageReport(loaded_files=[...], not_loaded_but_possibly_relevant=[...], completeness="low | medium | high", risk_notes=[...])
MemoryChangeReport(created=[...], updated=[...], archived=[...], pending_conflicts=[...], new_snapshot_id="SNAP-... | unchanged")
HandoffReport(checkpoint="HANDOFF-... | none", disposition="created | accepted | rebased | rejected | consumed | none", snapshot_id="SNAP-... | unchanged", next_action="... | none")
```

Use `new_snapshot_id="unchanged"` when no committed transaction occurred.
