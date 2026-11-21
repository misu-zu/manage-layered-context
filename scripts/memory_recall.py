#!/usr/bin/env python3
"""Deterministic, explainable, progressively routed candidate retrieval."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from memory_validation import record_status, transaction_warnings
from memory_model import ConfigError, ParsedCall, RecordSyntaxError, load_config, parse_markdown


DEFAULT_KINDS = (
    "lessons",
    "archive",
    "handoffs",
    "decisions",
    "issues",
    "conflicts",
    "tasks",
)
KIND_ALIASES = {
    "lesson": "lessons",
    "archives": "archive",
    "handoff": "handoffs",
    "decision": "decisions",
    "issue": "issues",
    "conflict": "conflicts",
    "task": "tasks",
}
ASCII_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.:/\\-]*", re.IGNORECASE)
WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
HEADING_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
MAX_FILE_BYTES = 2 * 1024 * 1024


STOP_WORDS = frozenset(
    "a an and are as at be by can did do does for from had has have how in into is it its "
    "of on or our should than that the their there this to was we were what when which who why will with".split()
)


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def term_counts(text: str) -> Counter[str]:
    value = normalize(text)
    terms: Counter[str] = Counter()
    for token in ASCII_TOKEN_RE.findall(value):
        token = token.strip(".:/\\\\_-")
        if len(token) >= 2:
            terms[token] += 1
    for word in WORD_RE.findall(value):
        if len(word) >= 2 and not CJK_RE.fullmatch(word):
            terms[word] += 1
    for sequence in CJK_RE.findall(value):
        if len(sequence) <= 24:
            terms[sequence] += 1
        for size in (2, 3):
            for offset in range(max(len(sequence) - size + 1, 0)):
                terms[sequence[offset : offset + size]] += 1
    for word in STOP_WORDS:
        terms.pop(word, None)
    return terms


def selected_kinds(raw: str) -> tuple[str, ...]:
    requested = [part.strip().casefold() for part in raw.split(",") if part.strip()]
    if not requested or requested == ["all"]:
        return DEFAULT_KINDS
    result: list[str] = []
    for item in requested:
        item = KIND_ALIASES.get(item, item)
        if item not in DEFAULT_KINDS:
            raise ValueError(f"unknown kind '{item}'; expected one of: {', '.join(DEFAULT_KINDS)}")
        if item not in result:
            result.append(item)
    return tuple(result)


def _read(path: Path, warnings: list[str]) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            warnings.append(f"skipped oversized record: {path}")
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        warnings.append(f"could not read {path}: {exc}")
        return None


def _safe_record_path(root: Path, relative: str, warnings: list[str]) -> Path | None:
    raw = Path(relative)
    if raw.is_absolute() or ".." in raw.parts:
        warnings.append(f"skipped absolute indexed path: {relative}")
        return None
    path = root / raw
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent.is_relative_to(root)):
        warnings.append(f"skipped symbolic link: {relative}")
        return None
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        warnings.append(f"skipped indexed path outside memory root: {relative}")
        return None
    if not resolved.is_file():
        warnings.append(f"indexed record is missing: {relative}")
        return None
    return resolved


def _parse_one(path: Path, warnings: list[str]) -> ParsedCall | None:
    text = _read(path, warnings)
    if text is None:
        return None
    try:
        calls = parse_markdown(text, path.as_posix())
    except RecordSyntaxError as exc:
        warnings.append(str(exc))
        return None
    if len(calls) != 1:
        warnings.append(f"expected one structured record in {path}")
        return None
    return calls[0]


def _route_score(route: ParsedCall, query_terms: Counter[str], query: str) -> float:
    fields = route.fields
    text = " ".join(
        [fields["route_id"], fields["summary"], fields["path_glob"], *fields["anchor_prefixes"]]
    )
    route_terms = term_counts(text)
    score = float(sum(min(count, route_terms[term]) for term, count in query_terms.items()))
    normalized_query = normalize(query)
    for prefix in fields["anchor_prefixes"]:
        if normalize(prefix) in normalized_query:
            score += 6.0
    return score


def _index_metadata(root: Path, index: ParsedCall, query: str, warnings: list[str]):
    entries = {e.fields["path"].replace("\\", "/"): e.fields for e in index.fields["entries"]}
    boosts: dict[str, float] = {}
    routes = []
    skipped_routes = []
    skipped_shards = []
    shard_scores: dict[Path, float] = {}
    query_terms = term_counts(query)
    for route in index.fields.get("routes", []):
        fields = route.fields
        pattern = PurePosixPath(fields["path_glob"].replace("\\", "/"))
        if len(pattern.parts) != 2 or pattern.parts[0] != "indexes" or ".." in pattern.parts or "**" in pattern.name:
            warnings.append(f"unsafe route ignored: {fields['route_id']}")
            skipped_routes.append(fields["route_id"])
            continue
        routes.append(fields["route_id"])
        score = _route_score(route, query_terms, query)
        matches = [path for path in root.glob(pattern.as_posix()) if path.is_file() and not path.name.startswith("_")]
        if not matches:
            warnings.append(f"no shards match route: {fields['route_id']}")
        for path in matches:
            shard_scores[path] = max(score, shard_scores.get(path, 0))
    for path in sorted((root / "indexes").glob("*.md")):
        if path.name.startswith("_"):
            continue
        if path not in shard_scores:
            warnings.append(f"unrouted topic shard: {path.name}")
            shard_scores[path] = 0
    for path, score in sorted(shard_scores.items()):
        relative = path.relative_to(root).as_posix()
        safe = _safe_record_path(root, relative, warnings)
        shard = _parse_one(safe, warnings) if safe else None
        if not shard or shard.name != "TopicIndex":
            skipped_shards.append(relative)
            warnings.append(f"could not use topic shard: {relative}")
            continue
        for entry in shard.fields["entries"]:
            relative = entry.fields["path"].replace("\\", "/")
            entries[relative] = entry.fields
            boosts[relative] = min(2.0, score * 0.25)
    return entries, boosts, routes, skipped_routes, skipped_shards


def _canonical_paths(root: Path, kinds: tuple[str, ...]) -> list[str]:
    paths: list[str] = []
    for kind in kinds:
        directory = root / kind
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.md")):
            if path.name.startswith("_") or path == root / "archive/manifest.md":
                continue
            paths.append(path.relative_to(root).as_posix())
    return paths


def collect_documents(root: Path, paths: list[str], entries: dict, boosts: dict, warnings: list[str]) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for relative in paths:
        resolved = _safe_record_path(root, relative, warnings)
        if resolved is None:
            continue
        text = _read(resolved, warnings)
        if text is None:
            continue
        heading = HEADING_RE.search(text)
        entry = entries.get(relative, {})
        try:
            records = parse_markdown(text, relative)
            status = record_status(records[0]) if len(records) == 1 else None
        except RecordSyntaxError:
            status = None
        if status is None:
            warnings.append(f"could not determine record status: {relative}")
        search_text = " ".join((text, relative, entry.get("summary", ""), *entry.get("anchors", [])))
        documents.append(
            {
                "kind": resolved.relative_to(root).parts[0],
                "relative_path": resolved.relative_to(root).as_posix(),
                "title": heading.group(1).strip() if heading else resolved.stem,
                "normalized": normalize(search_text),
                "terms": term_counts(search_text),
                "status": status,
                "index_status": entry.get("status"),
                "snapshot_id": entry.get("record_snapshot_id"),
                "route_bonus": boosts.get(relative, 0),
            }
        )
    return documents


def rank(documents: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    query_normalized = normalize(query).strip()
    query_counts = term_counts(query)
    if not query_counts:
        return []
    frequency: Counter[str] = Counter()
    for document in documents:
        frequency.update(document["terms"].keys())
    total = max(len(documents), 1)
    ranked: list[dict[str, Any]] = []
    for document in documents:
        matched = set(query_counts) & set(document["terms"])
        if not matched:
            continue
        score = document["route_bonus"]
        contribution: dict[str, float] = {}
        path_title = normalize(f"{document['relative_path']} {document['title']}")
        if len(query_counts) > 1 and len(matched) == 1:
            only = next(iter(matched))
            if only not in path_title and frequency[only] * 2 >= total and total > 1:
                continue
        for term in sorted(matched):
            inverse = math.log((total + 1) / (frequency[term] + 1)) + 1
            query_weight = 1 + math.log1p(query_counts[term])
            identifier_weight = 1.8 if any(char in term for char in ":/\\._-") else 1.0
            location_weight = 1.75 if term in path_title else 1.0
            term_score = inverse * query_weight * identifier_weight * location_weight
            contribution[term] = term_score
            score += term_score
        if query_normalized and query_normalized in document["normalized"]:
            score += 8.0
        if query_normalized and query_normalized in path_title:
            score += 12.0
        if document["status"] in {"deprecated", "superseded", "rejected"}:
            score *= 0.55
        ranked.append(
            {
                "path": document["relative_path"],
                "kind": document["kind"],
                "title": document["title"],
                "status": document["status"],
                "index_status": document["index_status"],
                "snapshot_id": document["snapshot_id"],
                "score": round(score, 3),
                "matched_terms": sorted(contribution, key=lambda item: (-contribution[item], item))[:12],
            }
        )
    ranked.sort(key=lambda item: item["path"])
    ranked.sort(key=lambda item: (item["score"], item["snapshot_id"] or ""), reverse=True)
    return ranked


def current_snapshot(root: Path) -> str | None:
    warnings: list[str] = []
    call = _parse_one(root / "current.md", warnings)
    return call.fields.get("snapshot_id") if call and call.name == "ProjectState" else None


def recall(
    root: Path,
    query: str,
    *,
    kinds: tuple[str, ...] = DEFAULT_KINDS,
    top: int | None = None,
    expand: bool = False,
    status: str | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"memory root does not exist: {root}")
    try:
        config, schema_version = load_config(root)
    except ConfigError as exc:
        raise ValueError(str(exc)) from exc
    effective_top = top if top is not None else config["retrieval"]["expanded_top_k" if expand else "normal_top_k"]
    if effective_top < 1:
        raise ValueError("top must be at least 1")
    warnings = transaction_warnings(root)
    snapshot = current_snapshot(root)
    index = _parse_one(root / "index.md", warnings)
    entries, boosts, routes, skipped_routes, skipped_shards = {}, {}, [], [], []
    if index and index.name in {"ActiveMemoryIndex", "MemoryIndex"}:
        entries, boosts, routes, skipped_routes, skipped_shards = _index_metadata(root, index, query, warnings)
    paths = _canonical_paths(root, kinds)
    for path in entries:
        if path.startswith(tuple(f"{kind}/" for kind in kinds)) and not (root / path).is_file():
            warnings.append(f"indexed record is missing: {path}")
    documents = collect_documents(root, paths, entries, boosts, warnings)
    if status:
        documents = [d for d in documents if status in (d["status"], d["index_status"])]
    candidates = rank(documents, query)[:effective_top]
    boundary_matches = bool(index and snapshot and index.fields.get("snapshot_id") == snapshot)
    if not boundary_matches:
        warnings.append("commit boundary mismatch or unreadable current.md/index.md")
    if snapshot != current_snapshot(root):
        boundary_matches = False
        warnings.append("snapshot changed during recall")
    warnings = list(dict.fromkeys([*warnings, *transaction_warnings(root)]))
    return {
        "memory_root": str(root),
        "schema_version": schema_version,
        "snapshot_id": snapshot,
        "query": query,
        "kinds": list(kinds),
        "top": effective_top,
        "expanded": expand,
        "selected_routes": routes,
        "unsearched_routes": skipped_routes,
        "unsearched_shards": skipped_shards,
        "boundary_matches": boundary_matches,
        "coverage": "low" if warnings else "complete",
        "no_confident_match": not candidates,
        "records_considered": len(documents),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "retrieval_policy": {
            "mode": "sequential",
            "candidate_limit": effective_top,
            "open_one_at_a_time": True,
            "stop_when_sufficient": True,
        },
        "warnings": warnings,
        "notice": (
            "Candidates are a ranked upper bound, not a batch read list; open one canonical "
            "record at a time and stop when coverage is sufficient."
        ),
    }
