from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from memory_model import RecordSyntaxError, parse_python_block  # noqa: E402


VALID = '''
IndexEntry(
    anchors=["component:alpha"],
    summary="Alpha decision.",
    path="decisions/ADR-0001-alpha.md",
    importance="L1_active_decision",
    status="active",
    related=[],
    record_snapshot_id="SNAP-20260830-010000",
)
'''


class SafeAstTests(unittest.TestCase):
    def test_parses_keyword_only_nested_data(self) -> None:
        record = parse_python_block(VALID, "valid.md")
        self.assertEqual("IndexEntry", record.name)
        self.assertEqual("decisions/ADR-0001-alpha.md", record.fields["path"])

    def test_rejects_positional_arguments(self) -> None:
        with self.assertRaisesRegex(RecordSyntaxError, "positional arguments"):
            parse_python_block('SourceRef("file", ref_id="x")')

    def test_rejects_duplicate_fields(self) -> None:
        with self.assertRaisesRegex(RecordSyntaxError, "duplicate field"):
            parse_python_block(
                'SourceRef(ref_type="file", ref_id="a", ref_id="b", note=None)'
            )

    def test_rejects_unknown_fields(self) -> None:
        with self.assertRaisesRegex(RecordSyntaxError, "unknown field"):
            parse_python_block(
                'SourceRef(ref_type="file", ref_id="a", note=None, surprise=True)'
            )

    def test_rejects_attribute_calls(self) -> None:
        with self.assertRaisesRegex(RecordSyntaxError, "direct schema calls"):
            parse_python_block('os.system(command="whoami")')

    def test_rejects_comprehensions(self) -> None:
        with self.assertRaisesRegex(RecordSyntaxError, "disallowed syntax"):
            parse_python_block(
                '''IndexEntry(
                    anchors=[x for x in ["component:alpha"]],
                    summary="Alpha", path="decisions/x.md",
                    importance="L1_active_decision", status="active",
                    related=[], record_snapshot_id="SNAP-1",
                )'''
            )

    def test_rejects_wrong_enum(self) -> None:
        with self.assertRaisesRegex(RecordSyntaxError, "is not one of"):
            parse_python_block(VALID.replace('status="active"', 'status="mystery"'))

    def test_rejects_wrong_list_item_type(self) -> None:
        with self.assertRaisesRegex(RecordSyntaxError, "expected str"):
            parse_python_block(VALID.replace('["component:alpha"]', '[42]'))

    def test_lite_handoff_rejects_full_only_fields(self) -> None:
        block = '''HandoffCheckpoint(
            profile="lite", checkpoint_id="HANDOFF-1", task_id="TASK-1",
            status="ready", objective="Continue.", state_summary="Stable.",
            completed=[], remaining=["Run tests"], next_action="Run tests.",
            changed_files=[], verification=[], blockers=[],
            source_snapshot_id="SNAP-1", checkpoint_snapshot_id="SNAP-2",
            risks=[],
        )'''
        with self.assertRaisesRegex(RecordSyntaxError, "full-only fields"):
            parse_python_block(block)

    def test_lite_handoff_accepts_first_class_fact_ids(self) -> None:
        block = '''HandoffCheckpoint(
            profile="lite", checkpoint_id="HANDOFF-1", task_id="TASK-1",
            status="ready", objective="Continue.", state_summary="Stable.",
            completed=[], remaining=["Run tests"], next_action="run_tests",
            changed_files=[], verification=[], blockers=[],
            source_snapshot_id="SNAP-1", checkpoint_snapshot_id="SNAP-2",
            fact_ids=["fact:tests-pending"],
        )'''
        record = parse_python_block(block)
        self.assertEqual(["fact:tests-pending"], record.fields["fact_ids"])


if __name__ == "__main__":
    unittest.main()
