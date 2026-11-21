from __future__ import annotations

import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from memory_model import ParsedCall, TokenMeasurement  # noqa: E402
from memory_recall import recall  # noqa: E402
from memory_transaction import render_markdown  # noqa: E402
from memory_validation import budget_report, primary_anchor, validate_memory  # noqa: E402
from support import call, entry, make_project, write  # noqa: E402


class ValidationTests(unittest.TestCase):
    def test_skill_entry_stays_below_hard_budget(self) -> None:
        report = budget_report(Path(__file__).resolve().parents[1])
        item = report["items"][0]
        self.assertEqual("skill_entry", item["kind"])
        self.assertLessEqual(item["tokens"], 2000)

    def test_v3_root_passes_strict_validation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = make_project(Path(raw), version=3)
            findings = validate_memory(memory, strict_v3=True)
            self.assertEqual([], findings.errors)

    def test_v2_root_remains_read_write_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = make_project(Path(raw), version=2)
            findings = validate_memory(memory, strict_v2=True)
            self.assertEqual([], findings.errors)

    def test_v3_requires_first_class_fact_ids_while_v2_allows_legacy_records(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            v2 = make_project(root / "v2", version=2)
            self.assertEqual([], validate_memory(v2, strict_v2=True).errors)

            v3 = make_project(root / "v3", version=3)
            decision_path = v3 / "decisions" / "ADR-0001-alpha.md"
            text = decision_path.read_text(encoding="utf-8")
            write(decision_path, text.replace('    fact_ids=[\n        "fact:alpha-uses-amber",\n    ],\n', ""))
            findings = validate_memory(v3, strict_v3=True)
            self.assertTrue(
                any("Decision.fact_ids is required by v3" in error for error in findings.errors),
                findings.errors,
            )

    def test_v3_open_issue_requires_explicit_contradiction_id_field(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = make_project(Path(raw), version=3)
            issue = call(
                "OpenIssue",
                id="ISSUE-0001",
                anchors=["issue:ISSUE-0001", "concept:alpha"],
                topic="Alpha",
                status="open",
                question="Is alpha coherent?",
                current_understanding="No contradiction has been established.",
                next_action=None,
                source_refs=[],
                resolved_by=None,
                fact_ids=[],
            )
            write(memory / "issues" / "ISSUE-0001.md", render_markdown("ISSUE-0001", issue))
            findings = validate_memory(memory, strict_v3=True)
            self.assertTrue(
                any("OpenIssue.contradiction_id is required by v3" in error for error in findings.errors),
                findings.errors,
            )

    def test_index_path_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = make_project(Path(raw), version=3)
            text = (memory / "index.md").read_text(encoding="utf-8")
            write(memory / "index.md", text.replace("decisions/ADR-0001-alpha.md", "../escape.md"))
            findings = validate_memory(memory, strict_v3=True)
            self.assertTrue(any("escapes memory root" in error for error in findings.errors))

    def test_topic_route_cannot_descend_below_one_index_level(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = make_project(Path(raw), version=3)
            index_path = memory / "index.md"
            write(
                index_path,
                index_path.read_text(encoding="utf-8").replace(
                    'path_glob="indexes/general.md"',
                    'path_glob="indexes/nested/*.md"',
                ),
            )
            findings = validate_memory(memory, strict_v3=True)
            self.assertTrue(any("one-level relative indexes/ glob" in error for error in findings.errors))

    def test_unreachable_canonical_record_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = make_project(Path(raw), version=3)
            task = call(
                "TaskRecord",
                id="TASK-20260830-001",
                anchors=["task:TASK-20260830-001"],
                objective="Unreachable task",
                status="active",
                base_snapshot_id="SNAP-20260830-010000",
                last_updated_snapshot_id="SNAP-20260830-010000",
                created_at="2026-08-30T01:00:00+09:00",
                updated_at="2026-08-30T01:00:00+09:00",
                relevant_anchors=[],
                lesson_refs=[],
                required_checks=[],
                handoff_refs=[],
                outcomes=[],
                remaining_work=[],
                source_refs=[],
                fact_ids=[],
            )
            write(memory / "tasks" / "TASK-20260830-001.md", render_markdown("TASK-1", task))
            findings = validate_memory(memory, strict_v3=True)
            self.assertIn(
                "canonical record is unreachable from indexes: tasks/TASK-20260830-001.md",
                findings.errors,
            )

    def test_v3_hard_budget_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = make_project(Path(raw), version=3)
            with (memory / "current.md").open("a", encoding="utf-8") as handle:
                handle.write("\n" + "overflow " * 5000)
            for exact in (False, True):
                measurement = TokenMeasurement(9000, "o200k_base", exact, "test")
                with self.subTest(exact=exact), patch("memory_validation.count_tokens", return_value=measurement):
                    findings = validate_memory(memory, strict_v3=True)
                    self.assertEqual(exact, any("budget hard limit exceeded" in e for e in findings.errors))
                    self.assertEqual(not exact, any("budget hard limit exceeded" in w for w in findings.warnings))


    def test_v2_hard_budget_is_only_a_warning(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = make_project(Path(raw), version=2)
            with (memory / "current.md").open("a", encoding="utf-8") as handle:
                handle.write("\n" + "legacy-overflow " * 5000)
            findings = validate_memory(memory, strict_v2=True)
            self.assertFalse(any("budget hard limit exceeded" in error for error in findings.errors))
            self.assertTrue(any("budget hard limit exceeded" in warning for warning in findings.warnings))

    def test_unknown_config_field_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = make_project(Path(raw), version=3)
            config = json.loads((memory / "config.json").read_text(encoding="utf-8"))
            config["surprise"] = True
            write(memory / "config.json", json.dumps(config))
            findings = validate_memory(memory, strict_v3=True)
            self.assertTrue(any("unknown config fields" in error for error in findings.errors))

    def test_ready_lite_handoff_validates_changed_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = make_project(Path(raw), version=3)
            project = memory.parent
            write(project / "src" / "alpha.py", "VALUE = 'amber'\n")
            handoff = call(
                "HandoffCheckpoint",
                profile="lite",
                checkpoint_id="HANDOFF-20260830-011000-alpha",
                task_id="TASK-20260830-001",
                fact_ids=["fact:alpha-ready"],
                status="ready",
                objective="Continue alpha verification.",
                state_summary="Alpha implementation is stable.",
                completed=["Implemented alpha."],
                remaining=["Run regression tests."],
                next_action="Run regression tests.",
                changed_files=["src/alpha.py"],
                verification=["syntax check passed"],
                blockers=[],
                source_snapshot_id="SNAP-20260830-010000",
                checkpoint_snapshot_id="SNAP-20260830-010000",
            )
            relative = "handoffs/HANDOFF-20260830-011000-alpha.md"
            write(memory / relative, render_markdown("HANDOFF alpha", handoff))
            shard = call(
                "TopicIndex",
                topic_id="topic:general",
                snapshot_id="SNAP-20260830-010000",
                anchors=["component:alpha"],
                entries=[entry(relative, "SNAP-20260830-010000", anchors=["component:alpha"])],
                archive_route="archive/manifest.md",
            )
            write(memory / "indexes" / "general.md", render_markdown("Topic Index: General", shard))
            findings = validate_memory(memory, strict_v3=True)
            self.assertEqual([], findings.errors)


class RecallTests(unittest.TestCase):
    def test_v3_recall_uses_top_three_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = make_project(Path(raw), version=3)
            result = recall(memory, "alpha amber decision")
            self.assertEqual(3, result["top"])
            self.assertEqual("decisions/ADR-0001-alpha.md", result["candidates"][0]["path"])

    def test_all_routes_are_considered_without_requiring_a_route_name(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = make_project(Path(raw), version=3)
            archive = call(
                "ArchiveRecord",
                id="ARCHIVE-20260830-omega",
                anchors=["concept:omega"],
                archived_at="2026-08-30T01:00:00+09:00",
                summary="Omega-only history.",
                affected_records=[],
                details=["No alpha facts."],
                mistakes=[],
                source_refs=[],
                fact_ids=["fact:omega-history"],
            )
            archive_path = "archive/ARCHIVE-20260830-omega.md"
            write(memory / archive_path, render_markdown("Omega archive", archive))
            omega_shard = call(
                "TopicIndex",
                topic_id="topic:omega",
                snapshot_id="SNAP-20260830-010000",
                anchors=["concept:omega"],
                entries=[entry(archive_path, "SNAP-20260830-010000", status="archived", anchors=["concept:omega"])],
                archive_route="archive/manifest.md",
            )
            write(memory / "indexes" / "omega.md", render_markdown("Topic Index: Omega", omega_shard))
            root_index = (memory / "index.md").read_text(encoding="utf-8")
            omega_route = '''        IndexRoute(
            route_id="route:omega",
            anchor_prefixes=["concept:omega"],
            path_glob="indexes/omega.md",
            summary="Omega history only.",
            status_scope=["active", "archived", "deprecated", "scratch"],
        ),\n'''
            write(memory / "index.md", root_index.replace("    ],\n)", omega_route + "    ],\n)"))
            result = recall(memory, "alpha amber decision")
            self.assertIn("route:omega", result["selected_routes"])
            self.assertEqual(2, result["records_considered"])
            self.assertEqual("decisions/ADR-0001-alpha.md", result["candidates"][0]["path"])
            result = recall(memory, "history", kinds=("archive",))
            self.assertEqual(archive_path, result["candidates"][0]["path"])

    def test_recall_declares_candidates_as_a_sequential_upper_bound(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = make_project(Path(raw), version=3)
            result = recall(memory, "alpha amber decision")
            self.assertEqual(
                {
                    "mode": "sequential",
                    "candidate_limit": 3,
                    "open_one_at_a_time": True,
                    "stop_when_sufficient": True,
                },
                result["retrieval_policy"],
            )
            self.assertIn("upper bound", result["notice"])

    def test_primary_anchor_precedence_is_deterministic(self) -> None:
        self.assertEqual(
            "component:alpha",
            primary_anchor(["risk:z", "operation:x", "concept:b", "component:alpha"]),
        )


if __name__ == "__main__":
    unittest.main()
