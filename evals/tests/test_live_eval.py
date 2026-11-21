from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evals.read_trace import observed_files
from evals.contracts import ContractError, require_exact_tokenizer
from evals.live_eval import (
    WORKTREE_REF,
    build_codex_command,
    build_prompt,
    build_project_fixture,
    classify_infrastructure_failure,
    emit_json_result,
    evaluate_gates,
    load_resume_runs,
    load_tasks,
    materialize_runtime,
    parse_codex_events,
    runtime_identity,
    run_codex,
    score_output,
    validate_agent_output,
    validate_project_fixture,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
TASKS_PATH = REPO_ROOT / "evals" / "live_tasks.json"
FIXTURES = REPO_ROOT / "evals" / "fixtures"


def _arm_metrics(*, rate: float, p50: int, p95: int, irrelevant: float) -> dict:
    metric_names = (
        "resume_success",
        "unstable_boundary_detected",
        "required_record_recall_at_3",
        "required_fact_extraction",
        "decision_id_exact",
        "contradiction_flag_accuracy",
        "contradiction_id_exact",
        "next_action_id_exact",
        "abstention_accuracy",
        "abstention_output_contract",
    )
    metrics = {
        name: {"passed": int(rate * 100), "total": 100, "rate": rate}
        for name in metric_names
    }
    metrics["unstable_boundary_detected"] = {"passed": 5, "total": 5, "rate": 1.0}
    metrics["input_tokens"] = {"p50": p50}
    metrics["memory_overhead_tokens"] = {"count": 100, "p50": p50, "p95": p95}
    metrics["irrelevant_record_load"] = {"count": 100, "total": irrelevant * 100, "mean_per_run": irrelevant, "max_per_run": irrelevant}
    return {
        "run_count": 100,
        "scored_count": 100,
        "failed_count": 0,
        "metrics": metrics,
    }


class LiveTaskContractTests(unittest.TestCase):
    def test_default_matrix_has_twenty_tasks_and_all_categories(self) -> None:
        tasks = load_tasks(TASKS_PATH)
        self.assertEqual(20, len(tasks))
        counts: dict[str, int] = {}
        for task in tasks:
            counts[task["category"]] = counts.get(task["category"], 0) + 1
        self.assertEqual(
            {
                "resume": 4,
                "decision_fact": 4,
                "contradiction": 4,
                "lesson": 4,
                "abstention_irrelevant": 4,
            },
            counts,
        )

    def test_codex_command_is_ephemeral_json_and_read_only(self) -> None:
        command = build_codex_command(
            "codex",
            Path("workspace"),
            Path("schema.json"),
            "prompt",
            None,
        )
        self.assertEqual(["codex", "--ask-for-approval", "never", "exec"], command[:4])
        self.assertIn("--ephemeral", command)
        self.assertIn("--json", command)
        self.assertIn("--ignore-user-config", command)
        self.assertEqual("read-only", command[command.index("--sandbox") + 1])

    def test_worktree_runtime_is_materialized_without_generated_bytecode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mlc-worktree-runtime-") as temporary:
            destination = Path(temporary) / "runtime"
            materialize_runtime(REPO_ROOT, WORKTREE_REF, destination)
            self.assertEqual(
                (REPO_ROOT / "SKILL.md").read_bytes(),
                (destination / "SKILL.md").read_bytes(),
            )
            self.assertFalse(any("__pycache__" in path.parts for path in destination.rglob("*")))
            self.assertTrue(runtime_identity(REPO_ROOT, WORKTREE_REF).startswith("WORKTREE:"))

    def test_every_task_builds_strict_v2_and_v3_fixtures(self) -> None:
        tasks = load_tasks(TASKS_PATH)
        with tempfile.TemporaryDirectory(prefix="mlc-live-fixtures-") as temporary:
            root = Path(temporary)
            v2_runtime = root / "runtime-v2"
            materialize_runtime(REPO_ROOT, "3ea56f1dcb98f098a7354b45ee4a0457fe1fae8d", v2_runtime)
            for arm, runtime in (("v2", v2_runtime), ("v3", REPO_ROOT)):
                for task in tasks:
                    project = root / arm / task["id"]
                    build_project_fixture(project, task, arm)
                    validate_project_fixture(runtime, project, arm)

    def test_success_fixture_captures_usage_and_scores_exact_fact(self) -> None:
        events = (FIXTURES / "codex-success.jsonl").read_text(encoding="utf-8")
        raw_output, usage, commands = parse_codex_events(events)
        self.assertEqual(3210, usage["input_tokens"])
        output = validate_agent_output(raw_output, "decision-sqlite-wal")
        task = next(task for task in load_tasks(TASKS_PATH) if task["id"] == "decision-sqlite-wal")
        with tempfile.TemporaryDirectory(prefix="mlc-live-score-") as temporary:
            workspace = Path(temporary)
            skill = workspace / "skill" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("local evaluation skill\n", encoding="utf-8")
            build_project_fixture(workspace / "project", task, "v2")
            score = score_output(task, output, workspace, require_exact_tokenizer(), observed_files(commands, workspace))
        self.assertTrue(score["scores"]["required_record_recall_at_3"])
        self.assertTrue(score["scores"]["required_fact_extraction"])
        self.assertTrue(score["scores"]["decision_id_exact"])
        self.assertEqual(0, score["irrelevant_record_load"])
        self.assertGreater(score["memory_overhead_tokens"], 0)

    def test_prompt_defines_output_without_teaching_retrieval(self) -> None:
        task = load_tasks(TASKS_PATH)[0]
        prompt = build_prompt(task)
        self.assertIn("skill/SKILL.md", prompt)
        self.assertNotIn("skill/SKILL.md", build_prompt(task, no_skill=True))
        self.assertNotIn("open rank 1", prompt)
        self.assertIn("decision_id", prompt)
        self.assertIn("contradiction_id", prompt)
        self.assertIn("abstained=true", prompt)

    def test_record_recall_fact_extraction_and_contradiction_ids_score_independently(self) -> None:
        task = next(task for task in load_tasks(TASKS_PATH) if task["id"] == "contradiction-auth-mode")
        output = {
            "task_id": task["id"],
            "loaded_files": [
                "skill/SKILL.md",
                "project/memory/conflicts/CONFLICT-AUTH.md",
                "project/memory/decisions/DEC-AUTH.md",
            ],
            "recalled_records": [
                "project/memory/decisions/DEC-AUTH.md",
                "project/memory/conflicts/CONFLICT-AUTH.md",
            ],
            "fact_ids": ["fact_gateway_bearer_tokens"],
            "decision_id": None,
            "contradiction_detected": True,
            "contradiction_id": "wrong_conflict_id",
            "next_action_id": None,
            "resume_success": False,
            "abstained": False,
            "unstable_boundary": False,
            "answer": "The sources conflict.",
        }
        with tempfile.TemporaryDirectory(prefix="mlc-live-split-score-") as temporary:
            workspace = Path(temporary)
            skill = workspace / "skill" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("local evaluation skill\n", encoding="utf-8")
            build_project_fixture(workspace / "project", task, "v3")
            score = score_output(task, validate_agent_output(output, task["id"]), workspace, require_exact_tokenizer(), output["loaded_files"])
        self.assertTrue(score["scores"]["required_record_recall_at_3"])
        self.assertFalse(score["scores"]["required_fact_extraction"])
        self.assertTrue(score["scores"]["contradiction_flag_accuracy"])
        self.assertFalse(score["scores"]["contradiction_id_exact"])

    def test_correct_abstention_scores_separately_from_supporting_policy_ids(self) -> None:
        task = next(task for task in load_tasks(TASKS_PATH) if task["id"] == "abstain-production-secret")
        output = {
            "task_id": task["id"],
            "loaded_files": [
                "skill/SKILL.md",
                "project/memory/lessons/LESSON-REDACT.md",
            ],
            "recalled_records": ["project/memory/lessons/LESSON-REDACT.md"],
            "fact_ids": ["secrets_are_never_canonical_memory"],
            "decision_id": None,
            "contradiction_detected": False,
            "contradiction_id": None,
            "next_action_id": None,
            "resume_success": False,
            "abstained": True,
            "unstable_boundary": False,
            "answer": "I cannot return a secret that canonical memory must not contain.",
        }
        with tempfile.TemporaryDirectory(prefix="mlc-live-abstention-score-") as temporary:
            workspace = Path(temporary)
            skill = workspace / "skill" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("local evaluation skill\n", encoding="utf-8")
            build_project_fixture(workspace / "project", task, "v3")
            score = score_output(task, validate_agent_output(output, task["id"]), workspace, require_exact_tokenizer(), output["loaded_files"])
        self.assertTrue(score["scores"]["abstention_accuracy"])
        self.assertFalse(score["scores"]["abstention_output_contract"])

    def test_auth_failure_fixture_is_classified_as_infrastructure(self) -> None:
        events = (FIXTURES / "codex-auth-failure.jsonl").read_text(encoding="utf-8")
        with self.assertRaises(ContractError) as caught:
            parse_codex_events(events)
        self.assertEqual("auth_failure", caught.exception.code)

    def test_usage_limit_is_classified_separately_from_authentication(self) -> None:
        message = "You've hit your usage limit. Purchase more credits or try again at 5:03 PM."
        self.assertEqual("quota_failure", classify_infrastructure_failure(message))

    def test_runner_disables_python_bytecode_writes(self) -> None:
        events = (FIXTURES / "codex-success.jsonl").read_text(encoding="utf-8")
        completed = mock.Mock(returncode=0, stdout=events, stderr="")
        with mock.patch("evals.live_eval.subprocess.run", return_value=completed) as run:
            run_codex(["codex", "exec"], 30)
        self.assertEqual("1", run.call_args.kwargs["env"]["PYTHONDONTWRITEBYTECODE"])

    def test_json_output_uses_utf8_bytes_when_stdout_has_a_binary_buffer(self) -> None:
        class LegacyStdout:
            def __init__(self) -> None:
                self.buffer = io.BytesIO()

            def write(self, _: str) -> None:
                raise AssertionError("text stdout must not be used")

        stream = LegacyStdout()
        with mock.patch("evals.live_eval.sys.stdout", stream):
            emit_json_result({"answer": "en dash –"}, None)
        self.assertIn("en dash –", stream.buffer.getvalue().decode("utf-8"))

    def test_resume_loader_reuses_only_strictly_matching_scored_runs(self) -> None:
        task = load_tasks(TASKS_PATH)[0]
        metadata = {
            "arm": "v2",
            "task_id": task["id"],
            "category": task["category"],
            "repetition": 1,
            "runtime_id": "v2-runtime",
            "commit_sha": "v2-commit",
            "codex_cli_version": "codex-cli test",
            "model": "test-model",
            "runner_sandbox": "danger-full-access",
        }
        scored = {
            "run_id": f"v2-{task['id']}-01",
            **metadata,
            "status": "scored",
            "latency_ms": 1.0,
            "usage": {"input_tokens": 5},
            "observed_files": [],
            "agent_output": {},
            "scores": {},
            "memory_overhead_tokens": 1,
            "irrelevant_record_load": 0,
        }
        with tempfile.TemporaryDirectory(prefix="mlc-live-resume-") as temporary:
            source = Path(temporary) / "runs.jsonl"
            source.write_text(json.dumps(scored) + "\n", encoding="utf-8")
            reusable, provenance = load_resume_runs(
                source,
                tasks=[task],
                repetitions=1,
                runtime_ids={"v2": "v2-runtime", "v3": "v3-runtime"},
                commit_shas={"v2": "v2-commit", "v3": None},
                cli_version="codex-cli test",
                model="test-model",
                runner_sandbox="danger-full-access",
            )
        self.assertEqual([scored], reusable)
        self.assertEqual(1, provenance["reused_scored_run_count"])
        self.assertEqual(1, provenance["missing_run_count"])

    def test_resume_loader_rejects_runtime_drift(self) -> None:
        task = load_tasks(TASKS_PATH)[0]
        record = {
            "run_id": f"v2-{task['id']}-01",
            "arm": "v2",
            "task_id": task["id"],
            "category": task["category"],
            "repetition": 1,
            "runtime_id": "stale-runtime",
            "commit_sha": "v2-commit",
            "codex_cli_version": "codex-cli test",
            "model": "test-model",
            "runner_sandbox": "danger-full-access",
            "status": "failed",
            "latency_ms": 1.0,
            "failure_class": "infrastructure",
            "failure": {"code": "quota_failure", "message": "retry later"},
        }
        with tempfile.TemporaryDirectory(prefix="mlc-live-resume-drift-") as temporary:
            source = Path(temporary) / "runs.jsonl"
            source.write_text(json.dumps(record) + "\n", encoding="utf-8")
            with self.assertRaises(ContractError) as caught:
                load_resume_runs(
                    source,
                    tasks=[task],
                    repetitions=1,
                    runtime_ids={"v2": "v2-runtime", "v3": "v3-runtime"},
                    commit_shas={"v2": "v2-commit", "v3": None},
                    cli_version="codex-cli test",
                    model="test-model",
                    runner_sandbox="danger-full-access",
                )
        self.assertEqual("resume_source_mismatch", caught.exception.code)

    def test_agent_output_rejects_recalled_file_that_was_not_loaded(self) -> None:
        output = {
            "task_id": "x",
            "loaded_files": ["skill/SKILL.md"],
            "recalled_records": ["project/memory/tasks/TASK-X.md"],
            "fact_ids": [],
            "decision_id": None,
            "contradiction_detected": False,
            "contradiction_id": None,
            "next_action_id": None,
            "resume_success": False,
            "abstained": True,
            "unstable_boundary": False,
            "answer": "unknown",
        }
        with self.assertRaises(ContractError) as caught:
            validate_agent_output(output, "x")
        self.assertEqual("agent_output_failure", caught.exception.code)


class LiveGateTests(unittest.TestCase):
    def test_all_release_gates_pass_at_thresholds_with_lower_v3_overhead(self) -> None:
        v2 = _arm_metrics(rate=0.97, p50=3500, p95=7000, irrelevant=0.8)
        v3 = _arm_metrics(rate=0.95, p50=3000, p95=7000, irrelevant=1.0)
        gates = evaluate_gates(v2, v3, [], [])
        self.assertTrue(all(gate["passed"] for gate in gates), json.dumps(gates, indent=2))

    def test_any_infrastructure_failure_blocks_release(self) -> None:
        v2 = _arm_metrics(rate=1.0, p50=3500, p95=7000, irrelevant=0.0)
        v3 = _arm_metrics(rate=1.0, p50=3000, p95=7000, irrelevant=0.0)
        gates = evaluate_gates(v2, v3, [{"failure": "network"}], [])
        gate = next(item for item in gates if item["name"] == "infrastructure_failures")
        self.assertFalse(gate["passed"])

    def test_abstention_output_contract_is_reported_but_does_not_gate_release(self) -> None:
        v2 = _arm_metrics(rate=1.0, p50=3500, p95=7000, irrelevant=0.0)
        v3 = _arm_metrics(rate=1.0, p50=3000, p95=7000, irrelevant=0.0)
        v3["metrics"]["abstention_output_contract"] = {"passed": 0, "total": 100, "rate": 0.0}
        gates = evaluate_gates(v2, v3, [], [])
        self.assertTrue(all(gate["passed"] for gate in gates), json.dumps(gates, indent=2))
        self.assertFalse(any("abstention_output_contract" in gate["name"] for gate in gates))

    def test_equal_v2_v3_overhead_fails_strict_improvement_gate(self) -> None:
        v2 = _arm_metrics(rate=1.0, p50=3000, p95=7000, irrelevant=0.0)
        v3 = _arm_metrics(rate=1.0, p50=3000, p95=7000, irrelevant=0.0)
        gates = evaluate_gates(v2, v3, [], [])
        gate = next(item for item in gates if item["name"] == "v3_overhead_p50_lower_than_v2")
        self.assertFalse(gate["passed"])


if __name__ == "__main__":
    unittest.main()
