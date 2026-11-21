from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import SCRIPTS, call, make_project, write
import memory_transaction as txn
from memory_model import RecordSyntaxError, TokenMeasurement, parse_python_block
from memory_recall import recall
from memory_validation import validate_memory


BASE = "SNAP-20260830-010000"
TARGET = "SNAP-20260830-010100"
DETAIL = "decisions/ADR-0001-alpha.md"
CONTROLS = ["current.md", "index.md"]


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = make_project(Path(temp.name))
        self.patch = "PATCH-review"

    def begin(self, *, mode="standard", files=(), deletes=(), target=TARGET):
        return txn.begin_transaction(self.root, patch_id=self.patch, mode=mode, operation="update",
                                     target_snapshot_id=target, files=[*CONTROLS, *files], delete_files=deletes)

    def advance(self):
        for relative in CONTROLS:
            p = txn.staged_path(self.root, self.patch, relative)
            write(p, p.read_text().replace(BASE, TARGET, 1))

    def test_delete_mode_follows_actual_files_at_begin_and_add(self):
        for mode in ("minimal", "standard"):
            with self.subTest(mode=mode), self.assertRaisesRegex(txn.TransactionError, "detailed"):
                self.begin(mode=mode, deletes=iter([DETAIL]))
        with self.assertRaisesRegex(txn.TransactionError, "archive"):
            self.begin(files=["archive/test.md"])
        self.begin()
        with self.assertRaisesRegex(txn.TransactionError, "detailed"):
            txn.add_transaction_files(self.root, self.patch, [], [DETAIL])
        self.assertTrue((self.root / DETAIL).is_file())

    def test_stale_shard_and_disguised_reorganization(self):
        self.begin(files=["indexes/general.md"])
        self.advance()
        root_index = txn.staged_path(self.root, self.patch, "index.md")
        shard_path = txn.staged_path(self.root, self.patch, "indexes/general.md")
        index, shard = txn._record(root_index), txn._record(shard_path)
        shard.fields["entries"] = index.fields.pop("entries")
        index.fields["entries"] = []
        write(root_index, txn.render_markdown("Root", index))
        write(shard_path, txn.render_markdown("Shard", shard))
        with self.assertRaisesRegex(txn.TransactionError, "moving indexed entries"):
            txn.commit_transaction(self.root, self.patch)
        txn.abort_transaction(self.root, self.patch)
        self.begin(mode="detailed", files=["indexes/general.md"])
        self.advance()
        write(shard_path, shard_path.read_text() + "\nUpdated routing notes.\n")
        with self.assertRaisesRegex(txn.TransactionError, "shard must use target"):
            txn.commit_transaction(self.root, self.patch)

    def test_invalid_targets_rejected_before_lock(self):
        for target in ("zzz", "SNAP-20260230-120000", "SNAP-99990101-000000", TARGET + "-Bad"):
            with self.subTest(target=target), self.assertRaisesRegex(txn.TransactionError, "snapshot"):
                self.begin(target=target)
            self.assertFalse((self.root / ".update-lock").exists())

    def test_preparation_interruption_can_recover(self):
        original_write = txn._json_write
        def interrupt(path, value, **kwargs):
            if path.name == "manifest.json":
                raise SystemExit
            return original_write(path, value, **kwargs)
        with patch.object(txn, "_json_write", side_effect=interrupt), self.assertRaises(SystemExit):
            self.begin()
        self.assertEqual(self.patch, json.loads((self.root / ".update-lock").read_text())["patch_id"])
        with self.assertRaises(txn.TransactionError):
            txn.recover_transaction(self.root, "PATCH-other")
        self.assertEqual("clean_base", txn.recover_transaction(self.root, self.patch)["disposition"])
        self.begin()

    def test_lost_prepared_manifest_is_ambiguous(self):
        self.begin()
        (self.root / ".staging" / self.patch / "manifest.json").unlink()
        with self.assertRaises(txn.TransactionError):
            txn.recover_transaction(self.root, self.patch)
        self.assertTrue((self.root / ".update-lock").exists())

    def test_full_budget_validation_and_unchanged_shard_during_commit(self):
        shard = self.root / "indexes/general.md"
        write(shard, shard.read_text() + "\nExisting long notes.\n")
        def measure(text, *args, **kwargs):
            return TokenMeasurement(2500 if "Existing long notes." in text else 120, "o200k_base", True, "test")
        with patch("memory_validation.count_tokens", side_effect=measure):
            self.assertFalse(validate_memory(self.root).ok)
            self.begin()
            self.advance()
            self.assertEqual("committed", txn.commit_transaction(self.root, self.patch)["status"])

    def test_technical_prose_and_real_template_values(self):
        path = self.root / DETAIL
        text = path.read_text()
        for value in ("p95 < 200ms", "detail -> shard -> index", "A, B... C", "List<String>", "Use YYYY-MM-DD"):
            write(path, text.replace("The accepted fixture requires amber.", value))
            self.assertTrue(validate_memory(self.root).ok, value)
        write(path, text.replace("The accepted fixture requires amber.", "<current goal>"))
        self.assertIn("unresolved placeholder", str(validate_memory(self.root).errors))

    def test_integrity_reports_distinct_errors(self):
        path = self.root / DETAIL
        write(self.root / "decisions/other.md", path.read_text())
        write(path, path.read_text().replace('status="active"', 'status="deprecated"'))
        index = self.root / "index.md"
        write(index, index.read_text().replace('record_snapshot_id="' + BASE, 'record_snapshot_id="' + TARGET))
        current = self.root / "current.md"
        write(current, current.read_text().replace("decision:ADR-0001", "decision:ADR-9999"))
        messages = str(validate_memory(self.root).errors)
        for error in ("duplicate record ID", "filename", "index status", "missing decisions", "newer than commit boundary"):
            self.assertIn(error, messages)

    def test_parser_rejects_duplicate_and_nonstring_keys(self):
        for keys in ('["x"]: []', '"x": [], "x": []'):
            with self.subTest(keys=keys), self.assertRaises(RecordSyntaxError):
                parse_python_block("AnchorRegistry(aliases={" + keys + "}, deprecated={})")

    def test_credentials_in_prose_and_preimages_are_redacted(self):
        path = self.root / DETAIL
        secret = "ghp_" + "A" * 36
        self.begin(mode="forensic", files=[DETAIL])
        self.advance()
        staged = txn.staged_path(self.root, self.patch, DETAIL)
        write(staged, staged.read_text() + "\n" + secret)
        with self.assertRaisesRegex(txn.TransactionError, "possible credential") as caught:
            txn.commit_transaction(self.root, self.patch)
        self.assertNotIn(secret, str(caught.exception))
        txn.abort_transaction(self.root, self.patch)
        write(path, path.read_text() + "\n" + secret)
        with self.assertRaisesRegex(txn.TransactionError, "original file"):
            self.begin(mode="forensic", deletes=[DETAIL])
        self.assertFalse((self.root / ".transactions").exists())
        self.assertNotIn(secret, str(validate_memory(self.root).as_dict()))

    def test_v3_missing_config_is_error_by_default(self):
        (self.root / "config.json").unlink()
        self.assertIn("config.json", str(validate_memory(self.root).errors))

    def test_recall_exposes_uncertain_boundary_and_rejects_stopwords(self):
        self.begin()
        result = recall(self.root, "who is the legal owner of the project")
        self.assertTrue(result["no_confident_match"])
        self.assertTrue(result["boundary_matches"])
        self.assertEqual("low", result["coverage"])
        self.assertIn(".update-lock", str(validate_memory(self.root).warnings))
        index = self.root / "index.md"
        write(index, index.read_text().replace(BASE, TARGET, 1))
        result = recall(self.root, "amber")
        self.assertFalse(result["boundary_matches"])
        self.assertEqual(("active", BASE), (result["candidates"][0]["status"], result["candidates"][0]["snapshot_id"]))

    def test_unindexed_manifest_archive_is_searchable(self):
        record = call("ArchiveRecord", id="HISTORY", anchors=[], fact_ids=[], archived_at="2026-08-30",
                      summary="Rotate TLS certificates", affected_records=[], details=[], mistakes=[], source_refs=[])
        write(self.root / "archive/HISTORY.md", txn.render_markdown("Certificate rotation", record))
        manifest = call("ArchiveEntry", id="HISTORY", anchors=[], summary="Renewal", path="archive/HISTORY.md",
                        affected_records=[], mistake_refs=[], source_refs=[])
        write(self.root / "archive/manifest.md", txn.render_markdown("Archive", call("ArchiveManifest", entries=[manifest])))
        self.assertTrue(validate_memory(self.root).ok)
        self.assertEqual("archive/HISTORY.md", recall(self.root, "rotate TLS certificates")["candidates"][0]["path"])

    def test_cli_init_put_recall_validate_and_abort_on_bad_input(self):
        root = self.root.parent / "new-memory"
        cli = [sys.executable, str(SCRIPTS / "memoryctl.py")]
        def run(*args):
            return subprocess.run([*cli, *map(str, args)], capture_output=True, text=True)
        self.assertEqual(0, run("init", root, "--name", '引き継ぎ "A"').returncode)
        self.assertNotEqual(0, run("init", root).returncode)
        self.assertEqual(0, run("record", "put", root, self.root / DETAIL, "--path", DETAIL).returncode)
        result = json.loads(run("recall", root, "--query", "amber", "--json").stdout)
        self.assertEqual(DETAIL, result["candidates"][0]["path"])
        self.assertEqual(result["snapshot_id"], result["candidates"][0]["snapshot_id"])
        self.assertIn("decision:ADR-0001", txn._record(root / "current.md").fields["active_decisions"])
        self.assertEqual(0, run("validate", root, "--strict-v3").returncode)
        source = self.root / DETAIL
        write(source, source.read_text().replace("Alpha mode", "<current goal>"))
        self.assertNotEqual(0, run("record", "put", root, source, "--path", DETAIL).returncode)
        self.assertFalse((root / ".update-lock").exists())
        self.assertEqual(result["snapshot_id"], txn.committed_snapshot(root))

    def test_cli_auto_target_and_add(self):
        cli = [sys.executable, str(SCRIPTS / "memoryctl.py"), "txn"]
        args = [str(self.root), "--patch-id", self.patch]
        result = subprocess.check_output([*cli, "begin", *args, "--auto-target", "--file", "current.md", "--file", "index.md"])
        self.assertGreater(json.loads(result)["target_snapshot_id"], BASE)
        result = subprocess.check_output([*cli, "add", *args, "--file", DETAIL])
        self.assertIn(DETAIL, [entry["path"] for entry in json.loads(result)["files"]])
        self.assertTrue(txn.staged_path(self.root, self.patch, DETAIL).is_file())
        committed = json.loads(subprocess.check_output([*cli, "commit", *args]))
        self.assertEqual(committed["target_snapshot_id"], txn.committed_snapshot(self.root))
