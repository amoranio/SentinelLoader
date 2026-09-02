from fastapi.testclient import TestClient

from sentinel_loader.app import app


def test_health():
    client = TestClient(app)
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["service"] == "SentinelLoader"


def test_demo_preview_and_dry_run():
    client = TestClient(app)
    demo = client.post("/api/demo")
    assert demo.status_code == 200, demo.text
    parsed = demo.json()
    assert parsed["files"]
    assert parsed["merged_columns"]
    assert any(i["code"] == "schema_union" for i in parsed["issues"])

    mapping = {
        "columns": parsed["merged_columns"],
        "time_generated": parsed["suggested_time_generated"],
        "add_source_file": True,
        "add_source_row": False,
        "table_name": parsed["suggested_table_name"],
    }
    preview = client.post("/api/preview", json=mapping)
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["table_name"].endswith("_CL")
    assert body["sample_records"]
    assert "TimeGenerated" in body["sample_records"][0]
    assert "can_create_table" in body
    assert "custom table must exist" in body["table_callout"].lower() or "Logs Ingestion API" in body["table_callout"]

    ingest = client.post(
        "/api/ingest",
        json={
            "mapping": mapping,
            "destination": {
                "mode": "create",
                "table_name": mapping["table_name"],
                "dry_run": True,
            },
        },
    )
    assert ingest.status_code == 200, ingest.text
    result = ingest.json()
    assert result["dry_run"] is True
    assert result["rows_sent"] > 0
    assert result["dry_run_path"]
    assert all(f["status"] == "dry_run" for f in result["files"])


def test_upload_single_csv():
    client = TestClient(app)
    csv_bytes = b"timestamp,host,message\n2026-01-01T00:00:00Z,box-1,hello\n2026-01-01T00:00:01Z,box-1,world\n"
    res = client.post(
        "/api/upload",
        files=[("files", ("events.csv", csv_bytes, "text/csv"))],
    )
    assert res.status_code == 200, res.text
    parsed = res.json()
    assert parsed["files"][0]["row_count"] == 2
    assert parsed["suggested_time_generated"]["mode"] == "column"
    assert parsed["suggested_time_generated"]["column"] == "timestamp"
