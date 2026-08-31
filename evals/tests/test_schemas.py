from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from evals.contracts import RESULT_SCHEMA_VERSION


SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "schemas"
REPO_ROOT = SCHEMA_ROOT.parents[1]


class SchemaTests(unittest.TestCase):
    def test_every_schema_is_valid_json_with_an_object_root(self) -> None:
        schemas = sorted(SCHEMA_ROOT.glob("*.schema.json"))
        self.assertGreaterEqual(len(schemas), 4)
        for path in schemas:
            with self.subTest(path=path.name):
                parsed = json.loads(path.read_text(encoding="utf-8"))
                self.assertIsInstance(parsed, dict)
                self.assertEqual("object", parsed["type"])
                if path.name != "live-agent-output.schema.json":
                    self.assertEqual("https://json-schema.org/draft/2020-12/schema", parsed["$schema"])

    def test_live_response_schema_uses_only_supported_subset(self) -> None:
        path = SCHEMA_ROOT / "live-agent-output.schema.json"
        parsed = json.loads(path.read_text(encoding="utf-8"))
        forbidden = {"$schema", "$id", "uniqueItems", "minLength", "maxItems"}

        def visit(value: object) -> None:
            if isinstance(value, dict):
                self.assertFalse(forbidden & set(value), f"unsupported keyword in {path.name}")
                for item in value.values():
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(parsed)

    def test_live_response_schema_separates_typed_identifiers(self) -> None:
        parsed = json.loads(
            (SCHEMA_ROOT / "live-agent-output.schema.json").read_text(encoding="utf-8")
        )
        properties = parsed["properties"]
        self.assertIn("fact_ids", properties)
        self.assertIn("decision_id", properties)
        self.assertIn("contradiction_id", properties)
        self.assertIn("next_action_id", properties)
        self.assertNotIn("facts", properties)
        self.assertNotIn("decision", properties)

    def test_result_schemas_match_the_evaluator_contract_version(self) -> None:
        for name in ("deterministic-result.schema.json", "live-result.schema.json"):
            with self.subTest(schema=name):
                parsed = json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))
                self.assertEqual(RESULT_SCHEMA_VERSION, parsed["properties"]["schema_version"]["const"])

    def test_result_schemas_are_not_hidden_by_gitignore(self) -> None:
        for name in ("deterministic-result.schema.json", "live-result.schema.json"):
            path = (SCHEMA_ROOT / name).relative_to(REPO_ROOT)
            with self.subTest(schema=name):
                completed = subprocess.run(
                    ["git", "check-ignore", "--no-index", "--quiet", path.as_posix()],
                    cwd=REPO_ROOT,
                    check=False,
                )
                self.assertEqual(1, completed.returncode)


if __name__ == "__main__":
    unittest.main()
