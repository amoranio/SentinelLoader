const state = {
  step: 1,
  files: [],
  parse: null,
  preview: null,
  workspace: null,
  azureReady: false,
  setupPath: "create",
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

function showStatus(el, message, kind) {
  if (!el) return;
  el.textContent = message || "";
  el.classList.remove("error", "ok");
  if (kind) el.classList.add(kind);
}

function go(step) {
  state.step = step;
  $$(".panel").forEach((p) => p.classList.add("hidden"));
  $(`#step-${step}`)?.classList.remove("hidden");
  $$("#steps button").forEach((btn) => {
    const n = Number(btn.dataset.step);
    btn.classList.toggle("active", n === step);
    btn.classList.toggle("done", n < step);
  });
}

function openGuide(hash) {
  $("#guideOverlay").classList.remove("hidden");
  document.body.style.overflow = "hidden";
  if (hash) {
    const target = document.querySelector(hash);
    if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

function closeGuide() {
  $("#guideOverlay").classList.add("hidden");
  document.body.style.overflow = "";
}

function setupPath() {
  return document.querySelector("input[name=setupPath]:checked")?.value || "create";
}

function applySetupPath() {
  const path = setupPath();
  state.setupPath = path;
  const note = $("#pathNote");
  const azureFields = $("#azureFields");
  const testBtn = $("#testConnectBtn");
  const skipBtn = $("#skipAzureBtn");
  if (path === "preview") {
    note.textContent = "Azure fields are optional. Parse files, validate the shape, and use dry run. Nothing is sent to Sentinel.";
    azureFields.classList.add("dimmed");
    testBtn.classList.add("hidden");
    skipBtn.textContent = "Continue to files";
    $("#dryRun").checked = true;
  } else if (path === "existing") {
    note.innerHTML = "Create the table in Azure first (setup guide, Path B). Then this screen only needs the Entra app + workspace. DCR immutable ID, endpoint, and stream are entered on Send.";
    azureFields.classList.remove("dimmed");
    testBtn.classList.remove("hidden");
    skipBtn.textContent = "Continue without Azure";
    $("#dryRun").checked = false;
  } else {
    note.textContent = "Paste workspace identity below. On Send, choose “Create the custom table and DCR” unless you have already provisioned them.";
    azureFields.classList.remove("dimmed");
    testBtn.classList.remove("hidden");
    skipBtn.textContent = "Continue without Azure";
    $("#dryRun").checked = false;
  }
}

function syncDestinationFromPath() {
  const path = state.setupPath;
  const create = document.querySelector("input[name=destMode][value=create]");
  const existing = document.querySelector("input[name=destMode][value=existing]");
  if (path === "existing" && existing) existing.checked = true;
  if (path === "create" && create) create.checked = true;
  updateDestMode();
}

function updateDestMode() {
  const mode = document.querySelector("input[name=destMode]:checked")?.value || "create";
  $("#existingPanel").classList.toggle("hidden", mode !== "existing");
  const table = state.preview?.table_name || "ImportedLogs_CL";
  if (mode === "create") {
    $("#tableCallout").innerHTML = `<strong>The app can create the table</strong><p>A custom table must exist before ingest. SentinelLoader will create <code>${table}</code> plus a Direct DCR. You do not need to pre-create it in the portal if this identity has permission. Required roles are listed in the setup guide (Path A).</p>`;
  } else {
    $("#tableCallout").innerHTML = `<strong>Table must already exist</strong><p>The Logs Ingestion API will not create <code>${table}</code> during upload. Create it in the portal (setup guide, Path B), grant Monitoring Metrics Publisher, then paste the three DCR values below. Connection credentials are still used to authenticate.</p>`;
    if (state.preview) {
      if (!$("#existingTable").value) $("#existingTable").value = state.preview.table_name || "";
      if (!$("#existingStream").value) $("#existingStream").value = state.preview.stream_name || "";
    }
  }
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
  return Object.fromEntries(new FormData(form).entries());
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
    return `<li><div><strong>${escapeHtml(f.name)}</strong><div class="meta">${escapeHtml(meta)}</div></div></li>`;
  }).join("");
}

function renderIssues(issues) {
  const box = $("#issueList");
  if (!issues?.length) {
    box.innerHTML = `<div class="issue info"><strong>Ready</strong> — no blocking issues. The panel on the right is the shape Sentinel will store.</div>`;
    return;
  }
  box.innerHTML = issues.map((i) => `<div class="issue ${i.severity}"><strong>${i.severity}</strong> — ${escapeHtml(i.message)}</div>`).join("");
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
  $("#previewKql").textContent = `${preview.kql_example}\n\n// Custom tables always end in _CL.\n// Rows can take a few minutes to become queryable after ingest.`;
  $("#previewSchema").textContent = JSON.stringify({
    table: preview.table_name,
    stream: preview.stream_name,
    outputStream: preview.output_stream,
    table_schema: preview.table_schema,
    dcr_stream: preview.dcr_stream_declaration,
  }, null, 2);
  $("#createTableName").textContent = preview.table_name;
  updateDestMode();
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

function downloadPortalSample() {
  const records = state.preview?.api_payload_example || state.preview?.sample_records || [];
  if (!records.length) {
    showStatus($("#previewStatus"), "Refresh the preview first so there are sample records to export.", "error");
    return;
  }
  const name = `${state.preview?.table_name || "sample"}-portal.json`;
  const blob = new Blob([JSON.stringify(records, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
  showStatus($("#previewStatus"), `Saved ${name}. Upload this file in Azure: Tables → New custom log (DCR-based) → sample data.`, "ok");
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
  showStatus($("#previewStatus"), `${preview.row_count} rows across ${preview.file_count} file(s) → ${preview.table_name}`, "ok");
}

async function ingest() {
  const dry = $("#dryRun").checked;
  const destMode = document.querySelector("input[name=destMode]:checked").value;
  if (!dry && !state.azureReady) {
    showStatus($("#ingestStatus"), "Save an Azure connection, or enable dry run.", "error");
    return;
  }
  if (!dry && destMode === "existing") {
    const missing = ["existingTable", "existingDcr", "existingEndpoint", "existingStream"].filter((id) => !$(`#${id}`).value.trim());
    if (missing.length) {
      showStatus($("#ingestStatus"), "Table name, DCR immutable ID, ingestion endpoint, and stream name are all required for an existing table.", "error");
      return;
    }
  }
  $("#ingestBtn").disabled = true;
  showStatus($("#ingestStatus"), dry ? "Running dry run…" : "Provisioning / ingesting… table creation can take a minute.");
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
  showStatus($("#connectStatus"), "Preview-only. Parse files and dry-run ingest without calling Azure.", "ok");
  go(2);
});

$("#connectForm [name=auth_mode]").addEventListener("change", (e) => {
  $$(".sp-only").forEach((el) => el.classList.toggle("hidden", e.target.value !== "service_principal"));
});

$$("input[name=setupPath]").forEach((r) => r.addEventListener("change", applySetupPath));

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
$("#downloadSampleBtn").addEventListener("click", () => {
  try { downloadPortalSample(); }
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
    syncDestinationFromPath();
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
  radio.addEventListener("change", updateDestMode);
});
$("#ingestBtn").addEventListener("click", ingest);

$$("[data-back]").forEach((b) => b.addEventListener("click", () => go(Number(b.dataset.back))));
$$("#steps button").forEach((btn) => btn.addEventListener("click", () => go(Number(btn.dataset.step))));

$("#guideBtn").addEventListener("click", () => openGuide());
$("#guideBtn2").addEventListener("click", () => openGuide());
$("#closeGuide").addEventListener("click", closeGuide);
$("#guideOverlay").addEventListener("click", (e) => {
  if (e.target === $("#guideOverlay")) closeGuide();
});
$("#openPortalGuide").addEventListener("click", () => openGuide("#guide-portal"));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#guideOverlay").classList.contains("hidden")) closeGuide();
});

$("#resetBtn").addEventListener("click", async () => {
  await api("/api/session/reset", { method: "POST" });
  state.files = [];
  state.parse = null;
  state.preview = null;
  state.azureReady = false;
  $("#fileList").innerHTML = "";
  $("#ingestResult").classList.add("hidden");
  $("#workspaceMeta").classList.add("hidden");
  go(1);
  applySetupPath();
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

applySetupPath();
updateDestMode();
