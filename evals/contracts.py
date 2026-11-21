from __future__ import annotations

import json
import math
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


RESULT_SCHEMA_VERSION = "1.1.0"
TOKENIZER_NAME = "o200k_base"
PINNED_V2_SHA = "3ea56f1dcb98f098a7354b45ee4a0457fe1fae8d"
MAX_CONCURRENCY = 16

ABSOLUTE_GATES = {
    "unstable_boundary_detected": 1.0,
    "resume_success": 0.95,
    "required_record_recall_at_3": 0.95,
    "required_fact_extraction": 0.95,
    "decision_id_exact": 0.95,
    "contradiction_flag_accuracy": 0.95,
    "contradiction_id_exact": 0.95,
    "next_action_id_exact": 0.95,
}
CORRECTNESS_METRICS = (*ABSOLUTE_GATES, "abstention_accuracy")
DIAGNOSTIC_METRICS = ("abstention_output_contract",)
RATE_METRICS = (*CORRECTNESS_METRICS, *DIAGNOSTIC_METRICS)


class ContractError(RuntimeError):
    """Raised when an evaluator dependency violates its documented contract."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            result["details"] = self.details
        return result


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError("missing_file", f"Required file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(
            "invalid_json",
            f"Invalid JSON in {path}: line {exc.lineno}, column {exc.colno}",
        ) from exc


def parse_json_document(text: str, source: str) -> Mapping[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractError(
            "invalid_json_output",
            f"{source} did not emit one JSON document: line {exc.lineno}, column {exc.colno}",
            stdout=text[-2000:],
        ) from exc
    if not isinstance(value, dict):
        raise ContractError(
            "invalid_json_output",
            f"{source} JSON output must be an object",
            actual_type=type(value).__name__,
        )
    return value


def write_json_result(result: Mapping[str, Any], output: Path | None) -> str:
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8", newline="\n")
    return rendered


def require_exact_tokenizer() -> Any:
    try:
        import tiktoken  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ContractError(
            "exact_tokenizer_unavailable",
            "tiktoken is required for exact o200k_base token counts",
            tokenizer=TOKENIZER_NAME,
        ) from exc
    try:
        return tiktoken.get_encoding(TOKENIZER_NAME)
    except Exception as exc:
        raise ContractError(
            "exact_tokenizer_unavailable",
            f"tiktoken could not load {TOKENIZER_NAME}",
            tokenizer=TOKENIZER_NAME,
        ) from exc


def count_tokens(encoding: Any, text: str) -> int:
    return len(encoding.encode_ordinary(text))


def normalize_relative_path(value: str, *, allowed_prefixes: Sequence[str]) -> str:
    normalized = value.replace("\\", "/")
    if ":" in normalized.split("/", 1)[0]:
        raise ContractError("unsafe_path", f"Drive-qualified path is not allowed: {value}")
    path = PurePosixPath(normalized)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ContractError("unsafe_path", f"Unsafe relative path: {value}")
    rendered = path.as_posix()
    if not any(rendered == prefix or rendered.startswith(prefix + "/") for prefix in allowed_prefixes):
        raise ContractError(
            "unsafe_path",
            f"Path is outside allowed prefixes: {value}",
            allowed_prefixes=list(allowed_prefixes),
        )
    return rendered


def exact_loaded_file_tokens(
    workspace: Path,
    loaded_files: Iterable[str],
    encoding: Any,
) -> tuple[int, list[str]]:
    normalized_paths: list[str] = []
    seen: set[str] = set()
    total = 0
    workspace_resolved = workspace.resolve()
    for raw_path in loaded_files:
        path = normalize_relative_path(
            raw_path,
            allowed_prefixes=("skill", "project/memory"),
        )
        if path in seen:
            continue
        seen.add(path)
        candidate = (workspace / PurePosixPath(path)).resolve()
        try:
            candidate.relative_to(workspace_resolved)
        except ValueError as exc:
            raise ContractError("unsafe_path", f"Resolved path escapes workspace: {raw_path}") from exc
        if not candidate.is_file():
            raise ContractError("reported_file_missing", f"Reported loaded file does not exist: {path}")
        text = candidate.read_text(encoding="utf-8")
        total += count_tokens(encoding, f"{path}\n{text}\n")
        normalized_paths.append(path)
    return total, normalized_paths


def nearest_rank(values: Sequence[int | float], percentile: float) -> int | float:
    if not values:
        raise ContractError("empty_metric", "Cannot compute a percentile over an empty sequence")
    if not 0 < percentile <= 1:
        raise ValueError("percentile must be in (0, 1]")
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def rate_entry(values: Iterable[bool | None]) -> dict[str, Any]:
    applicable = [value for value in values if value is not None]
    passed = sum(1 for value in applicable if value)
    total = len(applicable)
    return {
        "passed": passed,
        "total": total,
        "rate": (passed / total) if total else None,
    }


def require_bool(mapping: Mapping[str, Any], field: str, source: str) -> bool:
    value = mapping.get(field)
    if type(value) is not bool:
        raise ContractError(
            "invalid_contract_field",
            f"{source}.{field} must be a boolean",
            actual=value,
        )
    return value


def require_string_list(mapping: Mapping[str, Any], field: str, source: str) -> list[str]:
    value = mapping.get(field)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ContractError(
            "invalid_contract_field",
            f"{source}.{field} must be an array of strings",
            actual=value,
        )
    return value
