"""
Per-task event history for the Timeline page.

Timeline already keeps the full history of a task (status changes, comments,
commits, deadline, hours, group, …) under `/api/task/{task_id}/timeline`, each
event with a stable id, an author and a time. This module copies those events
into the SQLite file the planner snapshots use (`<base>/database/timeline_task.db`)
so one page can show them by time across tasks.

Only the tasks on the watchlist are fetched, never every task. Tasks are put on
the watchlist by hand, and only from the user's own open tasks. Syncing is
idempotent (`INSERT OR IGNORE` on Timeline's event id), so the first sync of a
newly watched task backfills its whole history and later syncs add only what
is new. Event times are Timeline's own clock (`dd.mm.yyyy HH:MM`, no seconds or
timezone) stored as naive ISO; ties are ordered by event id.
"""
import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, Iterable, Iterator, List, Optional

import requests

import panteon_client as pc

EVENT_KINDS = ("comment", "status", "field", "commit")
MAX_EVENTS_PAGE = 500
SYNC_WORKERS = 4


def parse_event_time(value: Any) -> str:
    """Timeline's `dd.mm.yyyy HH:MM` (or ISO) as naive ISO; '' if unparseable."""
    value = str(value or "").strip()
    dmy = re.match(r"(\d{2})\.(\d{2})\.(\d{4})(?:\s+(\d{2}):(\d{2}))?", value)
    if dmy:
        d, m, y, hh, mm = dmy.groups()
        return f"{y}-{m}-{d}T{hh or '00'}:{mm or '00'}:00"
    iso = re.match(r"(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2})(?::(\d{2}))?)?", value)
    if iso:
        return f"{iso.group(1)}T{iso.group(2) or '00:00'}:{iso.group(3) or '00'}"
    return ""


def event_kind(field_name: str) -> str:
    if field_name == "Comment Added":
        return "comment"
    if field_name == "Commit Added":
        return "commit"
    if field_name == "Status":
        return "status"
    return "field"


def parse_event(item: Dict[str, Any], issue_number: Any) -> Optional[Dict[str, Any]]:
    """One timeline item as a `task_events` row, or None for something unusable."""
    if not isinstance(item, dict) or not isinstance(item.get("id"), int):
        return None
    when = parse_event_time(item.get("created_at"))
    if not when:
        return None
    field_name = str(item.get("field_name") or "").strip() or "Change"
    kind = event_kind(field_name)
    employee = item.get("employee") if isinstance(item.get("employee"), dict) else {}
    name = employee.get("name") or item.get("employee_name") or None
    photo = employee.get("photo") if isinstance(employee.get("photo"), str) else None
    body = url = None
    old = new = None
    if kind == "comment":
        comment = item.get("task_comment") if isinstance(item.get("task_comment"), dict) else {}
        body = (comment.get("description") or item.get("description") or "").strip() or None
    elif kind == "commit":
        commit = item.get("task_commit") if isinstance(item.get("task_commit"), dict) else {}
        body = (commit.get("description") or "").strip() or None
        url = commit.get("commit_url") or None
    else:
        # Comment and commit rows carry placeholder values ("Old value"); only
        # real field changes keep theirs.
        old, new = item.get("original_value"), item.get("updated_value")
        if item.get("checklist_title"):
            field_name = f"{field_name}: {item['checklist_title']}"
    return {
        "id": item["id"], "issue_number": str(issue_number), "occurred_at": when, "kind": kind,
        "field_name": field_name,
        "old_value": None if old is None else str(old), "new_value": None if new is None else str(new),
        "employee": name, "photo": photo, "body": body, "url": url,
        "raw": json.dumps(item, ensure_ascii=False),
    }


# --------------------------------------------------------------------------- #
# Watchlist
# --------------------------------------------------------------------------- #
def _now_iso(now: Optional[datetime]) -> str:
    return (now or datetime.now()).isoformat(timespec="seconds")


def _issue(value: Any) -> str:
    text = str(value).strip()
    if not text.isdigit():
        raise ValueError("Issue ID must be a number.")
    return text


def list_watchlist(base_dir: Optional[str] = None, active_only: bool = False) -> List[Dict[str, Any]]:
    conn = pc._open_planner_db(base_dir, create=False)
    if conn is None:
        return []
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT w.*, (SELECT COUNT(*) FROM task_events e WHERE e.issue_number = w.issue_number) AS events "
            "FROM watchlist w " + ("WHERE w.active = 1 " if active_only else "") + "ORDER BY w.added_at, w.issue_number"
        ).fetchall()
    finally:
        conn.close()
    return [{**dict(r), "active": bool(r["active"]), "url": pc.task_url(r["issue_number"], r["code"])} for r in rows]


def watch_task(issue: Any, code: Any = None, title: str = "", base_dir: Optional[str] = None,
               now: Optional[datetime] = None) -> None:
    """Puts a task on the watchlist (or turns a paused one back on). Nothing is fetched here."""
    issue = _issue(issue)
    conn = pc._open_planner_db(base_dir, create=True)
    try:
        conn.execute(
            "INSERT INTO watchlist (issue_number, code, title, active, added_at) VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(issue_number) DO UPDATE SET active = 1, last_error = NULL, "
            "code = COALESCE(excluded.code, code), title = CASE WHEN excluded.title != '' THEN excluded.title ELSE title END",
            (issue, str(code) if code else None, title or "", _now_iso(now)),
        )
        conn.commit()
    finally:
        conn.close()


def unwatch_task(issue: Any, base_dir: Optional[str] = None) -> bool:
    """Stops syncing a task. Its stored events stay, so its history isn't lost."""
    issue = _issue(issue)
    conn = pc._open_planner_db(base_dir, create=False)
    if conn is None:
        return False
    try:
        changed = conn.execute("UPDATE watchlist SET active = 0 WHERE issue_number = ? AND active = 1", (issue,)).rowcount
        conn.commit()
    finally:
        conn.close()
    return bool(changed)


def iter_my_open_tasks(headers: Dict[str, str], projects: Iterable[Dict[str, Any]],
                       user_id: Optional[Any] = None) -> Iterator[Dict[str, Any]]:
    """The user's own open tasks (every tab of every configured project), one at
    a time and fetching lazily, so a caller that stops early saves requests."""
    for project in projects:
        data = pc.fetch_project_tasks(project["id"], headers, user_id, code=project.get("code"))
        for tab in [t["name"] for t in data["tabs"]] or [None]:
            page = data if tab in (None, data["tab"]) else pc.fetch_project_tasks(
                project["id"], headers, user_id, code=project.get("code"), tab=tab)
            for group in page["groups"]:
                for task in group["tasks"]:
                    yield {"issue_number": str(task["issue_number"]), "title": task["title"], "code": project.get("code")}


# --------------------------------------------------------------------------- #
# Sync
# --------------------------------------------------------------------------- #
def _fetch_one(row: Dict[str, Any], headers: Dict[str, str], refresh_meta: bool = True) -> Dict[str, Any]:
    issue = row["issue_number"]
    try:
        if refresh_meta or not row.get("task_id"):
            raw = pc.get_task_by_issue(issue, headers)
            meta = pc.extract_task_meta(raw)
            task_id = pc.extract_task_id_from_data(raw)
        else:  # the internal id never changes: one request (the timeline) instead of two
            meta, task_id = {}, row["task_id"]
        items = pc.get_task_timeline(task_id, headers)
        return {"issue": issue, "task_id": task_id, "meta": meta, "items": items}
    except PermissionError:
        raise
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else None
        message = "not found in Timeline" if code in (404, 422) else f"HTTP {code}"
        return {"issue": issue, "error": message}
    except (requests.RequestException, ValueError, KeyError) as exc:
        return {"issue": issue, "error": str(exc) or exc.__class__.__name__}


def sync_watchlist(headers: Dict[str, str], base_dir: Optional[str] = None, issues: Optional[Iterable[Any]] = None,
                   now: Optional[datetime] = None, refresh_meta: bool = True) -> Dict[str, Any]:
    """
    Fetches the timeline of every active watched task (or just `issues`) and
    stores the events not seen before. A task that is closed is synced
    one last time and then paused. With `refresh_meta` false a task whose id is
    already known skips the taskbyissue call, so it is not re-checked for being
    closed or retitled until the next sync that refreshes. One task failing is recorded on its row and
    doesn't stop the others; an authentication error does.
    Returns {"tasks", "new_events", "errors": {issue: message}, "paused": [issue]}.
    """
    wanted = {_issue(i) for i in issues} if issues is not None else None
    rows = [r for r in list_watchlist(base_dir, active_only=True) if wanted is None or r["issue_number"] in wanted]
    result: Dict[str, Any] = {"tasks": len(rows), "new_events": 0, "errors": {}, "paused": []}
    if not rows:
        return result
    with ThreadPoolExecutor(max_workers=SYNC_WORKERS) as pool:
        fetched = list(pool.map(lambda r: _fetch_one(r, headers, refresh_meta), rows))
    stamp = _now_iso(now)
    conn = pc._open_planner_db(base_dir, create=True)
    try:
        for got in fetched:
            issue = got["issue"]
            if "error" in got:
                result["errors"][issue] = got["error"]
                conn.execute("UPDATE watchlist SET last_error = ? WHERE issue_number = ?", (got["error"], issue))
                continue
            before = conn.total_changes
            for item in got["items"]:
                event = parse_event(item, issue)
                if event:
                    conn.execute(
                        "INSERT OR IGNORE INTO task_events (id, issue_number, occurred_at, kind, field_name, old_value, "
                        "new_value, employee, photo, body, url, raw) VALUES (:id, :issue_number, :occurred_at, :kind, "
                        ":field_name, :old_value, :new_value, :employee, :photo, :body, :url, :raw)", event)
            result["new_events"] += conn.total_changes - before
            still_open = got["meta"].get("status") != "closed"
            if not still_open:
                result["paused"].append(issue)
            conn.execute(
                "UPDATE watchlist SET task_id = ?, title = COALESCE(NULLIF(?, ''), title), active = ?, "
                "last_synced_at = ?, last_error = ? WHERE issue_number = ?",
                (got["task_id"], got["meta"].get("title") or "", int(still_open), stamp,
                 None if still_open else "Task is closed", issue))
        conn.commit()
    finally:
        conn.close()
    return result


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
def events_marker(base_dir: Optional[str] = None) -> Dict[str, int]:
    """{"max_id", "count"} of the stored events: a local read, cheap enough for the Timeline page
    to poll every couple of seconds and re-read the feed only when it changed."""
    conn = pc._open_planner_db(base_dir, create=False)
    if conn is None:
        return {"max_id": 0, "count": 0}
    try:
        max_id, count = conn.execute("SELECT COALESCE(MAX(id), 0), COUNT(*) FROM task_events").fetchone()
    finally:
        conn.close()
    return {"max_id": int(max_id), "count": int(count)}


def list_events(base_dir: Optional[str] = None, issues: Optional[Iterable[Any]] = None,
                kinds: Optional[Iterable[str]] = None, limit: int = 200,
                before: Optional[str] = None) -> Dict[str, Any]:
    """
    Stored events across all tasks, newest first: {"events", "tasks", "next"}.
    `issues` and `kinds` narrow it; `before` is the `next` cursor of the
    previous page. `tasks` maps each issue shown to its title and URL.
    """
    kinds = [k for k in (kinds or []) if k in EVENT_KINDS]
    issues = [_issue(i) for i in (issues or [])]
    limit = max(1, min(int(limit), MAX_EVENTS_PAGE))
    where, args = [], []
    if issues:
        where.append(f"issue_number IN ({','.join('?' * len(issues))})")
        args += issues
    if kinds:
        where.append(f"kind IN ({','.join('?' * len(kinds))})")
        args += kinds
    if before:
        cursor = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T[\d:]{8})\|(\d+)", before)
        if not cursor:
            raise ValueError("Bad cursor.")
        where.append("(occurred_at < ? OR (occurred_at = ? AND id < ?))")
        args += [cursor.group(1), cursor.group(1), int(cursor.group(2))]
    conn = pc._open_planner_db(base_dir, create=False)
    if conn is None:
        return {"events": [], "tasks": {}, "next": None}
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, issue_number, occurred_at, kind, field_name, old_value, new_value, employee, photo, body, url "
            "FROM task_events" + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY occurred_at DESC, id DESC LIMIT ?", args + [limit + 1]).fetchall()
        shown = {r["issue_number"] for r in rows[:limit]}
        titles = {r["issue_number"]: r for r in conn.execute("SELECT issue_number, title, code FROM watchlist")}
    finally:
        conn.close()
    page = rows[:limit]
    events = [dict(r) for r in page]
    return {
        "events": events,
        "tasks": {i: {"title": titles[i]["title"] if i in titles else "",
                      "url": pc.task_url(i, titles[i]["code"]) if i in titles else None} for i in shown},
        "next": f"{page[-1]['occurred_at']}|{page[-1]['id']}" if len(rows) > limit else None,
    }
