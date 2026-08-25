#!/usr/bin/env python3
"""Validate structural and resumability invariants of layered project memory."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


STRING_FIELD = r'\b{field}\s*=\s*"([^"]+)"'
STATUS_RE = re.compile(STRING_FIELD.format(field="status"))
SNAPSHOT_RE = re.compile(STRING_FIELD.format(field="snapshot_id"))
PATH_RE = re.compile(STRING_FIELD.format(field="path"))
PROJECT_POINTER_RE = re.compile(r"ProjectPointer\((.*?)\n\s*\),?", re.DOTALL)
HANDOFF_REQUIRED_FIELDS = (
    "checkpoint_id",
    "context_id",
    "task_id",
    "status",
    "task_base_snapshot_id",
    "source_snapshot_id",
    "created_snapshot_id",
    "last_updated_snapshot_id",
    "objective",
    "acceptance_criteria",
    "state_summary",
    "completed",
    "remaining",
    "next_action",
    "artifacts",
    "changed_files",
    "deleted_files",
    "verification",
    "assumptions",
    "risks",
    "blockers",
    "lesson_refs",
    "required_checks",
    "source_refs",
)
BASE_REQUIRED_FILES = ("current.md", "index.md", "anchors.md")
V2_FILES = ("project.md",)
V2_DIRECTORIES = ("lessons", "handoffs")


def field_value(text: str, field: str) -> str | None:
    match = re.search(STRING_FIELD.format(field=re.escape(field)), text)
    return match.group(1) if match else None


def field_present(text: str, field: str) -> bool:
    return re.search(rf"\b{re.escape(field)}\s*=", text) is not None


def list_values(text: str, field: str) -> list[str]:
    match = re.search(rf"\b{re.escape(field)}\s*=\s*\[(.*?)\]\s*,", text, re.DOTALL)
    if not match:
        return []
    return re.findall(r'"([^"]+)"', match.group(1))


def placeholder(value: str) -> bool:
    return any(marker in value for marker in ("<", ">", "YYYY", "NNNN", "..."))


class Findings:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.info: list[str] = []

    def as_dict(self) -> dict:
        return {
            "ok": not self.errors,
            "errors": self.errors,
            "warnings": self.warnings,
            "info": self.info,
        }


def read(path: Path, findings: Findings) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        findings.errors.append(f"cannot read {path}: {exc}")
        return None


def validate_structure(root: Path, strict_v2: bool, findings: Findings) -> None:
    for relative in BASE_REQUIRED_FILES:
        if not (root / relative).is_file():
            findings.errors.append(f"missing required file: {relative}")

    for relative in V2_FILES:
        if not (root / relative).is_file():
            message = f"missing v2 file: {relative}"
            (findings.errors if strict_v2 else findings.warnings).append(message)

    for relative in V2_DIRECTORIES:
        if not (root / relative).is_dir():
            message = f"missing v2 directory: {relative}/"
            (findings.errors if strict_v2 else findings.warnings).append(message)


def validate_snapshots(root: Path, findings: Findings) -> str | None:
    snapshots: dict[str, str] = {}
    for relative in ("current.md", "index.md"):
        path = root / relative
        if not path.is_file():
            continue
        text = read(path, findings)
        if text is None:
            continue
        match = SNAPSHOT_RE.search(text)
        if not match:
            findings.errors.append(f"missing snapshot_id in {relative}")
        elif placeholder(match.group(1)):
            findings.errors.append(f"unresolved snapshot placeholder in {relative}")
        else:
            snapshots[relative] = match.group(1)

    if len(snapshots) == 2 and len(set(snapshots.values())) != 1:
        findings.errors.append(
            "commit boundary mismatch: current.md="
            f"{snapshots['current.md']} index.md={snapshots['index.md']}"
        )
        return None
    snapshot = next(iter(snapshots.values()), None)
    if snapshot:
        findings.info.append(f"committed snapshot: {snapshot}")
    return snapshot


def validate_project_map(root: Path, findings: Findings) -> Path | None:
    path = root / "project.md"
    if not path.is_file():
        return None
    text = read(path, findings)
    if text is None:
        return None
    project_id = field_value(text, "project_id")
    project_root_value = field_value(text, "project_root")
    memory_root_value = field_value(text, "memory_root")
    if not project_id or placeholder(project_id):
        findings.errors.append("project.md: missing concrete project_id")
    if not project_root_value or placeholder(project_root_value):
        findings.errors.append("project.md: missing concrete project_root")
        return None
    project_root = (root / project_root_value).resolve()
    if not project_root.is_dir():
        findings.errors.append(f"project.md: project_root does not resolve: {project_root_value}")
        return None
    if not memory_root_value or placeholder(memory_root_value):
        findings.errors.append("project.md: missing concrete memory_root")
        return project_root
    declared_memory_root = (project_root / memory_root_value).resolve()
    if declared_memory_root != root:
        findings.errors.append(
            "project.md: memory_root resolves to "
            f"{declared_memory_root}, expected current root {root}"
        )
    findings.info.append(f"validated project map: {project_id or 'unknown'}")
    return project_root


def validate_index_paths(root: Path, findings: Findings) -> None:
    path = root / "index.md"
    if not path.is_file():
        return
    text = read(path, findings)
    if text is None:
        return

    seen: set[str] = set()
    for match in PATH_RE.finditer(text):
        relative = match.group(1)
        if placeholder(relative) or relative in seen:
            continue
        seen.add(relative)
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            findings.errors.append(f"indexed path escapes memory root: {relative}")
            continue
        if not candidate.is_file():
            findings.errors.append(f"indexed path does not resolve: {relative}")
    findings.info.append(f"validated indexed paths: {len(seen)}")


def validate_handoff_path(
    value: str,
    field: str,
    path: Path,
    project_root: Path,
    should_exist: bool,
    findings: Findings,
) -> None:
    if placeholder(value):
        findings.errors.append(f"{path.name}: unresolved {field} placeholder: {value}")
        return
    if re.match(r"^[a-z][a-z0-9+.-]*://", value, re.IGNORECASE):
        findings.info.append(f"{path.name}: skipped external {field}: {value}")
        return
    raw = Path(value)
    candidate = raw.resolve() if raw.is_absolute() else (project_root / raw).resolve()
    if not raw.is_absolute():
        try:
            candidate.relative_to(project_root)
        except ValueError:
            findings.errors.append(f"{path.name}: relative {field} escapes project root: {value}")
            return
    if should_exist and not candidate.exists():
        findings.errors.append(f"{path.name}: {field} does not resolve: {value}")
    if not should_exist and candidate.exists():
        findings.errors.append(f"{path.name}: deleted_files path still exists: {value}")


def validate_handoffs(
    root: Path,
    project_root: Path | None,
    snapshot: str | None,
    findings: Findings,
) -> None:
    directory = root / "handoffs"
    if not directory.is_dir():
        return
    count = 0
    for path in sorted(directory.glob("*.md")):
        if path.name.startswith("_"):
            continue
        count += 1
        text = read(path, findings)
        if text is None:
            continue
        missing = [field for field in HANDOFF_REQUIRED_FIELDS if not field_present(text, field)]
        if missing:
            findings.errors.append(f"{path.name}: missing handoff fields: {', '.join(missing)}")
        status = field_value(text, "status")
        checkpoint_id = field_value(text, "checkpoint_id")
        if not status:
            continue
        if status not in {
            "draft",
            "ready",
            "blocked",
            "accepted",
            "consumed",
            "rejected",
            "superseded",
        }:
            findings.errors.append(f"{path.name}: unknown handoff status '{status}'")
        if checkpoint_id and not placeholder(checkpoint_id):
            expected_prefix = checkpoint_id.removeprefix("HANDOFF-")
            if not path.stem.endswith(expected_prefix) and path.stem != checkpoint_id:
                findings.warnings.append(
                    f"{path.name}: filename and checkpoint_id differ ({checkpoint_id})"
                )
        handoff_snapshot = field_value(text, "last_updated_snapshot_id")
        if status in {"ready", "accepted"} and snapshot and handoff_snapshot != snapshot:
            findings.warnings.append(
                f"{path.name}: {status} checkpoint targets {handoff_snapshot}, current is {snapshot}"
            )
        if status == "ready" and re.search(r"\bnext_action\s*=\s*None\b", text):
            findings.warnings.append(f"{path.name}: ready checkpoint has no next_action")
        if status in {"ready", "accepted"} and project_root is not None:
            for field in ("artifacts", "changed_files"):
                for value in list_values(text, field):
                    validate_handoff_path(value, field, path, project_root, True, findings)
            for value in list_values(text, "deleted_files"):
                validate_handoff_path(value, "deleted_files", path, project_root, False, findings)
    findings.info.append(f"validated handoff records: {count}")


def validate_lessons(root: Path, findings: Findings) -> None:
    directory = root / "lessons"
    if not directory.is_dir():
        return
    count = 0
    for path in sorted(directory.glob("*.md")):
        if path.name.startswith("_"):
            continue
        count += 1
        text = read(path, findings)
        if text is None:
            continue
        status = field_value(text, "status")
        if status not in {"candidate", "active", "deprecated"}:
            findings.errors.append(f"{path.name}: invalid or missing lesson status")
            continue
        for field in ("id", "scope", "trigger_signals", "prevention", "supporting_evidence"):
            if not field_present(text, field):
                findings.errors.append(f"{path.name}: missing lesson field '{field}'")
        if status == "active" and re.search(r"\bsupporting_evidence\s*=\s*\[\s*\]", text):
            findings.warnings.append(f"{path.name}: active lesson has no supporting evidence")
    findings.info.append(f"validated lesson records: {count}")


def validate_catalog(catalog: Path, findings: Findings) -> None:
    if not catalog.is_file():
        findings.errors.append(f"catalog does not exist: {catalog}")
        return
    text = read(catalog, findings)
    if text is None:
        return
    base = catalog.parent.resolve()
    project_ids: set[str] = set()
    count = 0
    for block in PROJECT_POINTER_RE.findall(text):
        project_id = field_value(block, "project_id")
        root_value = field_value(block, "root")
        memory_value = field_value(block, "memory_root")
        if not project_id or placeholder(project_id):
            continue
        count += 1
        if project_id in project_ids:
            findings.errors.append(f"duplicate project_id in catalog: {project_id}")
        project_ids.add(project_id)
        for label, value in (("root", root_value), ("memory_root", memory_value)):
            if not value or placeholder(value):
                findings.errors.append(f"{project_id}: missing concrete {label}")
                continue
            target = (base / value).resolve()
            if not target.exists():
                findings.errors.append(f"{project_id}: {label} does not resolve: {value}")
    findings.info.append(f"validated project catalog entries: {count}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate layered project memory invariants.")
    parser.add_argument("memory_root", type=Path, help="Path to one project's memory root")
    parser.add_argument("--catalog", type=Path, help="Optional workspace PROJECTS.md to validate")
    parser.add_argument(
        "--strict-v2",
        action="store_true",
        help="Treat missing v2 project, lesson, and handoff structures as errors",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = args.memory_root.resolve()
    findings = Findings()
    if not root.is_dir():
        findings.errors.append(f"memory root does not exist: {root}")
    else:
        validate_structure(root, args.strict_v2, findings)
        snapshot = validate_snapshots(root, findings)
        project_root = validate_project_map(root, findings)
        validate_index_paths(root, findings)
        validate_handoffs(root, project_root, snapshot, findings)
        validate_lessons(root, findings)
    if args.catalog:
        validate_catalog(args.catalog.resolve(), findings)

    result = {"memory_root": str(root), **findings.as_dict()}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("memory validation: " + ("PASS" if result["ok"] else "FAIL"))
        for message in findings.errors:
            print(f"error: {message}")
        for message in findings.warnings:
            print(f"warning: {message}")
        for message in findings.info:
            print(f"info: {message}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
