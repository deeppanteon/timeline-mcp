"""
Release coverage: for each ticket you committed code for, which of its commits
are on qa-release and on production, and which are missing. Plan and the
reasoning behind each rule: docs/release-coverage-plan.md.

Read-only and git-only. It reads the `origin/*` refs of a checkout with fixed
argument lists (no shell); it never merges, pushes or changes a worktree.
`fetch_branches` is the one call that touches the checkout (it updates the
remote-tracking refs) and is separate, so the caller decides when it runs.

An old ticket that is fully on production (its newest commit is over STATUS_DAYS old) is hidden the same way, in
`coverage_retired`, and comes back as soon as you commit something newer for it.

Only tickets you are subscribed to in Timeline are included: `build_coverage(subscribed=…)` intersects the tickets found
from your commits with that set before any git work, and hides stored tickets outside it (kept, not deleted, in case
you subscribe again).

A ticket Timeline reports as closed is left out completely: its stored commits are deleted and it is remembered in
`coverage_closed`, so later checks do not look it up in Timeline or read its commits from git.

Commits seen are stored in the planner SQLite file (`coverage_commits`) with when each was first and last on QA and
on production, so a commit that was there and is gone (qa-release reset or rebuilt) shows as dropped, not just missing.

A commit belongs to a ticket when it is on a branch whose name carries the
ticket number (its own commits only, see `branch_commits`) or when its message
names the ticket (`TL #4542`) on any remote branch. "Released" is git
ancestry: the commit is reachable from `origin/qa-release` or from the
production branch. Teams merge by PR merge commits or directly, never by
squash, so no PR data is needed; `git cherry` only covers the odd cherry-pick.
"""
import datetime as dt
import re
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import panteon_client as pc
from release_check import (
    REPO_SPECS,
    SYNC_BRANCHES,
    ReleaseCheckError,
    Runner,
    _open,
    _Repo,
    _subprocess_runner,
    task_id_from_branch,
)

QA_BRANCH = "qa-release"
STATUS_DAYS = 90  # a shipped ticket is looked up in Timeline only while its newest commit is this recent; older ones are hidden
DEFAULT_SINCE = "12 months ago"  # how far back your commits are searched for tickets
# Worst first: how a ticket row is ordered and which repo decides its state.
STATE_ORDER = ["dropped", "not_released", "not_on_prod", "partial", "on_qa", "shipped", "n/a"]
_FIELDS = "%H%x1f%an%x1f%ae%x1f%aI%x1f%s"
_SUBJECT_ID = re.compile(r"TL\s*#\s*(\d{3,6})\b", re.IGNORECASE)


def prod_branch(repo_name: str) -> str:
    """The branch that updates the production server (master for duell-admin, prod-release for duell-pos)."""
    return REPO_SPECS[repo_name]["bases"][-1]


def work_base(repo_name: str) -> str:
    """The branch ticket branches start from; a branch's own commits are those not on it."""
    return REPO_SPECS[repo_name]["work_base"]


def _ref(name: str) -> str:
    return f"origin/{name}"


def message_regex(issue_id: str) -> str:
    """ERE for a commit message naming the ticket: the number, not part of a longer number."""
    return rf"(^|[^0-9]){re.escape(str(issue_id))}([^0-9]|$)"


def parse_commits(out: str) -> List[Dict[str, Any]]:
    """`git log --format=_FIELDS` output as commit dicts."""
    commits = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) < 5:
            continue
        sha, author, email, date, subject = parts[0], parts[1], parts[2], parts[3], "\x1f".join(parts[4:])
        commits.append({"sha": sha, "short": sha[:10], "author": author, "email": email, "date": date, "subject": subject})
    return commits


def fetch_branches(repo: _Repo) -> None:
    """Update `origin/*` from GitHub. Only remote-tracking refs change: no merge, no push, no worktree change."""
    repo.run("git", "fetch", "--quiet", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*", timeout=180)


def ticket_branches(repo: _Repo, issue_id: str) -> List[str]:
    """Remote branches whose name carries the ticket number, from the local `origin/*` refs."""
    out = repo.run("git", "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin")
    names = []
    for ref in out.splitlines():
        name = ref[len("origin/"):] if ref.startswith("origin/") else ""
        if name and name not in SYNC_BRANCHES and task_id_from_branch(name) == str(issue_id):
            names.append(name)
    return sorted(names)


def branch_commits(repo: _Repo, branch: str, base: str) -> List[Dict[str, Any]]:
    """
    A branch's own commits: its first-parent line, no merges, minus what the work base already has. First-parent
    keeps a merge of qa-release *into* the branch from pulling other people's commits in. If the base ref is
    missing the branch cannot be told apart from its history, so that is an error, not an empty list.
    """
    out = repo.run("git", "log", "--first-parent", "--no-merges", f"--format={_FIELDS}", _ref(branch), "--not", _ref(base))
    return parse_commits(out)


def message_commits(repo: _Repo, issue_id: str) -> List[Dict[str, Any]]:
    """Commits on any remote branch whose message names the ticket (catches a fix made on another branch)."""
    out = repo.run("git", "log", "--remotes=origin", "--no-merges", "-E", f"--grep={message_regex(issue_id)}",
                   f"--format={_FIELDS}", timeout=60)
    wanted = re.compile(message_regex(issue_id))
    return [c for c in parse_commits(out) if wanted.search(c["subject"])]


def release_sets(repo: _Repo, repo_name: str) -> Dict[str, Set[str]]:
    """Every commit reachable from qa-release and from the production branch."""
    return {
        "qa": set(repo.run("git", "rev-list", _ref(QA_BRANCH), timeout=90).split()),
        "prod": set(repo.run("git", "rev-list", _ref(prod_branch(repo_name)), timeout=90).split()),
    }


def patch_equivalent(repo: _Repo, target: str, sha: str) -> bool:
    """A cherry-pick of the commit is on `target` (same patch, other sha). False for a root commit or on error."""
    try:
        out = repo.run("git", "cherry", _ref(target), sha, f"{sha}^", timeout=60)
    except ReleaseCheckError:
        return False
    return any(line.startswith("- ") for line in out.splitlines())


def coverage_state(total: int, on_qa: int, on_prod: int, dropped: bool = False) -> str:
    """
    `on_qa` counts commits that reached qa-release or went past it. n/a: no commits here; dropped: a commit that was
    on QA or production is gone; shipped: all on production; on_qa: all on QA, not all on production;
    not_released: none on QA or production; not_on_prod: some reached QA, some did not, and none is on production;
    partial: the rest (some on production, not all).
    """
    if total == 0:
        return "n/a"
    if dropped:
        return "dropped"
    if on_prod == total:
        return "shipped"
    if on_qa == total:
        return "on_qa"
    if on_qa == 0 and on_prod == 0:
        return "not_released"
    if on_prod == 0:
        return "not_on_prod"
    return "partial"


def summarize(issue_id: str, repo_name: str, branches: List[str], commits: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The ticket row for one repo from its commits (each with qa, prod and, once stored, dropped_qa / dropped_prod)."""
    commits = sorted(commits, key=lambda c: (c["date"] or "", c["sha"]), reverse=True)
    on_qa = sum(1 for c in commits if c["qa"])
    on_prod = sum(1 for c in commits if c["prod"])
    dropped = any(c.get("dropped_qa") or c.get("dropped_prod") for c in commits)
    spec = REPO_SPECS.get(repo_name)
    return {
        "issue": str(issue_id),
        "repo": repo_name,
        "url": f"https://github.com/{spec['slug']}" if spec else None,  # a commit is <url>/commit/<sha>
        "branches": branches,
        "total": len(commits),
        "on_qa": on_qa,
        "on_prod": on_prod,
        "missing_qa": [c["sha"] for c in commits if not c["qa"]],
        "missing_prod": [c["sha"] for c in commits if not c["prod"]],
        "dropped": [c["sha"] for c in commits if c.get("dropped_qa") or c.get("dropped_prod")],
        # Dates of the newest commit that is there, not of when it arrived (qa_first_seen has that, once stored).
        "last_commit_at": commits[0]["date"] if commits else None,
        "last_qa_commit_at": next((c["date"] for c in commits if c["qa"]), None),
        "last_prod_commit_at": next((c["date"] for c in commits if c["prod"]), None),
        "commits": commits,
        "state": coverage_state(len(commits), on_qa, on_prod, dropped),
    }


def ticket_coverage(repo: _Repo, repo_name: str, issue_id: str, sets: Dict[str, Set[str]]) -> Dict[str, Any]:
    """One ticket in one repo: its commits, which are on QA and production, and a state."""
    issue_id = str(issue_id)
    branches = ticket_branches(repo, issue_id)
    found: Dict[str, Dict[str, Any]] = {}
    for branch in branches:
        for commit in branch_commits(repo, branch, work_base(repo_name)):
            found[commit["sha"]] = dict(commit, branch=branch)
    for commit in message_commits(repo, issue_id):
        found.setdefault(commit["sha"], dict(commit, branch=None))

    commits = []
    for commit in found.values():
        prod = commit["sha"] in sets["prod"] or patch_equivalent(repo, prod_branch(repo_name), commit["sha"])
        qa = commit["sha"] in sets["qa"] or prod or patch_equivalent(repo, QA_BRANCH, commit["sha"])
        commits.append(dict(commit, qa=qa, prod=prod))
    return summarize(issue_id, repo_name, branches, commits)


def _parse_date(text: Optional[str]) -> Optional[dt.datetime]:
    try:
        stamp = dt.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)


def _later(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """The later of two commit dates (either may be missing or unreadable)."""
    da, db = _parse_date(a), _parse_date(b)
    if da and db:
        return a if da >= db else b
    return a if da else b if db else None


def discover_issue_dates(repo: _Repo, authors: Sequence[str], since: str) -> Dict[str, str]:
    """
    Ticket numbers you committed code for since `since` (a git date such as "12 months ago"), each with the date of
    your newest such commit. Found from your non-merge commits on any remote branch: the number in the branch name,
    or `TL #N` in the message.
    """
    if not authors:
        return {}
    args = ["git", "log", "--remotes=origin", "--source", "--no-merges", f"--since={since}", "--format=%S%x1f%aI%x1f%s"]
    args += [f"--author={a}" for a in authors]
    found: Dict[str, str] = {}

    def note(issue: str, date: str) -> None:
        old = _parse_date(found.get(issue))
        new = _parse_date(date)
        if issue not in found or (new and (not old or new > old)):
            found[issue] = date

    for line in repo.run(*args, timeout=120).splitlines():
        ref, _, rest = line.partition("\x1f")
        date, _, subject = rest.partition("\x1f")
        name = ref[len("refs/remotes/origin/"):] if ref.startswith("refs/remotes/origin/") else ref
        if name and name not in SYNC_BRANCHES:
            issue = task_id_from_branch(name)
            if issue:
                note(issue, date)
        m = _SUBJECT_ID.search(subject)
        if m:
            note(m.group(1), date)
    return found


def discover_issues(repo: _Repo, authors: Sequence[str], since: str) -> List[str]:
    """The ticket numbers of `discover_issue_dates`, in order."""
    return sorted(discover_issue_dates(repo, authors, since), key=int)


def repo_coverage(repo: _Repo, repo_name: str, issues: Iterable[str],
                  sets: Optional[Dict[str, Set[str]]] = None) -> Dict[str, Dict[str, Any]]:
    """{issue: ticket_coverage} for the tickets that have commits in this repo; the rest are left out."""
    sets = sets or release_sets(repo, repo_name)
    rows = {}
    for issue in dict.fromkeys(str(i) for i in issues):
        row = ticket_coverage(repo, repo_name, issue, sets)
        if row["total"]:
            rows[issue] = row
    return rows


# ---- history ---------------------------------------------------------------
def _now_iso(now: Optional[dt.datetime] = None) -> str:
    return (now or dt.datetime.now(dt.timezone.utc)).replace(microsecond=0, tzinfo=None).isoformat()


_UPSERT = (
    "INSERT INTO coverage_commits (repo, sha, issue_number, branch, author, email, committed_at, subject, first_seen, "
    "in_qa, in_prod, qa_first_seen, qa_last_seen, prod_first_seen, prod_last_seen) "
    "VALUES (:repo, :sha, :issue, :branch, :author, :email, :date, :subject, :now, :qa, :prod, "
    ":qa_first, :qa_last, :prod_first, :prod_last) "
    "ON CONFLICT(repo, sha) DO UPDATE SET issue_number = excluded.issue_number, "
    "branch = COALESCE(excluded.branch, branch), in_qa = excluded.in_qa, in_prod = excluded.in_prod, "
    "qa_first_seen = COALESCE(qa_first_seen, excluded.qa_first_seen), "
    "qa_last_seen = COALESCE(excluded.qa_last_seen, qa_last_seen), "
    "prod_first_seen = COALESCE(prod_first_seen, excluded.prod_first_seen), "
    "prod_last_seen = COALESCE(excluded.prod_last_seen, prod_last_seen)"
)


def apply_history(base_dir: Optional[str], repo_name: str, rows: Dict[str, Dict[str, Any]], sets: Dict[str, Set[str]],
                  now: Optional[dt.datetime] = None, only: Optional[Set[str]] = None) -> Dict[str, Dict[str, Any]]:
    """
    Stores this check and returns the rows with what the history adds: for every commit when it was first and last on
    QA and production, and `dropped_qa` / `dropped_prod` when it was there before and is not now (qa-release was reset
    or rebuilt). Commits stored earlier are kept even when no branch or message finds them any more (a deleted branch,
    a rewritten history), so a ticket does not vanish from the list. Drops are only reported while both release sets
    are non-empty, so a half-failed read cannot call everything dropped. With `only`, stored tickets outside it are
    neither returned nor touched.
    """
    stamp = _now_iso(now)
    trust = bool(sets["qa"]) and bool(sets["prod"])
    conn = pc._open_planner_db(base_dir, create=True)
    try:
        conn.row_factory = sqlite3.Row
        stored: Dict[str, Dict[str, sqlite3.Row]] = {}
        for r in conn.execute("SELECT * FROM coverage_commits WHERE repo = ?", (repo_name,)):
            stored.setdefault(r["issue_number"], {})[r["sha"]] = r
        out: Dict[str, Dict[str, Any]] = {}
        writes = []
        wanted = set(rows) | set(stored)
        if only is not None:
            wanted &= only
        for issue in sorted(wanted, key=int):
            found = {c["sha"]: c for c in rows[issue]["commits"]} if issue in rows else {}
            for sha, r in stored.get(issue, {}).items():
                if sha not in found:  # no longer found: judged by ancestry alone
                    prod = sha in sets["prod"]
                    found[sha] = {"sha": sha, "short": sha[:10], "author": r["author"], "email": r["email"],
                                  "date": r["committed_at"], "subject": r["subject"], "branch": r["branch"],
                                  "qa": prod or sha in sets["qa"], "prod": prod}
            commits = []
            for sha, c in found.items():
                old = stored.get(issue, {}).get(sha)
                c = dict(c)
                c["qa_first_seen"] = (old["qa_first_seen"] if old else None) or (stamp if c["qa"] else None)
                c["prod_first_seen"] = (old["prod_first_seen"] if old else None) or (stamp if c["prod"] else None)
                c["dropped_qa"] = bool(trust and old and old["qa_last_seen"] and not c["qa"])
                c["dropped_prod"] = bool(trust and old and old["prod_last_seen"] and not c["prod"])
                writes.append({"repo": repo_name, "sha": sha, "issue": issue, "branch": c.get("branch"),
                               "author": c.get("author"), "email": c.get("email"), "date": c.get("date"),
                               "subject": c.get("subject"), "now": stamp, "qa": int(c["qa"]), "prod": int(c["prod"]),
                               "qa_first": c["qa_first_seen"], "qa_last": stamp if c["qa"] else None,
                               "prod_first": c["prod_first_seen"], "prod_last": stamp if c["prod"] else None})
                commits.append(c)
            branches = rows[issue]["branches"] if issue in rows else []
            out[issue] = summarize(issue, repo_name, branches, commits)
        with conn:
            conn.executemany(_UPSERT, writes)
            conn.execute("INSERT INTO coverage_checks (repo, checked_at, error) VALUES (?, ?, NULL) "
                         "ON CONFLICT(repo) DO UPDATE SET checked_at = excluded.checked_at, error = NULL", (repo_name, stamp))
    finally:
        conn.close()
    return out


def record_failure(base_dir: Optional[str], repo_name: str, error: str, now: Optional[dt.datetime] = None) -> None:
    """Notes that a repo could not be read; its stored commits are left as they were (nothing is called dropped)."""
    conn = pc._open_planner_db(base_dir, create=True)
    try:
        with conn:
            conn.execute("INSERT INTO coverage_checks (repo, checked_at, error) VALUES (?, ?, ?) "
                         "ON CONFLICT(repo) DO UPDATE SET checked_at = excluded.checked_at, error = excluded.error",
                         (repo_name, _now_iso(now), error[:500]))
    finally:
        conn.close()


def last_checks(base_dir: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """{repo: {checked_at, error}} of the latest check per repo; empty when nothing was checked (no file is created)."""
    conn = pc._open_planner_db(base_dir, create=False)
    if conn is None:
        return {}
    try:
        return {r[0]: {"checked_at": r[1], "error": r[2]}
                for r in conn.execute("SELECT repo, checked_at, error FROM coverage_checks")}
    finally:
        conn.close()


def build_repo_coverage(repo: _Repo, repo_name: str, issues: Iterable[str], base_dir: Optional[str] = None,
                        now: Optional[dt.datetime] = None, only: Optional[Set[str]] = None) -> Dict[str, Dict[str, Any]]:
    """One repo's rows with history: reads git, stores the check. A failure is recorded and re-raised."""
    try:
        sets = release_sets(repo, repo_name)
        rows = repo_coverage(repo, repo_name, issues, sets)
    except ReleaseCheckError as exc:
        record_failure(base_dir, repo_name, str(exc), now)
        raise
    return apply_history(base_dir, repo_name, rows, sets, now, only)


# ---- sections --------------------------------------------------------------
# Timeline's stage says what to expect of the code, so the page groups tickets by it: a ticket still in development
# is not expected on QA or production, an approved one is expected on production.
SECTION_ORDER = ["dev", "qa", "qa_blocked", "approved", "live", "unknown"]
_ATTENTION_STATES = {"not_released", "not_on_prod", "partial"}


def section_for(timeline: Optional[Dict[str, Any]]) -> str:
    """dev / qa / qa_blocked / approved / live from Timeline's release state; `unknown` when it was not read."""
    if not timeline:
        return "unknown"
    stage = timeline.get("release_state")
    if stage == "live":
        return "live"
    return {"before qa": "dev", "in qa": "qa", "qa blocked": "qa_blocked", "qa approved": "approved"}.get(stage, "unknown")


def needs_attention(ticket: Dict[str, Any]) -> bool:
    """
    Whether the ticket deserves a look given its section. Anything with a flag or a dropped commit does. Beyond that:
    in development nothing is expected of the code; in QA every commit should be on QA; approved or live tickets
    should be on production; with no Timeline status the code state alone decides.
    """
    state, section = ticket["state"], ticket["section"]
    if ticket["flags"] or state == "dropped":
        return True
    if section == "dev":
        return False
    if section in ("qa", "qa_blocked"):
        return state in _ATTENTION_STATES
    if section in ("approved", "live"):
        return state != "shipped"
    return state in _ATTENTION_STATES


def classify(ticket: Dict[str, Any]) -> Dict[str, Any]:
    """Sets `section` and `attention` on the ticket from its Timeline status, flags and code state."""
    ticket["section"] = section_for(ticket.get("timeline"))
    ticket["attention"] = needs_attention(ticket)
    return ticket


# ---- closed tickets --------------------------------------------------------
def closed_issues(base_dir: Optional[str] = None) -> Set[str]:
    """Tickets already found closed in Timeline (no file is created when nothing was stored)."""
    conn = pc._open_planner_db(base_dir, create=False)
    if conn is None:
        return set()
    try:
        return {r[0] for r in conn.execute("SELECT issue_number FROM coverage_closed")}
    finally:
        conn.close()


def mark_closed(base_dir: Optional[str], issues: Iterable[str], now: Optional[dt.datetime] = None) -> None:
    """Forgets closed tickets for good: remembers them so they are skipped, and deletes their stored commits."""
    issues = [str(i) for i in issues]
    if not issues:
        return
    stamp = _now_iso(now)
    conn = pc._open_planner_db(base_dir, create=True)
    try:
        with conn:
            for issue in issues:
                conn.execute("INSERT OR IGNORE INTO coverage_closed (issue_number, closed_seen) VALUES (?, ?)", (issue, stamp))
                conn.execute("DELETE FROM coverage_commits WHERE issue_number = ?", (issue,))
    finally:
        conn.close()


# ---- old shipped tickets ---------------------------------------------------
def retired_issues(base_dir: Optional[str] = None) -> Dict[str, str]:
    """{ticket: newest commit date when it was hidden} (no file is created when nothing was stored)."""
    conn = pc._open_planner_db(base_dir, create=False)
    if conn is None:
        return {}
    try:
        return {r[0]: r[1] for r in conn.execute("SELECT issue_number, last_commit_at FROM coverage_retired")}
    finally:
        conn.close()


def mark_retired(base_dir: Optional[str], issues: Dict[str, str], now: Optional[dt.datetime] = None) -> None:
    """Hides old shipped tickets: remembers each with its newest commit date and deletes its stored commits."""
    if not issues:
        return
    stamp = _now_iso(now)
    conn = pc._open_planner_db(base_dir, create=True)
    try:
        with conn:
            for issue, last in issues.items():
                conn.execute("INSERT OR REPLACE INTO coverage_retired (issue_number, last_commit_at, retired_at) VALUES (?, ?, ?)",
                             (str(issue), last, stamp))
                conn.execute("DELETE FROM coverage_commits WHERE issue_number = ?", (str(issue),))
    finally:
        conn.close()


def unretire(base_dir: Optional[str], issues: Iterable[str]) -> None:
    """Shows tickets again (a newer commit of yours was found)."""
    issues = [str(i) for i in issues]
    if not issues:
        return
    conn = pc._open_planner_db(base_dir, create=False)
    if conn is None:
        return
    try:
        with conn:
            conn.executemany("DELETE FROM coverage_retired WHERE issue_number = ?", [(i,) for i in issues])
    finally:
        conn.close()


def retire_candidates(tickets: List[Dict[str, Any]], now: Optional[dt.datetime] = None,
                      days: int = STATUS_DAYS) -> Dict[str, str]:
    """
    Tickets that are fully on production in every repo they have commits in, with nothing newer than `days` old, and
    whose repos were all read: {ticket: newest commit date}. These are the ones that are never looked up in Timeline.
    """
    cutoff = (now or dt.datetime.now(dt.timezone.utc)).replace(tzinfo=dt.timezone.utc) - dt.timedelta(days=days)
    out = {}
    for t in tickets:
        if t["state"] != "shipped" or t.get("unread"):
            continue
        dates = [(_parse_date(r["last_commit_at"]), r["last_commit_at"]) for r in t["repos"].values()]
        dates = [d for d in dates if d[0]]
        if dates and max(dates)[0] < cutoff:
            out[t["issue"]] = max(dates)[1]
    return out


def hidden_counts(base_dir: Optional[str] = None) -> Dict[str, int]:
    """How many tickets are left out for good (closed) or hidden until something newer is committed (old shipped)."""
    return {"closed": len(closed_issues(base_dir)), "retired": len(retired_issues(base_dir))}


# ---- Timeline cross-check --------------------------------------------------
_QA_STAGES = ("in qa", "qa blocked", "qa approved")


def _plural(n: int, word: str = "commit") -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def timeline_flags(ticket: Dict[str, Any], status: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Where Timeline and the code disagree, as [{key, level, text}] (level `missing` = red, `warn` = amber). Only
    reports; nothing is changed. `status` is a get_task_statuses row. Commits are counted over all repos.
    """
    if not status or status.get("error"):
        return []
    stage, code_state = status.get("release_state"), ticket["state"]
    prod_gap = sum(len(r["missing_prod"]) for r in ticket["repos"].values())
    qa_gap = sum(len(r["missing_qa"]) for r in ticket["repos"].values())
    flags = []
    if stage == "live":
        if prod_gap:
            flags.append({"key": "live_not_on_prod", "level": "missing",
                          "text": f"Timeline says live, but {_plural(prod_gap)} not on production."})
    else:
        if stage == "before qa" and code_state in ("on_qa", "shipped"):
            flags.append({"key": "stage_behind", "level": "warn",
                          "text": "Every commit is already on " + ("production" if code_state == "shipped" else "QA")
                          + ", but Timeline is still before QA."})
        if stage == "qa approved" and prod_gap:
            flags.append({"key": "approved_not_on_prod", "level": "warn",
                          "text": f"Timeline says QA approved, but {_plural(prod_gap)} not on production yet."})
        if stage in _QA_STAGES and qa_gap:
            flags.append({"key": "qa_not_on_qa", "level": "warn",
                          "text": f"Timeline says {stage}, but {_plural(qa_gap)} not on QA."})
        if code_state == "shipped" and stage != "before qa":  # "before qa" is already said by stage_behind
            flags.append({"key": "shipped_not_live", "level": "warn",
                          "text": f"All commits are on production, but Timeline is not Live (it says {stage or 'unknown'})."})
    return flags


def lookup_ids(tickets: List[Dict[str, Any]], now: Optional[dt.datetime] = None, days: int = STATUS_DAYS) -> List[str]:
    """Tickets worth a Timeline call: everything not shipped, and shipped ones with a recent commit."""
    cutoff = (now or dt.datetime.now(dt.timezone.utc)).replace(tzinfo=dt.timezone.utc) - dt.timedelta(days=days)
    ids = []
    for t in tickets:
        recent = False
        if t["state"] == "shipped":
            for r in t["repos"].values():
                try:
                    stamp = dt.datetime.fromisoformat(r["last_commit_at"])
                except (TypeError, ValueError):
                    continue
                recent = recent or (stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)) >= cutoff
        if t["state"] != "shipped" or recent:
            ids.append(t["issue"])
    return ids


def attach_timeline(report: Dict[str, Any], status_lookup: Optional[Any], now: Optional[dt.datetime] = None,
                    days: int = STATUS_DAYS) -> Dict[str, Any]:
    """
    Adds `title`, `timeline` ({release_state, task_status, stage_status, project_id}), `flags`, `section` and `attention` to each ticket from one
    `status_lookup(ids)` call. Tickets Timeline reports closed are taken out of the report and listed in `closed`.
    If Timeline cannot be reached the git report stays as it is, with `timeline_error` set.
    """
    report["timeline_error"] = None
    report.setdefault("closed", [])
    ids = lookup_ids(report["tickets"], now, days) if status_lookup else []
    if not ids:
        return report
    try:
        rows = {str(r["issue_id"]): r for r in status_lookup(ids)}
    except Exception as exc:  # auth or network: the git side is still worth showing
        report["timeline_error"] = str(exc) or exc.__class__.__name__
        return report
    closed = {i for i, r in rows.items() if not r.get("error") and r.get("task_status") == "closed"}
    report["tickets"] = [t for t in report["tickets"] if t["issue"] not in closed]
    report["closed"] = sorted(closed, key=int)
    for t in report["tickets"]:
        row = rows.get(t["issue"])
        if not row or row.get("error"):
            continue
        t["title"] = row.get("title") or ""
        t["timeline"] = {k: row.get(k) for k in ("release_state", "task_status", "stage_status", "project_id")}
        t["flags"] = timeline_flags(t, row)
    for t in report["tickets"]:
        classify(t)
    return report


def combine(per_repo: Dict[str, Dict[str, Dict[str, Any]]], errors: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Joins the per-repo results into ticket rows, worst first: {tickets: [{issue, state, repos: {name: row}}],
    errors}. A repo where the ticket has no commits is simply absent, never counted as missing. A repo that could
    not be read is listed in `errors`, and a ticket shown without it says so in `unread`.
    """
    errors = errors or {}
    issues = sorted({i for rows in per_repo.values() for i in rows}, key=int)
    tickets = []
    for issue in issues:
        repos = {name: rows[issue] for name, rows in per_repo.items() if issue in rows}
        state = min((r["state"] for r in repos.values()), key=STATE_ORDER.index)
        tickets.append({"issue": issue, "state": state, "repos": repos, "unread": sorted(errors),
                        "title": "", "timeline": None, "flags": [], "section": "unknown", "attention": False})
        classify(tickets[-1])
    tickets.sort(key=lambda t: (STATE_ORDER.index(t["state"]), -int(t["issue"])))
    return {"tickets": tickets, "errors": errors}


def default_authors(repo: _Repo) -> List[str]:
    """The checkout's `git config user.email`, or nothing when it is not set."""
    try:
        email = repo.run("git", "config", "user.email").strip()
    except ReleaseCheckError:
        return []
    return [email] if email else []


def build_coverage(
    dirs: Optional[Dict[str, Optional[str]]] = None,
    authors: Optional[Sequence[str]] = None,
    since: str = DEFAULT_SINCE,
    base_dir: Optional[str] = None,
    fetch: bool = True,
    runner: Runner = _subprocess_runner,
    now: Optional[dt.datetime] = None,
    progress: Optional[Any] = None,
    status_lookup: Optional[Any] = None,
    subscribed: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    The whole report: your tickets from both repos (`dirs` maps repo name to checkout, default the usual places), each
    with its commits, QA/production state and history. `authors` are the git emails that count as you (default: each
    checkout's `git config user.email`). A repo that cannot be opened, fetched or read is listed in `errors` and
    recorded, the other repo is still reported. Tickets are the union over both repos, so a ticket with commits only in
    one repo is looked up in the other too (and found there if someone else's commit names it).
    `status_lookup(ids)` (get_task_statuses rows) adds each ticket's title and where Timeline and the code disagree.
    Closed tickets and old fully shipped ones are taken out (`closed`, `retired`; `hidden` has the running totals).
    `subscribed()` returns the issue numbers you are subscribed to in Timeline; only those tickets are read at all.
    If it raises, so does the build (an unknown subscription list must not turn into "everything"), and the caller
    keeps its previous report. Without it every ticket found from your commits is included.
    """
    step = progress or (lambda _text: None)
    dirs = dirs or {}
    repos: Dict[str, _Repo] = {}
    errors: Dict[str, str] = {}
    dates: Dict[str, str] = {}
    used: Set[str] = set()
    for name in REPO_SPECS:
        try:
            repo = _open(dirs.get(name), name, runner)
            if fetch:
                step(f"Fetching {name} from GitHub")
                fetch_branches(repo)
            step(f"Finding your tickets in {name}")
            who = list(authors or []) or default_authors(repo)
            used.update(who)
            for issue, date in discover_issue_dates(repo, who, since).items():
                old = _parse_date(dates.get(issue))
                new = _parse_date(date)
                if issue not in dates or (new and (not old or new > old)):
                    dates[issue] = date
            repos[name] = repo
        except ReleaseCheckError as exc:
            errors[name] = str(exc)
            record_failure(base_dir, name, str(exc), now)
    mine = {str(i) for i in subscribed()} if subscribed else None
    issues = set(dates) - closed_issues(base_dir)  # closed in Timeline earlier: nothing more is read for them
    if mine is not None:
        issues &= mine  # not subscribed: not read at all
    retired = retired_issues(base_dir)
    floor = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    back = {i for i in issues if i in retired and (_parse_date(dates[i]) or floor) > (_parse_date(retired[i]) or floor)}
    unretire(base_dir, back)  # you committed something newer: it is checked again
    issues -= {i for i in retired if i not in back}
    per_repo: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for name, repo in repos.items():
        step(f"Checking {len(issues)} tickets in {name}")
        try:
            per_repo[name] = build_repo_coverage(repo, name, issues, base_dir, now, mine)
        except ReleaseCheckError as exc:
            errors[name] = str(exc)
    report = combine(per_repo, errors)
    if status_lookup:
        step("Checking Timeline status")
    attach_timeline(report, status_lookup, now)
    mark_closed(base_dir, report["closed"], now)
    # Hidden at the later of the ticket's own newest commit and your newest discovered one, so a commit that
    # discovery sees but the ticket check does not list cannot bring the ticket back on every check.
    old_shipped = {i: _later(last, dates.get(i)) for i, last in retire_candidates(report["tickets"], now).items()}
    report["tickets"] = [t for t in report["tickets"] if t["issue"] not in old_shipped]
    report["retired"] = sorted(old_shipped, key=int)
    mark_retired(base_dir, old_shipped, now)
    report.update(generated_at=_now_iso(now), authors=sorted(used), checks=last_checks(base_dir),
                  hidden=hidden_counts(base_dir), subscribed_only=mine is not None)
    return report
