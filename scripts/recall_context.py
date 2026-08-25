#!/usr/bin/env python3
"""Rank canonical memory files without loading their contents into model context.

This is a deterministic candidate generator. It does not decide whether a memory is
true or applicable; the calling agent must read and validate every selected record.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path


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
SNAPSHOT_RE = re.compile(r'\bsnapshot_id\s*=\s*"([^"]+)"')
MAX_FILE_BYTES = 2 * 1024 * 1024


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def term_counts(text: str) -> Counter[str]:
    """Tokenize identifiers, words, and CJK bi/trigrams for mixed-language recall."""
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
            if len(sequence) >= size:
                for offset in range(len(sequence) - size + 1):
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
            valid = ", ".join(DEFAULT_KINDS)
            raise ValueError(f"unknown kind '{item}'; expected one of: {valid}")
        if item not in result:
            result.append(item)
    return tuple(result)


def read_text(path: Path, warnings: list[str]) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            warnings.append(f"skipped oversized record: {path}")
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        warnings.append(f"could not read {path}: {exc}")
        return None


def collect_documents(root: Path, kinds: tuple[str, ...], warnings: list[str]) -> list[dict]:
    documents: list[dict] = []
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
            text = read_text(resolved, warnings)
            if text is None:
                continue
            heading = HEADING_RE.search(text)
            documents.append(
                {
                    "kind": kind,
                    "path": resolved,
                    "relative_path": resolved.relative_to(root).as_posix(),
                    "title": heading.group(1).strip() if heading else resolved.stem,
                    "text": text,
                    "normalized": normalize(text),
                    "terms": term_counts(text),
                }
            )
    return documents


def rank(documents: list[dict], query: str) -> list[dict]:
    query_normalized = normalize(query).strip()
    query_counts = term_counts(query)
    if not query_counts:
        return []

    document_frequency: Counter[str] = Counter()
    for document in documents:
        document_frequency.update(document["terms"].keys())

    total_documents = max(len(documents), 1)
    ranked: list[dict] = []
    for document in documents:
        matched = set(query_counts) & set(document["terms"])
        if not matched and query_normalized not in document["normalized"]:
            continue

        score = 0.0
        contribution: dict[str, float] = {}
        path_and_title = normalize(f"{document['relative_path']} {document['title']}")
        for term in matched:
            inverse_frequency = math.log((total_documents + 1) / (document_frequency[term] + 1)) + 1
            query_weight = 1 + math.log1p(query_counts[term])
            identifier_weight = 1.8 if any(char in term for char in ":/\\._-") else 1.0
            location_weight = 1.75 if term in path_and_title else 1.0
            frequency_weight = 1 + min(math.log1p(document["terms"][term]), 1.5) * 0.2
            term_score = inverse_frequency * query_weight * identifier_weight * location_weight * frequency_weight
            contribution[term] = term_score
            score += term_score

        if query_normalized and query_normalized in document["normalized"]:
            score += 8.0
        if query_normalized and query_normalized in path_and_title:
            score += 12.0

        best_terms = sorted(contribution, key=lambda item: (-contribution[item], item))[:12]
        ranked.append(
            {
                "path": document["relative_path"],
                "kind": document["kind"],
                "title": document["title"],
                "score": round(score, 3),
                "matched_terms": best_terms,
            }
        )

    ranked.sort(key=lambda item: (-item["score"], item["path"]))
    return ranked


def current_snapshot(root: Path) -> str | None:
    path = root / "current.md"
    try:
        match = SNAPSHOT_RE.search(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return None
    return match.group(1) if match else None


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rank task-relevant memory record paths with explainable lexical matching."
    )
    parser.add_argument("memory_root", type=Path, help="Path to one project's memory root")
    parser.add_argument("--query", required=True, help="Task fingerprint or search query")
    parser.add_argument(
        "--kinds",
        default=",".join(DEFAULT_KINDS),
        help="Comma-separated record directories, or 'all'",
    )
    parser.add_argument("--top", type=int, default=8, help="Maximum candidates to return")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = args.memory_root.resolve()
    if not root.is_dir():
        print(f"error: memory root does not exist: {root}", file=sys.stderr)
        return 2
    if args.top < 1:
        print("error: --top must be at least 1", file=sys.stderr)
        return 2

    try:
        kinds = selected_kinds(args.kinds)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    warnings: list[str] = []
    documents = collect_documents(root, kinds, warnings)
    candidates = rank(documents, args.query)[: args.top]
    result = {
        "memory_root": str(root),
        "snapshot_id": current_snapshot(root),
        "query": args.query,
        "kinds": list(kinds),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "warnings": warnings,
        "notice": "Candidates are search hints; read canonical records before relying on them.",
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"snapshot: {result['snapshot_id'] or 'unknown'}")
        print(f"query: {args.query}")
        if not candidates:
            print("no matching candidate records")
        for index, candidate in enumerate(candidates, start=1):
            terms = ", ".join(candidate["matched_terms"]) or "exact phrase"
            print(
                f"{index}. {candidate['path']} "
                f"(score={candidate['score']}, kind={candidate['kind']}, matches={terms})"
            )
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
        print(result["notice"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
