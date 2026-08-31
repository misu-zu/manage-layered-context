from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from memory_model import ParsedCall, parse_markdown  # noqa: E402
from memory_transaction import (  # noqa: E402
    SimulatedInterruption,
    TransactionError,
    abort_transaction,
    begin_transaction,
    commit_transaction,
    migrate_v2_to_v3,
    recover_transaction,
    staged_path,
    render_markdown,
)
from memory_validation import validate_memory  # noqa: E402
from support import entry, make_project, replace_snapshot, write  # noqa: E402


BASE = "SNAP-20260830-010000"
TARGET = "SNAP-20260830-010100"


def stage_standard_change(memory: Path, patch_id: str) -> None:
    for relative in ("current.md", "index.md"):
        path = staged_path(memory, patch_id, relative)
        replace_snapshot(path, BASE, TARGET)
    decision = staged_path(memory, patch_id, "decisions/ADR-0001-alpha.md")
    write(decision, decision.read_text(encoding="utf-8").replace("amber mode", "amber-v2 mode"))


class TransactionTests(unittest.TestCase):
    def test_detailed_delete_is_staged_and_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = make_project(Path(raw), version=3, snapshot=BASE)
            patch_id = "PATCH-20260830-010100-delete"
            begin_transaction(
                memory,
                patch_id=patch_id,
                mode="detailed",
                operation="delete",
                target_snapshot_id=TARGET,
                files=["current.md", "index.md"],
                delete_files=["decisions/ADR-0001-alpha.md"],
            )
            current_path = staged_path(memory, patch_id, "current.md")
            current = parse_markdown(current_path.read_text(encoding="utf-8"))[0]
            current_fields = dict(current.fields)
            current_fields["snapshot_id"] = TARGET
            current_fields["active_decisions"] = []
            write(current_path, render_markdown("Current Project Memory", ParsedCall("ProjectState", current_fields, 1)))
            index_path = staged_path(memory, patch_id, "index.md")
            index = parse_markdown(index_path.read_text(encoding="utf-8"))[0]
            index_fields = dict(index.fields)
            index_fields["snapshot_id"] = TARGET
            index_fields["entries"] = []
            write(index_path, render_markdown("Active Memory Index", ParsedCall("ActiveMemoryIndex", index_fields, 1)))
            result = commit_transaction(memory, patch_id)
            self.assertEqual("committed", result["status"])
            self.assertFalse((memory / "decisions" / "ADR-0001-alpha.md").exists())
            self.assertEqual([], validate_memory(memory, strict_v3=True).errors)

    def test_changed_record_requires_target_snapshot_in_index_entry(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = make_project(Path(raw), version=3, snapshot=BASE)
            patch_id = "PATCH-20260830-010100-stale-entry"
            begin_transaction(
                memory,
                patch_id=patch_id,
                mode="standard",
                operation="update",
                target_snapshot_id=TARGET,
                files=["current.md", "index.md", "decisions/ADR-0001-alpha.md"],
            )
            replace_snapshot(staged_path(memory, patch_id, "current.md"), BASE, TARGET)
            index = staged_path(memory, patch_id, "index.md")
            write(index, index.read_text(encoding="utf-8").replace(BASE, TARGET, 1))
            decision = staged_path(memory, patch_id, "decisions/ADR-0001-alpha.md")
            write(decision, decision.read_text(encoding="utf-8").replace("amber mode", "amber-v2 mode"))
            with self.assertRaisesRegex(TransactionError, "indexed at target snapshot"):
                commit_transaction(memory, patch_id)

    def test_standard_transaction_commits_in_one_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = make_project(Path(raw), version=3, snapshot=BASE)
            patch_id = "PATCH-20260830-010100-alpha"
            begin_transaction(
                memory,
                patch_id=patch_id,
                mode="standard",
                operation="update",
                target_snapshot_id=TARGET,
                files=["current.md", "index.md", "decisions/ADR-0001-alpha.md"],
            )
            stage_standard_change(memory, patch_id)
            result = commit_transaction(memory, patch_id)
            self.assertEqual("committed", result["status"])
            self.assertFalse((memory / ".update-lock").exists())
            self.assertFalse((memory / ".staging").exists())
            self.assertTrue((memory / "patches" / f"{patch_id}.md").is_file())
            self.assertEqual([], validate_memory(memory, strict_v3=True).errors)

    def test_minimal_mode_rejects_multiple_detail_records(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = make_project(Path(raw), version=3, snapshot=BASE)
            with self.assertRaisesRegex(TransactionError, "at most one"):
                begin_transaction(
                    memory,
                    patch_id="PATCH-20260830-010100-too-many",
                    mode="minimal",
                    operation="update",
                    target_snapshot_id=TARGET,
                    files=[
                        "current.md",
                        "index.md",
                        "decisions/ADR-0001-alpha.md",
                        "issues/ISSUE-0001.md",
                    ],
                )

    def test_existing_lock_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = make_project(Path(raw), version=3, snapshot=BASE)
            begin_transaction(
                memory,
                patch_id="PATCH-20260830-010100-first",
                mode="minimal",
                operation="update",
                target_snapshot_id=TARGET,
                files=["current.md", "index.md"],
            )
            original = (memory / ".update-lock").read_bytes()
            with self.assertRaisesRegex(TransactionError, "already exists"):
                begin_transaction(
                    memory,
                    patch_id="PATCH-20260830-010101-second",
                    mode="minimal",
                    operation="update",
                    target_snapshot_id="SNAP-20260830-010101",
                    files=["current.md", "index.md"],
                )
            self.assertEqual(original, (memory / ".update-lock").read_bytes())

    def test_prepared_transaction_can_abort_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = make_project(Path(raw), version=3, snapshot=BASE)
            patch_id = "PATCH-20260830-010100-abort"
            before = (memory / "current.md").read_bytes()
            begin_transaction(
                memory,
                patch_id=patch_id,
                mode="minimal",
                operation="update",
                target_snapshot_id=TARGET,
                files=["current.md", "index.md"],
            )
            result = abort_transaction(memory, patch_id)
            self.assertEqual("aborted", result["status"])
            self.assertEqual(before, (memory / "current.md").read_bytes())

    def test_recovery_finalizes_when_all_target_hashes_match(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = make_project(Path(raw), version=3, snapshot=BASE)
            patch_id = "PATCH-20260830-010100-finalize"
            begin_transaction(
                memory,
                patch_id=patch_id,
                mode="standard",
                operation="update",
                target_snapshot_id=TARGET,
                files=["current.md", "index.md", "decisions/ADR-0001-alpha.md"],
            )
            stage_standard_change(memory, patch_id)
            with self.assertRaises(SimulatedInterruption):
                commit_transaction(memory, patch_id, fail_after=4)
            result = recover_transaction(memory, patch_id)
            self.assertEqual("finalized_target", result["disposition"])
            self.assertEqual([], validate_memory(memory, strict_v3=True).errors)

    def test_recovery_retains_evidence_for_mixed_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = make_project(Path(raw), version=3, snapshot=BASE)
            patch_id = "PATCH-20260830-010100-mixed"
            begin_transaction(
                memory,
                patch_id=patch_id,
                mode="standard",
                operation="update",
                target_snapshot_id=TARGET,
                files=["current.md", "index.md", "decisions/ADR-0001-alpha.md"],
            )
            stage_standard_change(memory, patch_id)
            with self.assertRaises(SimulatedInterruption):
                commit_transaction(memory, patch_id, fail_after=1)
            with self.assertRaisesRegex(TransactionError, "mixed or ambiguous"):
                recover_transaction(memory, patch_id)
            self.assertTrue((memory / ".update-lock").is_file())
            self.assertTrue((memory / ".staging" / patch_id / "manifest.json").is_file())

    def test_interruption_at_each_ordered_boundary_recovers_or_stops_safely(self) -> None:
        for fail_after, expected in ((1, "stopped"), (3, "stopped"), (4, "stopped"), (5, "finalized")):
            with self.subTest(fail_after=fail_after), tempfile.TemporaryDirectory() as raw:
                memory = make_project(Path(raw), version=3, snapshot=BASE)
                patch_id = f"PATCH-20260830-010100-boundary-{fail_after}"
                begin_transaction(
                    memory,
                    patch_id=patch_id,
                    mode="standard",
                    operation="update",
                    target_snapshot_id=TARGET,
                    files=[
                        "current.md",
                        "index.md",
                        "indexes/general.md",
                        "decisions/ADR-0001-alpha.md",
                    ],
                )
                replace_snapshot(staged_path(memory, patch_id, "current.md"), BASE, TARGET)
                decision_path = staged_path(memory, patch_id, "decisions/ADR-0001-alpha.md")
                write(
                    decision_path,
                    decision_path.read_text(encoding="utf-8").replace("amber mode", "amber-v2 mode"),
                )
                root_index_path = staged_path(memory, patch_id, "index.md")
                root_index = parse_markdown(root_index_path.read_text(encoding="utf-8"))[0]
                root_fields = dict(root_index.fields)
                root_fields["snapshot_id"] = TARGET
                moved_entry = root_fields["entries"][0]
                root_fields["entries"] = []
                write(
                    root_index_path,
                    render_markdown("Active Memory Index", ParsedCall("ActiveMemoryIndex", root_fields, 1)),
                )
                shard_path = staged_path(memory, patch_id, "indexes/general.md")
                shard = parse_markdown(shard_path.read_text(encoding="utf-8"))[0]
                shard_fields = dict(shard.fields)
                shard_fields["snapshot_id"] = TARGET
                moved_fields = dict(moved_entry.fields)
                moved_fields["record_snapshot_id"] = TARGET
                shard_fields["entries"] = [ParsedCall("IndexEntry", moved_fields, 1)]
                write(
                    shard_path,
                    render_markdown("Topic Index: General", ParsedCall("TopicIndex", shard_fields, 1)),
                )
                with self.assertRaises(SimulatedInterruption):
                    commit_transaction(memory, patch_id, fail_after=fail_after)
                if expected == "finalized":
                    result = recover_transaction(memory, patch_id)
                    self.assertEqual("finalized_target", result["disposition"])
                    self.assertEqual([], validate_memory(memory, strict_v3=True).errors)
                else:
                    with self.assertRaisesRegex(TransactionError, "mixed or ambiguous"):
                        recover_transaction(memory, patch_id)
                    self.assertTrue((memory / ".update-lock").is_file())


class MigrationTests(unittest.TestCase):
    def test_migration_is_dry_run_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = make_project(Path(raw), version=2, snapshot=BASE)
            before = (memory / "index.md").read_bytes()
            result = migrate_v2_to_v3(memory, target_snapshot_id=TARGET)
            self.assertFalse(result["applied"])
            self.assertEqual(before, (memory / "index.md").read_bytes())
            self.assertFalse((memory / "config.json").exists())

    def test_migration_apply_requires_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = make_project(Path(raw), version=2, snapshot=BASE)
            with self.assertRaisesRegex(TransactionError, "--confirm"):
                migrate_v2_to_v3(
                    memory,
                    target_snapshot_id=TARGET,
                    apply=True,
                    mode="forensic",
                )

    def test_forensic_migration_produces_valid_v3_and_audit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = make_project(Path(raw), version=2, snapshot=BASE)
            result = migrate_v2_to_v3(
                memory,
                target_snapshot_id=TARGET,
                apply=True,
                mode="forensic",
                confirm="APPLY_V3_MIGRATION",
            )
            self.assertTrue(result["applied"])
            self.assertTrue((memory / "config.json").is_file())
            self.assertTrue((memory / ".transactions" / result["patch_id"] / "manifest.json").is_file())
            self.assertEqual([], validate_memory(memory, strict_v3=True).errors)
            migrated = parse_markdown(
                (memory / "decisions" / "ADR-0001-alpha.md").read_text(encoding="utf-8")
            )[0]
            self.assertEqual([], migrated.fields["fact_ids"])

    def test_migration_adds_conflict_identifier_without_inventing_fact_ids(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = make_project(Path(raw), version=2, snapshot=BASE)
            conflict_path = "conflicts/CONFLICT-0001.md"
            conflict = ParsedCall(
                "Conflict",
                {
                    "id": "CONFLICT-0001",
                    "anchors": ["conflict:CONFLICT-0001", "concept:alpha"],
                    "topic": "Alpha",
                    "competing_positions": ["amber", "blue"],
                    "reason": "Sources disagree.",
                    "resolution_status": "unresolved",
                    "next_action": None,
                    "source_refs": [],
                    "resolved_by": None,
                },
                1,
            )
            write(memory / conflict_path, render_markdown("CONFLICT-0001", conflict))
            index = parse_markdown((memory / "index.md").read_text(encoding="utf-8"))[0]
            index_fields = dict(index.fields)
            index_fields["entries"] = [
                *index_fields["entries"],
                entry(
                    conflict_path,
                    BASE,
                    anchors=["concept:alpha", "conflict:CONFLICT-0001"],
                ),
            ]
            write(
                memory / "index.md",
                render_markdown("Memory Index", ParsedCall("MemoryIndex", index_fields, 1)),
            )

            migrate_v2_to_v3(
                memory,
                target_snapshot_id=TARGET,
                apply=True,
                mode="forensic",
                confirm="APPLY_V3_MIGRATION",
            )
            migrated = parse_markdown((memory / conflict_path).read_text(encoding="utf-8"))[0]
            self.assertEqual([], migrated.fields["fact_ids"])
            self.assertEqual("CONFLICT-0001", migrated.fields["contradiction_id"])
            self.assertEqual([], validate_memory(memory, strict_v3=True).errors)


if __name__ == "__main__":
    unittest.main()
