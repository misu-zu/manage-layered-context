"""Evaluate authorized writes against the resulting disposable v3 memory."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from evals.contracts import ContractError, write_json_result
from evals.live_eval import (
    DEFAULT_MODEL, WORKTREE_REF, _workspace_manifest, build_codex_command, build_project_fixture,
    materialize_runtime, run_codex,
)
from memory_transaction import _record, committed_snapshot
from memory_validation import transaction_warnings, validate_memory


TASK = "tasks/TASK-EXPORT.md"
HANDOFF = "handoffs/HANDOFF-EXPORT.md"
DECISION = "decisions/ADR-EXPORT.md"
LESSON = "lessons/LESSON-EXPORT.md"
CASES = {
    "record": (DECISION, "Record the accepted decision ADR-EXPORT at decisions/ADR-EXPORT.md: "
               "use verify_export_checksum before publication. Preserve fact ID fact:checksum-required."),
    "handoff": (HANDOFF, "Create a ready same-agent lite checkpoint HANDOFF-EXPORT for TASK-EXPORT at "
                "handoffs/HANDOFF-EXPORT.md. The fixture has been verified. The next action is "
                "verify_export_checksum. Link the task and the current continuation."),
    "promotion": (LESSON, "Review candidate LESSON-EXPORT against the two recorded checksum incidents. "
                  "If supported, promote it in place and cite both task IDs as evidence. "
                  "The required check is verify_export_checksum."),
}


def seed(workspace: Path) -> Path:
    task = {"id": "export-checkpoint", "query": "Verify export integrity", "records": [
        {"path": TASK, "anchor": "export", "kind": "task", "relevance": "required",
         "body": 'Task(fact="daily_export_checksum_failed", next_action="verify_export_checksum")'},
        {"path": "tasks/TASK-RETRY.md", "anchor": "retry", "kind": "task", "relevance": "required",
         "body": 'Task(fact="separate_retry_checksum_failed", next_action="verify_export_checksum")'},
        {"path": LESSON, "anchor": "export-check", "kind": "lesson", "relevance": "required",
         "index_status": "scratch", "body": 'Lesson(fact="missing_export_checksum", required_check="verify_export_checksum")'},
    ]}
    build_project_fixture(workspace / "project", task, "v3")
    return workspace / "project/memory"


def score_write(case: str, workspace: Path, before: dict, base: str) -> dict:
    root = workspace / "project/memory"
    findings = validate_memory(root, strict_v3=True, require_exact_tokens=True)
    errors = [*findings.errors, *transaction_warnings(root)]
    after = _workspace_manifest(workspace)
    changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    detail = CASES[case][0]
    allowed = {detail, "current.md", "index.md"}
    if case == "handoff":
        allowed.add(TASK)
    for path in changed:
        if path.startswith("scratch/"):
            continue
        relative = path.removeprefix("project/memory/")
        if path == relative or path not in after or (
            relative not in allowed and not relative.startswith(("indexes/", "patches/", ".transactions/"))
        ) or (relative.startswith(("patches/", ".transactions/")) and path in before):
            errors.append(f"unexpected change: {path}")
    if "project/memory/" + detail not in changed:
        errors.append(f"required record did not change: {detail}")
    if not errors:
        target = committed_snapshot(root)
        record = _record(root / detail).fields
        state = _record(root / "current.md").fields
        patches = [p for p in changed if p.startswith("project/memory/patches/")]
        if target <= base or not patches or not any(
            _record(workspace / p).fields["target_snapshot_id"] == target for p in patches
        ):
            errors.append("missing committed transaction evidence")
        if case == "record":
            ok = (record["status"] == "active" and record["choice"] == "verify_export_checksum"
                  and "fact:checksum-required" in record["fact_ids"])
        elif case == "handoff":
            names = {"HANDOFF-EXPORT", "handoff:HANDOFF-EXPORT", HANDOFF}
            task = _record(root / TASK)
            ok = (record["profile"] == "lite" and record["status"] == "ready" and record["task_id"] == "TASK-EXPORT"
                  and record["next_action"] == "verify_export_checksum" and record["source_snapshot_id"] == base
                  and record["checkpoint_snapshot_id"] == target and state["latest_handoff"] in names
                  and not names.isdisjoint(task.fields.get("handoff_refs", [])))
        else:
            evidence = {r.fields["ref_id"] for r in record["supporting_evidence"] if r.fields["ref_type"] == "task"}
            ok = (record["status"] == "active" and {"TASK-EXPORT", "TASK-RETRY"} <= evidence
                  and "verify_export_checksum" in record["required_checks"])
        if not ok:
            errors.append("canonical state does not satisfy the requested outcome")
    return {"case": case, "passed": not errors, "changed_files": changed, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", action="store_true", help="Build and validate each starting fixture only")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    results = []
    for case, (_, objective) in CASES.items():
        with tempfile.TemporaryDirectory(prefix="mlc-write-eval-") as tmp:
            workspace = Path(tmp)
            materialize_runtime(REPO, WORKTREE_REF, workspace / "skill")
            try:
                root = seed(workspace)
                findings = validate_memory(root, strict_v3=True, require_exact_tokens=True)
                if not findings.ok or args.preflight:
                    results.append({"case": case, "passed": findings.ok, "errors": findings.errors})
                    continue
                schema = workspace / "response.schema.json"
                schema.write_text(json.dumps({"type": "object", "properties": {"done": {"type": "boolean"}},
                                              "required": ["done"], "additionalProperties": False}))
                before = _workspace_manifest(workspace)
                base = committed_snapshot(root)
                prompt = (f"Use skill/SKILL.md and the standard record mode in project/memory. {objective} "
                          "Use scratch/ for any drafts. Return JSON with a boolean done field.")
                command = build_codex_command(args.codex_bin, workspace, schema, prompt, args.model, "workspace-write")
                _, usage, _ = run_codex(command, 900)
                results.append({**score_write(case, workspace, before, base), "usage": usage})
            except ContractError as exc:
                results.append({"case": case, "passed": False, "failure": exc.as_dict()})
    passed = all(r["passed"] for r in results)
    print(write_json_result({"preflight": args.preflight, "passed": passed, "cases": results}, args.output))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
