import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evals.contracts import ContractError, PINNED_V2_SHA, require_exact_tokenizer
from evals.live_eval import _workspace_manifest, build_project_fixture, load_tasks, parse_codex_events, run_paired_evaluation, score_output
from evals.gate_summary import render
from evals.read_trace import observed_files
from evals.write_eval import CASES, DECISION, HANDOFF, LESSON, REPO, TASK, score_write, seed
from memory_model import ParsedCall
from memory_transaction import _record, committed_snapshot, render_markdown
from memory_writes import put_record


class ReadEvidenceTests(unittest.TestCase):
    def test_claimed_reads_do_not_determine_recall_or_overhead(self):
        task = next(t for t in load_tasks(REPO / "evals/live_tasks.json") if t["id"] == "decision-sqlite-wal")
        output, _, _ = parse_codex_events((REPO / "evals/fixtures/codex-success.jsonl").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            build_project_fixture(workspace / "project", task, "v2")
            events = [{"command": "cd project/memory && cat decisions/DEC-SQLITE-OLD.md"}]
            seen = observed_files(events, workspace)
            score = score_output(task, output, workspace, require_exact_tokenizer(), seen)
            self.assertFalse(score["scores"]["required_record_recall_at_3"])
            self.assertEqual(1, score["irrelevant_record_load"])
            self.assertEqual(seen, score["unreported_reads"])
            self.assertIn("project/memory/decisions/DEC-SQLITE.md", score["unobserved_claims"])

    def test_three_arm_replay_records_observed_reads_and_public_summary(self):
        task = next(t for t in load_tasks(REPO / "evals/live_tasks.json") if t["id"] == "decision-sqlite-wal")
        # The same response is scored in each arm, including an undeclared read.
        output, usage, _ = parse_codex_events((REPO / "evals/fixtures/codex-success.jsonl").read_text())
        required = task["expected"]["required_records"][0]
        command = {"command": f"cat {required} project/memory/decisions/DEC-SQLITE-OLD.md", "exit_code": 0}
        arms = {"v2": PINNED_V2_SHA, "v3": "WORKTREE", "no_skill": "no-skill"}
        def replay(argv, timeout):
            workspace = Path(argv[argv.index("-C") + 1])
            self.assertEqual((workspace / "skill/SKILL.md").exists(), "skill/SKILL.md" in argv[-1])
            return output, usage, [command]
        with tempfile.TemporaryDirectory() as tmp, patch("evals.live_eval.run_codex", side_effect=replay):
            result = run_paired_evaluation(
                repo_root=REPO, tasks=[task], sources=arms, runtime_ids=arms, repetitions=1, concurrency=1,
                codex_binary="codex", output_schema=REPO / "evals/schemas/live-agent-output.schema.json",
                model="fixture", runner_sandbox="read-only", cli_version="fixture", commit_shas=arms,
                run_jsonl=Path(tmp) / "runs.jsonl", timeout_seconds=10, encoding=require_exact_tokenizer(),
            )
        self.assertEqual(set(arms), set(result["arms"]))
        self.assertEqual([1, 1, 1], [r["irrelevant_record_load"] for r in result["runs"]])
        self.assertTrue(all(r["unreported_reads"] for r in result["runs"]))
        summary = render(result)
        self.assertIn("v3_input_tokens_p50_vs_v2", summary)
        self.assertIn("decision_fact", summary)
        self.assertNotIn("DEC-SQLITE-OLD.md", summary)

    def test_listing_and_recall_are_not_detail_reads_and_opaque_commands_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self.assertEqual([], observed_files([{"command": "rg --files project/memory"},
                {"command": "python skill/scripts/memoryctl.py recall project/memory --query x"}], workspace))
            with self.assertRaisesRegex(ContractError, "Unattributed"):
                observed_files([{"command": "python read_all.py"}], workspace)


class WriteOutcomeTests(unittest.TestCase):
    def test_diff_and_validator_require_the_real_outcome(self):
        for case in CASES:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                root = seed(workspace)
                base = committed_snapshot(root)
                before = _workspace_manifest(workspace)
                self.assertFalse(score_write(case, workspace, before, base)["passed"])
                if case == "record":
                    record = ParsedCall("Decision", {
                        "id": "ADR-EXPORT", "anchors": ["component:export"], "topic": "Export publication",
                        "choice": "verify_export_checksum", "status": "active", "confidence": "high",
                        "scope": "Export service", "rationale": "Accepted by export maintainers.",
                        "alternatives": [], "source_refs": [], "review_when": None, "supersedes": [],
                        "fact_ids": ["fact:checksum-required"],
                    }, 1)
                    path = DECISION
                elif case == "handoff":
                    record = ParsedCall("HandoffCheckpoint", {
                        "profile": "lite", "checkpoint_id": "HANDOFF-EXPORT", "task_id": "TASK-EXPORT",
                        "status": "ready", "objective": "Resume export", "state_summary": "Fixture verified.",
                        "completed": ["Verified fixture"], "remaining": ["Check export checksum"],
                        "next_action": "verify_export_checksum", "changed_files": [], "verification": ["Fixture verified"],
                        "blockers": [], "source_snapshot_id": base, "checkpoint_snapshot_id": base, "fact_ids": [],
                    }, 1)
                    path = HANDOFF
                else:
                    record = _record(root / LESSON)
                    record.fields.update(status="active", supporting_evidence=[
                        ParsedCall("SourceRef", {"ref_type": "task", "ref_id": name}, 1)
                        for name in ("TASK-EXPORT", "TASK-RETRY")
                    ])
                    path = LESSON
                draft = workspace / "scratch/draft.md"
                draft.parent.mkdir()
                draft.write_text(render_markdown("Export", record))
                put_record(root, draft, path=path)
                report = score_write(case, workspace, before, base)
                self.assertTrue(report["passed"], report)
                with (root / "project.md").open("a") as source:
                    source.write("\nUnrelated edit.\n")
                self.assertFalse(score_write(case, workspace, before, base)["passed"])
