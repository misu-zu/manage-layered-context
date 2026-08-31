#!/usr/bin/env python3
"""Deterministic, explainable, progressively routed candidate retrieval."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

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


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def term_counts(text: str) -> Counter[str]:
    value = normalize(text)
    terms: Counter[str] = Counter()
    for token in ASCII_TOKEN_RE.findall(value):
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
    if raw.is_absolute():
        warnings.append(f"skipped absolute indexed path: {relative}")
        return None
    resolved = (root / raw).resolve()
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


def _glob_shards(root: Path, pattern: str, warnings: list[str]) -> list[Path]:
    posix = pattern.replace("\\", "/")
    pure = PurePosixPath(posix)
    if pure.is_absolute() or ".." in pure.parts or not posix.startswith("indexes/"):
        warnings.append(f"unsafe route glob ignored: {pattern}")
        return []
    return sorted(path for path in root.glob(posix) if path.is_file())


def _choose_shard(paths: list[Path], query: str, warnings: list[str]) -> list[Path]:
    if len(paths) <= 1:
        return paths
    query_terms = term_counts(query)
    scored = []
    for path in paths:
        name_terms = term_counts(path.stem)
        score = 2 * sum(min(count, name_terms[term]) for term, count in query_terms.items())
        text = _read(path, warnings)
        if text is not None:
            shard_terms = term_counts(text)
            score += sum(min(count, shard_terms[term]) for term, count in query_terms.items())
        scored.append((score, path))
    positive = [item for item in scored if item[0] > 0]
    if positive:
        return [sorted(positive, key=lambda item: (-item[0], item[1].as_posix()))[0][1]]
    return [sorted(paths)[-1]]


def routed_paths(root: Path, query: str, kinds: tuple[str, ...], warnings: list[str]) -> tuple[list[str], list[str]]:
    """Return candidate record paths and selected v3 route IDs without scanning records."""
    index = _parse_one(root / "index.md", warnings)
    if index is None:
        return [], []
    allowed_prefixes = tuple(f"{kind}/" for kind in kinds)
    paths = [
        entry.fields["path"]
        for entry in index.fields.get("entries", [])
        if entry.fields["path"].replace("\\", "/").startswith(allowed_prefixes)
    ]
    if index.name == "MemoryIndex":
        return paths, []
    if index.name != "ActiveMemoryIndex":
        warnings.append(f"unsupported index type: {index.name}")
        return paths, []

    query_terms = term_counts(query)
    scored = [(_route_score(route, query_terms, query), route) for route in index.fields["routes"]]
    positive = [item for item in scored if item[0] > 0]
    chosen = sorted(positive or scored, key=lambda item: (-item[0], item[1].fields["route_id"]))[:1]
    selected_routes: list[str] = []
    seen_shards: set[Path] = set()
    for _, route in chosen:
        selected_routes.append(route.fields["route_id"])
        shard_paths = _choose_shard(
            _glob_shards(root, route.fields["path_glob"], warnings),
            query,
            warnings,
        )
        for shard_path in shard_paths:
            resolved = shard_path.resolve()
            if resolved in seen_shards:
                continue
            seen_shards.add(resolved)
            shard = _parse_one(shard_path, warnings)
            if shard is None or shard.name != "TopicIndex":
                warnings.append(f"route target is not a TopicIndex: {shard_path}")
                continue
            for entry in shard.fields["entries"]:
                relative = entry.fields["path"].replace("\\", "/")
                if relative.startswith(allowed_prefixes):
                    paths.append(relative)
    return list(dict.fromkeys(paths)), selected_routes


def _legacy_paths(root: Path, kinds: tuple[str, ...], warnings: list[str]) -> list[str]:
    paths: list[str] = []
    for kind in kinds:
        directory = root / kind
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.md")):
            if path.name.startswith("_") or ".staging" in path.parts:
                continue
            resolved = path.resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                warnings.append(f"skipped path outside memory root: {path}")
                continue
            paths.append(resolved.relative_to(root).as_posix())
    return paths


def collect_documents(root: Path, paths: list[str], warnings: list[str]) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for relative in paths:
        resolved = _safe_record_path(root, relative, warnings)
        if resolved is None:
            continue
        text = _read(resolved, warnings)
        if text is None:
            continue
        heading = HEADING_RE.search(text)
        documents.append(
            {
                "kind": resolved.relative_to(root).parts[0],
                "relative_path": resolved.relative_to(root).as_posix(),
                "title": heading.group(1).strip() if heading else resolved.stem,
                "normalized": normalize(text),
                "terms": term_counts(text),
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
        if not matched and query_normalized not in document["normalized"]:
            continue
        score = 0.0
        contribution: dict[str, float] = {}
        path_title = normalize(f"{document['relative_path']} {document['title']}")
        for term in matched:
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
        ranked.append(
            {
                "path": document["relative_path"],
                "kind": document["kind"],
                "title": document["title"],
                "score": round(score, 3),
                "matched_terms": sorted(contribution, key=lambda item: (-contribution[item], item))[:12],
            }
        )
    ranked.sort(key=lambda item: (-item["score"], item["path"]))
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
    warnings: list[str] = []
    if schema_version == 3:
        paths, selected_routes = routed_paths(root, query, kinds, warnings)
    else:
        paths = _legacy_paths(root, kinds, warnings)
        selected_routes = []
    documents = collect_documents(root, paths, warnings)
    candidates = rank(documents, query)[:effective_top]
    return {
        "memory_root": str(root),
        "schema_version": schema_version,
        "snapshot_id": current_snapshot(root),
        "query": query,
        "kinds": list(kinds),
        "top": effective_top,
        "expanded": expand,
        "selected_routes": selected_routes,
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
