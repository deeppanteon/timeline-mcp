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


def board(base_dir: Optional[str] = None) -> Dict[str, Any]:
    return {"statuses": STATUSES, "cards": list_cards(base_dir)}


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
