from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from evals.contracts import count_tokens, require_exact_tokenizer
from evals.deterministic_eval import (
    collect_preflight,
    generate_fixture,
    inspect_fixture,
    run_evaluation,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE_MEMORYCTL = REPO_ROOT / "evals" / "fixtures" / "fake_memoryctl.py"


class DeterministicFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.encoding = require_exact_tokenizer()

    def test_generated_histories_are_bounded_and_fully_reachable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mlc-fixture-test-") as temporary:
            root = Path(temporary)
            for task_count in (100, 300, 1000):
                with self.subTest(task_count=task_count):
                    fixture = generate_fixture(root / str(task_count), task_count)
                    inspection = inspect_fixture(
                        fixture,
                        lambda text: count_tokens(self.encoding, text),
                    )
                    self.assertTrue(inspection["current_bounded"])
                    self.assertTrue(inspection["root_index_bounded"])
                    self.assertFalse(inspection["root_index_contains_detail_paths"])
                    self.assertTrue(inspection["topic_indexes_bounded"])
                    self.assertTrue(inspection["all_topic_indexes_reachable"])
                    self.assertEqual(task_count, inspection["reachable_detail_count"])
                    self.assertTrue(inspection["all_details_reachable"])

    def test_contract_fake_produces_perfect_recall_and_exact_budgets(self) -> None:
        result = run_evaluation(
            REPO_ROOT,
            FAKE_MEMORYCTL,
            "python",
            (100,),
            5,
            self.encoding,
        )
        self.assertEqual("passed", result["status"])
        self.assertEqual(1.0, result["aggregate"]["recall_at_3"]["rate"])
        self.assertTrue(result["aggregate"]["all_exact_budget_checks_passed"])

    def test_missing_planned_cli_is_an_explicit_preflight_failure(self) -> None:
        failures, _ = collect_preflight(
            REPO_ROOT,
            REPO_ROOT / "scripts" / "does-not-exist.py",
            "python",
        )
        codes = {failure["code"] for failure in failures}
        self.assertIn("missing_memoryctl", codes)


if __name__ == "__main__":
    unittest.main()
