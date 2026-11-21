#!/usr/bin/env python3
"""Unified command line for layered memory validation, recall, budgets, and writes."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from memory_model import SCHEMAS, V3_REQUIRED_FIELDS, ConfigError, TokenizerUnavailable
from memory_recall import DEFAULT_KINDS, recall, selected_kinds
from memory_transaction import (
    MODES,
    OPERATIONS,
    TransactionError,
    abort_transaction,
    add_transaction_files,
    begin_transaction,
    commit_transaction,
    migrate_v2_to_v3,
    recover_transaction,
)
from memory_validation import budget_report, validate_memory
from memory_writes import init_memory, put_record


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _validate_command(args: argparse.Namespace) -> int:
    if args.target_version == 2 and args.strict_v3:
        print("error: --target-version 2 conflicts with --strict-v3", file=sys.stderr)
        return 2
    if args.target_version == 3 and args.strict_v2:
        print("error: --target-version 3 conflicts with --strict-v2", file=sys.stderr)
        return 2
    findings = validate_memory(
        args.memory_root,
        catalog=args.catalog,
        strict_v2=args.strict_v2 or args.target_version == 2,
        strict_v3=args.strict_v3 or args.target_version == 3,
        require_exact_tokens=args.require_exact_tokens,
    )
    result = {"memory_root": str(args.memory_root.resolve()), **findings.as_dict()}
    if args.json:
        _print_json(result)
    else:
        print("memory validation: " + ("PASS" if findings.ok else "FAIL"))
        for message in findings.errors:
            print(f"error: {message}")
        for message in findings.warnings:
            print(f"warning: {message}")
        for message in findings.info:
            print(f"info: {message}")
    return 0 if findings.ok else 1


def _recall_command(args: argparse.Namespace) -> int:
    try:
        kinds = selected_kinds(args.kinds)
        if args.expand_to is not None and args.expand:
            raise ValueError("use either --expand or --expand-to, not both")
        if args.expand_to is not None and args.expand_to <= 0:
            raise ValueError("--expand-to must be positive")
        result = recall(
            args.memory_root,
            args.query,
            kinds=kinds,
            status=args.status,
            top=args.expand_to if args.expand_to is not None else args.top,
            expand=args.expand if args.expand_to is None else False,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        _print_json(result)
    else:
        print(f"snapshot: {result['snapshot_id'] or 'unknown'}")
        print(f"query: {result['query']}")
        print(f"coverage: {result['coverage']}")
        if result["selected_routes"]:
            print("routes: " + ", ".join(result["selected_routes"]))
        if not result["candidates"]:
            print("no matching candidate records")
        for index, candidate in enumerate(result["candidates"], start=1):
            terms = ", ".join(candidate["matched_terms"]) or "exact phrase"
            print(
                f"{index}. {candidate['path']} "
                f"(score={candidate['score']}, kind={candidate['kind']}, status={candidate['status']}, matches={terms})"
            )
        for warning in result["warnings"]:
            print(f"warning: {warning}", file=sys.stderr)
        print(result["notice"])
    return 0


def _budget_command(args: argparse.Namespace) -> int:
    try:
        result = budget_report(args.memory_root.resolve(), require_exact=args.require_exact)
    except (ConfigError, TokenizerUnavailable, OSError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    result["memory_root"] = str(args.memory_root.resolve())
    blocked = result["schema_version"] == 3 and any(item["exact"] and item["over_hard"] for item in result["items"])
    result["ok"] = not blocked
    if args.json:
        _print_json(result)
    else:
        print("memory budget: " + ("PASS" if result["ok"] else "FAIL"))
        for item in result["items"]:
            precision = "exact" if item["exact"] else f"estimated:{item['method']}"
            state = "HARD" if item["over_hard"] else "target" if item["over_target"] else "ok"
            if item["over_hard"] and not item["exact"]:
                state = "estimated overrun (warning)"
            print(
                f"{item['path']}: {item['tokens']} tokens ({precision}), "
                f"target={item['target']} hard={item['hard']} [{state}]"
            )
    return 0 if result["ok"] else 1


def _field_rule(rule: Any, *, required_in_v3: bool = False) -> dict[str, Any]:
    value = asdict(rule)
    result = {key: item for key, item in value.items() if item not in (None, ())}
    if required_in_v3:
        result["required_in_v3"] = True
    return result


def _schema_command(args: argparse.Namespace) -> int:
    names = [args.record] if args.record else sorted(SCHEMAS)
    unknown = [name for name in names if name not in SCHEMAS]
    if unknown:
        print(f"error: unknown record schema: {unknown[0]}", file=sys.stderr)
        return 2
    result = {
        name: {
            field: _field_rule(
                rule,
                required_in_v3=field in V3_REQUIRED_FIELDS.get(name, ()),
            )
            for field, rule in SCHEMAS[name].fields.items()
        }
        for name in names
    }
    if args.json or not args.record:
        _print_json(result)
    else:
        print(args.record)
        for field, rule in result[args.record].items():
            required = (
                "required-in-v3"
                if field in V3_REQUIRED_FIELDS.get(args.record, ())
                else "required"
                if SCHEMAS[args.record].fields[field].required
                else "optional"
            )
            print(f"- {field}: {' | '.join(rule['kinds'])} ({required})")
    return 0


def _migrate_command(args: argparse.Namespace) -> int:
    if args.to != 3:
        print("error: only --to 3 is supported", file=sys.stderr)
        return 2
    try:
        result = migrate_v2_to_v3(
            args.memory_root,
            target_snapshot_id=args.target_snapshot,
            apply=args.apply,
            mode=args.mode,
            confirm=args.confirm,
        )
    except (TransactionError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        _print_json(result)
    elif result["applied"]:
        print(f"migration committed: {result['target_snapshot_id']} ({result['patch_id']})")
    else:
        print("migration dry-run: no files changed")
        print(f"base: {result['base_snapshot_id']}")
        print(f"target: {result['target_snapshot_id']}")
        for path in result["files"]:
            print(f"would update: {path}")
    return 0


def _init_command(args: argparse.Namespace) -> int:
    _print_json(init_memory(args.memory_root, args.name, args.purpose))
    return 0


def _put_command(args: argparse.Namespace) -> int:
    _print_json(put_record(args.memory_root, args.source, path=args.path, mode=args.mode, summary=args.summary))
    return 0


def _txn_add(args: argparse.Namespace) -> int:
    _print_json(add_transaction_files(args.memory_root, args.patch_id, args.file, args.delete_file))
    return 0


def _txn_begin(args: argparse.Namespace) -> int:
    try:
        result = begin_transaction(
            args.memory_root,
            patch_id=args.patch_id,
            mode=args.mode,
            operation=args.operation,
            target_snapshot_id=args.target_snapshot,
            files=args.file,
            delete_files=args.delete_file,
            owner=args.owner,
        )
    except (TransactionError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    result["staging_directory"] = str(
        args.memory_root.resolve() / ".staging" / args.patch_id / "files"
    )
    _print_json(result)
    return 0


def _txn_commit(args: argparse.Namespace) -> int:
    try:
        result = commit_transaction(
            args.memory_root,
            args.patch_id,
            fail_after=args.fail_after,
        )
    except (TransactionError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print_json(result)
    return 0


def _txn_abort(args: argparse.Namespace) -> int:
    try:
        result = abort_transaction(args.memory_root, args.patch_id)
    except (TransactionError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print_json(result)
    return 0


def _txn_recover(args: argparse.Namespace) -> int:
    try:
        result = recover_transaction(args.memory_root, args.patch_id)
    except (TransactionError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print_json(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memoryctl",
        description="Validate, retrieve, budget, migrate, and transactionally update layered memory.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="Initialize a new or empty v3 root")
    init.add_argument("memory_root", type=Path)
    init.add_argument("--name")
    init.add_argument("--purpose")
    init.set_defaults(handler=_init_command)

    record = subparsers.add_parser("record", help="Commit an authored canonical record")
    put = record.add_subparsers(dest="action", required=True).add_parser("put")
    put.add_argument("memory_root", type=Path)
    put.add_argument("source", type=Path, help="Markdown containing one structured record")
    put.add_argument("--path", help="Canonical destination, default KIND/ID.md")
    put.add_argument("--mode", choices=MODES)
    put.add_argument("--summary", help="Short index summary")
    put.set_defaults(handler=_put_command)

    validate = subparsers.add_parser("validate", help="Validate one memory root")
    validate.add_argument("memory_root", type=Path)
    validate.add_argument("--catalog", type=Path)
    validate.add_argument("--strict-v2", action="store_true")
    validate.add_argument("--strict-v3", action="store_true")
    validate.add_argument("--target-version", type=int, choices=(2, 3))
    validate.add_argument("--require-exact-tokens", action="store_true")
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(handler=_validate_command)

    recall_parser = subparsers.add_parser("recall", help="Rank task-relevant canonical records")
    recall_parser.add_argument("memory_root", type=Path)
    recall_parser.add_argument("--query", required=True)
    recall_parser.add_argument("--kinds", default=",".join(DEFAULT_KINDS))
    recall_parser.add_argument("--status", help="Match canonical or index status")
    recall_parser.add_argument("--top", type=int)
    recall_parser.add_argument("--expand", action="store_true", help="Use configured expanded top-k")
    recall_parser.add_argument("--expand-to", type=int, help="Explicit expanded result limit, normally 8")
    recall_parser.add_argument("--json", action="store_true")
    recall_parser.set_defaults(handler=_recall_command)

    budget = subparsers.add_parser("budget", help="Measure current and index token budgets")
    budget.add_argument("memory_root", type=Path)
    budget.add_argument("--require-exact", action="store_true")
    budget.add_argument("--json", action="store_true")
    budget.set_defaults(handler=_budget_command)

    schema = subparsers.add_parser("schema", help="Show machine-enforced record schemas")
    schema.add_argument("record", nargs="?")
    schema.add_argument("--json", action="store_true")
    schema.set_defaults(handler=_schema_command)

    migrate = subparsers.add_parser("migrate", help="Plan or apply explicit v2-to-v3 migration")
    migrate.add_argument("memory_root", type=Path)
    migrate.add_argument("--to", type=int, default=3)
    migrate.add_argument("--target-snapshot", required=True)
    migrate.add_argument("--mode", choices=MODES, default="forensic")
    migrate_mode = migrate.add_mutually_exclusive_group()
    migrate_mode.add_argument("--dry-run", action="store_true", help="Plan only; this is the default")
    migrate_mode.add_argument("--apply", action="store_true")
    migrate.add_argument("--confirm")
    migrate.add_argument("--json", action="store_true")
    migrate.set_defaults(handler=_migrate_command)

    txn = subparsers.add_parser("txn", help="Manage exclusive staged memory transactions")
    txn_sub = txn.add_subparsers(dest="txn_command", required=True)
    begin = txn_sub.add_parser("begin")
    begin.add_argument("memory_root", type=Path)
    begin.add_argument("--patch-id", required=True)
    begin.add_argument("--mode", choices=MODES, default="standard")
    begin.add_argument("--operation", choices=OPERATIONS, default="update")
    target = begin.add_mutually_exclusive_group(required=True)
    target.add_argument("--target-snapshot")
    target.add_argument("--auto-target", action="store_true", help="Generate the next UTC snapshot")
    begin.add_argument("--file", action="append", required=True)
    begin.add_argument("--delete-file", action="append", default=[])
    begin.add_argument("--owner", default="memoryctl")
    begin.set_defaults(handler=_txn_begin)
    add = txn_sub.add_parser("add", help="Add paths to a prepared transaction")
    add.add_argument("memory_root", type=Path)
    add.add_argument("--patch-id", required=True)
    add.add_argument("--file", action="append", default=[])
    add.add_argument("--delete-file", action="append", default=[])
    add.set_defaults(handler=_txn_add)
    commit = txn_sub.add_parser("commit")
    commit.add_argument("memory_root", type=Path)
    commit.add_argument("--patch-id", required=True)
    commit.add_argument("--fail-after", type=int, help=argparse.SUPPRESS)
    commit.set_defaults(handler=_txn_commit)
    abort = txn_sub.add_parser("abort")
    abort.add_argument("memory_root", type=Path)
    abort.add_argument("--patch-id", required=True)
    abort.set_defaults(handler=_txn_abort)
    recover = txn_sub.add_parser("recover")
    recover.add_argument("memory_root", type=Path)
    recover.add_argument("--patch-id", required=True)
    recover.set_defaults(handler=_txn_recover)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (TransactionError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
