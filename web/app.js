// Timeline desk. All Timeline text is inserted with textContent, never as HTML.
"use strict";

const STAGES = ["RSD", "Estimate", "Development", "Testing", "QA", "Live"];
const STAGE_SHORT = ["RSD", "Est", "Dev", "Test", "QA", "Live"];

const view = document.getElementById("view");
const meta = document.getElementById("meta");
const timelineUrls = new Map(); // issue number -> its Timeline URL, learned from every API response
const cache = { supportIds: null, jiraMine: null, jiraSupport: null, watched: new Set(), planner: null, statuses: new Map(), boards: null, projects: null, projectTasks: new Map(), workflows: null, guard: null };

// ---- helpers --------------------------------------------------------------
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

// replaceChildren, skipping null/false like h() does.
function put(el, ...children) {
  el.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false)
    .map((c) => (c instanceof Node ? c : document.createTextNode(String(c)))));
}

async function api(path, options) {
  const response = await fetch(path, options);
  let body;
  try { body = await response.json(); } catch { body = {}; }
  if (!response.ok) throw new Error(body.error || `Request failed (HTTP ${response.status}).`);
  rememberUrls(body);
  return body;
}

function rememberUrls(data, depth = 0) {
  if (!data || typeof data !== "object" || depth > 8) return;
  if (Array.isArray(data)) { for (const item of data) rememberUrls(item, depth + 1); return; }
  const m = typeof data.url === "string" && /^https:\/\/.+\/edit\/(\d+)$/.exec(data.url);
  if (m) timelineUrls.set(m[1], data.url);
  for (const value of Object.values(data)) rememberUrls(value, depth + 1);
}

// The "open in Timeline" button that sits next to a task title. A span, not a link or button, so it can
// live inside the cards and rows that are links or buttons themselves.
function openInTimeline(issue, url) {
  const href = url || timelineUrls.get(String(issue));
  if (!href) return null;
  const open = (e) => { e.preventDefault(); e.stopPropagation(); window.open(href, "_blank", "noopener,noreferrer"); };
  return h("span", {
    class: "open-tl", role: "link", tabindex: "0", title: "Open in Timeline", "aria-label": `Open #${issue} in Timeline`,
    onclick: open, onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") open(e); },
  }, "↗");
}

function niceDate(iso, withWeekday = true) {
  if (!iso) return "";
  const d = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-GB", withWeekday
    ? { weekday: "short", day: "numeric", month: "short" }
    : { day: "numeric", month: "short", year: "numeric" });
}

// Timeline rich text arrives as HTML. Parse it in an inert document and keep
// only the text, with line breaks where blocks ended.
function plainText(html) {
  if (!html) return "";
  const withBreaks = html.replace(/<br\s*\/?>/gi, "\n").replace(/<\/(p|div|li|h[1-6]|tr)>/gi, "\n");
  const doc = new DOMParser().parseFromString(withBreaks, "text/html");
  return (doc.body.textContent || "").replace(/\n{3,}/g, "\n\n").trim();
}

// A small, safe subset of markdown, built as DOM nodes: headings, list items,
// **bold**, *italic*, `code`, and images/links as plain links to absolute URLs.
function absolute(url) {
  try { return new URL(url, "https://timeline.panteon.no").href; } catch { return null; }
}

// Only Timeline's own attachments are previewed, through the local proxy.
function attachmentSrc(href) {
  const m = href.match(/^https:\/\/timeline\.panteon\.no\/storage\/attachments\/([\w-]+\.(?:jpe?g|png|gif|webp))$/i);
  return m ? `/api/attachment/${m[1]}` : null;
}

function inline(text) {
  const out = [];
  const pattern = /!\[([^\]]*)\]\(([^)\s]+)[^)]*\)|\[([^\]]+)\]\(([^)\s]+)[^)]*\)|\*\*([^*]+)\*\*|\*([^*\n]+)\*|`([^`]+)`/g;
  let last = 0;
  for (const m of text.matchAll(pattern)) {
    out.push(text.slice(last, m.index));
    last = m.index + m[0].length;
    const href = absolute(m[2] || m[4] || "");
    if (m[2] !== undefined || m[4] !== undefined) {
      const label = m[2] !== undefined ? `[image${m[1] ? `: ${m[1]}` : ""}]` : m[3];
      const preview = m[2] !== undefined && href && attachmentSrc(href);
      if (preview) {
        out.push(h("a", { class: "preview", href, target: "_blank", rel: "noopener noreferrer", title: "Open full size" },
          h("img", { src: preview, alt: m[1] || "image", loading: "lazy" })));
        continue;
      }
      out.push(href && /^https?:/.test(href)
        ? h("a", { href, target: "_blank", rel: "noopener noreferrer" }, label)
        : label);
    } else if (m[5] !== undefined) out.push(h("strong", {}, m[5]));
    else if (m[6] !== undefined) out.push(h("em", {}, m[6]));
    else out.push(h("code", {}, m[7]));
  }
  out.push(text.slice(last));
  return out;
}

const TABLE_SEPARATOR = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function tableCells(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}

function tableBlock(header, separator, rows) {
  const align = tableCells(separator).map((c) => (c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : null));
  const cell = (tag, text, i) => h(tag, align[i] ? { style: `text-align: ${align[i]}` } : {}, inline(text));
  return h("div", { class: "md-table" }, h("table", {},
    h("thead", {}, h("tr", {}, tableCells(header).map((c, i) => cell("th", c, i)))),
    h("tbody", {}, rows.map((r) => h("tr", {}, tableCells(r).map((c, i) => cell("td", c, i)))))));
}

function richText(source) {
  return markdown(plainText(source));
}

// The same subset for text that is already plain (task docs, Claude's replies).
function markdown(text) {
  const lines = String(text || "").split("\n");
  const root = h("div", { class: "body rich" });
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const heading = line.match(/^\s*(#{1,6})\s+(.*)$/);
    const item = line.match(/^\s*([-*]|\d+[.)])\s+(.*)$/);
    const fence = line.match(/^\s*(```|~~~)/);
    if (fence) {
      const code = [];
      for (i++; i < lines.length && !lines[i].trim().startsWith(fence[1]); i++) code.push(lines[i]);
      root.append(h("pre", { class: "md-code" }, code.join("\n")));
    } else if (line.includes("|") && i + 1 < lines.length && lines[i + 1].includes("|") && TABLE_SEPARATOR.test(lines[i + 1])) {
      const rows = [];
      let j = i + 2;
      while (j < lines.length && lines[j].includes("|") && lines[j].trim()) rows.push(lines[j++]);
      root.append(tableBlock(line, lines[i + 1], rows));
      i = j - 1;
    } else if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) root.append(h("hr", {}));
    else if (/^\s*>/.test(line)) {
      const quote = [];
      for (; i < lines.length && /^\s*>/.test(lines[i]); i++) quote.push(lines[i].replace(/^\s*>\s?/, ""));
      i--;
      root.append(h("blockquote", {}, quote.map((q) => h("div", {}, q ? inline(q) : " "))));
    } else if (heading) root.append(h("strong", { class: `md-h md-h${heading[1].length}` }, inline(heading[2])));
    else if (item) root.append(h("div", { class: "md-li" }, `${/\d/.test(item[1]) ? item[1] : "•"} `, inline(item[2])));
    else root.append(h("div", {}, line ? inline(line) : " "));
  }
  return root;
}

// Avatar: initials, with the profile photo (via the local proxy) over them once it loads.
function initials(name) {
  return (name || "?").split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("");
}

function avatar(name, photo, title) {
  const el = h("span", { class: "avatar", "aria-hidden": title ? null : "true", title }, initials(name));
  if (photo) {
    const img = h("img", { src: `/api/photo/${encodeURIComponent(photo)}`, alt: "", loading: "lazy" });
    img.addEventListener("load", () => el.classList.add("has-photo"));
    img.addEventListener("error", () => img.remove());
    el.append(img);
  }
  return el;
}

function showError(error) {
  put(view, h("p", { class: "error", role: "alert" }, error.message || String(error)));
}

function loading(text) {
  put(view, h("p", { class: "note" }, text));
}

// ---- stage rail -----------------------------------------------------------
function rail(status) {
  const known = status && !status.error;
  const label = known
    ? STAGES.map((s) => `${s}: ${status.stages[s]}`).join(", ")
    : "Stage progress loading";
  return h("div", { class: `rail${known ? "" : " pending"}`, role: "img", "aria-label": label, title: label },
    STAGES.map((s) => h("span", { "data-p": known ? status.stages[s] : null })));
}

function railKey() {
  return h("div", { class: "rail-key", "aria-hidden": "true" }, STAGE_SHORT.map((s) => h("span", {}, s)));
}

function legend() {
  const item = (p, text) => h("span", {}, h("i", { style: swatch(p) }), text);
  return h("p", { class: "legend" },
    item("done", "Done"), item("in progress", "In progress"), item("blocked", "Blocked"),
    item("skipped", "Skipped"), item(null, "Not started"));
}

function swatch(p) {
  const color = { done: "var(--done)", "in progress": "var(--progress)", blocked: "var(--blocked)" }[p];
  if (color) return `background:${color};border-color:${color}`;
  if (p === "skipped") return "background:repeating-linear-gradient(135deg,var(--skipped) 0 2px,transparent 2px 5px);border-color:var(--skipped)";
  return "";
}

async function loadStatuses(ids) {
  const missing = ids.filter((id) => !cache.statuses.has(String(id)));
  for (let i = 0; i < missing.length; i += 100) {
    const batch = missing.slice(i, i + 100);
    const { statuses } = await api(`/api/statuses?ids=${encodeURIComponent(batch.join(","))}`);
    for (const s of statuses) cache.statuses.set(String(s.issue_id), s);
  }
  return ids.map((id) => cache.statuses.get(String(id)));
}

// ---- board ----------------------------------------------------------------
async function getPlanner(force) {
  if (!cache.planner || force) cache.planner = await api(force ? "/api/planner?refresh=1" : "/api/planner");
  const p = cache.planner;
  const now = p.periods.now.tasks.length;
  meta.textContent = `User ${p.user_id} · ${niceDate(p.today)} · ${now} task${now === 1 ? "" : "s"} now`;
  return p;
}

function flagBadge(flag) {
  if (!flag) return null;
  return h("span", { class: `badge ${flag === "overdue" ? "overdue" : "soon"}` }, flag);
}

function taskCard(task, boards) {
  const start = workflowLink(task.issue_number, task.code, boards);
  const card = h("a", { class: "card", href: `#/task/${task.issue_number}` },
    h("div", { class: "head" },
      h("span", { class: "issue" }, `#${task.issue_number}`),
      flagBadge(task.deadline_flag),
      guardSlot(task.issue_number),
      task.support ? h("span", { class: "badge support" }, "support") : null),
    h("p", { class: "title" }, task.title, " ", openInTimeline(task.issue_number, task.url)),
    rail(cache.statuses.get(String(task.issue_number))),
    h("div", { class: "foot" },
      h("span", { class: "stage" }, task.stage_status || "No stage"),
      h("span", {}, task.deadline ? niceDate(task.deadline) : "No deadline")));
  return start ? h("div", { class: "card-wrap" }, card, start) : card;
}

async function showPlanner(force) {
  loading("Loading your planner…");
  const [p, boards] = await Promise.all([getPlanner(force), getBoards(), getWorkflows(force)]);
  // Stages aren't in the planner response: fetch them first so each card is drawn once, final.
  const ids = Object.values(p.periods).flatMap((period) => period.tasks.map((t) => t.issue_number));
  await loadStatuses(ids).catch(() => null); // on failure the rails stay dashed
  const columns = Object.entries(p.periods).map(([key, period]) => {
    const overdue = period.tasks.filter((t) => t.deadline_flag === "overdue").length;
    return h("section", { class: "column", "aria-labelledby": `col-${key}` },
      h("h2", { id: `col-${key}` }, period.label,
        h("span", { class: "count" }, period.tasks.length),
        overdue ? h("span", { class: "warn" }, `${overdue} overdue`) : null),
      railKey(),
      period.tasks.length
        ? period.tasks.map((t) => taskCard(t, boards))
        : h("p", { class: "note" }, "Nothing planned here."));
  });
  put(view, legend(), h("div", { class: "board" }, columns));
  trackGuard();
}

// ---- changes --------------------------------------------------------------
const PERIOD_LABEL = { now: "Now", next: "Next", commingup: "Coming up" };
const periodLabel = (p) => PERIOD_LABEL[p] || p || "—";

// What happened, in the order people care about. Colour is kept for deadlines only.
const CHANGE_KINDS = [
  { key: "added", label: "New", short: "New", icon: "M12 5v14M5 12h14" },
  { key: "removed", label: "Left the planner", short: "Left", icon: "M5 12h14" },
  { key: "moved", label: "Moved", short: "Moved", icon: "M7 4 3 8l4 4M3 8h14M17 20l4-4-4-4M21 16H7" },
  { key: "changed", label: "Changed", short: "Changed", icon: "M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z" },
];

const chip = (text, tone, title) => h("span", { class: `chip ${tone || ""}`, title: title || null }, text);

// "in 2 days", "tomorrow", "3 days overdue": how far the deadline is from today.
function countdown(deadline) {
  const days = Math.round((new Date(`${deadline}T00:00:00`) - new Date(new Date().toDateString())) / 86400000);
  if (Number.isNaN(days)) return "";
  if (days === 0) return "today";
  if (days === 1) return "tomorrow";
  if (days > 1) return `in ${days} days`;
  return `${-days} day${days === -1 ? "" : "s"} overdue`;
}

function deadlineChip(task) {
  if (!task.deadline) return chip("No deadline", "muted");
  const flag = task.deadline_flag;
  return chip(`${niceDate(task.deadline)} · ${countdown(task.deadline)}`,
    flag === "overdue" ? "overdue" : flag ? "soon" : "");
}

function spanText(from, to) {
  const minutes = Math.round((new Date(to) - new Date(from)) / 60000);
  if (Number.isNaN(minutes) || minutes < 0) return "";
  if (minutes < 60) return "under an hour";
  if (minutes < 60 * 24) return `${Math.round(minutes / 60)} h`;
  const days = Math.round(minutes / 1440);
  return `${days} day${days === 1 ? "" : "s"}`;
}

const RELEASE_LABEL = { "before qa": "Before QA", "in qa": "In QA", "qa blocked": "QA blocked", "qa approved": "QA approved", live: "Live" };

// One "field: before → after" row, as pills (same look as the Timeline tab).
function pairRow(name, before, after) {
  return h("div", { class: "tl-change" },
    h("span", { class: "tl-field" }, name),
    h("span", { class: "tl-pill was" }, before || "—"),
    h("span", { class: "tl-arrow", "aria-hidden": "true" }, "→"),
    h("span", { class: "tl-pill now" }, after || "—"));
}

let changeBoards = {};

// `first` puts the detection time in the left column; later cards of the same
// check leave it out. `fresh` marks entries found since the last visit.
function changeCard(task, kind, at, first, fresh) {
  let facts;
  if (kind.key === "removed") {
    facts = [h("span", { class: "note" }, `was in ${periodLabel(task.period)} · ${task.stage_status || "no stage"}`)];
  } else {
    facts = [
      chip(`${periodLabel(task.period)} · #${task.position}`, "", "Priority position in this period"),
      chip(task.stage_status || "No stage"),
      deadlineChip(task),
      task.release_state ? chip(RELEASE_LABEL[task.release_state] || task.release_state) : null,
      task.support ? chip("Support") : null,
      task.code ? chip(changeBoards[String(task.code)] || `Board ${task.code}`, "muted") : null,
    ];
  }
  // Under an hour says nothing useful, so the note starts at an hour.
  const onPlanner = task.on_planner_since ? spanText(task.on_planner_since, at) : "";
  if (onPlanner && onPlanner !== "under an hour") {
    facts.push(h("span", { class: "note" },
      `on your planner for ${task.since_tracking_start ? "at least " : ""}${onPlanner}`));
  }
  const show = (name, v) => (name === "deadline" && v ? niceDate(v) : v);
  const rows = kind.key === "changed" ? task.changes.map(([name, before, after]) => pairRow(name, show(name, before), show(name, after)))
    : kind.key === "moved" ? [pairRow("period", periodLabel(task.from), periodLabel(task.to))] : [];
  return h("li", { class: `tl-event ${kind.key}${fresh ? " fresh" : ""}`, style: taskStyle(task.issue_number) },
    first ? h("time", { class: "tl-when", datetime: at, title: niceTime(at) }, h("b", {}, clock(at)), h("small", {}, ago(at))) : null,
    h("span", { class: "tl-node", "aria-hidden": "true" }, svgIcon(kind.icon, 15)),
    h("article", { class: "tl-card" },
      h("header", {},
        h("span", { class: `tl-kindtag ${kind.key}` }, kind.short),
        h("a", { class: "tl-task", href: `#/task/${task.issue_number}`, style: taskStyle(task.issue_number), title: task.title || "" },
          h("b", {}, `#${task.issue_number}`), task.title ? h("span", {}, task.title) : null),
        task.url ? h("a", { class: "ext", href: task.url, target: "_blank", rel: "noopener noreferrer", title: "Open in Timeline" }, "↗") : null,
        fresh ? h("span", { class: "tl-new" }, "since your last visit") : null),
      rows.length ? h("div", { class: "tl-rows" }, rows) : null,
      task.stages && kind.key !== "removed" ? h("div", { class: "stages" }, rail({ stages: task.stages }), railKey()) : null,
      h("div", { class: "facts" }, facts)));
}

function ago(iso) {
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (Number.isNaN(minutes)) return "";
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  if (minutes < 60 * 24) return `${Math.round(minutes / 60)} h ago`;
  return `${Math.round(minutes / 1440)} d ago`;
}


function dayNote(date) {
  const day = (d) => new Date(d).toDateString();
  if (day(`${date}T12:00:00`) === day(Date.now())) return "Today";
  if (day(`${date}T12:00:00`) === day(Date.now() - 86400000)) return "Yesterday";
  return "";
}

// Remembered per browser, so entries found since your last visit stand out.
// Read once per page load: a background refresh must not mark them seen.
let changesSeenAt = "";
try { changesSeenAt = localStorage.getItem("changesSeenAt") || ""; } catch { /* storage blocked */ }
let changesFilter = "all";

function niceTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" })}, ${d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}`;
}

const CHANGES_REFRESH_MS = 60 * 1000;
let changesTimer = null;

const recordSnapshot = () => api("/api/snapshot", { method: "POST", headers: { "X-Timeline-Desk": "1" } });

// `check` fetches the planner from Timeline first, so a task added a minute
// ago shows up; otherwise this only reads what the background check stored.
async function showChanges(quiet = false, check = false) {
  clearTimeout(changesTimer);
  if (!quiet) loading(check ? "Checking the planner…" : "Loading planner history…");
  let checkError = "";
  if (check) {
    try { await recordSnapshot(); } catch (error) { checkError = error.message; }
  }
  const [history, boards] = await Promise.all([api("/api/changes"), getBoards()]);
  changeBoards = boards || {};

  // Totals for the last 24 hours double as the counts on the filters.
  const dayAgo = Date.now() - 24 * 3600 * 1000;
  const recent = history.changes.filter((e) => new Date(e.taken_at).getTime() >= dayAgo);
  const total = (list, key) => list.reduce((n, e) => n + e.diff[key].length, 0);
  const recentTotal = CHANGE_KINDS.reduce((n, k) => n + total(recent, k.key), 0);

  const tile = (value, label, sub) => h("div", { class: "tl-tile" }, h("b", {}, value), h("span", {}, label), sub ? h("small", {}, sub) : null);
  const summary = h("section", { class: "tl-summary", "aria-label": "Summary" },
    tile(recentTotal, "changes in 24 h"),
    tile(history.tracking_since ? niceDate(history.tracking_since.slice(0, 10)) : "—", "tracking since",
      history.tracking_since ? clock(history.tracking_since) : "press Refresh"),
    tile(history.last_checked ? clock(history.last_checked) : "—", "last checked", history.last_checked ? ago(history.last_checked) : ""),
    checkError ? h("div", { class: "tl-tile bad" }, h("b", {}, "!"), h("span", {}, checkError)) : null);

  const pick = (key) => { changesFilter = key; showChanges(true); };
  const filters = h("div", { class: "tl-kinds", role: "group", "aria-label": "Filter changes" },
    h("button", { type: "button", class: "tl-kind", "aria-pressed": String(changesFilter === "all"), onclick: () => pick("all") }, "All"),
    CHANGE_KINDS.map((k) => h("button", { type: "button", class: `tl-kind ${k.key}`, "aria-pressed": String(changesFilter === k.key),
      title: "Counts are for the last 24 hours", onclick: () => pick(changesFilter === k.key ? "all" : k.key) },
      svgIcon(k.icon, 14), k.label, h("span", { class: "tl-n" }, total(recent, k.key)))));

  const unseen = (e) => Boolean(changesSeenAt) && e.taken_at > changesSeenAt;
  // One section per day; inside it, one card per task, the time on the first card of each check.
  const feed = h("ol", { class: "tl-feed" });
  let day = null, any = false;
  for (const entry of history.changes) {
    const items = CHANGE_KINDS
      .filter((k) => changesFilter === "all" || changesFilter === k.key)
      .flatMap((k) => entry.diff[k.key].map((t) => ({ task: t, kind: k })));
    if (!items.length) continue;
    any = true;
    const date = entry.taken_at.slice(0, 10);
    if (date !== day) {
      day = date;
      const count = history.changes.filter((e) => e.taken_at.startsWith(date))
        .reduce((n, e) => n + CHANGE_KINDS.filter((k) => changesFilter === "all" || changesFilter === k.key)
          .reduce((m, k) => m + e.diff[k.key].length, 0), 0);
      feed.append(h("li", { class: "tl-day" }, h("h2", {}, niceDate(date), dayNote(date) ? h("em", {}, dayNote(date)) : null),
        h("span", { class: "tl-count" }, `${count} change${count === 1 ? "" : "s"}`)));
    }
    items.forEach(({ task, kind }, i) => feed.append(changeCard(task, kind, entry.taken_at, i === 0, unseen(entry))));
  }

  let intro = null;
  if (!history.tracking_since) intro = "No snapshots yet. Press Refresh to start tracking.";
  else if (!history.changes.length) intro = `Tracking since ${niceTime(history.tracking_since)}. Nothing has changed yet; the planner is checked every few minutes.`;
  else if (!any) intro = "No changes of this kind. Pick another filter.";
  put(view, h("div", { class: "tl-page" }, summary,
    history.changes.length ? h("div", { class: "tl-controls" }, filters) : null,
    intro ? h("p", { class: "note" }, intro) : null,
    any ? feed : null));
  if (history.changes.length) {
    try { localStorage.setItem("changesSeenAt", history.changes[0].taken_at); } catch { /* storage blocked */ }
  }
  // The dashboard checks the planner in the background; pick up what it found.
  changesTimer = setTimeout(() => {
    if (location.hash === "#/changes") showChanges(true).catch(() => {});
  }, CHANGES_REFRESH_MS);
}

// ---- timeline (events of watched tasks) -------------------------------------
const writeHeaders = { "X-Timeline-Desk": "1" };
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

async function loadWatched() {
  try {
    const { watchlist } = await api("/api/watchlist");
    cache.watched = new Set(watchlist.filter((w) => w.active).map((w) => String(w.issue_number)));
  } catch { /* the button then offers Watch; the server decides */ }
}

// Watch / Unwatch toggle. Only your own open tasks can be watched (the server checks).
function watchButton(issue) {
  const id = String(issue);
  const button = h("button", { type: "button", class: "watch" });
  const draw = () => {
    const on = cache.watched.has(id);
    button.textContent = on ? "★" : "☆";
    button.setAttribute("aria-pressed", String(on));
    button.setAttribute("aria-label", `${on ? "Stop watching" : "Watch"} #${id}`);
    button.title = on ? "Watching: its changes go to the Timeline. Click to stop." : "Add this task's changes to the Timeline";
  };
  draw();
  button.addEventListener("click", async (event) => {
    event.preventDefault();
    event.stopPropagation();
    button.disabled = true;
    try {
      if (cache.watched.has(id)) {
        const r = await api(`/api/watchlist/${id}`, { method: "DELETE", headers: writeHeaders });
        cache.watched.delete(id);
        toast(`Stopped watching #${id}. ${plural(r.deleted_events || 0, "event")} deleted.`);
      } else {
        const r = await api("/api/watchlist", { method: "POST", headers: { ...writeHeaders, "Content-Type": "application/json" }, body: JSON.stringify({ issue: id }) });
        cache.watched.add(id);
        toast(`Watching #${id}. ${plural((r.sync && r.sync.new_events) || 0, "event")} added.`);
      }
    } catch (error) { toast(error.message, true); }
    button.disabled = false;
    draw();
  });
  return button;
}

// Event kinds: label, verb on the card, and the icon on the rail.
const EVENT_KINDS = {
  created: { label: "Created", verb: "created the task", icon: "M12 5v14M5 12h14" },
  comment: { label: "Comments", verb: "commented", icon: "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" },
  status: { label: "Status", verb: "changed the status", icon: "M5 12h14M13 6l6 6-6 6" },
  field: { label: "Fields", verb: "updated", icon: "M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z" },
  commit: { label: "Commits", verb: "committed", icon: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM3 12h6M15 12h6" },
};
// The last event types and task chosen on the Timeline page survive a reload (per browser).
const FILTERS_KEY = "timeline-desk.timeline-filters";
function loadFilters() {
  const state = { issues: new Set(), kinds: new Set(["created", "comment", "status", "field"]), q: "" };
  try {
    const saved = JSON.parse(localStorage.getItem(FILTERS_KEY) || "null");
    if (saved && Array.isArray(saved.kinds)) state.kinds = new Set(saved.kinds.filter((k) => k in EVENT_KINDS).concat(saved.kinds.includes("comment") ? ["created"] : []));
    if (saved && Array.isArray(saved.issues)) state.issues = new Set(saved.issues.map(String));
  } catch (error) { /* storage blocked or unreadable: use the defaults */ }
  return state;
}
function saveFilters() {
  try {
    localStorage.setItem(FILTERS_KEY, JSON.stringify({ kinds: [...eventsState.kinds], issues: [...eventsState.issues] }));
  } catch (error) { /* storage blocked: the choice just won't be remembered */ }
}
const eventsState = loadFilters();
let eventsToken = 0;
let liveTimer = null;
let liveBusy = false;
const LIVE_POLL_MS = 2000;

function stopLive() {
  clearInterval(liveTimer);
  liveTimer = null;
}

// Watches the local event marker (a database read, never Timeline) and calls `refresh` when the stored
// events changed, so a comment the background sync just saved appears without pressing Refresh.
function startLive(marker, refresh) {
  stopLive();
  liveTimer = setInterval(async () => {
    if (liveBusy || document.hidden) return;
    liveBusy = true;
    try {
      const now = await api("/api/events/marker");
      if (now.max_id !== marker.max_id || now.count !== marker.count) {
        marker = now;
        await refresh();
      }
    } catch (error) { /* the next tick tries again */ } finally { liveBusy = false; }
  }, LIVE_POLL_MS);
}

function svgIcon(path, size = 16) {
  return new DOMParser().parseFromString(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="${path}"/></svg>`,
    "image/svg+xml").documentElement;
}

// Each task gets its own hue, so a task is recognisable at a glance down the feed.
const taskHue = (issue) => (Number(issue) * 137) % 360;
const taskStyle = (issue) => `--hue: ${taskHue(issue)}`;

function valueTone(text) {
  if (/block|fail|reject/i.test(text)) return "bad";
  if (/done|approved|live|complete|finish/i.test(text)) return "good";
  if (/progress|testing|working/i.test(text)) return "warn";
  return "";
}

function changePills(e) {
  const dated = /deadline|date/i.test(e.field_name);
  const show = (v) => (v ? (dated ? niceDate(v, false) : v) : "—");
  const status = e.kind === "status";
  return h("div", { class: "tl-change" },
    status ? null : h("span", { class: "tl-field" }, e.field_name),
    h("span", { class: `tl-pill was ${status ? valueTone(e.old_value || "") : ""}` }, show(e.old_value)),
    h("span", { class: "tl-arrow", "aria-hidden": "true" }, "→"),
    h("span", { class: `tl-pill now ${status ? valueTone(e.new_value || "") : ""}` }, show(e.new_value)));
}

function commentBody(e) {
  const text = plainText(e.body);
  if (!text) return h("p", { class: "note" }, "(no text)");
  const box = h("div", { class: "tl-bubble clamped" }, richText(e.body));
  const long = text.length > 320 || text.split("\n").length > 6;
  if (!long) box.classList.remove("clamped");
  const more = long ? h("button", { type: "button", class: "tl-more", "aria-expanded": "false",
    onclick: () => {
      const open = box.classList.toggle("clamped") === false;
      more.textContent = open ? "Show less" : "Show more";
      more.setAttribute("aria-expanded", String(open));
    } }, "Show more") : null;
  return h("div", {}, box, more);
}

function commitLine(e) {
  const sha = e.url && (e.url.match(/\/commit\/([0-9a-f]{7,40})/i) || [])[1];
  const message = plainText(e.body).replace(/^TL\s*#\d+\s*/i, "") || "Commit";
  const safe = e.url && /^https:\/\//.test(e.url);
  const code = sha ? h("code", { class: "tl-sha" }, sha.slice(0, 7)) : null;
  return h("div", { class: "tl-commit" },
    safe && code ? h("a", { href: e.url, target: "_blank", rel: "noopener noreferrer", title: "Open commit" }, code) : code,
    h("span", { class: "tl-commit-msg" }, message));
}

// The issue numbers of your support tasks, so the Timeline page can tag them. Loaded once; if the
// request fails the tags are simply missing and the next visit tries again.
async function loadSupportIds() {
  if (cache.supportIds) return cache.supportIds;
  const ids = new Set();
  try {
    for (const project of await getProjects()) {
      const data = await getProjectTasks(project, SUPPORT_TAB);
      for (const group of data.groups) for (const t of group.tasks) ids.add(String(t.issue_number));
    }
    cache.supportIds = ids;
  } catch { /* the tag is a nicety */ }
  return ids;
}

function supportTag(issue) {
  return cache.supportIds && cache.supportIds.has(String(issue)) ? h("span", { class: "badge support" }, "support") : null;
}

function taskPill(e, tasks) {
  const task = tasks[e.issue_number] || {};
  // The task link, the Timeline button and the support tag are siblings, so hovering one doesn't light up the others.
  return h("span", { class: "tl-taskline" },
    h("a", { class: "tl-task", href: `#/task/${e.issue_number}`, style: taskStyle(e.issue_number), title: task.title || "" },
      h("b", {}, `#${e.issue_number}`), task.title ? h("span", {}, task.title) : null),
    openInTimeline(e.issue_number, task.url), supportTag(e.issue_number));
}

function eventCard(e, tasks) {
  const kind = EVENT_KINDS[e.kind] || EVENT_KINDS.field;
  const compact = e.kind === "commit" || e.kind === "field";
  return h("li", { class: `tl-event ${e.kind}${compact ? " compact" : ""}`, style: taskStyle(e.issue_number) },
    h("time", { class: "tl-when", datetime: e.occurred_at, title: niceTime(e.occurred_at) },
      h("b", {}, e.occurred_at.slice(11, 16)), h("small", {}, ago(e.occurred_at))),
    h("span", { class: "tl-node", "aria-hidden": "true" }, svgIcon(kind.icon, 15)),
    h("article", { class: "tl-card" },
      h("header", {},
        avatar(e.employee, e.photo, e.employee),
        h("span", { class: "tl-who" }, h("strong", {}, e.employee || "Someone"), ` ${kind.verb}`,
          e.kind === "field" ? [" ", h("strong", {}, e.field_name)] : null),
        taskPill(e, tasks)),
      e.kind === "comment" || e.kind === "created" ? commentBody(e) : e.kind === "commit" ? commitLine(e) : changePills(e)));
}

function eventMatches(e, tasks, q) {
  if (!q) return true;
  const hay = `${e.issue_number} ${(tasks[e.issue_number] || {}).title || ""} ${e.employee || ""} ${e.field_name} ${e.old_value || ""} ${e.new_value || ""} ${plainText(e.body)}`.toLowerCase();
  return q.split(/\s+/).every((word) => hay.includes(word));
}

// Modal listing the watched tasks; "Stop watching" here removes a task and deletes its stored events.
function openWatchlist(initial, onClose) {
  const items = initial.slice();
  const dialog = h("dialog", { class: "tl-dialog tl-watch-dialog", "aria-label": "Watchlist" });
  const list = h("ul", { class: "tl-watch-list" });
  const close = () => dialog.close();
  const draw = () => {
    put(list, items.length ? items.map((w) => h("li", {},
      h("span", { class: "tl-watch-task", style: taskStyle(w.issue_number) }, h("i", { "aria-hidden": "true" }), h("b", {}, `#${w.issue_number}`), h("span", { class: "tl-watch-title" }, w.title || ""), openInTimeline(w.issue_number, w.url), supportTag(w.issue_number)),
      h("small", {}, `${w.events} events`),
      h("button", { type: "button", class: "btn ghost small", onclick: async (ev) => {
        ev.currentTarget.disabled = true;
        try {
          const r = await api(`/api/watchlist/${w.issue_number}`, { method: "DELETE", headers: writeHeaders });
          toast(`Stopped watching #${w.issue_number}. ${plural(r.deleted_events ?? w.events, "event")} deleted.`);
          items.splice(items.indexOf(w), 1);
          cache.watched.delete(String(w.issue_number));
          eventsState.issues.delete(w.issue_number);
          draw();
        } catch (error) { ev.currentTarget.disabled = false; toast(error.message, true); }
      } }, "Stop watching")))
      : h("li", { class: "note" }, "No tasks are watched."));
  };
  draw();
  put(dialog, h("header", {}, h("h2", {}, "Watchlist"), h("button", { type: "button", class: "tl-dialog-x", "aria-label": "Close", onclick: close }, "×")),
    h("p", { class: "note" }, "Stopping a watch removes the task and deletes its recorded events."), list,
    h("footer", {}, h("button", { type: "button", class: "btn", onclick: close }, "Done")));
  dialog.addEventListener("click", (ev) => { if (ev.target === dialog) close(); });
  dialog.addEventListener("close", () => { dialog.remove(); onClose(); });
  document.body.append(dialog);
  dialog.showModal();
}

async function showTimeline(force) {
  const token = ++eventsToken;
  stopLive();
  loading(force ? "Syncing watched tasks…" : "Loading the timeline…");
  if (force) {
    try {
      const r = await api("/api/events/sync", { method: "POST", headers: writeHeaders });
      if (r && r.new_events) toast(`${plural(r.new_events, "new event")} added.`);
    } catch (error) { toast(error.message, true); }
  }
  const query = (before) => {
    const q = new URLSearchParams({ limit: "100", kinds: [...eventsState.kinds].join(",") });
    if (eventsState.issues.size) q.set("issues", [...eventsState.issues].join(","));
    if (before) q.set("before", before);
    return api(`/api/events?${q}`);
  };
  const [first, , marker] = await Promise.all([query(), loadSupportIds(), api("/api/events/marker")]);
  if (token !== eventsToken) return;
  const active = first.watchlist.filter((w) => w.active);
  cache.watched = new Set(active.map((w) => String(w.issue_number)));
  // A remembered task that is no longer watched would leave the feed empty.
  for (const issue of [...eventsState.issues]) if (!cache.watched.has(String(issue))) eventsState.issues.delete(issue);
  const redraw = () => { saveFilters(); return showTimeline(false); };

  // ---- summary tiles (refilled by the live refresh)
  const summary = h("section", { class: "tl-summary", "aria-label": "Summary" });
  const fillSummary = (active) => {
  const lastSync = active.map((w) => w.last_synced_at).filter(Boolean).sort().pop();
  const totalEvents = active.reduce((n, w) => n + w.events, 0);
  const tile = (value, label, sub) => h("div", { class: "tl-tile" }, h("b", {}, value), h("span", {}, label), sub ? h("small", {}, sub) : null);
  const errors = active.filter((w) => w.last_error);
  // Hovering (or focusing) the count lists every watched task.
  const watchedTile = tile(active.length, active.length === 1 ? "task watched" : "tasks watched");
  if (active.length) {
    watchedTile.classList.add("has-hover");
    watchedTile.tabIndex = 0;
    watchedTile.append(h("ul", { class: "tl-hover-list", "aria-label": "Watched tasks" }, active.map((w) => h("li", {},
      h("strong", {}, `#${w.issue_number}`), " ", w.title || "", " ", openInTimeline(w.issue_number, w.url), supportTag(w.issue_number)))));
  }
  put(summary,
    watchedTile,
    tile(totalEvents, "events recorded"),
    tile(lastSync ? clock(lastSync) : "—", "last synced", lastSync ? ago(lastSync) : "press Refresh"),
    errors.length ? h("div", { class: "tl-tile bad" }, h("b", {}, "!"), h("span", {}, errors.map((w) => `#${w.issue_number}: ${w.last_error}`).join(" · "))) : null);
  };
  fillSummary(active);

  // ---- filters
  const kindFilters = h("div", { class: "tl-kinds", role: "group", "aria-label": "Event types" },
    Object.entries(EVENT_KINDS).map(([key, k]) => h("button", { type: "button", class: `tl-kind ${key}`,
      "aria-pressed": String(eventsState.kinds.has(key)),
      title: key === "commit" ? "Commits linked to the task (hidden by default)" : null,
      onclick: () => { if (eventsState.kinds.has(key)) eventsState.kinds.delete(key); else eventsState.kinds.add(key); redraw(); } },
      svgIcon(k.icon, 14), k.label)));
  const search = h("input", { type: "search", class: "tl-search", placeholder: "Search the loaded events…", "aria-label": "Search events", value: eventsState.q });
  const chips = h("div", { class: "tl-chips", role: "group", "aria-label": "Filter by task" });
  const fillChips = (active) => put(chips,
    active.map((w) => h("span", { class: "tl-chip", style: taskStyle(w.issue_number) },
      h("button", { type: "button", class: "tl-chip-main", "aria-pressed": String(eventsState.issues.has(w.issue_number)),
        title: `Show only #${w.issue_number}${w.title ? ` · ${w.title}` : ""}${w.last_synced_at ? ` (synced ${niceTime(w.last_synced_at)})` : ""}`,
        onclick: () => {
          const wasOn = eventsState.issues.has(w.issue_number);
          eventsState.issues.clear();
          if (!wasOn) eventsState.issues.add(w.issue_number);
          redraw();
        } },
        h("i", { "aria-hidden": "true" }), h("b", {}, `#${w.issue_number}`), supportTag(w.issue_number), h("small", {}, w.events)))));
  fillChips(active);

  // Watchlist dialog: the only place a task is removed from the watchlist.
  const watchlistButton = h("button", { type: "button", class: "btn ghost", onclick: () => openWatchlist(active, redraw) }, "Watchlist");

  // ---- feed
  const loaded = [];
  const tasks = {};
  let cursor = null;
  const fresh = new Set(); // ids that just arrived live; they glow briefly
  const feed = h("ol", { class: "tl-feed" });
  const more = h("div", { class: "tl-footer" });
  const empty = h("div", { class: "tl-empty" });
  const render = () => {
    const q = eventsState.q.trim().toLowerCase();
    const rows = [];
    let day = null, dayCount = 0, header = null;
    const countFor = (date) => loaded.filter((e) => e.occurred_at.startsWith(date) && eventMatches(e, tasks, q)).length;
    for (const e of loaded) {
      if (!eventMatches(e, tasks, q)) continue;
      const date = e.occurred_at.slice(0, 10);
      if (date !== day) {
        day = date;
        rows.push(h("li", { class: "tl-day" }, h("h2", {}, niceDate(date), dayNote(date) ? h("em", {}, dayNote(date)) : null),
          h("span", { class: "tl-count" }, `${countFor(date)} event${countFor(date) === 1 ? "" : "s"}`)));
      }
      const card = eventCard(e, tasks);
      if (fresh.has(e.id)) card.classList.add("tl-live");
      rows.push(card);
    }
    put(feed, rows);
    put(empty, rows.length ? null : h("p", { class: "note" }, q ? "No loaded event matches your search." : "No events match these filters."));
    put(more, cursor ? h("button", { type: "button", class: "btn ghost", onclick: async (ev) => {
      ev.currentTarget.disabled = true;
      try { add(await query(cursor)); } catch (error) { toast(error.message, true); }
    } }, "Load older events") : (loaded.length ? h("p", { class: "note" }, "That's the whole history.") : null));
  };
  const add = (page) => { loaded.push(...page.events); Object.assign(tasks, page.tasks); cursor = page.next; render(); };
  search.addEventListener("input", () => { eventsState.q = search.value; render(); });
  add(first);

  // Live: merge events that arrived since the page was drawn, without losing scroll, filters or older pages.
  const liveRefresh = async () => {
    const page = await query();
    if (token !== eventsToken) return;
    const now = page.watchlist.filter((w) => w.active);
    cache.watched = new Set(now.map((w) => String(w.issue_number)));
    fillSummary(now);
    fillChips(now);
    const seen = new Set(loaded.map((e) => e.id));
    const arrived = page.events.filter((e) => !seen.has(e.id));
    if (!arrived.length) return;
    for (const e of arrived) {
      fresh.add(e.id);
      setTimeout(() => fresh.delete(e.id), 8000);
    }
    const perTask = new Map();
    for (const e of arrived) perTask.set(e.issue_number, (perTask.get(e.issue_number) || 0) + 1);
    toast(`${plural(arrived.length, "new event")} added: ${[...perTask].map(([i, n]) => `#${i}${n > 1 ? ` ×${n}` : ""}`).join(", ")}`);
    loaded.push(...arrived);
    loaded.sort((a, b) => b.occurred_at.localeCompare(a.occurred_at) || b.id - a.id);
    Object.assign(tasks, page.tasks);
    render();
  };

  if (!active.length) {
    put(view, h("div", { class: "tl-hero" },
      h("div", { class: "tl-hero-star", "aria-hidden": "true" }, "★"),
      h("h2", {}, "Nothing is being watched yet"),
      h("p", {}, "Watch a task and every status change, comment, deadline edit and commit lands here, in time order, across all your tasks."),
      h("a", { class: "btn", href: "#/task" }, "Pick tasks to watch")));
    return;
  }
  put(view, h("div", { class: "tl-page" },
    h("div", { class: "tl-head" }, summary, h("div", { class: "tl-controls" }, kindFilters, search, watchlistButton)),
    chips, feed, empty, more));
  startLive(marker, liveRefresh);
}

// ---- release --------------------------------------------------------------
// Pending duell-admin PRs, grouped by the branch they merge into.
const VERDICT_CLASS = { ready: "ok", "needs notes": "soon", blocked: "overdue" };

function parseIds(text) {
  return [...new Set((text.match(/\/edit\/(\d+)|\b\d{2,}\b/g) || []).map((m) => m.replace("/edit/", "")))];
}

function clock(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

function prCard(pr) {
  const checks = pr.checks;
  const ci = checks.failed.length ? `CI failing: ${checks.failed.join(", ")}`
    : checks.pending.length ? "CI running"
    : checks.total ? "CI passed" : "No CI";
  const notes = [...pr.problems];
  if (pr.base === "master" && pr.via_qa === "no") notes.unshift("Direct to master, not via QA");
  if (pr.base === "master" && pr.via_qa === "partly") {
    const n = pr.commits_not_on_qa;
    notes.unshift(n ? `${n} commit${n === 1 ? "" : "s"} not on qa-release yet` : "Latest commits aren't on qa-release");
  }
  if (pr.verdict === "blocked") notes.unshift(pr.reason);
  // The task's workflow should be at the matching release step.
  const flow = pr.issue_id && cache.workflows && cache.workflows.get(String(pr.issue_id));
  const BEFORE_QA = ["intake", "estimate", "develop", "review", "release_qa"];
  if (flow && flow.state === "active" && pr.base === "qa-release" && BEFORE_QA.includes(flow.step)) {
    notes.push(`Workflow is still at ${flow.step_title}`);
  }
  if (flow && flow.state === "active" && pr.base === "master" && ![ "release_prod", "close", "prod_fix", "release_hotfix"].includes(flow.step)) {
    notes.push(`Workflow is at ${flow.step_title}, not Release to prod`);
  }
  const stage = pr.status && !pr.status.error ? pr.status.stage_status : null;
  return h("article", { class: "card pr" },
    h("div", { class: "head" },
      h("a", { href: pr.url, target: "_blank", rel: "noopener noreferrer", class: "issue" }, `PR ${pr.number} ↗`),
      pr.issue_id ? h("a", { href: `#/task/${pr.issue_id}`, class: "issue" }, `#${pr.issue_id}`) : h("span", { class: "badge" }, "hotfix"),
      h("span", { class: `badge ${VERDICT_CLASS[pr.verdict]}` }, pr.verdict),
      flow ? h("a", { href: `#/task/${pr.issue_id}`, class: `pill ${flow.status.code}`, title: flow.status.label }, flow.status.short) : null),
    h("p", { class: "title" }, pr.title),
    h("p", { class: "stage" },
      [`@${pr.author}`, `${pr.age_days ?? "?"}d`, ci, stage && `Timeline: ${stage}`].filter(Boolean).join(" · ")),
    notes.length ? h("ul", { class: "problems" }, notes.map((n) => h("li", {}, n))) : null,
    pr.body ? h("pre", { class: "body pr-body" }, pr.body) : h("p", { class: "note" }, "No description."));
}

function renderPrs(report, results, onChange) {
  put(results, guardPanel(report.guard, onChange), h("div", { class: "pr-columns" }, ["qa-release", "master"].map((base) => {
    const prs = report.prs[base];
    return h("section", { class: "lane" },
      h("h2", {}, `Into ${base}`, h("span", { class: "count" }, prs.length)),
      prs.length ? prs.map(prCard) : h("p", { class: "note" }, "No pending PRs."));
  })));
}

async function checkRelease(results, stamp, button, fetchFresh) {
  button.disabled = true;
  stamp.textContent = fetchFresh ? "Fetching from GitHub… (about 10 s)" : "Loading…";
  try {
    let { report } = await api("/api/release");
    // An old report would say "all PRs in place" about yesterday, so a stale one is rebuilt.
    const stale = report && Date.now() - new Date(report.generated_at) > RELEASE_STALE_MS;
    if (!report || fetchFresh || stale) {
      stamp.textContent = "Fetching from GitHub… (about 10 s)";
      ({ report } = await api("/api/release", { method: "POST", headers: { "X-Timeline-Desk": "1" } }));
    }
    await getWorkflows(true);
    renderPrs(report, results, () => checkRelease(results, stamp, button, true));
    await getGuard();
    stamp.textContent = `Checked at ${clock(report.generated_at)}`;
  } catch (error) {
    stamp.textContent = "";
    put(results, h("p", { class: "error", role: "alert" }, error.message));
  } finally {
    button.disabled = false;
  }
}

async function showRelease(force) {
  const results = h("div", { id: "release-results" });
  const stamp = h("span", { class: "note" });
  const check = h("button", { type: "button", class: "btn" }, "Check again");
  check.addEventListener("click", () => checkRelease(results, stamp, check, true));
  put(view, h("div", { class: "toolbar" }, check, stamp), results);
  await checkRelease(results, stamp, check, Boolean(force));
}

// ---- PR guard: does each ticket's stage match its pull requests? ---------------
// QA in progress needs a PR into qa-release; QA done / live pending one into master;
// live a merged one. The report is built with the Release page (see release_check.pr_guard).
const RELEASE_STALE_MS = 10 * 60 * 1000;
const GUARD_STALE_MS = 30 * 60 * 1000;
const GUARD_RETRY_MS = 10 * 60 * 1000;
const GUARD_BADGE = { ok: ["PRs ok", "ok"], warn: ["Check PR", "soon"], missing: ["No PR", "overdue"] };
const guardState = { busy: false, failedAt: 0 };

async function getGuard() {
  try {
    const r = await api("/api/pr-guard");
    cache.guard = r.guard ? { rows: new Map(r.guard.rows.map((row) => [row.issue_id, row])), at: r.generated_at } : null;
  } catch {
    cache.guard = null;
  }
  return cache.guard;
}

const guardSlot = (issue) => h("span", { class: "pr-slot", "data-issue": String(issue) });
const guardBox = (issue, title) => h("section", { class: "wf pr-box", "data-issue": String(issue), "data-title": title || "", hidden: true });

function guardBadge(row) {
  const [text, tone] = GUARD_BADGE[row.level];
  const checked = cache.guard && cache.guard.at ? ` · checked ${ago(cache.guard.at)}` : "";
  return h("a", { class: `badge ${tone}`, href: row.level === "ok" ? "#/release" : `#/task/${row.issue_id}`, title: `${row.summary}${checked}` }, text);
}

function fillGuardSlots() {
  for (const slot of document.querySelectorAll(".pr-slot")) {
    const row = cache.guard && cache.guard.rows.get(slot.dataset.issue);
    put(slot, row ? guardBadge(row) : null);
  }
  for (const box of document.querySelectorAll(".pr-box")) {
    const row = cache.guard && cache.guard.rows.get(box.dataset.issue);
    box.hidden = !row;
    put(box, row ? [h("h3", {}, "Pull requests"), guardRows([row], box.dataset.title, () => refreshGuard())] : null);
  }
}

// Shows the cached guard on the page at once; when it is old (or missing) rebuilds it once in the background.
async function trackGuard() {
  await getGuard();
  fillGuardSlots();
  const age = cache.guard && cache.guard.at ? Date.now() - new Date(cache.guard.at) : Infinity;
  if (age < GUARD_STALE_MS || guardState.busy || Date.now() - guardState.failedAt < GUARD_RETRY_MS) return;
  await refreshGuard(true);
}

async function refreshGuard(quiet) {
  if (guardState.busy) return;
  guardState.busy = true;
  try {
    await api("/api/release", { method: "POST", headers: { "X-Timeline-Desk": "1" } });
    await getGuard();
    fillGuardSlots();
  } catch (error) {
    guardState.failedAt = Date.now();
    if (!quiet) toast(error.message, true);
  } finally {
    guardState.busy = false;
  }
}

// ---- the readiness table ----------------------------------------------------
// One row per ticket, one cell per repository. Each line in a cell is "QA" or "Production" and what is
// there (merged, open, missing). Plain words: qa-release is shown as QA, master as Production.
const ENV_LABEL = { "qa-release": "QA", master: "Production", "prod-release": "Production" };
const LEVEL_ICON = { ok: ["✓", "Fine"], warn: ["!", "Check"], missing: ["✕", "Missing"], none: ["–", "Not used here"], unknown: ["?", "Not checked"] };

const prIcon = (level) => h("span", { class: `pr-ico ${level}`, role: "img", "aria-label": LEVEL_ICON[level][1], title: LEVEL_ICON[level][1] }, LEVEL_ICON[level][0]);

function checkLine(row, check, title, onChange) {
  const pr = check.pr;
  const link = pr && pr.url && /^https:\/\//.test(pr.url) ? h("a", { href: pr.url, target: "_blank", rel: "noopener noreferrer" }, `PR ${pr.number} ↗`) : null;
  let text;
  if (pr) text = pr.state === "merged" ? "merged" : "open, not merged yet";
  else if (check.level === "ok") text = check.message.charAt(0).toLowerCase() + check.message.slice(1);
  else text = check.no_branch ? "no PR, and no branch yet" : "no PR yet";
  const create = check.can_create
    ? h("button", { type: "button", class: "btn small", title: `Create a PR in ${check.repo} into ${check.base}`, onclick: async (e) => {
      e.target.disabled = true;
      const made = await createPr(check.repo, row.issue_id, title || row.title, check.base);
      e.target.disabled = false;
      if (made) await onChange();
    } }, "Create PR")
    : null;
  return h("li", { class: `pr-line ${check.level}`, title: `${check.repo}: ${check.message}` },
    prIcon(check.level),
    h("span", { class: "pr-line-text" }, h("strong", {}, ENV_LABEL[check.base] || check.base), " ", h("code", { class: "pr-base", title: `${check.repo}: PRs are checked against the ${check.base} branch` }, `→ ${check.base}`), " ", text, link ? " · " : null, link),
    create);
}

function guardCell(row, section, title, onChange) {
  const nowhere = section.level === "missing" && !section.checks.length;
  let body;
  if (section.level === "none" || section.level === "unknown") {
    body = h("p", { class: "pr-cell-note", title: section.note }, prIcon(section.level), section.level === "none" ? "Not used here" : "Couldn't check");
  } else if (nowhere) {
    body = h("p", { class: "pr-line missing" }, prIcon("missing"), h("span", { class: "pr-line-text" }, "No branch or PR yet"));
  } else {
    body = h("ul", { class: "pr-lines" }, section.checks.map((check) => checkLine(row, check, title, onChange)));
  }
  const extra = section.branches.length - 1;
  return h("section", { class: `pr-cell ${section.level}`, "aria-label": section.repo },
    h("div", { class: "pr-cell-head" },
      h("strong", {}, section.repo),
      section.branches.length ? h("span", { class: "pr-branch", title: section.branches.join("\n") }, `branch ${section.branches[0]}${extra > 0 ? ` +${extra}` : ""}`) : null),
    body);
}

function guardRows(rows, title, onChange) {
  return h("ul", { class: "pr-rows" }, rows.map((row) => {
    const nowhere = row.sections.every((section) => section.level === "missing" && !section.checks.length);
    return h("li", { class: `pr-row ${row.level}` },
      h("div", { class: "pr-ticket" },
        h("a", { class: "issue", href: `#/task/${row.issue_id}` }, `#${row.issue_id}`),
        h("span", { class: "pr-row-title" }, row.title),
        h("span", { class: "chip muted" }, row.stage_status || RELEASE_LABEL[row.release_state] || row.release_state)),
      h("div", { class: "pr-cells" }, row.sections.map((section) => guardCell(row, section, title, onChange))),
      nowhere ? h("p", { class: "pr-hint" }, `Push a branch with ${row.issue_id} in its name to duell-admin or duell-pos, then press Check again.`) : null);
  }));
}

// "Release readiness" at the top of the Release page: tickets in QA or going live, and what their PRs look like.
function guardPanel(guard, onChange) {
  if (!guard) return null;
  const { ok, warn, missing } = guard.counts;
  const need = guard.rows.filter((r) => r.level !== "ok");
  const fine = guard.rows.filter((r) => r.level === "ok");
  const count = missing + warn;
  const verdict = !guard.checked ? "None of your tickets are in QA or waiting to go live."
    : count ? `${count} ticket${count === 1 ? " needs" : "s need"} action, ${ok} ${ok === 1 ? "is" : "are"} fine.`
    : `All ${guard.checked} tickets have the PRs they need.`;
  return h("section", { class: `pr-guard ${missing ? "missing" : warn ? "warn" : "ok"}`, "aria-label": "Release readiness" },
    h("h2", {}, "Release readiness"),
    h("p", { class: "summary" }, verdict),
    h("p", { class: "pr-legend" },
      [["ok", "fine"], ["warn", "check"], ["missing", "missing"], ["none", "not used here"]].map(([level, text]) => h("span", {}, prIcon(level), ` ${text}`))),
    guard.timeline_error ? h("p", { class: "error", role: "alert" }, `Timeline stages are missing (${guard.timeline_error}), so tickets could not be checked.`) : null,
    Object.entries(guard.errors || {}).map(([repo, message]) => h("p", { class: "error", role: "alert" }, `${repo} could not be checked: ${message}`)),
    need.length ? [h("h3", { class: "pr-group" }, "Needs action"), guardRows(need, null, onChange)] : null,
    fine.length ? h("details", { class: "pr-fine" }, h("summary", {}, `${fine.length} ticket${fine.length === 1 ? "" : "s"} fine`), guardRows(fine, null, onChange)) : null,
    h("p", { class: "note" }, "Only your tickets: the ones in your planner, on your watchlist or with a workflow. Each line names the branch it checks (→ qa-release is the QA server; → master in duell-admin and → prod-release in duell-pos are Production). The branch in a cell's header is the ticket's own branch. Checked from each ticket's Timeline stage against the branches and PRs in both repositories. The desk never pushes: a branch has to be on GitHub before a PR can be created."));
}

async function createPr(repo, issue, title, base) {
  let draft;
  try {
    draft = await api(`/api/pr/draft?repo=${encodeURIComponent(repo)}&issue=${encodeURIComponent(issue)}&base=${encodeURIComponent(base)}&title=${encodeURIComponent(title || "")}`);
  } catch (error) {
    toast(error.message, true);
    return false;
  }
  if (draft.existing.length) {
    toast(`PR #${draft.existing[0].number} into ${repo} ${base} is already open. Check again to update this page.`, true);
    return false;
  }
  if (!draft.branches.length) {
    toast(`No branch for #${issue} in ${repo} on GitHub yet. Push it first (the desk never pushes), then try again.`, true);
    return false;
  }
  const values = await prDialog({ ...draft, ticket_title: title || "" });
  if (!values) return false;
  try {
    const made = await api("/api/pr/create", {
      method: "POST",
      headers: { "X-Timeline-Desk": "1", "Content-Type": "application/json" },
      body: JSON.stringify({ repo, issue: String(issue), base, ...values }),
    });
    toast(`Created PR ${made.number} into ${repo} ${base}.`);
    return true;
  } catch (error) {
    toast(error.message, true);
    return false;
  }
}

const PR_SOURCE_TEXT = { commits: "Drafted from the branch's commit messages.", title: "Only the heading is drafted: add what changed." };

// The create-PR dialog: title, branch and description are yours to edit. Resolves {head, title, body}, or null.
function prDialog(draft) {
  return new Promise((resolve) => {
    let result = null;
    const dialog = h("dialog", { class: "tl-dialog tl-modal pr-dialog", "aria-label": `Create a PR into ${draft.repo} ${draft.base}` });
    const entities = draft.repo === "duell-admin"; // only duell-admin has .env.dist and entity files to look at
    const title = h("input", { type: "text", value: draft.title, maxlength: "250", "aria-label": "PR title" });
    const body = h("textarea", { rows: "10", spellcheck: "false", "aria-label": "PR description" }, draft.body);
    const branch = h("select", { "aria-label": "Branch" }, draft.branches.map((b) => h("option", { value: b, selected: b === draft.head }, b)));
    const problem = h("p", { class: "tl-modal-problem", role: "alert", hidden: true });
    const cancel = () => dialog.close();
    // Asks Claude (claude -p, no tools) to word the description from the ticket title and the branch's commit messages.
    const ai = h("button", { type: "button", class: "btn ghost small", title: entities ? "Sends the ticket title, the branch's commit messages and the diffs of changed entity files to Claude" : "Sends the ticket title and the branch's commit messages to Claude" }, "Write with AI");
    // What the branch changes against the base: .env.dist variables (added to the description with empty
    // values) and Doctrine entities (the AI writes their SQL).
    const findings = h("div", { class: "pr-findings", hidden: true });
    const showFindings = (analysis, warnings, sql) => {
      const lines = [];
      if (analysis) {
        const vars = [...analysis.env.added, ...analysis.env.changed];
        if (vars.length) lines.push(`.env.dist: ${vars.length} new or changed variable${vars.length === 1 ? "" : "s"} (${vars.join(", ")}), added below with empty values to fill in.`);
        if (analysis.env.removed.length) lines.push(`.env.dist: removed ${analysis.env.removed.join(", ")}.`);
        if (analysis.entities.length) {
          const names = analysis.entities.map((e) => `${e.path.split("/").pop().replace(/\.php$/, "")} (${e.db})`).join(", ");
          lines.push(sql ? `Entity changes: ${names}. SQL added below (${sql} statement${sql === 1 ? "" : "s"}): check it.`
            : `Entity changes: ${names}. Press Write with AI to add the SQL.`);
        }
      }
      for (const w of warnings || []) lines.push(w);
      put(findings, lines.map((line) => h("p", {}, line)));
      findings.hidden = !lines.length;
    };
    showFindings(draft.analysis, draft.warnings, 0);
    ai.addEventListener("click", async () => {
      ai.disabled = true;
      ai.textContent = "Writing… (up to a minute)";
      problem.hidden = true;
      try {
        const r = await api("/api/pr/describe", {
          method: "POST",
          headers: { "X-Timeline-Desk": "1", "Content-Type": "application/json" },
          body: JSON.stringify({ repo: draft.repo, issue: draft.issue_id, base: draft.base, head: branch.value, title: draft.ticket_title }),
        });
        body.value = r.body;
        showFindings(r.analysis, r.warnings, r.sql);
        toast(`Description written from ${r.commits} commit message${r.commits === 1 ? "" : "s"}. Check it before creating.`);
      } catch (error) {
        problem.textContent = error.message;
        problem.hidden = false;
      } finally {
        ai.disabled = false;
        ai.textContent = "Write with AI";
      }
    });
    const form = h("form", { method: "dialog", onsubmit: (ev) => {
      ev.preventDefault();
      if (!title.value.trim()) { problem.textContent = "Give the PR a title."; problem.hidden = false; title.focus(); return; }
      if (!new RegExp(`^\\s*-\\s*#?${draft.issue_id}\\s*-\\s*\\S`, "m").test(body.value)) {
        problem.textContent = `The description needs a "- #${draft.issue_id} - <title>" line.`; problem.hidden = false; body.focus(); return;
      }
      result = { head: branch.value, title: title.value.trim(), body: body.value };
      dialog.close();
    } },
    h("header", {}, h("h2", {}, `Create a PR in ${draft.repo}`), h("button", { type: "button", class: "tl-dialog-x", "aria-label": "Close", onclick: cancel }, "×")),
    h("p", { class: "tl-modal-text" }, `Opens a pull request on GitHub in ${draft.repo} for #${draft.issue_id}. Nothing is pushed or merged. ${PR_SOURCE_TEXT[draft.source] || `Description taken from ${draft.source}.`}`),
    h("label", {}, "Title", title),
    h("label", {}, `Branch → ${draft.base}`, branch),
    h("label", {}, "Description", body),
    h("div", { class: "pr-ai" }, ai, h("span", { class: "note" }, entities
      ? "Uses your Claude: it gets the ticket title, the commit messages and the diffs of changed entity files (for the SQL), nothing else."
      : "Uses your Claude: it gets the ticket title and the commit messages, nothing else.")),
    findings,
    problem,
    h("footer", {},
      h("button", { type: "button", class: "btn ghost", onclick: cancel }, "Cancel"),
      h("button", { type: "submit", class: "btn" }, `Create PR into ${draft.base}`)));
    body.addEventListener("keydown", (ev) => { if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) form.requestSubmit(); });
    let pressed = null;
    dialog.addEventListener("pointerdown", (ev) => { pressed = ev.target; });
    dialog.addEventListener("click", (ev) => { if (ev.target === dialog && pressed === dialog) cancel(); });
    dialog.addEventListener("close", () => { dialog.remove(); resolve(result); });
    dialog.append(form);
    document.body.append(dialog);
    dialog.showModal();
  });
}

// ---- worktrees -------------------------------------------------------------
// Each workflow works in its own git worktree of the task's repo.
async function getBoards() {
  if (!cache.boards) cache.boards = (await api("/api/work/config").catch(() => ({ boards: {} }))).boards;
  return cache.boards;
}

function toast(text, isError) {
  let stack = document.querySelector(".toasts");
  if (!stack) { stack = h("div", { class: "toasts", "aria-live": "polite" }); document.body.append(stack); }
  const el = h("div", { class: `toast${isError ? " bad" : ""}`, role: isError ? "alert" : "status" }, text);
  stack.append(el);
  setTimeout(() => el.remove(), isError ? 9000 : 6000);
}

// The desk's own dialog, used everywhere instead of window.confirm / prompt.
// confirmDialog resolves true or false; askDialog resolves the trimmed text,
// or null when cancelled (a `required` message refuses an empty answer).
// Esc, ×, Cancel and a click on the backdrop cancel; Ctrl+Enter sends.
function modalDialog({ title, text, ok = "OK", danger = false, field = null }) {
  return new Promise((resolve) => {
    let result = field ? null : false;
    const dialog = h("dialog", { class: "tl-dialog tl-modal", "aria-label": title });
    const input = field ? h("textarea", { rows: "3", placeholder: field.placeholder || null, autofocus: true }) : null;
    const problem = h("p", { class: "tl-modal-problem", role: "alert", hidden: true });
    const cancel = () => dialog.close();
    const form = h("form", { method: "dialog", onsubmit: (ev) => {
      ev.preventDefault();
      if (input) {
        const value = input.value.trim();
        if (!value && field.required) { problem.textContent = field.required; problem.hidden = false; input.focus(); return; }
        result = value;
      } else result = true;
      dialog.close();
    } },
    h("header", {}, h("h2", {}, title), h("button", { type: "button", class: "tl-dialog-x", "aria-label": "Close", onclick: cancel }, "×")),
    text ? h("p", { class: "tl-modal-text" }, text) : null,
    input ? h("label", {}, field.label || "Your answer", input) : null,
    problem,
    h("footer", {},
      h("button", { type: "button", class: "btn ghost", onclick: cancel, autofocus: !input && danger }, "Cancel"),
      h("button", { type: "submit", class: `btn${danger ? " danger" : ""}`, autofocus: !input && !danger }, ok)));
    if (input) input.addEventListener("keydown", (ev) => { if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) form.requestSubmit(); });
    // Only a press that starts and ends on the backdrop cancels, so selecting
    // text in the field and letting go outside doesn't lose what you typed.
    let pressed = null;
    dialog.addEventListener("pointerdown", (ev) => { pressed = ev.target; });
    dialog.addEventListener("click", (ev) => { if (ev.target === dialog && pressed === dialog) cancel(); });
    dialog.addEventListener("close", () => { dialog.remove(); resolve(result); });
    dialog.append(form);
    document.body.append(dialog);
    dialog.showModal();
  });
}

const confirmDialog = (options) => modalDialog(options);
const askDialog = ({ label, placeholder, required, ...options }) => modalDialog({ ...options, field: { label, placeholder, required } });

// The workflow badge on a board card, task row or worktree: where the task's
// workflow stands (from /api/workflows), linking to the task page, which is
// the only place a step is run or decided.
async function getWorkflows(force) {
  if (!cache.workflows || force) {
    const r = await api("/api/workflows").catch(() => ({ workflows: [] }));
    cache.workflows = new Map(r.workflows.map((w) => [String(w.issue), w]));
  }
  return cache.workflows;
}

function workflowLink(issue, code, boards) {
  const flow = cache.workflows && cache.workflows.get(String(issue));
  if (!flow && (!code || !boards[String(code)])) return null;
  return h("a", {
    class: `start wf-${flow ? flow.status.code : "none"}`,
    href: `#/task/${issue}`,
    title: flow ? `Workflow: ${flow.status.label}` : `No workflow yet · ${boards[String(code)]}`,
  }, flow ? flow.status.short : "+ Workflow");
}

function sizeText(bytes) {
  return bytes >= 1048576 ? `${(bytes / 1048576).toFixed(0)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

async function removeWorktree(item, withBranch, button) {
  const ok = await confirmDialog({
    title: `Remove the worktree for #${item.issue_id || item.branch}?`,
    text: withBranch ? `Its branch ${item.branch} is deleted too.` : `The branch ${item.branch} stays.`,
    ok: withBranch ? "Remove worktree and branch" : "Remove worktree", danger: true });
  if (!ok) return;
  button.disabled = true;
  try {
    const r = await api("/api/work/remove", {
      method: "POST",
      headers: { "X-Timeline-Desk": "1", "Content-Type": "application/json" },
      body: JSON.stringify({ path: item.path, delete_branch: withBranch }),
    });
    toast(r.branch_note || `Removed ${item.branch}${r.branch_deleted ? " and its branch" : ""}.`, Boolean(r.branch_note));
  } catch (error) {
    toast(error.message, true);
  }
  showWork();
}

async function showWork() {
  loading("Loading task worktrees…");
  const [{ worktrees }, boards] = await Promise.all([api("/api/work/worktrees"), getBoards(), getWorkflows(true)]);
  if (!Object.keys(boards).length) {
    put(view, h("p", { class: "note" }, "No repos set up. Copy timeline-desk.example.json to timeline-desk.json and add your board → repo paths."));
    return;
  }
  const total = worktrees.reduce((sum, w) => sum + w.size, 0);
  const rows = worktrees.map((w) => {
    const facts = [];
    if (!w.exists) facts.push("folder missing");
    if (w.dirty) facts.push(`${w.dirty} uncommitted`);
    if (w.unpushed) facts.push(`${w.unpushed} commit${w.unpushed === 1 ? "" : "s"} only here`);
    if (w.merged) facts.push(`no new commits vs base`);
    if (!facts.length) facts.push("clean");
    const remove = h("button", { type: "button", class: "btn ghost small" }, "Remove");
    remove.addEventListener("click", () => removeWorktree(w, false, remove));
    const both = h("button", { type: "button", class: "btn ghost small" }, "Remove + branch");
    both.addEventListener("click", () => removeWorktree(w, true, both));
    if (w.dirty) { remove.disabled = true; both.disabled = true; remove.title = both.title = "Commit or discard the changes first"; }
    return h("tr", {},
      h("td", {}, w.issue_id ? h("a", { href: `#/task/${w.issue_id}`, class: "issue" }, `#${w.issue_id}`) : "—"),
      h("td", {}, h("code", {}, w.branch || "(detached)"), h("div", { class: "note" }, w.repo)),
      h("td", {}, sizeText(w.size)),
      h("td", { class: w.dirty || w.unpushed ? "warn-text" : "" }, facts.join(" · ")),
      h("td", { class: "actions" },
        w.issue_id ? workflowLink(w.issue_id, w.code, boards) : null, remove, both));
  });
  put(view,
    h("p", { class: "summary" }, h("strong", {}, String(worktrees.length)), ` task worktree${worktrees.length === 1 ? "" : "s"} · ${sizeText(total)}`),
    worktrees.length
      ? h("table", { class: "work" },
        h("thead", {}, h("tr", {}, ["Task", "Branch", "Size", "State", ""].map((t) => h("th", {}, t)))),
        h("tbody", {}, rows))
      : h("p", { class: "note" }, "No task worktrees. Starting a workflow on a task page makes one."),
    h("p", { class: "note" }, "Remove never forces: git keeps a worktree with uncommitted changes and a branch with commits that aren't merged anywhere."));
}

// ---- my tasks (project list) ----------------------------------------------
const listState = { project: null, tab: null, q: "", tag: "", stage: "", all: false, closed: new Set() };
let listToken = 0;
const TAG_COLOR = /^#[0-9a-f]{3,8}$/i;

async function getProjects() {
  if (!cache.projects) cache.projects = (await api("/api/work/config").catch(() => ({ projects: [] }))).projects || [];
  return cache.projects;
}

// Timeline's Support list isn't one of the project's tabs: it is asked for with its own flag.
const SUPPORT_TAB = "Support";

async function getProjectTasks(project, tab, refresh) {
  const key = `${project.id}|${tab || ""}`;
  if (refresh || !cache.projectTasks.has(key)) {
    const query = tab === SUPPORT_TAB ? "?support=1" : tab ? `?tab=${encodeURIComponent(tab)}` : "";
    cache.projectTasks.set(key, await api(`/api/project/${project.id}/tasks${query}`));
  }
  return cache.projectTasks.get(key);
}

function tagChip(tag) {
  const style = TAG_COLOR.test(tag.color || "") ? `--tag: ${tag.color}` : null;
  return h("span", { class: "tag", style }, tag.name);
}

function hoursText(t) {
  const fmt = (n) => (Number.isInteger(n) ? String(n) : n.toFixed(1));
  const spent = Number(t.hours_spent) || 0;
  const estimate = parseFloat(String(t.hours_estimated ?? "").replace(",", "."));
  if (!spent && !(estimate > 0)) return "–";
  return estimate > 0 ? `${fmt(spent)} / ${fmt(estimate)}h` : `${fmt(spent)}h`;
}

// The first few faces, then "+N" naming the rest, so a busy task can't spill into the next column.
const MAX_FACES = 6;
function people(subscribers) {
  const shown = subscribers.slice(0, MAX_FACES), rest = subscribers.slice(MAX_FACES);
  return h("span", { class: "people" },
    shown.map((p) => avatar(p.name, p.photo, p.name)),
    rest.length ? h("span", { class: "avatar more", title: rest.map((p) => p.name).join(", ") }, `+${rest.length}`) : null);
}

let jiraStatuses = new Map();  // issue number -> column on the Jira board
let jiraColumns = [];          // the board's columns, in order
const jiraStatus = (t) => jiraStatuses.get(String(t.issue_number)) || "";

const JIRA_HUES = [215, 40, 185, 270, 320, 5, 75, 140];  // spread apart so neighbouring columns never look alike
const jiraHue = (status, columns) => { const i = columns.indexOf(status); return i < JIRA_HUES.length ? JIRA_HUES[i] : (i * 47) % 360; };

const PENCIL = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';

let jiraMenu = null;
function closeJiraMenu() {
  if (jiraMenu) { jiraMenu.remove(); jiraMenu = null; }
  for (const el of document.querySelectorAll(".jira-edit[aria-expanded]")) el.removeAttribute("aria-expanded");
}
document.addEventListener("click", (e) => { if (jiraMenu && !jiraMenu.contains(e.target)) closeJiraMenu(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeJiraMenu(); });
window.addEventListener("resize", closeJiraMenu);
window.addEventListener("hashchange", closeJiraMenu);

// The Status cell of a task row: the Jira column as a coloured chip (a dash when the task is not on the board)
// and a pencil that moves the card, or adds the task to the board in the column you pick.
function jiraStatusCell(t) {
  const issue = String(t.issue_number);
  const cell = h("span", { class: "stage jira-cell" });
  const draw = () => {
    const status = jiraStatus(t);
    const edit = h("button", {
      type: "button", class: "jira-edit", title: status ? "Change the Jira status" : "Add to the Jira board",
      "aria-label": status ? "Change the Jira status" : "Add to the Jira board",
      onclick: (e) => {
        e.preventDefault(); e.stopPropagation();
        const reopen = jiraMenu && jiraMenu.dataset.issue === issue;
        closeJiraMenu();
        if (!reopen) openMenu(e.currentTarget);
      },
    });
    if (status) {
      edit.appendChild(new DOMParser().parseFromString(PENCIL, "image/svg+xml").documentElement);
      put(cell, h("span", { class: "pill status", style: `--sc:${jiraHue(status, jiraColumns)}`, title: status }, status), edit);
    } else {
      edit.classList.add("jira-addchip");  // a dash until the row is hovered, then a dashed "+ Add" chip
      put(edit, h("span", { class: "dash" }, "–"), h("span", { class: "add" }, "+ Add"));
      put(cell, edit);
    }
  };
  const choose = async (status) => {
    closeJiraMenu();
    const current = jiraStatus(t);
    if (status === current) return;
    try {
      if (current) await postJson(`/api/jira/cards/${encodeURIComponent(issue)}/move`, { status });
      else await postJson("/api/jira/cards", { issues: [issue], status });
      jiraStatuses.set(issue, status);
      draw();
    } catch (err) {
      toast(err.message, true);
    }
  };
  const openMenu = (anchor) => {
    const current = jiraStatus(t);
    jiraMenu = h("div", { class: "jira-menu", role: "menu", "data-issue": issue },
      h("p", { class: "note" }, current ? "Move to" : "Add to board in"),
      jiraColumns.map((c) => h("button", {
        type: "button", role: "menuitem", class: c === current ? "current" : "", style: `--sc:${jiraHue(c, jiraColumns)}`,
        onclick: (e) => { e.preventDefault(); e.stopPropagation(); choose(c); },
      }, c)));
    anchor.setAttribute("aria-expanded", "true");
    document.body.appendChild(jiraMenu);
    const box = anchor.getBoundingClientRect();
    const mh = jiraMenu.offsetHeight;
    jiraMenu.style.left = `${Math.max(8, Math.min(box.left, window.innerWidth - jiraMenu.offsetWidth - 8))}px`;
    jiraMenu.style.top = `${box.bottom + mh + 8 > window.innerHeight ? Math.max(8, box.top - mh - 4) : box.bottom + 4}px`;
  };
  draw();
  return cell;
}

function taskRow(t, boards, support = false) {
  return h("a", { class: `trow${support ? " support" : ""}`, href: `#/task/${t.issue_number}` },
    watchButton(t.issue_number),
    h("span", { class: "issue" }, `#${t.issue_number}`),
    h("div", { class: "ttitle" },
      h("span", { class: "tname" }, t.title, openInTimeline(t.issue_number, t.url), support ? h("span", { class: "badge support" }, "support") : null),
      t.tags.length ? h("span", { class: "tags" }, t.tags.map(tagChip)) : null),
    rail(t),
    jiraStatusCell(t),
    h("span", { class: "tdue" }, t.deadline ? niceDate(t.deadline, false) : "–", flagBadge(t.deadline_flag)),
    h("span", { class: "thours" }, hoursText(t)),
    people(t.subscribers),
    h("span", { class: "tact" }, workflowLink(t.issue_number, t.code, boards)));
}

async function showTaskList(refresh) {
  const projects = await getProjects();
  if (!projects.length) {
    put(view, h("p", { class: "note" }, 'No project set up. Add an "id" (the API project id) to a repo in timeline-desk.json.'));
    return;
  }
  const project = projects.find((p) => p.id === listState.project) || projects[0];
  listState.project = project.id;
  const boards = await getBoards();
  await Promise.all([loadWatched(), getWorkflows(refresh)]);
  const jira = await api("/api/jira/board").catch(() => null);
  jiraStatuses = new Map(jira ? jira.cards.map((c) => [String(c.issue_number), c.status]) : []);
  jiraColumns = jira ? jira.statuses : [];
  const body = h("div", { class: "mytasks" });
  put(view, body);
  const token = ++listToken;
  const outside = new Map(); // issue id -> task fetched by ID that is not in any of my tabs (or an error)
  let otherTabsLoaded = false;

  const draw = (data, baseTabs, support) => {
    const supportIds = new Set(support ? support.groups.flatMap((g) => g.tasks.map((t) => t.issue_number)) : []);
    const tabs = [...baseTabs];
    if (support && !tabs.some((t) => t.name === SUPPORT_TAB)) {
      tabs.push({ name: SUPPORT_TAB, count: support.tabs.find((t) => t.name === SUPPORT_TAB)?.count ?? supportIds.size });
    }
    const all = data.groups.flatMap((g) => g.tasks);
    const tagNames = [...new Set(all.flatMap((t) => t.tags.map((x) => x.name)))].sort();
    const stageNames = [...new Set(all.map(jiraStatus).filter(Boolean))].sort();
    if (listState.tag && !tagNames.includes(listState.tag)) listState.tag = "";
    if (listState.stage && !stageNames.includes(listState.stage)) listState.stage = "";

    const select = (label, options, key) => {
      const el = h("select", { "aria-label": label }, h("option", { value: "" }, label), options.map((o) => h("option", { value: o }, o)));
      el.value = listState[key];
      el.addEventListener("change", () => { listState[key] = el.value; renderList(); });
      return el;
    };
    const search = h("input", { type: "text", placeholder: "Filter tasks or enter an ID…", "aria-label": "Filter tasks or enter an ID", value: listState.q, size: "22" });
    search.addEventListener("input", () => { listState.q = search.value; renderList(); });
    const projectPicker = projects.length > 1
      ? h("select", { "aria-label": "Project", onchange: (e) => { listState.project = Number(e.target.value); listState.tab = null; showTaskList(); } },
        projects.map((p) => h("option", { value: p.id, selected: p.id === project.id }, p.name)))
      : null;
    const summary = h("span", { class: "note" });
    const allBox = h("input", { type: "checkbox" });
    allBox.checked = listState.all;
    allBox.addEventListener("change", () => { listState.all = allBox.checked; renderList(); });
    const listEl = h("div", { class: "tlist" });

    const renderList = () => {
      const q = listState.q.trim().toLowerCase();
      const keep = (t) => (!q || `#${t.issue_number} ${t.title} ${t.tags.map((x) => x.name).join(" ")}`.toLowerCase().includes(q))
        && (!listState.tag || t.tags.some((x) => x.name === listState.tag))
        && (!listState.stage || jiraStatus(t) === listState.stage);
      const groups = data.groups.map((g) => ({ name: g.name, tasks: g.tasks.filter(keep) })).filter((g) => g.tasks.length);
      const shown = groups.reduce((n, g) => n + g.tasks.length, 0);
      // A typed filter also searches my other tabs, and an ID that is in none of them is looked up in Timeline.
      const seen = new Set(all.map((t) => t.issue_number));
      const others = [];
      if (q || listState.all) {
        for (const [key, d] of cache.projectTasks) {
          if (!key.startsWith(`${project.id}|`) || !d.groups || d.tab === data.tab || d.tab === SUPPORT_TAB) continue;
          for (const g of d.groups) {
            const tasks = g.tasks.filter((t) => !seen.has(t.issue_number) && keep(t));
            tasks.forEach((t) => seen.add(t.issue_number));
            if (tasks.length) others.push({ name: `${d.tab} · ${g.name}`, tasks });
          }
        }
        if (!otherTabsLoaded) {
          otherTabsLoaded = true;
          Promise.all(baseTabs.filter((t) => t.name !== data.tab && t.name !== SUPPORT_TAB)
            .map((t) => getProjectTasks(project, t.name).catch(() => null))).then(() => { if (token === listToken) renderList(); });
        }
      }
      const lookupId = q && !others.length && !shown && /^#?\d{2,}$|\/edit\/\d+/.test(q) ? parseIds(q)[0] : null;
      let outsideRow = null;
      if (lookupId) {
        const got = outside.get(lookupId);
        if (got === undefined) {
          outside.set(lookupId, null);
          api(`/api/task/${encodeURIComponent(lookupId)}`).then((task) => outside.set(lookupId, { task }), (error) => outside.set(lookupId, { error: error.message }))
            .then(() => { if (token === listToken && parseIds(listState.q)[0] === lookupId) renderList(); });
        } else if (got) {
          outsideRow = got.task
            ? h("a", { class: "trow", href: `#/task/${got.task.issue_id}` }, watchButton(got.task.issue_id),
              h("span", { class: "issue" }, `#${got.task.issue_id}`),
              h("div", { class: "ttitle" }, h("span", { class: "tname" }, got.task.title, openInTimeline(got.task.issue_id, got.task.url))),
              h("span"), h("span", {}, got.task.status || ""), h("span"), h("span"), h("span"), h("span"))
            : h("p", { class: "note" }, `#${lookupId}: ${got.error}`);
        } else {
          outsideRow = h("p", { class: "note" }, `Looking up #${lookupId} in Timeline…`);
        }
      }
      const late = all.filter((t) => t.deadline_flag === "overdue").length;
      summary.textContent = `${shown} of ${all.length} task${all.length === 1 ? "" : "s"}${late ? ` · ${late} overdue` : ""}`;
      put(listEl,
        shown ? h("div", { class: "trow thead", "aria-hidden": "true" },
          h("span"), h("span"), h("span", {}, "Task"), railKey(), h("span", {}, "Status"), h("span", {}, "Due"), h("span", {}, "Hours"), h("span", {}, "People"), h("span")) : null,
        groups.map((g) => {
          const id = `${data.tab}|${g.name}`;
          const details = h("details", { class: "tgroup", open: !listState.closed.has(id) },
            h("summary", {}, h("span", { class: "gname" }, g.name), h("span", { class: "count" }, g.tasks.length)),
            g.tasks.map((t) => taskRow(t, boards, supportIds.has(t.issue_number))));
          details.addEventListener("toggle", () => {
            if (details.open) listState.closed.delete(id); else listState.closed.add(id);
          });
          return details;
        }),
        others.length ? h("h3", { class: "list-section" }, "In my other tabs") : null,
        others.map((g) => h("details", { class: "tgroup other", open: true },
          h("summary", {}, h("span", { class: "gname" }, g.name), h("span", { class: "count" }, g.tasks.length)),
          g.tasks.map((t) => taskRow(t, boards, supportIds.has(t.issue_number))))),
        outsideRow ? h("h3", { class: "list-section" }, "Not in my list") : null,
        outsideRow,
        shown || others.length || lookupId ? null : h("p", { class: "note" }, "No tasks match."));
    };

    put(body,
      h("div", { class: "list-head" },
        h("div", { class: "list-title" }, h("h2", {}, "My tasks"),
          sourceIcons(["live"]),
          projectPicker, summary),
        h("nav", { class: "subtabs", "aria-label": "Task tabs" }, tabs.map((t) => h("button", {
          type: "button", class: `subtab${t.name === SUPPORT_TAB ? " support" : ""}`, "aria-current": t.name === data.tab ? "page" : null, onclick: () => load(t.name),
        }, t.name, h("span", { class: "count" }, t.count)))),
        h("div", { class: "list-tools" },
          h("div", { class: "filters" }, search, select("All tags", tagNames, "tag"), select("All statuses", stageNames, "stage"),
            h("label", { class: "check" }, allBox, " All tasks")))),
      listEl);
    renderList();
  };

  const load = async (tab, force) => {
    put(body, h("p", { class: "note" }, "Loading your tasks…"));
    let data, base, support;
    try {
      data = await getProjectTasks(project, tab, force);
      // The project's own tabs, and the support list (its count, and which tasks to highlight).
      base = tab === SUPPORT_TAB ? await getProjectTasks(project, null, force) : data;
      support = tab === SUPPORT_TAB ? data : await getProjectTasks(project, SUPPORT_TAB, force).catch(() => null);
    } catch (error) {
      if (token === listToken) put(body, h("p", { class: "error", role: "alert" }, error.message));
      return;
    }
    if (token !== listToken) return;
    listState.tab = data.tab;
    draw(data, base.tabs, support);
  };
  await load(listState.tab, refresh);
}

// ---- task -----------------------------------------------------------------
async function showTask(issue, force) {
  const input = h("input", { type: "text", id: "task-id", value: issue || "", "aria-label": "Issue ID or task URL", placeholder: "Issue ID or task URL", size: "34" });
  const open = () => {
    const id = parseIds(input.value)[0] || input.value.trim();
    if (id) location.hash = `#/task/${id}`;
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
  const toolbar = h("div", { class: "toolbar" }, input, h("button", { type: "button", class: "btn", onclick: open }, "Open task"),
    issue ? h("a", { href: "#/task", class: "back" }, "← My tasks") : null);
  if (!issue) {
    await showTaskList(force);
    return;
  }
  put(view, toolbar, h("p", { class: "note" }, `Loading #${issue}…`));
  let task;
  try {
    task = await api(`/api/task/${encodeURIComponent(issue)}`);
  } catch (error) {
    put(view, toolbar, h("p", { class: "error", role: "alert" }, error.message));
    return;
  }
  const status = (await loadStatuses([task.issue_id]).catch(() => [null]))[0];
  await getPlanner().catch(() => null); // for the Timeline link of planner tasks
  const jiraBoard = await api("/api/jira/board").catch(() => null);
  const jiraCard = jiraBoard && jiraBoard.cards.find((c) => String(c.issue_number) === String(task.issue_id));
  const planned = cache.planner && Object.values(cache.planner.periods).flatMap((p) => p.tasks)
    .find((t) => String(t.issue_number) === String(task.issue_id));
  const link = planned && planned.url;
  const head = h("section", { class: "task-head" },
    h("div", { class: "head-info" },
    h("div", { class: "title-line" },
      h("span", { class: "issue" }, `#${task.issue_id}`),
      h("h2", {}, task.title || "(no title)"), openInTimeline(task.issue_id, link || task.url)),
    h("div", { class: "facts" },
      jiraBoard ? h("span", { class: "chip", title: "Status on your Jira board" }, jiraCard ? jiraCard.status : "Not on Jira board") : null,
      task.creator ? h("span", {}, `Created by ${task.creator}`) : null,
      task.created_date ? h("span", {}, niceDate(task.created_date, false)) : null,
      task.tags.length ? h("span", {}, `Tags: ${task.tags.join(", ")}`) : null,
      link ? h("a", { href: link, target: "_blank", rel: "noopener noreferrer" }, "Open in Timeline ↗") : null)),
    h("div", { class: "head-stages" },
      toolbar,
      railKey(),
      rail(status && !status.error ? status : null)));
  const description = plainText(task.description);
  const copyIcon = () => new DOMParser().parseFromString(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/></svg>',
    "image/svg+xml").documentElement;
  const copyText = async (text) => {
    try { await navigator.clipboard.writeText(text); toast("Copied to clipboard."); }
    catch { toast("Couldn't copy — clipboard not available.", true); }
  };
  const photos = new Map(task.comments.filter((c) => c.photo).map((c) => [c.author, c.photo]));
  const post = (author, date, content, label, text, photo) => h("li", { class: "post" },
    avatar(author, photo),
    h("div", { class: "post-card" },
      h("div", { class: "post-head" },
        h("strong", {}, author || "Unknown"),
        h("button", { type: "button", class: "icon-btn", title: "Copy text", "aria-label": "Copy text", onclick: () => copyText(text || "") }, copyIcon()),
        label ? h("span", { class: "creator" }, label) : null,
        h("small", {}, date || "")),
      content));
  const comments = task.comments.map((c) => post(c.author, c.created_at,
    plainText(c.description) ? richText(c.description) : h("p", { class: "note" }, "(no text)"),
    c.author && c.author === task.creator ? "task creator" : null, plainText(c.description), c.photo));
  const commits = task.commits || [];
  // Commits sit between the comments, by time (Timeline's `dd.mm.yyyy HH:MM`); on a tie the comment comes first.
  const stamp = (text) => { const m = /^(\d+)\.(\d+)\.(\d+)\s+(\d+):(\d+)/.exec(text || ""); return m ? Date.UTC(+m[3], +m[2] - 1, +m[1], +m[4], +m[5]) : 0; };
  const conversation = [...comments.map((el, i) => ({ el, at: stamp(task.comments[i].created_at), order: 0 })),
    ...commits.map((c) => ({ el: commitEntry(c, photos.get(c.author)), at: stamp(c.created_at), order: 1 }))]
    .sort((x, y) => x.at - y.at || x.order - y.order).map((x) => x.el);
  const listed = [...cache.projectTasks.values()].flatMap((d) => d.groups.flatMap((g) => g.tasks))
    .find((t) => String(t.issue_number) === String(task.issue_id));
  const flow = h("section", { class: "wf", "aria-label": "Workflow" }, h("p", { class: "note" }, "Loading the workflow…"));
  put(view,
    head,
    guardBox(task.issue_id, task.title),
    flow,
    h("h3", { class: "task-section" }, "Description"),
    h("ol", { class: "comments" }, post(task.creator, task.created_date ? niceDate(task.created_date, false) : "",
      description ? richText(task.description) : h("p", { class: "note" }, "No description."), "task creator", description, photos.get(task.creator))),
    h("h3", { class: "task-section" }, `Conversation (${task.comments.length})`),
    comments.length || commits.length ? h("ol", { class: "comments" }, conversation) : h("p", { class: "note" }, "No comments yet."),
    h("h3", { class: "task-section" }, `Commits (${commits.length})`),
    commits.length ? h("ul", { class: "commit-list" }, commits.map(commitRow)) : h("p", { class: "note" }, "No commits linked to this task."));
  trackGuard();
  await loadWorkflow(flow, String(task.issue_id), (planned && planned.code) || (listed && listed.code) || null);
}

function commitEntry(c, photo) {
  const sha = c.url && (c.url.match(/\/commit\/([0-9a-f]{7,40})/i) || [])[1];
  const code = sha ? h("code", { class: "tl-sha" }, sha.slice(0, 7)) : null;
  const safe = c.url && /^https:\/\//.test(c.url);
  return h("li", { class: "post commit-entry" },
    avatar(c.author, photo),
    h("div", { class: "commit-body" },
      h("div", { class: "commit-title" }, "Commit Added", safe && code ? h("a", { href: c.url, target: "_blank", rel: "noopener noreferrer", title: "Open commit" }, code) : code),
      h("p", {}, plainText(c.message) || "(no message)"),
      h("small", {}, [c.created_at, c.author && `by ${c.author}`].filter(Boolean).join(" "))));
}

function commitRow(c) {
  const sha = c.url && (c.url.match(/\/commit\/([0-9a-f]{7,40})/i) || [])[1];
  const code = sha ? h("code", { class: "tl-sha" }, sha.slice(0, 7)) : null;
  const safe = c.url && /^https:\/\//.test(c.url);
  return h("li", { class: "commit-row" },
    safe && code ? h("a", { href: c.url, target: "_blank", rel: "noopener noreferrer", title: "Open commit" }, code) : code,
    h("span", { class: "commit-msg" }, plainText(c.message) || "(no message)"),
    h("small", { class: "note" }, [c.author, c.created_at].filter(Boolean).join(" · ")),
    safe ? h("a", { class: "ext", href: c.url, target: "_blank", rel: "noopener noreferrer", title: "Open commit" }, "↗") : null);
}

// ---- task workflow ---------------------------------------------------------
// The flow, its status and history come from /api/workflow/<id> (workflow.py),
// including which buttons apply now: the page shows them and asks for the
// reasons, the server decides. Only Run and Continue open Claude, in a
// terminal; every decision is saved with its time.
const flowState = { token: 0, timer: null, open: new Set(), resetting: false };

const EVENT_LABEL = {
  start: "Started", run: "Ran", resume: "Continued", approve: "Approved", reject: "Rejected", abandon: "Abandoned",
  skip: "Skipped", finish: "Finished", back_to_develop: "Back to Develop", qa_issues: "QA found issues",
  qa_passed: "QA approved", close: "Closed", stop: "Stopped", reopen: "Reopened",
  ack: "Marked Timeline changes as seen", finding: "Finding", finding_added: "Added finding", prod_fix: "Started a prod fix",
  reset: "Reset",
};

function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function flowPost(issue, path, body) {
  return api(`/api/workflow/${encodeURIComponent(issue)}/${path}`, {
    method: "POST",
    headers: { "X-Timeline-Desk": "1", "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

async function loadWorkflow(el, issue, code) {
  const token = el._wfToken = (el._wfToken || 0) + 1;  // per panel: the Workflows tab shows several at once
  clearTimeout(el._wfTimer);
  let v;
  try {
    v = await api(`/api/workflow/${encodeURIComponent(issue)}${code ? `?code=${encodeURIComponent(code)}` : ""}`);
  } catch (error) {
    put(el, h("p", { class: "error", role: "alert" }, `Workflow: ${error.message}`));
    return;
  }
  if (token !== el._wfToken || !el.isConnected) return;
  code = code || v.code || null;  // the desk finds the board itself when the page didn't know it
  renderWorkflow(el, v, code);
  if (cache.workflows) {  // keep the badges elsewhere in step
    if (v.workflow) cache.workflows.set(issue, { issue, code: v.workflow.code, step: v.workflow.step, status: v.status });
    else cache.workflows.delete(issue);
  }
  // While a terminal is open on a run, look again every 5 s: its status line follows Claude's session.
  if (v.runs.some((r) => r.status === "running")) {
    el._wfTimer = setTimeout(() => { if (el.isConnected) loadWorkflow(el, issue, code); }, 5000);
  }
}

function stepper(steps) {
  return h("ol", { class: "wf-steps" }, steps.filter((s) => !s.hidden).map((s) => h("li", { class: `wf-step ${s.state}`, title: `${s.title}: ${s.state}` },
    h("span", { class: "dot", "aria-hidden": "true" }), s.title,
    s.optional ? h("small", {}, " optional") : null)));
}

function factList(rows) {
  return h("dl", { class: "wf-facts" }, rows.filter(([, value]) => value && (!Array.isArray(value) || value.length))
    .flatMap(([label, value]) => [h("dt", {}, label),
      h("dd", {}, Array.isArray(value) ? h("ul", {}, value.map((x) => h("li", {}, x))) : value)]));
}

function agoSeconds(seconds) {
  if (seconds === null || seconds === undefined) return "";
  if (seconds < 10) return "just now";
  if (seconds < 90) return `${seconds} s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  return `${Math.round(seconds / 3600)} h ago`;
}

// What the run's terminal is doing right now, from Claude's session log (workflow.terminal_activity).
function activityLine(run) {
  const a = run.activity;
  if (!a || run.decision || run.ended_at) return null;
  return h("div", { class: `wf-activity ${a.state}`, role: "status" },
    h("span", { class: "dot", "aria-hidden": "true" }),
    h("strong", {}, a.label),
    a.detail ? h("span", { class: "text" }, a.detail) : null,
    a.idle !== null && a.idle !== undefined ? h("small", {}, `last activity ${agoSeconds(a.idle)}`) : null);
}

function resultBlock(run) {
  const r = run.result;
  if (!r) {
    return h("p", { class: "note" }, run.status === "running"
      ? `Claude is working in the terminal. It reports back in ${run.result_file}; this page checks every 5 seconds.`
      : `No result in ${run.result_file}.${run.exit_code !== null && run.exit_code !== undefined ? ` Claude exited with code ${run.exit_code}.` : ""}`);
  }
  return h("div", { class: "wf-result" },
    r.error ? h("p", { class: "error" }, r.error) : null,
    r.summary ? markdown(r.summary) : null,
    factList([
      ["Questions", r.questions],
      ["Files", r.files],
      ["Commits", r.commits],
      ["Left after this", r.remaining !== undefined ? String(r.remaining) : null],
    ]));
}

function conversation(issue, run) {
  const box = h("div", { class: "wf-convo" }, h("p", { class: "note" }, "Loading…"));
  const details = h("details", { class: "wf-more" }, h("summary", {}, "Conversation"), box);
  const key = `convo-${run.id}`;
  const load = async () => {
    try {
      const t = await api(`/api/workflow/${encodeURIComponent(issue)}/runs/${run.id}/transcript`);
      if (!t.found) { put(box, h("p", { class: "note" }, `No session log for ${t.session_id} yet. Claude Code writes it once the session starts.`)); return; }
      put(box,
        t.truncated ? h("p", { class: "note" }, "Showing the latest part only.") : null,
        t.items.length ? t.items.map((i) => h("div", { class: `msg ${i.who}` },
          h("span", { class: "who" }, { you: "You", claude: "Claude" }[i.who] || "Tool"),
          i.who === "claude" ? markdown(i.text) : h("div", { class: "text" }, i.text))) : h("p", { class: "note" }, "Nothing said yet."));
    } catch (error) {
      put(box, h("p", { class: "error" }, error.message));
    }
  };
  details.addEventListener("toggle", () => {
    if (details.open) { flowState.open.add(key); load(); } else flowState.open.delete(key);
  });
  if (flowState.open.has(key)) { details.open = true; load(); }
  return details;
}

// The open run has its own card above; this lists the others, and counts only those.
function previousRuns(issue, v, open) {
  const others = v.runs.filter((r) => r !== open);
  return h("details", { class: "wf-section", open: others.length && !open ? true : null },
    h("summary", {}, `${open ? "Earlier runs" : "Runs"} (${others.length})`),
    others.length ? others.map((r) => runCard(issue, r, false)) : h("p", { class: "note" }, open ? "No earlier runs." : "No runs yet."));
}

function runCard(issue, run, open) {
  return h("article", { class: `wf-run ${open ? "open" : ""}` },
    h("div", { class: "wf-run-head" },
      h("strong", {}, `Run ${run.id} · ${run.step_title}`),
      h("span", { class: `pill ${run.status}` }, run.status_label),
      run.finding_id ? h("span", { class: "note" }, `F${run.finding_id}`) : null,
      h("small", {}, `${when(run.started_at)}${run.ended_at ? ` – ${when(run.ended_at)}` : ""}`)),
    activityLine(run),
    resultBlock(run),
    run.note ? h("p", { class: "note" }, `Your note: ${run.note}`) : null,
    h("details", { class: "wf-more" }, h("summary", {}, "Prompt and session"),
      factList([["Session", run.session_id], ["Folder", run.cwd], ["Result file", run.result_file]]),
      h("pre", { class: "md-code" }, run.prompt)),
    conversation(issue, run));
}

// Actions whose confirm button is red: they throw away a run or stop the flow.
const DANGER_ACTIONS = new Set(["reject", "abandon", "stop"]);

async function act(el, issue, code, action, v) {
  const open = v.runs.find((r) => !r.decision);
  if (action.kind === "reset") {  // opens the form below the buttons
    flowState.resetting = flowState.resetting === issue ? false : issue;
    renderWorkflow(el, v, code);
    return;
  }
  let note = "";
  if (action.kind === "approve") {
    const step = v.steps.find((s) => s.key === v.workflow.step);
    if (!await confirmDialog({ title: `Approve run ${open.id} of ${step.title}?`, text: `You confirm: ${step.approve}`, ok: "Approve" })) return;
  } else if (action.needs_note) {
    note = await askDialog({ title: `${action.label}?`, text: action.help, label: "Why? (saved in the history)",
      ok: action.label, danger: DANGER_ACTIONS.has(action.kind), required: "Add a reason, so the history says why." });
    if (note === null) return;
  } else if (["run", "resume", "ack"].includes(action.kind)) {
    // no extra question: the step card above (or the Timeline box) is what this acts on
  } else if (!await confirmDialog({ title: `${action.label}?`, text: action.help, ok: action.label, danger: DANGER_ACTIONS.has(action.kind) })) return;
  try {
    const runs = ["run", "explain_blocker"].includes(action.kind);
    const path = runs ? "run" : action.kind === "resume" ? "resume" : "decide";
    const body = action.kind === "run" ? {}
      : action.kind === "explain_blocker" ? { side: "unblock" }
      : action.kind === "resume" ? { run_id: open.id }
      : { kind: action.kind, run_id: open ? open.id : null, note };
    const r = await flowPost(issue, path, body);
    if (runs) toast(`Claude opened in a terminal for run ${r.started.run_id}.${r.note ? ` ${r.note}` : ""}`, Boolean(r.note));
    else if (action.kind === "resume") toast("Claude reopened in a terminal.");
  } catch (error) {
    toast(error.message, true);
  }
  await loadWorkflow(el, issue, code);
}

// Reset: erase the task's workflow and its timeline/<id>/ folder, after you type the task number.
function resetForm(el, issue, code, v) {
  if (flowState.resetting !== issue || !v.actions.some((a) => a.kind === "reset") || !v.reset_preview) return null;
  const p = v.reset_preview;
  const files = p.folders.flatMap((f) => f.files.map((name) => `${f.path}/${name}`));
  const typed = h("input", { type: "text", inputmode: "numeric", placeholder: issue, "aria-label": `Type ${issue} to confirm`, maxlength: "8" });
  const go = h("button", { type: "button", class: "btn danger" }, "Erase and reset");
  const cancel = h("button", { type: "button", class: "btn ghost" }, "Cancel");
  const open = v.runs.find((r) => !r.decision);
  cancel.addEventListener("click", () => { flowState.resetting = false; renderWorkflow(el, v, code); });
  go.addEventListener("click", async () => {
    if (typed.value.trim().replace(/^#/, "") !== issue) { toast(`Type ${issue} to confirm.`, true); typed.focus(); return; }
    go.disabled = true;
    try {
      const r = await flowPost(issue, "reset", { confirm: typed.value, code });
      flowState.resetting = false;
      toast(`#${issue} reset: ${r.removed.runs} run(s), ${r.removed.findings} finding(s) and ${r.removed.files} file(s) erased.`);
    } catch (error) {
      toast(error.message, true);
      go.disabled = false;
      return;
    }
    await loadWorkflow(el, issue, code);
  });
  return h("div", { class: "wf-card wf-reset" },
    h("h4", {}, `Reset #${issue}: erase everything`),
    h("p", {}, "This deletes, and it can't be undone:"),
    h("ul", {},
      h("li", {}, `The workflow's status, ${p.runs} run(s) with their results and conversations, every decision, ${p.findings} QA finding(s) and all approval requests.`),
      h("li", {}, files.length ? `${files.length} file(s) in ${p.folders.map((f) => f.path).join(" and ")}:` : "No files in the task's timeline folder."),
      files.length ? h("details", { class: "wf-more" }, h("summary", {}, "Show the files"), h("pre", { class: "md-code" }, files.join("\n"))) : null),
    h("p", { class: "note" }, `Kept: the worktree ${p.worktree || ""} and its branch ${p.branch || ""} (remove them on the Work tab), and the copied Timeline history.`,
      open ? ` Run ${open.id} is open: a run on this page is stopped first; a terminal run must be closed before you reset.` : ""),
    h("div", { class: "wf-actions" }, h("label", { class: "note" }, `Type ${issue} to confirm `, typed), go, cancel));
}

function actionBar(el, issue, code, v) {
  if (!v.actions.length) return null;
  return h("div", { class: "wf-actions" }, v.actions.map((a) => {
    const primary = ["run", "approve", "close", "reopen"].includes(a.kind) || a.suggested;
    const button = h("button", {
      type: "button",
      class: `btn${primary ? "" : " ghost"}${a.kind === "stop" ? " far" : ""}${a.suggested ? " suggested" : ""}`,
      title: a.blocked || (a.suggested ? `Suggested by a Timeline change. ${a.help}` : a.help),
      disabled: a.blocked ? true : null,
    }, a.label);
    button.addEventListener("click", async () => {
      button.disabled = true;
      await act(el, issue, code, a, v);
    });
    return button;
  }), v.actions.find((a) => a.blocked) ? h("p", { class: "warn-text" }, v.actions.find((a) => a.blocked).blocked) : null);
}

function stepCard(v) {
  const step = v.steps.find((s) => s.key === v.workflow.step);
  if (v.workflow.state !== "active") return null;
  return h("div", { class: "wf-card" },
    h("h4", {}, `Now: ${step.title}`, step.repeat ? h("small", {}, " · one item per run, repeat until done") : null),
    step.about ? h("p", {}, step.about) : null,
    step.run ? factList([
      ["Skills", step.skills],
      ["Reads", step.reads],
      ["Writes", step.writes],
      ["Won't", step.wont],
      ["Runs in", `A terminal, in ${v.workflow.worktree || "the task's worktree"}, with Claude's normal permission prompts`],
      ["You approve", step.approve],
    ]) : null,
    v.prompt_note ? h("p", { class: "note" }, v.prompt_note) : null,
    v.prompt_preview ? h("details", { class: "wf-more" }, h("summary", {}, "Exact prompt"),
      h("p", { class: "note" }, "<run> becomes the run number."), h("pre", { class: "md-code" }, v.prompt_preview)) : null);
}

function docsList(issue, code, v) {
  if (!v.docs.length) return h("p", { class: "note" }, `No task docs yet in ${v.repo || "the repo"}'s timeline/${issue}/.`);
  const shown = h("div", { class: "wf-doc" });
  return h("div", {},
    h("ul", { class: "wf-docs" }, v.docs.map((d) => h("li", {},
      h("button", {
        type: "button", class: "linkish",
        onclick: async () => {
          put(shown, h("p", { class: "note" }, `Loading ${d.name}…`));
          try {
            const doc = await api(`/api/workflow/${encodeURIComponent(issue)}/doc/${encodeURIComponent(d.name)}${code ? `?code=${encodeURIComponent(code)}` : ""}`);
            const copy = h("button", { type: "button", class: "btn ghost small" }, "Copy");
            copy.addEventListener("click", async () => {
              try { await navigator.clipboard.writeText(doc.text); toast(`Copied ${doc.name}.`); }
              catch { toast("Couldn't copy — clipboard not available.", true); }
            });
            put(shown, h("div", { class: "wf-doc-head" }, h("strong", {}, doc.name), h("small", {}, `changed ${when(doc.modified)}`), copy),
              /\.md$/.test(doc.name) ? markdown(doc.text) : h("pre", { class: "md-code" }, doc.text));
          } catch (error) {
            put(shown, h("p", { class: "error" }, error.message));
          }
        },
      }, d.name),
      h("small", {}, ` ${when(d.modified)}`)))),
    shown);
}

function startForm(el, issue, code, v) {
  if (!v.startable) return h("p", { class: "note" }, v.start_note);
  const select = h("select", { "aria-label": "Start at" }, v.steps.filter((s) => s.key !== "close" && !s.hidden)
    .map((s) => h("option", { value: s.key, selected: s.key === v.proposal.step ? true : null }, s.title)));
  const button = h("button", { type: "button", class: "btn" }, "Start workflow");
  button.addEventListener("click", async () => {
    const step = v.steps.find((s) => s.key === select.value);
    const first = v.steps.indexOf(step) === 0;
    if (!await confirmDialog({ title: `Start a workflow for #${issue} at ${step.title}?`, ok: "Start workflow",
      text: `The desk makes or reuses the task's worktree in ${v.repo}.${first ? "" : ` Steps before ${step.title} are recorded as skipped.`}` })) return;
    button.disabled = true;
    try {
      const r = await flowPost(issue, "start", { code: v.code, step: select.value });
      if (r.note) toast(r.note, true);
    } catch (error) {
      toast(error.message, true);
    }
    await loadWorkflow(el, issue, code);
  });
  return h("div", { class: "wf-card" },
    h("h4", {}, "Start a workflow"),
    h("p", {}, `Suggested start: ${v.steps.find((s) => s.key === v.proposal.step).title}. `, h("span", { class: "note" }, v.proposal.reason)),
    v.timeline_error ? h("p", { class: "warn-text" }, `Couldn't read Timeline's stages: ${v.timeline_error}`) : null,
    h("div", { class: "wf-actions" }, h("label", { class: "note" }, "Start at ", select), button),
    h("p", { class: "note" }, `Nothing runs until you click Run on a step. Every run and decision is kept in the history below.`));
}

// QA findings (phase 3): each QA comment (or a point you add) with your
// verdict. Only Bug findings get fixed, one per run; Questions get a drafted reply.
const FINDING_BUTTON = { bug: "Bug", question: "Question", not_bug: "Not a bug", wont_fix: "Won't fix", new: "Undecided" };

function findingsBox(el, issue, code, v) {
  const inQa = v.workflow && ["qa", "qa_fix"].includes(v.workflow.step) && v.workflow.state === "active";
  if (!v.findings.length && !inQa) return null;
  const after = () => loadWorkflow(el, issue, code);
  const post = async (path, body, okText) => {
    try { await flowPost(issue, path, body); if (okText) toast(okText); } catch (error) { toast(error.message, true); }
    await after();
  };
  const rows = v.findings.map((f) => {
    const classify = f.classify.map((target) => {
      const b = h("button", { type: "button", class: "btn ghost small" }, FINDING_BUTTON[target] || target);
      b.addEventListener("click", async () => {
        let note = "";
        if (["question", "not_bug", "wont_fix"].includes(target) || f.status === "fixed") {
          note = await askDialog({ title: `F${f.id} → ${FINDING_BUTTON[target]}`, label: "Why? (saved in the history)",
            ok: `Mark as ${FINDING_BUTTON[target]}`, required: "Add a reason, so the history says why." });
          if (note === null) return;
        }
        b.disabled = true;
        await post(`findings/${f.id}`, { status: target, note });
      });
      return b;
    });
    const fix = f.can_fix ? h("button", { type: "button", class: "btn small" }, "Fix this") : null;
    if (fix) fix.addEventListener("click", async () => { fix.disabled = true; await post("run", { finding_id: f.id }, `Claude opened in a terminal for F${f.id}.`); });
    const reply = f.can_reply ? h("button", { type: "button", class: "btn small" }, "Draft a reply") : null;
    if (reply) reply.addEventListener("click", async () => { reply.disabled = true; await post("run", { side: "qa_reply", finding_id: f.id }, `Claude opened in a terminal to draft a reply for F${f.id}.`); });
    const body = plainText(f.body);
    return h("li", { class: `finding ${f.status}` },
      h("div", { class: "finding-head" },
        h("strong", {}, `F${f.id}`),
        h("span", { class: `pill ${f.status === "fixed" ? "done" : f.status === "bug" ? "needs_you" : f.status === "new" ? "attention" : "none"}` }, f.status_label),
        h("span", { class: "note" }, [f.author, f.posted_at && f.posted_at.replace("T", " ").slice(0, 16), f.event_id && `comment ${f.event_id}`,
          `QA round ${f.qa_round + 1}`, f.run_id && `fixed in run ${f.run_id}`].filter(Boolean).join(" · "))),
      f.label ? h("p", {}, f.label) : null,
      body ? h("details", { class: "wf-more" }, h("summary", {}, body.length > 140 ? `${body.slice(0, 140)}…` : body), richText(f.body)) : null,
      f.note ? h("p", { class: "note" }, `Your note: ${f.note}`) : null,
      h("div", { class: "wf-actions" }, fix, reply, classify));
  });
  let add = null;
  if (inQa) {
    const input = h("input", { type: "text", placeholder: "Totals row is misaligned on the PDF", "aria-label": "New finding", maxlength: "300" });
    const button = h("button", { type: "button", class: "btn ghost small" }, "Add finding");
    button.addEventListener("click", async () => {
      if (!input.value.trim()) { toast("Describe the finding in a few words.", true); return; }
      button.disabled = true;
      await post("findings", { label: input.value });
    });
    add = h("div", { class: "wf-actions" }, input, button);
  }
  const open = v.findings.filter((f) => f.status === "new" || f.status === "bug").length;
  return h("div", { class: "wf-card wf-findings" },
    h("h4", {}, `QA findings (${v.findings.length})`, open ? h("small", {}, ` · ${open} to decide or fix`) : null),
    v.findings.length ? h("ul", {}, rows) : h("p", { class: "note" }, "No QA comments yet. They appear here as Timeline syncs (every few minutes)."),
    add);
}

// What changed in Timeline since you last marked it seen (phase 2 signals).
function timelineBox(v) {
  const hints = [];
  if (!v.watched) hints.push("This task isn't on the watchlist, so Timeline changes aren't followed. Start the workflow again or watch it on the Task tab.");
  if (!v.me) hints.push('Set "me" in timeline-desk.json to your Timeline name, so your own comments aren\'t counted.');
  if (!v.signals.length && !v.blocked && !hints.length) return null;
  return h("div", { class: `wf-signals${v.signals.some((s) => s.level !== "info") ? " loud" : ""}` },
    h("h4", {}, v.signals.length ? `New in Timeline (${v.signals.length})` : "Timeline"),
    v.blocked ? h("p", { class: "warn-text" }, `Timeline's latest stage: ${v.blocked}.`) : null,
    v.signals.length ? h("ul", {}, v.signals.map((s) => h("li", { class: s.level },
      h("small", {}, s.at ? s.at.replace("T", " ").slice(0, 16) : ""), " ", s.text,
      s.suggest ? h("span", { class: "note" }, ` → suggests ${(ACTION_LABEL[s.suggest] || s.suggest)}`) : null))) : null,
    hints.map((t) => h("p", { class: "note" }, t)));
}

const ACTION_LABEL = { qa_issues: "QA found issues", qa_passed: "QA approved", explain_blocker: "Explain the blocker" };

function renderWorkflow(el, v, code) {
  const issue = v.issue;
  if (!v.workflow) {
    put(el, h("div", { class: "wf-head" }, h("h3", {}, "Workflow"), h("span", { class: "pill none" }, "Not started")),
      stepper(v.steps), startForm(el, issue, code, v),
      h("h4", {}, "Task docs"), docsList(issue, code, v));
    return;
  }
  const w = v.workflow;
  const open = v.runs.find((r) => !r.decision);
  put(el,
    h("div", { class: "wf-head" },
      h("h3", {}, "Workflow"),
      h("span", { class: `pill ${v.status.code}` }, v.status.label),
      h("span", { class: "note" }, [w.repo, w.branch && `branch ${w.branch}`, w.qa_round ? `QA round ${w.qa_round + 1}` : null,
        `started ${when(w.started_at)}`].filter(Boolean).join(" · "))),
    stepper(v.steps),
    timelineBox(v),
    stepCard(v),
    findingsBox(el, issue, code, v),
    open ? runCard(issue, open, true) : null,
    actionBar(el, issue, code, v),
    resetForm(el, issue, code, v),
    previousRuns(issue, v, open),
    h("details", { class: "wf-section" }, h("summary", {}, `Decisions (${v.events.length})`),
      h("ol", { class: "wf-log" }, v.events.map((e) => h("li", {},
        h("small", {}, when(e.at)), h("strong", {}, EVENT_LABEL[e.kind] || e.kind),
        e.step ? h("span", { class: "note" }, ` · ${(v.steps.find((s) => s.key === e.step) || {}).title || e.step}`) : null,
        e.run_id ? h("span", { class: "note" }, ` · run ${e.run_id}`) : null,
        e.note ? h("div", { class: "text" }, e.note) : null)))),
    h("details", { class: "wf-section", open: true }, h("summary", {}, `Task docs (${v.docs.length})`), docsList(issue, code, v)));
}

// ---- workflows overview -----------------------------------------------------
// Every task workflow, grouped by what it needs: you, Claude, QA, or nothing.
const FLOW_GROUPS = [
  { key: "you", label: "Needs you", codes: ["attention", "needs_you"] },
  { key: "running", label: "Running", codes: ["running"] },
  { key: "waiting", label: "Waiting on QA", codes: ["waiting"] },
  { key: "stopped", label: "Stopped", codes: ["stopped"] },
  { key: "done", label: "Done", codes: ["done"] },
];

let flowListTimer = null;

async function showWorkflows(arg) {
  clearTimeout(flowListTimer);
  loading("Loading workflows…");
  let { workflows } = await api("/api/workflows");
  cache.workflows = new Map(workflows.map((w) => [String(w.issue), w]));
  if (!workflows.length) {
    put(view, h("p", { class: "note" }, "No workflows yet. Open a task (from the planner or the Task tab) and start one."));
    return;
  }
  // The same facts as a planner card (stage rail, stage, deadline, flag, support), where the task is on the planner.
  const [p, boards] = await Promise.all([getPlanner().catch(() => null), getBoards().catch(() => ({}))]);
  const planned = new Map();
  if (p) for (const period of Object.values(p.periods)) for (const t of period.tasks) if (!planned.has(String(t.issue_number))) planned.set(String(t.issue_number), { ...t, period: period.label });
  await loadStatuses(workflows.map((w) => w.issue)).catch(() => null);

  const order = (list) => FLOW_GROUPS.flatMap((g) => list.filter((w) => g.codes.includes(w.status.code)));
  let selected = String(arg || "");
  if (!workflows.some((w) => String(w.issue) === selected)) selected = String(order(workflows)[0].issue);

  const list = h("nav", { class: "wfl-list", "aria-label": "Workflows" });
  const detail = h("div", { class: "wfd" });

  const facts = (w) => {
    const t = planned.get(String(w.issue));
    return h("span", { class: "wf-meta" },
      flagBadge(t && t.deadline_flag),
      t && t.support ? h("span", { class: "badge support" }, "support") : null,
      t ? chip(t.period, "muted") : null,
      chip((t && t.stage_status) || "Not on planner", "muted"),
      t ? deadlineChip(t) : null,
      w.code && boards && boards[String(w.code)] ? chip(boards[String(w.code)], "muted") : null);
  };

  const item = (w) => {
    const id = String(w.issue);
    const t = planned.get(id);
    const a = h("a", { class: "wfl", href: `#/workflows/${id}`, "aria-current": id === selected ? "true" : null },
      h("span", { class: "wfl-top" }, h("span", { class: "issue" }, `#${id}`),
        h("span", { class: `pill ${w.status.code}` }, w.status.label.length > 40 ? w.status.short : w.status.label)),
      h("span", { class: "wfl-title" }, w.title || (t && t.title) || "(no title)", " ", openInTimeline(id, w.url)),
      h("small", { class: "note" }, `${w.step_title} · since ${when(w.step_since || w.updated_at)}`),
      w.newest_signal ? h("small", { class: "wfl-signal" }, w.newest_signal.text) : null,
      rail(cache.statuses.get(id)));
    a.addEventListener("click", (e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey) return;
      e.preventDefault();
      selected = id;
      history.replaceState(null, "", `#/workflows/${id}`);  // no hashchange: only the right side reloads
      drawList();
      drawDetail();
    });
    return a;
  };

  const drawList = () => put(list, ...FLOW_GROUPS.map((g) => {
    const items = workflows.filter((w) => g.codes.includes(w.status.code));
    if (!items.length) return null;
    return h("section", { class: "wfl-group" }, h("h3", {}, `${g.label} (${items.length})`), items.map(item));
  }));

  const drawDetail = () => {
    const w = workflows.find((x) => String(x.issue) === selected);
    const panel = h("section", { class: "wf", "aria-label": `Workflow #${selected}` }, h("p", { class: "note" }, "Loading the workflow…"));
    put(detail,
      h("header", { class: "wfd-head" },
        h("div", { class: "wfd-title" },
          h("span", { class: "issue" }, `#${selected}`),
          h("h2", {}, w.title || "(no title)"),
          openInTimeline(selected, w.url),
          h("a", { class: "btn ghost", href: `#/task/${selected}` }, "Open task page")),
        facts(w),
        rail(cache.statuses.get(selected))),
      panel);
    loadWorkflow(panel, selected, w.code || null);
  };

  drawList();
  put(view, h("div", { class: "wfl-layout" }, list, detail));
  drawDetail();

  // Keep the list's statuses current (the open panel refreshes itself).
  const refreshList = async () => {
    if (!list.isConnected) return;
    try {
      workflows = (await api("/api/workflows")).workflows;
      cache.workflows = new Map(workflows.map((x) => [String(x.issue), x]));
      if (workflows.some((x) => String(x.issue) === selected)) drawList();
    } catch { /* next time */ }
    flowListTimer = setTimeout(refreshList, 15000);
  };
  flowListTimer = setTimeout(refreshList, 15000);
}

// ---- jira board (cards are stored in SQLite; you add them from your assigned tasks) ----
async function postJson(path, body) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Timeline-Desk": "1" }, body: JSON.stringify(body) });
}

async function showJira() {
  loading("Loading the board…");
  const data = await api("/api/jira/board");
  const cards = data.cards;
  let query = "";
  let dragging = null;
  const error = h("p", { class: "error", role: "alert", hidden: true });
  const fail = (e) => { error.textContent = e.message; error.hidden = false; };
  const board = h("div", { class: "jira-board", style: `--cols:${data.statuses.length}` });

  // The picker: all your assigned tasks that are not on the board yet.
  const picker = h("dialog", { class: "jira-picker" });
  const openPicker = async (status) => {
    const list = h("ul", { class: "jira-pick-list" });
    const note = h("p", { class: "note" }, "Loading your tasks…");
    const selected = new Set();  // stays selected while you filter
    let mine = [];
    let support = [];
    let loaded = false;
    const addButton = h("button", { type: "button", class: "btn", disabled: true }, "Add");
    const refreshAdd = () => {
      addButton.disabled = !selected.size;
      addButton.textContent = selected.size ? `Add ${selected.size} to ${status}` : "Add";
    };
    const drawList = (q) => {
      const onBoard = new Set(cards.map((c) => c.issue_number));
      const keep = (t) => !onBoard.has(t.issue_number) && (!q || `${t.issue_number} ${t.title}`.toLowerCase().includes(q));
      const supportIds = new Set(support.map((t) => t.issue_number));
      const groups = [
        { name: "My tasks", tasks: mine.filter((t) => !supportIds.has(t.issue_number) && keep(t)), support: false },
        { name: "Support tasks", tasks: support.filter(keep), support: true },
      ];
      const row = (t, isSupport) => {
        const on = selected.has(t.issue_number);
        return h("li", {}, h("button", {
          type: "button", class: `jira-pick${on ? " selected" : ""}${isSupport ? " support" : ""}`, "aria-pressed": on ? "true" : "false",
          onclick: (e) => {
            if (selected.delete(t.issue_number)) { e.currentTarget.classList.remove("selected"); e.currentTarget.setAttribute("aria-pressed", "false"); }
            else { selected.add(t.issue_number); e.currentTarget.classList.add("selected"); e.currentTarget.setAttribute("aria-pressed", "true"); }
            refreshAdd();
          },
        }, h("span", { class: "issue" }, `#${t.issue_number}`), " ", t.title, " ", openInTimeline(t.issue_number, t.url),
        isSupport ? h("span", { class: "badge support" }, "support") : null));
      };
      put(list, groups.filter((g) => g.tasks.length).flatMap((g) => [
        h("li", { class: `jira-pick-section${g.support ? " support" : ""}` }, g.name, h("span", { class: "pill" }, String(g.tasks.length))),
        ...g.tasks.map((t) => row(t, g.support)),
      ]));
      if (loaded) note.textContent = groups.some((g) => g.tasks.length) ? "" : "No tasks to add.";
    };
    addButton.addEventListener("click", async () => {
      addButton.disabled = true;
      try {
        const { cards: added } = await postJson("/api/jira/cards", { issues: [...selected], status });
        cards.push(...added);
        picker.close();
        draw();
      } catch (err) {
        note.textContent = err.message;
        refreshAdd();
      }
    });
    const search = h("input", { type: "search", placeholder: "Filter tasks…", "aria-label": "Filter tasks", oninput: () => drawList(search.value.trim().toLowerCase()) });
    put(picker,
      h("div", { class: "jira-pick-head" }, h("h2", {}, `Add to ${status}`),
        h("button", { type: "button", class: "btn ghost small", onclick: () => picker.close() }, "Close")),
      search, note, list,
      h("div", { class: "jira-pick-foot" }, h("span", { class: "note" }, "Click tasks to select several."), addButton));
    picker.showModal();
    search.focus();
    try {
      if (!cache.jiraMine || !cache.jiraSupport) {
        [cache.jiraMine, cache.jiraSupport] = (await Promise.all([api("/api/jira/my-tasks"), api("/api/jira/support-tasks")])).map((r) => r.tasks);
      }
      mine = cache.jiraMine;
      support = cache.jiraSupport;
      loaded = true;
      drawList("");
    } catch (e) {
      note.textContent = e.message;
    }
  };
  picker.addEventListener("click", (e) => { if (e.target === picker) picker.close(); });  // the backdrop

  const sectionColor = (status) => `--sc:${jiraHue(status, data.statuses)}`;

  const card = (c) => h("div", {
    style: sectionColor(c.status),
    class: `card jira-card${c.support ? " support" : " task"}`,
    title: c.title,
    "data-issue": c.issue_number,
    onmouseenter: () => hover(c.issue_number, true),
    onmouseleave: () => hover(c.issue_number, false),
    draggable: "true",
    ondragstart: (e) => {
      dragging = c;
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", c.issue_number);  // Firefox needs data to start a drag
      e.currentTarget.classList.add("dragging");
    },
    ondragend: (e) => {
      dragging = null;
      e.currentTarget.classList.remove("dragging");
      for (const el of view.querySelectorAll(".drop")) el.classList.remove("drop");
    },
  },
    h("div", { class: "head" },
      h("a", { class: "issue", href: `#/task/${c.issue_number}`, draggable: "false" }, `#${c.issue_number}`),
      c.support ? h("span", { class: "badge support s-pill", title: "Support task" }, "S") : null,
      c.url ? h("a", { class: "ext", href: c.url, target: "_blank", rel: "noopener noreferrer", draggable: "false", title: "Open in Timeline" }, "↗") : null),
    );

  const column = (status) => {
    const items = cards.filter((c) => c.status === status && (!query || `${c.issue_number} ${c.title}`.toLowerCase().includes(query)));
    return h("section", {
      class: "jira-col",
      style: sectionColor(status),
      onmouseenter: () => hoverSection(status, true),
      onmouseleave: () => hoverSection(status, false),
      ondragover: (e) => { if (dragging) { e.preventDefault(); e.dataTransfer.dropEffect = "move"; e.currentTarget.classList.add("drop"); } },
      ondragleave: (e) => { if (!e.currentTarget.contains(e.relatedTarget)) e.currentTarget.classList.remove("drop"); },
      ondrop: async (e) => {
        e.preventDefault();
        const moved = dragging;
        if (moved && moved.task) {  // from the task list: put it on the board
          error.hidden = true;
          try {
            const { cards: added } = await postJson("/api/jira/cards", { issues: [moved.task.issue_number], status });
            cards.push(...added);
            draw();
          } catch (err) {
            fail(err);
          }
          return;
        }
        if (!moved || moved.status === status) return;
        const before = moved.status;
        moved.status = status;  // shown at once, undone if the server refuses
        error.hidden = true;
        draw();
        try {
          await postJson(`/api/jira/cards/${moved.issue_number}/move`, { status });
        } catch (err) {
          moved.status = before;
          fail(err);
          draw();
        }
      },
    },
      h("h2", {}, h("span", { class: "jira-col-name" }, status), h("span", { class: "pill" }, String(items.length)),
        h("button", { type: "button", class: "jira-add", title: `Add a task to ${status}`, "aria-label": `Add a task to ${status}`,
          onclick: () => openPicker(status) }, "+")),
      items.map(card));
  };

  async function removeCard(c) {
    error.hidden = true;
    try {
      await api(`/api/jira/cards/${c.issue_number}`, { method: "DELETE", headers: { "X-Timeline-Desk": "1" } });
      cards.splice(cards.indexOf(c), 1);
      draw();
    } catch (err) {
      fail(err);
    }
  }

  // Left: the tasks on the board (name + ID + status). Hovering a card or a row highlights its twin.
  const taskList = h("ul", { class: "jira-tasks" });
  const taskNote = h("p", { class: "note", hidden: true });
  const hover = (issue, on) => {
    for (const el of view.querySelectorAll(`[data-issue="${issue}"]`)) el.classList.toggle("hl", on);
    const row = taskList.querySelector(`[data-issue="${issue}"]`);
    const sec = row && taskList.querySelector(`.jira-task-sec[data-status="${row.dataset.status}"]`);
    if (sec) sec.classList.toggle("hl-sec", on);  // its section header lights up too
    if (on && row) scrollIntoList(row);
  };
  const hoverSection = (status, on) => {
    let first = null;
    for (const el of taskList.children) {
      const hit = on && el.dataset.status === status;
      el.classList.toggle("hl-sec", hit);
      if (hit && !first) first = el;
    }
    if (first) scrollIntoList(first);
  };
  // Scrolls only the list (never the page) so the row is visible.
  const scrollIntoList = (row) => {
    const side = taskList.parentElement;
    const r = row.getBoundingClientRect(), s = side.getBoundingClientRect();
    if (r.top < s.top + 30) side.scrollTop -= s.top + 30 - r.top;
    else if (r.bottom > s.bottom) side.scrollTop += r.bottom - s.bottom + 4;
  };
  function drawTasks() {
    const rank = (c) => data.statuses.indexOf(c.status);
    const shown = cards.filter((c) => !query || `${c.issue_number} ${c.title}`.toLowerCase().includes(query))
      .sort((a, b) => rank(a) - rank(b));  // by column; the order they were added within one
    taskNote.textContent = "No tasks on the board.";
    taskNote.hidden = shown.length > 0;
    const rows = [];
    for (const status of data.statuses) {
      const inSection = shown.filter((c) => c.status === status);
      if (!inSection.length) continue;  // empty sections stay hidden
      rows.push(h("li", { class: "jira-task-sec", "data-status": status, style: sectionColor(status) },
        status, h("span", { class: "pill" }, String(inSection.length))));
      for (const c of inSection) {
        rows.push(h("li", {
          class: `jira-task${c.support ? " support" : ""}`,
          style: sectionColor(status),
          "data-issue": c.issue_number,
          "data-status": c.status,
          onmouseenter: () => hover(c.issue_number, true),
          onmouseleave: () => hover(c.issue_number, false),
        },
          h("div", { class: "row" },
            h("a", { class: "issue", href: `#/task/${c.issue_number}` }, `#${c.issue_number}`),
            h("span", { class: "pill status" }, c.status),
            c.closed ? h("span", { class: "badge closed" }, "closed") : null,
            c.support ? h("span", { class: "badge support" }, "support") : h("span", { class: "badge task" }, "task")),
          h("div", { class: "name" }, c.title)));
      }
    }
    put(taskList, rows);
  }
  function draw() { put(board, data.statuses.map(column)); drawTasks(); }

  // Dropping a card here is the only way to take it off the board.
  const trash = h("div", {
    class: "jira-trash",
    ondragover: (e) => { if (dragging) { e.preventDefault(); e.dataTransfer.dropEffect = "move"; e.currentTarget.classList.add("drop"); } },
    ondragleave: (e) => { if (!e.currentTarget.contains(e.relatedTarget)) e.currentTarget.classList.remove("drop"); },
    ondrop: (e) => {
      e.preventDefault();
      e.currentTarget.classList.remove("drop");
      if (dragging && !dragging.task) removeCard(dragging);
    },
  }, "Drop a card here to delete it");

  put(view,
    h("div", { class: "toolbar" },
      h("input", {
        type: "search", class: "jira-search", placeholder: "Search cards…", "aria-label": "Search cards",
        oninput: (e) => { query = e.target.value.trim().toLowerCase(); draw(); },
      }),
      trash),
    error,
    h("div", { class: "jira-layout" },
      h("aside", { class: "jira-side" }, h("h2", {}, "My tasks on the board"), taskNote, taskList),
      board),
    picker);
  draw();

  // Board height = the rest of the window below it; re-measured on resize.
  const fit = () => {
    if (!board.isConnected) { window.removeEventListener("resize", fit); return; }
    const top = board.getBoundingClientRect().top + window.scrollY;
    board.parentElement.style.setProperty("--board-h", `${Math.max(320, window.innerHeight - top - 52)}px`);
  };
  fit();
  window.addEventListener("resize", fit);
}

// ---- where a page's data comes from ---------------------------------------
// Icons only: a green signal = read from Timeline when the page opens or refreshes, a blue database = our
// stored copy (SQLite), an amber branch = git and GitHub. A page that mixes sources shows each one.
const SOURCE_ICONS = {
  live: { label: "Live from Timeline: fetched when you open or refresh the page",
    path: "M5 12.55a11 11 0 0 1 14.08 0M1.42 9a16 16 0 0 1 21.16 0M8.53 16.11a6 6 0 0 1 6.95 0M12 20h.01" },
  cache: { label: "From a copy kept in memory for a few minutes: refreshed by background checks and the Refresh button",
    path: "M12 6v6l4 2M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z" },
  db: { label: "From our database: stored by the desk, not fetched from Timeline now",
    path: "M12 3c4.97 0 9 1.34 9 3s-4.03 3-9 3-9-1.34-9-3 4.03-3 9-3zM3 6v6c0 1.66 4.03 3 9 3s9-1.34 9-3V6M3 12v6c0 1.66 4.03 3 9 3s9-1.34 9-3v-6" },
  git: { label: "From git and GitHub on this computer",
    path: "M6 3v12M18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM18 9a9 9 0 0 1-9 9" },
};
const PAGE_SOURCES = {
  planner: ["live", "cache", "db", "git"], workflows: ["live", "cache", "db"], changes: ["db"], timeline: ["db"],
  release: ["cache", "git"], task: ["live", "db", "git"], work: ["git"], jira: ["live", "db"],
};

function sourceIcons(kinds) {
  return h("span", { class: "sources" }, kinds.map((k) =>
    h("span", { class: `src ${k}`, role: "img", title: SOURCE_ICONS[k].label, "aria-label": SOURCE_ICONS[k].label }, svgIcon(SOURCE_ICONS[k].path, 14))));
}

for (const tab of document.querySelectorAll(".tabs a")) {
  if (PAGE_SOURCES[tab.dataset.view]) tab.append(sourceIcons(PAGE_SOURCES[tab.dataset.view]));
}

// ---- help -----------------------------------------------------------------
// The text is in help.js; what can be configured or lives in the code (intervals, workflow steps and buttons)
// is read from /api/about, so this page shows what the running service really does.
const HELP_PAGES = HELP.pages;
const HELP_SOURCES = ["live", "cache", "db", "git"];
const HELP_SOURCE_NAME = { live: "Live from Timeline", cache: "Short-lived copy in memory", db: "Our database", git: "git and GitHub" };

const HELP_TOPIC_ICON = {
  planner: "M3 4h18v4H3zM3 12h8v8H3zM15 12h6v8h-6z",
  workflows: "M6 3v12M18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM18 9a9 9 0 0 1-9 9",
  changes: "M7 4 3 8l4 4M3 8h14M17 20l4-4-4-4M21 16H7",
  timeline: "M12 2v20M12 6h7M12 12H5M12 18h7",
  release: "M12 2l4 8h-8zM12 10v12M8 22h8",
  task: "M9 11l3 3 8-8M20 12v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9",
  work: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  jira: "M4 4h5v16H4zM10 4h5v10h-5zM16 4h4v6h-4z",
  icons: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 8h.01M11 12h1v4h1",
  glossary: "M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5zM4 19.5V21h16",
  background: "M12 6v6l4 2M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z",
  workflow: "M5 12h14M13 6l6 6-6 6",
  setup: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 0 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 0 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 0 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 0 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  safety: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
  stored: "M12 3c4.97 0 9 1.34 9 3s-4.03 3-9 3-9-1.34-9-3 4.03-3 9-3zM3 6v6c0 1.66 4.03 3 9 3s9-1.34 9-3V6M3 12v6c0 1.66 4.03 3 9 3s9-1.34 9-3v-6",
  trouble: "M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01",
};

function socketText(sock) {
  if (sock.state === "connected") return sock.last_event ? `connected, ${sock.events} events (last: ${sock.last_event})` : "connected, no events yet";
  return sock.error ? `${sock.state}: ${sock.error}` : sock.state;
}

function minutesText(m) { return `${Number(m)} minute${Number(m) === 1 ? "" : "s"}`; }

async function showHelp(arg) {
  loading("Loading help…");
  const about = await api("/api/about").catch(() => null);
  const sched = about && about.schedule;
  const fill = (text) => text.replace("{planner_cache}", about ? String(about.planner_cache_minutes) : "5");
  const icon = (k) => h("span", { class: `src ${k}` }, svgIcon(SOURCE_ICONS[k].path, 16));
  const list = (items) => items && items.length ? h("ul", { class: "help-list" }, items.map((t) => h("li", {}, fill(t)))) : null;
  const block = (id, title, ...children) => h("section", { class: "help-page", id: `help-${id}` }, h("h3", {}, title), ...children);
  const rows = (pairs) => h("table", { class: "help-table" }, h("tbody", {}, pairs.map(([name, text]) =>
    h("tr", {}, h("th", {}, name), h("td", {}, text)))));
  const section = (p) => h("section", { class: "help-page", id: `help-${p.key}` },
    h("h3", {}, p.title, sourceIcons(p.sources), h("a", { class: "help-go", href: `#/${p.key}` }, "Open page")),
    h("p", { class: "help-purpose" }, p.purpose),
    h("table", { class: "help-table" },
      h("thead", {}, h("tr", {}, h("th", {}, "Shown"), h("th", {}, "Comes from"), h("th", {}, "Details"))),
      h("tbody", {}, p.from.map(([what, k, text]) => h("tr", {}, h("td", {}, what),
        h("td", { class: "help-src" }, icon(k), " ", HELP_SOURCE_NAME[k]), h("td", {}, fill(text)))))),
    h("dl", {},
      h("dt", {}, "How to use it"), h("dd", {}, list(p.use)),
      h("dt", {}, "When it changes"), h("dd", {}, fill(p.refresh)),
      h("dt", {}, "How it works"), h("dd", {}, fill(p.how)),
      p.know.length ? [h("dt", {}, "Good to know"), h("dd", {}, list(p.know))] : null));
  const steps = about && about.workflow_steps || [];
  const stepRows = steps.map((st) => h("tr", {},
    h("th", {}, st.title, st.optional ? h("small", { class: "note" }, " optional") : null, st.extra ? h("small", { class: "note" }, " only after a live bug") : null,
      st.side ? h("small", { class: "note" }, " side run") : null),
    h("td", {}, st.run
      ? [st.repeat ? h("p", {}, "Repeats: one item per run, until nothing remains.") : null,
        h("p", {}, h("strong", {}, "Reads: "), (st.reads || []).join("; ")),
        h("p", {}, h("strong", {}, "Writes: "), (st.writes || []).join("; ")),
        h("p", {}, h("strong", {}, "Won't: "), (st.wont || []).join("; ")),
        h("p", {}, h("strong", {}, "You approve when: "), st.approve || "")]
      : h("p", {}, st.about || ""))));
  const all = h("div", { class: "help-all" },
    block("icons", "Icons",
      h("p", { class: "note" }, "Shown next to each tab. A page that mixes sources shows each one. Hover an icon for its meaning."),
      h("ul", { class: "help-legend" }, HELP_SOURCES.map((k) => h("li", {}, icon(k), h("strong", {}, HELP_SOURCE_NAME[k]), h("span", {}, SOURCE_ICONS[k].label))))),
    block("glossary", "Words used", rows(HELP.glossary)),
    block("background", "Background checks",
      h("p", { class: "note" }, "While the service runs it keeps the database current, even when no page is open. It polls in tiers so it feels live without bursts of requests."),
      sched ? h("table", { class: "help-table" }, h("tbody", {},
        [["Heartbeat", `every ${minutesText(sched.heartbeat_minutes)}`, "Reads only the Now period of your planner (1 request) and the timeline of each watched task (1 request each). Compares the planner with the last check."],
         ["Full planner check", `when the heartbeat sees a change, or at least every ${minutesText(sched.full_minutes)}`, "Fetches all three periods and saves a snapshot if your tasks differ."],
         ["Stage lookups", `on a change, or every ${minutesText(sched.stage_minutes)}`, "One request per task for the stage bars stored with snapshots."],
         ["Active hours", `weekdays ${sched.active_hours}`, `Outside them there is one full check every ${minutesText(sched.off_hours_minutes)}.`],
         ["Live socket", about.socket && about.socket.enabled ? socketText(about.socket) : "off", "Timeline's own push channel. An event makes the heartbeat run right away (at most every 15 s, active hours only); polling stays as the fallback. Turn off with --no-socket or TIMELINE_SOCKET=0; TIMELINE_SOCKET_EVENTS limits which events count."],
         ["Spacing", `waits vary by ±${sched.jitter_percent}%; at least ${about.request_gap_ms} ms between requests`, "So requests arrive as a trickle, never a burst."],
         ["In-memory planner copy", `kept up to ${minutesText(about.planner_cache_minutes)}`, "What the Planner and Workflows pages read; the Refresh button replaces it."],
         ["Last planner check", about.last_planner_check ? niceTime(about.last_planner_check) : "none yet", "Recorded even when nothing changed."]]
          .map(([name, when, text]) => h("tr", {}, h("th", {}, name), h("td", {}, when), h("td", {}, text)))))
        : h("p", { class: "error" }, "Couldn't read the schedule from the service."),
      h("p", { class: "note" }, "Failures are logged, not fatal. Change the numbers with --heartbeat, --interval, --active-hours, or TIMELINE_HEARTBEAT_MINUTES, TIMELINE_SNAPSHOT_MINUTES, TIMELINE_ACTIVE_HOURS (then restart the service).")),
    HELP_PAGES.map(section),
    block("workflow", "Workflow steps and rules",
      h("p", {}, "A workflow takes one task through fixed steps. Nothing moves without a decision of yours: the page shows exactly the buttons that are possible right now, and the server refuses anything else. Every run and decision is saved with its time and note."),
      stepRows.length ? h("table", { class: "help-table" }, h("tbody", {}, stepRows)) : h("p", { class: "note" }, "Couldn't read the steps from the service."),
      about && about.workflow_buttons ? [h("h4", {}, "Buttons"), h("table", { class: "help-table" }, h("tbody", {}, about.workflow_buttons.map((b) =>
        h("tr", {}, h("th", {}, b.label), h("td", {}, b.help, b.needs_note ? h("em", {}, " Needs a written reason.") : null)))))] : null,
      about && about.run_statuses ? [h("h4", {}, "Run statuses"), h("p", {}, about.run_statuses.map((r) => r.label).join(" · "))] : null,
      h("p", { class: "note" }, "A run is an ordinary Claude Code session in a terminal, in the task's worktree. When it ends it writes a result file (status, summary, files, commits, questions) that the desk reads. Claude never pushes, opens pull requests or merges.")),
    block("setup", "Setup", rows(HELP.setup)),
    block("safety", "Safety", list(HELP.safety)),
    block("stored", "Where things are stored", h("table", { class: "help-table" }, h("tbody", {}, [
        ["Database", about ? about.database : "database/timeline_task.db", "SQLite: planner snapshots, watchlist and task events, workflows, runs, decisions, findings, Jira cards."],
        ["Task files", "timeline/<issue>/", "task.md and the documents a workflow writes (analysis, runs). Kept out of git."],
        ["Request log", "logs/timeline-requests-<date>.jsonl", "One line per call to Timeline: time, path, status, duration. No credentials or query text."],
        ["Boards and repos", "timeline-desk.json", "Which Timeline board belongs to which repo, and your name. Read again on every request."],
        ["This browser", "localStorage", "Only small conveniences: timeline filters, when you last saw the Changes page."],
      ].map(([name, where, text]) => h("tr", {}, h("th", {}, name), h("td", {}, h("code", {}, where)), h("td", {}, text)))))),
    block("trouble", "Troubleshooting", rows(HELP.trouble)));
  // The landing page is a dashboard of tiles; a tile opens that topic on its own page.
  const page = (key) => HELP_PAGES.find((p) => p.key === key);
  const TOPICS = [
    ...HELP_PAGES.map((p) => [p.key, p.title, p.purpose, HELP_TOPIC_ICON[p.key], p.sources]),
    ["icons", "Icons", "What the small icons next to each tab mean.", HELP_TOPIC_ICON.icons, HELP_SOURCES],
    ["glossary", "Words used", "Issue number, board, stage, watch, snapshot, worktree and the other terms.", HELP_TOPIC_ICON.glossary],
    ["background", "Background checks", sched ? `Keeps the database current: a check every ${minutesText(sched.heartbeat_minutes)}, a full one at least every ${minutesText(sched.full_minutes)}.` : "How the desk keeps itself current.", HELP_TOPIC_ICON.background],
    ["workflow", "Workflow steps and rules", "Every step, every button and every run status.", HELP_TOPIC_ICON.workflow],
    ["setup", "Setup", "Credentials, boards and repos, the service.", HELP_TOPIC_ICON.setup],
    ["safety", "Safety", "What the desk will never do, and who can reach it.", HELP_TOPIC_ICON.safety],
    ["stored", "Where things are stored", "The database, task files, logs and settings.", HELP_TOPIC_ICON.stored],
    ["trouble", "Troubleshooting", "Common problems and what to do.", HELP_TOPIC_ICON.trouble],
  ];
  const topic = arg && all.querySelector(`#help-${CSS.escape(arg)}`);
  if (topic) {
    put(view, h("div", { class: "toolbar" }, h("a", { href: "#/help", class: "back" }, "← All help")), topic);
    return;
  }
  const tile = ([key, title, blurb, path, sources]) => h("a", { class: "help-tile", href: `#/help/${key}` },
    h("span", { class: "help-tile-icon", "aria-hidden": "true" }, svgIcon(path, 26)),
    h("span", { class: "help-tile-body" }, h("strong", {}, title), h("span", {}, blurb)),
    sources ? sourceIcons(sources) : null);
  put(view,
    h("div", { class: "list-title" }, h("h2", {}, "Timeline Desk help")),
    HELP.intro.map((t) => h("p", { class: "help-intro" }, t)),
    h("p", { class: "note" }, "Click a tile to open it. The logo at the top left opens the help for the page you are on."),
    h("h3", { class: "help-group" }, "Pages"),
    h("div", { class: "help-tiles" }, TOPICS.filter(([k]) => page(k)).map(tile)),
    h("h3", { class: "help-group" }, "How it works"),
    h("div", { class: "help-tiles" }, TOPICS.filter(([k]) => !page(k)).map(tile)));
}

// ---- routing --------------------------------------------------------------
async function route(force) {
  const [, rawName = "planner", arg] = (location.hash || "#/planner").split("/");
  const name = rawName === "board" ? "planner" : rawName; // old #/board links keep working
  let label = "Planner";
  stopLive();
  for (const tab of document.querySelectorAll(".tabs a")) {
    if (tab.dataset.view === name) { tab.setAttribute("aria-current", "page"); label = tab.textContent; }
    else tab.removeAttribute("aria-current");
  }
  if (name === "help") label = "Help";
  document.getElementById("page-help").href = HELP_PAGES.some((p) => p.key === name) ? `#/help/${name}` : "#/help";
  document.title = `${name === "task" && /^\d+$/.test(arg || "") ? `${label} #${arg}` : label} · Timeline Desk`;
  try {
    if (name === "changes") await showChanges(false, force);
    else if (name === "timeline") await showTimeline(force);
    else if (name === "release") await showRelease(force);
    else if (name === "work") await showWork();
    else if (name === "jira") await showJira();
    else if (name === "workflows") await showWorkflows(arg);
    else if (name === "help") await showHelp(arg);
    else if (name === "task") await showTask(arg ? decodeURIComponent(arg) : "", force);
    else await showPlanner(force);
  } catch (error) {
    showError(error);
  }
}

document.getElementById("refresh").addEventListener("click", async () => {
  cache.statuses.clear();
  cache.planner = null;
  cache.projectTasks.clear();
  cache.workflows = null;
  cache.jiraMine = null;
  cache.jiraSupport = null;
  cache.supportIds = null;
  await route(true);
  getPlanner().catch(() => {});
});

window.addEventListener("hashchange", () => route(false));
route(false);
if (!/^#\/(planner|board)/.test(location.hash) && location.hash) getPlanner().catch(() => {});
