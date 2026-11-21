"""Render aggregate evaluation evidence for CI summaries and release notes."""

import argparse
import json
from pathlib import Path


def render(report: dict) -> str:
    lines = [f"## {report['kind']} ({report['status']})", ""]
    if report["kind"] == "deterministic_synthetic_eval":
        lines += ["| History | Recall@3 | Current tokens | Root tokens | Largest shard | Exact budgets |",
                  "| ---: | ---: | ---: | ---: | ---: | --- |"]
        for row in report["histories"]:
            stats = row["inspection"]
            recall = row["metrics"]["recall_at_3"]
            lines.append(f"| {row['task_count']} | {recall['hits']}/{recall['total']} | {stats['current_tokens']} "
                         f"| {stats['root_index_tokens']} | {stats['topic_index_max_tokens']} | {row['cli']['budget_exact_ok']} |")
    elif report["kind"] == "paired_live_eval":
        setup = report["preflight"]
        lines += [f"{setup['model']}, {setup['codex_cli_version']}. {setup['total_runs']} runs.",
                  f"Baseline `{setup['v2_runtime_id']}`, candidate `{setup['v3_runtime_id']}`.", "",
                  "| Gate | Observed | Required | Passed |", "| --- | --- | --- | --- |"]
        for gate in report["gates"]:
            lines.append(f"| {gate['name']} | {gate['actual']} | {gate['comparison']} {gate['threshold']} | {gate['passed']} |")
        lines += ["", "| Arm | Category | Scored / runs | Metric | Passed / applicable |",
                  "| --- | --- | ---: | --- | ---: |"]
        for arm, rows in report["categories"].items():
            for category, row in rows.items():
                for name, metric in row["metrics"].items():
                    if "passed" in metric:
                        lines.append(f"| {arm} | {category} | {row['scored_count']}/{row['run_count']} | "
                                     f"{name} | {metric['passed']}/{metric['total']} |")
    else:
        lines.append("Preflight only. No behavioral scores.")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path)
    args = parser.parse_args()
    print(render(json.loads(args.result.read_text(encoding="utf-8"))), end="")
