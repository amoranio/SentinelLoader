from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from sentinel_loader.models import (
    ColumnMapping,
    FileSummary,
    Issue,
    TimeGeneratedConfig,
    TransformOptions,
)
from sentinel_loader.parser import iter_source_rows
from sentinel_loader.schema import (
    azure_column_type,
    coerce_value,
    ensure_cl_suffix,
    parse_datetime,
    stream_name_for,
)


def to_sentinel_record(
    row: dict[str, Any],
    columns: list[ColumnMapping],
    time_generated: TimeGeneratedConfig,
    *,
    source_file: str | None = None,
    source_row: int | None = None,
    ingest_time: datetime | None = None,
    file_mtime: datetime | None = None,
) -> dict[str, Any]:
    now = ingest_time or datetime.now(timezone.utc)
    record: dict[str, Any] = {}
    for column in columns:
        if not column.include:
            continue
        if column.sentinel_name == "TimeGenerated":
            continue
        raw = _lookup(row, column)
        value = coerce_value(raw, column.type)
        if value is None:
            continue
        record[column.sentinel_name] = value

    record["TimeGenerated"] = _resolve_time_generated(
        row, columns, time_generated, now, file_mtime
    )

    if source_file:
        record.setdefault("SourceFile", source_file)
    if source_row is not None:
        record.setdefault("SourceRow", source_row)
    return record


def iter_sentinel_records(
    path: Path,
    summary: FileSummary,
    columns: list[ColumnMapping],
    options: TransformOptions,
    *,
    ingest_time: datetime | None = None,
) -> Iterator[dict[str, Any]]:
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    for index, row in enumerate(iter_source_rows(path, summary), start=1):
        yield to_sentinel_record(
            row,
            columns,
            options.time_generated,
            source_file=summary.name if options.add_source_file else None,
            source_row=index if options.add_source_row else None,
            ingest_time=ingest_time,
            file_mtime=mtime,
        )


def build_table_schema(table_name: str, columns: list[ColumnMapping], options: TransformOptions) -> dict[str, Any]:
    table = ensure_cl_suffix(table_name)
    azure_columns = [{"name": "TimeGenerated", "type": "dateTime"}]
    seen = {"TimeGenerated"}
    for column in columns:
        if not column.include or column.sentinel_name in seen:
            continue
        azure_columns.append(
            {"name": column.sentinel_name, "type": azure_column_type(column.type)}
        )
        seen.add(column.sentinel_name)
    if options.add_source_file and "SourceFile" not in seen:
        azure_columns.append({"name": "SourceFile", "type": "string"})
        seen.add("SourceFile")
    if options.add_source_row and "SourceRow" not in seen:
        azure_columns.append({"name": "SourceRow", "type": "int"})
    return {
        "properties": {
            "schema": {"name": table, "columns": azure_columns},
            "plan": "Analytics",
        }
    }


def build_dcr_stream_declaration(
    table_name: str, columns: list[ColumnMapping], options: TransformOptions
) -> dict[str, Any]:
    schema = build_table_schema(table_name, columns, options)
    cols = schema["properties"]["schema"]["columns"]
    stream = stream_name_for(table_name)
    return {stream: {"columns": cols}}


def kql_example(table_name: str) -> str:
    table = ensure_cl_suffix(table_name)
    return (
        f"{table}\n"
        f"| where TimeGenerated > ago(1d)\n"
        f"| take 50"
    )


def preview_issues(
    columns: list[ColumnMapping],
    time_generated: TimeGeneratedConfig,
    row_count: int,
) -> list[Issue]:
    issues: list[Issue] = []
    included = [c for c in columns if c.include]
    if row_count == 0:
        issues.append(
            Issue(
                severity="error",
                code="no_rows",
                message="Nothing to ingest — no data rows were parsed.",
            )
        )
    if not included:
        issues.append(
            Issue(
                severity="error",
                code="no_columns",
                message="No columns are selected for ingest.",
            )
        )
    if time_generated.mode == "column" and not time_generated.column:
        issues.append(
            Issue(
                severity="error",
                code="time_column_missing",
                message="TimeGenerated is mapped from a column, but no column was selected.",
            )
        )
    if time_generated.mode == "ingest_time":
        issues.append(
            Issue(
                severity="warning",
                code="time_is_ingest",
                message="TimeGenerated will be the upload time, not the original event time. "
                "Map a timestamp column if these logs happened earlier — otherwise queries by time will be misleading.",
            )
        )
    if time_generated.mode == "file_mtime":
        issues.append(
            Issue(
                severity="info",
                code="time_is_mtime",
                message="TimeGenerated will use each file's last-modified time for every row in that file.",
            )
        )
    return issues


def table_callout(mode: str, table_name: str) -> str:
    table = ensure_cl_suffix(table_name)
    if mode == "create":
        return (
            f"A custom table must exist before the Logs Ingestion API will accept rows. "
            f"SentinelLoader can create `{table}` in your Log Analytics workspace, plus a Direct "
            f"Data Collection Rule (DCR) that points at it, and grant this identity the "
            f"Monitoring Metrics Publisher role on that DCR. You do not need to pre-create the "
            f"table in the Azure portal if this identity has permission."
        )
    return (
        f"The Logs Ingestion API will not create `{table}` for you at upload time. "
        f"The table and a Data Collection Rule with a matching stream must already exist. "
        f"If they do not, switch to “Create table + DCR” or create them in the Azure portal "
        f"(Log Analytics workspace → Tables → New custom log)."
    )


def _lookup(row: dict[str, Any], column: ColumnMapping) -> Any:
    if column.sentinel_name in row:
        return row[column.sentinel_name]
    if column.original_name in row:
        return row[column.original_name]
    lowered = {str(k).lower(): v for k, v in row.items()}
    return lowered.get(column.original_name.lower(), lowered.get(column.sentinel_name.lower()))


def _resolve_time_generated(
    row: dict[str, Any],
    columns: list[ColumnMapping],
    config: TimeGeneratedConfig,
    now: datetime,
    file_mtime: datetime | None,
) -> str:
    if config.mode == "file_mtime" and file_mtime is not None:
        return _iso(file_mtime)
    if config.mode == "column" and config.column:
        match = next(
            (c for c in columns if c.sentinel_name == config.column or c.original_name == config.column),
            None,
        )
        raw = _lookup(row, match) if match else row.get(config.column)
        if raw is not None and str(raw).strip():
            parsed = parse_datetime(str(raw))
            if parsed is not None:
                return _iso(parsed)
            return str(raw).strip()
    return _iso(now)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
