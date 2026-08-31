#!/usr/bin/env python3
"""Structural, schema, routing, handoff, and budget validation for memory roots."""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from memory_model import (
    ConfigError,
    ParsedCall,
    RecordSyntaxError,
    TokenizerUnavailable,
    V3_REQUIRED_FIELDS,
    count_tokens,
    load_config,
    parse_markdown,
)


BASE_REQUIRED_FILES = ("current.md", "index.md", "anchors.md")
V2_FILES = ("project.md",)
V2_DIRECTORIES = ("lessons", "handoffs")
CANONICAL_RECORD_DIRS = (
    "decisions",
    "issues",
    "conflicts",
    "lessons",
    "tasks",
    "handoffs",
    "archive",
)
RECORD_TYPES = {
    "decisions": "Decision",
    "issues": "OpenIssue",
    "conflicts": "Conflict",
    "lessons": "Lesson",
    "tasks": "TaskRecord",
    "handoffs": "HandoffCheckpoint",
    "archive": "ArchiveRecord",
    "patches": "ContextPatch",
}
PLACEHOLDER_MARKERS = ("<", ">", "YYYY", "NNNN", "...")


@dataclass
class Findings:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "info": self.info,
        }


def _read(path: Path, findings: Findings) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        findings.errors.append(f"cannot read {path}: {exc}")
        return None


def _parse_file(
    path: Path,
    findings: Findings,
    expected: str | tuple[str, ...] | None = None,
) -> ParsedCall | None:
    text = _read(path, findings)
    if text is None:
        return None
    try:
        calls = parse_markdown(text, path.as_posix())
    except RecordSyntaxError as exc:
        findings.errors.append(str(exc))
        return None
    if len(calls) != 1:
        findings.errors.append(f"{path}: expected one structured record, found {len(calls)}")
        return None
    call = calls[0]
    expected_names = (expected,) if isinstance(expected, str) else expected
    if expected_names and call.name not in expected_names:
        findings.errors.append(
            f"{path}: expected {' or '.join(expected_names)}, found {call.name}"
        )
        return None
    _validate_no_placeholders(call, path, findings)
    return call


def _walk_values(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, ParsedCall):
        for item in value.fields.values():
            yield from _walk_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_values(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk_values(item)


def _validate_no_placeholders(call: ParsedCall, path: Path, findings: Findings) -> None:
    for value in _walk_values(call):
        if isinstance(value, str) and any(marker in value for marker in PLACEHOLDER_MARKERS):
            findings.errors.append(f"{path.name}: unresolved placeholder: {value}")


def _validate_v3_identifier_fields(
    call: ParsedCall,
    path: Path,
    root: Path,
    schema_version: int,
    findings: Findings,
) -> None:
    if schema_version != 3:
        return
    for field_name in sorted(V3_REQUIRED_FIELDS.get(call.name, ())):
        if field_name not in call.fields:
            findings.errors.append(
                f"{call.name}.{field_name} is required by v3: {path.relative_to(root).as_posix()}"
            )
    fact_ids = call.fields.get("fact_ids")
    if isinstance(fact_ids, list):
        if any(not fact_id.strip() for fact_id in fact_ids):
            findings.errors.append(f"{path.name}: fact_ids must not contain blank identifiers")
        if len(fact_ids) != len(set(fact_ids)):
            findings.errors.append(f"{path.name}: fact_ids must be unique within a record")
    contradiction_id = call.fields.get("contradiction_id")
    if isinstance(contradiction_id, str) and not contradiction_id.strip():
        findings.errors.append(f"{path.name}: contradiction_id must not be blank")


def _safe_relative(root: Path, raw: str, label: str, findings: Findings) -> Path | None:
    candidate_path = Path(raw)
    if candidate_path.is_absolute():
        findings.errors.append(f"{label} must be relative to the memory root: {raw}")
        return None
    candidate = (root / candidate_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        findings.errors.append(f"{label} escapes memory root: {raw}")
        return None
    return candidate


def _safe_glob(root: Path, pattern: str, label: str, findings: Findings) -> set[Path]:
    posix = pattern.replace("\\", "/")
    pure = PurePosixPath(posix)
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or len(pure.parts) != 2
        or pure.parts[0] != "indexes"
    ):
        findings.errors.append(f"{label} must be a one-level relative indexes/ glob: {pattern}")
        return set()
    if not any(char in posix for char in "*?["):
        findings.warnings.append(f"{label} is not a glob and routes only one shard: {pattern}")
    matches: set[Path] = set()
    for path in root.glob(posix):
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            findings.errors.append(f"{label} matched outside memory root: {path}")
            continue
        if path.is_file():
            matches.add(resolved)
    return matches


def _index_entries(call: ParsedCall) -> list[ParsedCall]:
    return list(call.fields.get("entries", []))


def _entry_paths(entries: Iterable[ParsedCall]) -> list[str]:
    return [entry.fields["path"] for entry in entries]


def primary_anchor(anchors: list[str]) -> str:
    """Return the deterministic routing anchor using the v3 precedence rule."""
    precedence = ("component:", "concept:", "operation:", "risk:")
    for prefix in precedence:
        matches = sorted(anchor for anchor in anchors if anchor.startswith(prefix))
        if matches:
            return matches[0]
    return sorted(anchors)[0] if anchors else "record:unanchored"


def _validate_structure(
    root: Path,
    schema_version: int,
    strict_v2: bool,
    strict_v3: bool,
    findings: Findings,
) -> None:
    for relative in BASE_REQUIRED_FILES:
        if not (root / relative).is_file():
            findings.errors.append(f"missing required file: {relative}")
    for relative in V2_FILES:
        if not (root / relative).is_file():
            target = findings.errors if strict_v2 or strict_v3 else findings.warnings
            target.append(f"missing v2 file: {relative}")
    for relative in V2_DIRECTORIES:
        if not (root / relative).is_dir():
            target = findings.errors if strict_v2 or strict_v3 else findings.warnings
            target.append(f"missing v2 directory: {relative}/")
    if strict_v3 or schema_version == 3:
        if not (root / "config.json").is_file():
            findings.errors.append("missing v3 file: config.json")
        if not (root / "indexes").is_dir():
            findings.errors.append("missing v3 directory: indexes/")


def _validate_project_map(
    root: Path,
    findings: Findings,
    logical_root: Path | None = None,
) -> Path | None:
    path = root / "project.md"
    if not path.is_file():
        return None
    call = _parse_file(path, findings, "ProjectMap")
    if call is None:
        return None
    resolution_root = logical_root or root
    project_root = (resolution_root / call.fields["project_root"]).resolve()
    if not project_root.is_dir():
        findings.errors.append(
            f"project.md: project_root does not resolve: {call.fields['project_root']}"
        )
        return None
    declared_memory_root = (project_root / call.fields["memory_root"]).resolve()
    expected_memory_root = logical_root or root
    if declared_memory_root != expected_memory_root:
        findings.errors.append(
            "project.md: memory_root resolves to "
            f"{declared_memory_root}, expected current root {expected_memory_root}"
        )
    findings.info.append(f"validated project map: {call.fields['project_id']}")
    return project_root


def _validate_entry_paths(
    root: Path,
    entries: list[ParsedCall],
    label: str,
    findings: Findings,
) -> set[str]:
    seen: set[str] = set()
    for entry in entries:
        relative = entry.fields["path"].replace("\\", "/")
        if relative in seen:
            findings.errors.append(f"duplicate indexed path in {label}: {relative}")
            continue
        seen.add(relative)
        candidate = _safe_relative(root, relative, f"indexed path in {label}", findings)
        if candidate is not None and not candidate.is_file():
            findings.errors.append(f"indexed path does not resolve: {relative}")
    return seen


def _validate_indexes(
    root: Path,
    schema_version: int,
    snapshot: str | None,
    findings: Findings,
) -> tuple[set[str], ParsedCall | None]:
    index_path = root / "index.md"
    if not index_path.is_file():
        return set(), None
    expected = ("MemoryIndex", "ActiveMemoryIndex")
    index = _parse_file(index_path, findings, expected)
    if index is None:
        return set(), None
    index_snapshot = index.fields["snapshot_id"]
    if snapshot is not None and index_snapshot != snapshot:
        findings.errors.append(
            f"commit boundary mismatch: current.md={snapshot} index.md={index_snapshot}"
        )
    if schema_version == 3 and index.name != "ActiveMemoryIndex":
        findings.errors.append("v3 config requires ActiveMemoryIndex in index.md")
    if schema_version == 2 and index.name == "ActiveMemoryIndex":
        findings.warnings.append("v3 index found without config.json; treat root as incomplete v3")

    reachable = _validate_entry_paths(root, _index_entries(index), "index.md", findings)
    if index.name == "MemoryIndex":
        findings.info.append(f"validated indexed paths: {len(reachable)}")
        return reachable, index

    for entry in _index_entries(index):
        if entry.fields["status"] != "active":
            findings.errors.append(
                f"root index may contain only active entries: {entry.fields['path']}"
            )

    routes: list[ParsedCall] = index.fields["routes"]
    route_ids: set[str] = set()
    routed_shards: set[Path] = set()
    for route in routes:
        route_id = route.fields["route_id"]
        if route_id in route_ids:
            findings.errors.append(f"duplicate route_id: {route_id}")
        route_ids.add(route_id)
        routed_shards.update(
            _safe_glob(root, route.fields["path_glob"], f"route {route_id}", findings)
        )

    shard_count = 0
    for shard_path in sorted((root / "indexes").rglob("*.md")) if (root / "indexes").is_dir() else []:
        if shard_path.name.startswith("_"):
            continue
        shard_count += 1
        shard = _parse_file(shard_path, findings, "TopicIndex")
        if shard is None:
            continue
        if shard_path.resolve() not in routed_shards:
            findings.errors.append(
                f"topic shard is not reachable from an index route: {shard_path.relative_to(root).as_posix()}"
            )
        if snapshot is not None and shard.fields["snapshot_id"] > snapshot:
            findings.errors.append(
                f"topic shard snapshot is newer than commit boundary: "
                f"{shard_path.relative_to(root).as_posix()}={shard.fields['snapshot_id']} current={snapshot}"
            )
        paths = _validate_entry_paths(root, _index_entries(shard), shard_path.name, findings)
        overlap = reachable & paths
        if overlap:
            findings.errors.append(
                f"paths duplicated between root and topic indexes: {', '.join(sorted(overlap))}"
            )
        reachable.update(paths)
        archive_route = shard.fields["archive_route"]
        if archive_route is not None:
            candidate = _safe_relative(root, archive_route, "archive_route", findings)
            if candidate is not None and not candidate.is_file():
                findings.errors.append(f"archive_route does not resolve: {archive_route}")

    if not routes:
        findings.errors.append("ActiveMemoryIndex requires at least one topic route")
    if shard_count == 0:
        findings.errors.append("v3 memory requires at least one TopicIndex shard")
    findings.info.append(
        f"validated root entries: {len(_index_entries(index))}; topic shards: {shard_count}; reachable paths: {len(reachable)}"
    )
    return reachable, index


def _canonical_records(root: Path) -> set[str]:
    records: set[str] = set()
    for directory_name in CANONICAL_RECORD_DIRS:
        directory = root / directory_name
        if not directory.is_dir():
            continue
        for path in directory.rglob("*.md"):
            relative = path.relative_to(root).as_posix()
            if path.name.startswith("_") or relative == "archive/manifest.md":
                continue
            records.add(relative)
    return records


def _validate_archive_manifest(root: Path, findings: Findings) -> set[str]:
    path = root / "archive" / "manifest.md"
    if not path.is_file():
        return set()
    call = _parse_file(path, findings, "ArchiveManifest")
    if call is None:
        return set()
    paths: set[str] = set()
    for entry in call.fields["entries"]:
        relative = entry.fields["path"].replace("\\", "/")
        candidate = _safe_relative(root, relative, "archive manifest path", findings)
        if candidate is not None and not candidate.is_file():
            findings.errors.append(f"archive manifest path does not resolve: {relative}")
        paths.add(relative)
    return paths


def _validate_reachability(
    root: Path,
    schema_version: int,
    indexed: set[str],
    manifest_paths: set[str],
    findings: Findings,
) -> None:
    if schema_version != 3:
        return
    records = _canonical_records(root)
    missing = sorted(records - indexed - manifest_paths)
    for relative in missing:
        findings.errors.append(f"canonical record is unreachable from indexes: {relative}")
    if not missing:
        findings.info.append(f"validated canonical reachability: {len(records)} records")


def _project_path(
    raw: str,
    project_root: Path,
    source: Path,
    field_name: str,
    should_exist: bool,
    findings: Findings,
) -> None:
    if re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.IGNORECASE):
        findings.info.append(f"{source.name}: skipped external {field_name}: {raw}")
        return
    path = Path(raw)
    candidate = path.resolve() if path.is_absolute() else (project_root / path).resolve()
    if not path.is_absolute():
        try:
            candidate.relative_to(project_root)
        except ValueError:
            findings.errors.append(f"{source.name}: relative {field_name} escapes project root: {raw}")
            return
    if should_exist and not candidate.exists():
        findings.errors.append(f"{source.name}: {field_name} does not resolve: {raw}")
    if not should_exist and candidate.exists():
        findings.errors.append(f"{source.name}: deleted_files path still exists: {raw}")


def _validate_handoffs(
    root: Path,
    project_root: Path | None,
    snapshot: str | None,
    schema_version: int,
    findings: Findings,
) -> None:
    directory = root / "handoffs"
    if not directory.is_dir():
        return
    count = 0
    for path in sorted(directory.glob("*.md")):
        if path.name.startswith("_"):
            continue
        count += 1
        call = _parse_file(path, findings, "HandoffCheckpoint")
        if call is None:
            continue
        _validate_v3_identifier_fields(call, path, root, schema_version, findings)
        fields = call.fields
        profile = fields.get("profile", "full")
        checkpoint_snapshot = (
            fields.get("checkpoint_snapshot_id")
            if profile == "lite"
            else fields.get("last_updated_snapshot_id")
        )
        if fields["status"] in {"ready", "accepted"} and snapshot and checkpoint_snapshot != snapshot:
            findings.warnings.append(
                f"{path.name}: {fields['status']} checkpoint targets {checkpoint_snapshot}, current is {snapshot}"
            )
        if fields["status"] == "ready" and fields["next_action"] is None:
            findings.warnings.append(f"{path.name}: ready checkpoint has no next_action")
        if fields["status"] in {"ready", "accepted"} and project_root is not None:
            for value in fields.get("artifacts", []):
                _project_path(value, project_root, path, "artifacts", True, findings)
            for value in fields["changed_files"]:
                _project_path(value, project_root, path, "changed_files", True, findings)
            for value in fields.get("deleted_files", []):
                _project_path(value, project_root, path, "deleted_files", False, findings)
    findings.info.append(f"validated handoff records: {count}")


def _validate_lessons(root: Path, schema_version: int, findings: Findings) -> None:
    directory = root / "lessons"
    if not directory.is_dir():
        return
    count = 0
    for path in sorted(directory.glob("*.md")):
        if path.name.startswith("_"):
            continue
        count += 1
        call = _parse_file(path, findings, "Lesson")
        if call is None:
            continue
        _validate_v3_identifier_fields(call, path, root, schema_version, findings)
        if call.fields["status"] == "active" and not call.fields["supporting_evidence"]:
            findings.warnings.append(f"{path.name}: active lesson has no supporting evidence")
    findings.info.append(f"validated lesson records: {count}")


def _validate_record_schemas(root: Path, schema_version: int, findings: Findings) -> None:
    count = 0
    for directory_name, record_type in RECORD_TYPES.items():
        if directory_name in {"lessons", "handoffs"}:
            continue
        directory = root / directory_name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.md")):
            if path.name.startswith("_") or (
                directory_name == "archive" and path.name == "manifest.md"
            ):
                continue
            count += 1
            call = _parse_file(path, findings, record_type)
            if call is not None:
                _validate_v3_identifier_fields(call, path, root, schema_version, findings)
    findings.info.append(f"validated structured record schemas: {count}")


def _validate_catalog(path: Path, findings: Findings) -> None:
    if not path.is_file():
        findings.errors.append(f"catalog does not exist: {path}")
        return
    call = _parse_file(path, findings, "ProjectCatalog")
    if call is None:
        return
    base = path.parent.resolve()
    project_ids: set[str] = set()
    for pointer in call.fields["projects"]:
        project_id = pointer.fields["project_id"]
        if project_id in project_ids:
            findings.errors.append(f"duplicate project_id in catalog: {project_id}")
        project_ids.add(project_id)
        for field_name in ("root", "memory_root"):
            raw = pointer.fields[field_name]
            candidate = (base / raw).resolve()
            if not candidate.exists():
                findings.errors.append(f"{project_id}: {field_name} does not resolve: {raw}")
    findings.info.append(f"validated project catalog entries: {len(project_ids)}")


def budget_report(root: Path, require_exact: bool = False) -> dict[str, Any]:
    if (root / "SKILL.md").is_file() and not (root / "current.md").is_file():
        measurement = count_tokens(
            (root / "SKILL.md").read_text(encoding="utf-8"),
            "o200k_base",
            require_exact=require_exact,
        )
        return {
            "schema_version": 3,
            "profile": "skill",
            "items": [
                {
                    "kind": "skill_entry",
                    "path": "SKILL.md",
                    "tokens": measurement.tokens,
                    "target": 1800,
                    "hard": 2000,
                    "exact": measurement.exact,
                    "method": measurement.method,
                    "over_target": measurement.tokens > 1800,
                    "over_hard": measurement.tokens > 2000,
                }
            ],
        }
    config, schema_version = load_config(root)
    items: list[dict[str, Any]] = []
    targets = [("current", root / "current.md"), ("root_index", root / "index.md")]
    if (root / "indexes").is_dir():
        targets.extend(("topic_index", path) for path in sorted((root / "indexes").rglob("*.md")) if not path.name.startswith("_"))
    for kind, path in targets:
        if not path.is_file():
            continue
        measurement = count_tokens(
            path.read_text(encoding="utf-8"),
            config["tokenizer"]["encoding"],
            require_exact=require_exact,
        )
        limits = config["budgets"][kind]
        items.append(
            {
                "kind": kind,
                "path": path.relative_to(root).as_posix(),
                "tokens": measurement.tokens,
                "target": limits["target"],
                "hard": limits["hard"],
                "exact": measurement.exact,
                "method": measurement.method,
                "over_target": measurement.tokens > limits["target"],
                "over_hard": measurement.tokens > limits["hard"],
            }
        )
    return {"schema_version": schema_version, "items": items}


def _validate_budgets(root: Path, schema_version: int, findings: Findings, require_exact: bool) -> None:
    try:
        report = budget_report(root, require_exact=require_exact)
    except (ConfigError, TokenizerUnavailable, OSError, UnicodeError) as exc:
        findings.errors.append(str(exc))
        return
    for item in report["items"]:
        suffix = "exact" if item["exact"] else f"estimated:{item['method']}"
        message = (
            f"{item['path']}: {item['tokens']} tokens ({suffix}); "
            f"target={item['target']} hard={item['hard']}"
        )
        if item["over_hard"]:
            (findings.errors if schema_version == 3 else findings.warnings).append(
                f"budget hard limit exceeded: {message}"
            )
        elif item["over_target"]:
            findings.warnings.append(f"budget target exceeded: {message}")
        else:
            findings.info.append(f"budget: {message}")


def validate_memory(
    root: Path,
    *,
    catalog: Path | None = None,
    strict_v2: bool = False,
    strict_v3: bool = False,
    require_exact_tokens: bool = False,
    logical_root: Path | None = None,
) -> Findings:
    root = root.resolve()
    findings = Findings()
    if not root.is_dir():
        findings.errors.append(f"memory root does not exist: {root}")
        return findings
    try:
        _, schema_version = load_config(root)
    except ConfigError as exc:
        findings.errors.append(str(exc))
        schema_version = 3 if (root / "config.json").exists() else 2
    if strict_v3 and schema_version != 3:
        findings.errors.append("--strict-v3 requires config.json with schema_version 3")
    _validate_structure(root, schema_version, strict_v2, strict_v3, findings)

    current = _parse_file(root / "current.md", findings, "ProjectState") if (root / "current.md").is_file() else None
    snapshot = current.fields["snapshot_id"] if current else None
    project_root = _validate_project_map(
        root,
        findings,
        logical_root.resolve() if logical_root is not None else None,
    )
    _parse_file(root / "anchors.md", findings, "AnchorRegistry") if (root / "anchors.md").is_file() else None
    indexed, _ = _validate_indexes(root, schema_version, snapshot, findings)
    manifest_paths = _validate_archive_manifest(root, findings)
    _validate_reachability(root, schema_version, indexed, manifest_paths, findings)
    _validate_handoffs(root, project_root, snapshot, schema_version, findings)
    _validate_lessons(root, schema_version, findings)
    _validate_record_schemas(root, schema_version, findings)
    _validate_budgets(root, schema_version, findings, require_exact_tokens)
    if snapshot and not any("commit boundary mismatch" in error for error in findings.errors):
        findings.info.append(f"committed snapshot: {snapshot}")
    if catalog is not None:
        _validate_catalog(catalog.resolve(), findings)
    return findings


def path_matches_glob(path: str, pattern: str) -> bool:
    """Portable helper exposed for deterministic route tests."""
    return fnmatch.fnmatchcase(path.replace(os.sep, "/"), pattern.replace(os.sep, "/"))
