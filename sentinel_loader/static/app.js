const state = {
  step: 1,
  files: [],
  parse: null,
  preview: null,
  workspace: null,
  azureReady: false,
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

function showStatus(el, message, kind) {
  el.textContent = message || "";
  el.classList.remove("error", "ok");
  if (kind) el.classList.add(kind);
}

function go(step) {
  state.step = step;
  $$(".panel").forEach((p) => p.classList.add("hidden"));
  $(`#step-${step}`).classList.remove("hidden");
  $$(".steps li").forEach((li) => {
    const n = Number(li.dataset.step);
    li.classList.toggle("active", n === step);
    li.classList.toggle("done", n < step);
  });
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch {
    data = { detail: text };
  }
  if (!res.ok) {
    const detail = data?.detail;
    const msg = typeof detail === "string" ? detail : (detail?.[0]?.msg || JSON.stringify(detail) || res.statusText);
    throw new Error(msg);
  }
  return data;
}

function formConnection() {
  const form = $("#connectForm");
  const data = Object.fromEntries(new FormData(form).entries());
  return data;
}

function mappingPayload() {
  const columns = $$("#schemaTable tbody tr").map((tr) => {
    let samples = [];
    try { samples = JSON.parse(tr.dataset.samples || "[]"); } catch { samples = []; }
    return {
      original_name: tr.dataset.original,
      sentinel_name: tr.querySelector(".sentinel-name").value.trim(),
      type: tr.querySelector(".col-type").value,
      include: tr.querySelector(".include").checked,
      sample_values: samples,
      renamed: tr.dataset.renamed === "true",
      rename_reason: tr.dataset.reason || null,
    };
  });
  return {
    columns,
    time_generated: {
      mode: $("#timeMode").value,
      column: $("#timeColumn").value || null,
    },
    add_source_file: $("#addSourceFile").checked,
    add_source_row: false,
    table_name: $("#tableName").value.trim() || "ImportedLogs",
  };
}

function renderFiles() {
  const ul = $("#fileList");
  ul.innerHTML = state.files.map((f, i) => {
    const extra = state.parse?.files?.[i];
    const meta = extra
      ? `${extra.kind} · ${extra.row_count} rows · ${extra.encoding}${extra.delimiter ? ` · delim ${JSON.stringify(extra.delimiter)}` : ""}`
      : `${(f.size / 1024).toFixed(1)} KB`;
    return `<li><div><strong>${f.name}</strong><div class="meta">${meta}</div></div></li>`;
  }).join("");
}

function renderIssues(issues) {
  const box = $("#issueList");
  if (!issues?.length) {
    box.innerHTML = `<div class="issue info">No blocking issues. Review the sample rows on the right — that is the shape Sentinel will store.</div>`;
    return;
  }
  box.innerHTML = issues.map((i) => `<div class="issue ${i.severity}"><strong>${i.severity}</strong> — ${i.message}</div>`).join("");
}

function renderSchema(parse) {
  const tbody = $("#schemaTable tbody");
  tbody.innerHTML = parse.merged_columns.map((col) => {
    const samples = (col.sample_values || []).slice(0, 3).join(" · ");
    return `<tr data-original="${encodeAttr(col.original_name)}" data-samples='${encodeAttr(JSON.stringify(col.sample_values || []))}' data-renamed="${col.renamed}" data-reason="${encodeAttr(col.rename_reason || "")}">
      <td><input type="checkbox" class="include" ${col.include ? "checked" : ""} /></td>
      <td>${escapeHtml(col.original_name)}</td>
      <td><input class="sentinel-name" value="${encodeAttr(col.sentinel_name)}" /></td>
      <td>
        <select class="col-type">
          ${["string","int","long","real","boolean","datetime","dynamic"].map((t) => `<option ${t === col.type ? "selected" : ""}>${t}</option>`).join("")}
        </select>
      </td>
      <td class="samples">${escapeHtml(samples)}</td>
    </tr>`;
  }).join("");
  $("#tableName").value = parse.suggested_table_name;
  const timeSel = $("#timeColumn");
  timeSel.innerHTML = parse.merged_columns.map((c) => `<option value="${encodeAttr(c.sentinel_name)}">${escapeHtml(c.sentinel_name)}</option>`).join("");
  const suggested = parse.suggested_time_generated;
  $("#timeMode").value = suggested.mode;
  if (suggested.column) timeSel.value = suggested.column;
  toggleTimeColumn();
}

function toggleTimeColumn() {
  $("#timeColumnWrap").classList.toggle("hidden", $("#timeMode").value !== "column");
}

function renderPreview(preview) {
  state.preview = preview;
  renderIssues(preview.issues);
  const records = preview.sample_records || [];
  const keys = [...new Set(records.flatMap((r) => Object.keys(r)))];
  if (!records.length) {
    $("#previewTable").innerHTML = "<p class='status'>No sample rows.</p>";
  } else {
    $("#previewTable").innerHTML = `<div class="table-wrap"><table class="preview-table">
      <thead><tr>${keys.map((k) => `<th>${escapeHtml(k)}</th>`).join("")}</tr></thead>
      <tbody>${records.map((r) => `<tr>${keys.map((k) => `<td>${escapeHtml(formatCell(r[k]))}</td>`).join("")}</tr>`).join("")}</tbody>
    </table></div>`;
  }
  $("#previewJson").textContent = JSON.stringify(preview.api_payload_example, null, 2);
  $("#previewKql").textContent = `${preview.kql_example}\n\n-- Custom tables always end in _CL.\n-- Data can take a few minutes to become queryable after ingest.`;
  $("#previewSchema").textContent = JSON.stringify({
    table: preview.table_name,
    stream: preview.stream_name,
    outputStream: preview.output_stream,
    table_schema: preview.table_schema,
    dcr_stream: preview.dcr_stream_declaration,
  }, null, 2);
  $("#tableCallout").innerHTML = `<strong>Table requirement</strong><p>${preview.table_callout}</p>`;
  $("#createTableName").textContent = preview.table_name;
}

function formatCell(value) {
  if (value == null) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function encodeAttr(value) {
  return String(value ?? "").replaceAll('"', "&quot;");
}

function showPreviewTab(name) {
  $$("#previewTabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $("#previewTable").classList.toggle("hidden", name !== "table");
  $("#previewJson").classList.toggle("hidden", name !== "json");
  $("#previewKql").classList.toggle("hidden", name !== "kql");
  $("#previewSchema").classList.toggle("hidden", name !== "schema");
}

async function parseFiles() {
  if (!state.files.length) {
    showStatus($("#uploadStatus"), "Add at least one file.", "error");
    return;
  }
  showStatus($("#uploadStatus"), "Parsing…");
  const form = new FormData();
  state.files.forEach((f) => form.append("files", f));
  const delim = $("#optDelimiter").value.trim();
  const enc = $("#optEncoding").value.trim();
  const header = $("#optHeader").value;
  const qs = new URLSearchParams();
  if (delim) qs.set("delimiter", delim);
  if (enc) qs.set("encoding", enc);
  if (header) qs.set("has_header", header);
  const parse = await api(`/api/upload${qs.toString() ? `?${qs}` : ""}`, { method: "POST", body: form });
  state.parse = parse;
  renderFiles();
  renderSchema(parse);
  go(3);
  await refreshPreview();
}

async function refreshPreview() {
  showStatus($("#previewStatus"), "Building Sentinel-shaped preview…");
  const preview = await api("/api/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(mappingPayload()),
  });
  renderPreview(preview);
  showStatus($("#previewStatus"), `${preview.row_count} rows across ${preview.file_count} file(s) → table ${preview.table_name}`, "ok");
}

async function ingest() {
  const dry = $("#dryRun").checked;
  const destMode = document.querySelector("input[name=destMode]:checked").value;
  if (!dry && !state.azureReady && destMode === "create") {
    showStatus($("#ingestStatus"), "Save an Azure connection, or enable dry run.", "error");
    return;
  }
  $("#ingestBtn").disabled = true;
  showStatus($("#ingestStatus"), dry ? "Running dry run…" : "Provisioning / ingesting… this can take a minute if the table is being created.");
  try {
    const mapping = mappingPayload();
    const destination = {
      mode: destMode,
      table_name: mapping.table_name,
      table_plan: "Analytics",
      dry_run: dry,
      existing: destMode === "existing" ? {
        table_name: $("#existingTable").value.trim(),
        dcr_immutable_id: $("#existingDcr").value.trim(),
        ingestion_endpoint: $("#existingEndpoint").value.trim(),
        stream_name: $("#existingStream").value.trim(),
      } : null,
    };
    const result = await api("/api/ingest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mapping, destination }),
    });
    const files = (result.files || []).map((f) => `${f.file_name}: ${f.status} (${f.rows_sent}/${f.rows_total}${f.error ? ` — ${f.error}` : ""})`).join("\n");
    const msgs = [...(result.messages || []), ...(result.warnings || [])].join("\n");
    $("#ingestResult").classList.remove("hidden");
    $("#ingestResult").textContent = [
      result.dry_run ? "DRY RUN — Azure was not contacted." : "INGEST COMPLETE",
      `Table: ${result.table_name}`,
      `Rows sent: ${result.rows_sent}`,
      result.query_note,
      "",
      "Per file:",
      files,
      msgs ? `\n${msgs}` : "",
      result.dry_run_path ? `\nLocal NDJSON: ${result.dry_run_path}` : "",
      "\nQuery:\n" + result.kql_example,
    ].filter(Boolean).join("\n");
    showStatus($("#ingestStatus"), result.dry_run ? "Dry run finished." : "Upload finished.", "ok");
  } catch (err) {
    showStatus($("#ingestStatus"), err.message, "error");
  } finally {
    $("#ingestBtn").disabled = false;
  }
}

$("#testConnectBtn").addEventListener("click", async () => {
  const payload = formConnection();
  showStatus($("#connectStatus"), "Testing workspace access…");
  try {
    await api("/api/connection", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await api("/api/connection/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    state.azureReady = true;
    state.workspace = result;
    $("#workspaceMeta").classList.remove("hidden");
    const custom = (result.tables || []).filter((t) => t.is_custom).slice(0, 12).map((t) => t.name).join(", ") || "none yet";
    $("#workspaceMeta").textContent = `Workspace ${result.workspace.name} (${result.workspace.location})\nCustom tables: ${custom}\nDCRs: ${(result.dcrs || []).map((d) => d.name).join(", ") || "none"}\n${result.note}`;
    const pick = $("#workspacePick");
    pick.classList.remove("hidden");
    pick.textContent = $("#workspaceMeta").textContent;
    if (result.dcrs?.[0]) {
      $("#existingDcr").value = result.dcrs[0].immutable_id || "";
      $("#existingEndpoint").value = result.dcrs[0].ingestion_endpoint || "";
      $("#existingStream").value = result.dcrs[0].streams?.[0] || "";
    }
    showStatus($("#connectStatus"), "Connection works. Continue to files.", "ok");
    go(2);
  } catch (err) {
    state.azureReady = false;
    showStatus($("#connectStatus"), err.message, "error");
  }
});

$("#skipAzureBtn").addEventListener("click", () => {
  state.azureReady = false;
  $("#dryRun").checked = true;
  showStatus($("#connectStatus"), "Preview-only mode. You can still parse and dry-run ingest.", "ok");
  go(2);
});

$("#connectForm [name=auth_mode]").addEventListener("change", (e) => {
  $$(".sp-only").forEach((el) => el.classList.toggle("hidden", e.target.value !== "service_principal"));
});

const drop = $("#dropZone");
const fileInput = $("#fileInput");
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("drag"); });
drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
drop.addEventListener("drop", (e) => {
  e.preventDefault();
  drop.classList.remove("drag");
  state.files = [...e.dataTransfer.files];
  renderFiles();
});
fileInput.addEventListener("change", () => {
  state.files = [...fileInput.files];
  renderFiles();
});

$("#parseBtn").addEventListener("click", async () => {
  try { await parseFiles(); }
  catch (err) { showStatus($("#uploadStatus"), err.message, "error"); }
});

$("#refreshPreviewBtn").addEventListener("click", async () => {
  try { await refreshPreview(); }
  catch (err) { showStatus($("#previewStatus"), err.message, "error"); }
});
$("#toSendBtn").addEventListener("click", async () => {
  try {
    await refreshPreview();
    const errors = (state.preview?.issues || []).filter((i) => i.severity === "error");
    if (errors.length) {
      showStatus($("#previewStatus"), errors[0].message, "error");
      return;
    }
    go(4);
  } catch (err) {
    showStatus($("#previewStatus"), err.message, "error");
  }
});
$("#timeMode").addEventListener("change", toggleTimeColumn);
$("#previewTabs").addEventListener("click", (e) => {
  if (e.target.dataset.tab) showPreviewTab(e.target.dataset.tab);
});
document.querySelectorAll("input[name=destMode]").forEach((radio) => {
  radio.addEventListener("change", () => {
    const mode = document.querySelector("input[name=destMode]:checked").value;
    $("#existingFields").classList.toggle("hidden", mode !== "existing");
    const table = state.preview?.table_name || "ImportedLogs_CL";
    if (mode === "create") {
      $("#tableCallout").innerHTML = `<strong>The app can create the table</strong><p>A custom table must exist before ingest. SentinelLoader will create <code>${table}</code> plus a Direct DCR. You do not need to pre-create it in the portal if this identity has permission.</p>`;
    } else {
      $("#tableCallout").innerHTML = `<strong>Table must already exist</strong><p>The Logs Ingestion API will not create <code>${table}</code> during upload. Provide a DCR that already streams into that table.</p>`;
    }
  });
});
$("#ingestBtn").addEventListener("click", ingest);

$$("[data-back]").forEach((b) => b.addEventListener("click", () => go(Number(b.dataset.back))));
$$(".steps li").forEach((li) => li.addEventListener("click", () => go(Number(li.dataset.step))));

$("#resetBtn").addEventListener("click", async () => {
  await api("/api/session/reset", { method: "POST" });
  state.files = [];
  state.parse = null;
  state.preview = null;
  $("#fileList").innerHTML = "";
  $("#ingestResult").classList.add("hidden");
  go(1);
});

$("#demoBtn").addEventListener("click", async () => {
  try {
    const result = await api("/api/demo", { method: "POST" });
    state.parse = result;
    state.files = result.files.map((f) => ({ name: f.name, size: 1 }));
    renderFiles();
    renderSchema(result);
    go(3);
    await refreshPreview();
  } catch (err) {
    showStatus($("#previewStatus") || $("#connectStatus"), err.message, "error");
  }
});
