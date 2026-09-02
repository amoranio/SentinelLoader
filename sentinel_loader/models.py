from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


ColumnType = Literal["string", "int", "long", "real", "boolean", "datetime", "dynamic"]
TimeGeneratedMode = Literal["column", "ingest_time", "file_mtime"]
AuthMode = Literal["service_principal", "default"]
DestinationMode = Literal["create", "existing"]
FileKind = Literal["csv", "json", "jsonl", "raw"]


class Issue(BaseModel):
    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    file_name: str | None = None
    column: str | None = None


class ColumnMapping(BaseModel):
    original_name: str
    sentinel_name: str
    type: ColumnType = "string"
    include: bool = True
    sample_values: list[str] = Field(default_factory=list)
    renamed: bool = False
    rename_reason: str | None = None


class ParseOptions(BaseModel):
    encoding: str | None = None
    delimiter: str | None = None
    has_header: bool | None = None
    kind: FileKind | None = None


class TimeGeneratedConfig(BaseModel):
    mode: TimeGeneratedMode = "ingest_time"
    column: str | None = None


class TransformOptions(BaseModel):
    time_generated: TimeGeneratedConfig = Field(default_factory=TimeGeneratedConfig)
    add_source_file: bool = True
    add_source_row: bool = False
    table_name: str = "ImportedLogs"


class ConnectionConfig(BaseModel):
    auth_mode: AuthMode = "service_principal"
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    subscription_id: str = ""
    resource_group: str = ""
    workspace_name: str = ""
    cloud: str = "public"


class ExistingDestination(BaseModel):
    table_name: str
    dcr_immutable_id: str
    ingestion_endpoint: str
    stream_name: str


class DestinationConfig(BaseModel):
    mode: DestinationMode = "create"
    table_name: str = "ImportedLogs"
    table_plan: Literal["Analytics", "Basic", "Auxiliary"] = "Analytics"
    dcr_name: str | None = None
    existing: ExistingDestination | None = None
    dry_run: bool = False


class FileSummary(BaseModel):
    file_id: str
    name: str
    size_bytes: int
    kind: FileKind
    encoding: str
    delimiter: str | None = None
    has_header: bool = True
    row_count: int
    columns: list[ColumnMapping]
    sample_rows: list[dict[str, Any]]
    issues: list[Issue] = Field(default_factory=list)


class PreviewRecord(BaseModel):
    """One row as it will be posted to the Logs Ingestion API."""

    values: dict[str, Any]


class ParseResult(BaseModel):
    files: list[FileSummary]
    merged_columns: list[ColumnMapping]
    time_generated_candidates: list[str]
    suggested_time_generated: TimeGeneratedConfig
    suggested_table_name: str
    issues: list[Issue] = Field(default_factory=list)


class PreviewResult(BaseModel):
    table_name: str
    stream_name: str
    output_stream: str
    columns: list[ColumnMapping]
    time_generated: TimeGeneratedConfig
    sample_records: list[dict[str, Any]]
    api_payload_example: list[dict[str, Any]]
    kql_example: str
    table_schema: dict[str, Any]
    dcr_stream_declaration: dict[str, Any]
    issues: list[Issue]
    row_count: int
    file_count: int
    can_create_table: bool = True
    table_callout: str


class ProvisionResult(BaseModel):
    created_table: bool
    created_dcr: bool
    assigned_role: bool
    table_name: str
    dcr_name: str
    dcr_immutable_id: str
    ingestion_endpoint: str
    stream_name: str
    messages: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class IngestFileProgress(BaseModel):
    file_name: str
    rows_sent: int
    rows_total: int
    status: str
    error: str | None = None


class IngestResult(BaseModel):
    dry_run: bool
    table_name: str
    rows_sent: int
    files: list[IngestFileProgress]
    kql_example: str
    messages: list[str] = Field(default_factory=list)
    dry_run_path: str | None = None
