#!/usr/bin/env python3
"""Safe structured-record parsing, schemas, configuration, and token budgets."""

from __future__ import annotations

import ast
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any


PYTHON_FENCE_RE = re.compile(r"```python\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
RECORD_MODES = ("minimal", "standard", "detailed", "forensic")
RECORD_STATUSES = ("active", "archived", "deprecated", "scratch")
IMPORTANCE_LEVELS = (
    "L0_invariant",
    "L0_project_map",
    "L1_active_decision",
    "L1_active_lesson",
    "L2_active_issue",
    "L2_active_handoff",
    "L3_working_note",
    "L4_historical_detail",
    "L5_scratch",
)
FACT_ID_RECORDS = (
    "Decision",
    "OpenIssue",
    "Conflict",
    "Lesson",
    "TaskRecord",
    "HandoffCheckpoint",
    "ArchiveRecord",
)
V3_REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    record_name: frozenset({"fact_ids"}) for record_name in FACT_ID_RECORDS
}
V3_REQUIRED_FIELDS["OpenIssue"] = frozenset({"fact_ids", "contradiction_id"})
V3_REQUIRED_FIELDS["Conflict"] = frozenset({"fact_ids", "contradiction_id"})


SNAPSHOT_RE = re.compile(r"SNAP-([0-9]{8}-[0-9]{6})(?:-[a-z0-9]+(?:-[a-z0-9]+)*)?")
TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "assets" / "memory-template"
# Older roots use local wall time. Allow one day for timezone and clock skew.
SNAPSHOT_SKEW = timedelta(days=1)


def validate_snapshot(value: str) -> None:
    match = SNAPSHOT_RE.fullmatch(value)
    if not match:
        raise ValueError(f"invalid snapshot ID: {value}")
    try:
        stamp = datetime.strptime(match[1], "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(f"invalid snapshot date: {value}") from exc
    if stamp > datetime.now(timezone.utc) + SNAPSHOT_SKEW:
        raise ValueError(f"snapshot is too far in the future: {value}")


def next_snapshot(base: str | None = None) -> str:
    now = datetime.now(timezone.utc)
    if base:
        validate_snapshot(base)
        previous = datetime.strptime(base[5:20], "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
        now = max(now, previous + timedelta(seconds=1))
    value = now.strftime("SNAP-%Y%m%d-%H%M%S")
    validate_snapshot(value)
    return value


class RecordSyntaxError(ValueError):
    """Raised when a structured Markdown block is unsafe or schema-invalid."""


class ConfigError(ValueError):
    """Raised when memory/config.json is malformed."""


class TokenizerUnavailable(RuntimeError):
    """Raised when exact counting is required but tiktoken is unavailable."""


@dataclass(frozen=True)
class ParsedCall:
    name: str
    fields: dict[str, Any]
    line: int


@dataclass(frozen=True)
class FieldRule:
    kinds: tuple[str, ...]
    required: bool = True
    enum: tuple[Any, ...] | None = None
    item: "FieldRule | None" = None
    value: "FieldRule | None" = None
    record: str | None = None


@dataclass(frozen=True)
class RecordSpec:
    fields: dict[str, FieldRule]


@dataclass(frozen=True)
class TokenMeasurement:
    tokens: int
    encoding: str
    exact: bool
    method: str


def _rule(
    *kinds: str,
    required: bool = True,
    enum: tuple[Any, ...] | None = None,
    item: FieldRule | None = None,
    value: FieldRule | None = None,
    record: str | None = None,
) -> FieldRule:
    return FieldRule(kinds, required, enum, item, value, record)


STR = _rule("str")
OPT_STR = _rule("str", "none", required=False)
INT = _rule("int")
BOOL = _rule("bool")
STR_LIST = _rule("list", item=STR)
OPT_STR_LIST = _rule("list", required=False, item=STR)
SOURCE_LIST = _rule("list", item=_rule("record", record="SourceRef"))
OPT_SOURCE_LIST = _rule("list", required=False, item=_rule("record", record="SourceRef"))
STR_DICT = _rule("dict", value=STR)
STR_LIST_DICT = _rule("dict", value=STR_LIST)


def _fields(required: tuple[str, ...], optional: tuple[str, ...] = ()) -> dict[str, FieldRule]:
    result = {name: STR for name in required}
    result.update({name: OPT_STR for name in optional})
    return result


SCHEMAS: dict[str, RecordSpec] = {
    "SourceRef": RecordSpec(
        {
            "ref_type": _rule(
                "str",
                enum=(
                    "project",
                    "task",
                    "handoff",
                    "lesson",
                    "mistake",
                    "patch",
                    "archive",
                    "conversation",
                    "file",
                    "commit",
                ),
            ),
            "ref_id": STR,
            "note": OPT_STR,
        }
    ),
    "ProjectPointer": RecordSpec(
        {
            **_fields(("project_id", "name", "purpose", "root", "memory_root")),
            "status": _rule("str", enum=("active", "paused", "archived")),
            "aliases": STR_LIST,
        }
    ),
    "ProjectCatalog": RecordSpec(
        {
            "catalog_version": INT,
            "projects": _rule("list", item=_rule("record", record="ProjectPointer")),
        }
    ),
    "ProjectMap": RecordSpec(
        {
            **_fields(("project_id", "name", "purpose", "project_root", "memory_root")),
            "boundaries": STR_LIST,
            "components": STR_DICT,
            "entry_points": STR_LIST,
            "authoritative_sources": STR_LIST,
            "generated_or_disposable": STR_LIST,
            "navigation_notes": STR_LIST,
        }
    ),
    "ProjectState": RecordSpec(
        {
            "snapshot_id": STR,
            "project_identity": STR,
            "record_mode": _rule("str", enum=RECORD_MODES),
            "active_goal": _rule("str", "none"),
            "invariants": STR_LIST,
            "active_decisions": STR_LIST,
            "active_lessons": OPT_STR_LIST,
            "open_issues": STR_LIST,
            "unresolved_conflicts": STR_LIST,
            "active_tasks": STR_LIST,
            "latest_handoff": OPT_STR,
            "pending_patches": STR_LIST,
            "next_actions": STR_LIST,
            "last_updated": STR,
        }
    ),
    "IndexEntry": RecordSpec(
        {
            "anchors": STR_LIST,
            "summary": STR,
            "path": STR,
            "importance": _rule("str", enum=IMPORTANCE_LEVELS),
            "status": _rule("str", enum=RECORD_STATUSES),
            "related": STR_LIST,
            "record_snapshot_id": STR,
        }
    ),
    "MemoryIndex": RecordSpec(
        {
            "snapshot_id": STR,
            "entries": _rule("list", item=_rule("record", record="IndexEntry")),
        }
    ),
    "IndexRoute": RecordSpec(
        {
            "route_id": STR,
            "anchor_prefixes": STR_LIST,
            "path_glob": STR,
            "summary": STR,
            "status_scope": _rule(
                "list", item=_rule("str", enum=RECORD_STATUSES)
            ),
        }
    ),
    "ActiveMemoryIndex": RecordSpec(
        {
            "snapshot_id": STR,
            "entries": _rule("list", item=_rule("record", record="IndexEntry")),
            "routes": _rule("list", item=_rule("record", record="IndexRoute")),
        }
    ),
    "TopicIndex": RecordSpec(
        {
            "topic_id": STR,
            "snapshot_id": STR,
            "anchors": STR_LIST,
            "entries": _rule("list", item=_rule("record", record="IndexEntry")),
            "archive_route": _rule("str", "none"),
        }
    ),
    "AnchorRegistry": RecordSpec(
        {"aliases": STR_LIST_DICT, "deprecated": STR_DICT}
    ),
    "Decision": RecordSpec(
        {
            "id": STR,
            "anchors": STR_LIST,
            "topic": STR,
            "choice": STR,
            "status": _rule("str", enum=("tentative", "active", "deprecated")),
            "confidence": _rule("str", enum=("low", "medium", "high")),
            "scope": STR,
            "rationale": STR,
            "alternatives": STR_LIST,
            "source_refs": SOURCE_LIST,
            "review_when": _rule("str", "none"),
            "supersedes": STR_LIST,
            "fact_ids": OPT_STR_LIST,
        }
    ),
    "OpenIssue": RecordSpec(
        {
            "id": STR,
            "anchors": STR_LIST,
            "topic": STR,
            "status": _rule(
                "str", enum=("open", "blocked", "partially_resolved", "resolved")
            ),
            "question": STR,
            "current_understanding": STR,
            "next_action": _rule("str", "none"),
            "source_refs": SOURCE_LIST,
            "resolved_by": _rule("str", "none"),
            "fact_ids": OPT_STR_LIST,
            "contradiction_id": _rule("str", "none", required=False),
        }
    ),
    "Conflict": RecordSpec(
        {
            "id": STR,
            "anchors": STR_LIST,
            "topic": STR,
            "competing_positions": STR_LIST,
            "reason": STR,
            "resolution_status": _rule("str", enum=("unresolved", "resolved")),
            "next_action": _rule("str", "none"),
            "source_refs": SOURCE_LIST,
            "resolved_by": _rule("str", "none"),
            "fact_ids": OPT_STR_LIST,
            "contradiction_id": _rule("str", required=False),
        }
    ),
    "MistakeDigest": RecordSpec(
        {
            "id": STR,
            "kind": _rule(
                "str",
                enum=(
                    "wrong_assumption",
                    "omission",
                    "tool_misuse",
                    "verification_gap",
                    "regression",
                    "coordination_gap",
                    "unknown",
                ),
            ),
            "signal": STR,
            "impact": STR,
            "cause": _rule("str", "none"),
            "prevention": STR,
            "applies_when": STR_LIST,
            "source_refs": SOURCE_LIST,
            "promoted_to": _rule("str", "none"),
        }
    ),
    "Lesson": RecordSpec(
        {
            "id": STR,
            "anchors": STR_LIST,
            "title": STR,
            "status": _rule("str", enum=("candidate", "active", "deprecated")),
            "scope": STR,
            "trigger_signals": STR_LIST,
            "anti_pattern": STR,
            "prevention": STR,
            "required_checks": STR_LIST,
            "severity": _rule("str", enum=("low", "medium", "high", "critical")),
            "confidence": _rule("str", enum=("low", "medium", "high")),
            "supporting_evidence": SOURCE_LIST,
            "contradicting_evidence": SOURCE_LIST,
            "outcome_notes": STR_LIST,
            "review_when": _rule("str", "none"),
            "supersedes": STR_LIST,
            "fact_ids": OPT_STR_LIST,
        }
    ),
    "TaskRecord": RecordSpec(
        {
            "id": STR,
            "anchors": STR_LIST,
            "objective": STR,
            "status": _rule("str", enum=("active", "completed", "blocked", "archived")),
            "base_snapshot_id": STR,
            "last_updated_snapshot_id": STR,
            "created_at": STR,
            "updated_at": STR,
            "relevant_anchors": OPT_STR_LIST,
            "lesson_refs": OPT_STR_LIST,
            "required_checks": OPT_STR_LIST,
            "handoff_refs": OPT_STR_LIST,
            "outcomes": STR_LIST,
            "remaining_work": STR_LIST,
            "source_refs": SOURCE_LIST,
            "fact_ids": OPT_STR_LIST,
        }
    ),
    "HandoffCheckpoint": RecordSpec(
        {
            "profile": _rule("str", required=False, enum=("lite", "full")),
            "checkpoint_id": STR,
            "context_id": _rule("str", required=False),
            "task_id": STR,
            "status": _rule(
                "str",
                enum=(
                    "draft",
                    "ready",
                    "blocked",
                    "accepted",
                    "consumed",
                    "rejected",
                    "superseded",
                ),
            ),
            "created_at": _rule("str", required=False),
            "from_actor": _rule("str", required=False),
            "intended_receiver": _rule("str", "none", required=False),
            "task_base_snapshot_id": _rule("str", required=False),
            "source_snapshot_id": STR,
            "created_snapshot_id": _rule("str", required=False),
            "last_updated_snapshot_id": _rule("str", required=False),
            "checkpoint_snapshot_id": _rule("str", required=False),
            "predecessor_checkpoint_id": _rule("str", "none", required=False),
            "objective": STR,
            "acceptance_criteria": _rule("list", required=False, item=STR),
            "state_summary": STR,
            "completed": STR_LIST,
            "remaining": STR_LIST,
            "next_action": _rule("str", "none"),
            "artifacts": _rule("list", required=False, item=STR),
            "changed_files": STR_LIST,
            "deleted_files": _rule("list", required=False, item=STR),
            "verification": STR_LIST,
            "assumptions": _rule("list", required=False, item=STR),
            "risks": _rule("list", required=False, item=STR),
            "blockers": STR_LIST,
            "relevant_anchors": _rule("list", required=False, item=STR),
            "lesson_refs": _rule("list", required=False, item=STR),
            "required_checks": _rule("list", required=False, item=STR),
            "pending_patches": _rule("list", required=False, item=STR),
            "source_refs": _rule("list", required=False, item=_rule("record", record="SourceRef")),
            "consumed_by": _rule("str", "none", required=False),
            "fact_ids": OPT_STR_LIST,
        }
    ),
    "ArchiveRecord": RecordSpec(
        {
            "id": STR,
            "anchors": STR_LIST,
            "archived_at": STR,
            "summary": STR,
            "affected_records": STR_LIST,
            "details": STR_LIST,
            "mistakes": _rule("list", item=_rule("record", record="MistakeDigest")),
            "source_refs": SOURCE_LIST,
            "fact_ids": OPT_STR_LIST,
        }
    ),
    "ArchiveEntry": RecordSpec(
        {
            "id": STR,
            "anchors": STR_LIST,
            "summary": STR,
            "path": STR,
            "affected_records": STR_LIST,
            "mistake_refs": STR_LIST,
            "source_refs": SOURCE_LIST,
        }
    ),
    "ArchiveManifest": RecordSpec(
        {"entries": _rule("list", item=_rule("record", record="ArchiveEntry"))}
    ),
    "ContextPatch": RecordSpec(
        {
            "patch_id": STR,
            "status": _rule(
                "str",
                enum=(
                    "proposed",
                    "applying",
                    "merged",
                    "rejected",
                    "superseded",
                    "recovery_required",
                ),
            ),
            "created_at": STR,
            "base_snapshot_id": _rule("str", "none"),
            "target_snapshot_id": _rule("str", "none"),
            "affected_anchors": STR_LIST,
            "target_files": STR_LIST,
            "add_or_update_decisions": STR_LIST,
            "add_or_update_issues": STR_LIST,
            "add_or_update_conflicts": STR_LIST,
            "add_or_update_lessons": OPT_STR_LIST,
            "add_or_update_handoffs": OPT_STR_LIST,
            "project_map_updates": OPT_STR_LIST,
            "archive_targets": STR_LIST,
            "source_refs": SOURCE_LIST,
            "assumptions": STR_LIST,
            "merge_started_at": _rule("str", "none"),
            "merge_completed_at": _rule("str", "none"),
            "merge_notes": _rule("str", "none"),
        }
    ),
}


LITE_HANDOFF_REQUIRED = {
    "profile",
    "checkpoint_id",
    "task_id",
    "status",
    "objective",
    "state_summary",
    "completed",
    "remaining",
    "next_action",
    "changed_files",
    "verification",
    "blockers",
    "source_snapshot_id",
    "checkpoint_snapshot_id",
}
LITE_HANDOFF_ALLOWED = LITE_HANDOFF_REQUIRED | {"fact_ids"}
FULL_HANDOFF_REQUIRED = {
    "checkpoint_id",
    "context_id",
    "task_id",
    "status",
    "created_at",
    "from_actor",
    "intended_receiver",
    "task_base_snapshot_id",
    "source_snapshot_id",
    "created_snapshot_id",
    "last_updated_snapshot_id",
    "predecessor_checkpoint_id",
    "objective",
    "acceptance_criteria",
    "state_summary",
    "completed",
    "remaining",
    "next_action",
    "artifacts",
    "changed_files",
    "deleted_files",
    "verification",
    "assumptions",
    "risks",
    "blockers",
    "relevant_anchors",
    "lesson_refs",
    "required_checks",
    "pending_patches",
    "source_refs",
    "consumed_by",
}


def _kind(value: Any) -> str:
    if isinstance(value, ParsedCall):
        return "record"
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


def _validate_value(value: Any, rule: FieldRule, location: str) -> None:
    actual = _kind(value)
    if actual not in rule.kinds:
        expected = " | ".join(rule.kinds)
        raise RecordSyntaxError(f"{location}: expected {expected}, got {actual}")
    if rule.enum is not None and value not in rule.enum:
        choices = ", ".join(repr(item) for item in rule.enum)
        raise RecordSyntaxError(f"{location}: {value!r} is not one of {choices}")
    if actual == "record" and rule.record and value.name != rule.record:
        raise RecordSyntaxError(f"{location}: expected {rule.record}, got {value.name}")
    if actual == "list" and rule.item:
        for index, item in enumerate(value):
            _validate_value(item, rule.item, f"{location}[{index}]")
    if actual == "dict":
        for key, item in value.items():
            if not isinstance(key, str):
                raise RecordSyntaxError(f"{location}: dictionary keys must be strings")
            if rule.value:
                _validate_value(item, rule.value, f"{location}[{key!r}]")


def _convert(node: ast.AST, source: str) -> Any:
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, bool, type(None))):
        return node.value
    if isinstance(node, ast.List):
        return [_convert(item, source) for item in node.elts]
    if isinstance(node, ast.Dict):
        if any(key is None for key in node.keys):
            raise RecordSyntaxError(f"{source}:{node.lineno}: dictionary unpacking is not allowed")
        result = {}
        for key, value in zip(node.keys, node.values):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                raise RecordSyntaxError(f"{source}:{node.lineno}: dictionary keys must be strings")
            if key.value in result:
                raise RecordSyntaxError(f"{source}:{node.lineno}: duplicate dictionary key")
            result[key.value] = _convert(value, source)
        return result
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        operand = _convert(node.operand, source)
        if isinstance(operand, bool) or not isinstance(operand, int):
            raise RecordSyntaxError(f"{source}:{node.lineno}: unary operators require an integer")
        return -operand if isinstance(node.op, ast.USub) else operand
    if isinstance(node, ast.Call):
        return _parse_call(node, source)
    raise RecordSyntaxError(
        f"{source}:{getattr(node, 'lineno', '?')}: disallowed syntax {type(node).__name__}"
    )


def _parse_call(node: ast.Call, source: str) -> ParsedCall:
    if not isinstance(node.func, ast.Name):
        raise RecordSyntaxError(f"{source}:{node.lineno}: only direct schema calls are allowed")
    name = node.func.id
    spec = SCHEMAS.get(name)
    if spec is None:
        raise RecordSyntaxError(f"{source}:{node.lineno}: unknown schema call {name}")
    if node.args:
        raise RecordSyntaxError(f"{source}:{node.lineno}: positional arguments are not allowed")
    fields: dict[str, Any] = {}
    for keyword in node.keywords:
        if keyword.arg is None:
            raise RecordSyntaxError(f"{source}:{node.lineno}: keyword unpacking is not allowed")
        if keyword.arg in fields:
            raise RecordSyntaxError(f"{source}:{node.lineno}: duplicate field {keyword.arg}")
        if keyword.arg not in spec.fields:
            raise RecordSyntaxError(f"{source}:{node.lineno}: unknown field {name}.{keyword.arg}")
        fields[keyword.arg] = _convert(keyword.value, source)

    required = {field for field, rule in spec.fields.items() if rule.required}
    if name == "HandoffCheckpoint":
        profile = fields.get("profile", "full")
        if profile == "lite":
            required = LITE_HANDOFF_REQUIRED
            extra = set(fields) - LITE_HANDOFF_ALLOWED
            if extra:
                raise RecordSyntaxError(
                    f"{source}:{node.lineno}: lite handoff contains full-only fields: {', '.join(sorted(extra))}"
                )
        else:
            required = FULL_HANDOFF_REQUIRED
    missing = required - set(fields)
    if missing:
        raise RecordSyntaxError(
            f"{source}:{node.lineno}: {name} missing fields: {', '.join(sorted(missing))}"
        )
    for field, value in fields.items():
        _validate_value(value, spec.fields[field], f"{source}:{node.lineno}:{name}.{field}")
    return ParsedCall(name=name, fields=fields, line=node.lineno)


def parse_python_block(block: str, source: str = "<memory>") -> ParsedCall:
    """Parse exactly one safe schema call from a Python-like fenced block."""
    try:
        tree = ast.parse(block, filename=source, mode="exec")
    except SyntaxError as exc:
        raise RecordSyntaxError(f"{source}:{exc.lineno}: invalid structured syntax: {exc.msg}") from exc
    statements = [item for item in tree.body if not isinstance(item, ast.Pass)]
    if len(statements) != 1 or not isinstance(statements[0], ast.Expr):
        raise RecordSyntaxError(f"{source}: block must contain exactly one schema call")
    value = statements[0].value
    if not isinstance(value, ast.Call):
        raise RecordSyntaxError(f"{source}:{statements[0].lineno}: block must contain a schema call")
    return _parse_call(value, source)


def parse_markdown(text: str, source: str = "<memory>") -> list[ParsedCall]:
    """Parse every Python fenced block in Markdown without executing it."""
    blocks = PYTHON_FENCE_RE.findall(text)
    if not blocks:
        raise RecordSyntaxError(f"{source}: no Python structured block found")
    return [parse_python_block(block, source) for block in blocks]


DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": 3,
    "record_mode": "standard",
    "budgets": {
        "current": {"target": 450, "hard": 600},
        "root_index": {"target": 1500, "hard": 2000},
        "topic_index": {"target": 1500, "hard": 2000},
    },
    "tokenizer": {
        "encoding": "o200k_base",
        "fallback": "max_utf8_bytes_or_chars_div_3",
    },
    "retrieval": {"normal_top_k": 3, "expanded_top_k": 8},
}


def validate_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ConfigError("config root must be an object")
    expected = {"schema_version", "record_mode", "budgets", "tokenizer", "retrieval"}
    unknown = set(config) - expected
    missing = expected - set(config)
    if unknown:
        raise ConfigError(f"unknown config fields: {', '.join(sorted(unknown))}")
    if missing:
        raise ConfigError(f"missing config fields: {', '.join(sorted(missing))}")
    if config["schema_version"] != 3:
        raise ConfigError("schema_version must be 3")
    if config["record_mode"] not in RECORD_MODES:
        raise ConfigError("record_mode must be minimal, standard, detailed, or forensic")

    budgets = config["budgets"]
    if not isinstance(budgets, dict) or set(budgets) != {"current", "root_index", "topic_index"}:
        raise ConfigError("budgets must contain exactly current, root_index, and topic_index")
    for name, values in budgets.items():
        if not isinstance(values, dict) or set(values) != {"target", "hard"}:
            raise ConfigError(f"budgets.{name} must contain exactly target and hard")
        target, hard = values["target"], values["hard"]
        if isinstance(target, bool) or isinstance(hard, bool) or not isinstance(target, int) or not isinstance(hard, int):
            raise ConfigError(f"budgets.{name} values must be integers")
        if target < 1 or hard < target:
            raise ConfigError(f"budgets.{name} must satisfy 1 <= target <= hard")

    tokenizer = config["tokenizer"]
    if not isinstance(tokenizer, dict) or set(tokenizer) != {"encoding", "fallback"}:
        raise ConfigError("tokenizer must contain exactly encoding and fallback")
    if tokenizer["encoding"] != "o200k_base":
        raise ConfigError("tokenizer.encoding must be o200k_base")
    if tokenizer["fallback"] != "max_utf8_bytes_or_chars_div_3":
        raise ConfigError("unknown tokenizer fallback")

    retrieval = config["retrieval"]
    if not isinstance(retrieval, dict) or set(retrieval) != {"normal_top_k", "expanded_top_k"}:
        raise ConfigError("retrieval must contain exactly normal_top_k and expanded_top_k")
    normal, expanded = retrieval["normal_top_k"], retrieval["expanded_top_k"]
    if isinstance(normal, bool) or isinstance(expanded, bool) or not isinstance(normal, int) or not isinstance(expanded, int):
        raise ConfigError("retrieval top-k values must be integers")
    if normal < 1 or expanded < normal:
        raise ConfigError("retrieval must satisfy 1 <= normal_top_k <= expanded_top_k")
    return config


def load_config(root: Path) -> tuple[dict[str, Any], int]:
    path = root / "config.json"
    if not path.is_file():
        index = root / "index.md"
        if index.is_file():
            try:
                calls = parse_markdown(index.read_text(encoding="utf-8"), str(index))
            except RecordSyntaxError:
                calls = []  # The validator reports the original syntax error.
            if any(call.name == "ActiveMemoryIndex" for call in calls):
                raise ConfigError("missing config.json for ActiveMemoryIndex (incomplete v3 root)")
        legacy = json.loads(json.dumps(DEFAULT_CONFIG))
        legacy["schema_version"] = 2
        return legacy, 2
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot parse config.json: {exc}") from exc
    return validate_config(config), 3


def config_json() -> str:
    return json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


@lru_cache(maxsize=1)
def _tokenizer(encoding: str):
    try:
        import tiktoken
        return tiktoken.get_encoding(encoding)
    except (ImportError, OSError, ValueError):
        # The package may be installed while its encoding data is unavailable.
        return None


def count_tokens(text: str, encoding: str = "o200k_base", require_exact: bool = False) -> TokenMeasurement:
    tokenizer = _tokenizer(encoding)
    if tokenizer is None:
        if require_exact:
            raise TokenizerUnavailable(f"tiktoken and cached {encoding} data are required for exact counting")
        utf8_estimate = math.ceil(len(text.encode("utf-8")) / 3)
        char_estimate = math.ceil(len(text) / 3)
        return TokenMeasurement(
            tokens=max(utf8_estimate, char_estimate),
            encoding=encoding,
            exact=False,
            method="max_utf8_bytes_or_chars_div_3",
        )
    return TokenMeasurement(
        tokens=len(tokenizer.encode_ordinary(text)),
        encoding=encoding,
        exact=True,
        method="tiktoken",
    )


def record_to_data(value: ParsedCall) -> dict[str, Any]:
    """Convert a parsed call to JSON-compatible data for reports and tests."""
    def convert(item: Any) -> Any:
        if isinstance(item, ParsedCall):
            return {"__type__": item.name, **{key: convert(val) for key, val in item.fields.items()}}
        if isinstance(item, list):
            return [convert(entry) for entry in item]
        if isinstance(item, dict):
            return {key: convert(entry) for key, entry in item.items()}
        return item

    return convert(value)
