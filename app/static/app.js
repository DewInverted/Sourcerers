/* Sourcerers front-end — vanilla JS, no build step. */
"use strict";

const state = {
  view: "find",
  settings: {},
  providers: [],
  caps: {},
  stats: {},
  current: null,
  queue: [],
  processing: false,
  filter: "",
  query: "",
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/* ------------------------------------------------------------------ utils */
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function toast(message, kind = "") {
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  node.textContent = message;
  $("#toasts").appendChild(node);
  setTimeout(() => {
    node.style.transition = "opacity .25s, transform .25s";
    node.style.opacity = "0";
    node.style.transform = "translateX(12px)";
    setTimeout(() => node.remove(), 260);
  }, 4200);
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!res.ok) throw new Error((data && (data.detail || data.error)) || `request failed (${res.status})`);
  return data;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function humanDuration(seconds) {
  const s = Math.round(seconds || 0);
  if (!s) return "";
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return url; }
}

function thumbUrl(path) {
  if (!path) return "";
  if (path.startsWith("http") || path.startsWith("/")) return path;
  return `/clips/${path}`;
}

function verdictBadge(clip) {
  const map = {
    duplicate: ["bad", "repeat"],
    near: ["warn", "similar"],
    distinct: ["good", "new footage"],
  };
  const hit = map[clip.verdict];
  if (!hit) return "";
  return `<span class="badge ${hit[0]}">${hit[1]}</span>`;
}

/* ------------------------------------------------------------------- shell */
const VIEW_META = {
  find: ["Find sources", "Drop a clip or screenshot — I'll find where the original lives."],
  library: ["Library", "Everything you've sourced, with how many times you've used it."],
  duplicates: ["Repeats", "The same footage showing up more than once."],
  scan: ["Scan folder", "Fingerprint a whole folder and catch repeats before editing."],
  settings: ["Settings", "Tune sensitivity, engines and preferences."],
};

function setView(view) {
  state.view = view;
  $$(".nav-item").forEach((b) => b.classList.toggle("is-active", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("is-active", v.id === `view-${view}`));
  const [title, sub] = VIEW_META[view] || ["", ""];
  $("#view-title").textContent = title;
  $("#view-sub").textContent = sub;
  $("#topbar-actions").innerHTML = "";
  if (view === "library") loadLibrary();
  if (view === "duplicates") loadDuplicates();
  if (view === "settings") fillSettings();
  if (view === "scan") {
    const folder = localStorage.getItem("sourcerers:folder");
    if (folder && !$("#scan-folder").value) $("#scan-folder").value = folder;
  }
  if (view === "find") refreshStats();
}

/* ------------------------------------------------------------------ search */
function engineChips() {
  const box = $("#engine-picker");
  const active = new Set(state.settings.providers || ["lens", "yandex"]);
  box.innerHTML = state.providers
    .filter((p) => p.id !== "demo" && (p.free || p.available))
    .map((p) => `<button class="chip ${active.has(p.id) ? "is-active" : ""}" data-engine="${p.id}">${esc(p.label)}</button>`)
    .join("");
}

function activeEngines() {
  return $$("#engine-picker .chip.is-active").map((b) => b.dataset.engine);
}

/* -------------------------------------------------------------- job polling */
async function pollJob(jobId, onTick) {
  for (let attempt = 0; attempt < 900; attempt++) {
    const job = await api(`/api/jobs/${jobId}`);
    if (onTick) onTick(job);
    if (job.status === "done") return job.result;
    if (job.status === "error") throw new Error(job.error || "job failed");
    await sleep(job.stage === "download" ? 900 : 600);
  }
  throw new Error("gave up waiting for the job");
}

const ANALYZE_STEPS = [
  ["upload", "reading file"],
  ["frames", "sampling frames"],
  ["fingerprint", "fingerprinting"],
  ["dedup", "checking library"],
];

function renderPipeline(steps, activeStage, progress) {
  $("#pipeline").innerHTML = steps
    .map(([key, label], index) => {
      const activeIndex = steps.findIndex(([k]) => k === activeStage);
      let cls = "step";
      if (activeIndex === -1) {
        cls += progress >= 100 ? " is-done" : "";
      } else if (index < activeIndex) cls += " is-done";
      else if (index === activeIndex) cls += " is-active";
      return `<div class="${cls}"><span class="step-dot"></span>${esc(label)}</div>`;
    })
    .join("");
  $("#progress-bar").style.width = `${Math.max(4, progress || 0)}%`;
}

async function analyzeAndSearch(file) {
  const panel = $("#find-progress");
  panel.classList.remove("hidden");
  $("#find-result").innerHTML = "";
  $("#dropzone").classList.add("busy");
  renderPipeline(ANALYZE_STEPS, "upload", 6);
  $("#progress-msg").textContent = `reading ${file.name || "clip"}…`;

  const form = new FormData();
  form.append("file", file);
  form.append("frames", String(state.settings.frames || 6));
  const { job_id } = await api("/api/analyze", { method: "POST", body: form });

  const analysis = await pollJob(job_id, (job) => {
    renderPipeline(ANALYZE_STEPS, job.stage, job.progress);
    if (job.message) $("#progress-msg").textContent = job.message;
  });

  state.current = analysis.clip;
  renderResult(analysis);

  const dup = analysis.dedup && analysis.dedup.verdict === "duplicate";
  if (dup) {
    $("#progress-msg").textContent = "already in your library — search skipped";
    panel.classList.add("hidden");
    $("#dropzone").classList.remove("busy");
    return;
  }
  await runSearch(analysis.clip.id);
}

async function analyzeUrl(url) {
  const panel = $("#find-progress");
  panel.classList.remove("hidden");
  $("#find-result").innerHTML = "";
  $("#dropzone").classList.add("busy");
  renderPipeline(ANALYZE_STEPS, "upload", 6);
  $("#progress-msg").textContent = "downloading the clip…";

  const form = new FormData();
  form.append("url", url);
  form.append("frames", String(state.settings.frames || 6));
  const { job_id } = await api("/api/analyze", { method: "POST", body: form });

  const analysis = await pollJob(job_id, (job) => {
    renderPipeline(ANALYZE_STEPS, job.stage, job.progress);
    if (job.message) $("#progress-msg").textContent = job.message;
  });

  state.current = analysis.clip;
  renderResult(analysis);
  const dup = analysis.dedup && analysis.dedup.verdict === "duplicate";
  if (dup) {
    panel.classList.add("hidden");
    $("#dropzone").classList.remove("busy");
    return;
  }
  await runSearch(analysis.clip.id);
}

async function runSearch(clipId, opts = {}) {
  const panel = $("#find-progress");
  panel.classList.remove("hidden");
  $("#dropzone").classList.add("busy");
  const depth = $("#opt-deep").checked ? 3 : 1;
  renderPipeline([["search", "asking the engines"]], "search", 35);
  $("#progress-msg").textContent = "searching Google Lens and Yandex in parallel…";

  try {
    const { job_id } = await api(`/api/clips/${clipId}/search`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ providers: activeEngines(), depth }),
    });
    const result = await pollJob(job_id, (job) => {
      renderPipeline([["search", "asking the engines"]], job.stage, job.progress);
      if (job.message) $("#progress-msg").textContent = job.message;
    });
    state.current = result.clip;
    renderResult({ clip: result.clip, dedup: null, frames: [], run: result, searched: true });
    const count = (result.candidates || []).length;
    toast(count ? `${count} possible sources found` : "No links found — try the browser fallback", count ? "good" : "warn");
  } catch (error) {
    toast(error.message, "bad");
    $("#progress-msg").textContent = `search failed: ${error.message}`;
  } finally {
    $("#dropzone").classList.remove("busy");
    panel.classList.add("hidden");
    refreshStats();
    if (state.view === "library") loadLibrary();
  }
}

/* ------------------------------------------------------------- find result */
function renderResult(payload) {
  const clip = payload.clip;
  if (!clip) return;
  const dedup = payload.dedup || null;
  const candidates = clip.candidates || [];
  const frames = (payload.frames && payload.frames.length
    ? payload.frames
    : Array.from({ length: Math.min(clip.frames || 0, 8) }, (_, i) => `${clip.id}/frames/frame_0${i}.jpg`));

  const providerRuns = payload.run ? payload.run.providers : [];
  const queryUrls = (payload.run && payload.run.query_urls) || {};
  const used = clip.times_used || 0;

  const html = [];
  html.push(`
    <div class="result-head">
      <img class="result-thumb" src="${thumbUrl(clip.thumb)}" alt="" />
      <div class="result-meta">
        <h2>${esc(clip.title || "untitled clip")}</h2>
        <div class="sub">${esc(clip.source_label || "")} · ${clip.width}×${clip.height}${clip.duration ? " · " + humanDuration(clip.duration) : ""} · ${clip.frames} frames sampled</div>
        <div class="badge-row">
          ${clip.best_source ? '<span class="badge good">source found</span>' : '<span class="badge">no source saved yet</span>'}
          ${verdictBadge(clip)}
          ${used ? `<span class="badge warn">used ${used}×${clip.used_in ? " · " + esc(clip.used_in) : ""}</span>` : '<span class="badge">never used</span>'}
          ${clip.tags && clip.tags.length ? clip.tags.map((t) => `<span class="badge accent">${esc(t)}</span>`).join("") : ""}
        </div>
        <div class="result-actions">
          <button class="btn btn-sm" data-act="use" data-id="${clip.id}">Mark as used</button>
          <button class="btn btn-sm" data-act="note" data-id="${clip.id}">Add note / project</button>
          <button class="btn btn-sm" data-act="research" data-id="${clip.id}">Search again</button>
          <button class="btn btn-sm btn-ghost" data-act="open-clip" data-id="${clip.id}">Details</button>
        </div>
        <div class="frames-strip">
          ${frames.slice(0, 10).map((f) => `<img src="${thumbUrl(f)}" data-act="zoom" data-src="${thumbUrl(f)}" alt="" onerror="this.remove()" />`).join("")}
        </div>
      </div>
    </div>`);

  if (dedup && dedup.verdict !== "distinct" && dedup.match) {
    const m = dedup.match;
    const isDup = dedup.verdict === "duplicate";
    html.push(`
      <div class="dup-banner">
        <h3>${isDup ? "⚠️ You already have this footage" : "🤔 This looks familiar"}</h3>
        <p>${isDup
          ? `Skip the search and reuse your existing clip instead of pulling it twice.`
          : `Some frames overlap with an existing clip — check before adding it to another edit.`}</p>
        <div class="dup-match">
          <img src="${thumbUrl(m.thumb)}" alt="" />
          <div>
            <div class="what">${esc(m.title || "clip #" + m.id)}</div>
            <div class="why">${m.comparison.percent}% match · ${esc((m.comparison.reasons || []).join(" · "))}${m.used_in ? " · used in " + esc(m.used_in) : ""}</div>
          </div>
          <button class="btn btn-sm" data-act="open-clip" data-id="${m.id}" style="margin-left:auto">Open that clip</button>
        </div>
        <div class="result-actions">
          <button class="btn btn-sm btn-primary" data-act="search-anyway" data-id="${clip.id}">Search anyway</button>
          <button class="btn btn-sm btn-ghost" data-act="delete-clip" data-id="${clip.id}">Discard this copy</button>
        </div>
      </div>`);
  }

  const recognition = (payload.run && payload.run.recognition) || [];
  if (recognition.length) {
    html.push(`<div class="callout" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      <strong style="font-size:12.5px;color:var(--text)">Engines recognise:</strong>
      ${recognition.map((t) => `<span class="badge accent">${esc(t)}</span>`).join("")}
      <small style="color:var(--muted-dim);font-size:11.5px">— search these words on YouTube if the links look wrong</small>
    </div>`);
  }

  if (providerRuns.length || candidates.length) {
    html.push(`<div class="section-title"><h3>Where it came from</h3>
      <small>${candidates.length} link${candidates.length === 1 ? "" : "s"} · ranked by how likely they are the original</small></div>`);
    if (providerRuns.length) {
      html.push(`<div class="engine-status">${providerRuns.map((r) => `
        <span class="engine-pill ${r.status === "ok" ? "ok" : "err"}" title="${esc(r.error || r.note || "")}">
          <b>${esc(r.provider)}</b> ${r.status === "ok" ? `${r.found} found` : "failed"}
          <span class="ms">${r.ms}ms</span>
        </span>`).join("")}</div>`);
    }
    if (candidates.length) {
      html.push(`<div class="cand-list">${candidates.map((c, i) => candidateCard(c, i)).join("")}</div>`);
    } else {
      const why = providerRuns.filter((r) => r.status !== "ok" && r.error).map((r) => `${r.provider}: ${r.error}`);
      html.push(`<div class="empty"><strong>No usable links yet</strong>
        The engines may be rate-limiting this machine. Try the browser fallback below, or paste the original link you find.
        ${why.length ? `<div style="margin-top:12px;font-family:var(--mono);font-size:11px;color:var(--muted-dim)">${why.map(esc).join("<br>")}</div>` : ""}
        </div>`);
    }
  }

  html.push(`
    <div class="panel" style="margin-top:18px">
      <h2>Add the original by hand</h2>
      <p class="muted">Found it yourself? Paste the link and it becomes the saved source for this clip.</p>
      <div class="url-field" style="max-width:none">
        <input type="url" id="manual-url" placeholder="https://www.youtube.com/watch?v=…" />
        <button class="btn" data-act="manual" data-id="${clip.id}">Save source</button>
      </div>
    </div>`);

  html.push(`
    <div class="panel">
      <h2>If the engines come up empty</h2>
      <p class="muted">These run in your browser where Google/Yandex see your real session. Drag the frame below into the search box.</p>
      <div class="result-actions">
        <a class="btn btn-sm" href="https://lens.google.com/" target="_blank" rel="noopener">Open Google Lens</a>
        <a class="btn btn-sm" href="https://yandex.com/images/" target="_blank" rel="noopener">Open Yandex Images</a>
        <a class="btn btn-sm" href="https://tineye.com/" target="_blank" rel="noopener">Open TinEye</a>
        <a class="btn btn-sm btn-ghost" href="${clip.frames ? `/clips/${clip.id}/frames/frame_00.jpg` : thumbUrl(clip.thumb)}" download="frame.jpg">Download query frame</a>
        ${queryUrls.lens ? `<a class="btn btn-sm btn-ghost" href="${esc(queryUrls.lens)}" target="_blank" rel="noopener">Re-open last Lens results</a>` : ""}
      </div>
    </div>`);

  $("#find-result").innerHTML = html.join("");
}

function candidateCard(cand, index) {
  const thumb = cand.thumb
    ? `<img class="cand-thumb" src="${esc(cand.thumb)}" alt="" onerror="this.style.visibility='hidden'" />`
    : `<div class="cand-thumb empty">${esc((cand.host || "?").slice(0, 2).toUpperCase())}</div>`;
  const providers = (cand.provider || "").split(",").filter(Boolean)
    .map((p) => ({ lens: "Lens", yandex: "Yandex", bing: "Bing", manual: "manual", demo: "demo" }[p] || p))
    .join(" + ");
  const score = Math.round(cand.score || 0);
  return `
    <div class="cand ${cand.confirmed ? "is-confirmed" : ""}">
      <div class="cand-rank">${index + 1}</div>
      ${thumb}
      <div class="cand-body">
        <div class="cand-title"><a href="${esc(cand.url)}" target="_blank" rel="noopener">${esc(cand.title || cand.url)}</a></div>
        <div class="cand-host">${esc(cand.host)}${providers ? " · " + esc(providers) : ""}${cand.author ? " · " + esc(cand.author) : ""}${cand.manual ? " · added by you" : ""}</div>
        <div class="cand-reasons">${(cand.reasons || []).map((r) => `<span class="reason">${esc(r)}</span>`).join("")}</div>
      </div>
      <div class="cand-side">
        <div class="score"><span>${score}</span><span class="score-bar"><i style="width:${Math.min(100, score)}%"></i></span></div>
        <div class="cand-actions">
          <a class="btn btn-sm" href="${esc(cand.url)}" target="_blank" rel="noopener">Open</a>
          <button class="btn btn-sm ${cand.confirmed ? "btn-good" : "btn-ghost"}" data-act="confirm-cand" data-id="${cand.id}">
            ${cand.confirmed ? "✓ Original" : "This is it"}
          </button>
          <button class="icon-btn" title="Remove" data-act="delete-cand" data-id="${cand.id}">✕</button>
        </div>
      </div>
    </div>`;
}

/* ----------------------------------------------------------------- library */
async function loadLibrary() {
  const params = new URLSearchParams();
  if (state.query) params.set("q", state.query);
  if (state.filter) params.set("status", state.filter);
  const data = await api(`/api/clips?${params.toString()}`);
  state.stats = data.stats;
  paintStats();
  const box = $("#library-list");
  if (!data.clips.length) {
    box.innerHTML = `<div class="empty" style="grid-column:1/-1"><strong>Nothing here yet</strong>
      Drop your first clip on the Find sources tab.</div>`;
    return;
  }
  box.innerHTML = data.clips.map(clipCard).join("");
}

function clipCard(clip) {
  const used = clip.times_used || 0;
  return `
    <div class="clip-card" data-act="open-clip" data-id="${clip.id}">
      ${used ? `<span class="use-badge ${used > 1 ? "repeat" : ""}">used ${used}×</span>` : ""}
      ${clip.thumb
        ? `<img class="clip-thumb" src="${thumbUrl(clip.thumb)}" alt="" loading="lazy" />`
        : `<div class="clip-thumb-empty">no preview</div>`}
      <div class="clip-info">
        <div class="clip-name">${esc(clip.title || clip.source_label || "untitled")}</div>
        <div class="clip-foot">
          <span class="badge ${clip.best_source ? "good" : ""}">${clip.best_source ? "source saved" : clip.status === "no_results" ? "not found" : "unsearched"}</span>
          ${verdictBadge(clip) || `<span class="badge">${esc(clip.source_kind || "clip")}</span>`}
        </div>
      </div>
    </div>`;
}

/* -------------------------------------------------------------- duplicates */
async function loadDuplicates() {
  const box = $("#dupe-list");
  box.innerHTML = `<div class="empty">comparing fingerprints…</div>`;
  const data = await api("/api/duplicates");
  const count = data.groups.length;
  $("#count-dupes").textContent = count;
  if (!count) {
    box.innerHTML = `<div class="empty"><strong>No repeats</strong>
      Every clip in your library is unique footage. Get to work.</div>`;
    return;
  }
  box.innerHTML = data.groups.map((group) => `
    <div class="dupe-group">
      <div class="dupe-head">
        <h3>${group.size} versions of the same footage · ${group.total_uses} total use${group.total_uses === 1 ? "" : "s"}</h3>
        <span class="badge warn">keep one, drop the rest</span>
      </div>
      <div class="dupe-members">
        ${group.members.map((m) => `
          <div class="dupe-member" data-act="open-clip" data-id="${m.id}">
            ${m.thumb ? `<img src="${thumbUrl(m.thumb)}" alt="" loading="lazy" />` : `<div class="clip-thumb-empty">—</div>`}
            <div>${esc(m.title || "clip #" + m.id)}
              <small>used ${m.times_used || 0}× ${m.used_in ? "· " + esc(m.used_in) : ""}</small>
            </div>
          </div>`).join("")}
      </div>
    </div>`).join("");
}

/* -------------------------------------------------------------------- scan */
async function startScan() {
  const folder = $("#scan-folder").value.trim();
  if (!folder) { toast("Paste a folder path first", "warn"); return; }
  localStorage.setItem("sourcerers:folder", folder);
  $("#scan-result").innerHTML = "";
  $("#scan-progress").classList.remove("hidden");
  $("#scan-bar").style.width = "4%";
  $("#scan-msg").textContent = "walking the folder…";
  try {
    const { job_id } = await api("/api/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder, recursive: $("#scan-recursive").checked }),
    });
    const result = await pollJob(job_id, (job) => {
      $("#scan-bar").style.width = `${Math.max(4, job.progress)}%`;
      if (job.message) $("#scan-msg").textContent = job.message;
    });
    $("#scan-progress").classList.add("hidden");
    renderScanResult(result);
    refreshStats();
  } catch (error) {
    $("#scan-progress").classList.add("hidden");
    toast(error.message, "bad");
  }
}

function renderScanResult(result) {
  const box = $("#scan-result");
  const rows = (items, kind) => items.map((item) => `
    <div class="cand">
      <div class="cand-rank">${kind === "repeat" ? "♻" : "+"}</div>
      ${item.thumb ? `<img class="cand-thumb" src="${thumbUrl(item.thumb)}" alt="" />` : `<div class="cand-thumb empty">—</div>`}
      <div class="cand-body">
        <div class="cand-title">${esc(item.title)}</div>
        <div class="cand-host">${esc(item.file)}</div>
        ${item.match ? `<div class="cand-reasons"><span class="reason">${item.match.comparison.percent}% match with “${esc(item.match.title || "clip #" + item.match.id)}”</span></div>` : ""}
      </div>
      <div class="cand-side">
        <div class="cand-actions"><button class="btn btn-sm" data-act="open-clip" data-id="${item.id}">Open</button></div>
      </div>
    </div>`).join("");

  box.innerHTML = `
    <div class="section-title">
      <h3>${result.scanned} files scanned in ${esc(result.folder)}</h3>
      <small>${result.added.length} new · ${result.repeats.length} repeats · ${result.errors.length} skipped</small>
    </div>
    ${result.repeats.length ? `<div class="section-title"><h3>Already in your library</h3></div>
      <div class="cand-list">${rows(result.repeats, "repeat")}</div>` : ""}
    ${result.added.length ? `<div class="section-title"><h3>New footage added</h3></div>
      <div class="cand-list">${rows(result.added, "new")}</div>` : ""}
    ${result.errors.length ? `<div class="section-title"><h3>Couldn't read</h3></div>
      <div class="cand-list">${result.errors.map((e) => `<div class="cand"><div class="cand-rank">!</div><div class="cand-body"><div class="cand-title">${esc(e.file)}</div><div class="cand-host">${esc(e.error)}</div></div></div>`).join("")}</div>` : ""}`;
}

/* ---------------------------------------------------------------- settings */
function fillSettings() {
  const s = state.settings;
  $("#set-frames").value = s.frames || 6;
  $("#out-frames").textContent = s.frames || 6;
  $("#set-mean").value = s.dup_mean_distance ?? 0.2;
  $("#out-mean").textContent = Number(s.dup_mean_distance ?? 0.2).toFixed(2);
  $("#set-cov").value = s.dup_coverage ?? 0.45;
  $("#out-cov").textContent = Number(s.dup_coverage ?? 0.45).toFixed(2);
  $("#set-bing").value = s.bing_key || "";
  $("#set-enrich").checked = s.enrich_results !== false;
  $("#set-fullvideo").checked = s.prefer_full_video !== false;
  $("#set-demo").checked = !!s.demo_mode;
  $("#data-dir").textContent = state.caps.data_dir || "—";

  $("#engine-settings").innerHTML = state.providers.map((p) => `
    <div class="engine-row">
      <label class="switch small"><input type="checkbox" data-engine-setting="${p.id}" ${(s.providers || []).includes(p.id) ? "checked" : ""} ${p.available ? "" : "disabled"} />
        <span class="switch-track"></span></label>
      <div class="grow">
        <div class="name">${esc(p.label)} ${p.free ? '<span class="badge good">free</span>' : '<span class="badge">optional key</span>'}</div>
        <div class="help">${esc(p.help)}${p.available ? "" : " · " + esc(p.reason)}</div>
      </div>
    </div>`).join("");
}

async function saveSettings() {
  const providers = $$("[data-engine-setting]").filter((i) => i.checked).map((i) => i.dataset.engineSetting);
  const patch = {
    frames: Number($("#set-frames").value),
    dup_mean_distance: Number($("#set-mean").value),
    dup_coverage: Number($("#set-cov").value),
    bing_key: $("#set-bing").value.trim(),
    enrich_results: $("#set-enrich").checked,
    prefer_full_video: $("#set-fullvideo").checked,
    demo_mode: $("#set-demo").checked,
    providers,
  };
  const data = await api("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  state.settings = data.settings;
  state.providers = data.providers;
  engineChips();
  toast("Settings saved", "good");
}

/* ------------------------------------------------------------------- modal */
async function openClip(id) {
  try {
    const { clip } = await api(`/api/clips/${id}`);
    state.current = clip;
    const candidates = clip.candidates || [];
    $("#modal-title").textContent = clip.title || `clip #${clip.id}`;
    $("#modal-body").innerHTML = `
      ${clip.thumb ? `<img class="preview" src="${thumbUrl(clip.thumb)}" alt="" />` : ""}
      <div class="badge-row">
        <span class="badge">${clip.width}×${clip.height}</span>
        <span class="badge">${clip.frames} frames</span>
        ${clip.duration ? `<span class="badge">${humanDuration(clip.duration)}</span>` : ""}
        <span class="badge ${clip.times_used ? "warn" : ""}">used ${clip.times_used || 0}×</span>
        ${verdictBadge(clip)}
      </div>
      <div class="frames-strip">${Array.from({ length: Math.min(clip.frames, 8) }, (_, i) =>
        `<img src="/clips/${clip.id}/frames/frame_0${i}.jpg" data-act="zoom" data-src="/clips/${clip.id}/frames/frame_0${i}.jpg" alt="" onerror="this.remove()" />`).join("")}</div>
      <div class="result-actions" style="margin:14px 0">
        <button class="btn btn-sm" data-act="use" data-id="${clip.id}">Mark as used</button>
        <button class="btn btn-sm btn-ghost" data-act="research" data-id="${clip.id}">Search again</button>
        <button class="btn btn-sm btn-danger" data-act="delete-clip" data-id="${clip.id}">Delete clip</button>
      </div>
      <label class="field"><span>Title</span><input id="m-title" value="${esc(clip.title)}" /></label>
      <label class="field"><span>Used in (project / video)</span><input id="m-usedin" value="${esc(clip.used_in)}" placeholder="e.g. Top 10 Movies #14" /></label>
      <label class="field"><span>Notes</span><textarea id="m-notes" rows="3">${esc(clip.notes)}</textarea></label>
      <label class="field"><span>Tags (comma separated)</span><input id="m-tags" value="${esc((clip.tags || []).join(", "))}" /></label>
      <button class="btn btn-primary" data-act="save-clip" data-id="${clip.id}">Save clip details</button>
      <div class="section-title"><h3>Sources</h3><small>${candidates.length} saved</small></div>
      ${candidates.length
        ? `<div class="cand-list">${candidates.map((c, i) => candidateCard(c, i)).join("")}</div>`
        : `<div class="empty">No source found yet. <button class="btn btn-sm" data-act="research" data-id="${clip.id}">Run a search</button></div>`}
    `;
    $("#modal").hidden = false;
  } catch (error) {
    toast(error.message, "bad");
  }
}

function closeModal() { $("#modal").hidden = true; }

/* --------------------------------------------------------------- lifecycle */
function paintStats() {
  const s = state.stats || {};
  $("#stat-clips").textContent = s.clips ?? 0;
  $("#stat-sourced").textContent = s.sourced ?? 0;
  $("#stat-used").textContent = s.used ?? 0;
  $("#count-clips").textContent = s.clips ?? 0;
}

async function refreshStats() {
  try {
    const data = await api("/api/clips?limit=1");
    state.stats = data.stats;
    paintStats();
    const dupes = await api("/api/duplicates");
    $("#count-dupes").textContent = dupes.groups.length;
  } catch { /* ignore */ }
}

async function loadState() {
  const data = await api("/api/state");
  state.settings = data.settings;
  state.providers = data.providers;
  state.caps = data.capabilities;
  state.stats = data.stats;
  $("#version").textContent = `v${data.version}`;

  const caps = [
    `<span class="cap ${data.capabilities.ffmpeg ? "ok" : "off"}">ffmpeg ${data.capabilities.ffmpeg ? "✓" : "missing"}</span>`,
    `<span class="cap ${data.capabilities.ytdlp ? "ok" : "off"}">yt-dlp ${data.capabilities.ytdlp ? "✓" : "missing"}</span>`,
  ];
  if (data.settings.demo_mode) caps.push('<span class="cap off">demo mode</span>');
  $("#cap-row").innerHTML = caps.join("");
  $("#dz-hint").textContent = data.capabilities.ffmpeg
    ? ""
    : "ffmpeg not found — videos won't work. Run: pip install imageio-ffmpeg";

  engineChips();
  paintStats();
  refreshStats();
}

function bindEvents() {
  $("#nav").addEventListener("click", (e) => {
    const btn = e.target.closest(".nav-item");
    if (btn) setView(btn.dataset.view);
  });

  // dropzone
  const dz = $("#dropzone");
  $("#browse-btn").addEventListener("click", () => $("#file-input").click());
  $("#file-input").addEventListener("change", (e) => {
    if (e.target.files.length) enqueueFiles([...e.target.files]);
  });
  dz.addEventListener("click", (e) => {
    if (e.target.closest("input, button, a")) return;
    $("#file-input").click();
  });
  dz.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#file-input").click(); }
  });
  ["dragenter", "dragover"].forEach((type) => dz.addEventListener(type, (e) => {
    e.preventDefault(); dz.classList.add("is-drag");
  }));
  ["dragleave", "drop"].forEach((type) => dz.addEventListener(type, (e) => {
    e.preventDefault(); dz.classList.remove("is-drag");
  }));
  dz.addEventListener("drop", (e) => {
    const files = [...(e.dataTransfer?.files || [])];
    if (files.length) enqueueFiles(files);
  });

  // paste anywhere
  document.addEventListener("paste", (e) => {
    const target = e.target;
    const typing = target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA");
    const items = [...(e.clipboardData?.items || [])];
    const image = items.find((i) => i.type.startsWith("image/"));
    if (image) {
      e.preventDefault();
      const file = image.getAsFile();
      if (file) {
        setView("find");
        enqueueFiles([new File([file], `screenshot-${Date.now()}.png`, { type: file.type })]);
      }
      return;
    }
    if (typing) return;
    const text = e.clipboardData?.getData("text")?.trim();
    if (text && /^https?:\/\//i.test(text)) {
      setView("find");
      $("#url-input").value = text;
      toast("Link pasted — hit Fetch clip");
    }
  });

  $("#url-btn").addEventListener("click", () => {
    const url = $("#url-input").value.trim();
    if (!url) { toast("Paste a link first", "warn"); return; }
    runExclusive(() => analyzeUrl(url));
  });
  $("#url-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("#url-btn").click();
  });

  $("#engine-picker").addEventListener("click", (e) => {
    const chip = e.target.closest("[data-engine]");
    if (!chip) return;
    chip.classList.toggle("is-active");
    if (!activeEngines().length) chip.classList.add("is-active");
  });

  // library
  let searchTimer;
  $("#library-search").addEventListener("input", (e) => {
    state.query = e.target.value.trim();
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadLibrary, 260);
  });
  $("#library-filters").addEventListener("click", (e) => {
    const chip = e.target.closest("[data-filter]");
    if (!chip) return;
    state.filter = chip.dataset.filter;
    $$("#library-filters .chip").forEach((c) => c.classList.toggle("is-active", c === chip));
    loadLibrary();
  });

  // scan + settings
  $("#scan-btn").addEventListener("click", startScan);
  $("#save-settings").addEventListener("click", () => saveSettings().catch((e) => toast(e.message, "bad")));
  [["#set-frames", "#out-frames", 0], ["#set-mean", "#out-mean", 2], ["#set-cov", "#out-cov", 2]]
    .forEach(([input, out, digits]) => {
      $(input).addEventListener("input", (e) => {
        $(out).textContent = digits ? Number(e.target.value).toFixed(digits) : e.target.value;
      });
    });

  // modal
  $("#modal-close").addEventListener("click", closeModal);
  $("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

  // delegated actions
  document.addEventListener("click", async (e) => {
    const target = e.target.closest("[data-act]");
    if (!target) return;
    const act = target.dataset.act;
    const id = Number(target.dataset.id);

    if (act === "zoom") {
      $("#modal-title").textContent = "Frame";
      $("#modal-body").innerHTML = `<img class="preview" src="${target.dataset.src}" alt="" />`;
      $("#modal").hidden = false;
      return;
    }
    if (act === "open-clip") { e.stopPropagation(); openClip(id); return; }
    if (act === "use") {
      let clip = state.current && state.current.id === id
        ? state.current
        : (await api(`/api/clips/${id}`)).clip;
      const updated = await patchClip(id, {
        times_used: (clip.times_used || 0) + 1,
        used_in: clip.used_in || "",
      });
      toast(`Marked as used (${updated.times_used}×)`, "good");
      if (!$("#modal").hidden) openClip(id);
      else if ($("#find-result").innerHTML) renderResult({ clip: updated, dedup: null, frames: [], run: null });
      return;
    }
    if (act === "note") {
      const value = prompt("Where did you use this clip? (project / video name)");
      if (value !== null) { await patchClip(id, { used_in: value }); toast("Saved", "good"); }
      return;
    }
    if (act === "research") { closeModal(); setView("find"); await runSearch(id); return; }
    if (act === "search-anyway") { await runSearch(id); return; }
    if (act === "delete-clip") {
      if (!confirm("Remove this clip from the library?")) return;
      await api(`/api/clips/${id}`, { method: "DELETE" });
      closeModal();
      $("#find-result").innerHTML = "";
      toast("Clip deleted");
      refreshStats();
      if (state.view === "library") loadLibrary();
      return;
    }
    if (act === "confirm-cand") {
      const data = await api(`/api/candidates/${id}/confirm`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirmed: true }),
      });
      state.current = data.clip;
      refreshStats();
      toast("Saved as the original source", "good");
      if (!$("#modal").hidden) openClip(data.clip.id);
      else renderResult({ clip: data.clip, dedup: null, frames: [], run: null });
      return;
    }
    if (act === "delete-cand") {
      const data = await api(`/api/candidates/${id}`, { method: "DELETE" });
      state.current = data.clip;
      if (!$("#modal").hidden) openClip(data.clip.id);
      else renderResult({ clip: data.clip, dedup: null, frames: [], run: null });
      return;
    }
    if (act === "manual") {
      const input = $("#manual-url");
      const url = input && input.value.trim();
      if (!url) { toast("Paste a link first", "warn"); return; }
      try {
        const data = await api(`/api/clips/${id}/candidates`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url }),
        });
        state.current = data.clip;
        toast("Source saved", "good");
        renderResult({ clip: data.clip, dedup: null, frames: [], run: null });
      } catch (error) { toast(error.message, "bad"); }
      return;
    }
    if (act === "save-clip") {
      await api(`/api/clips/${id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: $("#m-title").value.trim(),
          used_in: $("#m-usedin").value.trim(),
          notes: $("#m-notes").value.trim(),
          tags: $("#m-tags").value.split(",").map((t) => t.trim()).filter(Boolean),
        }),
      });
      toast("Clip details saved", "good");
      closeModal();
      if (state.view === "library") loadLibrary();
      return;
    }
  });
}

async function patchClip(id, patch) {
  const data = await api(`/api/clips/${id}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  state.current = data.clip;
  refreshStats();
  if (state.view === "library") loadLibrary();
  return data.clip;
}

/* file queue: process dropped files one after another */
function enqueueFiles(files) {
  const usable = files.filter((f) => f.type.startsWith("video/") || f.type.startsWith("image/") || /\.(mp4|mov|mkv|webm|avi|m4v|gif|png|jpe?g|webp)$/i.test(f.name));
  if (!usable.length) { toast("That doesn't look like a video or image", "warn"); return; }
  state.queue.push(...usable);
  processQueue();
}

async function processQueue() {
  if (state.processing) return;
  state.processing = true;
  while (state.queue.length) {
    const file = state.queue.shift();
    try {
      await analyzeAndSearch(file);
    } catch (error) {
      toast(error.message, "bad");
      $("#dropzone").classList.remove("busy");
      $("#progress-msg").textContent = `failed: ${error.message}`;
      $("#find-progress").classList.remove("hidden");
    }
    if (state.queue.length) await sleep(600);
  }
  state.processing = false;
}

async function runExclusive(fn) {
  if (state.processing) { toast("Still working on the last one", "warn"); return; }
  state.processing = true;
  try { await fn(); } catch (error) { toast(error.message, "bad"); }
  finally {
    state.processing = false;
    $("#dropzone").classList.remove("busy");
    $("#find-progress").classList.add("hidden");
  }
}

loadState().then(() => {
  bindEvents();
  setView("find");
}).catch((error) => {
  document.body.innerHTML = `<div style="padding:40px;font-family:system-ui;color:#eee">
    <h2>Could not reach the Sourcerers backend</h2><pre>${esc(error.message)}</pre></div>`;
});
