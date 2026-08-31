from __future__ import annotations

import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import memoryctl  # noqa: E402


class MemoryctlInterfaceTests(unittest.TestCase):
    def test_target_version_three_enables_strict_v3_validation(self) -> None:
        args = memoryctl.build_parser().parse_args(
            ["validate", ".", "--target-version", "3", "--json"]
        )
        findings = type(
            "Findings",
            (),
            {"ok": True, "as_dict": lambda self: {"ok": True, "errors": [], "warnings": [], "info": []}},
        )()
        with patch.object(memoryctl, "validate_memory", return_value=findings) as validate:
            with redirect_stdout(StringIO()):
                self.assertEqual(0, args.handler(args))
        self.assertTrue(validate.call_args.kwargs["strict_v3"])
        self.assertFalse(validate.call_args.kwargs["strict_v2"])

    def test_expand_to_eight_is_public_recall_option(self) -> None:
        args = memoryctl.build_parser().parse_args(
            ["recall", ".", "--query", "resume", "--top", "3", "--expand-to", "8"]
        )
        self.assertEqual(3, args.top)
        self.assertEqual(8, args.expand_to)

    def test_schema_marks_compatibility_fields_as_required_in_v3(self) -> None:
        args = memoryctl.build_parser().parse_args(["schema", "OpenIssue", "--json"])
        stream = StringIO()
        with redirect_stdout(stream):
            self.assertEqual(0, args.handler(args))
        schema = json.loads(stream.getvalue())["OpenIssue"]
        self.assertTrue(schema["fact_ids"]["required_in_v3"])
        self.assertTrue(schema["contradiction_id"]["required_in_v3"])
        self.assertFalse(schema["fact_ids"]["required"])

    def test_migration_dry_run_and_apply_are_mutually_exclusive(self) -> None:
        parser = memoryctl.build_parser()
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    [
                        "migrate",
                        ".",
                        "--target-snapshot",
                        "SNAP-TEST",
                        "--dry-run",
                        "--apply",
                    ]
                )


if __name__ == "__main__":
    unittest.main()
