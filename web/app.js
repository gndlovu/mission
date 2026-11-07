/* Mission — frontend. No framework, no build step. */

const KIND = {
  agent:     { color: "#eaf2f5", glow: "#29f5ff", r: 7.5 },
  core:      { color: "#34d399", glow: "#34d399", r: 5.0 },
  workspace: { color: "#29f5ff", glow: "#29f5ff", r: 4.2 },
  index:     { color: "#a855f7", glow: "#a855f7", r: 4.2 },
  hub:       { color: "#ec4899", glow: "#ec4899", r: 7.0 },
  stale:     { color: "#fb7185", glow: "#fb7185", r: 4.0 },
};

const DOC_ICON = {
  html: "◈", markdown: "❖", code: "▣", data: "⬢",
  image: "▦", doc: "▤", text: "▭", other: "◻",
};

const state = {
  nodes: [], edges: [],
  layout: "mid", motion: "flow", links: true,
  hover: null,
  cam: { x: 0, y: 0, zoom: 1 },
  drag: null,
  docs: [], docFilter: "all", docQuery: "",
};

const fmt = (n) => (n === null || n === undefined ? "—" : Number(n).toLocaleString());
const money = (n) => "$" + (Number(n) || 0).toFixed(2);
const kb = (b) => (b >= 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(b / 1024)) + " KB");

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

async function getJSON(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(path + " → " + r.status);
  return r.json();
}

/* ------------------------------------------------------------- routing */

function show(view) {
  document.querySelectorAll("[data-page]").forEach((el) =>
    el.classList.toggle("hidden", el.dataset.page !== view));
  document.querySelectorAll("#nav button").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === view));
  document.getElementById("crumbView").textContent = view;
  if (view === "home") resize();
}

document.getElementById("nav").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (b) show(b.dataset.view);
});

/* ---------------------------------------------------------------- data */

async function loadStatus() {
  const s = await getJSON("/api/status");
  const t = s.totals || {}, g = s.graph || {}, d = s.docs || {};

  document.getElementById("homeMeta").textContent = `${s.home} · profile ${s.profile}`;
  document.getElementById("railFoot").innerHTML =
    `profile ${esc(s.profile)}<br>${g.nodes || 0} nodes<br>${d.count || 0} artifacts`;

  const cards = [
    ["Sessions", fmt(t.sessions), "", ""],
    ["Messages", fmt(t.messages), "", ""],
    ["Tool calls", fmt(t.tool_calls), "", ""],
    ["Tokens", fmt((t.input_tokens || 0) + (t.output_tokens || 0)),
      `${fmt(t.input_tokens)} in · ${fmt(t.output_tokens)} out`, ""],
    ["Spend", money(t.cost_usd), "all time", "magenta"],
    ["Skills", fmt(g.skills), `${g.stale || 0} stale`, g.stale ? "red" : "lime"],
    ["Artifacts", fmt(d.count), kb(d.bytes || 0), "lime"],
  ];
  document.getElementById("homeStats").innerHTML = cards
    .map(([k, v, sub, cls]) =>
      `<div class="card stat"><div class="k">${k}</div>
       <div class="v ${cls}">${v}</div>
       ${sub ? `<div class="sub">${sub}</div>` : ""}</div>`).join("");
}

async function loadGraph() {
  const g = await getJSON("/api/graph");
  state.nodes = g.nodes.map((n, i) => {
    const a = (i / g.nodes.length) * Math.PI * 2;
    const rad = n.kind === "agent" ? 0 : 120 + (i % 7) * 42;
    return {
      ...n,
      x: Math.cos(a) * rad + (Math.random() - 0.5) * 30,
      y: Math.sin(a) * rad + (Math.random() - 0.5) * 30,
      vx: 0, vy: 0,
      r: (KIND[n.kind] || KIND.core).r * (0.75 + (n.weight || 1) * 0.22),
      phase: Math.random() * Math.PI * 2,
    };
  });
  const byId = new Map(state.nodes.map((n) => [n.id, n]));
  state.edges = g.edges
    .map((e) => ({ s: byId.get(e.source), t: byId.get(e.target) }))
    .filter((e) => e.s && e.t);
  document.getElementById("graphCount").textContent =
    `${g.stats.nodes} nodes · ${g.stats.edges} edges`;
}

async function loadSkills() {
  const { skills, count } = await getJSON("/api/skills");
  document.getElementById("skillMeta").textContent =
    `${count} skills · ${new Set(skills.map((s) => s.category)).size} categories`;
  document.getElementById("skillList").innerHTML = skills.length
    ? skills.map((s) => `
      <div class="row">
        <span class="tag magenta">${esc(s.category)}</span>
        <div class="grow">
          <div class="name">${esc(s.name)}</div>
          <div class="desc">${esc(s.description || "—")}</div>
        </div>
        ${s.stale ? '<span class="tag stale">stale</span>' : ""}
        <span class="tag">${kb(s.bytes)}</span>
      </div>`).join("")
    : '<div class="empty">no skills found</div>';
}

async function loadMemory() {
  const { entries, count } = await getJSON("/api/memory");
  document.getElementById("memMeta").textContent = `${count} entries`;
  document.getElementById("memList").innerHTML = entries.length
    ? entries.map((e) => `<div class="memo">${esc(e.text)}</div>`).join("")
    : '<div class="empty">no memory entries</div>';
}

async function loadActivity() {
  const { sessions, spend } = await getJSON("/api/activity");
  document.getElementById("actMeta").textContent = `${sessions.length} recent sessions`;
  const max = Math.max(...spend.map((d) => d.cost), 0.0001);
  document.getElementById("spendBars").innerHTML = spend.map((d) => {
    const h = Math.max(2, (d.cost / max) * 56);
    return `<div class="bar${d.cost >= max * 0.7 ? " hot" : ""}" style="height:${h}px" title="${d.day} · ${money(d.cost)}"></div>`;
  }).join("");
  document.getElementById("sessList").innerHTML = sessions.length
    ? sessions.map((s) => `
      <div class="row">
        <span class="tag">${esc(s.source || "?")}</span>
        <div class="grow">
          <div class="name">${esc(s.title || s.display_name || s.id)}</div>
          <div class="desc">${fmt(s.message_count)} msg · ${fmt(s.tool_call_count)} tools · ${esc(s.model || "—")}</div>
        </div>
        <span class="tag magenta">${money(s.cost_usd)}</span>
      </div>`).join("")
    : '<div class="empty">no sessions</div>';
}

/* ----------------------------------------------------------- documents */

async function loadDocs() {
  const { docs, stats } = await getJSON("/api/docs");
  state.docs = docs;
  document.getElementById("docMeta").textContent =
    `${stats.count} files · ${kb(stats.bytes)} · ${stats.root}`;

  const kinds = Object.entries(stats.by_kind).sort((a, b) => b[1] - a[1]);
  document.getElementById("docChips").innerHTML =
    [`<button class="chip${state.docFilter === "all" ? " on" : ""}" data-kind="all">all ${stats.count}</button>`]
      .concat(kinds.map(([k, n]) =>
        `<button class="chip${state.docFilter === k ? " on" : ""}" data-kind="${k}">${k} ${n}</button>`))
      .join("");
  renderDocs();
}

function renderDocs() {
  const q = state.docQuery.toLowerCase();
  const list = state.docs.filter((d) =>
    (state.docFilter === "all" || d.kind === state.docFilter) &&
    (!q || d.title.toLowerCase().includes(q) || d.path.toLowerCase().includes(q) ||
     (d.blurb || "").toLowerCase().includes(q)));

  const grid = document.getElementById("docGrid");
  if (!list.length) {
    grid.innerHTML = `<div class="empty" style="grid-column:1/-1">${
      state.docs.length ? "nothing matches that filter" : "artifact folder is empty"
    }</div>`;
    return;
  }

  grid.innerHTML = list.map((d) => `
    <div class="doc" data-path="${esc(d.path)}">
      <div class="doc-thumb">${
        d.kind === "image"
          ? `<img src="/api/docs/raw?path=${encodeURIComponent(d.path)}" alt="" loading="lazy" />`
          : (DOC_ICON[d.kind] || DOC_ICON.other)
      }</div>
      <div class="doc-body">
        <div class="doc-title">${esc(d.title)}</div>
        <div class="doc-blurb">${esc(d.blurb || d.name)}</div>
        <div class="doc-foot">
          <span class="tag">${esc(d.ext || d.kind)}</span>
          <span>${kb(d.bytes)}</span>
          <button class="doc-del" data-del="${esc(d.path)}">del</button>
        </div>
      </div>
    </div>`).join("");
}

document.getElementById("docChips").addEventListener("click", (e) => {
  const b = e.target.closest(".chip");
  if (!b) return;
  state.docFilter = b.dataset.kind;
  document.querySelectorAll("#docChips .chip").forEach((c) => c.classList.remove("on"));
  b.classList.add("on");
  renderDocs();
});

document.getElementById("docSearch").addEventListener("input", (e) => {
  state.docQuery = e.target.value.trim();
  renderDocs();
});

document.getElementById("docGrid").addEventListener("click", async (e) => {
  const del = e.target.closest("[data-del]");
  if (del) {
    e.stopPropagation();
    const p = del.dataset.del;
    if (!confirm("Delete " + p + " ?")) return;
    try {
      await getJSON("/api/docs?path=" + encodeURIComponent(p), { method: "DELETE" });
      await loadDocs();
      loadStatus().catch(() => {});
    } catch (err) { alert("Delete failed: " + err.message); }
    return;
  }
  const card = e.target.closest(".doc");
  if (card) openDoc(card.dataset.path);
});

/* --------------------------------------------------------------- modal */

const modal = document.getElementById("modal");

function closeModal() {
  modal.classList.remove("open");
  document.getElementById("modalBody").innerHTML = "";
}
document.getElementById("modalClose").addEventListener("click", closeModal);
modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });
window.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

async function openDoc(path) {
  const d = state.docs.find((x) => x.path === path);
  if (!d) return;
  document.getElementById("modalTitle").textContent = d.title;
  document.getElementById("modalPath").textContent = d.path + " · " + kb(d.bytes);
  const body = document.getElementById("modalBody");
  const url = "/api/docs/raw?path=" + encodeURIComponent(d.path);

  if (d.kind === "image") {
    body.innerHTML = `<img src="${url}" alt="" />`;
  } else if (d.kind === "html") {
    // sandboxed: the server also sends a CSP sandbox header
    body.innerHTML = `<iframe src="${url}" sandbox referrerpolicy="no-referrer"></iframe>`;
  } else if (d.previewable) {
    body.innerHTML = `<pre>loading…</pre>`;
    try {
      const r = await fetch(url);
      body.innerHTML = `<pre>${esc(await r.text())}</pre>`;
    } catch (err) {
      body.innerHTML = `<pre>could not read: ${esc(err.message)}</pre>`;
    }
  } else {
    body.innerHTML = `<pre>no inline preview for .${esc(d.ext)} — <a style="color:#29f5ff" href="${url}">open raw</a></pre>`;
  }
  modal.classList.add("open");
}

/* ------------------------------------------------------------ dreams */

const SEV = {
  high: { label: "act now", col: "#fb7185", cls: "stale" },
  warn: { label: "watch",   col: "#fbbf24", cls: "" },
  good: { label: "healthy", col: "#34d399", cls: "" },
  info: { label: "idea",    col: "#29f5ff", cls: "" },
};

async function loadDreams() {
  const d = await getJSON("/api/dreams");
  const obs = d.observations || [], briefs = d.briefs || [];
  const high = obs.filter((o) => o.severity === "high").length;

  document.getElementById("dreamMeta").textContent =
    `${obs.length} findings · ${high} need action · ${briefs.length} briefs`;
  document.getElementById("briefMeta").textContent = d.dreams_dir || "";

  document.getElementById("obsList").innerHTML = obs.length
    ? obs.map((o) => {
        const s = SEV[o.severity] || SEV.info;
        return `<div class="row" style="align-items:flex-start;border-left:2px solid ${s.col}">
          <span class="tag ${s.cls}" style="color:${s.col};border-color:${s.col}66;margin-top:2px">${s.label}</span>
          <div class="grow">
            <div class="name">${esc(o.title)}</div>
            <div class="desc" style="white-space:normal">${esc(o.detail)}</div>
            ${o.action ? `<div style="font-family:var(--mono);font-size:9.5px;color:${s.col};margin-top:5px;letter-spacing:.08em">→ ${esc(o.action)}</div>` : ""}
          </div>
        </div>`;
      }).join("")
    : '<div class="empty">nothing to report</div>';

  document.getElementById("briefList").innerHTML = briefs.length
    ? briefs.map((b, i) => `
      <div class="row" data-brief="${i}" style="cursor:pointer">
        <span class="tag magenta">brief</span>
        <div class="grow">
          <div class="name">${esc(b.title)}</div>
          <div class="desc">${esc(b.file)} · ${kb(b.bytes)}</div>
        </div>
        <span class="tag">open</span>
      </div>`).join("")
    : `<div class="empty">no briefs yet — run scripts/dream.py</div>`;

  window.__briefs = briefs;
}

document.getElementById("briefList").addEventListener("click", (e) => {
  const row = e.target.closest("[data-brief]");
  if (!row) return;
  const b = (window.__briefs || [])[Number(row.dataset.brief)];
  if (!b) return;
  document.getElementById("modalTitle").textContent = b.title;
  document.getElementById("modalPath").textContent = b.file;
  document.getElementById("modalBody").innerHTML = `<pre>${esc(b.body)}</pre>`;
  modal.classList.add("open");
});

/* --------------------------------------------------- mission control */

const TASK_TONE = { ready: "", running: "lime", done: "lime", failed: "red", blocked: "red" };

async function loadMission() {
  const m = await getJSON("/api/mission");
  const s = m.stats;
  document.getElementById("missionMeta").textContent =
    `${s.tasks} tasks · ${s.runs} runs · ${s.events} events`;

  document.getElementById("missionStats").innerHTML = [
    ["Tasks", fmt(s.tasks), Object.entries(m.by_status).map(([k, v]) => `${v} ${k}`).join(" · "), ""],
    ["Runs", fmt(s.runs), "worker executions", ""],
    ["Events", fmt(s.events), "audit trail", ""],
    ["Failing", fmt(s.failing), s.failing ? "needs attention" : "all clear", s.failing ? "red" : "lime"],
  ].map(([k, v, sub, cls]) =>
    `<div class="card stat"><div class="k">${k}</div><div class="v ${cls}">${v}</div>
     ${sub ? `<div class="sub">${esc(sub)}</div>` : ""}</div>`).join("");

  document.getElementById("missionTasks").innerHTML = m.tasks.length
    ? m.tasks.map((t) => `
      <div class="row">
        <span class="tag ${TASK_TONE[t.status] === "red" ? "stale" : ""}">${esc(t.status || "?")}</span>
        <div class="grow">
          <div class="name">${esc(t.title || t.id)}</div>
          <div class="desc">${esc(t.id)} · ${esc(t.assignee || "—")} · ${esc(t.workspace_kind || "—")}${
            t.last_failure_error ? " · ⚠ " + esc(String(t.last_failure_error).slice(0, 80)) : ""}</div>
        </div>
        ${t.failures ? `<span class="tag stale">${t.failures} fail</span>` : ""}
        <span class="tag magenta">p${esc(t.priority ?? "-")}</span>
      </div>`).join("")
    : '<div class="empty">no kanban tasks</div>';

  document.getElementById("missionRuns").innerHTML = m.runs.length
    ? m.runs.slice(0, 15).map((r) => `
      <div class="row">
        <span class="tag ${r.status === "failed" ? "stale" : ""}">${esc(r.status || "?")}</span>
        <div class="grow">
          <div class="name">run ${esc(String(r.id))} · ${esc(r.task_id || "—")}</div>
          <div class="desc">${esc(r.summary || r.error || r.outcome || "—")}</div>
        </div>
      </div>`).join("")
    : '<div class="empty">no runs yet</div>';
}

/* --------------------------------------------------------- schedule */

async function loadSchedule() {
  const s = await getJSON("/api/schedule");
  document.getElementById("schedMeta").textContent =
    s.has_jobs ? "jobs configured" : "no jobs configured";

  const age = s.heartbeat_age_s;
  document.getElementById("schedStats").innerHTML = [
    ["Jobs", s.has_jobs ? "yes" : "0", s.has_jobs ? "" : "nothing scheduled", s.has_jobs ? "lime" : "red"],
    ["Scheduler", s.scheduler_alive ? "alive" : "stale",
      age === null ? "no heartbeat" : `beat ${age}s ago`, s.scheduler_alive ? "lime" : "red"],
    ["Executions", fmt(s.executions.length), "recorded runs", ""],
  ].map(([k, v, sub, cls]) =>
    `<div class="card stat"><div class="k">${k}</div><div class="v ${cls}">${v}</div>
     ${sub ? `<div class="sub">${esc(sub)}</div>` : ""}</div>`).join("");

  document.getElementById("schedListing").textContent = s.listing || "—";

  document.getElementById("schedExecs").innerHTML = s.executions.length
    ? s.executions.map((e) => `
      <div class="row">
        <span class="tag">${esc(e.status || "?")}</span>
        <div class="grow"><div class="name">${esc(e.job_id || "—")}</div>
        <div class="desc">${esc(e.source || "")}</div></div>
      </div>`).join("")
    : '<div class="empty">no executions recorded</div>';
}

/* --------------------------------------------------------- insights */

async function loadInsights() {
  const i = await getJSON("/api/insights");
  const e = i.efficiency, c = i.curator;
  document.getElementById("insMeta").textContent =
    `curator ${c.enabled || "?"} · ${c.runs || 0} runs · ${c.interval || "?"}`;

  document.getElementById("insStats").innerHTML = [
    ["Cost / session", money(e.cost_per_session), "", "magenta"],
    ["Cache hit", e.cache_hit_pct + "%", "of billable input", e.cache_hit_pct > 70 ? "lime" : ""],
    ["Cache read", fmt(e.cache_read_tokens), "tokens served warm", "lime"],
    ["Tokens / session", fmt(e.tokens_per_session), "", ""],
    ["Messages / session", e.messages_per_session, "", ""],
    ["Tools / session", e.tools_per_session, "", ""],
  ].map(([k, v, sub, cls]) =>
    `<div class="card stat"><div class="k">${k}</div><div class="v ${cls}">${v}</div>
     ${sub ? `<div class="sub">${esc(sub)}</div>` : ""}</div>`).join("");

  document.getElementById("insCurator").textContent = c.raw || "—";
}

/* ------------------------------------------------------------- stack */

async function loadStack() {
  const s = await getJSON("/api/stack");
  document.getElementById("stackMeta").textContent =
    `${esc(s.model || "model unset")} · ${esc(s.provider || "provider unset")}`;

  const block = (title, items, render) => `
    <div class="page-head" style="margin:18px 0 10px"><h1 style="font-size:14px">${title}</h1>
    <span class="meta">${items.length}</span></div>
    ${items.length ? items.map(render).join("") : '<div class="empty">none</div>'}`;

  document.getElementById("stackBody").innerHTML =
    block("Model", [{ m: s.model, p: s.provider }], (x) => `
      <div class="row"><span class="tag magenta">${esc(x.p || "—")}</span>
      <div class="grow"><div class="name">${esc(x.m || "not set")}</div></div></div>`) +
    block("Channels", s.channels, (c) => `
      <div class="row"><span class="tag">${esc(c.platform)}</span>
      <div class="grow"><div class="name">${esc(c.name || "—")}</div>
      <div class="desc">${esc(c.type || "")}</div></div></div>`) +
    block("MCP servers", s.mcp, (m) => `
      <div class="row"><span class="tag${m.enabled ? "" : " stale"}">${m.enabled ? "enabled" : "off"}</span>
      <div class="grow"><div class="name">${esc(m.name)}</div></div></div>`) +
    block("Plugins", s.plugins, (p) => `
      <div class="row"><span class="tag">plugin</span>
      <div class="grow"><div class="name">${esc(p.name)}</div>
      <div class="desc">${p.dashboard ? "ships a dashboard extension" : "CLI/gateway only"}</div></div></div>`) +
    block("Profiles", s.profiles.map((n) => ({ n })), (x) => `
      <div class="row"><span class="tag">profile</span>
      <div class="grow"><div class="name">${esc(x.n)}</div></div></div>`) +
    block("Dashboard themes", s.dashboard_themes.map((n) => ({ n })), (x) => `
      <div class="row"><span class="tag magenta">theme</span>
      <div class="grow"><div class="name">${esc(x.n)}</div></div></div>`);
}

/* ----------------------------------------------------- conversations */

async function loadChats() {
  const { sessions } = await getJSON("/api/activity");
  document.getElementById("chatMeta").textContent = `${sessions.length} sessions · read-only`;
  document.getElementById("chatList").innerHTML = sessions.length
    ? sessions.map((s) => `
      <div class="row" data-sid="${esc(s.id)}" style="cursor:pointer">
        <span class="tag">${esc(s.source || "?")}</span>
        <div class="grow">
          <div class="name">${esc(s.title || s.display_name || s.id)}</div>
          <div class="desc">${fmt(s.message_count)} messages · ${esc(s.model || "—")} · ${esc(s.id)}</div>
        </div>
        <span class="tag magenta">open</span>
      </div>`).join("")
    : '<div class="empty">no sessions</div>';
}

document.getElementById("chatList").addEventListener("click", async (e) => {
  const row = e.target.closest("[data-sid]");
  if (!row) return;
  const sid = row.dataset.sid;
  document.getElementById("modalTitle").textContent = "Transcript";
  document.getElementById("modalPath").textContent = sid;
  const body = document.getElementById("modalBody");
  body.innerHTML = "<pre>loading…</pre>";
  modal.classList.add("open");
  try {
    const { messages } = await getJSON("/api/conversation?id=" + encodeURIComponent(sid));
    body.innerHTML = messages.length
      ? `<div style="padding:14px;max-height:72vh;overflow:auto">` + messages.map((m) => {
          const col = m.role === "user" ? "#a855f7" : m.role === "tool" ? "#34d399" : "#29f5ff";
          const who = m.role === "tool" ? `tool · ${esc(m.tool || "")}` : m.role;
          return `<div style="margin-bottom:12px">
            <div style="font-family:var(--mono);font-size:8.5px;letter-spacing:.2em;text-transform:uppercase;color:${col}">${esc(who)}</div>
            <div style="font-size:12px;line-height:1.55;white-space:pre-wrap;color:rgba(234,242,245,.72)">${esc(m.text.slice(0, 1200))}</div>
          </div>`;
        }).join("") + `</div>`
      : "<pre>no messages</pre>";
  } catch (err) {
    body.innerHTML = `<pre>could not load: ${esc(err.message)}</pre>`;
  }
});

/* --------------------------------------------------------------- graph */

const canvas = document.getElementById("graph");
const ctx = canvas.getContext("2d");
let W = 0, H = 0, DPR = 1;

function resize() {
  DPR = Math.min(window.devicePixelRatio || 1, 2);
  const rect = canvas.getBoundingClientRect();
  W = rect.width; H = rect.height;
  canvas.width = Math.round(W * DPR);
  canvas.height = Math.round(H * DPR);
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
}
window.addEventListener("resize", resize);

function visible() {
  if (state.layout === "full") return state.nodes;
  if (state.layout === "mid")
    return state.nodes.filter((n) => n.kind !== "index" || (n.weight || 0) > 1.3);
  return state.nodes.filter((n) => n.kind === "agent" || n.kind === "hub" || n.kind === "core");
}

function physics(dt) {
  const vis = visible();
  const ids = new Set(vis.map((n) => n.id));
  const repel = state.layout === "full" ? 2600 : 4200;

  for (let i = 0; i < vis.length; i++) {
    const a = vis[i];
    if (a.kind === "agent") { a.x = 0; a.y = 0; a.vx = a.vy = 0; continue; }
    a.vx -= a.x * 0.0016;
    a.vy -= a.y * 0.0016;
    for (let j = i + 1; j < vis.length; j++) {
      const b = vis[j];
      let dx = a.x - b.x, dy = a.y - b.y;
      let d2 = dx * dx + dy * dy;
      if (d2 < 1) { d2 = 1; dx = Math.random() - 0.5; dy = Math.random() - 0.5; }
      if (d2 > 90000) continue;
      const d = Math.sqrt(d2), f = repel / d2;
      const fx = (dx / d) * f, fy = (dy / d) * f;
      a.vx += fx; a.vy += fy;
      if (b.kind !== "agent") { b.vx -= fx; b.vy -= fy; }
    }
  }

  for (const e of state.edges) {
    if (!ids.has(e.s.id) || !ids.has(e.t.id)) continue;
    const dx = e.t.x - e.s.x, dy = e.t.y - e.s.y;
    const d = Math.hypot(dx, dy) || 1;
    const rest = e.s.kind === "agent" || e.t.kind === "agent" ? 170 : 92;
    const f = (d - rest) * 0.0042;
    const fx = (dx / d) * f, fy = (dy / d) * f;
    if (e.s.kind !== "agent") { e.s.vx += fx; e.s.vy += fy; }
    if (e.t.kind !== "agent") { e.t.vx -= fx; e.t.vy -= fy; }
  }

  for (const n of vis) {
    if (n.kind === "agent") continue;
    n.vx *= 0.86; n.vy *= 0.86;
    n.vx = Math.max(-6, Math.min(6, n.vx));
    n.vy = Math.max(-6, Math.min(6, n.vy));
    n.x += n.vx * dt; n.y += n.vy * dt;
  }
}

function toScreen(n) {
  return {
    x: W / 2 + (n.x + state.cam.x) * state.cam.zoom,
    y: H / 2 + (n.y + state.cam.y) * state.cam.zoom,
  };
}

function hexA(hex, a) {
  const n = parseInt(hex.replace("#", ""), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

let t0 = performance.now();
function frame(now) {
  const dt = Math.min((now - t0) / 16.67, 2.5);
  t0 = now;
  if (state.motion === "flow") physics(dt);
  draw(now);
  requestAnimationFrame(frame);
}

function draw(now) {
  ctx.clearRect(0, 0, W, H);
  const vis = visible();
  const ids = new Set(vis.map((n) => n.id));

  if (state.links) {
    ctx.lineWidth = 0.6;
    for (const e of state.edges) {
      if (!ids.has(e.s.id) || !ids.has(e.t.id)) continue;
      const a = toScreen(e.s), b = toScreen(e.t);
      const hot = state.hover && (state.hover.id === e.s.id || state.hover.id === e.t.id);
      ctx.strokeStyle = hot ? "rgba(255,43,209,0.6)" : "rgba(0,240,255,0.11)";
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    }
  }

  ctx.globalCompositeOperation = "lighter";
  for (const n of vis) {
    const p = toScreen(n);
    const k = KIND[n.kind] || KIND.core;
    const pulse = state.motion === "flow" ? 1 + Math.sin(now / 900 + n.phase) * 0.12 : 1;
    const r = n.r * state.cam.zoom * pulse, halo = r * 5.4;
    const g = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, halo);
    g.addColorStop(0, hexA(k.glow, 0.6));
    g.addColorStop(0.32, hexA(k.glow, 0.15));
    g.addColorStop(1, hexA(k.glow, 0));
    ctx.fillStyle = g;
    ctx.beginPath(); ctx.arc(p.x, p.y, halo, 0, Math.PI * 2); ctx.fill();
  }
  ctx.globalCompositeOperation = "source-over";

  for (const n of vis) {
    const p = toScreen(n);
    const k = KIND[n.kind] || KIND.core;
    const pulse = state.motion === "flow" ? 1 + Math.sin(now / 900 + n.phase) * 0.12 : 1;
    const r = n.r * state.cam.zoom * pulse;
    ctx.fillStyle = k.color;
    ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2); ctx.fill();
    if (state.hover && state.hover.id === n.id) {
      ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.arc(p.x, p.y, r + 4, 0, Math.PI * 2); ctx.stroke();
    }
    if (n.kind === "agent" || n.kind === "hub") {
      ctx.fillStyle = "rgba(216,246,255,0.8)";
      ctx.font = '500 10px "JetBrains Mono", monospace';
      ctx.textAlign = "center";
      ctx.fillText(n.label, p.x, p.y + r + 14);
    }
  }

  document.getElementById("graphReadout").textContent =
    `${vis.length} shown · zoom ${state.cam.zoom.toFixed(2)}× · ${state.motion}`;
}

/* ------------------------------------------------------- interactions */

const tip = document.getElementById("tip");

canvas.addEventListener("mousemove", (e) => {
  const rect = canvas.getBoundingClientRect();
  const mx = e.clientX - rect.left, my = e.clientY - rect.top;

  if (state.drag) {
    state.cam.x += (mx - state.drag.x) / state.cam.zoom;
    state.cam.y += (my - state.drag.y) / state.cam.zoom;
    state.drag = { x: mx, y: my };
    return;
  }

  let best = null, bestD = 16;
  for (const n of visible()) {
    const p = toScreen(n);
    const d = Math.hypot(p.x - mx, p.y - my);
    if (d < bestD + n.r) { best = n; bestD = d; }
  }
  state.hover = best;

  if (best) {
    tip.classList.add("show");
    tip.innerHTML = `<span class="t-kind">${best.kind}</span>${esc(best.label)}` +
      (best.detail ? `<div class="t-detail">${esc(best.detail)}</div>` : "");
    const tw = tip.offsetWidth || 200;
    tip.style.left = Math.min(mx + 14, rect.width - tw - 8) + "px";
    tip.style.top = (my + 50) + "px";
  } else {
    tip.classList.remove("show");
  }
});

canvas.addEventListener("mouseleave", () => {
  tip.classList.remove("show"); state.hover = null; state.drag = null;
});
canvas.addEventListener("mousedown", (e) => {
  const rect = canvas.getBoundingClientRect();
  state.drag = { x: e.clientX - rect.left, y: e.clientY - rect.top };
});
window.addEventListener("mouseup", () => { state.drag = null; });
canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  const f = e.deltaY < 0 ? 1.12 : 1 / 1.12;
  state.cam.zoom = Math.max(0.25, Math.min(4, state.cam.zoom * f));
}, { passive: false });

function segment(id, key, apply) {
  document.getElementById(id).addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    document.querySelectorAll(`#${id} button`).forEach((x) => x.classList.remove("on"));
    b.classList.add("on");
    apply(b.dataset[key]);
  });
}
segment("layoutSeg", "layout", (v) => { state.layout = v; });
segment("motionSeg", "motion", (v) => { state.motion = v; });
segment("linkSeg", "links", (v) => { state.links = v === "on"; });

/* ---------------------------------------------------------------- media */

function etaStr(s) {
  if (s === null || s === undefined || s < 0) return "";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (h > 48) return ">2d";
  return h ? `${h}h ${m}m` : `${m}m`;
}

const ACTIVE = new Set(["downloading", "dl-wait", "checking", "stalledDL", "queuedDL", "metaDL"]);

async function loadMedia() {
  const m = await getJSON("/api/media");
  if (m.error) {
    document.getElementById("mediaMeta").textContent = "unavailable: " + m.error;
    return;
  }
  const lib = m.library || {}, dk = m.disk || {}, rq = m.requests || {}, ix = m.indexers || {};

  document.getElementById("mediaMeta").textContent =
    `${m.active || 0} active · ${(m.dl_total_mbps || 0).toFixed(1)} MB/s · ${dk.free_gb ? fmt(Math.round(dk.free_gb)) + " GB free" : "—"}`;

  const cards = [
    ["Downloading", fmt(m.active || 0), `${(m.dl_total_mbps || 0).toFixed(1)} MB/s`, "cyan"],
    ["Movies", fmt(lib.movies), `${fmt(lib.movies_missing)} missing`, lib.movies_missing ? "" : "lime"],
    ["Series", fmt(lib.series), `${fmt(lib.episodes_have)}/${fmt(lib.episodes_total)} eps`, ""],
    ["Requests", fmt(rq.processing || 0), "processing", (rq.processing || 0) > 5 ? "red" : "lime"],
    ["Available", fmt(rq.available || 0), "in Seerr", "lime"],
    ["Indexers", `${fmt(ix.total)}`, `${ix.failing || 0} failing`, ix.failing ? "red" : "lime"],
    ["Disk", dk.free_gb ? `${fmt(Math.round(dk.free_gb))} GB` : "—",
      dk.used_pct ? `${dk.used_pct}% used` : "", (dk.used_pct || 0) > 90 ? "red" : "magenta"],
  ];
  document.getElementById("mediaStats").innerHTML = cards
    .map(([k, v, sub, cls]) =>
      `<div class="card stat"><div class="k">${k}</div>
       <div class="v ${cls}">${v}</div>
       ${sub ? `<div class="sub">${esc(sub)}</div>` : ""}</div>`).join("");

  document.getElementById("mediaSvc").innerHTML = (m.services || [])
    .map((s) => `<span><i style="background:${s.up ? "#34d399" : "#fb7185"}"></i>${esc(s.name)}</span>`)
    .join("");

  const dls = m.downloads || [];
  document.getElementById("dlCount").textContent =
    `${dls.length} torrent${dls.length === 1 ? "" : "s"}`;

  // launcher tiles — every one opens in a new tab
  document.getElementById("mediaLinks").innerHTML = (m.links || [])
    .map((l) => {
      const cls = l.up === true ? "up" : l.up === false ? "down" : "";
      return `<a class="launch-tile ${cls}" href="${esc(l.url)}"
        target="_blank" rel="noopener noreferrer" title="${esc(l.url)}">
        <span class="lt-glyph">${esc(l.glyph)}</span>
        <span class="lt-name">${esc(l.name)}</span>
        <span class="lt-go">↗</span></a>`;
    }).join("");

  // recently imported
  const rec = m.recent || [];
  document.getElementById("recCount").textContent = `${rec.length} items`;
  document.getElementById("recBody").innerHTML = rec.length === 0
    ? `<div class="sub" style="padding:14px 2px">nothing recent</div>`
    : rec.map((r) => `<div class="mini-row">
        <span class="mini-ico">${r.kind === "movie" ? "▣" : "▦"}</span>
        <span class="mini-name">${esc(r.title)}</span>
        <span class="mini-tag">${esc(r.quality || "")}</span>
      </div>`).join("");

  // health warnings
  const hl = m.health || [];
  document.getElementById("healthCount").textContent =
    hl.length === 0 ? "all clear" : `${hl.length} warning${hl.length === 1 ? "" : "s"}`;
  document.getElementById("healthBody").innerHTML = hl.length === 0
    ? `<div class="sub" style="padding:14px 2px">no warnings</div>`
    : hl.map((h) => `<div class="mini-row">
        <span class="mini-ico ${h.type === "error" ? "bad" : "warn"}">●</span>
        <span class="mini-name">${esc(h.message)}</span>
        <span class="mini-tag">${esc(h.service)}</span>
      </div>`).join("");

  // upcoming
  const up = m.upcoming || [];
  document.getElementById("upCount").textContent = `${up.length} episodes`;
  document.getElementById("upBody").innerHTML = up.length === 0
    ? `<div class="sub" style="padding:14px 2px">nothing airing in the next 7 days</div>`
    : up.map((u) => `<div class="mini-row">
        <span class="mini-ico">${u.has ? "✓" : "◻"}</span>
        <span class="mini-name">${esc(u.title)} <span class="dim">${esc(u.ep)}</span> ${esc(u.name)}</span>
        <span class="mini-tag">${esc((u.airs || "").slice(0, 16).replace("T", " "))}</span>
      </div>`).join("");

  document.getElementById("dlBody").innerHTML = dls.length === 0
    ? `<div class="sub" style="padding:14px 2px">nothing downloading</div>`
    : dls.map((d) => {
        const live = ACTIVE.has(d.state);
        return `<div class="dl-row">
        <div class="dl-top">
          <span class="dl-name">${esc(d.name)}</span>
          <span class="dl-tag ${live ? "on" : ""}">${esc(d.state)}</span>
        </div>
        <div class="dl-bar"><i style="width:${Math.max(0.6, d.pct)}%"></i></div>
        <div class="dl-foot">
          <span>${d.pct.toFixed(1)}%</span>
          <span>${d.done_gb.toFixed(2)} / ${d.size_gb.toFixed(2)} GB</span>
          <span>${d.dl_rate ? d.dl_rate.toFixed(2) + " MB/s" : "—"}</span>
          <span>${etaStr(d.eta)}</span>
          <span class="dim">${esc(d.client)}</span>
        </div>
      </div>`;
      }).join("");
}

/* ----------------------------------------------------------------- go */

(async function init() {
  resize();
  requestAnimationFrame(frame);
  try {
    await Promise.all([loadStatus(), loadGraph()]);
    await Promise.all([
      loadDocs(), loadSkills(), loadMemory(), loadActivity(),
      loadMission(), loadSchedule(), loadInsights(), loadStack(), loadChats(),
      loadDreams(), loadMedia(),
    ]);
  } catch (err) {
    document.getElementById("railFoot").textContent = "error: " + err.message;
    console.error(err);
  }
  setInterval(() => { loadStatus().catch(() => {}); loadDocs().catch(() => {}); }, 15000);
  // downloads move: refresh the media panel more often, but only when visible
  setInterval(() => {
    const p = document.querySelector('[data-page="media"]');
    if (p && !p.classList.contains("hidden")) loadMedia().catch(() => {});
  }, 10000);
})();
