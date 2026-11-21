from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.contracts import (  # noqa: E402
    ContractError,
    RESULT_SCHEMA_VERSION,
    TOKENIZER_NAME,
    count_tokens,
    parse_json_document,
    require_bool,
    require_exact_tokenizer,
    write_json_result,
)


DEFAULT_HISTORY_SIZES = (100, 300, 1000)
CURRENT_HARD_LIMIT = 600
ROOT_INDEX_HARD_LIMIT = 2000
TOPIC_INDEX_HARD_LIMIT = 2000
DEFAULT_QUERIES_PER_SIZE = 10


@dataclass(frozen=True)
class SyntheticFixture:
    root: Path
    task_count: int
    snapshot_id: str
    detail_paths: tuple[str, ...]
    query_cases: tuple[tuple[str, str], ...]


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8", newline="\n")


def _structured(call: str) -> str:
    return f"```python\n{call.rstrip()}\n```"


def _probe_numbers(task_count: int, query_count: int) -> list[int]:
    if task_count <= 0:
        raise ValueError("task_count must be positive")
    if query_count <= 0:
        raise ValueError("query_count must be positive")
    if query_count == 1:
        return [1]
    if query_count >= task_count:
        return list(range(1, task_count + 1))
    points = {
        1 + round(index * (task_count - 1) / (query_count - 1))
        for index in range(query_count)
    }
    candidate = 1
    while len(points) < query_count:
        points.add(candidate)
        candidate += 1
    return sorted(points)


def generate_fixture(root: Path, task_count: int, query_count: int = DEFAULT_QUERIES_PER_SIZE) -> SyntheticFixture:
    base = "SNAP-20260101-000000"
    snapshot_id = "SNAP-20260101-000100"
    details: list[str] = []
    entries: list[str] = []
    probes: list[tuple[str, str]] = []
    root = root.resolve()
    memory = root
    numbers = _probe_numbers(task_count, query_count)
    # Each clue is shared by many records. The place, service and incident distinguish a task.
    services = ("gateway", "ledger", "catalog", "queue", "billing", "search", "worker", "storage",
                "monitor", "ingress", "export", "mirror", "router", "vault", "cache", "parser",
                "audit", "registry", "scheduler", "journal")
    changes = ("certificate rotation", "quota rollback", "timeout tuning", "queue draining",
               "checksum verification", "lease renewal", "replica recovery", "schema upgrade",
               "鍵の交換", "転送の再開")
    questions = ("How was the certificate replaced", "Why was the new quota reverted",
                 "Why change the timeout", "How was draining handled", "Which checksum was checked",
                 "When did the lease expire", "How did the replica come back", "Why revise the schema",
                 "暗号鍵をいつ交換しましたか", "いつ転送を再開しましたか")
    regions = ("Tokyo", "Berlin", "Lisbon", "Boston", "Seoul")
    _write_text(
        memory / "current.md",
        _structured(f'''ProjectState(
    snapshot_id="{base}",
    project_identity="synthetic-eval",
    record_mode="standard",
    active_goal="Evaluate bounded retrieval over {task_count} archived tasks",
    invariants=["Root and topic indexes stay within hard budgets"],
    active_decisions=[],
    active_lessons=[],
    open_issues=[],
    unresolved_conflicts=[],
    active_tasks=[],
    latest_handoff=None,
    pending_patches=[],
    next_actions=["Run deterministic recall probes"],
    last_updated="2026-01-01T00:00:00Z",
)'''),
    )
    _write_text(
        memory / "project.md",
        _structured('''ProjectMap(
    project_id="synthetic-eval",
    name="Synthetic evaluation",
    purpose="Deterministic retrieval evaluation",
    project_root=".",
    memory_root=".",
    boundaries=["Temporary fixture only"],
    components={"synthetic-history": "tasks/"},
    entry_points=["current.md", "index.md"],
    authoritative_sources=["tasks/"],
    generated_or_disposable=["indexes/"],
    navigation_notes=["Route by component before loading task detail"],
)'''),
    )
    _write_text(
        memory / "anchors.md",
        _structured('''AnchorRegistry(
    aliases={},
    deprecated={},
)'''),
    )
    (memory / "lessons").mkdir(parents=True, exist_ok=True)
    (memory / "handoffs").mkdir(parents=True, exist_ok=True)

    for number in range(1, task_count + 1):
        service = services[(number - 1) % len(services)]
        incident = ((number - 1) // len(services)) % len(changes)
        change = changes[incident]
        region = regions[((number - 1) // (len(services) * len(changes))) % len(regions)]
        cycle = (number - 1) // (len(services) * len(changes) * len(regions))
        if cycle:
            region = f"{region} cohort-{cycle + 1}"
        task_id = f"TASK-{number:06d}"
        task_anchor = f"task:{task_id}"
        namespace = "component" if number % 2 else "concept"
        component_anchor = f"{namespace}:{service}"
        detail_path = f"tasks/{task_id}.md"
        fact = f"fact-{number:06d}-owner-team-{number % 17:02d}"
        decision = f"decision-{number:06d}-mode-{number % 11:02d}"
        next_action = f"verify-task-{number:06d}"
        _write_text(
            root / detail_path,
            _structured(f'''TaskRecord(
    id="{task_id}",
    anchors=["{component_anchor}", "{task_anchor}"],
    fact_ids=["{fact}", "{decision}"],
    objective="{region} {service} maintenance: {change}.",
    status="archived",
    base_snapshot_id="{base}",
    last_updated_snapshot_id="{base}",
    created_at="2026-01-01T00:00:00Z",
    updated_at="2026-01-01T00:00:00Z",
    relevant_anchors=["{component_anchor}", "{task_anchor}"],
    lesson_refs=[],
    required_checks=[],
    handoff_refs=[],
    outcomes=["{fact}", "{decision}"],
    remaining_work=["{next_action}"],
    source_refs=[],
)'''),
        )
        details.append(detail_path)

        entries.append(
            f'IndexEntry(anchors=["{component_anchor}"], summary="{region} {service} {change}", '
            f'path="{detail_path}", importance="L4_historical_detail", status="archived", '
            f'related=[], record_snapshot_id="{base}"),'
        )
        if number in numbers:
            # Vary word order and omit status, paths, IDs and anchor syntax.
            probes.append((f"{questions[incident]} for {region} {service}?", detail_path))

    _write_text(memory / "index.md", _structured(
        f'MemoryIndex(snapshot_id="{base}", entries=[\n' + "\n".join(entries) + "\n])"
    ))
    _write_text(memory / "archive/manifest.md", _structured("ArchiveManifest(entries=[])"))
    cli = Path(__file__).resolve().parents[1] / "scripts/memoryctl.py"
    _run_memoryctl(sys.executable, cli, ["migrate", str(root), "--target-snapshot", snapshot_id,
                   "--apply", "--confirm", "APPLY_V3_MIGRATION", "--json"], cwd=cli.parent)
    return SyntheticFixture(
        root=root,
        task_count=task_count,
        snapshot_id=snapshot_id,
        detail_paths=tuple(details),
        query_cases=tuple(probes),
    )


def inspect_fixture(fixture: SyntheticFixture, token_counter: Callable[[str], int]) -> dict[str, Any]:
    memory = fixture.root
    current_text = (memory / "current.md").read_text(encoding="utf-8")
    root_index_text = (memory / "index.md").read_text(encoding="utf-8")
    route_patterns = set(re.findall(r'path_glob="([^"]+)"', root_index_text))
    expected_shards = {
        path.relative_to(fixture.root).as_posix()
        for path in (memory / "indexes").glob("*.md")
    }
    routed_shards = {
        path.relative_to(fixture.root).as_posix()
        for pattern in route_patterns
        for path in memory.glob(pattern)
        if path.is_file()
    }
    routed_details: set[str] = set()
    topic_token_counts: dict[str, int] = {}
    for shard_path in sorted(expected_shards):
        text = (fixture.root / shard_path).read_text(encoding="utf-8")
        routed_details.update(re.findall(r'path="([^"]+)"', text))
        topic_token_counts[shard_path] = token_counter(text)

    detail_set = set(fixture.detail_paths)
    root_tokens = token_counter(root_index_text)
    current_tokens = token_counter(current_text)
    topic_over_budget = {
        path: tokens
        for path, tokens in topic_token_counts.items()
        if tokens > TOPIC_INDEX_HARD_LIMIT
    }
    return {
        "current_tokens": current_tokens,
        "current_hard_limit": CURRENT_HARD_LIMIT,
        "current_bounded": current_tokens <= CURRENT_HARD_LIMIT,
        "root_index_tokens": root_tokens,
        "root_index_hard_limit": ROOT_INDEX_HARD_LIMIT,
        "root_index_bounded": root_tokens <= ROOT_INDEX_HARD_LIMIT,
        "root_index_contains_detail_paths": 'path="tasks/' in root_index_text,
        "topic_index_count": len(expected_shards),
        "topic_index_max_tokens": max(topic_token_counts.values(), default=0),
        "topic_index_hard_limit": TOPIC_INDEX_HARD_LIMIT,
        "topic_indexes_bounded": not topic_over_budget,
        "topic_indexes_over_budget": topic_over_budget,
        "all_topic_indexes_reachable": routed_shards == expected_shards,
        "detail_record_count": len(detail_set),
        "reachable_detail_count": len(routed_details & detail_set),
        "all_details_reachable": routed_details == detail_set,
        "missing_details": sorted(detail_set - routed_details)[:20],
        "unexpected_details": sorted(routed_details - detail_set)[:20],
    }


def _run_memoryctl(
    python_executable: str,
    memoryctl: Path,
    args: Sequence[str],
    *,
    cwd: Path,
) -> Mapping[str, Any]:
    command = [python_executable, str(memoryctl), *args]
    environment = os.environ.copy()
    environment["MLC_EVAL_OFFLINE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise ContractError(
            "memoryctl_command_failed",
            f"memoryctl exited with {completed.returncode}",
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout[-4000:],
            stderr=completed.stderr[-4000:],
        )
    return parse_json_document(completed.stdout, "memoryctl")


def _validate_validate_output(output: Mapping[str, Any]) -> None:
    require_bool(output, "ok", "validate")
    if output["ok"] is not True:
        raise ContractError("validation_failed", "memoryctl validate reported ok=false", output=output)
    errors = output.get("errors")
    if not isinstance(errors, list):
        raise ContractError("invalid_contract_field", "validate.errors must be an array")
    if errors:
        raise ContractError("validation_failed", "memoryctl validate returned errors", errors=errors)


def _validate_budget_output(output: Mapping[str, Any]) -> None:
    if require_bool(output, "ok", "budget") is not True:
        raise ContractError("budget_failed", "memoryctl budget reported ok=false", output=output)
    if output.get("schema_version") != 3:
        raise ContractError(
            "budget_schema_mismatch",
            "memoryctl budget must report schema_version 3",
            actual=output.get("schema_version"),
        )
    items = output.get("items")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ContractError("invalid_contract_field", "budget.items must be an array of objects")
    inexact = [item for item in items if item.get("exact") is not True]
    if inexact:
        raise ContractError(
            "budget_not_exact",
            "memoryctl budget must report exact o200k_base counts for every item",
            items=inexact,
        )
    failed = [item for item in items if item.get("over_hard") is not False]
    if failed:
        raise ContractError("budget_failed", "One or more files exceed their hard budget", files=failed)


def _recall_paths(output: Mapping[str, Any]) -> list[str]:
    results = output.get("candidates")
    if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
        raise ContractError("invalid_contract_field", "recall.candidates must be an array of objects")
    paths: list[str] = []
    for item in results:
        path = item.get("path")
        if not isinstance(path, str):
            raise ContractError("invalid_contract_field", "Each recall result must contain a string path")
        paths.append(path.replace("\\", "/"))
    if len(paths) > 3:
        raise ContractError("recall_top_k_violation", "recall --top 3 returned more than three results")
    return paths


def collect_preflight(repo_root: Path, memoryctl: Path, python_executable: str) -> tuple[list[dict[str, Any]], Any | None]:
    failures: list[dict[str, Any]] = []
    encoding: Any | None = None
    if not repo_root.is_dir():
        failures.append({"code": "missing_repo_root", "message": f"Repository root does not exist: {repo_root}"})
    if not memoryctl.is_file():
        failures.append(
            {
                "code": "missing_memoryctl",
                "message": f"Planned CLI is not present: {memoryctl}",
                "required_interface": [
                    "validate ROOT --strict-v3 --json",
                    "recall ROOT --query QUERY --top 3 --json",
                    "budget ROOT --require-exact --json",
                ],
            }
        )
    elif python_executable:
        required_help = {
            "validate": ("--strict-v3", "--json"),
            "recall": ("--query", "--top", "--json"),
            "budget": ("--require-exact", "--json"),
        }
        for command, required_options in required_help.items():
            try:
                completed = subprocess.run(
                    [python_executable, str(memoryctl), command, "--help"],
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "MLC_EVAL_OFFLINE": "1"},
                )
            except OSError as exc:
                failures.append(
                    {
                        "code": "memoryctl_interface_unavailable",
                        "message": f"Could not execute memoryctl {command} --help: {exc}",
                    }
                )
                break
            missing_options = [option for option in required_options if option not in completed.stdout]
            if completed.returncode != 0 or missing_options:
                failures.append(
                    {
                        "code": "memoryctl_interface_mismatch",
                        "message": f"memoryctl {command} does not match the planned interface",
                        "returncode": completed.returncode,
                        "missing_options": missing_options,
                        "stderr": completed.stderr[-2000:],
                    }
                )
    if not python_executable:
        failures.append({"code": "missing_python", "message": "Python executable is empty"})
    try:
        encoding = require_exact_tokenizer()
    except ContractError as exc:
        failures.append(exc.as_dict())
    return failures, encoding


def run_evaluation(
    repo_root: Path,
    memoryctl: Path,
    python_executable: str,
    history_sizes: Sequence[int],
    query_count: int,
    encoding: Any,
) -> dict[str, Any]:
    histories: list[dict[str, Any]] = []
    all_failures: list[dict[str, Any]] = []
    total_hits = 0
    total_queries = 0

    with tempfile.TemporaryDirectory(prefix="mlc-v3-deterministic-") as temporary:
        temporary_root = Path(temporary)
        for task_count in history_sizes:
            fixture = generate_fixture(temporary_root / f"history-{task_count}", task_count, query_count)
            inspection = inspect_fixture(fixture, lambda text: count_tokens(encoding, text))
            failures: list[dict[str, Any]] = []
            local_checks = {
                "current_bounded": inspection["current_bounded"],
                "root_index_bounded": inspection["root_index_bounded"],
                "root_index_has_no_detail_paths": not inspection["root_index_contains_detail_paths"],
                "topic_indexes_bounded": inspection["topic_indexes_bounded"],
                "all_topic_indexes_reachable": inspection["all_topic_indexes_reachable"],
                "all_details_reachable": inspection["all_details_reachable"],
            }
            for name, passed in local_checks.items():
                if not passed:
                    failures.append(
                        {
                            "code": "local_fixture_check_failed",
                            "message": f"Synthetic fixture check failed: {name}",
                        }
                    )

            validate_ok = False
            budget_ok = False
            hits = 0
            recall_cases: list[dict[str, Any]] = []
            try:
                validate_output = _run_memoryctl(
                    python_executable,
                    memoryctl,
                    ["validate", str(fixture.root), "--strict-v3", "--json"],
                    cwd=repo_root,
                )
                _validate_validate_output(validate_output)
                validate_ok = True

                budget_output = _run_memoryctl(
                    python_executable,
                    memoryctl,
                    ["budget", str(fixture.root), "--require-exact", "--json"],
                    cwd=repo_root,
                )
                _validate_budget_output(budget_output)
                budget_ok = True

                absent = _run_memoryctl(python_executable, memoryctl, [
                    "recall", str(fixture.root), "--query", "who is the legal proprietor", "--top", "3", "--json",
                ], cwd=repo_root)
                if _recall_paths(absent):
                    failures.append({"code": "false_positive", "message": "unanswerable query returned candidates"})
                if task_count >= 2:
                    across = _run_memoryctl(python_executable, memoryctl, [
                        "recall", str(fixture.root), "--query", "Tokyo gateway ledger certificate replacement",
                        "--top", "3", "--json",
                    ], cwd=repo_root)
                    if not set(fixture.detail_paths[:2]).issubset(_recall_paths(across)):
                        failures.append({"code": "cross_route_miss", "message": "missing a record from a second route"})
                for query, expected_path in fixture.query_cases:
                    recall_output = _run_memoryctl(
                        python_executable,
                        memoryctl,
                        [
                            "recall",
                            str(fixture.root),
                            "--query",
                            query,
                            "--top",
                            "3",
                            "--json",
                        ],
                        cwd=repo_root,
                    )
                    paths = _recall_paths(recall_output)
                    hit = expected_path in paths[:3]
                    hits += int(hit)
                    recall_cases.append(
                        {
                            "query": query,
                            "expected_path": expected_path,
                            "returned_paths": paths,
                            "hit": hit,
                        }
                    )
            except ContractError as exc:
                failures.append(exc.as_dict())

            query_total = len(fixture.query_cases)
            misses = [case for case in recall_cases if not case["hit"]]
            if len(recall_cases) == query_total and misses:
                failures.append(
                    {
                        "code": "recall_at_3_failed",
                        "message": f"{len(misses)} of {query_total} required records missed Recall@3",
                        "misses": misses,
                    }
                )
            total_hits += hits
            total_queries += query_total
            all_failures.extend(
                {"task_count": task_count, **failure}
                for failure in failures
            )
            histories.append(
                {
                    "task_count": task_count,
                    "inspection": inspection,
                    "local_checks": local_checks,
                    "cli": {
                        "validate_ok": validate_ok,
                        "budget_exact_ok": budget_ok,
                    },
                    "metrics": {
                        "recall_at_3": {
                            "hits": hits,
                            "total": query_total,
                            "rate": hits / query_total if query_total else None,
                        }
                    },
                    "recall_cases": recall_cases,
                    "failures": failures,
                }
            )

    passed = not all_failures and total_hits == total_queries
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "kind": "deterministic_synthetic_eval",
        "status": "passed" if passed else "failed",
        "config": {
            "history_sizes": list(history_sizes),
            "queries_per_size": query_count,
            "fixture_creation": "flat v2 index migrated by forensic transaction",
            "tokenizer": TOKENIZER_NAME,
            "exact_budget_required": True,
            "network_allowed": False,
        },
        "aggregate": {
            "history_count": len(history_sizes),
            "recall_at_3": {
                "hits": total_hits,
                "total": total_queries,
                "rate": total_hits / total_queries if total_queries else None,
            },
            "all_root_indexes_bounded": all(
                history["inspection"]["root_index_bounded"] for history in histories
            ),
            "all_records_reachable": all(
                history["inspection"]["all_details_reachable"] for history in histories
            ),
            "all_exact_budget_checks_passed": all(
                history["cli"]["budget_exact_ok"] for history in histories
            ),
        },
        "histories": histories,
        "failures": all_failures,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run deterministic, offline synthetic evaluations for manage-layered-context v3.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root containing scripts/memoryctl.py.",
    )
    parser.add_argument(
        "--memoryctl",
        type=Path,
        default=None,
        help="Override the planned memoryctl.py path (used by contract tests).",
    )
    parser.add_argument("--python", default=sys.executable, help="Python executable for memoryctl.")
    parser.add_argument(
        "--history-sizes",
        nargs="+",
        type=int,
        default=list(DEFAULT_HISTORY_SIZES),
        help="Synthetic task-history sizes. Default: 100 300 1000.",
    )
    parser.add_argument(
        "--queries-per-size",
        type=int,
        default=DEFAULT_QUERIES_PER_SIZE,
        help="Evenly distributed Recall@3 probes per history.",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Check the exact tokenizer and planned CLI without generating fixtures.",
    )
    parser.add_argument("--output", type=Path, help="Optional JSON result path.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    memoryctl = (args.memoryctl or (repo_root / "scripts" / "memoryctl.py")).resolve()
    history_sizes = tuple(sorted(set(args.history_sizes)))
    input_failures: list[dict[str, Any]] = []
    if not history_sizes or any(size <= 0 for size in history_sizes):
        input_failures.append({"code": "invalid_history_sizes", "message": "History sizes must be positive"})
    if args.queries_per_size <= 0:
        input_failures.append({"code": "invalid_query_count", "message": "queries-per-size must be positive"})

    preflight_failures, encoding = collect_preflight(repo_root, memoryctl, args.python)
    failures = [*input_failures, *preflight_failures]
    if failures:
        result = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "kind": "deterministic_synthetic_eval_preflight",
            "status": "preflight_failed",
            "tokenizer": TOKENIZER_NAME,
            "memoryctl": str(memoryctl),
            "failures": failures,
        }
        sys.stdout.write(write_json_result(result, args.output))
        return 2

    if args.preflight:
        result = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "kind": "deterministic_synthetic_eval_preflight",
            "status": "passed",
            "tokenizer": TOKENIZER_NAME,
            "memoryctl": str(memoryctl),
            "history_sizes": list(history_sizes),
            "failures": [],
        }
        sys.stdout.write(write_json_result(result, args.output))
        return 0

    assert encoding is not None
    try:
        result = run_evaluation(
            repo_root,
            memoryctl,
            args.python,
            history_sizes,
            args.queries_per_size,
            encoding,
        )
    except ContractError as exc:
        result = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "kind": "deterministic_synthetic_eval",
            "status": "failed",
            "failures": [exc.as_dict()],
        }
    sys.stdout.write(write_json_result(result, args.output))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
