from __future__ import annotations

import csv
import io
import json
import uuid
from pathlib import Path
from typing import Any, Iterator

from charset_normalizer import from_bytes

from sentinel_loader.models import ColumnMapping, FileKind, FileSummary, Issue, ParseOptions
from sentinel_loader.schema import infer_column_type, sanitize_column_name, uniquify_names

SAMPLE_ROW_LIMIT = 25
INFER_ROW_LIMIT = 2_000
PREVIEW_VALUE_LIMIT = 5


def detect_encoding(raw: bytes) -> str:
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return "utf-16"
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    result = from_bytes(raw[: 256_000]).best()
    if result is None:
        return "utf-8"
    encoding = result.encoding or "utf-8"
    if encoding.lower() in {"ascii", "utf_8"}:
        return "utf-8"
    return encoding


def _decode(raw: bytes, encoding: str) -> str:
    return raw.decode(encoding, errors="replace")


def detect_kind(name: str, text: str) -> FileKind:
    lower = name.lower()
    if lower.endswith((".jsonl", ".ndjson")):
        return "jsonl"
    if lower.endswith(".json"):
        return "json"
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        first_line = stripped.splitlines()[0] if stripped else ""
        if stripped.startswith("[") or (first_line.startswith("{") and not _looks_like_jsonl(stripped)):
            return "json"
        if _looks_like_jsonl(stripped):
            return "jsonl"
    if lower.endswith((".csv", ".tsv", ".txt")):
        return "csv"
    if "," in text.splitlines()[0] if text.splitlines() else False:
        return "csv"
    if "\t" in (text.splitlines()[0] if text.splitlines() else ""):
        return "csv"
    return "raw"


def _looks_like_jsonl(text: str) -> bool:
    lines = [ln for ln in text.splitlines() if ln.strip()][:8]
    if len(lines) < 2:
        return False
    return all(ln.lstrip().startswith("{") and ln.rstrip().endswith("}") for ln in lines)


def detect_csv_dialect(text: str) -> tuple[str, bool]:
    sample = "\n".join(text.splitlines()[:80])
    if not sample.strip():
        return ",", True
    first = text.splitlines()[0] if text.splitlines() else ""
    if first.lower().startswith("sep="):
        sep = first[4:].strip()[:1] or ","
        return sep, True
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        if "\t" in first and first.count("\t") >= first.count(","):
            delimiter = "\t"
        elif first.count(";") > first.count(","):
            delimiter = ";"
        else:
            delimiter = ","
    try:
        has_header = csv.Sniffer().has_header(sample)
    except csv.Error:
        has_header = True
    return delimiter, has_header


def _open_text(path: Path, encoding: str) -> io.TextIOBase:
    return path.open("r", encoding=encoding, errors="replace", newline="")


def parse_file(path: Path, original_name: str, options: ParseOptions | None = None) -> FileSummary:
    options = options or ParseOptions()
    raw = path.read_bytes()
    encoding = options.encoding or detect_encoding(raw)
    # Only decode a prefix for detection; full parse reads from disk.
    preview_text = _decode(raw[: min(len(raw), 512_000)], encoding)
    kind = options.kind or detect_kind(original_name, preview_text)

    if kind == "json":
        return _parse_json(path, original_name, encoding, raw)
    if kind == "jsonl":
        return _parse_jsonl(path, original_name, encoding)
    if kind == "raw":
        return _parse_raw(path, original_name, encoding)
    return _parse_csv(path, original_name, encoding, preview_text, options)


def iter_source_rows(path: Path, summary: FileSummary) -> Iterator[dict[str, Any]]:
    if summary.kind == "csv":
        yield from _iter_csv_rows(path, summary)
    elif summary.kind == "json":
        payload = json.loads(path.read_text(encoding=summary.encoding, errors="replace"))
        rows = _json_as_rows(payload)
        for row in rows:
            yield {str(k): v for k, v in row.items()}
    elif summary.kind == "jsonl":
        with _open_text(path, summary.encoding) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield {str(k): v for k, v in obj.items()}
    else:
        with _open_text(path, summary.encoding) as handle:
            for i, line in enumerate(handle, start=1):
                yield {"LineNumber": i, "RawData": line.rstrip("\n")}


def _parse_csv(
    path: Path,
    original_name: str,
    encoding: str,
    preview_text: str,
    options: ParseOptions,
) -> FileSummary:
    delimiter = options.delimiter or detect_csv_dialect(preview_text)[0]
    has_header = options.has_header
    if has_header is None:
        has_header = detect_csv_dialect(preview_text)[1]

    issues: list[Issue] = []
    rows: list[list[str]] = []
    total = 0
    with _open_text(path, encoding) as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        for i, row in enumerate(reader):
            if i == 0 and row and row[0].lower().startswith("sep="):
                continue
            total += 1
            if len(rows) < INFER_ROW_LIMIT:
                rows.append(row)

    if not rows:
        issues.append(
            Issue(
                severity="error",
                code="empty_file",
                message="File is empty or has no parseable rows.",
                file_name=original_name,
            )
        )
        return FileSummary(
            file_id=str(uuid.uuid4()),
            name=original_name,
            size_bytes=path.stat().st_size,
            kind="csv",
            encoding=encoding,
            delimiter=delimiter,
            has_header=bool(has_header),
            row_count=0,
            columns=[],
            sample_rows=[],
            issues=issues,
        )

    if has_header:
        header = rows[0]
        data_rows = rows[1:]
        total_data = max(total - 1, 0)
    else:
        width = max(len(r) for r in rows)
        header = [f"Column{i+1}" for i in range(width)]
        data_rows = rows
        total_data = total

    width = max(len(header), max((len(r) for r in data_rows), default=0))
    header = list(header) + [f"Column{i+1}" for i in range(len(header), width)]
    sanitized = []
    reasons = []
    for name in header:
        s, reason = sanitize_column_name(str(name) if name is not None else "")
        sanitized.append(s)
        reasons.append(reason)
    sentinel_names = uniquify_names(sanitized)

    columns: list[ColumnMapping] = []
    for idx, (original, sentinel, reason) in enumerate(zip(header, sentinel_names, reasons)):
        values = []
        for row in data_rows:
            cell = row[idx] if idx < len(row) else ""
            values.append(cell)
        samples = [v for v in values if v][:PREVIEW_VALUE_LIMIT]
        inferred = infer_column_type(values[:INFER_ROW_LIMIT])
        columns.append(
            ColumnMapping(
                original_name=str(original),
                sentinel_name=sentinel,
                type=inferred,
                include=True,
                sample_values=samples,
                renamed=reason is not None or sentinel != str(original),
                rename_reason=reason,
            )
        )

    sample_rows: list[dict[str, Any]] = []
    for row in data_rows[:SAMPLE_ROW_LIMIT]:
        sample_rows.append(
            {
                col.sentinel_name: (row[i] if i < len(row) else "")
                for i, col in enumerate(columns)
            }
        )

    if total_data == 0:
        issues.append(
            Issue(
                severity="error",
                code="header_only",
                message="File has a header but no data rows.",
                file_name=original_name,
            )
        )

    return FileSummary(
        file_id=str(uuid.uuid4()),
        name=original_name,
        size_bytes=path.stat().st_size,
        kind="csv",
        encoding=encoding,
        delimiter=delimiter,
        has_header=bool(has_header),
        row_count=total_data,
        columns=columns,
        sample_rows=sample_rows,
        issues=issues,
    )


def _iter_csv_rows(path: Path, summary: FileSummary) -> Iterator[dict[str, Any]]:
    delimiter = summary.delimiter or ","
    with _open_text(path, summary.encoding) as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        first = True
        for row in reader:
            if first and row and row[0].lower().startswith("sep="):
                continue
            if first and summary.has_header:
                first = False
                continue
            first = False
            record: dict[str, Any] = {}
            for i, col in enumerate(summary.columns):
                cell = row[i] if i < len(row) else ""
                record[col.original_name] = cell
                record[col.sentinel_name] = cell
            yield record


def _json_as_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item if isinstance(item, dict) else {"value": item} for item in payload]
    if isinstance(payload, dict):
        for key in ("records", "value", "logs", "data", "items", "events"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [item if isinstance(item, dict) else {"value": item} for item in inner]
        return [payload]
    return [{"value": payload}]


def _columns_from_dicts(rows: list[dict[str, Any]], original_name: str) -> tuple[list[ColumnMapping], list[dict[str, Any]], list[Issue]]:
    issues: list[Issue] = []
    keys: list[str] = []
    seen = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                keys.append(str(key))
    if not keys:
        issues.append(
            Issue(
                severity="error",
                code="empty_file",
                message="JSON contained no fields.",
                file_name=original_name,
            )
        )
        return [], [], issues

    sanitized = []
    reasons = []
    for name in keys:
        s, reason = sanitize_column_name(name)
        sanitized.append(s)
        reasons.append(reason)
    sentinel_names = uniquify_names(sanitized)

    columns: list[ColumnMapping] = []
    for original, sentinel, reason in zip(keys, sentinel_names, reasons):
        values = []
        for row in rows[:INFER_ROW_LIMIT]:
            value = row.get(original)
            values.append("" if value is None else str(value) if not isinstance(value, (dict, list)) else json.dumps(value))
        samples = [v for v in values if v][:PREVIEW_VALUE_LIMIT]
        inferred = infer_column_type(values)
        # Nested objects are dynamic.
        if any(isinstance(row.get(original), (dict, list)) for row in rows[:50]):
            inferred = "dynamic"
        columns.append(
            ColumnMapping(
                original_name=original,
                sentinel_name=sentinel,
                type=inferred,
                include=True,
                sample_values=samples,
                renamed=reason is not None or sentinel != original,
                rename_reason=reason,
            )
        )

    sample_rows = []
    for row in rows[:SAMPLE_ROW_LIMIT]:
        sample_rows.append(
            {
                col.sentinel_name: _stringify(row.get(col.original_name))
                for col in columns
            }
        )
    return columns, sample_rows, issues


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def _parse_json(path: Path, original_name: str, encoding: str, raw: bytes) -> FileSummary:
    issues: list[Issue] = []
    try:
        payload = json.loads(raw.decode(encoding, errors="replace"))
    except json.JSONDecodeError as exc:
        issues.append(
            Issue(
                severity="error",
                code="invalid_json",
                message=f"Invalid JSON: {exc}",
                file_name=original_name,
            )
        )
        return FileSummary(
            file_id=str(uuid.uuid4()),
            name=original_name,
            size_bytes=path.stat().st_size,
            kind="json",
            encoding=encoding,
            row_count=0,
            columns=[],
            sample_rows=[],
            issues=issues,
        )
    rows = _json_as_rows(payload)
    columns, sample_rows, extra = _columns_from_dicts(rows, original_name)
    return FileSummary(
        file_id=str(uuid.uuid4()),
        name=original_name,
        size_bytes=path.stat().st_size,
        kind="json",
        encoding=encoding,
        has_header=True,
        row_count=len(rows),
        columns=columns,
        sample_rows=sample_rows,
        issues=issues + extra,
    )


def _parse_jsonl(path: Path, original_name: str, encoding: str) -> FileSummary:
    rows: list[dict[str, Any]] = []
    total = 0
    issues: list[Issue] = []
    with _open_text(path, encoding) as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                issues.append(
                    Issue(
                        severity="warning",
                        code="jsonl_skip",
                        message=f"Skipped line {line_no}: {exc}",
                        file_name=original_name,
                    )
                )
                continue
            total += 1
            if isinstance(obj, dict) and len(rows) < INFER_ROW_LIMIT:
                rows.append(obj)
            elif not isinstance(obj, dict) and len(rows) < INFER_ROW_LIMIT:
                rows.append({"value": obj})
    columns, sample_rows, extra = _columns_from_dicts(rows, original_name)
    return FileSummary(
        file_id=str(uuid.uuid4()),
        name=original_name,
        size_bytes=path.stat().st_size,
        kind="jsonl",
        encoding=encoding,
        has_header=True,
        row_count=total,
        columns=columns,
        sample_rows=sample_rows,
        issues=issues + extra,
    )


def _parse_raw(path: Path, original_name: str, encoding: str) -> FileSummary:
    lines = path.read_text(encoding=encoding, errors="replace").splitlines()
    columns = [
        ColumnMapping(
            original_name="LineNumber",
            sentinel_name="LineNumber",
            type="int",
            sample_values=["1", "2", "3"],
        ),
        ColumnMapping(
            original_name="RawData",
            sentinel_name="RawData",
            type="string",
            sample_values=[ln for ln in lines[:PREVIEW_VALUE_LIMIT]],
        ),
    ]
    sample_rows = [
        {"LineNumber": str(i), "RawData": line}
        for i, line in enumerate(lines[:SAMPLE_ROW_LIMIT], start=1)
    ]
    issues = []
    if not lines:
        issues.append(
            Issue(
                severity="error",
                code="empty_file",
                message="File is empty.",
                file_name=original_name,
            )
        )
    return FileSummary(
        file_id=str(uuid.uuid4()),
        name=original_name,
        size_bytes=path.stat().st_size,
        kind="raw",
        encoding=encoding,
        has_header=False,
        row_count=len(lines),
        columns=columns,
        sample_rows=sample_rows,
        issues=issues,
    )
