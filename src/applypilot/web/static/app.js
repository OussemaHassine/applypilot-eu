/* ApplyPilot UI — no framework, no build step. */
"use strict";

// ── tiny helpers ────────────────────────────────────────────────────────
const $ = (s, el = document) => el.querySelector(s);
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const svg = (tag, attrs = {}) => {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
};
// replaceChildren() renders null as the text "null"; mount() drops null/false children first.
function mount(el, ...kids) {
  // live pages re-render every 1.5 s: keep the page where the user scrolled to
  const y = window.scrollY;
  el.replaceChildren(...kids.flat(Infinity).filter((k) => k != null && k !== false));
  if (window.scrollY !== y) window.scrollTo(0, y);
  requestAnimationFrame(() => { if (window.scrollY !== y) window.scrollTo(0, y); });
}
// log boxes: follow the end unless the user scrolled up inside them
const SCROLL_MEMO = new Map();
function keepScroll(pre, key) {
  pre.addEventListener("scroll", () => SCROLL_MEMO.set(key, { top: pre.scrollTop, atBottom: pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 12 }));
  requestAnimationFrame(() => { const m = SCROLL_MEMO.get(key); pre.scrollTop = (!m || m.atBottom) ? pre.scrollHeight : m.top; });
  return pre;
}
async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
}
let toastTimer;
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast on" + (err ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = "toast"), err ? 5000 : 2400);
}

// ── Watch browsers (visible windows vs headless), one click, applies from the next job ──
let WATCH = null;
async function loadWatch() {
  try { WATCH = (await api("/api/settings")).run.HEADLESS !== "1"; } catch (e) { /* keep last value */ }
}
async function setWatch(on) {
  WATCH = on;
  try {
    await api("/api/settings", { method: "PUT", body: { run: { HEADLESS: on ? "0" : "1" } } });
    let touched = 0;
    try { touched = (await api("/api/watch", { method: "POST", body: { watch: on } })).windows_touched || 0; } catch (e) { /* older server */ }
    toast(on ? "Browsers get a window from the next job on" + (touched ? "; hidden windows shown" : "")
             : "Browsers run hidden from the next job on" + (touched ? "; open windows hidden now" : ""));
  } catch (e) { toast(e.message, true); }
}
const OPEN_DETAILS = new Set();
const OPEN_LOGS = new Map();   // finished-run id -> log text shown under its row
function stickyDetails(key, summary, body) {
  // the Runs page re-renders every 1.5 s; remember which <details> the user opened
  const d = h("details", {}, h("summary", {}, summary), body);
  d.open = OPEN_DETAILS.has(key);
  d.addEventListener("toggle", () => { if (d.open) OPEN_DETAILS.add(key); else OPEN_DETAILS.delete(key); });
  return d;
}
function watchSwitch() {
  const cb = h("input", { type: "checkbox", class: "switch", onchange: (e) => setWatch(e.target.checked) });
  cb.checked = WATCH === true;
  return h("label", { class: "check small", style: "white-space:nowrap",
    title: "On: every job opens a visible Chrome window you can watch. Off: browsers run headless, nothing pops up. Takes effect at the next job; windows already open are hidden right away." },
    cb, "Watch browsers");
}
const fmtNum = (n) => (n == null ? "–" : Number(n).toLocaleString());
function ago(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const s = (Date.now() - d.getTime()) / 1000;
  if (s < 60) return "now";
  if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h";
  return Math.floor(s / 86400) + "d";
}
function when(iso, withTime = true) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" }) +
    (withTime ? " " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : "");
}
const human = (k) => k.replace(/^_+/, "").replace(/_/g, " ").replace(/\b\w/, (c) => c.toUpperCase());

const ERR_LABEL = {
  duplicate_application: "Duplicate (already applied)", login_issue: "Account sign-in", captcha: "CAPTCHA",
  expired: "Posting expired", not_eligible_work_auth: "Work rights", not_eligible_location: "Location",
  email_only_application: "Email-only", sso_required: "Single sign-on only", unsafe_verification: "Video / ID check",
  "missing info": "Missing info from you", no_result_line: "No result (agent stopped)",
};
const FAIL_HELP = {
  duplicate_application: "The same role at this employer was already applied to through another posting, so this copy was skipped on purpose.",
  login_issue: "The employer's career site has an account under your email with a password the agent did not have. Password resets are now allowed, so a retry can get through.",
  captcha: "An image CAPTCHA blocked the form. A CapSolver key in Settings would let the agent solve these.",
  expired: "The posting was closed when the agent got there.",
  not_eligible_work_auth: "The posting requires existing work rights the profile does not claim (usually the UK).",
  not_eligible_location: "On-site only, in a place outside your accepted locations, or the employer restricts candidates to local residents.",
  email_only_application: "Applications only by email; the agent never sends email on your behalf.",
  sso_required: "The site only offers Google or Microsoft sign-in, which the agent refuses by design.",
  unsafe_verification: "The site asked for a recorded video interview, ID or selfie check. Finish it yourself if the role interests you.",
  no_result_line: "The agent stopped without a result, most likely because the Claude usage limit was reached. Re-queue to retry.",
  "skipped by you": "You skipped this one from the dashboard.",
};
const failHelp = (e) => (e || "").startsWith("missing_required_")
  ? `The form required "${e.slice(17).replace(/_/g, " ")}", which is not in your profile. Add it under Profile → Extra facts, then re-queue.`
  : FAIL_HELP[e] || "";
const errLabel = (e) => ERR_LABEL[e] || (e && e.startsWith("missing_required_") ? "Needs: " + e.slice(17).replace(/_/g, " ") : human(e || "unknown"));

function statusChip(status, error) {
  if (status === "applied") return h("span", { class: "chip good" }, "Applied");
  if (status === "in_progress") return h("span", { class: "chip info" }, "Applying now");
  if (status === "failed" && (error || "").startsWith("missing_required")) return h("span", { class: "chip warn" }, "Needs info");
  if (status === "failed" && error === "duplicate_application") return h("span", { class: "chip" }, "Duplicate");
  if (status === "failed") return h("span", { class: "chip bad" }, "Not submitted");
  return h("span", { class: "chip plain" }, "Queued");
}
const CAT = {
  interview: ["Interview", "good"], assessment: ["Test / assessment", "good"], offer: ["Offer", "good"],
  info_request: ["Action needed", "warn"], recruiter_outreach: ["Recruiter", "info"], rejection: ["Rejected", "bad"],
  confirmation: ["Application received", ""], job_alert: ["Job alert", ""], other: ["Other", ""],
};
const catChip = (c) => h("span", { class: "chip " + ((CAT[c] || [])[1] || "") }, (CAT[c] || [human(c || "other")])[0]);

// ── charts ──────────────────────────────────────────────────────────────
const tip = h("div", { class: "tip" });
document.body.append(tip);
function showTip(e, text) { tip.textContent = text; tip.style.opacity = 1; moveTip(e); }
function moveTip(e) { tip.style.left = e.clientX + 12 + "px"; tip.style.top = e.clientY - 34 + "px"; }
function hideTip() { tip.style.opacity = 0; }

function columnChart(data, { height = 180 } = {}) {
  // data: [{label, value, tip}]
  const wrap = h("div");
  requestAnimationFrame(() => {
    const W = Math.max(320, wrap.clientWidth), H = height, padL = 28, padB = 22, padT = 8;
    const max = Math.max(1, ...data.map((d) => d.value));
    const step = Math.pow(10, Math.floor(Math.log10(max)));
    const top = Math.ceil(max / step) * step;
    const s = svg("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img" });
    const plotW = W - padL, plotH = H - padB - padT;
    [0, 0.5, 1].forEach((f) => {
      const y = padT + plotH * (1 - f);
      s.append(svg("line", { class: f === 0 ? "axis" : "grid", x1: padL, x2: W, y1: y, y2: y }));
      const t = svg("text", { x: padL - 6, y: y + 4, "text-anchor": "end" });
      t.textContent = Math.round(top * f);
      s.append(t);
    });
    const bw = plotW / data.length, barW = Math.max(4, Math.min(28, bw - 6));
    data.forEach((d, i) => {
      const x = padL + i * bw + (bw - barW) / 2;
      const bh = d.value ? Math.max(2, (d.value / top) * plotH) : 2;
      const y = padT + plotH - bh;
      const g = svg("g");
      const r = Math.min(4, barW / 2, bh);
      g.append(svg("path", {
        class: "bar" + (d.value ? "" : " zero"),
        d: `M${x},${padT + plotH} V${y + r} Q${x},${y} ${x + r},${y} H${x + barW - r} Q${x + barW},${y} ${x + barW},${y + r} V${padT + plotH} Z`,
      }));
      const hit = svg("rect", { class: "hit", x: padL + i * bw, y: padT, width: bw, height: plotH + 2 });
      hit.addEventListener("mouseenter", (e) => showTip(e, d.tip || `${d.label}: ${d.value}`));
      hit.addEventListener("mousemove", moveTip);
      hit.addEventListener("mouseleave", hideTip);
      g.append(hit);
      s.append(g);
      if (data.length <= 16 && (i % 2 === 0 || data.length <= 8)) {
        const t = svg("text", { x: x + barW / 2, y: H - 6, "text-anchor": "middle" });
        t.textContent = d.label;
        s.append(t);
      }
    });
    wrap.append(s);
  });
  return wrap;
}

function hbars(items, { ramp = false, fmt = fmtNum } = {}) {
  const max = Math.max(1, ...items.map((i) => i.value));
  const shades = ["--ramp-6", "--ramp-5", "--ramp-4", "--ramp-3", "--ramp-2", "--ramp-1"];
  return h("div", { class: "hbars" + (ramp ? " funnel" : "") }, items.map((it, i) =>
    h("div", { class: "hbar", title: it.tip || "" },
      h("span", { class: "small", title: it.label }, it.label),
      h("div", { class: "track" }, h("div", { class: "fill", style: `width:${(it.value / max) * 100}%;` + (ramp ? `background:var(${shades[Math.min(i, shades.length - 1)]})` : "") })),
      h("span", { class: "n" }, fmt(it.value)))));
}

// ── router ──────────────────────────────────────────────────────────────
const routes = {};
let pageTimers = [];
function every(ms, fn) { pageTimers.push(setInterval(fn, ms)); }
function clearTimers() { pageTimers.forEach(clearInterval); pageTimers = []; }
async function navigate() {
  clearTimers();
  closeDrawer();
  const name = (location.hash.replace(/^#\/?/, "").split("?")[0] || "overview");
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === name));
  const view = $("#view");
  mount(view, h("div", { class: "empty" }, "Loading…"));
  try {
    await (routes[name] || routes.overview)(view);
  } catch (e) {
    mount(view, h("div", { class: "callout" }, "Could not load this page: " + e.message));
  }
  view.focus({ preventScroll: true });
}
window.addEventListener("hashchange", navigate);

// sidebar live status
async function pollSide() {
  try {
    const l = await api("/api/live");
    const el = $("#side-status");
    const running = l.runs.length || l.external.length || l.in_progress.length;
    el.classList.toggle("running", !!running);
    let txt;
    if (l.runs.length) txt = "Running: " + l.runs.map((r) => r.label).join(", ");
    else if (l.external.length) txt = "Running outside the UI";
    else txt = "Idle";
    if (l.in_progress.length) txt += ` · ${l.in_progress.length} applying`;
    const next = l.next_round ? "Next round " + when(l.next_round) : "No round scheduled";
    mount(el, h("span", { class: "dot" }), h("span", {}, txt, h("br"), h("span", { class: "muted" }, next)));
  } catch (_) {}
}
setInterval(pollSide, 5000);

// ── drawer (job detail) ─────────────────────────────────────────────────
function closeDrawer() { const d = $("#drawer"); d.classList.remove("open"); d.setAttribute("aria-hidden", "true"); $("#backdrop").classList.remove("on"); }
document.addEventListener("DOMContentLoaded", () => $("#backdrop").addEventListener("click", closeDrawer));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
async function openJob(url) {
  const d = $("#drawer");
  mount(d, h("div", { class: "empty" }, "Loading…"));
  d.classList.add("open");
  d.setAttribute("aria-hidden", "false");
  $("#backdrop").classList.add("on");
  const j = await api("/api/job?url=" + encodeURIComponent(url));
  mount(d, 
    h("button", { class: "btn sm close", onclick: closeDrawer }, "Close"),
    h("p", { class: "muted small" }, [j.country, j.site].filter(Boolean).join(" · ")),
    h("h1", {}, j.title),
    h("p", { class: "sub" }, j.location || ""),
    h("div", { class: "row", style: "margin:14px 0 18px" },
      statusChip(j.status, j.error),
      j.fit_score != null && h("span", { class: "chip plain" }, `Fit ${j.fit_score}/10`),
      h("span", { class: "spacer" }),
      h("a", { class: "btn sm", href: j.application_url || j.url, target: "_blank", rel: "noopener" }, "Open posting"),
      j.cv_pdf && h("a", { class: "btn sm", href: j.cv_pdf, target: "_blank" }, "CV (PDF)"),
      j.cl_pdf && h("a", { class: "btn sm", href: j.cl_pdf, target: "_blank" }, "Cover letter (PDF)")),
    h("dl", { class: "kv" },
      h("dt", {}, "Applied"), h("dd", {}, j.applied_at ? when(j.applied_at) : "–"),
      j.error && [h("dt", {}, "Outcome"), h("dd", {}, errLabel(j.error))],
      h("dt", {}, "Why it matched"), h("dd", {}, (j.score_reasoning || "–").split("\n").pop())),
    j.error && failHelp(j.error) ? h("div", { class: "callout" + ((j.error || "").startsWith("missing_required") ? "" : " info") }, failHelp(j.error)) : null,
    h("div", { class: "actions" }, h("span", { class: "lbl" }, "Actions"),
      j.status !== "applied" && h("button", { class: "btn sm", onclick: () => jobAction(url, "requeue", "Back in the queue") }, "Re-queue"),
      j.status !== "applied" && h("button", { class: "btn sm", onclick: () => jobAction(url, "skip", "Skipped") }, "Skip this job"),
      j.status !== "applied" && h("button", { class: "btn sm", onclick: () => confirm("Mark as applied? Use this if you applied by hand.") && jobAction(url, "mark_applied", "Marked as applied") }, "I applied by hand"),
      h("button", { class: "btn sm", onclick: () => confirm("Throw away this CV and cover letter so they are rebuilt on the next round?") && jobAction(url, "retailor", "Will be rebuilt on the next round") }, "Rebuild CV next round")),
    j.emails.length ? h("div", { class: "section" }, h("h2", {}, "Emails from this employer"),
      j.emails.map((m) => h("div", { class: "row", style: "padding:6px 0" }, catChip(m.category), h("span", {}, m.summary), h("span", { class: "muted small" }, when(m.received_at))))) : null,
    h("div", { class: "section" },
      j.agent_notes && h("details", { open: true }, h("summary", {}, "What the agent did"), h("pre", { class: "log tall" }, j.agent_notes)),
      j.cv_text && h("details", {}, h("summary", {}, "Tailored CV (text)"), h("pre", { class: "log tall" }, j.cv_text)),
      j.cover_text && h("details", {}, h("summary", {}, "Cover letter"), h("pre", { class: "log" }, j.cover_text)),
      h("details", {}, h("summary", {}, "Job description"), h("pre", { class: "log tall" }, j.description || "–"))));
}

async function jobAction(url, action, msg) {
  try { await api("/api/job/action", { method: "POST", body: { url, action } }); toast(msg); closeDrawer(); navigate(); }
  catch (e) { toast(e.message, true); }
}

// ── Overview ────────────────────────────────────────────────────────────
routes.overview = async (view) => {
  await loadWatch();
  const render = async () => {
    const o = await api("/api/overview");
    const c = o.counts, r = o.responses.by || {};
    const actionNeeded = r.info_request || 0, good = (r.interview || 0) + (r.assessment || 0) + (r.offer || 0);
    const live = o.live;
    const liveCard = (live.runs.length || live.in_progress.length || live.external.length)
      ? h("div", { class: "card section" }, h("h2", {}, "Running now"),
          h("div", { class: "live" },
            live.runs.map((run) => h("div", { class: "live-run" }, h("div", { class: "row", style: "margin-bottom:6px" }, watchSwitch(), h("span", { class: "spacer" }), h("a", { class: "btn sm", href: "#/runs" }, "Open full view")), livePanel(run, run.log, live.workers, live.in_progress))),
            live.external.map((x) => h("div", { class: "live-run small" }, "Started outside the UI: ", h("span", { class: "mono" }, x))),
            live.in_progress.length ? h("div", { class: "small" }, "Applying to: ", live.in_progress.map((j) => j.title).join(" · ")) : null))
      : null;

    const todo = o.todo || { emails: [], forms: [], auto: [] };
    const FU = { verify_link: "Verify link", complete_application: "Finish application", consent: "Give consent" };
    const autoCard = (todo.auto || []).length ? h("div", { class: "card section" }, h("div", { class: "row", style: "margin-bottom:10px" }, h("h2", { style: "margin:0" }, "ApplyPilot is handling these"),
        h("span", { class: "spacer" }), (todo.auto.some((a) => a.followup_status === "pending") ? h("button", { class: "btn sm primary", onclick: () => startRun("followups") }, "Handle now") : null)),
      h("ul", { class: "feed" }, todo.auto.map((a) => h("li", {}, h("span", { class: "t" }, ago(a.received_at)),
        h("div", { class: "what" }, h("div", { class: "row", style: "gap:6px" }, h("span", { class: "chip " + (a.followup_status === "done" ? "good" : "info") }, a.followup_status === "done" ? "Done" : "Pending"), h("span", { class: "small muted" }, FU[a.followup] || a.followup)),
          h("div", { class: "t-title", style: "margin-top:3px" }, a.company || a.subject), h("div", { class: "t-sub" }, a.followup_note || a.summary || "")))))) : null;
    const todoCard = (todo.emails.length || todo.forms.length) ? h("div", { class: "card section" }, h("h2", {}, "Needs you"),
      h("div", { class: "todo" },
        h("div", {}, h("h3", {}, "Emails asking for something"), todo.emails.length ? h("ul", {}, todo.emails.map((e) => h("li", {},
          h("div", {}, h("div", { class: "t-title" }, e.company || e.subject), h("div", { class: "t-sub" }, (e.followup_status === "needs_human" || e.followup_status === "failed") ? "ApplyPilot tried: " + (e.followup_note || "") : (e.action || e.summary))),
          h("div", { class: "row" }, h("a", { class: "btn sm", href: e.gmail, target: "_blank", rel: "noopener" }, "Open"),
            h("button", { class: "btn sm ghost", title: "Mark as handled", onclick: async () => { await api("/api/responses/handled", { method: "POST", body: { message_id: e.message_id, handled: true } }); render(); } }, "Done"))))) : h("p", { class: "muted small" }, "Nothing pending.")),
        h("div", {}, h("h3", {}, "Forms waiting for a fact"), todo.forms.length ? h("ul", {}, todo.forms.map((f) => h("li", {},
          h("div", {}, h("a", { href: "#", class: "t-title", onclick: (ev) => { ev.preventDefault(); openJob(f.url); } }, f.title), h("div", { class: "t-sub" }, "Needs: " + f.missing.replace(/_/g, " ") + " · " + (f.location || ""))),
          h("a", { class: "btn sm", href: "#/profile" }, "Add in profile")))) : h("p", { class: "muted small" }, "Nothing parked."),
          todo.forms.length ? h("div", { style: "margin-top:10px" }, h("button", { class: "btn sm", onclick: async () => { const r = await api("/api/jobs/requeue_parked", { method: "POST" }); toast(`${r.requeued} re-queued`); render(); } }, "Re-queue all (after adding the facts)")) : null))) : null;

    mount(view, 
      h("div", { class: "head" },
        h("div", {}, h("h1", {}, "Overview"), h("p", { class: "sub" }, `${fmtNum(c.total)} postings found · ${fmtNum(c.matches)} matched your profile · ${fmtNum(c.queued)} ready to send`)),
        h("div", { class: "row" }, h("a", { class: "btn", href: "#/responses" }, "Responses"), h("button", { class: "btn primary", onclick: () => startRun("round") }, "Run a round now"))),
      h("div", { class: "kpis" },
        h("div", { class: "kpi hero" }, h("div", { class: "l" }, "Applications sent"), h("div", { class: "v" }, fmtNum(c.applied)), h("div", { class: "d" }, `${fmtNum(c.applied_24h)} in the last 24 h`)),
        h("div", { class: "kpi" }, h("div", { class: "l" }, "Companies that replied"), h("div", { class: "v" }, fmtNum(o.responses.replied_companies || 0)), h("div", { class: "d" }, c.applied ? Math.round(((o.responses.replied_companies || 0) / c.applied) * 100) + "% response rate" : "")),
        h("div", { class: "kpi" }, h("div", { class: "l" }, "Interviews & tests"), h("div", { class: "v" }, fmtNum(good)), h("div", { class: "d" }, `${fmtNum(r.offer || 0)} offers`)),
        h("div", { class: "kpi" }, h("div", { class: "l" }, "Needs your action"), h("div", { class: "v" }, fmtNum(actionNeeded + (c.parked || 0))), h("div", { class: "d" }, `${actionNeeded} emails · ${c.parked || 0} forms`)),
        h("div", { class: "kpi" }, h("div", { class: "l" }, "Rejections"), h("div", { class: "v" }, fmtNum(r.rejection || 0)), h("div", { class: "d" }, "from email")),
        h("div", { class: "kpi" }, h("div", { class: "l" }, "Queued"), h("div", { class: "v" }, fmtNum(c.queued)), h("div", { class: "d" }, "CV ready, not sent"))),
      liveCard,
      todoCard,
      autoCard,
      h("div", { class: "grid g-main section" },
        h("div", { class: "card" }, h("h2", {}, "Applications per day, last 14 days"),
          columnChart(o.per_day.map((d) => ({ label: new Date(d.day).toLocaleDateString(undefined, { day: "numeric" }), value: d.n, tip: `${new Date(d.day).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })}: ${d.n} sent` })))),
        h("div", { class: "card" }, h("h2", {}, "From posting to application"),
          hbars([
            { label: "Found", value: c.total }, { label: "With description", value: c.described },
            { label: "Scored", value: c.scored }, { label: "Good match (7+)", value: c.matches },
            { label: "CV tailored", value: c.tailored }, { label: "Sent", value: c.applied },
          ], { ramp: true }))),
      h("div", { class: "grid g3 section" },
        h("div", { class: "card" }, h("h2", {}, "Where you applied"), hbars(o.countries.map((x) => ({ label: x.name, value: x.n })))),
        h("div", { class: "card" }, h("h2", {}, "Why applications were not sent"),
          o.failures.length ? hbars(o.failures.map((x) => ({ label: errLabel(x.reason), value: x.n }))) : h("p", { class: "muted" }, "None")),
        h("div", { class: "card" }, h("h2", {}, "Recent activity"),
          h("ul", { class: "feed" }, o.activity.slice(0, 7).map((e) => h("li", {},
            h("span", { class: "t" }, ago(e.at)),
            h("div", { class: "what" },
              h("div", { class: "row", style: "gap:6px" }, e.type.startsWith("email:") ? catChip(e.type.slice(6)) : statusChip(e.type, e.detail)),
              h("div", { class: "t-title", style: "margin-top:3px", title: e.title }, e.url ? h("a", { href: "#", onclick: (ev) => { ev.preventDefault(); openJob(e.url); } }, e.title) : e.title),
              h("div", { class: "t-sub" }, e.sub || ""))))))));
  };
  await render();
  every(10000, () => render().catch(() => {}));
};

async function startRun(kind) {
  try {
    const r = await api("/api/runs/" + kind, { method: "POST" });
    toast(r.label + " started");
    pollSide();
    if (!location.hash.startsWith("#/runs")) location.hash = "#/runs";
    else navigate();
  } catch (e) { toast(e.message, true); }
}

// ── Applications ────────────────────────────────────────────────────────
routes.applications = async (view) => {
  const state = { status: "applied", q: "", country: "" };
  const tabs = [["applied", "Sent"], ["queued", "Ready to send"], ["parked", "Needs info"], ["failed", "Not sent"], ["matches", "All matches"]];
  const list = h("div");
  const counts = {};
  const tabBar = h("div", { class: "tabs" });
  const drawTabs = () => mount(tabBar, ...tabs.map(([k, l]) =>
    h("button", { class: state.status === k ? "on" : "", onclick: () => { state.status = k; drawTabs(); load(); } }, l, counts[k] != null ? h("span", { class: "count" }, counts[k]) : null)));
  const search = h("input", { type: "search", placeholder: "Search role, place or text…", style: "max-width:320px", oninput: debounce((e) => { state.q = e.target.value; load(); }, 300) });
  const banner = h("div");
  const exportBtn = h("a", { class: "btn sm", href: "/api/applications.csv?status=applied", download: "" }, "Export sent (CSV)");
  const countrySel = h("select", { style: "max-width:200px", onchange: (e) => { state.country = e.target.value; load(); } }, h("option", { value: "" }, "All countries"));

  async function load() {
    const d = await api(`/api/applications?status=${state.status}&q=${encodeURIComponent(state.q)}&country=${encodeURIComponent(state.country)}&limit=500`);
    counts[state.status] = d.total;
    drawTabs();
    exportBtn.href = `/api/applications.csv?status=${state.status}`;
    exportBtn.textContent = { applied: "Export sent (CSV)", queued: "Export queue (CSV)", parked: "Export (CSV)", failed: "Export (CSV)", matches: "Export matches (CSV)" }[state.status];
    mount(banner, state.status === "parked" && d.total ? h("div", { class: "callout", style: "margin-bottom:14px" },
      "These forms asked for a fact that is not in your profile. Add it under Profile → Extra facts (or the matching field), then ",
      h("button", { class: "btn sm", style: "margin-left:6px", onclick: async () => { const r = await api("/api/jobs/requeue_parked", { method: "POST" }); toast(`${r.requeued} re-queued`); load(); } }, "Re-queue all")) : null);
    if (!state.q && !state.country && countrySel.options.length === 1) {
      [...new Set(d.items.map((i) => i.country))].sort().forEach((c) => countrySel.append(h("option", { value: c }, c)));
    }
    mount(list, d.items.length ? h("div", { class: "card flush" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "When"), h("th", {}, "Role"), h("th", {}, "Where"), h("th", { class: "num" }, "Fit"), h("th", {}, "Status"), h("th", {}, ""))),
      h("tbody", {}, d.items.map((it) => h("tr", { class: "click", onclick: () => openJob(it.url) },
        h("td", { class: "muted small num", style: "text-align:left;white-space:nowrap" }, when(it.applied_at || it.last_attempted_at, false) || "–"),
        h("td", {}, h("div", { class: "t-title" }, it.title), h("div", { class: "t-sub" }, it.site)),
        h("td", {}, h("div", {}, it.country), h("div", { class: "t-sub" }, it.location || "")),
        h("td", { class: "num" }, h("span", { class: "score" }, it.fit_score ?? "–")),
        h("td", {}, statusChip(it.apply_status, it.apply_error), it.apply_error && it.apply_error !== "duplicate_application" ? h("div", { class: "t-sub", style: "margin-top:4px" }, errLabel(it.apply_error)) : null),
        h("td", { class: "small muted" }, [it.has_cv ? "CV" : "", it.has_cl ? "Letter" : ""].filter(Boolean).join(" · "))))))) : h("div", { class: "card empty" }, "Nothing here."));
  }
  mount(view, 
    h("div", { class: "head" }, h("div", {}, h("h1", {}, "Applications"), h("p", { class: "sub" }, "Click a row for the tailored CV, cover letter, emails and what the agent did on the form."))),
    tabBar, h("div", { class: "row", style: "margin-bottom:14px" }, search, countrySel, h("span", { class: "spacer" }), exportBtn), banner, list);
  drawTabs();
  await load();
  // fill counts for the other tabs quietly
  for (const [k] of tabs) if (counts[k] == null) api(`/api/applications?status=${k}&limit=1`).then((d) => { counts[k] = d.total; drawTabs(); }).catch(() => {});
};
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

// ── Responses ───────────────────────────────────────────────────────────
routes.responses = async (view) => {
  let cat = "", showHandled = false;
  const order = ["", "info_request", "interview", "assessment", "offer", "recruiter_outreach", "rejection", "confirmation", "job_alert"];
  const listEl = h("div");
  const tabBar = h("div", { class: "tabs" });
  async function load() {
    const d = await api(`/api/responses?category=${cat}&include_handled=${showHandled ? 1 : 0}`);
    const total = Object.entries(d.counts).filter(([k]) => !["confirmation", "job_alert", "other"].includes(k)).reduce((a, [, v]) => a + v, 0);
    mount(tabBar, ...order.map((k) => h("button", { class: cat === k ? "on" : "", onclick: () => { cat = k; load(); } },
      k ? (CAT[k] || [k])[0] : "Real replies", h("span", { class: "count" }, k ? (d.counts[k] || 0) : total))));
    let items = d.items;
    if (!cat) items = items.filter((i) => !["confirmation", "job_alert", "other"].includes(i.category));
    mount(listEl, items.length ? h("div", { class: "card flush" }, h("table", {}, h("tbody", {}, items.map((m) => h("tr", {},
      h("td", { style: "width:150px" }, catChip(m.category), m.followup && ["verify_link", "complete_application", "consent"].includes(m.followup) ? h("div", { style: "margin-top:6px" }, h("span", { class: "chip " + (m.followup_status === "done" ? "good" : m.followup_status === "pending" ? "info" : "warn") }, "Auto: " + (m.followup_status || "pending"))) : null, h("div", { class: "t-sub", style: "margin-top:6px" }, when(m.received_at))),
      h("td", {}, h("div", { class: "t-title" }, m.company || m.sender), m.role ? h("div", { class: "t-sub" }, m.role) : null,
        h("div", { style: "margin-top:4px" }, m.summary), m.followup_note ? h("div", { class: "small muted", style: "margin-top:4px" }, "ApplyPilot: " + m.followup_note) : (m.action ? h("div", { class: "small", style: "margin-top:4px" }, h("strong", {}, "To do: "), m.action) : null)),
      h("td", { style: "width:150px;text-align:right" }, h("div", { class: "row", style: "justify-content:flex-end" }, h("a", { class: "btn sm", href: m.gmail, target: "_blank", rel: "noopener" }, "Open in Gmail"),
          h("button", { class: "btn sm ghost", title: m.handled ? "Mark as not handled" : "Mark as handled", onclick: async () => { await api("/api/responses/handled", { method: "POST", body: { message_id: m.message_id, handled: !m.handled } }); load(); } }, m.handled ? "Undo" : "Done")),
        m.job_url ? h("div", { style: "margin-top:6px" }, h("a", { href: "#", class: "small", onclick: (e) => { e.preventDefault(); openJob(m.job_url); } }, "Application")) : null))))))
      : h("div", { class: "card empty" }, "No emails in this category yet."));
  }
  mount(view, 
    h("div", { class: "head" },
      h("div", {}, h("h1", {}, "Responses"), h("p", { class: "sub" }, "Job emails from your Gmail, sorted by the AI and labelled in Gmail under ApplyPilot/…. Confirmations and job alerts are kept out of the way.")),
      h("div", { class: "row" }, h("label", { class: "check small" }, h("input", { type: "checkbox", onchange: (e) => { showHandled = e.target.checked; load(); } }), "Show handled"),
        h("button", { class: "btn primary", onclick: () => startRun("inbox") }, "Check Gmail now"))),
    tabBar, listEl);
  await load();
  every(30000, () => load().catch(() => {}));
};

// ── live run panel ─────────────────────────────────────────────────────
const STAGE_NAMES = { discover: "Finding jobs", enrich: "Fetching job descriptions", pipeline: "Scoring & tailoring", score: "Scoring", tailor: "Tailoring CVs", cover: "Cover letters", pdf: "PDFs", apply: "Applying", inbox: "Checking Gmail", followups: "Email follow-ups" };
const NOISE_RE = /HTTP Request|already have PDFs|no job data returned|URL resolution:|No pending jobs/;
function parseLive(logText) {
  const lines = (logText || "").split("\n").filter(Boolean);
  let stage = null, progress = null, last = null;
  for (const l of lines) {
    const m = l.match(/^--- (\w+)/) || l.match(/\bFull crawl\b/) && ["", "discover"];
    if (m) stage = m[1];
    if (/Scoring \d+ jobs|\] score=/.test(l)) stage = stage === "pipeline" || !stage ? "score" : stage;
    if (/Tailoring resumes|\[APPROVED\]|\[FAILED_VALIDATION\]/.test(l)) stage = "tailor";
    if (/Generating cover letters|cover letters done/i.test(l)) stage = "cover";
    if (/Launching apply pipeline|Chrome started/.test(l)) stage = "apply";
    const p = l.match(/Progress: (\d+)\/(\d+)/) || l.match(/\[(\d+)\/(\d+)\] /) || l.match(/(\d+)\/(\d+) \[(APPROVED|OK|FAILED[_A-Z]*)\]/);
    if (p) progress = { done: +p[1], total: +p[2] };
    if (!NOISE_RE.test(l)) last = l;
  }
  return { stage, progress, last: last ? last.replace(/^\d\d:\d\d:\d\d - \w+ - /, "") : "" };
}
function livePanel(run, logText, workers, inProgress) {
  const info = parseLive(logText);
  const started = run ? new Date(run.started) : null;
  const elapsed = started ? Math.round((Date.now() - started.getTime()) / 60000) : null;
  const pre = h("pre", { class: "log tall", style: "max-height:420px" }, (logText || "Starting…").split("\n").filter((l) => !NOISE_RE.test(l)).join("\n"));
  keepScroll(pre, "runlog:" + (run ? run.id : "none"));
  return h("div", {},
    h("div", { class: "row", style: "margin-bottom:8px;gap:12px" },
      h("span", { class: "chip info" }, "Running"),
      run && h("strong", {}, run.label),
      info.stage && h("span", { class: "chip plain" }, "Stage: " + (STAGE_NAMES[info.stage] || info.stage)),
      info.progress && h("span", { class: "muted small num" }, `${info.progress.done} / ${info.progress.total}`),
      elapsed != null && h("span", { class: "muted small" }, `${elapsed} min`)),
    info.last && h("div", { class: "small", style: "margin-bottom:8px;color:var(--ink-2)" }, "Latest: ", h("span", { class: "mono" }, info.last.slice(0, 180))),
    inProgress && inProgress.length ? h("div", { class: "small", style: "margin-bottom:8px" }, "Browser agents on: ", inProgress.map((j) => h("span", { class: "chip plain" }, j.title.slice(0, 40)))) : null,
    workers && workers.length ? h("div", { class: "grid g2", style: "margin-bottom:10px" }, workers.map((w) => h("div", { class: "live-run" }, h("div", { class: "small muted", style: "margin-bottom:4px" }, w.worker.replace("worker-", "Browser agent ") + (w.job ? " · " + w.job.slice(0, 48) : "") + (w.age > 90 ? ` · no new note for ${Math.round(w.age / 60)} min` : "")), keepScroll(h("pre", { class: "log", style: "margin:0;max-height:160px;font-size:12px" }, w.lines.join("\n")), "worker:" + w.worker)))) : null,
    pre);
}

// ── Runs & schedule ────────────────────────────────────────────────────
const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
routes.runs = async (view) => {
  await loadWatch();
  const sched = await api("/api/schedule");
  const s = sched.schedule, ib = sched.inbox;
  const runsEl = h("div");
  const histEl = h("div");
  const nextEl = h("p", { class: "muted small" });

  const actions = [
    ["round", "Full round", "Find new jobs, score them, tailor CVs and letters, then apply until the queue is empty.", true],
    ["discover", "Find jobs", "Search all boards and markets for new postings."],
    ["pipeline", "Score & tailor", "Fetch descriptions, score fit, write CVs, cover letters and PDFs."],
    ["apply", "Apply", "Send applications for everything ready, up to the per-round limit."],
    ["apply-dry", "Dry run", "Fill one form without submitting, to watch the agent."],
    ["inbox", "Check Gmail", "Sort new job emails, label them and notify you."],
    ["followups", "Email follow-ups", "Click verification links, finish incomplete applications and give consent where an email asks for it."],
  ];

  let liveInProgress = [];
  async function loadRuns() {
    const [r, live] = await Promise.all([api("/api/runs"), api("/api/live")]);
    liveInProgress = live.in_progress || [];
    if (!r.workers) r.workers = live.workers || [];
    const cur = r.current.filter((x) => x.running);
    mount(runsEl, 
      r.external.length ? h("div", { class: "callout", style: "margin-bottom:12px" }, h("div", { class: "row" }, h("span", {}, "An ApplyPilot process started outside the UI is running: ", h("span", { class: "mono small" }, r.external.join(" · "))), h("span", { class: "spacer" }),
        h("button", { class: "btn sm danger", onclick: async () => { const x = await api("/api/runs/external/stop", { method: "POST" }); toast(x.still_running.length ? "Asked it to stop; still finishing" : "Stopped"); loadRuns(); } }, "Stop it"))) : null,
      cur.length ? cur.map((x) => h("div", { class: "live-run", style: "margin-bottom:12px" },
        h("div", { class: "row", style: "margin-bottom:8px" }, h("span", { class: "muted small" }, (x.trigger === "schedule" ? "scheduled · " : "") + "started " + when(x.started)),
          h("span", { class: "spacer" }), h("button", { class: "btn sm danger", onclick: async () => { await api(`/api/runs/${x.id}/stop`, { method: "POST" }); toast("Stopping…"); } }, "Stop")),
        livePanel(x, x.kind === "round" && (x.log || "").split("\n").length < 5 ? (x.log || "") + "\n" + (r.nightly_log || "") : x.log, r.workers, liveInProgress)))
        : h("p", { class: "muted" }, "Nothing running."),
      !cur.length && r.nightly_log ? stickyDetails("nightly-log", "Today's round log", h("pre", { class: "log tall" }, r.nightly_log)) : null);
    const finished = [...r.current.filter((x) => !x.running), ...r.history].slice(0, 15);
    mount(histEl, finished.length ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Started"), h("th", {}, "Run"), h("th", {}, "How"), h("th", {}, "Result"), h("th", {}, ""))),
      h("tbody", {}, finished.map((x) => [h("tr", {},
        h("td", { class: "small" }, when(x.started)), h("td", {}, x.label), h("td", { class: "small muted" }, x.trigger),
        h("td", {}, x.rc === 0 ? h("span", { class: "chip good" }, "Done") : h("span", { class: "chip bad" }, x.rc == null ? "Unknown" : "Exit " + x.rc)),
        h("td", { class: "num" }, h("button", { class: "btn sm ghost", onclick: async () => {
          // the table is re-rendered every 1.5 s, so the open log lives in OPEN_LOGS and is drawn as a row
          if (OPEN_LOGS.has(x.id)) { OPEN_LOGS.delete(x.id); loadRuns(); return; }
          OPEN_LOGS.set(x.id, null);
          try { OPEN_LOGS.set(x.id, (await api(`/api/runs/${x.id}/log`)).log || "(empty log)"); } catch (e) { OPEN_LOGS.set(x.id, "Log not available: " + e.message); }
          loadRuns();
        } }, OPEN_LOGS.has(x.id) ? "Hide" : "Log"))),
        OPEN_LOGS.has(x.id) ? h("tr", {}, h("td", { colspan: 5, style: "padding:0 0 10px" }, keepScroll(h("pre", { class: "log tall", style: "margin:0" }, OPEN_LOGS.get(x.id) || "Loading…"), "histlog:" + x.id))) : null])))
      : h("p", { class: "muted" }, "No runs yet in this session."));
    nextEl.textContent = (r.scheduler.next_round ? "Next scheduled round: " + when(r.scheduler.next_round) : "No round scheduled") +
      (r.scheduler.next_inbox ? " · next Gmail check " + when(r.scheduler.next_inbox) : "");
  }

  const enabled = h("input", { type: "checkbox", class: "switch", checked: s.enabled });
  const timeIn = h("input", { type: "time", value: s.time, style: "max-width:130px" });
  const dayBoxes = DAY_NAMES.map((n, i) => h("label", {}, h("input", { type: "checkbox", value: i, checked: s.days.includes(i) }), h("span", {}, n)));
  const ibEnabled = h("input", { type: "checkbox", class: "switch", checked: ib.enabled });
  const ibInterval = h("input", { type: "number", min: 2, max: 240, value: ib.interval_min, style: "max-width:90px" });
  async function saveSchedule() {
    try {
      const body = { schedule: { enabled: enabled.checked, time: timeIn.value, days: dayBoxes.map((l) => $("input", l)).filter((x) => x.checked).map((x) => +x.value) },
        inbox: { enabled: ibEnabled.checked, interval_min: +ibInterval.value || 10 } };
      await api("/api/schedule", { method: "PUT", body });
      toast("Schedule saved");
      loadRuns();
      pollSide();
    } catch (e) { toast(e.message, true); }
  }

  mount(view, 
    h("div", { class: "head" }, h("div", {}, h("h1", {}, "Runs & schedule"), h("p", { class: "sub" }, "Start any stage by hand, watch it live, or let the scheduler run a full round every day. The scheduler works while this app is running and the Mac is awake."))),
    h("div", { class: "grid g3" }, actions.map(([k, title, desc, primary]) => h("div", { class: "card" },
      h("h3", {}, title), h("p", { class: "muted small", style: "min-height:40px;margin:0 0 12px" }, desc),
      h("button", { class: "btn" + (primary ? " primary" : ""), onclick: () => startRun(k) }, "Start")))),
    h("div", { class: "card section" }, h("div", { class: "row", style: "margin-bottom:8px" }, h("h2", { style: "margin:0" }, "Live"), watchSwitch()), runsEl),
    h("div", { class: "grid g2 section" },
      h("div", { class: "card" }, h("h2", {}, "Daily round"),
        h("label", { class: "check", style: "margin-bottom:14px" }, enabled, "Run a full round automatically"),
        h("div", { class: "row", style: "margin-bottom:12px" }, h("span", { class: "small muted", style: "width:50px" }, "At"), timeIn),
        h("div", { class: "days", style: "margin-bottom:14px" }, dayBoxes),
        h("button", { class: "btn primary", onclick: saveSchedule }, "Save schedule"), nextEl),
      h("div", { class: "card" }, h("h2", {}, "Gmail watcher"),
        h("label", { class: "check", style: "margin-bottom:14px" }, ibEnabled, "Check Gmail for replies automatically"),
        h("div", { class: "row", style: "margin-bottom:14px" }, h("span", { class: "small muted" }, "Every"), ibInterval, h("span", { class: "small muted" }, "minutes")),
        h("p", { class: "muted small" }, "New replies show up as macOS notifications and in Responses. Needs a Gmail app password in Settings."),
        h("button", { class: "btn primary", onclick: saveSchedule }, "Save"))),
    h("div", { class: "card section" }, h("h2", {}, "Recent runs"), histEl));
  await loadRuns();
  every(1500, () => loadRuns().catch(() => {}));
};

// ── generic form builder (profile) ─────────────────────────────────────
function tagInput(values, onChange) {
  const box = h("div", { class: "tags" });
  const input = h("input", { type: "text", placeholder: "Add…" });
  const draw = () => mount(box, ...values.map((v, i) => h("span", { class: "tag" }, v,
    h("button", { type: "button", "aria-label": "Remove " + v, onclick: () => { values.splice(i, 1); onChange(values); draw(); } }, "×"))), input);
  input.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === ",") && input.value.trim()) {
      e.preventDefault(); values.push(input.value.trim()); input.value = ""; onChange(values); draw(); input.focus();
    } else if (e.key === "Backspace" && !input.value && values.length) { values.pop(); onChange(values); draw(); input.focus(); }
  });
  input.addEventListener("blur", () => { if (input.value.trim()) { values.push(input.value.trim()); input.value = ""; onChange(values); draw(); } });
  draw();
  return box;
}

function buildFields(obj, help = {}, path = "") {
  return Object.entries(obj).filter(([k]) => !k.startsWith("_")).map(([k, v]) => {
    const key = path ? path + "." + k : k;
    const label = HELP_LABELS[key] || human(k);
    const hint = help[key];
    if (Array.isArray(v) && v.every((x) => typeof x === "string")) {
      return h("label", { class: "f wide" }, label, tagInput(v, (nv) => { obj[k] = nv; }), hint && h("span", { class: "h" }, hint));
    }
    if (v && typeof v === "object" && !Array.isArray(v)) {
      const make = () => {
        const fs = h("fieldset", {}, h("legend", {}, label), hint && h("p", { class: "muted small wide", style: "margin:0;grid-column:1/-1" }, hint), buildFields(v, help, key));
        if (KEYED_OBJECTS.includes(key)) fs.append(h("div", { class: "wide", style: "grid-column:1/-1" }, h("button", { type: "button", class: "btn sm", onclick: () => {
          const name = (prompt("Name of the new item (for example: codice_fiscale)") || "").trim().replace(/\s+/g, "_");
          if (name && !(name in v)) { v[name] = ""; fs.replaceWith(make()); }
        } }, "Add item")));
        return fs;
      };
      return make();
    }
    const long = typeof v === "string" && v.length > 60;
    const input = long
      ? h("textarea", { rows: Math.min(6, Math.ceil(v.length / 70) + 1), oninput: (e) => (obj[k] = e.target.value) }, v)
      : h("input", { type: k.includes("password") ? "password" : "text", value: v ?? "", oninput: (e) => (obj[k] = e.target.value) });
    return h("label", { class: "f" + (long ? " wide" : "") }, label, input, hint && h("span", { class: "h" }, hint));
  });
}
const KEYED_OBJECTS = ["personal.extra_facts"];
const HELP_LABELS = {
  "personal.extra_facts": "Extra facts used on forms", "personal.location_by_country": "Where you are, per job country",
  "personal.location_by_country.FR": "Jobs in France", "personal.location_by_country.GULF": "Jobs in the Gulf",
  "personal.location_by_country.default": "All other jobs", "skills_boundary": "Skills the CV may mention",
  "resume_facts": "Facts the CV must keep", "languages.listed": "Shown on every CV (per CV language)",
  "languages.gulf_only": "Added only for Gulf jobs", "personal.password": "Password for job-site accounts",
};
const PROFILE_SECTIONS = [
  ["personal", "You", "Name, contact details and address used on every form."],
  ["work_authorization", "Work rights", "How the agent answers work-permit and sponsorship questions. Never claims more than this."],
  ["availability", "Availability", "Notice period, contract types and relocation."],
  ["compensation", "Salary", "Your expectation per country and contract type. The agent answers the posting's midpoint if it is higher."],
  ["experience", "Experience", "Years, education and the role you are targeting."],
  ["languages", "Languages", "Exactly what CVs and forms say about your languages."],
  ["skills_boundary", "Skills", "The tailored CV may only use skills from this list or from your base CV."],
  ["resume_facts", "CV facts", "Companies, projects and numbers the tailoring must keep exactly."],
  ["eeo_voluntary", "Diversity questions", "Answers to optional diversity questions."],
];
const PROFILE_HELP = {
  "personal.password": "Used to create or sign in to employer career-site accounts. Kept only on this machine.",
  "personal.extra_facts": "Anything a form might require (date of birth, codice fiscale…). Facts not listed here are never invented: the job is parked instead.",
  "personal.location_by_country": "Each country group sets the city, address and the label printed on the CV.",
  "compensation.salary_notes": "Plain-language rule the agent follows, e.g. figures per country.",
  "work_authorization.permit_choice_rule": "What to pick when a form forces a permit type.",
  "resume_facts.real_metrics": "The only numbers a CV or letter may quote.",
};

routes.profile = async (view) => {
  const data = await api("/api/profile");
  const save = async () => {
    try { await api("/api/profile", { method: "PUT", body: data }); toast("Profile saved. Used from the next run."); }
    catch (e) { toast(e.message, true); }
  };
  const raw = h("textarea", { class: "code", rows: 30 }, JSON.stringify(data, null, 2));
  const advanced = h("details", { class: "section" }, h("summary", {}, "Advanced: edit the raw file"),
    h("p", { class: "muted small" }, "Every field, including ones not shown above. Must be valid JSON."), raw,
    h("div", { class: "row", style: "margin-top:10px" }, h("button", { class: "btn", onclick: async () => {
      try { const parsed = JSON.parse(raw.value); await api("/api/profile", { method: "PUT", body: parsed }); toast("Saved"); navigate(); }
      catch (e) { toast(e.message, true); }
    } }, "Save raw file")));
  const known = PROFILE_SECTIONS.map(([k]) => k);
  const extra = Object.keys(data).filter((k) => !k.startsWith("_") && !known.includes(k));
  mount(view, 
    h("div", { class: "head" }, h("div", {}, h("h1", {}, "Profile"), h("p", { class: "sub" }, "Everything employers are told about you. Changes apply from the next run; the previous version is kept in backups."))),
    ...PROFILE_SECTIONS.concat(extra.map((k) => [k, human(k), ""])).filter(([k]) => data[k] && typeof data[k] === "object").map(([k, title, intro]) =>
      h("div", { class: "form-section" }, h("div", { class: "intro" }, h("h3", {}, title), h("p", {}, intro)),
        h("div", { class: "fields" }, buildFields(data[k], PROFILE_HELP, k)))),
    h("div", { class: "savebar" }, h("button", { class: "btn primary", onclick: save }, "Save profile"), h("span", { class: "muted small" }, "Saved to profile.json")),
    advanced);
};

// ── Job search ─────────────────────────────────────────────────────────
routes.searches = async (view) => {
  const { raw, parsed } = await api("/api/searches");
  const p = parsed;
  p.defaults = p.defaults || {};
  const qRows = h("div", { class: "list-rows" });
  const nSearches = () => p.queries.reduce((n, q) => n + (q.markets && q.markets.length ? q.markets.length : p.locations.length), 0);
  const qCols = "grid-template-columns:1fr 150px 230px 32px";
  const drawQ = () => mount(qRows,
    h("div", { class: "list-row small muted", style: qCols }, h("span", {}, "Job title"), h("span", {}, "Tier"), h("span", {}, "Only in these markets (labels, blank = all)"), h("span")),
    ...p.queries.map((q, i) => h("div", { class: "list-row", style: qCols },
    h("input", { type: "text", value: q.query, oninput: (e) => (q.query = e.target.value) }),
    h("select", { onchange: (e) => (q.tier = +e.target.value) }, [1, 2, 3].map((t) => h("option", { value: t, selected: q.tier === t ? "selected" : null }, ["", "Tier 1 · core", "Tier 2 · adjacent", "Tier 3 · wide net"][t]))),
    h("input", { type: "text", value: (q.markets || []).join(", "), placeholder: "all markets", title: "Comma-separated market labels, e.g. france, belgium",
      oninput: (e) => { const m = e.target.value.split(",").map((x) => x.trim()).filter(Boolean); if (m.length) q.markets = m; else delete q.markets; } }),
    h("button", { class: "btn sm ghost", "aria-label": "Remove", onclick: () => { p.queries.splice(i, 1); drawQ(); } }, "×"))),
    h("button", { class: "btn sm", style: "justify-self:start", onclick: () => { p.queries.push({ query: "", tier: 2 }); drawQ(); } }, "Add a job title"));
  const lRows = h("div", { class: "list-rows" });
  const drawL = () => mount(lRows, 
    h("div", { class: "list-row small muted", style: "grid-template-columns:1fr 160px 90px 32px" }, h("span", {}, "Search location"), h("span", {}, "Indeed country site"), h("span", {}, "Remote only"), h("span")),
    ...p.locations.map((l, i) => h("div", { class: "list-row", style: "grid-template-columns:1fr 160px 90px 32px" },
      h("input", { type: "text", value: l.location, oninput: (e) => { l.location = e.target.value; l.label = l.label || e.target.value.toLowerCase(); } }),
      h("input", { type: "text", value: l.country_indeed || "", placeholder: "e.g. germany", oninput: (e) => (l.country_indeed = e.target.value) }),
      h("input", { type: "checkbox", class: "switch", checked: !!l.remote, onchange: (e) => (l.remote = e.target.checked) }),
      h("button", { class: "btn sm ghost", "aria-label": "Remove", onclick: () => { p.locations.splice(i, 1); drawL(); } }, "×"))),
    h("button", { class: "btn sm", style: "justify-self:start", onclick: () => { p.locations.push({ location: "", remote: false, label: "new", country_indeed: "" }); drawL(); } }, "Add a market"));
  drawQ(); drawL();
  const sites = ["indeed", "linkedin", "google", "glassdoor", "zip_recruiter"];
  p.sites = p.sites || [];
  const save = async () => {
    try { await api("/api/searches", { method: "PUT", body: { parsed: p } }); toast("Search settings saved"); }
    catch (e) { toast(e.message, true); }
  };
  const rawArea = h("textarea", { class: "code", rows: 28 }, raw);
  mount(view, 
    h("div", { class: "head" }, h("div", {}, h("h1", {}, "Job search"), h("p", { class: "sub" }, "What to search for and where. Each title is searched in every market."))),
    h("div", { class: "form-section" }, h("div", { class: "intro" }, h("h3", {}, "Job titles"), h("p", {}, "Tier 1 also searches company Workday portals. Write titles in any language; a title in one language can be limited to the markets where it is spoken."), h("p", {}, `${p.queries.length} titles, ${p.locations.length} markets, ${nSearches()} searches per run.`)), qRows),
    h("div", { class: "form-section" }, h("div", { class: "intro" }, h("h3", {}, "Markets"), h("p", {}, "Countries or cities to search. The Indeed country site must match (italy, france, uk, czech republic, united arab emirates…).")), lRows),
    h("div", { class: "form-section" }, h("div", { class: "intro" }, h("h3", {}, "Freshness & boards")),
      h("div", { class: "fields" },
        h("label", { class: "f" }, "Postings from the last (hours)", h("input", { type: "number", value: p.defaults.hours_old ?? 72, oninput: (e) => (p.defaults.hours_old = +e.target.value) }), h("span", { class: "h" }, "72 = 3 days. Use 24 for a daily schedule.")),
        h("label", { class: "f" }, "Results per board, per search", h("input", { type: "number", value: p.defaults.results_per_site ?? 25, oninput: (e) => (p.defaults.results_per_site = +e.target.value) })),
        h("div", { class: "f wide" }, "Job boards", h("div", { class: "row" }, sites.map((sname) => h("label", { class: "check" },
          h("input", { type: "checkbox", checked: p.sites.includes(sname), onchange: (e) => { p.sites = e.target.checked ? [...new Set([...p.sites, sname])] : p.sites.filter((x) => x !== sname); } }), human(sname))))))),
    h("div", { class: "form-section" }, h("div", { class: "intro" }, h("h3", {}, "Accepted places"), h("p", {}, "On-site or hybrid jobs must mention one of these. Remote jobs pass unless rejected below.")),
      tagInput(p.location_accept = p.location_accept || [], (v) => (p.location_accept = v))),
    h("div", { class: "form-section" }, h("div", { class: "intro" }, h("h3", {}, "Rejected places"), h("p", {}, "Checked first, even for remote jobs (e.g. 'Remote - US').")),
      tagInput(p.location_reject_non_remote = p.location_reject_non_remote || [], (v) => (p.location_reject_non_remote = v))),
    h("div", { class: "savebar" }, h("button", { class: "btn primary", onclick: save }, "Save search settings"), h("span", { class: "muted small" }, "Comments in searches.yaml are not kept when saving here; a backup is.")),
    h("details", { class: "section" }, h("summary", {}, "Advanced: edit searches.yaml"), rawArea,
      h("div", { class: "row", style: "margin-top:10px" }, h("button", { class: "btn", onclick: async () => {
        try { await api("/api/searches", { method: "PUT", body: { raw: rawArea.value } }); toast("Saved"); navigate(); } catch (e) { toast(e.message, true); }
      } }, "Save YAML"))));
};

// ── Base CVs ───────────────────────────────────────────────────────────
routes.cvs = async (view) => {
  const cvs = await api("/api/resumes");
  let lang = "en";
  const names = { en: "English", fr: "French", it: "Italian" };
  const area = h("textarea", { class: "code", rows: 34 });
  const tabBar = h("div", { class: "tabs" });
  const draw = () => {
    mount(tabBar, ...Object.keys(names).map((k) => h("button", { class: lang === k ? "on" : "", onclick: () => { cvs[lang] = area.value; lang = k; draw(); } }, names[k])));
    area.value = cvs[lang];
  };
  draw();
  mount(view, 
    h("div", { class: "head" }, h("div", {}, h("h1", {}, "Base CVs"), h("p", { class: "sub" }, "The source of every tailored CV. Add a job, project or skill here and every future CV can use it; nothing outside these files (and your profile) is ever added."))),
    h("div", { class: "callout info", style: "margin-bottom:16px" }, "Keep the structure: name, title, contact line, then ALL-CAPS section headings. One entry per line as 'Company | Role | Place | Dates', bullets starting with '- '. Every project listed here appears on every tailored CV."),
    tabBar, area,
    h("div", { class: "savebar" },
      h("button", { class: "btn primary", onclick: async () => { cvs[lang] = area.value; try { await api(`/api/resumes/${lang}`, { method: "PUT", body: { text: area.value } }); toast(names[lang] + " CV saved"); } catch (e) { toast(e.message, true); } } }, "Save"),
      h("a", { class: "btn", href: "#", onclick: (e) => { e.preventDefault(); window.open(`/api/resumes/${lang}/preview`, "_blank"); } }, "Preview as PDF"),
      h("span", { class: "muted small" }, "Preview renders the saved version with the CV template.")));
};

// ── Settings ───────────────────────────────────────────────────────────
routes.settings = async (view) => {
  const [s, doc] = await Promise.all([api("/api/settings"), api("/api/doctor")]);
  const run = { ...s.run }, keys = {}, prices = { ...s.prices };
  const score = h("input", { type: "range", min: 1, max: 10, value: run.MIN_SCORE, oninput: (e) => { run.MIN_SCORE = e.target.value; scoreV.textContent = e.target.value; } });
  const scoreV = h("strong", {}, run.MIN_SCORE);
  const KEY_HELP = {
    OPENAI_API_KEY: "Scoring, CVs, letters and email sorting.", LLM_MODEL: "e.g. gpt-6-luna", GEMINI_API_KEY: "Alternative to OpenAI.",
    LLM_URL: "Local OpenAI-compatible server instead.", CAPSOLVER_API_KEY: "Optional: solves CAPTCHAs during apply.",
    GMAIL_ADDRESS: "Inbox to watch for replies.", GMAIL_APP_PASSWORD: "16-letter app password from myaccount.google.com/apppasswords.",
  };
  const KEY_NAMES = { OPENAI_API_KEY: "OpenAI API key", GEMINI_API_KEY: "Gemini API key", LLM_MODEL: "AI model", LLM_URL: "Local model URL",
    CAPSOLVER_API_KEY: "CapSolver key", GMAIL_ADDRESS: "Gmail address", GMAIL_APP_PASSWORD: "Gmail app password" };
  const PRICE_NAMES = { llm_input: "Input tokens ($ / 1M)", llm_cached_input: "Cached input ($ / 1M)", llm_output: "Output tokens ($ / 1M)",
    apply_estimate_per_success: "Form filling, per sent application ($)", apply_estimate_per_failure: "Form filling, per failed attempt ($)" };
  const check = (ok, label) => h("div", { class: "row", style: "padding:4px 0" }, ok ? h("span", { class: "chip good" }, "OK") : h("span", { class: "chip bad" }, "Missing"), label);
  mount(view, 
    h("div", { class: "head" }, h("div", {}, h("h1", {}, "Settings & keys"), h("p", { class: "sub" }, `Data folder: ${s.app_dir}`))),
    h("div", { class: "form-section" }, h("div", { class: "intro" }, h("h3", {}, "How hard to push"), h("p", {}, "Used by every round, manual or scheduled.")),
      h("div", { class: "fields" },
        h("label", { class: "f wide" }, h("span", {}, "Minimum fit score to apply: ", scoreV, " / 10"), score, h("span", { class: "h" }, "7 = strong match. 6 widens the net to moderate matches; 8 is very selective.")),
        h("label", { class: "f" }, "Max applications per round", h("input", { type: "number", min: 0, value: run.MAX_APPLY, oninput: (e) => (run.MAX_APPLY = e.target.value) }), h("span", { class: "h" }, "0 = no limit: apply to every scored job, then stop.")),
        h("label", { class: "f" }, "Browser windows in parallel", h("input", { type: "number", min: 1, max: 4, value: run.WORKERS, oninput: (e) => (run.WORKERS = e.target.value) }), h("span", { class: "h" }, "More is faster but uses your Claude limit sooner.")),
        h("label", { class: "f" }, "Model that fills forms", h("select", { onchange: (e) => (run.APPLY_MODEL = e.target.value) },
          ["sonnet", "haiku", "opus"].map((m) => h("option", { value: m, selected: run.APPLY_MODEL === m ? "selected" : null }, { sonnet: "Sonnet (reliable)", haiku: "Haiku (cheaper, weaker)", opus: "Opus (strongest, slowest)" }[m])))),
        h("label", { class: "check f wide" }, h("input", { type: "checkbox", class: "switch", checked: run.HEADLESS === "1", onchange: (e) => (run.HEADLESS = e.target.checked ? "1" : "0") }),
          h("span", {}, "Run browsers without a window ", h("span", { class: "muted" }, "(needed on a server; on your Mac, off lets you watch)"))))),
    h("div", { class: "form-section" }, h("div", { class: "intro" }, h("h3", {}, "API keys"), h("p", {}, "Stored in api_keys.env on this machine. Leave a field empty to keep the current value.")),
      h("div", { class: "fields" }, Object.entries(s.keys).map(([k, v]) => h("label", { class: "f" }, h("span", {}, KEY_NAMES[k] || human(k.toLowerCase()), " ", v.set ? h("span", { class: "muted" }, "· set " + (v.value || "")) : h("span", { class: "muted" }, "· not set")),
        h("input", { type: /KEY|PASSWORD/.test(k) ? "password" : "text", placeholder: v.set ? "Replace…" : "", autocomplete: "off", oninput: (e) => (keys[k] = e.target.value) }),
        h("span", { class: "h" }, KEY_HELP[k] || ""))))),
    h("div", { class: "form-section" }, h("div", { class: "intro" }, h("h3", {}, "Prices"), h("p", {}, "Used by the Costs page. USD per million tokens; apply figures are estimates per attempt.")),
      h("div", { class: "fields" }, Object.entries(prices).map(([k, v]) => h("label", { class: "f" }, PRICE_NAMES[k] || human(k), h("input", { type: "number", step: "0.01", value: v, oninput: (e) => (prices[k] = e.target.value) }))))),
    h("div", { class: "form-section" }, h("div", { class: "intro" }, h("h3", {}, "Connection tests"), h("p", {}, "Uses the keys saved in the file, not unsaved edits.")),
      h("div", { class: "row" },
        h("button", { class: "btn", onclick: (e) => runTest(e.target, "/api/test/llm") }, "Test AI key"),
        h("button", { class: "btn", onclick: (e) => runTest(e.target, "/api/test/gmail") }, "Test Gmail"))),
    h("div", { class: "form-section" }, h("div", { class: "intro" }, h("h3", {}, "System check")),
      h("div", {}, check(doc.llm_key, "LLM key"), check(doc.claude, "Claude Code CLI (fills forms)"), check(doc.chrome, "Google Chrome"), check(doc.npx, "Node.js / npx"),
        check(doc.pdflatex, "LaTeX (optional, nicer CVs)"), check(doc.gmail, "Gmail app password (optional, reply tracking)"))),
    h("div", { class: "savebar" }, h("button", { class: "btn primary", onclick: async () => {
      try { await api("/api/settings", { method: "PUT", body: { run, keys, prices } }); toast("Settings saved"); navigate(); } catch (e) { toast(e.message, true); }
    } }, "Save settings")));
};

async function runTest(btn, path) {
  let out = btn.nextElementSibling && btn.nextElementSibling.classList.contains("result") ? btn.nextElementSibling : null;
  if (!out) { out = h("span", { class: "result" }); btn.after(out); }
  out.className = "result"; out.textContent = "Testing…"; btn.disabled = true;
  try { const r = await api(path, { method: "POST" }); out.className = "result " + (r.ok ? "ok" : "bad"); out.textContent = r.ok ? r.detail : r.error; }
  catch (e) { out.className = "result bad"; out.textContent = e.message; }
  btn.disabled = false;
}

// ── Costs ──────────────────────────────────────────────────────────────
routes.costs = async (view) => {
  const c = await api("/api/costs");
  const usd = (n, d = 2) => "$" + Number(n || 0).toFixed(d);
  const stages = Object.entries(c.llm.by_stage).sort((a, b) => b[1].usd - a[1].usd);
  const NAMES = { scorer: "Scoring", tailor: "CV tailoring", judge: "Fact-check judge", cover_letter: "Cover letters", detail: "Reading postings", inbox: "Email sorting", smartextract: "Company sites", other: "Other" };
  mount(view, 
    h("div", { class: "head" }, h("div", {}, h("h1", {}, "Costs"), h("p", { class: "sub" }, "Measured from every call since usage logging started. Change prices in Settings."))),
    h("div", { class: "kpis", style: "grid-template-columns:repeat(4,1fr)" },
      h("div", { class: "kpi" }, h("div", { class: "l" }, "AI text (OpenAI), measured"), h("div", { class: "v" }, usd(c.llm.total_usd, 3)), h("div", { class: "d" }, `${fmtNum(c.llm.calls)} calls`)),
      h("div", { class: "kpi" }, h("div", { class: "l" }, "Form filling (Claude), measured"), h("div", { class: "v" }, usd(c.apply.measured_usd)), h("div", { class: "d" }, `${c.apply.measured_runs} attempts${c.apply.avg_measured ? " · avg " + usd(c.apply.avg_measured) : ""}`)),
      h("div", { class: "kpi" }, h("div", { class: "l" }, "Form filling, all-time estimate"), h("div", { class: "v" }, usd(c.apply.estimate_all_time_usd, 0)), h("div", { class: "d" }, `${c.apply.applied} sent · ${c.apply.failed} not sent`)),
      h("div", { class: "kpi" }, h("div", { class: "l" }, "Price (per 1M tokens)"), h("div", { class: "v", style: "font-size:22px" }, `${usd(c.prices.llm_input)} / ${usd(c.prices.llm_output)}`), h("div", { class: "d" }, "input / output"))),
    h("div", { class: "callout info section" }, "On a Claude Pro or Max plan, form filling is not billed per token: it uses your plan's 5-hour usage window. The dollar figures show the API-equivalent cost."),
    h("div", { class: "grid g2 section" },
      h("div", { class: "card flush" }, h("h2", {}, "AI text by stage"), stages.length ? h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "Stage"), h("th", { class: "num" }, "Calls"), h("th", { class: "num" }, "Avg in"), h("th", { class: "num" }, "Avg out"), h("th", { class: "num" }, "Cost"))),
        h("tbody", {}, stages.map(([k, s]) => h("tr", {}, h("td", {}, NAMES[k] || human(k)), h("td", { class: "num" }, fmtNum(s.calls)),
          h("td", { class: "num" }, fmtNum(Math.round(s.input / s.calls))), h("td", { class: "num" }, fmtNum(Math.round(s.output / s.calls))), h("td", { class: "num" }, usd(s.usd, 4)))))) : h("p", { class: "muted", style: "padding:0 20px 16px" }, "No calls logged yet.")),
      h("div", { class: "card" }, h("h2", {}, "AI text cost per day"), c.llm.by_day.length ? columnChart(c.llm.by_day.slice(-14).map((d) => ({ label: new Date(d.day).getDate(), value: d.usd, tip: `${d.day}: ${usd(d.usd, 3)}` }))) : h("p", { class: "muted" }, "No data yet."))),
    h("div", { class: "card flush section" }, h("h2", {}, "Recent form-filling runs"), c.apply.recent.length ? h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "When"), h("th", {}, "Job"), h("th", {}, "Model"), h("th", { class: "num" }, "Seconds"), h("th", { class: "num" }, "Cost"))),
      h("tbody", {}, c.apply.recent.map((a) => h("tr", {}, h("td", { class: "small" }, when(a.ts)), h("td", {}, a.job), h("td", { class: "small" }, a.model), h("td", { class: "num" }, a.seconds), h("td", { class: "num" }, usd(a.cost_usd)))))) : h("p", { class: "muted", style: "padding:0 20px 16px" }, "Logged from the next apply run.")));
};

// ── boot ───────────────────────────────────────────────────────────────
pollSide();
navigate();
