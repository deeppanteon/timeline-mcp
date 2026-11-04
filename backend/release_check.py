"""
Release overview for Panteon-norway/duell-admin, for the dashboard's Release
view: open PRs into qa-release and master, tickets on QA that aren't in
production yet, their QA approval in Timeline, and draft release notes.

The same rules as skills/release-pr-check/SKILL.md, minus the parts that need
judgement (rewriting notes, translation and migration checks).

Read-only: it runs `git fetch`, `git log`, `git rev-list` and `gh pr/release
list` with fixed argument lists (no shell), and never merges, pushes, comments
or creates releases.
"""
import datetime as dt
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

REPO = "Panteon-norway/duell-admin"
BASES = ("qa-release", "master")
STALE_DAYS = 14
# Branches whose merges into qa-release are syncs, not tickets.
SYNC_BRANCHES = {"master", "development", "qa-release"}
PR_FIELDS = "number,title,author,headRefName,headRefOid,body,isDraft,createdAt,reviewDecision,mergeable,statusCheckRollup,url"

Runner = Callable[[Sequence[str], str, int], Tuple[int, str, str]]


class ReleaseCheckError(RuntimeError):
    """git or gh failed, or the checkout isn't duell-admin."""


def default_repo_dir() -> str:
    """DUELL_ADMIN_DIR, else a duell-admin checkout next to this project."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # the project root
    return os.environ.get("DUELL_ADMIN_DIR") or os.path.join(os.path.dirname(here), "duell-admin")


def _subprocess_runner(args: Sequence[str], cwd: str, timeout: int) -> Tuple[int, str, str]:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GH_PROMPT_DISABLED="1", NO_COLOR="1")
    try:
        done = subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError:
        raise ReleaseCheckError(f"`{args[0]}` is not installed.")
    except subprocess.TimeoutExpired:
        raise ReleaseCheckError(f"`{' '.join(args[:3])}` timed out after {timeout}s.")
    return done.returncode, done.stdout, done.stderr


class _Repo:
    def __init__(self, path: str, runner: Runner):
        self.path = path
        self.runner = runner

    def run(self, *args: str, timeout: int = 30) -> str:
        code, out, err = self.runner(args, self.path, timeout)
        if code != 0:
            detail = (err or out).strip().splitlines()
            raise ReleaseCheckError(f"`{' '.join(args[:3])}` failed: {detail[-1] if detail else f'exit {code}'}")
        return out

    def gh_json(self, *args: str) -> Any:
        import json

        return json.loads(self.run("gh", *args, "--repo", REPO) or "null")


# ---- parsing ---------------------------------------------------------------
_MERGE_PATTERNS = [
    re.compile(r"^Merge pull request #\d+ from [^/\s]+/(?P<branch>\S+)"),
    re.compile(r"^Merge (?:remote-tracking )?branch '(?:origin/)?(?P<branch>[^']+)'"),
    re.compile(r"^Merge (?P<branch>\d{3,})\b"),  # "Merge 4054 In qa-release"
]
_BRANCH_ID = re.compile(r"(?:^|[-/_])(\d{3,6})(?=[-_]|$)")
_TITLE_ID = re.compile(r"#\s*(\d{3,6})\b|\b(\d{3,6})\b")


def merged_branch(subject: str) -> Optional[str]:
    """The branch a qa-release merge commit brought in, or None for syncs and pulls."""
    if " of https://" in subject:  # "Merge branch 'qa-release' of https://…" (a git pull)
        return None
    for pattern in _MERGE_PATTERNS:
        m = pattern.match(subject)
        if m:
            branch = m.group("branch")
            return None if branch in SYNC_BRANCHES else branch
    return None


def task_id_from_branch(branch: str) -> Optional[str]:
    m = _BRANCH_ID.search(branch or "")
    return m.group(1) if m else None


def task_id_from_title(title: str) -> Optional[str]:
    m = _TITLE_ID.search(title or "")
    return (m.group(1) or m.group(2)) if m else None


def pr_task_id(pr: Dict[str, Any]) -> Optional[str]:
    return task_id_from_title(pr.get("title", "")) or task_id_from_branch(pr.get("headRefName", ""))


_ENTRY = re.compile(r"^(?P<indent>\s*)-\s*#?\s*(?P<id>\d{3,6})\s*-\s*(?P<title>.+?)\s*$")
_BULLET = re.compile(r"^(?P<indent>\s*)[-*]\s+(?P<text>\S.*)$")


def release_entry(body: str, issue_id: str) -> Optional[Dict[str, Any]]:
    """
    Finds `- #<id> - <title>` in a PR body and the indented bullets under it.
    Returns {title, bullets: [(level, text)], indent_ok}, or None if absent.
    """
    lines = (body or "").replace("\r\n", "\n").split("\n")
    for i, line in enumerate(lines):
        m = _ENTRY.match(line)
        if not m or m.group("id") != str(issue_id):
            continue
        base = len(m.group("indent"))
        raw: List[Tuple[int, str]] = []
        for follow in lines[i + 1:]:
            if not follow.strip():
                continue
            b = _BULLET.match(follow)
            if not b or len(b.group("indent")) <= base:
                break
            raw.append((len(b.group("indent")), b.group("text").strip()))
        depths = sorted({d for d, _ in raw})
        bullets = [(depths.index(d) + 1, text) for d, text in raw]
        indent_ok = base == 0 and all(d == 2 * (depths.index(d) + 1) for d in depths)
        return {"title": m.group("title"), "bullets": bullets, "indent_ok": indent_ok}
    return None


def note_problems(pr: Dict[str, Any], base: str, issue_id: Optional[str], timeline_title: Optional[str]) -> List[str]:
    """Release-note problems in one PR (title, entry line, bullets, format)."""
    problems = []
    title = pr.get("title", "")
    if base == "master" and not re.match(r"^\s*PROD\b", title, re.I):
        problems.append("Title should start with PROD:")
    if not issue_id:
        return problems  # a hotfix: no entry line expected
    body = pr.get("body") or ""
    if re.search(r"^\s{0,3}#{1,6}\s", body, re.M) or re.search(r"^\s*[-*]\s*\[[ xX]\]", body, re.M):
        problems.append("Description has headings or checklists")
    entry = release_entry(body, issue_id)
    if not entry:
        problems.append(f"No `- #{issue_id} - <title>` entry in the description")
        return problems
    if not any(len(text.split()) >= 3 for _, text in entry["bullets"]):
        problems.append("No change bullets under the entry")
    if not entry["indent_ok"]:
        problems.append("Indentation isn't 2 spaces per level")
    if timeline_title and _norm(entry["title"]) != _norm(timeline_title):
        problems.append(f"Entry title differs from Timeline: {timeline_title}")
    return problems


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def summarize_checks(rollup: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """CI state of a PR. A failing snyk check is a warning only (often its quota)."""
    failed, pending, warnings = [], [], []
    for check in rollup or []:
        name = check.get("name") or check.get("context") or "check"
        if check.get("__typename") == "StatusContext" or "state" in check:
            state = (check.get("state") or "").upper()
            bad, waiting = state in ("FAILURE", "ERROR"), state in ("PENDING", "EXPECTED")
        else:
            conclusion = (check.get("conclusion") or "").upper()
            waiting = (check.get("status") or "").upper() != "COMPLETED"
            bad = not waiting and conclusion in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE")
        if bad and "snyk" in name.lower():
            warnings.append(f"{name} failed (may be the snyk private-test quota)")
        elif bad:
            failed.append(name)
        elif waiting:
            pending.append(name)
    return {"total": len(rollup or []), "failed": failed, "pending": pending, "warnings": warnings}


def next_versions(releases: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Latest releases and the suggested next production / QA versions."""
    tags = {r.get("tagName") for r in releases}
    prod = [r for r in releases if not r.get("isDraft") and not str(r.get("tagName", "")).endswith("-qa")]
    qa = [r for r in releases if not r.get("isDraft") and str(r.get("tagName", "")).endswith("-qa")]
    draft = next((r for r in releases if r.get("isDraft") and not str(r.get("tagName", "")).endswith("-qa")), None)

    def date(r):
        return (r.get("publishedAt") or r.get("createdAt") or "")[:10]

    latest_prod = max(prod, key=lambda r: _version_key(r["tagName"]), default=None)
    latest_qa = max(qa, key=lambda r: _version_key(r["tagName"]), default=None)
    if draft:
        next_prod = draft["tagName"]
    elif latest_prod:
        next_prod = bump_version(latest_prod["tagName"])
    else:
        next_prod = None
    next_qa = next_prod
    while next_qa and f"{next_qa}-qa" in tags:
        next_qa = bump_version(next_qa)
    return {
        "latest_prod": {"tag": latest_prod["tagName"], "date": date(latest_prod)} if latest_prod else None,
        "latest_qa": {"tag": latest_qa["tagName"], "date": date(latest_qa)} if latest_qa else None,
        "draft": draft["tagName"] if draft else None,
        "next_prod": next_prod,
        "next_qa": f"{next_qa}-qa" if next_qa else None,
    }


def _version_key(tag: str) -> Tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", tag.replace("-qa", "")))


def bump_version(tag: str) -> str:
    """v1.0.2.9 → v1.0.3.0 → v1.0.3.1 (each segment after the first rolls over at 10)."""
    parts = list(_version_key(tag))
    if not parts:
        return tag
    i = len(parts) - 1
    parts[i] += 1
    while i > 0 and parts[i] >= 10:
        parts[i] = 0
        i -= 1
        parts[i] += 1
    return "v" + ".".join(str(p) for p in parts)


# ---- assembling the report -------------------------------------------------
def qa_merges(repo: _Repo) -> List[Dict[str, Any]]:
    """Merges into qa-release since master, newest first: {sha, merged, date, subject, branch}."""
    out = repo.run("git", "log", "--first-parent", "--merges", "--format=%H%x1f%P%x1f%cI%x1f%s",
                   "origin/master..origin/qa-release")
    merges = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        sha, parents, date, subject = parts
        parent_list = parents.split()
        merges.append({
            "sha": sha,
            "merged": parent_list[1] if len(parent_list) > 1 else None,
            "date": date,
            "subject": subject,
            "branch": merged_branch(subject),
        })
    return merges


def tickets_on_qa(merges: List[Dict[str, Any]], in_master: Set[str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Tickets merged into qa-release whose merged commit isn't in master yet,
    oldest first, and branches with no ticket ID (hotfix candidates). Branches
    usually reach master through their own PR, so the qa-release merge range
    also lists shipped work; checking each merged commit drops it.
    """
    tickets: Dict[str, Dict[str, Any]] = {}
    no_ticket: Dict[str, Dict[str, Any]] = {}
    for merge in reversed(merges):  # oldest first
        branch = merge["branch"]
        if not branch or not merge["merged"] or merge["merged"] in in_master:
            continue
        issue_id = task_id_from_branch(branch)
        target = tickets if issue_id else no_ticket
        key = issue_id or branch
        entry = target.setdefault(key, {"issue_id": issue_id, "branches": [], "first_merged": merge["date"]})
        entry["last_merged"] = merge["date"]
        if not branch.isdigit() and branch not in entry["branches"]:
            entry["branches"].append(branch)
    return list(tickets.values()), list(no_ticket.values())


def shape_pr(pr: Dict[str, Any], base: str, today: dt.date) -> Dict[str, Any]:
    created = (pr.get("createdAt") or "")[:10]
    try:
        age = (today - dt.date.fromisoformat(created)).days
    except ValueError:
        age = None
    return {
        "number": pr.get("number"),
        "url": pr.get("url"),
        "title": pr.get("title", ""),
        "author": (pr.get("author") or {}).get("login", ""),
        "branch": pr.get("headRefName", ""),
        "body": (pr.get("body") or "").replace("\r\n", "\n").strip(),
        "issue_id": pr_task_id(pr),
        "base": base,
        "draft": bool(pr.get("isDraft")),
        "age_days": age,
        "stale": age is not None and age > STALE_DAYS,
        "review": pr.get("reviewDecision") or "",
        "mergeable": pr.get("mergeable") or "UNKNOWN",
        "checks": summarize_checks(pr.get("statusCheckRollup")),
    }


def via_qa(repo: "_Repo", oid: Optional[str], on_qa: Set[str], in_master: Set[str], was_on_qa: bool) -> Tuple[str, Optional[int]]:
    """
    Whether a master PR's code went through qa-release: "yes", "partly" (an
    earlier version did; the count is commits since, ignoring merges of master
    into the branch) or "no".
    """
    if oid and oid in on_qa and oid not in in_master:
        return "yes", 0
    count = None
    if oid:
        code, out, _ = repo.runner(("git", "rev-list", "--no-merges", "--count", oid, "^origin/qa-release", "^origin/master"),
                                   repo.path, 30)
        if code == 0 and out.strip().isdigit():
            count = int(out.strip())
    if count == 0:
        return "yes", 0
    return ("partly" if was_on_qa else "no"), count


def verdict(pr: Dict[str, Any]) -> Tuple[str, str]:
    blockers = []
    if pr["draft"]:
        blockers.append("draft")
    if pr["mergeable"] == "CONFLICTING":
        blockers.append("merge conflicts")
    if pr["checks"]["failed"]:
        blockers.append("failing: " + ", ".join(pr["checks"]["failed"]))
    if pr["review"] == "CHANGES_REQUESTED":
        blockers.append("changes requested")
    if blockers:
        return "blocked", "; ".join(blockers)
    if pr["problems"]:
        return "needs notes", pr["problems"][0]
    return "ready", "checks pass" if not pr["checks"]["pending"] else "checks still running"


def build_report(
    repo_dir: Optional[str] = None,
    status_lookup: Optional[Callable[[List[str]], List[Dict[str, Any]]]] = None,
    fetch: bool = True,
    runner: Runner = _subprocess_runner,
    today: Optional[dt.date] = None,
) -> Dict[str, Any]:
    """
    Builds the whole release overview. `status_lookup(ids)` returns
    get_task_statuses rows; if it raises (auth, network) the git and GitHub
    parts are still returned, with `timeline_error` set.
    """
    path = repo_dir or default_repo_dir()
    if not os.path.isdir(os.path.join(path, ".git")) and not os.path.isfile(os.path.join(path, ".git")):
        raise ReleaseCheckError(f"No duell-admin checkout at {path}. Set DUELL_ADMIN_DIR.")
    repo = _Repo(path, runner)
    origin = repo.run("git", "remote", "get-url", "origin").strip()
    if not re.search(r"[:/]Panteon-norway/duell-admin(\.git)?/?$", origin):
        raise ReleaseCheckError(f"{path} is not a duell-admin checkout (origin is {origin}).")
    today = today or dt.date.today()

    if fetch:
        repo.run("git", "fetch", "--quiet", "origin", "qa-release", "master", timeout=90)
    merges = qa_merges(repo)
    in_master = set(repo.run("git", "rev-list", "origin/master").split())
    on_qa = set(repo.run("git", "rev-list", "origin/master..origin/qa-release").split()) | in_master

    with ThreadPoolExecutor(max_workers=4) as pool:
        open_qa = pool.submit(repo.gh_json, "pr", "list", "--base", "qa-release", "--state", "open", "--limit", "100", "--json", PR_FIELDS)
        open_master = pool.submit(repo.gh_json, "pr", "list", "--base", "master", "--state", "open", "--limit", "100", "--json", PR_FIELDS)
        merged_qa = pool.submit(repo.gh_json, "pr", "list", "--base", "qa-release", "--state", "merged", "--limit", "200", "--json", "number,headRefName,body,title")
        releases = pool.submit(repo.gh_json, "release", "list", "--limit", "20", "--json", "tagName,name,isDraft,isPrerelease,publishedAt,createdAt")
        raw_prs = {"qa-release": open_qa.result() or [], "master": open_master.result() or []}
        merged_qa_prs = merged_qa.result() or []
        release_list = releases.result() or []

    tickets, no_ticket = tickets_on_qa(merges, in_master)
    prs = {base: [shape_pr(p, base, today) for p in raw_prs[base]] for base in BASES}

    # Every ticket we know about, for one status lookup.
    ids: List[str] = []
    for t in tickets:
        ids.append(t["issue_id"])
    for base in BASES:
        ids += [p["issue_id"] for p in prs[base] if p["issue_id"]]
    ids = list(dict.fromkeys(ids))
    statuses: Dict[str, Dict[str, Any]] = {}
    timeline_error = None
    if ids and status_lookup:
        try:
            statuses = {row["issue_id"]: row for row in status_lookup(ids)}
        except Exception as exc:  # auth or network: still show the git side
            timeline_error = str(exc) or exc.__class__.__name__

    def status_of(issue_id):
        row = statuses.get(issue_id)
        return row if row and not row.get("error") else None

    def title_of(issue_id):
        row = status_of(issue_id)
        return row["title"] if row else None

    # PR checks.
    qa_branches = {t["issue_id"] for t in tickets}
    merged_qa_ids = {task_id_from_branch(p.get("headRefName", "")) or task_id_from_title(p.get("title", "")) for p in merged_qa_prs}
    merged_qa_branches = {p.get("headRefName") for p in merged_qa_prs}
    for base in BASES:
        for raw, pr in zip(raw_prs[base], prs[base]):
            pr["problems"] = note_problems(raw, base, pr["issue_id"], title_of(pr["issue_id"]))
            pr["status"] = statuses.get(pr["issue_id"]) if pr["issue_id"] else None
            if base == "master":
                pr["via_qa"], pr["commits_not_on_qa"] = via_qa(repo, raw.get("headRefOid"), on_qa, in_master,
                                                               pr["branch"] in merged_qa_branches or (pr["issue_id"] and (
                                                                   pr["issue_id"] in qa_branches or pr["issue_id"] in merged_qa_ids)))
            pr["verdict"], pr["reason"] = verdict(pr)

    # Where to take each ticket's release-note entry from: open master PR,
    # then the newest merged qa-release PR, then an open qa-release PR.
    bodies: Dict[str, str] = {}
    for p in reversed(merged_qa_prs):  # gh lists newest first; let newer win
        tid = task_id_from_branch(p.get("headRefName", "")) or task_id_from_title(p.get("title", ""))
        if tid and release_entry(p.get("body") or "", tid):
            bodies[tid] = p.get("body") or ""
    for base in ("qa-release", "master"):
        for raw, pr in zip(raw_prs[base], prs[base]):
            if pr["issue_id"] and release_entry(raw.get("body") or "", pr["issue_id"]):
                bodies[pr["issue_id"]] = raw.get("body") or ""

    drafted: List[str] = []

    def entry_lines(issue_id: str, fallback_title: str = "") -> List[str]:
        found = release_entry(bodies.get(issue_id, ""), issue_id)
        title = title_of(issue_id) or (found and found["title"]) or fallback_title or "(title unknown)"
        lines = [f"- #{issue_id} - {title}"]
        if found and found["bullets"]:
            lines += ["  " * level + f"- {text}" for level, text in found["bullets"]]
        else:
            drafted.append(issue_id)
        return lines

    master_prs = {p["issue_id"]: p for p in prs["master"] if p["issue_id"]}
    qa_open = [p for p in prs["qa-release"] if p["issue_id"] and p["issue_id"] not in qa_branches]

    # Ticket rows for the view.
    ticket_rows = []
    for t in tickets:
        row = status_of(t["issue_id"])
        ticket_rows.append({
            **t,
            "title": row["title"] if row else None,
            "status": statuses.get(t["issue_id"]),
            "approved": bool(row and row["qa_approved"]),
            "master_pr": master_prs[t["issue_id"]]["number"] if t["issue_id"] in master_prs else None,
        })
    approved = [r for r in ticket_rows if r["approved"]]
    not_approved = [r for r in ticket_rows if not r["approved"]]

    mismatches = []
    for r in ticket_rows:
        st = r["status"] or {}
        if st.get("release_state") == "before qa":
            mismatches.append({"issue_id": r["issue_id"], "title": r["title"], "detail": "On qa-release, but Timeline hasn't handed it to QA"})
        if st.get("release_state") == "live":
            mismatches.append({"issue_id": r["issue_id"], "title": r["title"], "detail": "Live in Timeline, but not in master"})
        elif r["approved"] and not r["master_pr"]:
            mismatches.append({"issue_id": r["issue_id"], "title": r["title"], "detail": "QA approved, but no open PR into master"})

    # Release notes.
    versions = next_versions(release_list)
    date = today.strftime("%d.%m.%Y")
    master_body: List[str] = []
    held_back: List[str] = []
    in_note: Set[str] = set()
    for r in approved:
        master_body += entry_lines(r["issue_id"])
        in_note.add(r["issue_id"])
    direct = [p for p in prs["master"] if p["issue_id"] and p.get("via_qa") == "no" and p["issue_id"] not in in_note]
    for p in direct:
        master_body += entry_lines(p["issue_id"], p["title"])
        in_note.add(p["issue_id"])
    master_hotfixes = [p["title"] for p in prs["master"] if not p["issue_id"]]
    if master_hotfixes:
        master_body += ["- Hotfixes"] + [f"  - {t}" for t in master_hotfixes]
    for r in not_approved:
        held_back.append(f"  - #{r['issue_id']} - {r['title'] or '(title unknown)'} — {(r['status'] or {}).get('stage_status') or 'status unknown'}")
    if held_back:
        master_body += ["- Not approved (held back)"] + held_back

    qa_body: List[str] = []
    for r in ticket_rows:
        qa_body += entry_lines(r["issue_id"])
    qa_hotfixes = [b for t in no_ticket for b in t["branches"]] + [p["title"] for p in prs["qa-release"] if not p["issue_id"]]
    if qa_hotfixes:
        qa_body += ["- Hotfixes"] + [f"  - {b}" for b in qa_hotfixes]
    if qa_open:
        qa_body += ["- Not approved (held back)"] + [
            f"  - #{p['issue_id']} - {title_of(p['issue_id']) or p['title']} — not on QA yet" for p in qa_open
        ]

    total = len(ticket_rows) + len(qa_open)
    return {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "fetched": fetch,
        "repo": REPO,
        "repo_dir": path,
        "releases": versions,
        "prs": prs,
        "tickets": ticket_rows,
        "no_ticket": no_ticket,
        "pending_qa": [{"issue_id": p["issue_id"], "number": p["number"], "title": title_of(p["issue_id"]) or p["title"]} for p in qa_open],
        "approval": {"approved": len(approved), "total": total},
        "mismatches": mismatches,
        "timeline_error": timeline_error,
        "notes": {
            "master": {
                "name": f"Release note {date} {versions['next_prod']}" if versions["next_prod"] else None,
                "tag": versions["next_prod"], "target": "master",
                "body": "\n".join(master_body),
                "included": len(in_note), "held_back": len(held_back),
            },
            "qa": {
                "name": f"QA Release note {date} {versions['next_qa']}" if versions["next_qa"] else None,
                "tag": versions["next_qa"], "target": "qa-release",
                "body": "\n".join(qa_body),
                "on_qa": len(ticket_rows), "pending": len(qa_open),
            },
            "drafted": sorted(set(drafted)),
        },
    }
