"""
The Jira board: the tasks you put on it and the column each one is in.

Cards live in the planner SQLite file (`<base>/database/timeline_task.db`,
tables `jira_cards` and `jira_events`), not in anything fetched from Timeline's
planner. You add a card from your own assigned tasks; only its number and title
are kept. Every add, move and removal writes a row to `jira_events` with no
`delivered_at`: that is the queue a Jira bot reads (`pending_updates`) and marks
done (`mark_delivered`). Nothing here talks to Jira.
"""
import sqlite3
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

import panteon_client as pc
import task_events

REMOVED = "Removed"  # the to_status the bot is told when a card is taken off the board
STATUSES = ["To Do", "In Progress", "In Dev", "Dev Done", "Ready for QA", "On QA", "Ready for Release", "Done"]


def _now_iso(now: Optional[datetime]) -> str:
    return (now or datetime.now()).isoformat(timespec="seconds")


def _status(value: Any) -> str:
    if value not in STATUSES:
        raise ValueError(f"Unknown status {value!r}.")
    return value


def list_cards(base_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = pc._open_planner_db(base_dir, create=False)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT issue_number, title, status, code, support FROM jira_cards ORDER BY added_at, issue_number"
        ).fetchall()
    finally:
        conn.close()
    return [{"issue_number": i, "title": t, "status": s, "url": pc.task_url(i, c), "support": bool(sp)}
            for i, t, s, c, sp in rows]


OVERVIEW_EVENTS = 150  # newest timeline events sent to the board's task panel
OVERVIEW_TEXT = 600    # characters kept of a comment or commit message in the history list
OVERVIEW_DESCRIPTION = 20000  # the description and the conversation are shown whole (capped only for safety)


def _clip(text: Optional[str], limit: int = OVERVIEW_TEXT) -> Optional[str]:
    text = (text or "").strip()
    if not text:
        return None
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def task_overview(issue: Any, headers: Dict[str, str]) -> Dict[str, Any]:
    """
    Everything the board's task panel shows for one task, from two Timeline calls (taskbyissue and the
    timeline) and nothing stored: header, the six workflow stages, deadline and hours, the newest timeline
    events (status and field changes, comments, commits) with long text clipped, and the whole conversation
    (description, comments) and commit list as the task page has them.
    """
    issue_id = pc.extract_issue_id(str(issue))
    raw = pc.get_task_by_issue(issue_id, headers)
    meta = pc.extract_task_meta(raw)
    status = pc.extract_task_status(raw, issue_id)
    items = pc.get_task_timeline(pc.extract_task_id_from_data(raw), headers)
    events = [e for e in (task_events.parse_event(i, issue_id) for i in items) if e]
    events.sort(key=lambda e: (e["occurred_at"], e["id"]), reverse=True)
    events = [{
        "id": e["id"], "at": e["occurred_at"], "kind": e["kind"], "field": e["field_name"],
        "old": e["old_value"], "new": e["new_value"], "by": e["employee"], "photo": e["photo"],
        "body": _clip(e["body"]), "url": e["url"],
    } for e in events[:OVERVIEW_EVENTS]]
    comments = [{**c, "description": _clip(c.get("description"), OVERVIEW_DESCRIPTION) or ""}
                for c in pc.parse_conversation_comments(items)]
    return {
        "issue_id": issue_id, "task_id": pc.extract_task_id_from_data(raw), "title": meta["title"],
        "creator": meta["creator"], "created": meta["created_date"],
        "tags": meta["tags"], "description": _clip(meta["description"], OVERVIEW_DESCRIPTION),
        "comments": comments, "commits": pc.parse_commits(items), "project_id": status["project_id"],
        "stages": status["stages"], "release_state": status["release_state"], "task_status": status["task_status"],
        "stage_status": status["stage_status"], "deadline": status["deadline"],
        "deadline_flag": pc.deadline_flag(status["deadline"]),
        "hours_spent": status["hours_spent"], "hours_estimated": status["hours_estimated"],
        "events": events,
    }


def _in_column_since(base_dir: Optional[str]) -> Dict[str, str]:
    """issue -> when its card last entered its column (the newest add or move in jira_events)."""
    conn = pc._open_planner_db(base_dir, create=False)
    if conn is None:
        return {}
    try:
        rows = conn.execute("SELECT issue_number, MAX(occurred_at) FROM jira_events GROUP BY issue_number").fetchall()
    finally:
        conn.close()
    return {str(i): at for i, at in rows}


def board(base_dir: Optional[str] = None) -> Dict[str, Any]:
    cards = list_cards(base_dir)
    since = _in_column_since(base_dir)
    for card in cards:
        card["since"] = since.get(str(card["issue_number"]))
    return {"statuses": STATUSES, "cards": cards}


def list_support_tasks(headers: Dict[str, str], projects: Iterable[Dict[str, Any]],
                       user_id: Optional[Any] = None) -> List[Dict[str, str]]:
    """The open tasks in each project's Support tab that are assigned to you, once
    each, as {"issue_number", "title", "code"}. One request per project."""
    seen: Dict[str, Dict[str, str]] = {}
    for project in projects:
        data = pc.fetch_project_tasks(project["id"], headers, user_id, code=project.get("code"), support=True)
        for group in data["groups"]:
            for task in group["tasks"]:
                issue = str(task["issue_number"])
                seen.setdefault(issue, {"issue_number": issue, "title": task["title"],
                                        "code": str(project["code"]) if project.get("code") else None})
    return list(seen.values())


def list_my_tasks(headers: Dict[str, str], projects: Iterable[Dict[str, Any]],
                  user_id: Optional[Any] = None) -> List[Dict[str, str]]:
    """Every open task assigned to you, once each, as {"issue_number", "title"}."""
    seen: Dict[str, Dict[str, str]] = {}
    for task in task_events.iter_my_open_tasks(headers, projects, user_id):
        seen.setdefault(task["issue_number"], {"issue_number": task["issue_number"], "title": task["title"]})
    return list(seen.values())


def fill_missing_codes(headers: Dict[str, str], projects: Iterable[Dict[str, Any]], user_id: Optional[Any] = None,
                       base_dir: Optional[str] = None) -> int:
    """Gives cards added without a list code (before links existed) theirs, from
    your assigned tasks, so they get a Timeline link. A task that is no longer
    one of yours is marked '' so it is not looked up on every load. Returns how
    many cards were updated."""
    conn = pc._open_planner_db(base_dir, create=False)
    if conn is None:
        return 0
    try:
        missing = {i for (i,) in conn.execute("SELECT issue_number FROM jira_cards WHERE code IS NULL")}
        if not missing:
            return 0
        codes: Dict[str, Any] = {}
        for task in task_events.iter_my_open_tasks(headers, projects, user_id):
            if task["issue_number"] in missing and task.get("code"):
                codes[task["issue_number"]] = str(task["code"])
        for issue in missing:
            conn.execute("UPDATE jira_cards SET code = ? WHERE issue_number = ? AND code IS NULL", (codes.get(issue, ""), issue))
        conn.commit()
    finally:
        conn.close()
    return len(missing)


def add_cards(tasks: List[Dict[str, Any]], status: str, base_dir: Optional[str] = None,
              now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Puts several tasks ({"issue_number", "title", "code", "support"}) on the board
    in one column, all or none: a task already on the board refuses the whole batch."""
    status = _status(status)
    tasks = [{**t, "issue_number": task_events._issue(t["issue_number"])} for t in tasks]
    when = _now_iso(now)
    conn = pc._open_planner_db(base_dir, create=True)
    try:
        on_board = {i for (i,) in conn.execute("SELECT issue_number FROM jira_cards")}
        for t in tasks:
            if t["issue_number"] in on_board:
                raise ValueError(f"Task #{t['issue_number']} is already on the board.")
        for t in tasks:
            conn.execute("INSERT INTO jira_cards (issue_number, title, status, added_at, updated_at, code, support) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (t["issue_number"], t["title"], status, when, when, str(t["code"]) if t.get("code") else None,
                          1 if t.get("support") else 0))
            conn.execute("INSERT INTO jira_events (issue_number, title, from_status, to_status, occurred_at) VALUES (?, ?, NULL, ?, ?)",
                         (t["issue_number"], t["title"], status, when))
        conn.commit()
    finally:
        conn.close()
    return [{"issue_number": t["issue_number"], "title": t["title"], "status": status,
             "url": pc.task_url(t["issue_number"], t.get("code")), "support": bool(t.get("support"))} for t in tasks]


def add_card(issue: Any, title: str, status: str, base_dir: Optional[str] = None,
             now: Optional[datetime] = None, code: Any = None, support: bool = False) -> Dict[str, Any]:
    return add_cards([{"issue_number": issue, "title": title, "code": code, "support": support}], status, base_dir, now)[0]


def move_card(issue: Any, status: str, base_dir: Optional[str] = None,
              now: Optional[datetime] = None) -> Dict[str, Any]:
    issue = task_events._issue(issue)
    status = _status(status)
    conn = pc._open_planner_db(base_dir, create=False)
    if conn is None:
        raise LookupError(f"Task #{issue} isn't on the board.")
    try:
        row = conn.execute("SELECT title, status FROM jira_cards WHERE issue_number = ?", (issue,)).fetchone()
        if row is None:
            raise LookupError(f"Task #{issue} isn't on the board.")
        title, previous = row
        if previous != status:  # dropping a card where it already is changes nothing
            when = _now_iso(now)
            conn.execute("UPDATE jira_cards SET status = ?, updated_at = ? WHERE issue_number = ?", (status, when, issue))
            conn.execute("INSERT INTO jira_events (issue_number, title, from_status, to_status, occurred_at) VALUES (?, ?, ?, ?, ?)",
                         (issue, title, previous, status, when))
            conn.commit()
    finally:
        conn.close()
    return {"issue_number": issue, "title": title, "status": status}


def remove_card(issue: Any, base_dir: Optional[str] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Takes a card off the board. The change log keeps it (to_status "Removed"), so the bot hears about it."""
    issue = task_events._issue(issue)
    conn = pc._open_planner_db(base_dir, create=False)
    if conn is None:
        raise LookupError(f"Task #{issue} isn't on the board.")
    try:
        row = conn.execute("SELECT title, status FROM jira_cards WHERE issue_number = ?", (issue,)).fetchone()
        if row is None:
            raise LookupError(f"Task #{issue} isn't on the board.")
        title, previous = row
        conn.execute("DELETE FROM jira_cards WHERE issue_number = ?", (issue,))
        conn.execute("INSERT INTO jira_events (issue_number, title, from_status, to_status, occurred_at) VALUES (?, ?, ?, ?, ?)",
                     (issue, title, previous, REMOVED, _now_iso(now)))
        conn.commit()
    finally:
        conn.close()
    return {"issue_number": issue, "title": title}


def pending_updates(base_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """Changes the Jira bot has not been given yet, oldest first."""
    conn = pc._open_planner_db(base_dir, create=False)
    if conn is None:
        return []
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, issue_number, title, from_status, to_status, occurred_at "
                            "FROM jira_events WHERE delivered_at IS NULL ORDER BY id").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def mark_delivered(ids: Iterable[int], base_dir: Optional[str] = None, now: Optional[datetime] = None) -> int:
    ids = [int(i) for i in ids]
    conn = pc._open_planner_db(base_dir, create=False)
    if conn is None or not ids:
        return 0
    try:
        marks = ",".join("?" * len(ids))
        changed = conn.execute(f"UPDATE jira_events SET delivered_at = ? WHERE delivered_at IS NULL AND id IN ({marks})",
                               [_now_iso(now), *ids]).rowcount
        conn.commit()
    finally:
        conn.close()
    return changed
