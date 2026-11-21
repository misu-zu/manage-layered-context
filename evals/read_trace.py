"""Extract attributable file reads from completed Codex command events."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any, Mapping, Sequence

from evals.contracts import ContractError


def observed_files(events: Sequence[Mapping[str, Any]], workspace: Path) -> list[str]:
    root = workspace.resolve()
    observed = []
    for event in events:
        command = event.get("command")
        if not isinstance(command, str):
            raise ContractError("unmeasured_read_trace", "Tool call has no attributable shell command")
        cwd = Path(event.get("cwd") or root)
        if not cwd.is_absolute():
            cwd = root / cwd
        try:
            tokens = shlex.split(command)
            if len(tokens) >= 3 and Path(tokens[0]).name in {"sh", "bash", "zsh", "pwsh", "powershell"}:
                if tokens[-2].lower() in {"-c", "-lc", "-command"}:
                    command = tokens[-1]
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
            lexer.whitespace_split = True
            lexer.commenters = ""
            groups: list[list[str]] = [[]]
            for word in lexer:
                if word in {"&&", ";", "|"}:
                    groups.append([])
                else:
                    groups[-1].append(word)
        except ValueError as exc:
            raise ContractError("unmeasured_read_trace", "Could not parse tool command") from exc
        if event.get("exit_code", 0) != 0:
            raise ContractError("unmeasured_read_trace", "A failed command may have returned partial reads")
        for args in groups:
            if not args:
                continue
            tool = Path(args[0]).name.lower()
            if tool in {"ls", "pwd", "dir", "get-childitem"} or (tool == "rg" and "--files" in args):
                continue
            if tool in {"cd", "set-location"} and len(args) == 2:
                cwd = (cwd / args[1]).resolve()
                continue
            if tool.startswith("python") and len(args) > 2 and (
                (Path(args[1]).name == "memoryctl.py" and args[2] in {"recall", "budget", "validate", "schema"})
                or Path(args[1]).name == "recall_context.py"
            ):
                continue  # Machine-side scans produce metadata, not agent detail reads.
            if tool not in {"cat", "head", "tail", "sed", "rg", "grep", "get-content", "gc", "type"}:
                raise ContractError("unmeasured_read_trace", f"Unattributed tool operation: {tool}")
            if any(re.search(r"[\x24*?{}<>]", arg) for arg in args[1:]):
                raise ContractError("unmeasured_read_trace", "Dynamic file arguments cannot be attributed")
            operands = list(args[1:])
            if tool in {"sed", "rg", "grep"}:
                # Accept a literal pattern/script and explicit file operands.
                while operands and operands[0] in {"-n", "-i", "-F", "-E", "--", "-e"}:
                    operands.pop(0)
                if not operands or operands.pop(0).startswith("-"):
                    raise ContractError("unmeasured_read_trace", "Unattributed search arguments")
            files = []
            for arg in operands:
                candidate = (cwd / arg.replace("\\", "/")).resolve()
                if not candidate.is_relative_to(root):
                    continue
                relative = candidate.relative_to(root).as_posix()
                if relative.startswith(("skill/", "project/memory/")) and candidate.is_file():
                    files.append(relative)
                elif candidate.is_dir() and tool in {"rg", "grep"}:
                    raise ContractError("unmeasured_read_trace", "Recursive search has unattributed file reads")
            if not files:
                raise ContractError("unmeasured_read_trace", f"Cannot verify all reads from {tool}")
            observed.extend(files)
    return list(dict.fromkeys(observed))
