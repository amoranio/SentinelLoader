from pathlib import Path

from sentinel_loader.models import ParseOptions
from sentinel_loader.parser import parse_file
from sentinel_loader.schema import infer_column_type, sanitize_column_name
from sentinel_loader.transform import to_sentinel_record
from sentinel_loader.models import TimeGeneratedConfig

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def test_sanitize_reserved_and_spaces():
    assert sanitize_column_name("id")[0] == "id_col"
    name, reason = sanitize_column_name("Time Generated")
    assert name == "Time_Generated"
    assert reason
    assert sanitize_column_name("123abc")[0].startswith("C_")
    assert sanitize_column_name("src-ip")[0] == "src_ip"


def test_infer_types():
    assert infer_column_type(["1", "2", "3"]) == "int"
    assert infer_column_type(["true", "false"]) == "boolean"
    assert infer_column_type(["1.5", "2.0"]) == "real"
    assert infer_column_type(["2026-08-12T14:22:01Z", "2026-08-13T02:11:09Z"]) == "datetime"
    assert infer_column_type(["hello", "1"]) == "string"


def test_parse_firewall_csv():
    summary = parse_file(SAMPLES / "firewall_day1.csv", "firewall_day1.csv")
    assert summary.kind == "csv"
    assert summary.row_count == 10
    names = [c.sentinel_name for c in summary.columns]
    assert "timestamp" in names
    assert "src_ip" in names
    ts = next(c for c in summary.columns if c.sentinel_name == "timestamp")
    assert ts.type == "datetime"
    bytes_col = next(c for c in summary.columns if c.sentinel_name == "bytes")
    assert bytes_col.type == "int"


def test_parse_semicolon_csv():
    summary = parse_file(SAMPLES / "european_semicolon.csv", "european_semicolon.csv")
    assert summary.delimiter == ";"
    assert summary.row_count == 3
    assert any(c.sentinel_name == "event_time" for c in summary.columns)


def test_parse_jsonl():
    summary = parse_file(SAMPLES / "auth_events.jsonl", "auth_events.jsonl")
    assert summary.kind == "jsonl"
    assert summary.row_count == 6
    mfa = next(c for c in summary.columns if c.sentinel_name == "MFA")
    assert mfa.type == "boolean"


def test_messy_headers_and_transform(tmp_path):
    summary = parse_file(SAMPLES / "messy_headers.csv", "messy_headers.csv")
    names = {c.original_name: c.sentinel_name for c in summary.columns}
    assert names["id"] == "id_col"
    assert names["Time Generated"] == "Time_Generated"
    assert names["src-ip"] == "src_ip"
    record = to_sentinel_record(
        summary.sample_rows[0],
        summary.columns,
        TimeGeneratedConfig(mode="column", column="Time_Generated"),
        source_file="messy_headers.csv",
    )
    assert record["TimeGenerated"].startswith("2026-08-01")
    assert record["SourceFile"] == "messy_headers.csv"
    assert "id_col" in record


def test_parse_options_no_header(tmp_path):
    path = tmp_path / "raw.csv"
    path.write_text("alpha,10\nbeta,20\n", encoding="utf-8")
    summary = parse_file(path, "raw.csv", ParseOptions(has_header=False, delimiter=","))
    assert summary.columns[0].sentinel_name.startswith("Column")
    assert summary.row_count == 2
