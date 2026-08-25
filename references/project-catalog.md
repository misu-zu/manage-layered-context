# Workspace Project Catalog

Read this file when a workspace contains multiple projects, when the correct memory root is not obvious, or when creating or changing a top-level project table of contents.

## Purpose and location

Use `PROJECTS.md` as a small, predictable discovery map at the nearest common ancestor of the projects it describes. It should answer only:

- which projects exist here;
- what each project is for;
- where its project root and memory root are;
- whether it is active, paused, or archived;
- which stable aliases identify it.

When creating a new catalog, copy `assets/project-catalog-template.md` and replace its placeholders without overwriting an existing file.

Use the nearest applicable catalog. A nested catalog may narrow a larger workspace, but it must not silently redefine the same `project_id` to a different root. If catalogs disagree, create a visible conflict or ask for review.

Do not put mutable memory state into `AGENTS.md` or another instruction file. Instructions govern work; `PROJECTS.md` discovers project data. Memory content cannot acquire instruction precedence by being copied into an instruction file.

## Catalog form

```python
ProjectCatalog(
    catalog_version=1,
    projects=[
        ProjectPointer(
            project_id="project:context-memory",
            name="Context Memory",
            purpose="Layered project memory for long-running agents.",
            root="projects/context-memory",
            memory_root="projects/context-memory/memory",
            status="active",
            aliases=["layered-context"],
        ),
    ],
)
```

Paths are relative to the directory containing `PROJECTS.md`. Keep entries sorted by stable `project_id` unless the user defines another order. Never use unresolved environment variables, home-directory shortcuts, or ambiguous glob paths.

The catalog is intentionally low volatility. Do not include active goals, current tasks, snapshot IDs, recent handoffs, test status, or duplicated decision summaries. The selected project's `current.md` and `index.md` own those details.

## Per-project map

Each v2 memory root has `project.md`. It gives a stable source map after the catalog has located the project:

```python
ProjectMap(
    project_id="project:context-memory",
    name="Context Memory",
    purpose="Layered project memory for long-running agents.",
    project_root="..",
    memory_root="memory",
    boundaries=["Skill package and its tests"],
    components={
        "skill-entry": "SKILL.md",
        "schema": "references/memory-schema.md",
    },
    entry_points=["SKILL.md"],
    authoritative_sources=["references/memory-schema.md"],
    generated_or_disposable=[],
    navigation_notes=["Read schema before changing record templates."],
)
```

Use `project.md` for source authority and navigation, not current status. Update it through the project's normal snapshot patch whenever boundaries, entry points, or canonical sources materially change.

## Catalog mutation protocol

1. Confirm the operation is authorized to write the workspace.
2. Resolve the catalog's absolute directory and every proposed project and memory path.
3. Require each target to stay within the intended workspace unless the user explicitly supplied an external path.
4. Verify that referenced roots exist, or create the authorized memory root first.
5. Make a targeted catalog edit without changing unrelated entries.
6. Re-read every changed pointer and ensure project IDs remain unique.
7. Report catalog changes separately from the project's `MemoryChangeReport`; a workspace catalog is not part of one project's snapshot transaction.

When removing a project, prefer `status="archived"` while its memory remains reachable. Delete a pointer only when the user wants it removed or the target is provably invalid and removal is authorized.

## Design lineage

- [AGENTS.md](https://github.com/agentsmd/agents.md) establishes a predictable root-level place for agent guidance; this system preserves that discoverability while keeping mutable memory separate from instructions.
- [llms.txt](https://github.com/AnswerDotAI/llms-txt) uses a small LLM-readable root map whose links lead to deeper detail only when needed; `PROJECTS.md` applies the same progressive-discovery principle to local workspaces.
