from __future__ import annotations

import json
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from sentinel_loader.azure_auth import build_credential
from sentinel_loader.azure_ops import AzureError, SentinelClient
from sentinel_loader.models import (
    ColumnMapping,
    ConnectionConfig,
    DestinationConfig,
    ExistingDestination,
    FileSummary,
    Issue,
    ParseOptions,
    ParseResult,
    PreviewResult,
    TimeGeneratedConfig,
    TransformOptions,
)
from sentinel_loader.parser import parse_file
from sentinel_loader.schema import (
    ensure_cl_suffix,
    mapping_issues,
    merge_column_mappings,
    stream_name_for,
    suggest_time_generated_column,
    table_name_from_files,
)
from sentinel_loader.transform import (
    build_dcr_stream_declaration,
    build_table_schema,
    iter_sentinel_records,
    kql_example,
    preview_issues,
    table_callout,
    to_sentinel_record,
)

router = APIRouter(prefix="/api")

MAX_UPLOAD_BYTES = 512 * 1024 * 1024
PREVIEW_RECORDS = 12
SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"


@dataclass
class StoredFile:
    file_id: str
    name: str
    path: Path
    summary: FileSummary | None = None


@dataclass
class SessionState:
    session_id: str
    directory: Path
    files: dict[str, StoredFile] = field(default_factory=dict)
    connection: ConnectionConfig | None = None
    columns: list[ColumnMapping] = field(default_factory=list)
    transform: TransformOptions = field(default_factory=TransformOptions)
    destination: ExistingDestination | None = None


_SESSIONS: dict[str, SessionState] = {}


def _get_session(request: Request) -> SessionState:
    sid = request.session.get("sid")
    if sid and sid in _SESSIONS:
        return _SESSIONS[sid]
    sid = str(uuid.uuid4())
    request.session["sid"] = sid
    directory = Path(tempfile.mkdtemp(prefix=f"sentinel-loader-{sid[:8]}-"))
    state = SessionState(session_id=sid, directory=directory)
    _SESSIONS[sid] = state
    return state


class ConnectionPayload(ConnectionConfig):
    pass


class MappingPayload(BaseModel):
    columns: list[ColumnMapping]
    time_generated: TimeGeneratedConfig = Field(default_factory=TimeGeneratedConfig)
    add_source_file: bool = True
    add_source_row: bool = False
    table_name: str = "ImportedLogs"


class IngestPayload(BaseModel):
    mapping: MappingPayload
    destination: DestinationConfig


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "SentinelLoader"}


@router.post("/session/reset")
def reset_session(request: Request) -> dict[str, str]:
    sid = request.session.get("sid")
    if sid and sid in _SESSIONS:
        del _SESSIONS[sid]
    request.session.clear()
    return {"status": "reset"}


@router.post("/connection")
def save_connection(request: Request, payload: ConnectionPayload) -> dict[str, Any]:
    state = _get_session(request)
    state.connection = payload
    return {"saved": True, "auth_mode": payload.auth_mode, "workspace_name": payload.workspace_name}


@router.post("/connection/test")
def test_connection(request: Request, payload: ConnectionPayload | None = None) -> dict[str, Any]:
    state = _get_session(request)
    config = payload or state.connection
    if config is None:
        raise HTTPException(400, "Save Azure connection details first.")
    state.connection = config
    try:
        credential = build_credential(config)
        client = SentinelClient(config, credential)
        workspace = client.get_workspace()
        tables = client.list_tables()
        dcrs = client.list_dcrs()
        client.close()
    except Exception as exc:
        raise HTTPException(400, f"Could not reach the workspace: {exc}") from exc
    return {
        "ok": True,
        "workspace": {
            "name": config.workspace_name,
            "id": workspace.get("id"),
            "location": workspace.get("location"),
            "customer_id": (workspace.get("properties") or {}).get("customerId"),
        },
        "tables": tables,
        "dcrs": dcrs,
        "custom_table_count": sum(1 for t in tables if t.get("is_custom")),
        "note": (
            "Custom tables end in _CL. The Logs Ingestion API requires the table to exist "
            "before any rows can be sent. SentinelLoader can create one for you in the next step."
        ),
    }


def _parse_uploaded(state: SessionState, paths: list[tuple[str, Path]], options: ParseOptions) -> ParseResult:
    summaries: list[FileSummary] = []
    for filename, path in paths:
        summary = parse_file(path, filename, options)
        summary.file_id = path.name.split("__", 1)[0] if "__" in path.name else str(uuid.uuid4())
        state.files[summary.file_id] = StoredFile(
            file_id=summary.file_id, name=filename, path=path, summary=summary
        )
        summaries.append(summary)
    return _build_parse_result(state, summaries)


def _build_parse_result(state: SessionState, summaries: list[FileSummary]) -> ParseResult:
    merged = merge_column_mappings([s.columns for s in summaries if s.columns])
    time_col = suggest_time_generated_column(merged)
    time_cfg = (
        TimeGeneratedConfig(mode="column", column=time_col)
        if time_col
        else TimeGeneratedConfig(mode="ingest_time")
    )
    table = table_name_from_files([s.name for s in summaries])
    issues: list[Issue] = []
    for summary in summaries:
        issues.extend(summary.issues)
    issues.extend(mapping_issues(merged))
    schemas = {tuple(c.sentinel_name for c in s.columns) for s in summaries}
    if len(schemas) > 1:
        issues.append(
            Issue(
                severity="warning",
                code="schema_union",
                message=(
                    "These files do not share the same columns. SentinelLoader will union the "
                    "schemas into one table; missing fields on a given row are omitted."
                ),
            )
        )

    state.columns = merged
    state.transform = TransformOptions(
        time_generated=time_cfg,
        add_source_file=True,
        table_name=table,
    )
    return ParseResult(
        files=summaries,
        merged_columns=merged,
        time_generated_candidates=[
            c.sentinel_name for c in merged if c.type == "datetime" or c.sentinel_name == time_col
        ],
        suggested_time_generated=time_cfg,
        suggested_table_name=table,
        issues=issues,
    )


@router.post("/upload")
async def upload_files(
    request: Request,
    files: list[UploadFile] = File(...),
    encoding: str | None = Query(None),
    delimiter: str | None = Query(None),
    has_header: bool | None = Query(None),
    kind: str | None = Query(None),
) -> ParseResult:
    state = _get_session(request)
    options = ParseOptions(
        encoding=encoding or None,
        delimiter=delimiter or None,
        has_header=has_header,
        kind=kind if kind in {"csv", "json", "jsonl", "raw"} else None,  # type: ignore[arg-type]
    )
    paths: list[tuple[str, Path]] = []
    for upload in files:
        data = await upload.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"{upload.filename} exceeds the 512 MB upload limit.")
        file_id = str(uuid.uuid4())
        filename = upload.filename or f"upload-{file_id}.csv"
        path = state.directory / f"{file_id}__{Path(filename).name}"
        path.write_bytes(data)
        paths.append((filename, path))
    return _parse_uploaded(state, paths, options)


@router.post("/demo")
def load_demo(request: Request) -> ParseResult:
    """Load bundled sample logs so the GUI can be exercised without picking files."""
    state = _get_session(request)
    state.files.clear()
    if not SAMPLES_DIR.exists():
        raise HTTPException(404, "Sample files are missing from this install.")
    paths: list[tuple[str, Path]] = []
    wanted = ["firewall_day1.csv", "firewall_day2.csv", "auth_events.jsonl"]
    for name in wanted:
        src = SAMPLES_DIR / name
        if not src.exists():
            continue
        file_id = str(uuid.uuid4())
        dest = state.directory / f"{file_id}__{name}"
        dest.write_bytes(src.read_bytes())
        paths.append((name, dest))
    if not paths:
        raise HTTPException(404, "No sample files were found.")
    return _parse_uploaded(state, paths, ParseOptions())


@router.get("/files")
def list_files(request: Request) -> dict[str, Any]:
    state = _get_session(request)
    return {
        "files": [
            {"file_id": f.file_id, "name": f.name, "size_bytes": f.path.stat().st_size}
            for f in state.files.values()
        ]
    }


@router.delete("/files/{file_id}")
def delete_file(request: Request, file_id: str) -> dict[str, str]:
    state = _get_session(request)
    stored = state.files.pop(file_id, None)
    if stored and stored.path.exists():
        stored.path.unlink()
    return {"deleted": file_id}


@router.post("/preview")
def preview(request: Request, payload: MappingPayload) -> PreviewResult:
    state = _get_session(request)
    if not state.files:
        raise HTTPException(400, "Upload at least one file first.")
    state.columns = payload.columns
    state.transform = TransformOptions(
        time_generated=payload.time_generated,
        add_source_file=payload.add_source_file,
        add_source_row=payload.add_source_row,
        table_name=payload.table_name,
    )
    table = ensure_cl_suffix(payload.table_name)
    sample_records: list[dict[str, Any]] = []
    row_count = 0
    now = datetime.now(timezone.utc)
    for stored in state.files.values():
        if stored.summary is None:
            continue
        row_count += stored.summary.row_count
        mtime = datetime.fromtimestamp(stored.path.stat().st_mtime, tz=timezone.utc)
        for i, row in enumerate(stored.summary.sample_rows):
            if len(sample_records) >= PREVIEW_RECORDS:
                break
            sample_records.append(
                to_sentinel_record(
                    row,
                    payload.columns,
                    payload.time_generated,
                    source_file=stored.name if payload.add_source_file else None,
                    source_row=i + 1 if payload.add_source_row else None,
                    ingest_time=now,
                    file_mtime=mtime,
                )
            )

    issues = list(mapping_issues(payload.columns))
    issues.extend(preview_issues(payload.columns, payload.time_generated, row_count))
    schemas = {tuple(c.sentinel_name for c in stored.summary.columns) for stored in state.files.values() if stored.summary}
    if len(schemas) > 1:
        issues.append(
            Issue(
                severity="warning",
                code="schema_union",
                message=(
                    "These files do not share the same columns. SentinelLoader will union the "
                    "schemas into one table; missing fields on a given row are omitted."
                ),
            )
        )
    for stored in state.files.values():
        if stored.summary:
            issues.extend(stored.summary.issues)
    schema = build_table_schema(table, payload.columns, state.transform)
    stream_decl = build_dcr_stream_declaration(table, payload.columns, state.transform)
    return PreviewResult(
        table_name=table,
        stream_name=stream_name_for(table),
        output_stream=f"Custom-{table}",
        columns=payload.columns,
        time_generated=payload.time_generated,
        sample_records=sample_records,
        api_payload_example=sample_records[:3],
        kql_example=kql_example(table),
        table_schema=schema,
        dcr_stream_declaration=stream_decl,
        issues=issues,
        row_count=row_count,
        file_count=len(state.files),
        can_create_table=True,
        table_callout=table_callout("create", table),
    )


@router.post("/ingest")
def ingest(request: Request, payload: IngestPayload) -> dict[str, Any]:
    state = _get_session(request)
    if not state.files:
        raise HTTPException(400, "Upload at least one file first.")
    mapping = payload.mapping
    dest_cfg = payload.destination
    transform = TransformOptions(
        time_generated=mapping.time_generated,
        add_source_file=mapping.add_source_file,
        add_source_row=mapping.add_source_row,
        table_name=mapping.table_name,
    )
    table = ensure_cl_suffix(mapping.table_name)
    issues = preview_issues(mapping.columns, mapping.time_generated, 1)
    if any(i.severity == "error" for i in issues):
        raise HTTPException(400, issues[0].message)

    schema = build_table_schema(table, mapping.columns, transform)
    stream_decl = build_dcr_stream_declaration(table, mapping.columns, transform)
    destination: ExistingDestination | None = dest_cfg.existing
    provision_info: dict[str, Any] | None = None
    messages: list[str] = []
    warnings: list[str] = []

    if dest_cfg.dry_run:
        messages.append("Dry run — nothing was written to Microsoft Sentinel.")
    elif dest_cfg.mode == "create":
        if state.connection is None:
            raise HTTPException(
                400,
                "Azure connection is required to create a table. Use dry run to preview without Azure, "
                "or save connection details first.",
            )
        try:
            client = SentinelClient(state.connection, build_credential(state.connection))
            provision = client.provision(
                table_name=table,
                schema_payload=schema,
                stream_declaration=stream_decl,
                plan=dest_cfg.table_plan,
                dcr_name=dest_cfg.dcr_name,
            )
            client.close()
        except AzureError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(400, f"Provisioning failed: {exc}") from exc
        destination = ExistingDestination(
            table_name=provision.table_name,
            dcr_immutable_id=provision.dcr_immutable_id,
            ingestion_endpoint=provision.ingestion_endpoint,
            stream_name=provision.stream_name,
        )
        provision_info = provision.model_dump()
        messages.extend(provision.messages)
        warnings.extend(provision.warnings)
        if not provision.assigned_role:
            warnings.append(
                "Ingest may fail until Monitoring Metrics Publisher is granted on the DCR."
            )
    else:
        if dest_cfg.existing is None:
            raise HTTPException(
                400,
                "Existing table mode needs a DCR immutable ID, ingestion endpoint, and stream name. "
                "The table must already exist — the API will not create it during upload.",
            )
        destination = dest_cfg.existing
        messages.append(
            f"Using existing table {destination.table_name} and DCR {destination.dcr_immutable_id}."
        )

    file_results = []
    total_sent = 0
    dry_run_path = None
    dry_handle = None
    if dest_cfg.dry_run:
        dry_run_path = str(state.directory / f"{table}-dry-run.ndjson")
        dry_handle = open(dry_run_path, "w", encoding="utf-8")

    ingest_client: SentinelClient | None = None
    if not dest_cfg.dry_run:
        if state.connection is None or destination is None:
            raise HTTPException(400, "Azure connection and destination are required for ingest.")
        ingest_client = SentinelClient(state.connection, build_credential(state.connection))

    try:
        for stored in state.files.values():
            if stored.summary is None:
                continue
            records = list(
                iter_sentinel_records(stored.path, stored.summary, mapping.columns, transform)
            )
            status = "ok"
            error = None
            sent = 0
            try:
                if dest_cfg.dry_run:
                    for record in records:
                        dry_handle.write(json.dumps(record, default=str) + "\n")
                    sent = len(records)
                    status = "dry_run"
                else:
                    assert ingest_client is not None and destination is not None
                    sent = ingest_client.ingest(destination, records)
            except AzureError as exc:
                status = "error"
                error = str(exc)
            except Exception as exc:  # pragma: no cover - defensive
                status = "error"
                error = str(exc)
            total_sent += sent if status != "error" else 0
            file_results.append(
                {
                    "file_name": stored.name,
                    "rows_sent": sent,
                    "rows_total": stored.summary.row_count,
                    "status": status,
                    "error": error,
                }
            )
    finally:
        if dry_handle:
            dry_handle.close()
        if ingest_client:
            ingest_client.close()

    return {
        "dry_run": dest_cfg.dry_run,
        "table_name": table,
        "rows_sent": total_sent,
        "files": file_results,
        "kql_example": kql_example(table),
        "messages": messages,
        "warnings": warnings,
        "provision": provision_info,
        "destination": destination.model_dump() if destination else None,
        "dry_run_path": dry_run_path,
        "table_callout": table_callout(dest_cfg.mode, table),
        "query_note": (
            "Rows typically appear in Sentinel within a few minutes. "
            f"Run this in Logs: {table} | where TimeGenerated > ago(1d)"
        ),
    }
