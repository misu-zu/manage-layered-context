"""Contract-compatible fake used only by evaluator unit tests."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def _option(args: list[str], name: str) -> str:
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError) as exc:
        raise SystemExit(f"missing option: {name}") from exc


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) < 2:
        raise SystemExit("usage: fake_memoryctl.py COMMAND ROOT [OPTIONS]")
    command = args[0]
    root = Path(args[1])
    if not root.is_dir():
        print(json.dumps({"ok": False, "errors": ["root missing"]}))
        return 1

    if command == "validate":
        required = [root / "current.md", root / "index.md"]
        errors = [str(path) for path in required if not path.is_file()]
        print(json.dumps({"command": "validate", "ok": not errors, "errors": errors, "warnings": []}))
        return 0 if not errors else 1

    if command == "budget":
        files = []
        for path in sorted(root.rglob("*.md")):
            files.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "tokens": len(path.read_text(encoding="utf-8").split()),
                    "limit": 2000,
                    "within_limit": True,
                }
            )
        print(
            json.dumps(
                {
                    "ok": True,
                    "schema_version": 3,
                    "items": [
                        {
                            "kind": "topic_index",
                            "path": item["path"],
                            "tokens": item["tokens"],
                            "target": 1500,
                            "hard": 2000,
                            "exact": True,
                            "method": "tiktoken:o200k_base",
                            "over_target": False,
                            "over_hard": False,
                        }
                        for item in files
                    ],
                }
            )
        )
        return 0

    if command == "recall":
        query = _option(args, "--query")
        match = re.search(r"task-(\d{6})", query)
        results = []
        if match:
            number = match.group(1)
            path = f"tasks/TASK-{number}.md"
            if (root / path).is_file():
                results.append({"path": path, "score": 1.0, "anchor": f"task-{number}"})
        print(
            json.dumps(
                {
                    "query": query,
                    "top": int(_option(args, "--top")),
                    "candidates": results,
                }
            )
        )
        return 0

    print(json.dumps({"ok": False, "errors": [f"unknown command: {command}"]}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
