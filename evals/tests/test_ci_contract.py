from __future__ import annotations

import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
REQUIREMENTS = REPO_ROOT / "requirements-ci.txt"


class CiContractTests(unittest.TestCase):
    def test_workflow_parses_and_actions_are_sha_pinned(self) -> None:
        document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        self.assertIsInstance(document, dict)
        jobs = document.get("jobs")
        self.assertIsInstance(jobs, dict)
        actions: list[str] = []
        for job in jobs.values():
            for step in job.get("steps", []):
                if "uses" in step:
                    actions.append(step["uses"])
        self.assertTrue(actions)
        for action in actions:
            with self.subTest(action=action):
                self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")

    def test_ci_runs_runtime_eval_budget_and_synthetic_gates(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        for required in (
            "unittest discover -s tests",
            "unittest discover -s evals/tests",
            "memoryctl.py budget . --require-exact --json",
            "--history-sizes 100 300 1000",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)

    def test_checkout_fetches_pinned_v2_history(self) -> None:
        document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        jobs = document["jobs"]
        checkout_steps = [
            step
            for job in jobs.values()
            for step in job.get("steps", [])
            if str(step.get("uses", "")).startswith("actions/checkout@")
        ]
        self.assertTrue(checkout_steps)
        for step in checkout_steps:
            with self.subTest(step=step.get("name", "checkout")):
                self.assertEqual(0, step.get("with", {}).get("fetch-depth"))

    def test_ci_dependencies_are_exactly_pinned(self) -> None:
        requirements = [
            line.strip()
            for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertTrue(requirements)
        for requirement in requirements:
            with self.subTest(requirement=requirement):
                self.assertRegex(requirement, r"^[A-Za-z0-9_.-]+==[^=\s]+$")


if __name__ == "__main__":
    unittest.main()
