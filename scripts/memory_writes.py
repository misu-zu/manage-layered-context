"""Initialize a v3 root and commit a single authored Markdown record."""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

from memory_model import ParsedCall, TEMPLATE_ROOT, load_config, next_snapshot, parse_markdown
from memory_transaction import (
    CONTROL_PATHS, TransactionError, _now, _record, _safe_relative,
    abort_transaction, add_transaction_files, begin_transaction, commit_transaction,
    replace_record, staged_path, write_staged,
)
from memory_validation import (
    CURRENT_FIELDS, RECORD_TYPES, SECRET_PATTERN, index_status, primary_anchor, record_id, validate_memory,
)


def _update_block(path: Path, call: ParsedCall) -> None:
    text = path.read_text(encoding="utf-8")
    path.write_text(replace_record(text, call), encoding="utf-8", newline="\n")


def init_memory(root: Path, name: str | None, purpose: str | None) -> dict:
    if root.is_symlink():
        raise TransactionError("init cannot replace a symbolic link")
    root = root.resolve()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise TransactionError("init requires a new or empty memory directory")
    root.parent.mkdir(parents=True, exist_ok=True)
    name = name or root.parent.name or root.name
    target = next_snapshot()
    with tempfile.TemporaryDirectory(dir=root.parent, prefix=".memoryctl-init-") as tmp:
        draft = Path(tmp) / "memory"
        shutil.copytree(TEMPLATE_ROOT, draft)
        state = _record(draft / "current.md")
        state.fields.update(snapshot_id=target, project_identity=name, active_goal=None, last_updated=_now())
        _update_block(draft / "current.md", state)
        project = _record(draft / "project.md")
        slug = re.sub(r"[^\w-]+", "-", name.casefold()).strip("-") or "project"
        project.fields.update(project_id=f"project:{slug}", name=name, purpose=purpose or "", memory_root=root.name)
        _update_block(draft / "project.md", project)
        for relative in ("index.md", "indexes/general.md"):
            record = _record(draft / relative)
            record.fields["snapshot_id"] = target
            _update_block(draft / relative, record)
        findings = validate_memory(draft, strict_v3=True, logical_root=root)
        if not findings.ok:
            raise TransactionError("init validation failed: " + "; ".join(findings.errors))
        draft.replace(root)
    return {"memory_root": str(root), "snapshot_id": target, "status": "initialized"}


def _sync_current(current: ParsedCall, kind: str, record: ParsedCall, path: str, previous: ParsedCall | None) -> None:
    if kind not in CURRENT_FIELDS:
        return
    field, prefix = CURRENT_FIELDS[kind]
    identifier = record_id(record)
    ref = f"{prefix}:{identifier}"
    aliases = {identifier, ref, path, *record.fields.get("anchors", [])}
    if previous:
        aliases.update(previous.fields.get("anchors", []))
    active = index_status(record) == "active"
    if field == "latest_handoff":
        if record.fields["status"] in {"ready", "accepted", "blocked"}:
            current.fields[field] = ref
        elif current.fields.get(field) in aliases:
            current.fields[field] = None
    else:
        values = current.fields.setdefault(field, [])
        current.fields[field] = [value for value in values if value not in aliases]
        if active:
            current.fields[field].append(ref)


def put_record(root: Path, source: Path, *, path: str | None = None, mode: str | None = None,
               summary: str | None = None) -> dict:
    root = root.resolve()
    config, version = load_config(root)
    if version != 3:
        raise TransactionError("record put requires a v3 memory root")
    text = source.read_text(encoding="utf-8")
    if SECRET_PATTERN.search(text.encode("utf-8")):
        raise TransactionError("possible credential or private key in record")
    calls = parse_markdown(text, str(source))
    if len(calls) != 1:
        raise TransactionError("supply one canonical Markdown record")
    record = calls[0]
    kind = next((k for k, typ in RECORD_TYPES.items() if typ == record.name and k != "patches"), None)
    if not kind:
        raise TransactionError(f"record put does not accept {record.name}")
    identifier = record_id(record)
    relative, existing = _safe_relative(root, path or f"{kind}/{identifier}.md")
    if Path(relative).parts[0] != kind or existing.suffix != ".md" or existing.name.startswith("_") or relative == "archive/manifest.md":
        raise TransactionError(f"expected a canonical Markdown path under {kind}/")

    patch = f"PATCH-{uuid4().hex}"
    manifest = begin_transaction(
        root, patch_id=patch, mode=mode or config["record_mode"], operation="update" if existing.exists() else "create",
        target_snapshot_id=None, files=[*sorted(CONTROL_PATHS), relative],
    )
    target = manifest["target_snapshot_id"]
    try:
        previous = _record(existing) if existing.is_file() else None
        if previous and record_id(previous) != identifier:
            raise TransactionError("record put cannot replace a different record ID")
        if record.name == "TaskRecord":
            record.fields.update(last_updated_snapshot_id=target, updated_at=_now())
        if record.name == "HandoffCheckpoint":
            if record.fields.get("profile") == "lite":
                record.fields["checkpoint_snapshot_id"] = target
            else:
                record.fields["last_updated_snapshot_id"] = target
                if not previous:
                    record.fields["created_snapshot_id"] = target
        write_staged(root, patch, relative, replace_record(text, record))

        indexes = {"index.md": _record(root / "index.md")}
        for shard in sorted((root / "indexes").glob("*.md")):
            if not shard.name.startswith("_"):
                indexes[shard.relative_to(root).as_posix()] = _record(shard)
        owner = None
        old_fields = {}
        for location, index in indexes.items():
            for entry in index.fields["entries"]:
                if entry.fields["path"].replace("\\", "/") == relative:
                    owner, old_fields = location, entry.fields
        anchors = record.fields.get("anchors", record.fields.get("relevant_anchors", []))
        status = index_status(record)
        destination = owner or "index.md"
        if destination == "index.md" and status != "active":
            shards = [name for name in indexes if name != "index.md"]
            if not shards:
                raise TransactionError("no topic shard available")
            anchor = primary_anchor(anchors)
            destination = max(shards, key=lambda name: anchor in indexes[name].fields["anchors"])
        touched = {destination, "index.md"}
        if owner:
            touched.add(owner)
        if kind == "handoffs":
            task_id = record.fields["task_id"].removeprefix("task:")
            for location, index in indexes.items():
                for entry in index.fields["entries"]:
                    task_path = entry.fields["path"]
                    if not task_path.startswith("tasks/"):
                        continue
                    _, safe_task = _safe_relative(root, task_path)
                    if safe_task.stem != task_id and not safe_task.stem.startswith(task_id + "-"):
                        continue
                    task = _record(safe_task)
                    if record_id(task) != task_id:
                        continue
                    refs = task.fields.setdefault("handoff_refs", [])
                    if identifier in refs or f"handoff:{identifier}" in refs or relative in refs:
                        continue
                    refs.append(f"handoff:{identifier}")
                    task.fields.update(last_updated_snapshot_id=target, updated_at=_now())
                    add_transaction_files(root, patch, [task_path])
                    _update_block(staged_path(root, patch, task_path), task)
                    entry.fields["record_snapshot_id"] = target
                    touched.add(location)
        for location in sorted(touched - {"index.md"}):
            add_transaction_files(root, patch, [location])
        if owner:
            indexes[owner].fields["entries"] = [
                e for e in indexes[owner].fields["entries"] if e.fields is not old_fields
            ]
        default_summary = next(
            (record.fields[k] for k in ("topic", "title", "objective", "summary") if k in record.fields), identifier
        )
        importance = {
            "decisions": "L1_active_decision", "lessons": "L1_active_lesson", "handoffs": "L2_active_handoff",
            "conflicts": "L2_active_issue", "issues": "L2_active_issue",
        }.get(kind, "L3_working_note") if status == "active" else "L4_historical_detail"
        fields = dict(old_fields)
        fields.update(anchors=anchors, path=relative, summary=summary or old_fields.get("summary") or default_summary,
                      importance=importance, status=status, related=old_fields.get("related", []), record_snapshot_id=target)
        indexes[destination].fields["entries"].append(ParsedCall("IndexEntry", fields, 1))
        for location in sorted(touched):
            index = indexes[location]
            index.fields["snapshot_id"] = target
            _update_block(staged_path(root, patch, location), index)
        current_path = staged_path(root, patch, "current.md")
        current = _record(current_path)
        current.fields.update(snapshot_id=target, last_updated=_now())
        _sync_current(current, kind, record, relative, previous)
        _update_block(current_path, current)
        commit_transaction(root, patch)
    except Exception:
        # Only a clean prepared transaction can abort. Preserve mixed-state recovery evidence.
        try:
            abort_transaction(root, patch)
        except (TransactionError, OSError):
            pass
        raise
    return {"status": "committed", "patch_id": patch, "path": relative, "snapshot_id": target}
