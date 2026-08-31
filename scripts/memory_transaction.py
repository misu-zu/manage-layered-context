#!/usr/bin/env python3
"""Exclusive staged transactions, recovery, and explicit v2-to-v3 migration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from memory_model import (
    PYTHON_FENCE_RE,
    V3_REQUIRED_FIELDS,
    ParsedCall,
    config_json,
    count_tokens,
    load_config,
    parse_markdown,
)
from memory_validation import Findings, primary_anchor, validate_memory


LOCK_NAME = ".update-lock"
STAGING_NAME = ".staging"
AUDIT_NAME = ".transactions"
MODES = ("minimal", "standard", "detailed", "forensic")
OPERATIONS = (
    "create",
    "update",
    "archive",
    "delete",
    "index_reorg",
    "merge",
    "migrate",
    "irreversible",
    "security",
)
HIGH_RISK_OPERATIONS = {"migrate", "irreversible", "security"}
DETAILED_OPERATIONS = {"archive", "delete", "index_reorg", "merge"}
CONTROL_PATHS = {"current.md", "index.md"}


class TransactionError(RuntimeError):
    """Raised when a transaction cannot proceed safely."""


class SimulatedInterruption(TransactionError):
    """Test-only deterministic interruption after one or more committed writes."""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_write(path: Path, value: Any, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "x" if exclusive else "w"
    with path.open(mode, encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _json_read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TransactionError(f"cannot read transaction control file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TransactionError(f"transaction control file is not an object: {path}")
    return value


def _safe_relative(root: Path, raw: str) -> tuple[str, Path]:
    normalized = raw.replace("\\", "/")
    path = Path(normalized)
    if path.is_absolute() or normalized.startswith("/"):
        raise TransactionError(f"transaction path must be relative: {raw}")
    if not normalized or normalized in {".", ".."}:
        raise TransactionError(f"invalid transaction path: {raw}")
    if any(part in {"", ".", ".."} for part in PureParts(normalized)):
        raise TransactionError(f"transaction path contains unsafe segments: {raw}")
    if normalized == LOCK_NAME or normalized.startswith(f"{STAGING_NAME}/") or normalized.startswith(f"{AUDIT_NAME}/"):
        raise TransactionError(f"transaction cannot target control path: {raw}")
    candidate = (root / Path(normalized)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise TransactionError(f"transaction path escapes memory root: {raw}") from exc
    return normalized, candidate


def PureParts(path: str) -> tuple[str, ...]:
    return tuple(path.replace("\\", "/").split("/"))


def _snapshot(path: Path) -> str:
    try:
        calls = parse_markdown(path.read_text(encoding="utf-8"), path.as_posix())
    except (OSError, UnicodeError, ValueError) as exc:
        raise TransactionError(f"cannot read snapshot from {path}: {exc}") from exc
    if len(calls) != 1 or "snapshot_id" not in calls[0].fields:
        raise TransactionError(f"missing snapshot_id in {path}")
    return calls[0].fields["snapshot_id"]


def committed_snapshot(root: Path) -> str:
    current = _snapshot(root / "current.md")
    index = _snapshot(root / "index.md")
    if current != index:
        raise TransactionError(f"commit boundary mismatch: current.md={current} index.md={index}")
    return current


def _mode_allows(mode: str, operation: str, files: list[str]) -> None:
    if mode not in MODES:
        raise TransactionError(f"unknown record mode: {mode}")
    if operation not in OPERATIONS:
        raise TransactionError(f"unknown transaction operation: {operation}")
    if operation in HIGH_RISK_OPERATIONS and mode != "forensic":
        raise TransactionError(f"operation {operation} requires forensic mode")
    if operation in DETAILED_OPERATIONS and mode not in {"detailed", "forensic"}:
        raise TransactionError(f"operation {operation} requires detailed or forensic mode")
    if mode == "minimal":
        if operation not in {"create", "update"}:
            raise TransactionError("minimal mode allows only create or update")
        details = [
            path
            for path in files
            if path not in CONTROL_PATHS
            and not path.startswith("indexes/")
            and path != "config.json"
            and not path.startswith("patches/")
        ]
        if len(details) > 1:
            raise TransactionError("minimal mode allows at most one canonical detail record")
        if any(path.startswith("archive/") for path in files):
            raise TransactionError("minimal mode cannot archive records")


def _transaction_paths(root: Path, patch_id: str) -> tuple[Path, Path, Path]:
    lock = root / LOCK_NAME
    staging = root / STAGING_NAME / patch_id
    manifest = staging / "manifest.json"
    return lock, staging, manifest


def begin_transaction(
    root: Path,
    *,
    patch_id: str,
    mode: str,
    operation: str,
    target_snapshot_id: str,
    files: Iterable[str],
    delete_files: Iterable[str] = (),
    owner: str = "memoryctl",
) -> dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise TransactionError(f"memory root does not exist: {root}")
    if not re.fullmatch(r"PATCH-[A-Za-z0-9._-]+", patch_id):
        raise TransactionError("patch_id must start with PATCH- and contain only safe identifier characters")
    normalized_files: list[str] = []
    targets: dict[str, Path] = {}
    delete_set: set[str] = set()
    for raw in [*files, *delete_files]:
        normalized, target = _safe_relative(root, raw)
        if normalized in targets:
            raise TransactionError(f"duplicate transaction path: {normalized}")
        normalized_files.append(normalized)
        targets[normalized] = target
    for raw in delete_files:
        normalized, _ = _safe_relative(root, raw)
        delete_set.add(normalized)
    if delete_set & CONTROL_PATHS:
        raise TransactionError("current.md and index.md cannot be deleted")
    if not CONTROL_PATHS.issubset(targets):
        raise TransactionError("every memory transaction must stage both current.md and index.md")
    _mode_allows(mode, operation, normalized_files)
    base_snapshot_id = committed_snapshot(root)
    if target_snapshot_id <= base_snapshot_id:
        raise TransactionError(
            f"target snapshot must sort after base: base={base_snapshot_id} target={target_snapshot_id}"
        )

    lock_path, staging, manifest_path = _transaction_paths(root, patch_id)
    lock = {
        "version": 1,
        "patch_id": patch_id,
        "owner": owner,
        "mode": mode,
        "operation": operation,
        "base_snapshot_id": base_snapshot_id,
        "target_snapshot_id": target_snapshot_id,
        "acquired_at": _now(),
    }
    try:
        _json_write(lock_path, lock, exclusive=True)
    except FileExistsError as exc:
        raise TransactionError(f"update lock already exists: {lock_path}") from exc

    try:
        staging.mkdir(parents=True, exist_ok=False)
        entries: list[dict[str, Any]] = []
        for relative in normalized_files:
            target = targets[relative]
            if target.exists() and not target.is_file():
                raise TransactionError(f"transaction target is not a regular file: {relative}")
            if target.is_symlink():
                raise TransactionError(f"transaction target cannot be a symbolic link: {relative}")
            staged = staging / "files" / Path(relative)
            before = _sha256(target) if target.is_file() else None
            change = "delete" if relative in delete_set else "update" if target.exists() else "create"
            if change == "delete":
                if before is None:
                    raise TransactionError(f"delete target does not exist: {relative}")
            else:
                staged.parent.mkdir(parents=True, exist_ok=True)
                if target.is_file():
                    shutil.copy2(target, staged)
                else:
                    staged.touch()
            entries.append(
                {
                    "path": relative,
                    "change": change,
                    "before_sha256": before,
                    "staged_sha256": None,
                    "after_sha256": None,
                }
            )
        manifest = {
            **lock,
            "status": "prepared",
            "started_at": _now(),
            "files": entries,
            "writes_completed": 0,
            "validation": None,
        }
        _json_write(manifest_path, manifest)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        try:
            lock_path.unlink()
        except OSError:
            pass
        raise


def staged_path(root: Path, patch_id: str, relative: str) -> Path:
    root = root.resolve()
    normalized, _ = _safe_relative(root, relative)
    _, staging, manifest_path = _transaction_paths(root, patch_id)
    manifest = _json_read(manifest_path)
    entries = {entry["path"]: entry for entry in manifest["files"]}
    if normalized not in entries:
        raise TransactionError(f"path is not part of transaction {patch_id}: {normalized}")
    if entries[normalized]["change"] == "delete":
        raise TransactionError(f"delete target has no staged content: {normalized}")
    return staging / "files" / Path(normalized)


def write_staged(root: Path, patch_id: str, relative: str, content: str) -> Path:
    path = staged_path(root, patch_id, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _render_value(value: Any, indent: int) -> str:
    prefix = " " * indent
    if isinstance(value, ParsedCall):
        return _render_call(value, indent)
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (str, int)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        if not value:
            return "[]"
        parts = [f"{prefix}    {_render_value(item, indent + 4)}," for item in value]
        return "[\n" + "\n".join(parts) + f"\n{prefix}]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        parts = [
            f"{prefix}    {json.dumps(key, ensure_ascii=False)}: {_render_value(item, indent + 4)},"
            for key, item in value.items()
        ]
        return "{\n" + "\n".join(parts) + f"\n{prefix}}}"
    raise TypeError(f"cannot render structured value: {type(value).__name__}")


def _render_call(call: ParsedCall, indent: int = 0) -> str:
    prefix = " " * indent
    lines = [f"{call.name}("]
    for key, value in call.fields.items():
        rendered = _render_value(value, indent + 4)
        lines.append(f"{prefix}    {key}={rendered},")
    lines.append(f"{prefix})")
    return "\n".join(lines)


def render_markdown(title: str, call: ParsedCall, note: str | None = None) -> str:
    body = f"# {title}\n\n```python\n{_render_call(call)}\n```\n"
    if note:
        body += f"\n{note.rstrip()}\n"
    return body


def _compact_patch(manifest: dict[str, Any]) -> str:
    call = ParsedCall(
        "ContextPatch",
        {
            "patch_id": manifest["patch_id"],
            "status": "merged",
            "created_at": manifest["started_at"],
            "base_snapshot_id": manifest["base_snapshot_id"],
            "target_snapshot_id": manifest["target_snapshot_id"],
            "affected_anchors": [],
            "target_files": [entry["path"] for entry in manifest["files"]],
            "add_or_update_decisions": [],
            "add_or_update_issues": [],
            "add_or_update_conflicts": [],
            "add_or_update_lessons": [],
            "add_or_update_handoffs": [],
            "project_map_updates": [],
            "archive_targets": [],
            "source_refs": [],
            "assumptions": [],
            "merge_started_at": manifest["started_at"],
            "merge_completed_at": _now(),
            "merge_notes": f"Committed by memoryctl in {manifest['mode']} mode for {manifest['operation']}.",
        },
        1,
    )
    return render_markdown(manifest["patch_id"], call)


def _ensure_patch_record(root: Path, staging: Path, manifest: dict[str, Any]) -> None:
    if manifest["mode"] == "minimal":
        return
    relative = f"patches/{manifest['patch_id']}.md"
    if relative in {entry["path"] for entry in manifest["files"]}:
        return
    target = root / Path(relative)
    if target.exists():
        raise TransactionError(f"generated patch record already exists: {relative}")
    staged = staging / "files" / Path(relative)
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(_compact_patch(manifest), encoding="utf-8", newline="\n")
    manifest["files"].append(
        {
            "path": relative,
            "change": "create",
            "before_sha256": None,
            "staged_sha256": None,
            "after_sha256": None,
        }
    )


def _effective_root(root: Path, staging: Path, manifest: dict[str, Any], temp: Path) -> Path:
    effective = temp / "memory"
    shutil.copytree(
        root,
        effective,
        ignore=shutil.ignore_patterns(LOCK_NAME, STAGING_NAME, AUDIT_NAME),
    )
    for entry in manifest["files"]:
        target = effective / Path(entry["path"])
        if entry["change"] == "delete":
            if target.exists():
                target.unlink()
            continue
        source = staging / "files" / Path(entry["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return effective


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temp = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_handle:
            shutil.copyfileobj(input_handle, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()


def _order(entry: dict[str, Any]) -> tuple[int, str]:
    path = entry["path"]
    if path == "current.md":
        return 4, path
    if path == "index.md":
        return 3, path
    if path.startswith("indexes/"):
        return 2, path
    return 1, path


def _validate_staged(root: Path, staging: Path, manifest: dict[str, Any]) -> Findings:
    current = staging / "files" / "current.md"
    index = staging / "files" / "index.md"
    current_snapshot = _snapshot(current)
    index_snapshot = _snapshot(index)
    target = manifest["target_snapshot_id"]
    if current_snapshot != target or index_snapshot != target:
        raise TransactionError(
            f"staged commit boundary must equal target {target}: current={current_snapshot} index={index_snapshot}"
        )
    with tempfile.TemporaryDirectory(prefix="memoryctl-effective-") as raw_temp:
        effective = _effective_root(root, staging, manifest, Path(raw_temp))
        _, schema_version = load_config(effective)
        findings = validate_memory(
            effective,
            strict_v3=schema_version == 3,
            logical_root=root,
        )
        if schema_version == 3:
            indexed_snapshots: dict[str, str] = {}
            for index_path in [effective / "index.md", *sorted((effective / "indexes").rglob("*.md"))]:
                if not index_path.is_file() or index_path.name.startswith("_"):
                    continue
                index_call = _record(index_path)
                for entry in index_call.fields.get("entries", []):
                    indexed_snapshots[entry.fields["path"].replace("\\", "/")] = entry.fields[
                        "record_snapshot_id"
                    ]
            canonical_prefixes = (
                "decisions/",
                "issues/",
                "conflicts/",
                "lessons/",
                "tasks/",
                "handoffs/",
            )
            for entry in manifest["files"]:
                relative = entry["path"].replace("\\", "/")
                if not relative.startswith(canonical_prefixes):
                    continue
                if entry["change"] == "delete":
                    if relative in indexed_snapshots:
                        findings.errors.append(
                            f"deleted record remains indexed in staged memory: {relative}"
                        )
                    continue
                staged_hash = _sha256(staging / "files" / Path(relative))
                if staged_hash == entry["before_sha256"]:
                    continue
                if indexed_snapshots.get(relative) != manifest["target_snapshot_id"]:
                    findings.errors.append(
                        f"changed record must be indexed at target snapshot: {relative}"
                    )
        return findings


def _retain_audit(root: Path, staging: Path, manifest: dict[str, Any]) -> None:
    if manifest["mode"] not in {"detailed", "forensic"}:
        return
    audit = root / AUDIT_NAME / manifest["patch_id"]
    audit.mkdir(parents=True, exist_ok=False)
    _json_write(audit / "manifest.json", manifest)
    if manifest["mode"] == "forensic" and (staging / "backups").is_dir():
        shutil.copytree(staging / "backups", audit / "backups")


def _load_active(root: Path, patch_id: str) -> tuple[Path, Path, Path, dict[str, Any], dict[str, Any]]:
    lock_path, staging, manifest_path = _transaction_paths(root, patch_id)
    lock = _json_read(lock_path)
    manifest = _json_read(manifest_path)
    if lock.get("patch_id") != patch_id or manifest.get("patch_id") != patch_id:
        raise TransactionError("lock or manifest belongs to a different patch")
    if lock.get("target_snapshot_id") != manifest.get("target_snapshot_id"):
        raise TransactionError("lock and manifest target snapshots differ")
    return lock_path, staging, manifest_path, lock, manifest


def commit_transaction(root: Path, patch_id: str, *, fail_after: int | None = None) -> dict[str, Any]:
    root = root.resolve()
    lock_path, staging, manifest_path, _, manifest = _load_active(root, patch_id)
    if manifest["status"] != "prepared":
        raise TransactionError(f"transaction is not prepared: {manifest['status']}")
    if committed_snapshot(root) != manifest["base_snapshot_id"]:
        raise TransactionError("committed snapshot changed after transaction began")

    _ensure_patch_record(root, staging, manifest)
    findings = _validate_staged(root, staging, manifest)
    manifest["validation"] = findings.as_dict()
    if not findings.ok:
        _json_write(manifest_path, manifest)
        raise TransactionError("staged memory validation failed: " + "; ".join(findings.errors))

    backups = staging / "backups"
    for entry in manifest["files"]:
        target = root / Path(entry["path"])
        if entry["change"] != "delete":
            staged = staging / "files" / Path(entry["path"])
            entry["staged_sha256"] = _sha256(staged)
        if target.is_file():
            backup = backups / Path(entry["path"])
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
            if _sha256(backup) != entry["before_sha256"]:
                raise TransactionError(f"backup hash mismatch before commit: {entry['path']}")
    manifest["status"] = "applying"
    _json_write(manifest_path, manifest)

    writes = 0
    try:
        for entry in sorted(manifest["files"], key=_order):
            target = root / Path(entry["path"])
            if entry["change"] == "delete":
                deleted = staging / "deleted" / Path(entry["path"])
                deleted.parent.mkdir(parents=True, exist_ok=True)
                if not target.is_file():
                    raise TransactionError(f"delete target disappeared during commit: {entry['path']}")
                os.replace(target, deleted)
            else:
                _atomic_copy(staging / "files" / Path(entry["path"]), target)
            writes += 1
            manifest["writes_completed"] = writes
            _json_write(manifest_path, manifest)
            if fail_after is not None and writes >= fail_after:
                raise SimulatedInterruption(f"simulated interruption after {writes} writes")

        if committed_snapshot(root) != manifest["target_snapshot_id"]:
            raise TransactionError("committed snapshot does not equal transaction target")
        for entry in manifest["files"]:
            target = root / Path(entry["path"])
            if entry["change"] == "delete":
                if target.exists():
                    raise TransactionError(f"deleted target still exists: {entry['path']}")
                entry["after_sha256"] = None
            else:
                entry["after_sha256"] = _sha256(target)
                if entry["after_sha256"] != entry["staged_sha256"]:
                    raise TransactionError(f"committed hash differs from staged hash: {entry['path']}")
        manifest["status"] = "committed"
        manifest["committed_at"] = _now()
        _json_write(manifest_path, manifest)
        _retain_audit(root, staging, manifest)
        shutil.rmtree(staging)
        lock_path.unlink()
        parent = root / STAGING_NAME
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
        return manifest
    except Exception:
        manifest["status"] = "recovery_required"
        manifest["writes_completed"] = writes
        _json_write(manifest_path, manifest)
        raise


def _matches_before(root: Path, manifest: dict[str, Any]) -> bool:
    for entry in manifest["files"]:
        target = root / Path(entry["path"])
        before = entry["before_sha256"]
        if before is None:
            if target.exists():
                return False
        elif not target.is_file() or _sha256(target) != before:
            return False
    return True


def _matches_staged(root: Path, manifest: dict[str, Any]) -> bool:
    for entry in manifest["files"]:
        target = root / Path(entry["path"])
        if entry["change"] == "delete":
            if target.exists():
                return False
            continue
        staged_hash = entry.get("staged_sha256")
        if not staged_hash or not target.is_file() or _sha256(target) != staged_hash:
            return False
    return True


def abort_transaction(root: Path, patch_id: str) -> dict[str, Any]:
    root = root.resolve()
    lock_path, staging, _, _, manifest = _load_active(root, patch_id)
    if manifest["status"] != "prepared":
        raise TransactionError("only a prepared transaction can be aborted")
    if committed_snapshot(root) != manifest["base_snapshot_id"] or not _matches_before(root, manifest):
        raise TransactionError("committed files changed; use recover instead of abort")
    shutil.rmtree(staging)
    lock_path.unlink()
    parent = root / STAGING_NAME
    if parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
    return {"patch_id": patch_id, "status": "aborted", "snapshot_id": manifest["base_snapshot_id"]}


def recover_transaction(root: Path, patch_id: str) -> dict[str, Any]:
    root = root.resolve()
    lock_path, staging, manifest_path, _, manifest = _load_active(root, patch_id)
    current = _snapshot(root / "current.md")
    index = _snapshot(root / "index.md")
    base = manifest["base_snapshot_id"]
    target = manifest["target_snapshot_id"]
    if current == target and index == target and _matches_staged(root, manifest):
        manifest["status"] = "committed"
        manifest["recovered_at"] = _now()
        for entry in manifest["files"]:
            target_path = root / Path(entry["path"])
            entry["after_sha256"] = None if entry["change"] == "delete" else _sha256(target_path)
        _json_write(manifest_path, manifest)
        _retain_audit(root, staging, manifest)
        shutil.rmtree(staging)
        lock_path.unlink()
        return {"patch_id": patch_id, "status": "committed", "disposition": "finalized_target"}
    if current == base and index == base and _matches_before(root, manifest):
        shutil.rmtree(staging)
        lock_path.unlink()
        return {"patch_id": patch_id, "status": "aborted", "disposition": "clean_base"}
    manifest["status"] = "recovery_required"
    manifest["recovery_diagnosis"] = {
        "current_snapshot_id": current,
        "index_snapshot_id": index,
        "base_snapshot_id": base,
        "target_snapshot_id": target,
        "matches_before": _matches_before(root, manifest),
        "matches_staged": _matches_staged(root, manifest),
    }
    _json_write(manifest_path, manifest)
    raise TransactionError(
        "mixed or ambiguous transaction state; lock and staging evidence were retained"
    )


def _record(path: Path) -> ParsedCall:
    calls = parse_markdown(path.read_text(encoding="utf-8"), path.as_posix())
    if len(calls) != 1:
        raise TransactionError(f"expected one record in {path}")
    return calls[0]


def _upgrade_v3_identifier_fields(path: Path) -> str | None:
    call = _record(path)
    required = V3_REQUIRED_FIELDS.get(call.name)
    if not required:
        return None
    fields = dict(call.fields)
    changed = False
    if "fact_ids" in required and "fact_ids" not in fields:
        fields["fact_ids"] = []
        changed = True
    if "contradiction_id" in required and "contradiction_id" not in fields:
        fields["contradiction_id"] = fields["id"] if call.name == "Conflict" else None
        changed = True
    if not changed:
        return None
    text = path.read_text(encoding="utf-8")
    matches = list(PYTHON_FENCE_RE.finditer(text))
    if len(matches) != 1:
        raise TransactionError(f"expected one Python record block in {path}")
    match = matches[0]
    replacement = f"```python\n{_render_call(ParsedCall(call.name, fields, call.line))}\n```"
    return text[: match.start()] + replacement + text[match.end() :]


def _migration_record_upgrades(root: Path) -> dict[str, str]:
    upgrades: dict[str, str] = {}
    for directory_name in (
        "decisions",
        "issues",
        "conflicts",
        "lessons",
        "tasks",
        "handoffs",
        "archive",
    ):
        directory = root / directory_name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.md")):
            if path.name.startswith("_") or path.relative_to(root).as_posix() == "archive/manifest.md":
                continue
            content = _upgrade_v3_identifier_fields(path)
            if content is not None:
                upgrades[path.relative_to(root).as_posix()] = content
    return upgrades


def _replace_snapshot(call: ParsedCall, target: str) -> ParsedCall:
    fields = dict(call.fields)
    fields["snapshot_id"] = target
    return ParsedCall(call.name, fields, call.line)


def _date_bucket(path: str) -> str:
    match = re.search(r"(?:19|20)\d{2}(?:[-_]?(0[1-9]|1[0-2]))?", path)
    if not match:
        return "undated"
    raw = match.group(0).replace("_", "-")
    if "-" not in raw and len(raw) >= 6:
        raw = raw[:4] + "-" + raw[4:6]
    return raw


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return value or "other"


def _chunk_topic_entries(
    kind: str,
    anchor_group: str,
    bucket: str,
    entries: list[ParsedCall],
    target_snapshot_id: str,
) -> list[list[ParsedCall]]:
    chunks: list[list[ParsedCall]] = []
    current: list[ParsedCall] = []
    for entry in entries:
        candidate = [*current, entry]
        probe = ParsedCall(
            "TopicIndex",
            {
                "topic_id": f"topic:{_slug(kind)}:{_slug(anchor_group)}:{_slug(bucket)}",
                "snapshot_id": target_snapshot_id,
                "anchors": sorted({primary_anchor(item.fields["anchors"]) for item in candidate}),
                "entries": candidate,
                "archive_route": "archive/manifest.md" if kind == "archive" else None,
            },
            1,
        )
        tokens = count_tokens(_render_call(probe)).tokens
        if current and tokens > 1500:
            chunks.append(current)
            current = [entry]
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def plan_v2_migration(root: Path, target_snapshot_id: str) -> dict[str, Any]:
    root = root.resolve()
    _, schema_version = load_config(root)
    if schema_version != 2:
        raise TransactionError("migration requires a v2 root without config.json")
    base = committed_snapshot(root)
    if target_snapshot_id <= base:
        raise TransactionError("migration target snapshot must sort after the v2 base")
    index = _record(root / "index.md")
    if index.name != "MemoryIndex":
        raise TransactionError("v2 migration expects MemoryIndex")
    record_upgrades = _migration_record_upgrades(root)
    active: list[ParsedCall] = []
    historical: dict[tuple[str, str, str], list[ParsedCall]] = {}
    for entry in index.fields["entries"]:
        if entry.fields["path"].replace("\\", "/") in record_upgrades:
            entry_fields = dict(entry.fields)
            entry_fields["record_snapshot_id"] = target_snapshot_id
            entry = ParsedCall("IndexEntry", entry_fields, entry.line)
        importance = entry.fields["importance"]
        status = entry.fields["status"]
        kind = entry.fields["path"].split("/", 1)[0]
        active_kinds = {"decisions", "issues", "conflicts", "lessons", "tasks", "handoffs"}
        if status == "active" and (
            kind in active_kinds or importance.startswith(("L0_", "L1_", "L2_"))
        ):
            active.append(entry)
            continue
        anchor_group = primary_anchor(entry.fields["anchors"])
        historical.setdefault((kind, anchor_group, _date_bucket(entry.fields["path"])), []).append(entry)

    shard_specs: list[tuple[str, ParsedCall]] = []
    route_kinds: dict[str, list[str]] = {}
    for (kind, anchor_group, bucket), entries in sorted(historical.items()):
        chunks = _chunk_topic_entries(kind, anchor_group, bucket, entries, target_snapshot_id)
        for chunk_index, chunk in enumerate(chunks, start=1):
            suffix = f"-{chunk_index:03d}" if len(chunks) > 1 else ""
            filename = f"{_slug(kind)}-{_slug(anchor_group)}-{_slug(bucket)}{suffix}.md"
            route_kinds.setdefault(kind, []).append(filename)
            anchors = sorted({primary_anchor(entry.fields["anchors"]) for entry in chunk})
            shard_specs.append(
                (
                    f"indexes/{filename}",
                    ParsedCall(
                        "TopicIndex",
                        {
                            "topic_id": (
                                f"topic:{_slug(kind)}:{_slug(anchor_group)}:"
                                f"{_slug(bucket)}:{chunk_index:03d}"
                            ),
                            "snapshot_id": target_snapshot_id,
                            "anchors": anchors,
                            "entries": chunk,
                            "archive_route": "archive/manifest.md" if kind == "archive" else None,
                        },
                        1,
                    ),
                )
            )
    if not shard_specs:
        shard_specs.append(
            (
                "indexes/general.md",
                ParsedCall(
                    "TopicIndex",
                    {
                        "topic_id": "topic:general",
                        "snapshot_id": target_snapshot_id,
                        "anchors": [],
                        "entries": [],
                        "archive_route": "archive/manifest.md",
                    },
                    1,
                ),
            )
        )
        route_kinds["general"] = ["general.md"]

    routes: list[ParsedCall] = []
    for kind in sorted(route_kinds):
        pattern = f"indexes/{_slug(kind)}-*.md" if kind != "general" else "indexes/general.md"
        routes.append(
            ParsedCall(
                "IndexRoute",
                {
                    "route_id": f"route:{_slug(kind)}",
                    "anchor_prefixes": [f"{_slug(kind.rstrip('s'))}:"],
                    "path_glob": pattern,
                    "summary": f"Route {_slug(kind)} history to bounded topic shards.",
                    "status_scope": ["active", "archived", "deprecated", "scratch"],
                },
                1,
            )
        )
    active_index = ParsedCall(
        "ActiveMemoryIndex",
        {"snapshot_id": target_snapshot_id, "entries": active, "routes": routes},
        1,
    )
    current = _replace_snapshot(_record(root / "current.md"), target_snapshot_id)
    files = [
        "config.json",
        "current.md",
        "index.md",
        *[path for path, _ in shard_specs],
        *record_upgrades,
    ]
    return {
        "base_snapshot_id": base,
        "target_snapshot_id": target_snapshot_id,
        "files": files,
        "current": render_markdown("Current Project Memory", current),
        "index": render_markdown(
            "Active Memory Index",
            active_index,
            "Root entries stay active and small. Routes lead to bounded topic shards.",
        ),
        "config": config_json(),
        "shards": {
            path: render_markdown(
                f"Topic Index: {call.fields['topic_id']}",
                call,
                "This shard is canonical routing metadata, not a substitute for linked records.",
            )
            for path, call in shard_specs
        },
        "records": record_upgrades,
    }


def migrate_v2_to_v3(
    root: Path,
    *,
    target_snapshot_id: str,
    apply: bool = False,
    mode: str = "forensic",
    confirm: str | None = None,
) -> dict[str, Any]:
    plan = plan_v2_migration(root, target_snapshot_id)
    if not apply:
        return {
            "applied": False,
            "mode": mode,
            **{
                key: value
                for key, value in plan.items()
                if key not in {"current", "index", "config", "shards", "records"}
            },
        }
    if mode != "forensic":
        raise TransactionError("v2-to-v3 migration apply requires forensic mode")
    if confirm != "APPLY_V3_MIGRATION":
        raise TransactionError("migration apply requires --confirm APPLY_V3_MIGRATION")
    patch_id = f"PATCH-{target_snapshot_id.removeprefix('SNAP-')}-v3-migration"
    begin_transaction(
        root,
        patch_id=patch_id,
        mode=mode,
        operation="migrate",
        target_snapshot_id=target_snapshot_id,
        files=plan["files"],
        owner="memoryctl:migrate",
    )
    write_staged(root, patch_id, "config.json", plan["config"])
    write_staged(root, patch_id, "current.md", plan["current"])
    write_staged(root, patch_id, "index.md", plan["index"])
    for path, content in plan["shards"].items():
        write_staged(root, patch_id, path, content)
    for path, content in plan["records"].items():
        write_staged(root, patch_id, path, content)
    result = commit_transaction(root, patch_id)
    return {"applied": True, "patch_id": patch_id, "status": result["status"], "target_snapshot_id": target_snapshot_id}
