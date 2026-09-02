from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable

from dateutil import parser as date_parser

from sentinel_loader.models import ColumnMapping, ColumnType, Issue

RESERVED_COLUMN_NAMES = {
    "_ResourceId",
    "id",
    "_SubscriptionId",
    "TenantId",
    "Type",
    "UniqueId",
    "Title",
}

# Azure Monitor custom column names: letter first, then alnum/underscore, max 45.
_INVALID_CHARS = re.compile(r"[^A-Za-z0-9_]")
_LEADING_NON_LETTER = re.compile(r"^[^A-Za-z]+")

INT32_MIN = -2_147_483_648
INT32_MAX = 2_147_483_647

TIME_NAME_HINTS = (
    "timegenerated",
    "timestamp",
    "eventtime",
    "event_time",
    "datetime",
    "date_time",
    "createdutc",
    "created_at",
    "createdat",
    "logtime",
    "log_time",
    "occurred",
    "occurredat",
    "time",
    "date",
)


def sanitize_column_name(name: str) -> tuple[str, str | None]:
    """Return (sentinel_name, reason_if_changed)."""
    original = name or ""
    cleaned = original.strip()
    if not cleaned:
        return "Column", "empty column name"

    cleaned = cleaned.replace(" ", "_").replace("-", "_").replace(".", "_")
    cleaned = _INVALID_CHARS.sub("_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = "Column"

    if _LEADING_NON_LETTER.match(cleaned):
        cleaned = "C_" + cleaned.lstrip("_")

    if len(cleaned) > 45:
        cleaned = cleaned[:45].rstrip("_")

    if cleaned in RESERVED_COLUMN_NAMES:
        cleaned = f"{cleaned}_col"[:45]

    if cleaned != original:
        return cleaned, f"renamed {original!r} → {cleaned!r}"
    return cleaned, None


def uniquify_names(names: list[str]) -> list[str]:
    seen: Counter[str] = Counter()
    out: list[str] = []
    for name in names:
        seen[name] += 1
        if seen[name] == 1 and name not in out:
            out.append(name)
            continue
        n = seen[name]
        candidate = f"{name}_{n}"
        while candidate in seen or candidate in out:
            n += 1
            candidate = f"{name}_{n}"
        seen[candidate] += 1
        out.append(candidate[:45])
    return out


def _is_bool(value: str) -> bool:
    return value.strip().lower() in {"true", "false", "0", "1", "yes", "no"}


def _is_int(value: str) -> bool:
    text = value.strip()
    if text.startswith(("+", "-")):
        text = text[1:]
    return text.isdigit() and "_" not in value


def _is_float(value: str) -> bool:
    text = value.strip().replace(",", "")
    if text.lower() in {"nan", "inf", "+inf", "-inf"}:
        return False
    try:
        float(text)
    except ValueError:
        return False
    return "." in text or "e" in text.lower()


def _is_datetime(value: str) -> bool:
    text = value.strip()
    if not text or len(text) < 6:
        return False
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        return False
    try:
        date_parser.parse(text)
    except (ValueError, OverflowError, TypeError):
        return False
    return bool(
        re.search(r"\d{4}[-/]", text)
        or re.search(r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}", text)
        or re.search(r"T\d{2}:", text)
        or re.search(r"\d{2}:\d{2}", text)
    )


def _is_dynamic(value: str) -> bool:
    text = value.strip()
    return (text.startswith("{") and text.endswith("}")) or (
        text.startswith("[") and text.endswith("]")
    )


def infer_column_type(values: Iterable[str | None]) -> ColumnType:
    samples = [v.strip() for v in values if v is not None and str(v).strip() != ""]
    if not samples:
        return "string"

    if all(_is_bool(v) and v.strip().lower() not in {"0", "1"} for v in samples):
        return "boolean"
    # 0/1 alone is too ambiguous; leave as int if numeric.

    if all(_is_int(v) for v in samples):
        ints = [int(v.strip()) for v in samples]
        if all(INT32_MIN <= n <= INT32_MAX for n in ints):
            return "int"
        return "long"

    if all(_is_int(v) or _is_float(v) for v in samples):
        return "real"

    if all(_is_datetime(v) for v in samples):
        return "datetime"

    if all(_is_dynamic(v) for v in samples):
        return "dynamic"

    return "string"


def parse_datetime(value: str) -> datetime | None:
    try:
        parsed = date_parser.parse(value.strip())
    except (ValueError, OverflowError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def coerce_value(value: Any, column_type: ColumnType) -> Any:
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    text = str(value).strip()
    try:
        if column_type == "boolean":
            return text.lower() in {"true", "1", "yes"}
        if column_type == "int":
            return int(float(text.replace(",", "")))
        if column_type == "long":
            return int(float(text.replace(",", "")))
        if column_type == "real":
            return float(text.replace(",", ""))
        if column_type == "datetime":
            parsed = parse_datetime(text)
            return parsed.isoformat().replace("+00:00", "Z") if parsed else text
        if column_type == "dynamic":
            import json

            return json.loads(text)
        return text
    except (ValueError, TypeError, OverflowError):
        return text


def azure_column_type(column_type: ColumnType) -> str:
    if column_type == "datetime":
        return "dateTime"
    return column_type


def score_time_column(column: ColumnMapping) -> int:
    name = column.sentinel_name.lower()
    original = column.original_name.lower()
    score = 0
    if column.type == "datetime":
        score += 50
    for i, hint in enumerate(TIME_NAME_HINTS):
        if hint == name or hint == original or hint in name or hint in original:
            score += 40 - i
            break
    if name == "timegenerated":
        score += 80
    return score


def suggest_time_generated_column(columns: list[ColumnMapping]) -> str | None:
    ranked = sorted(
        ((score_time_column(c), c.sentinel_name) for c in columns if c.include),
        reverse=True,
    )
    if not ranked or ranked[0][0] < 40:
        return None
    return ranked[0][1]


def merge_column_mappings(file_columns: list[list[ColumnMapping]]) -> list[ColumnMapping]:
    """Union columns across files, keeping first-seen types and samples."""
    by_name: dict[str, ColumnMapping] = {}
    for columns in file_columns:
        for column in columns:
            existing = by_name.get(column.sentinel_name)
            if existing is None:
                by_name[column.sentinel_name] = column.model_copy(deep=True)
                continue
            if existing.type != column.type:
                existing.type = "string"
            for sample in column.sample_values:
                if sample not in existing.sample_values and len(existing.sample_values) < 5:
                    existing.sample_values.append(sample)
    return list(by_name.values())


def mapping_issues(columns: list[ColumnMapping]) -> list[Issue]:
    issues: list[Issue] = []
    for column in columns:
        if column.renamed and column.rename_reason:
            issues.append(
                Issue(
                    severity="warning",
                    code="column_renamed",
                    message=column.rename_reason
                    + ". Azure Monitor column names must start with a letter and use only letters, numbers, and underscores (max 45).",
                    column=column.sentinel_name,
                )
            )
    return issues


def table_name_from_files(file_names: list[str]) -> str:
    if not file_names:
        return "ImportedLogs"
    stem = re.sub(r"\.[^.]+$", "", file_names[0])
    stem = re.sub(r"[^A-Za-z0-9_]", "", stem.replace(" ", "_").replace("-", "_"))
    stem = _LEADING_NON_LETTER.sub("", stem) or "ImportedLogs"
    return stem[:40]


def ensure_cl_suffix(table_name: str) -> str:
    name, _ = sanitize_column_name(table_name)
    if not name.endswith("_CL"):
        name = f"{name}_CL"
    return name[:45]


def stream_name_for(table_name: str) -> str:
    table = ensure_cl_suffix(table_name)
    base = table[:-3] if table.endswith("_CL") else table
    return f"Custom-{base}"
