from __future__ import annotations

import argparse
import ast
import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.contracts import (  # noqa: E402
    ABSOLUTE_GATES,
    CORRECTNESS_METRICS,
    MAX_CONCURRENCY,
    PINNED_V2_SHA,
    RATE_METRICS,
    RESULT_SCHEMA_VERSION,
    TOKENIZER_NAME,
    ContractError,
    exact_loaded_file_tokens,
    load_json,
    nearest_rank,
    normalize_relative_path,
    rate_entry,
    require_bool,
    require_exact_tokenizer,
    require_string_list,
    write_json_result,
)


DEFAULT_REPETITIONS = 5
DEFAULT_CONCURRENCY = 4
DEFAULT_TIMEOUT_SECONDS = 900
WORKTREE_REF = "WORKTREE"
DEFAULT_MODEL = "gpt-5.6-luna"
RUNNER_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
EXPECTED_CATEGORIES = {
    "resume",
    "decision_fact",
    "contradiction",
    "lesson",
    "abstention_irrelevant",
}
RUNTIME_PREFIXES = ("agents/", "assets/", "references/", "scripts/")
AUTH_PATTERNS = ("401", "403", "unauthorized", "authentication", "not logged in", "api key")
QUOTA_PATTERNS = ("usage limit", "purchase more credits", "try again at")
NETWORK_PATTERNS = (
    "connection refused",
    "connection reset",
    "dns",
    "network is unreachable",
    "name resolution",
    "tls",
    "timed out connecting",
)
EXPLICIT_RUNNER_FAILURE_CODES = {
    "auth_failure",
    "network_failure",
    "quota_failure",
    "runner_failure",
}
INFRASTRUCTURE_FAILURE_CODES = EXPLICIT_RUNNER_FAILURE_CODES | {
    "runner_timeout",
    "runner_protocol_failure",
}


def _git(repo_root: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=text,
        encoding="utf-8" if text else None,
        errors="replace" if text else None,
        check=False,
    )


def resolve_commit(repo_root: Path, ref: str) -> str:
    completed = _git(repo_root, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if completed.returncode != 0:
        raise ContractError(
            "git_ref_unavailable",
            f"Git ref does not resolve to a commit: {ref}",
            stderr=completed.stderr[-2000:],
        )
    commit = completed.stdout.strip().lower()
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise ContractError("git_ref_invalid", f"Git returned an invalid commit for {ref}", actual=commit)
    return commit


def _runtime_files(repo_root: Path, commit: str) -> list[str]:
    completed = _git(repo_root, "ls-tree", "-r", "-z", "--name-only", commit, text=False)
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace") if completed.stderr else ""
        raise ContractError("git_tree_unavailable", f"Could not list commit {commit}", stderr=stderr[-2000:])
    names = completed.stdout.decode("utf-8").split("\0")
    runtime = [
        name
        for name in names
        if name == "SKILL.md" or any(name.startswith(prefix) for prefix in RUNTIME_PREFIXES)
    ]
    if "SKILL.md" not in runtime:
        raise ContractError("skill_entrypoint_missing", f"Commit {commit} has no SKILL.md")
    return sorted(runtime)


def _worktree_runtime_files(repo_root: Path) -> list[str]:
    files: list[str] = []
    candidates = [repo_root / "SKILL.md"]
    for prefix in RUNTIME_PREFIXES:
        directory = repo_root / prefix.rstrip("/")
        if directory.is_dir():
            candidates.extend(path for path in directory.rglob("*") if path.is_file())
    for path in candidates:
        relative = path.relative_to(repo_root).as_posix()
        if "__pycache__" in path.parts or path.suffix.lower() in {".pyc", ".pyo"}:
            continue
        normalized = normalize_relative_path(
            relative,
            allowed_prefixes=("SKILL.md", "agents", "assets", "references", "scripts"),
        )
        resolved = path.resolve()
        try:
            resolved.relative_to(repo_root.resolve())
        except ValueError as exc:
            raise ContractError("unsafe_runtime_file", f"Runtime file escapes repository: {relative}") from exc
        files.append(normalized)
    if "SKILL.md" not in files:
        raise ContractError("skill_entrypoint_missing", "Working tree has no SKILL.md")
    return sorted(set(files))


def _commit_blob(repo_root: Path, commit: str, name: str) -> bytes:
    completed = _git(repo_root, "show", f"{commit}:{name}", text=False)
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace") if completed.stderr else ""
        raise ContractError(
            "git_blob_unavailable",
            f"Could not read {name} from {commit}",
            stderr=stderr[-2000:],
        )
    return completed.stdout


def _runtime_fingerprint(files: Sequence[str], read_bytes: Any) -> str:
    digest = hashlib.sha256()
    for name in files:
        payload = read_bytes(name)
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).digest())
        digest.update(b"\0")
    return digest.hexdigest()


def runtime_identity(repo_root: Path, source: str) -> str:
    if source.upper() == WORKTREE_REF:
        files = _worktree_runtime_files(repo_root)
        fingerprint = _runtime_fingerprint(files, lambda name: (repo_root / PurePosixPath(name)).read_bytes())
        return f"{WORKTREE_REF}:{fingerprint}"
    commit = resolve_commit(repo_root, source)
    files = _runtime_files(repo_root, commit)
    fingerprint = _runtime_fingerprint(files, lambda name: _commit_blob(repo_root, commit, name))
    return f"{commit}:{fingerprint}"


def materialize_runtime(repo_root: Path, source: str, destination: Path) -> None:
    is_worktree = source.upper() == WORKTREE_REF
    commit = None if is_worktree else resolve_commit(repo_root, source)
    names = _worktree_runtime_files(repo_root) if is_worktree else _runtime_files(repo_root, commit or source)
    for name in names:
        normalized = normalize_relative_path(
            name,
            allowed_prefixes=("SKILL.md", "agents", "assets", "references", "scripts"),
        )
        payload = (
            (repo_root / PurePosixPath(normalized)).read_bytes()
            if is_worktree
            else _commit_blob(repo_root, commit or source, normalized)
        )
        target = destination / PurePosixPath(normalized)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)


def load_tasks(path: Path) -> list[dict[str, Any]]:
    document = load_json(path)
    if not isinstance(document, dict) or document.get("schema_version") != "1.0.0":
        raise ContractError("invalid_task_spec", "Task spec must be a schema_version 1.0.0 object")
    tasks = document.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ContractError("invalid_task_spec", "Task spec must contain a non-empty tasks array")
    seen_ids: set[str] = set()
    categories: Counter[str] = Counter()
    validated: list[dict[str, Any]] = []
    for index, raw_task in enumerate(tasks):
        if not isinstance(raw_task, dict):
            raise ContractError("invalid_task_spec", f"tasks[{index}] must be an object")
        task_id = raw_task.get("id")
        category = raw_task.get("category")
        query = raw_task.get("query")
        records = raw_task.get("records")
        expected = raw_task.get("expected")
        if not isinstance(task_id, str) or not task_id or task_id in seen_ids:
            raise ContractError("invalid_task_spec", f"Task id is missing or duplicated at index {index}")
        if category not in EXPECTED_CATEGORIES:
            raise ContractError("invalid_task_spec", f"Task {task_id} has unknown category: {category}")
        if not isinstance(query, str) or not query.strip():
            raise ContractError("invalid_task_spec", f"Task {task_id} has an empty query")
        if not isinstance(records, list) or not records:
            raise ContractError("invalid_task_spec", f"Task {task_id} has no records")
        if not isinstance(expected, dict):
            raise ContractError("invalid_task_spec", f"Task {task_id} has no expected object")
        for record in records:
            if not isinstance(record, dict):
                raise ContractError("invalid_task_spec", f"Task {task_id} has a non-object record")
            for field in ("path", "anchor", "kind", "relevance", "body"):
                if not isinstance(record.get(field), str) or not record[field]:
                    raise ContractError("invalid_task_spec", f"Task {task_id} record field {field} is invalid")
            normalize_relative_path(record["path"], allowed_prefixes=("handoffs", "decisions", "issues", "conflicts", "lessons", "tasks"))
            if record["relevance"] not in {"required", "irrelevant"}:
                raise ContractError("invalid_task_spec", f"Task {task_id} has invalid record relevance")
        required_records = expected.get("required_records")
        required_facts = expected.get("required_facts")
        if not isinstance(required_records, list) or any(not isinstance(item, str) for item in required_records):
            raise ContractError("invalid_task_spec", f"Task {task_id} expected.required_records is invalid")
        if len(required_records) > 3:
            raise ContractError("invalid_task_spec", f"Task {task_id} needs more than Recall@3 can return")
        if not isinstance(required_facts, list) or any(not isinstance(item, str) for item in required_facts):
            raise ContractError("invalid_task_spec", f"Task {task_id} expected.required_facts is invalid")
        for field in ("decision", "contradiction", "next_action"):
            if expected.get(field) is not None and not isinstance(expected.get(field), str):
                raise ContractError("invalid_task_spec", f"Task {task_id} expected.{field} is invalid")
        for field in ("resume_success", "abstained"):
            if expected.get(field) is not None and type(expected.get(field)) is not bool:
                raise ContractError("invalid_task_spec", f"Task {task_id} expected.{field} is invalid")
        seen_ids.add(task_id)
        categories[category] += 1
        validated.append(raw_task)

    if len(validated) != 20 or categories != Counter({category: 4 for category in EXPECTED_CATEGORIES}):
        raise ContractError(
            "invalid_task_matrix",
            "Default paired evaluation requires exactly 20 tasks, four in each category",
            task_count=len(validated),
            category_counts=dict(categories),
        )
    return validated


def collect_preflight(
    repo_root: Path,
    tasks_path: Path,
    v2_ref: str,
    v3_ref: str,
    codex_binary: str,
    repetitions: int,
    concurrency: int,
    output_schema: Path,
    model: str | None,
    runner_sandbox: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]] | None, Any | None]:
    failures: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] | None = None
    encoding: Any | None = None
    resolved: dict[str, Any] = {
        "repo_root": str(repo_root),
        "tasks_path": str(tasks_path),
        "v2_ref": v2_ref,
        "v3_ref": v3_ref,
        "repetitions": repetitions,
        "concurrency": concurrency,
        "tokenizer": TOKENIZER_NAME,
        "model": model,
        "runner_sandbox": runner_sandbox,
        "approval_policy": "never",
        "ignore_user_config": True,
        "python_bytecode_disabled": True,
    }
    if repetitions <= 0 or repetitions > 20:
        failures.append({"code": "invalid_repetitions", "message": "repetitions must be between 1 and 20"})
    if concurrency <= 0 or concurrency > MAX_CONCURRENCY:
        failures.append(
            {
                "code": "invalid_concurrency",
                "message": f"concurrency must be between 1 and {MAX_CONCURRENCY}",
            }
        )
    if runner_sandbox not in RUNNER_SANDBOXES:
        failures.append(
            {
                "code": "invalid_runner_sandbox",
                "message": f"runner-sandbox must be one of: {', '.join(RUNNER_SANDBOXES)}",
            }
        )
    if model is not None and not model.strip():
        failures.append({"code": "invalid_model", "message": "model must be non-empty when provided"})
    if not repo_root.is_dir():
        failures.append({"code": "missing_repo_root", "message": f"Repository root does not exist: {repo_root}"})
    else:
        try:
            v2_commit = resolve_commit(repo_root, v2_ref)
            resolved["v2_commit"] = v2_commit
            resolved["v2_source"] = v2_commit
            if v2_commit != PINNED_V2_SHA:
                failures.append(
                    {
                        "code": "v2_pin_mismatch",
                        "message": "v2 ref does not resolve to the required pinned commit",
                        "expected": PINNED_V2_SHA,
                        "actual": v2_commit,
                    }
                )
            resolved["v2_runtime_id"] = runtime_identity(repo_root, v2_commit)
        except ContractError as exc:
            failures.append(exc.as_dict())
        try:
            if v3_ref.upper() == WORKTREE_REF:
                v3_source = WORKTREE_REF
                resolved["v3_commit"] = None
            else:
                v3_source = resolve_commit(repo_root, v3_ref)
                resolved["v3_commit"] = v3_source
            resolved["v3_source"] = v3_source
            resolved["v3_runtime_id"] = runtime_identity(repo_root, v3_source)
            if v3_source == PINNED_V2_SHA:
                failures.append(
                    {
                        "code": "v3_candidate_is_v2",
                        "message": "v3 candidate resolves to the pinned v2 commit",
                        "actual": v3_source,
                    }
                )
            v2_runtime_id = resolved.get("v2_runtime_id")
            if (
                isinstance(v2_runtime_id, str)
                and resolved["v3_runtime_id"].rsplit(":", 1)[-1]
                == v2_runtime_id.rsplit(":", 1)[-1]
            ):
                failures.append(
                    {
                        "code": "v3_candidate_is_v2",
                        "message": "v3 candidate runtime is byte-identical to pinned v2",
                        "actual": resolved["v3_runtime_id"],
                    }
                )
        except ContractError as exc:
            failures.append(exc.as_dict())
    try:
        tasks = load_tasks(tasks_path)
        resolved["task_count"] = len(tasks)
        resolved["total_runs"] = len(tasks) * repetitions * 2
    except ContractError as exc:
        failures.append(exc.as_dict())
    try:
        encoding = require_exact_tokenizer()
    except ContractError as exc:
        failures.append(exc.as_dict())
    codex_path = shutil.which(codex_binary)
    if codex_path is None:
        failures.append(
            {
                "code": "runner_missing",
                "message": f"Codex executable was not found: {codex_binary}",
            }
        )
    else:
        resolved["codex_binary"] = codex_path
        try:
            version = subprocess.run(
                [codex_path, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(
                {
                    "code": "runner_version_failure",
                    "message": f"Could not determine Codex CLI version: {exc}",
                }
            )
        else:
            if version.returncode != 0 or not version.stdout.strip():
                failures.append(
                    {
                        "code": "runner_version_failure",
                        "message": "Could not determine Codex CLI version",
                        "stderr": version.stderr[-2000:],
                    }
                )
            else:
                resolved["codex_cli_version"] = version.stdout.strip()
    if not output_schema.is_file():
        failures.append(
            {
                "code": "output_schema_missing",
                "message": f"Live agent output schema is missing: {output_schema}",
            }
        )
    if (
        tasks
        and isinstance(resolved.get("v2_source"), str)
        and isinstance(resolved.get("v3_source"), str)
    ):
        try:
            with tempfile.TemporaryDirectory(prefix="mlc-live-preflight-") as temporary:
                temporary_root = Path(temporary)
                validated_arms: list[str] = []
                for arm in ("v2", "v3"):
                    runtime_root = temporary_root / f"runtime-{arm}"
                    project_root = temporary_root / f"project-{arm}"
                    materialize_runtime(repo_root, resolved[f"{arm}_source"], runtime_root)
                    build_project_fixture(project_root, tasks[0], arm)
                    validate_project_fixture(runtime_root, project_root, arm)
                    validated_arms.append(arm)
                resolved["fixture_validation"] = validated_arms
        except ContractError as exc:
            failures.append(exc.as_dict())
    return resolved, failures, tasks, encoding


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8", newline="\n")


def _python_literal(value: Any) -> str:
    if value is None:
        return "None"
    if type(value) is bool:
        return "True" if value else "False"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_python_literal(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(
            f"{_python_literal(key)}: {_python_literal(item)}" for key, item in value.items()
        ) + "}"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    raise TypeError(f"Unsupported structured value: {type(value).__name__}")


def _structured_record(name: str, fields: Mapping[str, Any]) -> str:
    lines = [f"{name}("]
    lines.extend(f"    {field}={_python_literal(value)}," for field, value in fields.items())
    lines.append(")")
    return "```python\n" + "\n".join(lines) + "\n```"


def _task_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    body = str(record["body"])
    try:
        expression = ast.parse(body, mode="eval").body
    except SyntaxError as exc:
        raise ContractError("invalid_task_spec", f"Record body is not a safe call: {body}") from exc
    if not isinstance(expression, ast.Call) or not isinstance(expression.func, ast.Name):
        raise ContractError("invalid_task_spec", f"Record body must be one named call: {body}")
    if expression.args:
        raise ContractError("invalid_task_spec", f"Record body may not use positional arguments: {body}")
    payload: dict[str, Any] = {}
    for keyword in expression.keywords:
        if keyword.arg is None or keyword.arg in payload:
            raise ContractError("invalid_task_spec", f"Record body has unsafe or duplicate fields: {body}")
        try:
            payload[keyword.arg] = ast.literal_eval(keyword.value)
        except (ValueError, TypeError) as exc:
            raise ContractError("invalid_task_spec", f"Record body field is not literal: {body}") from exc
    return payload


def _record_fields(
    record: Mapping[str, Any],
    task: Mapping[str, Any],
    snapshot: str,
    arm: str,
) -> tuple[str, dict[str, Any]]:
    payload = _task_payload(record)
    relative = PurePosixPath(str(record["path"]))
    record_id = relative.stem
    anchor = f"concept:{record['anchor']}"
    fact = str(payload.get("fact", f"fixture_fact_{record_id.lower()}"))
    timestamp = "2026-08-30T00:00:00+09:00"
    kind = str(record["kind"])
    if kind == "decision":
        fields = {
            "id": record_id,
            "anchors": [anchor],
            "topic": str(record["anchor"]),
            "choice": str(payload.get("decision", f"fixture_choice_{record_id.lower()}")),
            "status": "active",
            "confidence": "high",
            "scope": "paired live evaluation fixture",
            "rationale": f"Stable fact identifier: {fact}",
            "alternatives": [],
            "source_refs": [],
            "review_when": None,
            "supersedes": [],
        }
        if arm == "v3":
            fields["fact_ids"] = [fact]
        return "Decision", fields
    if kind == "issue":
        contradiction = payload.get("contradiction")
        understanding = f"Stable fact identifier: {fact}"
        if contradiction is not None:
            understanding += f"; unresolved contradiction identifier: {contradiction}"
        fields = {
            "id": record_id,
            "anchors": [anchor],
            "topic": str(record["anchor"]),
            "status": "open",
            "question": f"Resolve the active issue for {record['anchor']}",
            "current_understanding": understanding,
            "next_action": payload.get("next_action"),
            "source_refs": [],
            "resolved_by": None,
        }
        if arm == "v3":
            fields["fact_ids"] = [fact]
            fields["contradiction_id"] = (
                str(contradiction) if contradiction is not None else None
            )
        return "OpenIssue", fields
    if kind == "conflict":
        contradiction = str(payload.get("contradiction", f"fixture_conflict_{record_id.lower()}"))
        fields = {
            "id": record_id,
            "anchors": [anchor],
            "topic": str(record["anchor"]),
            "competing_positions": [fact, "The active source states a different value"],
            "reason": f"Unresolved contradiction identifier: {contradiction}",
            "resolution_status": "unresolved",
            "next_action": None,
            "source_refs": [],
            "resolved_by": None,
        }
        if arm == "v3":
            fields["fact_ids"] = [fact]
            fields["contradiction_id"] = contradiction
        return "Conflict", fields
    if kind == "lesson":
        required_check = str(payload.get("required_check", f"verify_{record_id.lower()}"))
        fields = {
            "id": record_id,
            "anchors": [anchor],
            "title": f"Prevention for {record['anchor']}",
            "status": "active",
            "scope": "paired live evaluation fixture",
            "trigger_signals": [str(payload.get("trigger", record["anchor"]))],
            "anti_pattern": f"Observed stable fact identifier: {fact}",
            "prevention": required_check,
            "required_checks": [required_check],
            "severity": "high",
            "confidence": "high",
            "supporting_evidence": [],
            "contradicting_evidence": [],
            "outcome_notes": [],
            "review_when": None,
            "supersedes": [],
        }
        if arm == "v3":
            fields["fact_ids"] = [fact]
        return "Lesson", fields
    if kind == "task":
        next_action = payload.get("next_action")
        fields = {
            "id": record_id,
            "anchors": [anchor],
            "objective": f"Evaluation task for {record['anchor']}",
            "status": "active",
            "base_snapshot_id": snapshot,
            "last_updated_snapshot_id": snapshot,
            "created_at": timestamp,
            "updated_at": timestamp,
            "relevant_anchors": [anchor],
            "lesson_refs": [],
            "required_checks": [],
            "handoff_refs": [],
            "outcomes": [fact],
            "remaining_work": [str(next_action)] if next_action is not None else [],
            "source_refs": [],
        }
        if arm == "v3":
            fields["fact_ids"] = [fact]
        return "TaskRecord", fields
    if kind == "handoff":
        next_action = payload.get("next_action")
        fields = {
            "checkpoint_id": record_id,
            "context_id": f"eval:{task['id']}",
            "task_id": str(task["id"]),
            "status": "accepted",
            "created_at": timestamp,
            "from_actor": "paired-eval-fixture",
            "intended_receiver": None,
            "task_base_snapshot_id": snapshot,
            "source_snapshot_id": snapshot,
            "created_snapshot_id": snapshot,
            "last_updated_snapshot_id": snapshot,
            "checkpoint_snapshot_id": snapshot,
            "predecessor_checkpoint_id": None,
            "objective": str(task["query"]),
            "acceptance_criteria": ["Resume from the named next action"],
            "state_summary": f"Stable fact identifier: {fact}",
            "completed": [str(item) for item in payload.get("completed", [])],
            "remaining": [str(item) for item in payload.get("remaining", [])],
            "next_action": str(next_action) if next_action is not None else None,
            "artifacts": [],
            "changed_files": [],
            "deleted_files": [],
            "verification": ["Fixture schema validation passed before evaluation"],
            "assumptions": [],
            "risks": [],
            "blockers": [],
            "relevant_anchors": [anchor],
            "lesson_refs": [],
            "required_checks": [],
            "pending_patches": [],
            "source_refs": [],
            "consumed_by": None,
        }
        if arm == "v3":
            fields["fact_ids"] = [fact]
        return "HandoffCheckpoint", fields
    raise ContractError("invalid_task_spec", f"Unsupported record kind: {kind}")


IMPORTANCE_BY_KIND = {
    "decision": "L1_active_decision",
    "lesson": "L1_active_lesson",
    "issue": "L2_active_issue",
    "conflict": "L2_active_issue",
    "handoff": "L2_active_handoff",
    "task": "L3_working_note",
}


def build_project_fixture(project_root: Path, task: Mapping[str, Any], arm: str) -> None:
    memory = project_root / "memory"
    digest = hashlib.sha256(str(task["id"]).encode("utf-8")).hexdigest()[:12].upper()
    snapshot = f"SNAP-EVAL-{digest}"
    records = task["records"]
    entries: list[str] = []
    anchors: list[str] = []
    for record in records:
        relative = normalize_relative_path(
            record["path"],
            allowed_prefixes=("handoffs", "decisions", "issues", "conflicts", "lessons", "tasks"),
        )
        anchor = f"concept:{record['anchor']}"
        anchors.append(anchor)
        record_name, record_fields = _record_fields(record, task, snapshot, arm)
        _write_text(memory / PurePosixPath(relative), _structured_record(record_name, record_fields))
        summary = f"{record['kind']} record for {record['anchor']}"
        entries.append(
            "        IndexEntry("
            f"anchors={_python_literal([anchor])}, "
            f"summary={_python_literal(summary)}, "
            f"path={_python_literal(relative)}, "
            f"importance={_python_literal(IMPORTANCE_BY_KIND[str(record['kind'])])}, "
            'status="active", related=[], '
            f'record_snapshot_id="{snapshot}"),'
        )

    _write_text(
        memory / "current.md",
        _structured_record(
            "ProjectState",
            {
                "snapshot_id": snapshot,
                "project_identity": "paired-live-eval",
                "record_mode": "standard",
                "active_goal": str(task["query"]),
                "invariants": ["Use only authoritative records and preserve missing facts as absent"],
                "active_decisions": [anchor for anchor, record in zip(anchors, records) if record["kind"] == "decision"],
                "active_lessons": [anchor for anchor, record in zip(anchors, records) if record["kind"] == "lesson"],
                "open_issues": [anchor for anchor, record in zip(anchors, records) if record["kind"] == "issue"],
                "unresolved_conflicts": [anchor for anchor, record in zip(anchors, records) if record["kind"] == "conflict"],
                "active_tasks": [anchor for anchor, record in zip(anchors, records) if record["kind"] == "task"],
                "latest_handoff": next((anchor for anchor, record in zip(anchors, records) if record["kind"] == "handoff"), None),
                "pending_patches": [],
                "next_actions": ["Retrieve only records relevant to the query"],
                "last_updated": "2026-08-30T00:00:00+09:00",
            },
        ),
    )
    _write_text(
        memory / "project.md",
        _structured_record(
            "ProjectMap",
            {
                "project_id": "paired-live-eval",
                "name": "Paired live evaluation fixture",
                "purpose": "Compare pinned v2 and candidate v3 retrieval",
                "project_root": "..",
                "memory_root": "memory",
                "boundaries": ["Read-only isolated fixture"],
                "components": {"component:evaluation": "memory"},
                "entry_points": ["memory/current.md", "memory/index.md"],
                "authoritative_sources": ["memory detail records"],
                "generated_or_disposable": [],
                "navigation_notes": [
                    "Open ranked detail candidates one at a time and stop when sufficient"
                ],
            },
        ),
    )
    _write_text(
        memory / "anchors.md",
        _structured_record(
            "AnchorRegistry",
            {
                "aliases": {anchor: [str(record["anchor"])] for anchor, record in zip(anchors, records)},
                "deprecated": {},
            },
        ),
    )
    for directory in ("decisions", "issues", "conflicts", "lessons", "tasks", "handoffs", "patches", "archive"):
        (memory / directory).mkdir(parents=True, exist_ok=True)
    _write_text(memory / "archive" / "manifest.md", _structured_record("ArchiveManifest", {"entries": []}))

    if arm == "v2":
        _write_text(
            memory / "index.md",
            "```python\n"
            + "\n".join(
                ["MemoryIndex(", f'    snapshot_id="{snapshot}",', "    entries=[", *entries, "    ],", ")"]
            )
            + "\n```",
        )
    elif arm == "v3":
        _write_text(
            memory / "config.json",
            json.dumps(
                {
                    "schema_version": 3,
                    "record_mode": "standard",
                    "budgets": {
                        "current": {"target": 450, "hard": 600},
                        "root_index": {"target": 1500, "hard": 2000},
                        "topic_index": {"target": 1500, "hard": 2000},
                    },
                    "tokenizer": {
                        "encoding": TOKENIZER_NAME,
                        "fallback": "max_utf8_bytes_or_chars_div_3",
                    },
                    "retrieval": {"normal_top_k": 3, "expanded_top_k": 8},
                },
                indent=2,
                sort_keys=True,
            ),
        )
        _write_text(
            memory / "indexes" / "eval-topic.md",
            "```python\n"
            + "\n".join(
                [
                    "TopicIndex(",
                    '    topic_id="eval-topic",',
                    f'    snapshot_id="{snapshot}",',
                    f"    anchors={_python_literal(anchors)},",
                    "    entries=[",
                    *entries,
                    "    ],",
                    '    archive_route="archive/manifest.md",',
                    ")",
                ]
            )
            + "\n```",
        )
        _write_text(
            memory / "index.md",
            "```python\n"
            + "\n".join(
                [
                    "ActiveMemoryIndex(",
                    f'    snapshot_id="{snapshot}",',
                    "    entries=[],",
                    "    routes=[",
                    "        IndexRoute("
                    f"route_id=\"eval-topic\", anchor_prefixes={_python_literal(anchors)}, "
                    'path_glob="indexes/eval-topic.md", summary="Task-scoped active records", '
                    'status_scope=["active"]),',
                    "    ],",
                    ")",
                ]
            )
            + "\n```",
        )
    else:
        raise ValueError(f"Unknown arm: {arm}")


def validate_project_fixture(runtime_root: Path, project_root: Path, arm: str) -> None:
    memory_root = project_root / "memory"
    if arm == "v3":
        script = runtime_root / "scripts" / "memoryctl.py"
        command = [sys.executable, str(script), "validate", str(memory_root), "--strict-v3", "--json"]
    elif arm == "v2":
        script = runtime_root / "scripts" / "validate_memory.py"
        command = [sys.executable, str(script), str(memory_root), "--strict-v2", "--json"]
    else:
        raise ValueError(f"Unknown arm: {arm}")
    if not script.is_file():
        raise ContractError("fixture_validator_missing", f"{arm} validator is missing: {script}")
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise ContractError(
            "fixture_validation_failed",
            f"{arm} fixture did not validate with its own runtime",
            returncode=completed.returncode,
            stdout=completed.stdout[-4000:],
            stderr=completed.stderr[-4000:],
        )


def build_codex_command(
    codex_binary: str,
    workspace: Path,
    output_schema: Path,
    prompt: str,
    model: str | None,
    runner_sandbox: str = "read-only",
) -> list[str]:
    if runner_sandbox not in RUNNER_SANDBOXES:
        raise ValueError(f"Unknown runner sandbox: {runner_sandbox}")
    command = [
        codex_binary,
        "--ask-for-approval",
        "never",
        "exec",
        "--ephemeral",
        "--json",
        "--ignore-user-config",
        "--skip-git-repo-check",
        "--sandbox",
        runner_sandbox,
        "--output-schema",
        str(output_schema),
        "-C",
        str(workspace),
    ]
    if model:
        command.extend(["--model", model])
    command.append(prompt)
    return command


def _workspace_manifest(workspace: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for path in sorted(candidate for candidate in workspace.rglob("*") if candidate.is_file()):
        relative = path.relative_to(workspace).as_posix()
        manifest[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest


def _assert_workspace_unchanged(before: Mapping[str, str], workspace: Path) -> None:
    after = _workspace_manifest(workspace)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(path for path in set(before) & set(after) if before[path] != after[path])
    if added or removed or changed:
        raise ContractError(
            "workspace_mutation",
            "Read-only evaluation run changed its isolated workspace",
            added=added,
            removed=removed,
            changed=changed,
        )


def build_prompt(task: Mapping[str, Any]) -> str:
    return f"""Controlled manage-layered-context paired evaluation.

Read and follow skill/SKILL.md as the only skill version for this run. The local skill root is exactly `skill/`: report the entrypoint as `skill/SKILL.md` and references as `skill/references/...`; never insert `manage-layered-context/` after `skill/`. Project memory paths begin exactly with `project/memory/`. Read only the local skill references it routes to and the project memory files needed for the query. Do not use a globally installed manage-layered-context skill. Do not edit files.

Query: {task['query']}

Return only the JSON required by the supplied schema. task_id must be {task['id']}. loaded_files must list every skill protocol file and project memory file actually read, using workspace-relative paths beginning with skill/ or project/memory/.

Treat top 3 as a candidate upper bound, never a batch-read instruction. Rank candidates, open rank 1, assess whether the query and required evidence are covered, and stop immediately when sufficient. Open ranks 2 and 3 one at a time only for a named remaining gap. recalled_records lists only canonical detail records actually opened, in open order, and must contain at most three workspace-relative paths.

Before returning JSON, verify the provenance invariant recalled_records ⊆ loaded_files. Every path in recalled_records must also appear verbatim in loaded_files; if a canonical record was not actually opened, remove it from recalled_records instead of claiming it.

Copy exact stable identifiers only: fact_ids contains exact fact identifiers; decision_id contains only the exact Decision.choice identifier, never a fact identifier; contradiction_id contains only the exact first-class contradiction identifier, never explanatory prose; next_action_id contains only the exact next-action or required-check identifier. Put human-readable explanation in answer. Set contradiction_detected=true exactly when authoritative loaded records expose an unresolved contradiction. Do not infer a missing identifier. If the query cannot be answered from authoritative memory, set abstained=true, fact_ids=[], decision_id=null, contradiction_detected=false, contradiction_id=null, and next_action_id=null."""


def classify_infrastructure_failure(message: str) -> str:
    lowered = message.lower()
    if any(pattern in lowered for pattern in QUOTA_PATTERNS):
        return "quota_failure"
    if any(pattern in lowered for pattern in AUTH_PATTERNS):
        return "auth_failure"
    if any(pattern in lowered for pattern in NETWORK_PATTERNS):
        return "network_failure"
    return "runner_failure"


def parse_codex_events(text: str) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    final_message: str | None = None
    usage: Mapping[str, Any] | None = None
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ContractError(
                "runner_protocol_failure",
                f"Codex JSONL line {line_number} is invalid JSON",
                line=line[-2000:],
            ) from exc
        if not isinstance(event, dict):
            raise ContractError("runner_protocol_failure", f"Codex JSONL line {line_number} is not an object")
        if event.get("type") == "turn.failed":
            error = event.get("error")
            raw_message = error.get("message") if isinstance(error, dict) else error
            message = str(raw_message or "Codex turn failed without an error message")
            raise ContractError(classify_infrastructure_failure(message), message)
        if event.get("type") == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                final_message = item["text"]
        if event.get("type") == "turn.completed":
            candidate_usage = event.get("usage")
            if isinstance(candidate_usage, dict):
                usage = candidate_usage
    if usage is None:
        raise ContractError("runner_protocol_failure", "Codex JSONL has no turn.completed.usage object")
    if final_message is None:
        raise ContractError("agent_output_failure", "Codex JSONL has no completed agent message")
    try:
        output = json.loads(final_message)
    except json.JSONDecodeError as exc:
        raise ContractError(
            "agent_output_failure",
            "Final agent message is not valid JSON",
            output=final_message[-2000:],
        ) from exc
    if not isinstance(output, dict):
        raise ContractError("agent_output_failure", "Final agent output must be a JSON object")
    return output, usage


def validate_agent_output(output: Mapping[str, Any], task_id: str) -> dict[str, Any]:
    required_fields = {
        "task_id",
        "loaded_files",
        "recalled_records",
        "fact_ids",
        "decision_id",
        "contradiction_detected",
        "contradiction_id",
        "next_action_id",
        "resume_success",
        "abstained",
        "answer",
    }
    if set(output) != required_fields:
        raise ContractError(
            "agent_output_failure",
            "Agent output fields do not match the required schema",
            missing=sorted(required_fields - set(output)),
            unknown=sorted(set(output) - required_fields),
        )
    if output.get("task_id") != task_id:
        raise ContractError(
            "agent_output_failure",
            "Agent output task_id does not match the run",
            expected=task_id,
            actual=output.get("task_id"),
        )
    loaded = require_string_list(output, "loaded_files", "agent_output")
    recalled = require_string_list(output, "recalled_records", "agent_output")
    fact_ids = require_string_list(output, "fact_ids", "agent_output")
    if any(not fact_id.strip() for fact_id in fact_ids) or len(fact_ids) != len(set(fact_ids)):
        raise ContractError(
            "agent_output_failure",
            "agent_output.fact_ids must contain unique non-blank exact identifiers",
        )
    if len(recalled) > 3:
        raise ContractError("agent_output_failure", "recalled_records exceeds Recall@3")
    for field in ("decision_id", "contradiction_id", "next_action_id"):
        if output[field] is not None and not isinstance(output[field], str):
            raise ContractError("agent_output_failure", f"agent_output.{field} must be a string or null")
        if isinstance(output[field], str) and not output[field].strip():
            raise ContractError("agent_output_failure", f"agent_output.{field} must not be blank")
    for field in ("contradiction_detected", "resume_success", "abstained"):
        require_bool(output, field, "agent_output")
    if not isinstance(output["answer"], str):
        raise ContractError("agent_output_failure", "agent_output.answer must be a string")
    normalized_loaded = [
        normalize_relative_path(path, allowed_prefixes=("skill", "project/memory"))
        for path in loaded
    ]
    normalized_recalled = [
        normalize_relative_path(path, allowed_prefixes=("project/memory",))
        for path in recalled
    ]
    if len(set(normalized_loaded)) != len(normalized_loaded) or len(set(normalized_recalled)) != len(normalized_recalled):
        raise ContractError("agent_output_failure", "Agent output file arrays must not contain duplicates")
    if not set(normalized_recalled).issubset(set(normalized_loaded)):
        raise ContractError("agent_output_failure", "Every recalled record must also appear in loaded_files")
    validated = dict(output)
    validated["loaded_files"] = normalized_loaded
    validated["recalled_records"] = normalized_recalled
    validated["fact_ids"] = fact_ids
    return validated


def score_output(
    task: Mapping[str, Any],
    output: Mapping[str, Any],
    workspace: Path,
    encoding: Any,
) -> dict[str, Any]:
    expected = task["expected"]
    required_records = set(expected["required_records"])
    required_facts = set(expected["required_facts"])
    recalled = output["recalled_records"][:3]
    recalled_set = set(recalled)
    fact_ids = set(output["fact_ids"])
    record_recall_score = (
        required_records.issubset(recalled_set) if required_records else None
    )
    fact_extraction_score = (
        required_facts.issubset(fact_ids) if required_facts else None
    )
    decision_score = (
        output["decision_id"] == expected["decision"]
        if expected["decision"] is not None
        else None
    )
    expected_contradiction = expected["contradiction"]
    contradiction_flag_score = output["contradiction_detected"] is (
        expected_contradiction is not None
    )
    contradiction_id_score = (
        output["contradiction_id"] == expected_contradiction
        if expected_contradiction is not None
        else None
    )
    next_action_score = (
        output["next_action_id"] == expected["next_action"]
        if expected["next_action"] is not None
        else None
    )
    resume_score = None
    if expected["resume_success"] is not None:
        resume_score = (
            output["resume_success"] == expected["resume_success"]
            and next_action_score is True
            and record_recall_score is True
            and fact_extraction_score is True
        )
    abstention_accuracy_score = None
    abstention_output_contract_score = None
    if expected["abstained"] is not None:
        abstention_accuracy_score = output["abstained"] == expected["abstained"]
        abstention_output_contract_score = (
            not output["fact_ids"]
            and output["decision_id"] is None
            and output["contradiction_detected"] is False
            and output["contradiction_id"] is None
            and output["next_action_id"] is None
        )

    overhead_tokens, normalized_loaded = exact_loaded_file_tokens(
        workspace,
        output["loaded_files"],
        encoding,
    )
    irrelevant_records = {
        f"project/memory/{record['path']}"
        for record in task["records"]
        if record["relevance"] == "irrelevant"
    }
    irrelevant_loaded = sorted(irrelevant_records & set(normalized_loaded))
    return {
        "scores": {
            "resume_success": resume_score,
            "required_record_recall_at_3": record_recall_score,
            "required_fact_extraction": fact_extraction_score,
            "decision_id_exact": decision_score,
            "contradiction_flag_accuracy": contradiction_flag_score,
            "contradiction_id_exact": contradiction_id_score,
            "next_action_id_exact": next_action_score,
            "abstention_accuracy": abstention_accuracy_score,
            "abstention_output_contract": abstention_output_contract_score,
        },
        "memory_overhead_tokens": overhead_tokens,
        "irrelevant_record_load": len(irrelevant_loaded),
        "irrelevant_records_loaded": irrelevant_loaded,
    }


def run_codex(
    command: Sequence[str],
    timeout_seconds: int,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    runner_environment = dict(os.environ)
    runner_environment["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=runner_environment,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ContractError(
            "runner_timeout",
            f"Codex run exceeded {timeout_seconds} seconds",
            stdout=(exc.stdout or "")[-2000:] if isinstance(exc.stdout, str) else "",
            stderr=(exc.stderr or "")[-2000:] if isinstance(exc.stderr, str) else "",
        ) from exc
    if completed.returncode != 0:
        combined = f"{completed.stdout}\n{completed.stderr}"
        try:
            parse_codex_events(completed.stdout)
        except ContractError as exc:
            if exc.code in EXPLICIT_RUNNER_FAILURE_CODES:
                raise
        raise ContractError(
            classify_infrastructure_failure(combined),
            f"Codex exited with {completed.returncode}",
            returncode=completed.returncode,
            stdout=completed.stdout[-4000:],
            stderr=completed.stderr[-4000:],
        )
    return parse_codex_events(completed.stdout)


def _run_one(
    *,
    arm: str,
    task: Mapping[str, Any],
    repetition: int,
    run_root: Path,
    skill_base: Path,
    codex_binary: str,
    output_schema: Path,
    model: str | None,
    runner_sandbox: str,
    runtime_id: str,
    commit_sha: str | None,
    cli_version: str,
    timeout_seconds: int,
    encoding: Any,
) -> dict[str, Any]:
    run_id = f"{arm}-{task['id']}-{repetition:02d}"
    workspace = run_root / run_id
    shutil.copytree(skill_base, workspace / "skill")
    build_project_fixture(workspace / "project", task, arm)
    command = build_codex_command(
        codex_binary,
        workspace,
        output_schema,
        build_prompt(task),
        model,
        runner_sandbox,
    )
    before = _workspace_manifest(workspace)
    started = time.perf_counter()
    try:
        try:
            raw_output, usage = run_codex(command, timeout_seconds)
        finally:
            _assert_workspace_unchanged(before, workspace)
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        output = validate_agent_output(raw_output, str(task["id"]))
        score = score_output(task, output, workspace, encoding)
        return {
            "run_id": run_id,
            "arm": arm,
            "task_id": task["id"],
            "category": task["category"],
            "repetition": repetition,
            "status": "scored",
            "runtime_id": runtime_id,
            "commit_sha": commit_sha,
            "codex_cli_version": cli_version,
            "model": model,
            "runner_sandbox": runner_sandbox,
            "latency_ms": latency_ms,
            "usage": dict(usage),
            "agent_output": output,
            **score,
        }
    except ContractError as exc:
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        failure_class = "infrastructure" if exc.code in INFRASTRUCTURE_FAILURE_CODES else "evaluation"
        return {
            "run_id": run_id,
            "arm": arm,
            "task_id": task["id"],
            "category": task["category"],
            "repetition": repetition,
            "status": "failed",
            "runtime_id": runtime_id,
            "commit_sha": commit_sha,
            "codex_cli_version": cli_version,
            "model": model,
            "runner_sandbox": runner_sandbox,
            "latency_ms": latency_ms,
            "failure_class": failure_class,
            "failure": exc.as_dict(),
        }


def aggregate_arm(
    runs: Sequence[Mapping[str, Any]],
    arm: str,
    category: str | None = None,
) -> dict[str, Any]:
    arm_runs = [
        run
        for run in runs
        if run["arm"] == arm and (category is None or run.get("category") == category)
    ]
    scored = [run for run in arm_runs if run["status"] == "scored"]
    metrics = {
        metric: rate_entry(run["scores"][metric] for run in scored)
        for metric in RATE_METRICS
    }
    overhead = [run["memory_overhead_tokens"] for run in scored]
    irrelevant = [run["irrelevant_record_load"] for run in scored]
    latencies = [run["latency_ms"] for run in arm_runs if isinstance(run.get("latency_ms"), (int, float))]
    usage_totals: dict[str, int | float] = {}
    for run in scored:
        for key, value in run["usage"].items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage_totals[key] = usage_totals.get(key, 0) + value
    metrics["memory_overhead_tokens"] = {
        "count": len(overhead),
        "p50": nearest_rank(overhead, 0.50) if overhead else None,
        "p95": nearest_rank(overhead, 0.95) if overhead else None,
    }
    metrics["irrelevant_record_load"] = {
        "count": len(irrelevant),
        "total": sum(irrelevant),
        "mean_per_run": (sum(irrelevant) / len(irrelevant)) if irrelevant else None,
    }
    metrics["latency_ms"] = {
        "count": len(latencies),
        "p50": nearest_rank(latencies, 0.50) if latencies else None,
        "p95": nearest_rank(latencies, 0.95) if latencies else None,
        "mean": (sum(latencies) / len(latencies)) if latencies else None,
    }
    result = {
        "arm": arm,
        "run_count": len(arm_runs),
        "scored_count": len(scored),
        "failed_count": len(arm_runs) - len(scored),
        "metrics": metrics,
        "usage_totals": usage_totals,
    }
    if category is not None:
        result["category"] = category
    return result


def evaluate_gates(
    v2: Mapping[str, Any],
    v3: Mapping[str, Any],
    infrastructure_failures: Sequence[Mapping[str, Any]],
    evaluation_failures: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    gates: list[dict[str, Any]] = []

    def add(name: str, actual: Any, comparison: str, threshold: Any, passed: bool) -> None:
        gates.append(
            {
                "name": name,
                "actual": actual,
                "comparison": comparison,
                "threshold": threshold,
                "passed": passed,
            }
        )

    add("infrastructure_failures", len(infrastructure_failures), "==", 0, not infrastructure_failures)
    add("evaluation_failures", len(evaluation_failures), "==", 0, not evaluation_failures)
    add(
        "all_runs_scored",
        {"v2": v2["scored_count"], "v3": v3["scored_count"]},
        "==",
        {"v2": v2["run_count"], "v3": v3["run_count"]},
        v2["scored_count"] == v2["run_count"] and v3["scored_count"] == v3["run_count"],
    )

    for metric, threshold in ABSOLUTE_GATES.items():
        actual = v3["metrics"][metric]["rate"]
        add(f"v3_{metric}", actual, ">=", threshold, actual is not None and actual >= threshold)

    v3_overhead = v3["metrics"]["memory_overhead_tokens"]
    add("v3_memory_overhead_p50", v3_overhead["p50"], "<=", 4000, v3_overhead["p50"] is not None and v3_overhead["p50"] <= 4000)
    add("v3_memory_overhead_p95", v3_overhead["p95"], "<=", 8000, v3_overhead["p95"] is not None and v3_overhead["p95"] <= 8000)
    irrelevant_mean = v3["metrics"]["irrelevant_record_load"]["mean_per_run"]
    add("v3_irrelevant_records_per_run", irrelevant_mean, "<=", 1.0, irrelevant_mean is not None and irrelevant_mean <= 1.0)

    for metric in CORRECTNESS_METRICS:
        v2_rate = v2["metrics"][metric]["rate"]
        v3_rate = v3["metrics"][metric]["rate"]
        delta = round(v3_rate - v2_rate, 12) if v2_rate is not None and v3_rate is not None else None
        add(
            f"v3_vs_v2_{metric}_delta",
            delta,
            ">=",
            -0.02,
            delta is not None and delta >= -0.02,
        )

    v2_p50 = v2["metrics"]["memory_overhead_tokens"]["p50"]
    v3_p50 = v3["metrics"]["memory_overhead_tokens"]["p50"]
    add(
        "v3_overhead_p50_lower_than_v2",
        {"v3": v3_p50, "v2": v2_p50},
        "v3 < v2",
        True,
        v2_p50 is not None and v3_p50 is not None and v3_p50 < v2_p50,
    )
    return gates


def _expected_jobs(
    tasks: Sequence[Mapping[str, Any]],
    repetitions: int,
) -> dict[str, tuple[str, Mapping[str, Any], int]]:
    jobs: dict[str, tuple[str, Mapping[str, Any], int]] = {}
    for arm in ("v2", "v3"):
        for task in tasks:
            for repetition in range(1, repetitions + 1):
                run_id = f"{arm}-{task['id']}-{repetition:02d}"
                jobs[run_id] = (arm, task, repetition)
    return jobs


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_resume_runs(
    path: Path,
    *,
    tasks: Sequence[Mapping[str, Any]],
    repetitions: int,
    runtime_ids: Mapping[str, str],
    commit_shas: Mapping[str, str | None],
    cli_version: str,
    model: str | None,
    runner_sandbox: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not path.is_file():
        raise ContractError("resume_source_missing", f"Resume JSONL does not exist: {path}")
    expected_jobs = _expected_jobs(tasks, repetitions)
    records: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ContractError(
                    "resume_source_invalid",
                    f"Resume JSONL line {line_number} is invalid JSON",
                ) from exc
            if not isinstance(raw, dict):
                raise ContractError(
                    "resume_source_invalid",
                    f"Resume JSONL line {line_number} is not an object",
                )
            run_id = raw.get("run_id")
            if not isinstance(run_id, str) or run_id not in expected_jobs:
                raise ContractError(
                    "resume_source_mismatch",
                    f"Resume JSONL line {line_number} has an unexpected run_id",
                    actual=run_id,
                )
            if run_id in records:
                raise ContractError(
                    "resume_source_duplicate",
                    f"Resume JSONL contains duplicate run_id: {run_id}",
                )
            arm, task, repetition = expected_jobs[run_id]
            expected_metadata = {
                "arm": arm,
                "task_id": task["id"],
                "category": task["category"],
                "repetition": repetition,
                "runtime_id": runtime_ids[arm],
                "commit_sha": commit_shas[arm],
                "codex_cli_version": cli_version,
                "model": model,
                "runner_sandbox": runner_sandbox,
            }
            mismatches = {
                field: {"expected": expected, "actual": raw.get(field)}
                for field, expected in expected_metadata.items()
                if raw.get(field) != expected
            }
            if mismatches:
                raise ContractError(
                    "resume_source_mismatch",
                    f"Resume JSONL metadata does not match the current evaluation: {run_id}",
                    mismatches=mismatches,
                )
            status = raw.get("status")
            if status == "scored":
                if not isinstance(raw.get("usage"), dict):
                    raise ContractError("resume_source_invalid", f"Scored run has no usage object: {run_id}")
                if not isinstance(raw.get("agent_output"), dict):
                    raise ContractError(
                        "resume_source_invalid",
                        f"Scored run has no agent_output object: {run_id}",
                    )
                if not isinstance(raw.get("scores"), dict):
                    raise ContractError("resume_source_invalid", f"Scored run has no scores object: {run_id}")
                overhead = raw.get("memory_overhead_tokens")
                irrelevant = raw.get("irrelevant_record_load")
                latency = raw.get("latency_ms")
                if type(overhead) is not int or overhead < 0:
                    raise ContractError("resume_source_invalid", f"Scored run has invalid overhead: {run_id}")
                if type(irrelevant) is not int or irrelevant < 0:
                    raise ContractError(
                        "resume_source_invalid",
                        f"Scored run has invalid irrelevant-record count: {run_id}",
                    )
                if isinstance(latency, bool) or not isinstance(latency, (int, float)) or latency < 0:
                    raise ContractError("resume_source_invalid", f"Scored run has invalid latency: {run_id}")
            elif status == "failed":
                if raw.get("failure_class") not in {"infrastructure", "evaluation"} or not isinstance(
                    raw.get("failure"), dict
                ):
                    raise ContractError("resume_source_invalid", f"Failed run is malformed: {run_id}")
            else:
                raise ContractError(
                    "resume_source_invalid",
                    f"Resume JSONL run has an unknown status: {run_id}",
                    actual=status,
                )
            records[run_id] = raw

    reusable = [record for record in records.values() if record["status"] == "scored"]
    provenance = {
        "source_jsonl": str(path.resolve()),
        "source_sha256": _file_sha256(path),
        "source_record_count": len(records),
        "reused_scored_run_count": len(reusable),
        "discarded_failed_run_count": sum(
            1 for record in records.values() if record["status"] == "failed"
        ),
        "missing_run_count": len(expected_jobs) - len(records),
    }
    return reusable, provenance


def _revalidate_reused_run(
    run: Mapping[str, Any],
    *,
    task: Mapping[str, Any],
    run_root: Path,
    skill_base: Path,
    encoding: Any,
) -> None:
    workspace = run_root / f"reused-{run['run_id']}"
    shutil.copytree(skill_base, workspace / "skill")
    build_project_fixture(workspace / "project", task, str(run["arm"]))
    output = validate_agent_output(run["agent_output"], str(task["id"]))
    rescored = score_output(task, output, workspace, encoding)
    if run["agent_output"] != output or any(run.get(field) != value for field, value in rescored.items()):
        raise ContractError(
            "resume_score_mismatch",
            f"Reused run does not reproduce under the current scorer: {run['run_id']}",
        )


def run_paired_evaluation(
    *,
    repo_root: Path,
    tasks: Sequence[Mapping[str, Any]],
    sources: Mapping[str, str],
    runtime_ids: Mapping[str, str],
    repetitions: int,
    concurrency: int,
    codex_binary: str,
    output_schema: Path,
    model: str | None,
    runner_sandbox: str,
    cli_version: str,
    commit_shas: Mapping[str, str | None],
    run_jsonl: Path,
    timeout_seconds: int,
    encoding: Any,
    resume_jsonl: Path | None = None,
) -> dict[str, Any]:
    run_jsonl.parent.mkdir(parents=True, exist_ok=True)
    if run_jsonl.exists():
        raise ContractError("output_exists", f"Per-run JSONL already exists: {run_jsonl}")
    reusable_runs: list[dict[str, Any]] = []
    resume_provenance: dict[str, Any] | None = None
    if resume_jsonl is not None:
        reusable_runs, resume_provenance = load_resume_runs(
            resume_jsonl,
            tasks=tasks,
            repetitions=repetitions,
            runtime_ids=runtime_ids,
            commit_shas=commit_shas,
            cli_version=cli_version,
            model=model,
            runner_sandbox=runner_sandbox,
        )
    with tempfile.TemporaryDirectory(prefix="mlc-v3-live-") as temporary:
        temporary_root = Path(temporary)
        skill_bases = {
            "v2": temporary_root / "base-v2",
            "v3": temporary_root / "base-v3",
        }
        materialize_runtime(repo_root, sources["v2"], skill_bases["v2"])
        materialize_runtime(repo_root, sources["v3"], skill_bases["v3"])
        run_root = temporary_root / "runs"
        expected_jobs = _expected_jobs(tasks, repetitions)
        reused_run_ids = {str(run["run_id"]) for run in reusable_runs}
        jobs = [job for run_id, job in expected_jobs.items() if run_id not in reused_run_ids]
        task_categories = {str(task["id"]): str(task["category"]) for task in tasks}
        task_by_id = {str(task["id"]): task for task in tasks}
        results: list[dict[str, Any]] = list(reusable_runs)
        for run in reusable_runs:
            _revalidate_reused_run(
                run,
                task=task_by_id[str(run["task_id"])],
                run_root=run_root,
                skill_base=skill_bases[str(run["arm"])],
                encoding=encoding,
            )
        if resume_provenance is not None:
            resume_provenance["retried_run_count"] = len(jobs)
        with run_jsonl.open("x", encoding="utf-8", newline="\n") as run_log:
            for run in reusable_runs:
                run_log.write(json.dumps(run, ensure_ascii=False, sort_keys=True) + "\n")
                run_log.flush()
                os.fsync(run_log.fileno())
            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
                future_to_job = {
                    executor.submit(
                        _run_one,
                        arm=arm,
                        task=task,
                        repetition=repetition,
                        run_root=run_root,
                        skill_base=skill_bases[arm],
                        codex_binary=codex_binary,
                        output_schema=output_schema,
                        model=model,
                        runner_sandbox=runner_sandbox,
                        runtime_id=runtime_ids[arm],
                        commit_sha=commit_shas[arm],
                        cli_version=cli_version,
                        timeout_seconds=timeout_seconds,
                        encoding=encoding,
                    ): (arm, task["id"], repetition)
                    for arm, task, repetition in jobs
                }
                for future in concurrent.futures.as_completed(future_to_job):
                    arm, task_id, repetition = future_to_job[future]
                    try:
                        run = future.result()
                    except Exception as exc:
                        run = {
                            "run_id": f"{arm}-{task_id}-{repetition:02d}",
                            "arm": arm,
                            "task_id": task_id,
                            "category": task_categories[str(task_id)],
                            "repetition": repetition,
                            "status": "failed",
                            "runtime_id": runtime_ids[arm],
                            "commit_sha": commit_shas[arm],
                            "codex_cli_version": cli_version,
                            "model": model,
                            "runner_sandbox": runner_sandbox,
                            "latency_ms": None,
                            "failure_class": "infrastructure",
                            "failure": {
                                "code": "runner_worker_failure",
                                "message": f"Worker raised {type(exc).__name__}: {exc}",
                            },
                        }
                    results.append(run)
                    run_log.write(json.dumps(run, ensure_ascii=False, sort_keys=True) + "\n")
                    run_log.flush()
                    os.fsync(run_log.fileno())

    task_order = {task["id"]: index for index, task in enumerate(tasks)}
    arm_order = {"v2": 0, "v3": 1}
    results.sort(key=lambda run: (arm_order[run["arm"]], task_order[run["task_id"]], run["repetition"]))
    infrastructure_failures = [
        run for run in results if run.get("failure_class") == "infrastructure"
    ]
    evaluation_failures = [
        run for run in results if run.get("failure_class") == "evaluation"
    ]
    v2_metrics = aggregate_arm(results, "v2")
    v3_metrics = aggregate_arm(results, "v3")
    category_metrics = {
        arm: {
            category: aggregate_arm(results, arm, category)
            for category in sorted(EXPECTED_CATEGORIES)
        }
        for arm in ("v2", "v3")
    }
    gates = evaluate_gates(v2_metrics, v3_metrics, infrastructure_failures, evaluation_failures)
    passed = all(gate["passed"] for gate in gates)
    failures = [
        {
            "run_id": run["run_id"],
            "arm": run["arm"],
            "task_id": run["task_id"],
            "failure_class": run["failure_class"],
            "failure": run["failure"],
        }
        for run in results
        if run["status"] == "failed"
    ]
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "kind": "paired_live_eval",
        "status": "passed" if passed else "failed",
        "preflight": {
            "v2_source": sources["v2"],
            "v3_source": sources["v3"],
            "v2_runtime_id": runtime_ids["v2"],
            "v3_runtime_id": runtime_ids["v3"],
            "task_count": len(tasks),
            "repetitions": repetitions,
            "total_runs": len(results),
            "concurrency": concurrency,
            "tokenizer": TOKENIZER_NAME,
            "codex_cli_version": cli_version,
            "model": model,
            "runner_sandbox": runner_sandbox,
            "approval_policy": "never",
            "ignore_user_config": True,
            "python_bytecode_disabled": True,
        },
        "arms": {"v2": v2_metrics, "v3": v3_metrics},
        "categories": category_metrics,
        "gates": gates,
        "infrastructure_failure_count": len(infrastructure_failures),
        "evaluation_failure_count": len(evaluation_failures),
        "runs": results,
        "failures": failures,
        "run_jsonl": str(run_jsonl),
        "resume": resume_provenance,
    }


def build_parser() -> argparse.ArgumentParser:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Run paired live Codex evaluation for pinned manage-layered-context v2 and a v3 candidate.",
    )
    parser.add_argument("--repo-root", type=Path, default=repo_root)
    parser.add_argument("--v2-ref", default=PINNED_V2_SHA, help="Must resolve to the pinned v2 commit.")
    parser.add_argument(
        "--v3-ref",
        default=WORKTREE_REF,
        help="Git ref for v3, or WORKTREE to evaluate the uncommitted release candidate.",
    )
    parser.add_argument("--tasks", type=Path, default=repo_root / "evals" / "live_tasks.json")
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Exact Codex model used for both arms.")
    parser.add_argument(
        "--runner-sandbox",
        choices=RUNNER_SANDBOXES,
        default="read-only",
        help="Codex shell sandbox. Windows may require danger-full-access; workspace hashes still enforce no writes.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--dry-run",
        "--preflight",
        dest="dry_run",
        action="store_true",
        help="Validate refs, tasks, tokenizer, and runner without invoking codex exec.",
    )
    parser.add_argument("--output", type=Path, help="Optional JSON result path.")
    parser.add_argument("--jsonl-output", type=Path, help="Per-run JSONL path; defaults beside --output.")
    parser.add_argument(
        "--resume-from",
        type=Path,
        help="Reuse strictly matching scored runs from an earlier durable per-run JSONL and retry the rest.",
    )
    return parser


def emit_json_result(result: Mapping[str, Any], output: Path | None) -> None:
    rendered = write_json_result(result, output)
    binary_stdout = getattr(sys.stdout, "buffer", None)
    if binary_stdout is None:
        sys.stdout.write(rendered)
        return
    binary_stdout.write(rendered.encode("utf-8"))
    binary_stdout.flush()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    tasks_path = args.tasks.resolve()
    output_schema = repo_root / "evals" / "schemas" / "live-agent-output.schema.json"
    preflight, failures, tasks, encoding = collect_preflight(
        repo_root,
        tasks_path,
        args.v2_ref,
        args.v3_ref,
        args.codex_bin,
        args.repetitions,
        args.concurrency,
        output_schema,
        args.model,
        args.runner_sandbox,
    )
    if args.timeout_seconds <= 0:
        failures.append({"code": "invalid_timeout", "message": "timeout-seconds must be positive"})
    if failures:
        result = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "kind": "paired_live_eval_preflight",
            "status": "preflight_failed",
            "preflight": preflight,
            "codex_runs_started": 0,
            "failures": failures,
        }
        emit_json_result(result, args.output)
        return 2
    if args.dry_run:
        result = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "kind": "paired_live_eval_preflight",
            "status": "dry_run",
            "preflight": preflight,
            "codex_runs_started": 0,
            "failures": [],
        }
        emit_json_result(result, args.output)
        return 0
    if args.output is None and args.jsonl_output is None:
        result = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "kind": "paired_live_eval_preflight",
            "status": "preflight_failed",
            "preflight": preflight,
            "codex_runs_started": 0,
            "failures": [
                {
                    "code": "output_required",
                    "message": "A full live evaluation requires --output or --jsonl-output for durable diagnostics",
                }
            ],
        }
        emit_json_result(result, args.output)
        return 2

    assert tasks is not None
    assert encoding is not None
    resume_jsonl = args.resume_from.resolve() if args.resume_from is not None else None
    if resume_jsonl is not None:
        preflight["resume_from"] = str(resume_jsonl)
    sources = {"v2": preflight["v2_source"], "v3": preflight["v3_source"]}
    runtime_ids = {"v2": preflight["v2_runtime_id"], "v3": preflight["v3_runtime_id"]}
    commit_shas = {"v2": preflight["v2_commit"], "v3": preflight["v3_commit"]}
    jsonl_output = args.jsonl_output
    if jsonl_output is None and args.output is not None:
        jsonl_output = args.output.with_suffix(".runs.jsonl")
    assert jsonl_output is not None
    jsonl_output = jsonl_output.resolve()
    existing_outputs = [
        str(path.resolve())
        for path in (args.output, jsonl_output)
        if path is not None and path.exists()
    ]
    if existing_outputs:
        result = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "kind": "paired_live_eval_preflight",
            "status": "preflight_failed",
            "preflight": preflight,
            "codex_runs_started": 0,
            "failures": [
                {
                    "code": "output_exists",
                    "message": "Refusing to overwrite existing evaluation output",
                    "paths": existing_outputs,
                }
            ],
        }
        emit_json_result(result, None)
        return 2
    try:
        result = run_paired_evaluation(
            repo_root=repo_root,
            tasks=tasks,
            sources=sources,
            runtime_ids=runtime_ids,
            repetitions=args.repetitions,
            concurrency=args.concurrency,
            codex_binary=preflight["codex_binary"],
            output_schema=output_schema,
            model=args.model,
            runner_sandbox=args.runner_sandbox,
            cli_version=preflight["codex_cli_version"],
            commit_shas=commit_shas,
            run_jsonl=jsonl_output,
            timeout_seconds=args.timeout_seconds,
            encoding=encoding,
            resume_jsonl=resume_jsonl,
        )
    except ContractError as exc:
        result = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "kind": "paired_live_eval",
            "status": "failed",
            "preflight": preflight,
            "failures": [exc.as_dict()],
        }
    emit_json_result(result, args.output)
    if result["status"] == "passed":
        return 0
    if result.get("infrastructure_failure_count", 0):
        return 3
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
