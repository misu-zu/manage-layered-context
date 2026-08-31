from __future__ import annotations

import json
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from memory_model import ParsedCall, config_json  # noqa: E402
from memory_transaction import render_markdown  # noqa: E402


def call(record_name: str, **fields: object) -> ParsedCall:
    return ParsedCall(record_name, fields, 1)


def entry(path: str, snapshot: str, *, status: str = "active", anchors: list[str] | None = None) -> ParsedCall:
    return call(
        "IndexEntry",
        anchors=anchors or ["concept:alpha"],
        summary=f"Retrieve {path} for alpha behavior.",
        path=path,
        importance="L1_active_decision" if status == "active" else "L4_historical_detail",
        status=status,
        related=[],
        record_snapshot_id=snapshot,
    )


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def make_project(tmp: Path, *, version: int = 3, snapshot: str = "SNAP-20260830-010000") -> Path:
    project = tmp / "project"
    memory = project / "memory"
    for directory in (
        "decisions",
        "issues",
        "conflicts",
        "lessons",
        "tasks",
        "handoffs",
        "patches",
        "archive",
    ):
        (memory / directory).mkdir(parents=True, exist_ok=True)

    state = call(
        "ProjectState",
        snapshot_id=snapshot,
        project_identity="Alpha test project",
        record_mode="standard",
        active_goal="Verify alpha routing",
        invariants=["Do not invent missing evidence."],
        active_decisions=["decision:ADR-0001"],
        active_lessons=[],
        open_issues=[],
        unresolved_conflicts=[],
        active_tasks=[],
        latest_handoff=None,
        pending_patches=[],
        next_actions=["Inspect alpha decision."],
        last_updated="2026-08-30T01:00:00+09:00",
    )
    write(memory / "current.md", render_markdown("Current Project Memory", state))
    project_map = call(
        "ProjectMap",
        project_id="project:alpha",
        name="Alpha",
        purpose="Test layered memory.",
        project_root="..",
        memory_root="memory",
        boundaries=["Tests"],
        components={"component:alpha": "src/alpha.py"},
        entry_points=[],
        authoritative_sources=[],
        generated_or_disposable=[],
        navigation_notes=[],
    )
    write(memory / "project.md", render_markdown("Project Map", project_map))
    write(
        memory / "anchors.md",
        render_markdown("Anchor Registry", call("AnchorRegistry", aliases={}, deprecated={})),
    )
    decision_fields = dict(
        id="ADR-0001",
        anchors=["decision:ADR-0001", "component:alpha", "concept:alpha"],
        topic="Alpha mode",
        choice="Use amber mode for alpha.",
        status="active",
        confidence="high",
        scope="Alpha component only.",
        rationale="The accepted fixture requires amber.",
        alternatives=["Blue mode"],
        source_refs=[],
        review_when=None,
        supersedes=[],
    )
    if version == 3:
        decision_fields["fact_ids"] = ["fact:alpha-uses-amber"]
    decision = call("Decision", **decision_fields)
    write(memory / "decisions" / "ADR-0001-alpha.md", render_markdown("ADR-0001: Alpha", decision))
    write(
        memory / "archive" / "manifest.md",
        render_markdown("Archive Manifest", call("ArchiveManifest", entries=[])),
    )
    decision_entry = entry(
        "decisions/ADR-0001-alpha.md",
        snapshot,
        anchors=["component:alpha", "concept:alpha", "decision:ADR-0001"],
    )
    if version == 2:
        index = call("MemoryIndex", snapshot_id=snapshot, entries=[decision_entry])
        write(memory / "index.md", render_markdown("Memory Index", index))
        return memory

    write(memory / "config.json", config_json())
    (memory / "indexes").mkdir()
    route = call(
        "IndexRoute",
        route_id="route:general",
        anchor_prefixes=["component:", "concept:"],
        path_glob="indexes/general.md",
        summary="General bounded topic route for alpha records.",
        status_scope=["active", "archived", "deprecated", "scratch"],
    )
    index = call(
        "ActiveMemoryIndex",
        snapshot_id=snapshot,
        entries=[decision_entry],
        routes=[route],
    )
    write(memory / "index.md", render_markdown("Active Memory Index", index))
    shard = call(
        "TopicIndex",
        topic_id="topic:general",
        snapshot_id=snapshot,
        anchors=["component:alpha", "concept:alpha"],
        entries=[],
        archive_route="archive/manifest.md",
    )
    write(memory / "indexes" / "general.md", render_markdown("Topic Index: General", shard))
    return memory


def replace_snapshot(path: Path, before: str, after: str) -> None:
    text = path.read_text(encoding="utf-8")
    if before not in text:
        raise AssertionError(f"snapshot {before} not present in {path}")
    write(path, text.replace(before, after))


def config(memory: Path) -> dict:
    return json.loads((memory / "config.json").read_text(encoding="utf-8"))
