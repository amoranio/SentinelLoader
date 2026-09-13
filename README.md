# SentinelLoader

Bulk-parse CSV, TSV, JSON, and JSONL log files, **validate the parsed shape in a GUI**, and ingest the records into **Microsoft Sentinel** (Log Analytics) using the [Logs Ingestion API](https://learn.microsoft.com/en-us/azure/azure-monitor/logs/logs-ingestion-api-overview).

Microsoft Sentinel has no “upload CSV” button. This app is that button: detect schema, show what the table will look like, create the custom table if you want, then post the rows.

## Do I have to create the table first?

**The table must exist before any row can be ingested.** The Logs Ingestion API will not invent a table at upload time.

**SentinelLoader can create it for you.** On Connect choose **App creates the table**, then on Send keep **Create the custom table and DCR**. The app will:

1. Create a custom Log Analytics table named `{YourName}_CL` with the schema you approved in the preview
2. Create a **Direct** Data Collection Rule (DCR) whose stream matches that schema
3. Grant this identity **Monitoring Metrics Publisher** on the DCR (needed to POST logs)
4. Upload the parsed records in 1 MB chunks (gzip)

### Create the table yourself (then only paste details in the app)

Use this when you want Azure to own the table, or the app identity cannot create resources.

1. In SentinelLoader, choose **Preview only**, parse your files, and on Validate click **Download sample JSON for Azure portal**.
2. Azure portal → Log Analytics workspace (the one behind Sentinel) → **Tables** → **Create** → **New custom log (DCR-based)**. Name it; the portal adds `_CL`.
3. Create a DCR in the same region. If asked, attach a Data Collection Endpoint in that region.
4. Upload the sample JSON. Map or add `TimeGenerated`.
5. On the DCR: **Access control (IAM)** → **Monitoring Metrics Publisher** → assign your Entra app registration.
6. Copy from the DCR: **Immutable Id**, JSON `properties.logsIngestion.endpoint`, and the `streamDeclarations` key (usually `Custom-YourTable`).
7. In the app: Connect with tenant / client / secret / subscription / RG / workspace. On Send, choose **Use a table I already created** and paste those three DCR values.

The in-app **Setup guide** repeats this with portal click-paths.

### Permissions

| Goal | Azure roles |
| --- | --- |
| Let the app create the table + DCR | **Log Analytics Contributor** on the workspace, **Monitoring Contributor** on the resource group, **User Access Administrator** on the RG/DCR (so it can grant itself ingest permission) |
| Ingest only into an existing table | **Monitoring Metrics Publisher** on the DCR |

A service principal (app registration + client secret) is the usual choice. Azure CLI (`az login`) works via DefaultAzureCredential if you run the app locally.

## Run it

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m sentinel_loader --host 127.0.0.1 --port 8080
```

Open [http://127.0.0.1:8080](http://127.0.0.1:8080).

1. **Connect** — save tenant / subscription / workspace (or skip Azure and use dry run)
2. **Files** — drop one or many CSV/JSON/JSONL files (or click **Load sample logs**)
3. **Validate** — inspect inferred types, map `TimeGenerated`, and view sample rows as they will appear in Sentinel (table, API JSON, KQL, and table schema)
4. **Send** — create the table or point at an existing one, or tick **Dry run** to write NDJSON locally without calling Azure

After a live ingest, wait a few minutes and query:

```kusto
YourTable_CL
| where TimeGenerated > ago(1d)
| take 50
```

## What gets parsed

| Format | Notes |
| --- | --- |
| CSV / TSV | Encoding, delimiter (`,`, `;`, tab, `\|`), and header row are detected. `sep=;` Excel prefixes are honored |
| JSON | Array of objects, or `{ "records" \| "value" \| "logs": [ ... ] }` |
| JSONL / NDJSON | One object per line |
| Other text | Each line becomes `RawData` + `LineNumber` |

Column names are sanitized to Azure Monitor rules (letter first, `[A-Za-z0-9_]`, max 45). Reserved names such as `id` and `TenantId` are renamed. `TimeGenerated` is required on every table; if your file has no timestamp, the app can use upload time (a warning is shown because historical logs will then all share “now”).

## Repository layout

```
sentinel_loader/     Python app (parser, Azure ARM + ingest, FastAPI GUI)
samples/             Example firewall CSV and auth JSONL
tests/               Parser and API tests (including dry-run ingest)
```

```bash
pytest
```
