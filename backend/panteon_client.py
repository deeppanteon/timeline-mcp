import base64
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
import requests
from typing import Any, Callable, Dict, List, Optional
from dotenv import load_dotenv

# Load the .env sitting next to this module, so credentials resolve regardless of
# which project the MCP server is launched from (it runs globally at user scope,
# with the cwd set to whatever project you're in). Variables already present in
# the environment — e.g. those set in the MCP registration — are NOT overridden.
# Falls back to the default cwd-based search when there is no local .env.
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_PATH if _ENV_PATH.exists() else None)

BASE_URL = "https://timeline.panteon.no"

# Panteon tenant/company id used in API URLs. This server is single-tenant (one
# Panteon company per deployment), so PANTEON_COMPANY_ID is normally never set
# and this default applies. See .env.example.
DEFAULT_COMPANY_ID = "16"

# Reused across calls for connection pooling.
_SESSION = requests.Session()

# Spaces out the start of Timeline API calls (across all threads), so a burst such
# as one planner check turns into a steady trickle instead of a spike.
# PANTEON_MIN_REQUEST_GAP_MS overrides the gap; 0 turns it off.
DEFAULT_REQUEST_GAP_MS = 200
_pace_lock = threading.Lock()
_next_slot = 0.0


def _pace() -> None:
    global _next_slot
    raw = os.environ.get("PANTEON_MIN_REQUEST_GAP_MS")
    gap = (float(raw) if raw not in (None, "") else DEFAULT_REQUEST_GAP_MS) / 1000
    if gap <= 0:
        return
    with _pace_lock:
        now = time.monotonic()
        wait = max(0.0, _next_slot - now)
        _next_slot = max(now, _next_slot) + gap
    if wait:
        time.sleep(wait)


def request_log_dir() -> Path:
    """Folder for the daily request logs: PANTEON_REQUEST_LOG_DIR, else `logs/` next to
    this module, so the MCP server, CLI and dashboard all write to the same place."""
    return Path(os.environ.get("PANTEON_REQUEST_LOG_DIR") or Path(__file__).resolve().parent.parent / "logs")


def log_request(method: str, url: str, status: Optional[int], started: float, error: str = "") -> None:
    """
    Appends one JSON line (time, process, method, path, status, duration in ms) to
    `<log dir>/timeline-requests-YYYY-MM-DD.jsonl`, to count Timeline requests per
    day. Only the URL path is logged (no query, headers or credentials). Never raises.
    """
    try:
        now = datetime.now()
        path = url.split("://", 1)[-1].partition("/")[2].partition("?")[0]
        entry = {
            "time": now.isoformat(timespec="milliseconds"),
            "source": Path(sys.argv[0]).name or "python",
            "pid": os.getpid(),
            "method": method,
            "path": "/" + path,
            "status": status,
            "ms": round((time.perf_counter() - started) * 1000),
        }
        if error:
            entry["error"] = error
        log_dir = request_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / f"timeline-requests-{now.date().isoformat()}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def timed_request(method: str, url: str, send: Callable[[], requests.Response]) -> requests.Response:
    """Runs `send()` and logs the request's status and duration (see `log_request`)."""
    started = time.perf_counter()
    try:
        response = send()
    except Exception as exc:
        log_request(method, url, None, started, error=type(exc).__name__)
        raise
    log_request(method, url, response.status_code, started)
    return response

# Markdown image: ![alt](src)  and  HTML image: <img src="...">
# Shared by extract_images (src only) and absolutize_markdown_images (alt + src,
# to rewrite in place) so the two never drift apart.
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(\s*<?([^)\s>]+)[^)]*\)")
_HTML_IMAGE_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)


def _api_get(url: str, headers: Dict[str, str], *, context: str = "") -> requests.Response:
    """
    Performs a GET against the Panteon API with a shared session, a 30s timeout,
    and uniform authentication-error handling. `context` is appended to the
    auth-error message (e.g. " when fetching task 5461").
    """
    _pace()
    response = timed_request("GET", url, lambda: _SESSION.get(url, headers=headers, timeout=30))
    if response.status_code in (401, 403):
        raise PermissionError(
            f"Authentication failed (HTTP {response.status_code}){context}. "
            "Please verify your PANTEON_BEARER_TOKEN or PANTEON_COOKIE."
        )
    response.raise_for_status()
    return response


def extract_issue_id(issue_identifier: str) -> str:
    """
    Extracts the numeric issue ID from a URL or raw ID string.
    Example URL: https://timeline.panteon.no/tasks#/list/15033/edit/5461 -> returns '5461'
    Example ID: '5461' -> returns '5461'
    """
    issue_identifier = issue_identifier.strip()

    # Check if input is already a purely numeric ID
    if issue_identifier.isdigit():
        return issue_identifier

    # Check for URL pattern /edit/<id> — the issue id lives here.
    match = re.search(r"/edit/(\d+)", issue_identifier)
    if match:
        return match.group(1)

    # We intentionally do NOT fall back to "trailing digits" here: a URL such as
    # https://timeline.panteon.no/tasks#/list/15033 would silently yield the
    # board/list id (15033) instead of an issue id, fetching the wrong task.
    raise ValueError(
        f"Could not extract a valid issue ID from input: '{issue_identifier}'. "
        "Provide a task URL containing '/edit/<id>' "
        "(e.g. https://timeline.panteon.no/tasks#/list/15033/edit/5461) "
        "or a bare numeric ID (e.g. 5461)."
    )


def build_api_headers(
    bearer_token: Optional[str] = None,
    cookie: Optional[str] = None,
    xsrf_token: Optional[str] = None,
) -> Dict[str, str]:
    """
    Constructs required headers for Panteon API requests.
    Prioritizes passed parameters over environment variables.
    """
    token = bearer_token or os.getenv("PANTEON_BEARER_TOKEN")
    cookie_str = cookie or os.getenv("PANTEON_COOKIE")
    xsrf = xsrf_token or os.getenv("PANTEON_XSRF_TOKEN")

    headers = {
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    if token:
        # Remove prefix if user accidentally included 'Bearer '
        clean_token = token.replace("Bearer ", "").strip()
        headers["Authorization"] = f"Bearer {clean_token}"

    if cookie_str:
        headers["Cookie"] = cookie_str.strip()

    if xsrf:
        headers["X-XSRF-TOKEN"] = xsrf.strip()

    if not token and not cookie_str:
        raise PermissionError(
            "Authentication credentials missing! Please supply `bearer_token` or `cookie` argument, "
            "or set `PANTEON_BEARER_TOKEN` or `PANTEON_COOKIE` in your `.env` file."
        )

    return headers


def _decode_jwt_payload(token: str) -> Optional[Dict[str, Any]]:
    """
    Decodes (without verifying the signature) the payload segment of a JWT,
    purely to read informational claims like `exp`. Returns None if the
    token isn't a parseable 3-segment JWT.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def describe_token_expiry(token: str) -> Optional[str]:
    """
    Returns a human-readable expiry description for a bearer JWT (e.g.
    "expired 2h ago" or "valid, expires in 3d 4h"), or None if the token
    isn't a JWT or carries no `exp` claim.
    """
    payload = _decode_jwt_payload(token)
    if not payload or "exp" not in payload:
        return None
    try:
        exp = datetime.fromtimestamp(float(payload["exp"]), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None

    total_seconds = int((exp - datetime.now(tz=timezone.utc)).total_seconds())
    days, rem = divmod(abs(total_seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    span = f"{days}d {hours}h" if days else (f"{hours}h {minutes}m" if hours else f"{minutes}m")

    return f"expired {span} ago" if total_seconds <= 0 else f"valid, expires in {span}"


def check_auth(headers: Dict[str, str], company_id: Optional[str] = None) -> Dict[str, str]:
    """
    Verifies that `headers` are accepted by the Panteon API, without needing a
    real issue ID. Requests a deliberately non-existent issue id and inspects
    the outcome: Panteon's auth layer rejects with 401/403 before it would
    ever look up the task, so any other response (including a 404) means the
    credentials themselves are valid — only the probe id doesn't exist.

    Returns {"status": "valid" | "invalid" | "unknown", "detail": str}.
    Never raises for auth or network failures; those are reported in the
    result instead.
    """
    probe_issue_id = "999999999"
    try:
        get_task_by_issue(probe_issue_id, headers, company_id=company_id)
        return {"status": "valid", "detail": "Credentials accepted by the Panteon API."}
    except PermissionError as exc:
        return {"status": "invalid", "detail": str(exc)}
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else "?"
        return {
            "status": "valid",
            "detail": f"Credentials accepted (probe request returned HTTP {status_code}, as expected).",
        }
    except requests.RequestException as exc:
        return {"status": "unknown", "detail": f"Could not reach Panteon API: {exc}"}


def get_task_by_issue(
    issue_id: str,
    headers: Dict[str, str],
    company_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Calls Panteon API 1: /api/taskbyissue/{issue_id}/{company_id}
    Resolves the external issue ID to the internal task record.
    """
    comp_id = company_id or os.getenv("PANTEON_COMPANY_ID", DEFAULT_COMPANY_ID)
    url = f"{BASE_URL}/api/taskbyissue/{issue_id}/{comp_id}"

    response = _api_get(url, headers, context=f" when fetching task {issue_id}")
    return response.json()


def extract_task_id_from_data(task_data: Dict[str, Any]) -> int:
    """
    Extracts the underlying internal task_id required for timeline API calls.
    Follows exact fallback logic from the reference Chrome extension.
    """
    if isinstance(task_data, dict):
        if "task_id" in task_data and task_data["task_id"]:
            return int(task_data["task_id"])

        data_section = task_data.get("data", {})
        if isinstance(data_section, dict):
            if "task_id" in data_section and data_section["task_id"]:
                return int(data_section["task_id"])
            if "id" in data_section and data_section["id"]:
                return int(data_section["id"])

        if "id" in task_data and task_data["id"]:
            return int(task_data["id"])

    raise ValueError("Could not extract internal `task_id` from Panteon API response.")


def extract_task_meta(raw_task: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pulls the task header (title, full description body, creator, timestamp, status,
    tags) out of the API-1 response.
    """
    data = raw_task.get("data") if isinstance(raw_task.get("data"), dict) else raw_task
    data = data or {}
    creator = data.get("task_creator") or {}
    tags = [t.get("name") for t in (data.get("task_tags") or []) if isinstance(t, dict) and t.get("name")]
    return {
        "issue_number": data.get("issue_number"),
        "project_id": data.get("project_id"),
        "title": data.get("title") or data.get("name") or "",
        "description": (data.get("description") or "").strip(),
        "creator": creator.get("name") if isinstance(creator, dict) else "",
        "created_at": data.get("created_at_string") or data.get("created_at") or "",
        "created_date": _normalize_date(data.get("created_at") or data.get("created_at_string") or ""),
        "status": data.get("task_status") or "",
        "tags": tags,
    }


# --------------------------------------------------------------------------- #
# Workflow stage status (bulk, no timeline fetch, nothing written to disk)
# --------------------------------------------------------------------------- #
# Timeline tracks each task through six stages, each with a progress code.
# task_status stays "open" for every live task, so the stages are the signal.
TASK_STAGES = {
    "stage_rsd": "RSD",
    "stage_est": "Estimate",
    "stage_dev": "Development",
    "stage_tst": "Testing",
    "stage_qa": "QA",
    "stage_liv": "Live",
}
STAGE_PROGRESS = {0: "not started", 1: "skipped", 2: "in progress", 3: "blocked", 4: "done"}
# HTTP statuses taskbyissue answers with for an issue that doesn't exist.
_TASK_NOT_FOUND = (404, 422)


def _stage_code(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def release_state(stages: Dict[str, Optional[int]]) -> str:
    """
    Where a task is on the way to production, from its stage codes:
    "live" (Live done), "qa approved" (QA done, not live), "in qa",
    "qa blocked", or "before qa".
    """
    qa, live = stages.get("stage_qa"), stages.get("stage_liv")
    if live == 4:
        return "live"
    if qa == 4:
        return "qa approved"
    if qa == 2:
        return "in qa"
    if qa == 3:
        return "qa blocked"
    return "before qa"


def extract_task_status(raw_task: Dict[str, Any], issue_id: str) -> Dict[str, Any]:
    """
    Pulls the workflow status out of an API-1 (taskbyissue) response:
    {"issue_id", "title", "project_id", "task_status", "stage_status", "stages",
    "release_state", "qa_approved"}. `stages` maps each TASK_STAGES label to its
    progress word. `qa_approved` is true once QA is done (or the task is already live).
    """
    data = raw_task.get("data") if isinstance(raw_task.get("data"), dict) else raw_task
    data = data or {}
    codes = {field: _stage_code(data.get(field)) for field in TASK_STAGES}
    state = release_state(codes)
    return {
        "issue_id": str(issue_id),
        "title": data.get("title") or data.get("name") or "",
        "project_id": data.get("project_id"),
        "task_status": data.get("task_status") or "",
        "stage_status": data.get("stage_status") or "",
        "stages": {
            label: STAGE_PROGRESS.get(codes[field], "unknown") for field, label in TASK_STAGES.items()
        },
        "release_state": state,
        "qa_approved": state in ("qa approved", "live"),
    }


def get_task_statuses(
    issue_identifiers: List[str],
    headers: Dict[str, str],
    max_workers: int = 6,
) -> List[Dict[str, Any]]:
    """
    Looks up the workflow status of several tasks with one taskbyissue call each
    (no timeline fetch, nothing written to disk), in the order given, without
    duplicates. A bad identifier or an unknown issue gives an entry with an
    "error" instead of failing the batch; auth and connection errors still raise.
    """
    from concurrent.futures import ThreadPoolExecutor

    ordered: List[str] = []
    results: Dict[str, Dict[str, Any]] = {}
    for identifier in issue_identifiers:
        try:
            issue_id = extract_issue_id(str(identifier))
        except ValueError:
            issue_id = str(identifier)
            results[issue_id] = {"issue_id": issue_id, "error": "invalid issue ID"}
        if issue_id not in ordered:
            ordered.append(issue_id)

    def lookup(issue_id: str) -> Dict[str, Any]:
        try:
            return extract_task_status(get_task_by_issue(issue_id, headers), issue_id)
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else None
            if code in _TASK_NOT_FOUND:
                return {"issue_id": issue_id, "error": "not found"}
            raise

    pending = [i for i in ordered if i not in results]
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(pending) or 1))) as pool:
        for issue_id, status in zip(pending, pool.map(lookup, pending)):
            results[issue_id] = status
    return [results[i] for i in ordered]


def render_task_statuses_markdown(statuses: List[Dict[str, Any]]) -> str:
    """Renders get_task_statuses output as a table plus release-state counts."""
    found = [s for s in statuses if "error" not in s]
    counts: Dict[str, int] = {}
    for s in found:
        counts[s["release_state"]] = counts.get(s["release_state"], 0) + 1
    order = ["live", "qa approved", "in qa", "qa blocked", "before qa"]
    lines = [
        "# Task Statuses",
        "",
        f"**Tasks:** {len(statuses)} · **QA approved:** {sum(s['qa_approved'] for s in found)}"
        + (f" · **Not found / invalid:** {len(statuses) - len(found)}" if len(found) < len(statuses) else ""),
    ]
    if counts:
        lines.append("**By release state:** " + " · ".join(f"{k} {counts[k]}" for k in order if k in counts))
    lines += [
        "",
        "| Issue | Title | Stage | QA | Live | Release state |",
        "|-------|-------|-------|----|------|---------------|",
    ]
    for s in statuses:
        if "error" in s:
            lines.append(f"| #{_table_cell(s['issue_id'])} | — | _{_table_cell(s['error'])}_ | — | — | — |")
            continue
        lines.append(
            f"| #{s['issue_id']} | {_table_cell(s['title'])} | {_table_cell(s['stage_status'])} "
            f"| {s['stages']['QA']} | {s['stages']['Live']} | {s['release_state']} |"
        )
    lines.append("")
    return "\n".join(lines)


def _normalize_date(value: str) -> str:
    """Reduces a timestamp to a YYYY-MM-DD date string. Accepts ISO (2026-06-26T..)
    or Panteon's `dd.mm.yyyy HH:MM` format. Returns '' if it can't be parsed."""
    value = (value or "").strip()
    if not value:
        return ""
    iso = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
    if iso:
        return iso.group(0)
    dmy = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", value)
    if dmy:
        return f"{dmy.group(3)}-{dmy.group(2)}-{dmy.group(1)}"
    return value


def get_task_timeline(task_id: int, headers: Dict[str, str]) -> List[Dict[str, Any]]:
    """
    Calls Panteon API 2: /api/task/{task_id}/timeline
    Retrieves full timeline and comments for the task.
    """
    url = f"{BASE_URL}/api/task/{task_id}/timeline"
    response = _api_get(url, headers, context=f" when fetching timeline for task {task_id}")

    data = response.json()
    if isinstance(data, list):
        return data
    elif isinstance(data, dict) and isinstance(data.get("data"), list):
        return data["data"]
    return []


def _resolve_url(src: str) -> str:
    """Resolves a possibly-relative image src to an absolute Panteon URL. Inline
    `data:` URIs are already self-contained and returned untouched."""
    src = (src or "").strip()
    if src.startswith("data:"):
        return src
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return BASE_URL + src
    if not src.startswith(("http://", "https://")):
        return f"{BASE_URL}/{src.lstrip('/')}"
    return src


def extract_images(description: str) -> List[str]:
    """
    Extracts every image referenced in a comment description and returns absolute URLs.
    Handles markdown (`![](src)`) and HTML (`<img src="">`) images. Relative
    `/storage/...` paths are resolved against the Panteon base URL. Non-image
    attachment links (plain markdown `[label](file.csv)`) are intentionally ignored.
    Order is preserved and duplicates removed.
    """
    urls: List[str] = []
    md_srcs = [src for _alt, src in _MD_IMAGE_RE.findall(description or "")]
    for match in md_srcs + _HTML_IMAGE_RE.findall(description or ""):
        src = _resolve_url(match)
        if src and src not in urls:
            urls.append(src)
    return urls


def collect_all_images(
    description: str,
    comments: List[Dict[str, Any]],
    extra_descriptions: Optional[List[str]] = None,
) -> List[str]:
    """
    Aggregates every image URL across the task description, optional extra
    context, and all comments, preserving order and removing duplicates.
    """
    all_images: List[str] = []
    for text in [description] + (extra_descriptions or []):
        for url in extract_images(text):
            if url not in all_images:
                all_images.append(url)
    for comment in comments:
        for url in comment.get("images", []):
            if url not in all_images:
                all_images.append(url)
    return all_images


def absolutize_markdown_images(description: str) -> str:
    """
    Rewrites markdown image srcs (`![alt](src)`) to absolute URLs so they render
    when the conversation is saved to a standalone markdown file. Non-image links
    are left untouched.
    """
    def _repl(match: "re.Match[str]") -> str:
        alt, src = match.group(1), match.group(2)
        return f"![{alt}]({_resolve_url(src)})"

    return _MD_IMAGE_RE.sub(_repl, description or "")


def parse_commits(timeline_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The commits linked to a task (timeline items with a `task_commit`), oldest first:
    {"id", "author", "created_at", "message", "url"}."""
    commits: List[Dict[str, Any]] = []
    for item in timeline_items:
        commit = item.get("task_commit") if isinstance(item, dict) else None
        if not isinstance(commit, dict) and not (isinstance(item, dict) and item.get("field_name") == "Commit Added"):
            continue
        commit = commit if isinstance(commit, dict) else {}
        employee = item.get("employee") if isinstance(item.get("employee"), dict) else {}
        commits.append({
            "id": item.get("id"),
            "author": employee.get("name") or item.get("employee_name") or "Unknown",
            "created_at": item.get("created_at") or "",
            "message": (commit.get("description") or "").strip(),
            "url": commit.get("commit_url") or None,
        })
    return commits


def parse_conversation_comments(timeline_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Filters timeline events for task comments and returns them chronologically,
    each with its author, timestamp, description text, and all embedded images
    (as absolute URLs).
    """
    comments: List[Dict[str, Any]] = []
    for item in timeline_items:
        if not isinstance(item, dict):
            continue

        item_type = item.get("type") or item.get("event") or item.get("event_type")
        has_comment = item.get("task_comment") or item.get("field_name") == "Comment Added"
        if not (item_type == "task_comment" or has_comment):
            continue

        employee = item.get("employee") or {}
        author = employee.get("name") if isinstance(employee, dict) else None
        if not author:
            author = item.get("employee_name", "Unknown")

        desc = ""
        if isinstance(item.get("task_comment"), dict):
            desc = item["task_comment"].get("description", "")
        if not desc:
            desc = item.get("description", "")
        desc = (desc or "").strip()

        images = extract_images(desc)

        # Skip empty timeline rows that carry neither text nor images.
        if not desc and not images:
            continue

        photo = employee.get("photo") if isinstance(employee, dict) else None
        comments.append({
            "id": item.get("id") if isinstance(item.get("id"), int) else None,
            "author": author,
            "photo": photo if isinstance(photo, str) else None,
            "created_at": item.get("created_at", ""),
            "description": desc,
            "images": images,
        })

    return comments


def _as_code_block(text: str) -> str:
    """
    Wraps the original (markdown) content in a fenced code block so it is shown
    literally and never collides with this file's own markdown structure. The fence
    length is chosen to be longer than any backtick run inside the content so that
    embedded code fences cannot break out.
    """
    longest = 0
    for run in re.findall(r"`+", text or ""):
        longest = max(longest, len(run))
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def _is_same_person(a: Optional[str], b: Optional[str]) -> bool:
    """Case/whitespace-insensitive name match; empty names never match."""
    a, b = (a or "").strip().casefold(), (b or "").strip().casefold()
    return bool(a) and a == b


def render_task_markdown(
    issue_id: str,
    task_meta: Dict[str, Any],
    comments: List[Dict[str, Any]],
) -> str:
    """
    Renders the task as a `task.md` document:

        # Task {id}: {title}
        **ID / Title / Creator / Date Created / Status / Tags**
        ## Description
        ## User-Provided Context
        ## Conversation
        ## Acceptance Criteria
        ## Open Questions
        ## Images

    The file's own structure (heading, metadata, section headings) is plain markdown,
    while every piece of *original* content (the description and each comment) is
    placed inside a fenced code block so the two markdown layers never mix. Image
    references inside that content are rewritten to absolute URLs so the links remain
    usable.

    Comments written by the task creator are labelled "(task creator)" in their
    heading, so requirements can be told apart from discussion by other people.

    `## Acceptance Criteria` and `## Open Questions` need reasoning over the
    description and conversation, which this deterministic fetcher cannot do, so
    they are emitted as placeholders for an AI assistant to complete.
    """
    title = task_meta.get("title") or f"Issue {issue_id}"
    user_context = (task_meta.get("user_context") or "").strip()
    all_images = collect_all_images(
        task_meta.get("description", ""),
        comments,
        extra_descriptions=[user_context] if user_context else None,
    )

    creator = task_meta.get("creator") or ""
    tags = ", ".join(task_meta.get("tags") or []) or "—"

    lines = [
        f"# Task {issue_id}: {title}",
        "",
        f"**ID:** {issue_id}",
        f"**Title:** {title}",
        f"**Creator:** {creator or '—'}",
        f"**Date Created:** {task_meta.get('created_date') or task_meta.get('created_at') or '—'}",
        f"**Status:** {task_meta.get('status') or '(unknown)'}",
        f"**Tags:** {tags}",
        "",
    ]

    lines.append("## Description")
    lines.append("")
    description = absolutize_markdown_images(task_meta.get("description", ""))
    lines.append(_as_code_block(description) if description else "_(no description)_")
    lines.append("")

    if user_context:
        lines.append("## User-Provided Context")
        lines.append("")
        lines.append(_as_code_block(absolutize_markdown_images(user_context)))
        lines.append("")

    lines.append(f"## Conversation ({len(comments)} comment(s))")
    lines.append("")
    if not comments:
        lines.append("_(no comments)_")
        lines.append("")
    for comment in comments:
        header = comment["author"] or "Unknown"
        if _is_same_person(comment["author"], creator):
            header += " (task creator)"
        if comment.get("created_at"):
            header += f" — {comment['created_at']}"
        if comment.get("id"):
            # Timeline's id for the comment, so a workflow can point at one finding without quoting it.
            header += f" · comment id {comment['id']}"
        lines.append(f"### {header}")
        lines.append("")
        body = absolutize_markdown_images(comment["description"])
        lines.append(_as_code_block(body) if body else "_(no text)_")
        lines.append("")

    lines.append("## Acceptance Criteria")
    lines.append("")
    lines.append("_To be derived from the description above._")
    lines.append("")

    lines.append("## Open Questions")
    lines.append("")
    lines.append("_To be derived from the description and conversation above._")
    lines.append("")

    lines.append(f"## Images ({len(all_images)})")
    lines.append("")
    if not all_images:
        lines.append("_(no images)_")
    else:
        for i, url in enumerate(all_images, 1):
            lines.append(f"{i}. {url}")
            lines.append("")
            lines.append(f"   ![image {i}]({url})")
            lines.append("")
    lines.append("")
    return "\n".join(lines)


def task_markdown_path(issue_id: str, base_dir: Optional[str] = None) -> str:
    """
    Returns the absolute path where a task's markdown lives:
    `<base>/timeline/<issue_id>/task.md`.

    base_dir defaults to the PANTEON_TIMELINE_DIR env var, then the current working
    directory. Does not create anything on disk.
    """
    return os.path.join(timeline_dir(base_dir), str(issue_id), "task.md")


def timeline_dir(base_dir: Optional[str] = None) -> str:
    """`<base>/timeline`, where <base> is base_dir, then PANTEON_TIMELINE_DIR,
    then the current working directory."""
    base = base_dir or os.getenv("PANTEON_TIMELINE_DIR") or os.getcwd()
    return os.path.abspath(os.path.join(base, "timeline"))


def save_task_markdown(
    issue_id: str,
    task_meta: Dict[str, Any],
    comments: List[Dict[str, Any]],
    base_dir: Optional[str] = None,
) -> str:
    """
    Writes the task to `<base_dir>/timeline/<issue_id>/task.md` and returns the
    absolute path of the written file. See `task_markdown_path` for how the base
    directory is resolved.
    """
    path = task_markdown_path(issue_id, base_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render_task_markdown(issue_id, task_meta, comments))
    return path


def resolve_user_id(user_id: Optional[Any] = None, bearer_token: Optional[str] = None) -> int:
    """
    Resolves the Panteon user id whose planner should be shown, in order:
    explicit `user_id` argument, then the PANTEON_USER_ID env var, then the
    `sub` claim of the bearer JWT (Panteon's tokens carry the user id there).
    Cookie-only setups have no JWT to read, so they must set PANTEON_USER_ID.
    """
    candidate = user_id if user_id not in (None, "") else os.getenv("PANTEON_USER_ID")
    if candidate in (None, ""):
        token = (bearer_token or os.getenv("PANTEON_BEARER_TOKEN") or "").replace("Bearer ", "").strip()
        payload = _decode_jwt_payload(token) if token else None
        candidate = (payload or {}).get("sub")

    try:
        return int(str(candidate).strip())
    except (TypeError, ValueError):
        raise ValueError(
            "Could not determine your Panteon user id. Set PANTEON_USER_ID, pass "
            "`user_id`, or use a PANTEON_BEARER_TOKEN whose JWT carries a numeric `sub`."
        ) from None


# Planner periods (the endpoint's path segment) mapped to a display label.
# "commingup" (sic) is the real API spelling and is the only one accepted.
# Unknown segments are NOT rejected by the API — they silently return every
# planner task (e.g. "comingup") — so callers must validate.
PLANNER_PERIODS = {
    "now": "Now",
    "next": "Next",
    "commingup": "Coming Up",
}


def normalize_planner_period(period: Optional[str]) -> str:
    """Returns the API path segment for a planner period (now, next, or
    commingup). Raises ValueError for anything else."""
    key = (period or "now").strip().lower()
    if key not in PLANNER_PERIODS:
        raise ValueError(
            f"Unknown planner period '{period}'. Use one of: {', '.join(PLANNER_PERIODS)}."
        )
    return key


def get_priority_tasks(headers: Dict[str, str], period: str = "now") -> List[Dict[str, Any]]:
    """
    Calls /api/employee-planner/get-priority-tasks/{period} (now, next or
    commingup) and returns the per-user list:
    [{"user_id", "sort_order", "data": [task, ...]}, ...].
    The endpoint returns every employee's planner, not just the caller's.
    """
    segment = normalize_planner_period(period)
    url = f"{BASE_URL}/api/employee-planner/get-priority-tasks/{segment}"
    data = _api_get(url, headers, context=f" when fetching the employee planner ({segment})").json()
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return data["data"]
    return data if isinstance(data, list) else []


def extract_user_priority_tasks(planner: List[Dict[str, Any]], user_id: int) -> List[Dict[str, Any]]:
    """
    Picks one user's tasks out of the planner, ordered by priority_sort_order.
    Raises LookupError when the user isn't in the planner at all — that almost
    always means a wrong user id, which is different from having zero tasks.
    """
    for entry in planner:
        if isinstance(entry, dict) and str(entry.get("user_id")) == str(user_id):
            tasks = [t for t in (entry.get("data") or []) if isinstance(t, dict)]
            return sorted(tasks, key=lambda t: (t.get("priority_sort_order") is None, t.get("priority_sort_order") or 0))
    raise LookupError(
        f"User {user_id} was not found in the employee planner. "
        "Check PANTEON_USER_ID (or the `user_id` argument)."
    )


def _table_cell(value: Any) -> str:
    """Flattens a value into a single markdown table cell (no pipes/newlines)."""
    text = " ".join(str(value if value is not None else "").split())
    return text.replace("|", "\\|") or "—"


DUE_SOON_DAYS = 7


def deadline_flag(deadline: Optional[str], today: Optional[date] = None) -> str:
    """
    Classifies a deadline relative to `today`: "overdue", "due today",
    "due in Nd" (within DUE_SOON_DAYS), or "" when it is later, missing or
    unparseable.
    """
    normalized = _normalize_date(deadline or "")
    try:
        due = date.fromisoformat(normalized)
    except ValueError:
        return ""
    days = (due - (today or date.today())).days
    if days < 0:
        return "overdue"
    if days == 0:
        return "due today"
    if days <= DUE_SOON_DAYS:
        return f"due in {days}d"
    return ""


def _deadline_cell(deadline: Optional[str], flag: str) -> str:
    cell = _table_cell(_normalize_date(deadline or ""))
    return f"{cell} **({flag})**" if flag else cell


def render_priority_tasks_markdown(
    user_id: int,
    tasks: List[Dict[str, Any]],
    period: str = "now",
    today: Optional[date] = None,
    title: Optional[str] = None,
    level: int = 1,
) -> str:
    """
    Renders a user's planner tasks as a markdown table in priority order. Each
    issue links to its task page and can be passed to get_task_conversation.
    Deadlines that are overdue or due within DUE_SOON_DAYS of `today` are
    flagged in the Deadline column and counted in the header.

    `title`/`level` let fetch_my_day embed this as a sub-section; the User ID
    line is only shown at the top level.
    """
    label = PLANNER_PERIODS[normalize_planner_period(period)]
    today = today or date.today()
    flags = [deadline_flag(task.get("deadline"), today) for task in tasks]
    overdue = flags.count("overdue")
    due_soon = sum(1 for flag in flags if flag and flag != "overdue")
    lines = [f"{'#' * level} {title or f'My Priority Tasks — {label}'}", ""]
    if level == 1:
        lines.append(f"**User ID:** {user_id}")
    lines.append(f"**Tasks:** {len(tasks)}")
    if tasks:
        lines.append(
            f"**Overdue:** {overdue} · **Due within {DUE_SOON_DAYS} days:** {due_soon} "
            f"(as of {today.isoformat()})"
        )
    lines.append("")
    if not tasks:
        lines += ["_(no priority tasks)_", ""]
        return "\n".join(lines)

    lines += [
        "| # | Issue | Title | Stage | Status | Deadline | Support |",
        "|---|-------|-------|-------|--------|----------|---------|",
    ]
    for i, (task, flag) in enumerate(zip(tasks, flags), 1):
        issue = task.get("issue_number")
        code = task.get("code")
        issue_cell = f"#{issue}"
        if issue and code:
            issue_cell = f"[#{issue}]({BASE_URL}/tasks#/list/{code}/edit/{issue})"
        lines.append(
            f"| {i} | {issue_cell} | {_table_cell(task.get('title'))} "
            f"| {_table_cell(task.get('stage_status'))} | {_table_cell(task.get('task_status'))} "
            f"| {_deadline_cell(task.get('deadline'), flag)} "
            f"| {'yes' if task.get('support') else 'no'} |"
        )
    lines.append("")
    return "\n".join(lines)


def fetch_my_priority_tasks(
    headers: Dict[str, str],
    user_id: Optional[Any] = None,
    bearer_token: Optional[str] = None,
    period: str = "now",
) -> Dict[str, Any]:
    """
    Shared orchestration for the planner (MCP tool and CLI): resolve the user id,
    fetch the planner for `period` (now, next or commingup), and return
    {"user_id", "period", "tasks", "markdown"}. Blocking I/O.
    """
    segment = normalize_planner_period(period)
    resolved = resolve_user_id(user_id, bearer_token=bearer_token)
    tasks = extract_user_priority_tasks(get_priority_tasks(headers, segment), resolved)
    return {
        "user_id": resolved,
        "period": segment,
        "tasks": tasks,
        "markdown": render_priority_tasks_markdown(resolved, tasks, segment),
    }


# --------------------------------------------------------------------------- #
# Planner change tracking: snapshots of the user's planner kept in a SQLite
# file. A snapshot row is only added when something changed since the latest
# one, so polling every few minutes stays cheap and every row is a change.
# --------------------------------------------------------------------------- #
PLANNER_CHANGE_FIELDS = {
    "deadline": "deadline",
    "stage_status": "stage",
    "task_status": "status",
}


def planner_snapshot_dir(base_dir: Optional[str] = None) -> str:
    """`<base>/timeline/planner` (see timeline_dir)."""
    return os.path.join(timeline_dir(base_dir), "planner")


def planner_db_path(base_dir: Optional[str] = None) -> str:
    """`<base>/database/timeline_task.db`, the SQLite file holding the planner
    snapshots (next to, not inside, the generated `timeline/` exports)."""
    base = base_dir or os.getenv("PANTEON_TIMELINE_DIR") or os.getcwd()
    return os.path.abspath(os.path.join(base, "database", "timeline_task.db"))


def _import_json_snapshots(conn: sqlite3.Connection, directory: str) -> None:
    """One-time import of the earlier `<date>.json` snapshots, so switching to
    SQLite doesn't lose the history. The files are left where they are."""
    if not os.path.isdir(directory):
        return
    for name in sorted(os.listdir(directory)):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", name):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                snapshot = json.load(fh)
            snapshot["taken_at"] = snapshot.get("taken_at") or f"{snapshot['date']}T00:00:00"
            _insert_snapshot(conn, snapshot)
        except (OSError, ValueError, KeyError):
            continue


def _insert_snapshot(conn: sqlite3.Connection, snapshot: Dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO snapshots (user_id, date, taken_at, data) VALUES (?, ?, ?, ?)",
        (str(snapshot["user_id"]), snapshot["date"], snapshot["taken_at"],
         json.dumps(snapshot, ensure_ascii=False)),
    )


def _open_planner_db(base_dir: Optional[str], create: bool) -> Optional[sqlite3.Connection]:
    """Opens the snapshot database. Without `create`, a missing file gives None
    and nothing is written, so read-only calls leave no trace on disk."""
    path = planner_db_path(base_dir)
    if not create and not os.path.exists(path):
        return None
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)  # the dashboard and the MCP server may write at once
    conn.execute("PRAGMA journal_mode=WAL")
    if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                date TEXT NOT NULL,
                taken_at TEXT NOT NULL,
                data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS snapshots_user_time ON snapshots (user_id, taken_at);
            CREATE TABLE IF NOT EXISTS checks (user_id TEXT PRIMARY KEY, checked_at TEXT NOT NULL);
            """
        )
        _import_json_snapshots(conn, planner_snapshot_dir(base_dir))
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    if conn.execute("PRAGMA user_version").fetchone()[0] < 2:
        # Per-task event history (see task_events.py): the tasks being watched
        # and every Timeline event fetched for them, keyed by Timeline's own id.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS watchlist (
                issue_number TEXT PRIMARY KEY,
                task_id INTEGER,
                code TEXT,
                title TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                added_at TEXT NOT NULL,
                last_synced_at TEXT,
                last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS task_events (
                id INTEGER PRIMARY KEY,
                issue_number TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                kind TEXT NOT NULL,
                field_name TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                employee TEXT,
                photo TEXT,
                body TEXT,
                url TEXT,
                raw TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS task_events_time ON task_events (occurred_at, id);
            CREATE INDEX IF NOT EXISTS task_events_issue ON task_events (issue_number, occurred_at);
            """
        )
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    if conn.execute("PRAGMA user_version").fetchone()[0] < 3:
        # Task workflows (see workflow.py): each task's current step, every
        # Claude run of a step, and every decision (approvals, skips, …).
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS workflows (
                issue_number TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                repo TEXT NOT NULL,
                branch TEXT,
                worktree TEXT,
                step TEXT NOT NULL,
                state TEXT NOT NULL,
                qa_round INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workflow_runs (
                id INTEGER PRIMARY KEY,
                issue_number TEXT NOT NULL,
                step TEXT NOT NULL,
                session_id TEXT NOT NULL,
                cwd TEXT NOT NULL,
                prompt TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                exit_code INTEGER,
                result TEXT,
                result_at TEXT,
                decision TEXT,
                decided_at TEXT,
                note TEXT
            );
            CREATE INDEX IF NOT EXISTS workflow_runs_issue ON workflow_runs (issue_number, id);
            CREATE TABLE IF NOT EXISTS workflow_events (
                id INTEGER PRIMARY KEY,
                issue_number TEXT NOT NULL,
                at TEXT NOT NULL,
                step TEXT,
                run_id INTEGER,
                kind TEXT NOT NULL,
                note TEXT
            );
            CREATE INDEX IF NOT EXISTS workflow_events_issue ON workflow_events (issue_number, id);
            """
        )
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
    if conn.execute("PRAGMA user_version").fetchone()[0] < 4:
        # Workflows follow Timeline (phase 2): the last Timeline event the user
        # has seen, and where the current step started (its time, the last run
        # and the last Timeline event before it), so "new since" is exact.
        conn.executescript(
            """
            ALTER TABLE workflows ADD COLUMN seen_event_id INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE workflows ADD COLUMN step_since TEXT;
            ALTER TABLE workflows ADD COLUMN step_run_floor INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE workflows ADD COLUMN step_event_floor INTEGER NOT NULL DEFAULT 0;
            """
        )
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
    if conn.execute("PRAGMA user_version").fetchone()[0] < 5:
        # QA findings checklist and prod fixes (phase 3): each QA comment (or a
        # finding you add) with your verdict, the run that fixed it, and where
        # the current QA window started.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS qa_findings (
                id INTEGER PRIMARY KEY,
                issue_number TEXT NOT NULL,
                event_id INTEGER,
                author TEXT,
                posted_at TEXT,
                label TEXT,
                qa_round INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                run_id INTEGER,
                note TEXT,
                created_at TEXT NOT NULL,
                decided_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS qa_findings_comment ON qa_findings (issue_number, event_id) WHERE label IS NULL;
            CREATE INDEX IF NOT EXISTS qa_findings_issue ON qa_findings (issue_number, id);
            ALTER TABLE workflow_runs ADD COLUMN finding_id INTEGER;
            ALTER TABLE workflows ADD COLUMN qa_event_floor INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE workflows ADD COLUMN prod_round INTEGER NOT NULL DEFAULT 0;
            """
        )
        conn.execute("PRAGMA user_version = 5")
        conn.commit()
    if conn.execute("PRAGMA user_version").fetchone()[0] < 6:
        # Runs on the page (phase 4): a run is in a terminal or in the background
        # on the desk, and a background run's process group id.
        conn.executescript(
            """
            ALTER TABLE workflow_runs ADD COLUMN mode TEXT NOT NULL DEFAULT 'terminal';
            ALTER TABLE workflow_runs ADD COLUMN pid INTEGER;
            """
        )
        conn.execute("PRAGMA user_version = 6")
        conn.commit()
    if conn.execute("PRAGMA user_version").fetchone()[0] < 7:
        # Approvals on the page (phase 5): every tool call an in-dashboard run
        # wants to make, and your decision on it.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS workflow_permissions (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL,
                issue_number TEXT NOT NULL,
                tool TEXT NOT NULL,
                summary TEXT,
                input TEXT,
                status TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                decided_at TEXT,
                note TEXT
            );
            CREATE INDEX IF NOT EXISTS workflow_permissions_run ON workflow_permissions (run_id, id);
            """
        )
        conn.execute("PRAGMA user_version = 7")
        conn.commit()
    if conn.execute("PRAGMA user_version").fetchone()[0] < 8:
        # The Jira board (see jira_board.py): the cards you added and the column
        # each is in, plus a log of every change, which a Jira bot reads and
        # marks delivered.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jira_cards (
                issue_number TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                added_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jira_events (
                id INTEGER PRIMARY KEY,
                issue_number TEXT NOT NULL,
                title TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                delivered_at TEXT
            );
            CREATE INDEX IF NOT EXISTS jira_events_pending ON jira_events (delivered_at, id);
            """
        )
        conn.execute("PRAGMA user_version = 8")
        conn.commit()
    if conn.execute("PRAGMA user_version").fetchone()[0] < 9:
        # The board's list code, so a card can link to its page in Timeline.
        conn.execute("ALTER TABLE jira_cards ADD COLUMN code TEXT")
        conn.execute("PRAGMA user_version = 9")
        conn.commit()
    if conn.execute("PRAGMA user_version").fetchone()[0] < 10:
        # Whether the card is one of Timeline's support tasks, so it stays highlighted.
        conn.execute("ALTER TABLE jira_cards ADD COLUMN support INTEGER NOT NULL DEFAULT 0")
        conn.execute("PRAGMA user_version = 10")
        conn.commit()
    if conn.execute("PRAGMA user_version").fetchone()[0] < 11:
        # Release coverage (see release_coverage.py): every commit of your tickets that was seen, and when it was
        # first and last seen on qa-release and on production, so a commit that vanished from them shows as dropped.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS coverage_commits (
                repo TEXT NOT NULL,
                sha TEXT NOT NULL,
                issue_number TEXT NOT NULL,
                branch TEXT,
                author TEXT,
                email TEXT,
                committed_at TEXT,
                subject TEXT,
                first_seen TEXT NOT NULL,
                in_qa INTEGER NOT NULL DEFAULT 0,
                in_prod INTEGER NOT NULL DEFAULT 0,
                qa_first_seen TEXT,
                qa_last_seen TEXT,
                prod_first_seen TEXT,
                prod_last_seen TEXT,
                PRIMARY KEY (repo, sha)
            );
            CREATE INDEX IF NOT EXISTS coverage_commits_issue ON coverage_commits (repo, issue_number);
            CREATE TABLE IF NOT EXISTS coverage_checks (
                repo TEXT PRIMARY KEY,
                checked_at TEXT NOT NULL,
                error TEXT
            );
            """
        )
        conn.execute("PRAGMA user_version = 11")
        conn.commit()
    if conn.execute("PRAGMA user_version").fetchone()[0] < 12:
        # Tickets Timeline reports as closed are left out of release coverage for good: not looked up, not checked in git.
        conn.execute("CREATE TABLE IF NOT EXISTS coverage_closed (issue_number TEXT PRIMARY KEY, closed_seen TEXT NOT NULL)")
        conn.execute("PRAGMA user_version = 12")
        conn.commit()
    if conn.execute("PRAGMA user_version").fetchone()[0] < 13:
        # Old tickets that are fully on production: hidden from release coverage and not read again until you commit
        # something newer for them (last_commit_at is the newest commit they had when they were hidden).
        conn.execute("CREATE TABLE IF NOT EXISTS coverage_retired (issue_number TEXT PRIMARY KEY, "
                     "last_commit_at TEXT NOT NULL, retired_at TEXT NOT NULL)")
        conn.execute("PRAGMA user_version = 13")
        conn.commit()
    return conn


def build_planner_snapshot(
    planners: Dict[str, List[Dict[str, Any]]],
    user_id: int,
    today: Optional[date] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Builds a snapshot of one user's tasks across planner periods from
    {period: raw planner}. Tasks are keyed by issue number; a task listed in
    several periods keeps the earliest one (now before next before commingup).
    Raises LookupError if the user is absent from every period.
    """
    tasks: Dict[str, Dict[str, Any]] = {}
    found = False
    for period in PLANNER_PERIODS:
        try:
            period_tasks = extract_user_priority_tasks(planners.get(period) or [], user_id)
        except LookupError:
            continue
        found = True
        for position, task in enumerate(period_tasks, 1):
            key = str(task.get("issue_number"))
            if key in tasks:
                continue
            tasks[key] = {
                "issue_number": task.get("issue_number"),
                "code": task.get("code"),
                "title": task.get("title"),
                "period": period,
                "position": position,
                "stage_status": task.get("stage_status"),
                "task_status": task.get("task_status"),
                "deadline": _normalize_date(task.get("deadline") or "") or None,
                "support": bool(task.get("support")),
            }
    if not found:
        raise LookupError(f"User {user_id} was not found in any planner period.")
    moment = now or datetime.now()
    day = today or moment.date()
    return {
        "user_id": user_id,
        "date": day.isoformat(),
        "taken_at": datetime.combine(day, moment.time()).isoformat(timespec="seconds"),
        "tasks": tasks,
    }


STAGE_DETAIL_FIELDS = ("stages", "release_state", "qa_approved")


def attach_task_stages(snapshot: Dict[str, Any], headers: Dict[str, str]) -> None:
    """
    Adds each task's six workflow stages, release state and QA approval to the
    snapshot (one taskbyissue call per task, in a small thread pool). Stage
    progress is then tracked like any other field. If the lookup fails the
    snapshot is left without them, so a flaky stage call never blocks the
    planner check.
    """
    tasks = snapshot["tasks"]
    try:
        statuses = get_task_statuses([k for k in tasks], headers)
    except (requests.RequestException, PermissionError):
        return
    for status in statuses:
        task = tasks.get(str(status.get("issue_id")))
        if task is None or status.get("error"):
            continue
        for field in STAGE_DETAIL_FIELDS:
            task[field] = status.get(field)


def _carry_forward_stage_details(latest: Dict[str, Any], snapshot: Dict[str, Any]) -> None:
    """A snapshot taken without stage lookups (the MCP tools) inherits the
    latest stored stage details for tasks it shares, so a later check that has
    them reports the change from what was last known."""
    for key, task in snapshot["tasks"].items():
        before = latest["tasks"].get(key)
        if before and "stages" in before and "stages" not in task:
            for field in STAGE_DETAIL_FIELDS:
                task[field] = before.get(field)


def save_planner_snapshot(snapshot: Dict[str, Any], base_dir: Optional[str] = None) -> str:
    """
    Records the snapshot in the database and returns the database path. A row
    is only added when the tasks differ from the user's latest stored snapshot
    (priority order alone doesn't count); either way the time of this check is
    kept, so the page can show when the planner was last looked at.
    """
    conn = _open_planner_db(base_dir, create=True)
    try:
        user = str(snapshot["user_id"])
        row = conn.execute(
            "SELECT data FROM snapshots WHERE user_id = ? ORDER BY taken_at DESC, id DESC LIMIT 1", (user,)
        ).fetchone()
        latest = json.loads(row[0]) if row else None
        if latest is not None:
            _carry_forward_stage_details(latest, snapshot)
        # Also record when stage details arrive for tasks stored without them,
        # so stage tracking has a baseline to compare against.
        gains_stages = latest is not None and any(
            "stages" in task and "stages" not in latest["tasks"][key]
            for key, task in snapshot["tasks"].items() if key in latest["tasks"]
        )
        if latest is None or gains_stages or any(diff_planner_snapshots(latest, snapshot).values()):
            _insert_snapshot(conn, snapshot)
        conn.execute(
            "INSERT INTO checks (user_id, checked_at) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET checked_at = excluded.checked_at",
            (user, snapshot["taken_at"]),
        )
        conn.commit()
    finally:
        conn.close()
    return planner_db_path(base_dir)


def load_previous_planner_snapshot(
    user_id: int,
    before: str,
    base_dir: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Returns the user's latest snapshot dated strictly before `before`
    (YYYY-MM-DD), or None. Comparing against an earlier day keeps repeated
    runs on the same day reporting the same "since last time" changes.
    """
    conn = _open_planner_db(base_dir, create=False)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT data FROM snapshots WHERE user_id = ? AND date < ? ORDER BY taken_at DESC, id DESC LIMIT 1",
            (str(user_id), before),
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row else None


def diff_planner_snapshots(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Compares two snapshots. Returns {"added", "removed", "moved", "changed"}:
    added/removed are task dicts; moved items carry "from"/"to" periods;
    changed items carry "changes": [(label, old, new), …] for PLANNER_CHANGE_FIELDS.
    """
    old_tasks, new_tasks = old.get("tasks", {}), new.get("tasks", {})
    period_rank = {p: i for i, p in enumerate(PLANNER_PERIODS)}

    def order(task):
        return (period_rank.get(task.get("period"), 99), task.get("position") or 0)

    added = sorted((t for k, t in new_tasks.items() if k not in old_tasks), key=order)
    removed = sorted((t for k, t in old_tasks.items() if k not in new_tasks), key=order)
    moved, changed = [], []

    # Priority: compare the order of the tasks both snapshots share in a
    # period, so a task added on top doesn't make everything below it "move".
    priority: Dict[str, Any] = {}
    for period in PLANNER_PERIODS:
        shared = [k for k, t in new_tasks.items()
                  if k in old_tasks and t.get("period") == old_tasks[k].get("period") == period]
        old_rank = {k: i for i, k in enumerate(sorted(shared, key=lambda k: old_tasks[k].get("position") or 0))}
        new_rank = {k: i for i, k in enumerate(sorted(shared, key=lambda k: new_tasks[k].get("position") or 0))}
        for k in shared:
            # Rank decides whether anything really moved; the position must differ
            # too, or a task that merely got overtaken would be reported as moving.
            if old_rank[k] != new_rank[k] and old_tasks[k].get("position") != new_tasks[k].get("position"):
                priority[k] = (f"#{old_tasks[k].get('position')}", f"#{new_tasks[k].get('position')}")

    for key, task in sorted(new_tasks.items(), key=lambda kv: order(kv[1])):
        before = old_tasks.get(key)
        if before is None:
            continue
        if before.get("period") != task.get("period"):
            moved.append({**task, "from": before.get("period"), "to": task.get("period")})
        changes = [
            (label, before.get(field), task.get(field))
            for field, label in PLANNER_CHANGE_FIELDS.items()
            if (before.get(field) or None) != (task.get(field) or None)
        ]
        # Stage progress and release state, only when both snapshots looked them up.
        if "stages" in before and "stages" in task:
            changes += [
                (name, before["stages"].get(name), now)
                for name, now in (task["stages"] or {}).items()
                if before["stages"].get(name) is not None and before["stages"].get(name) != now
            ]
            if before.get("release_state") != task.get("release_state"):
                changes.append(("release", before.get("release_state"), task.get("release_state")))
        if key in priority:
            changes.append(("priority", *priority[key]))
        if changes:
            changed.append({**task, "changes": changes})
    return {"added": added, "removed": removed, "moved": moved, "changed": changed}


def _planner_task_ref(task: Dict[str, Any]) -> str:
    issue, code = task.get("issue_number"), task.get("code")
    ref = f"[#{issue}]({BASE_URL}/tasks#/list/{code}/edit/{issue})" if issue and code else f"#{issue}"
    return f"{ref} {_table_cell(task.get('title'))}"


def render_planner_changes_markdown(
    snapshot: Dict[str, Any],
    previous: Optional[Dict[str, Any]],
    diff: Optional[Dict[str, List[Dict[str, Any]]]],
    snapshot_path: Optional[str] = None,
    level: int = 1,
) -> str:
    """Renders the planner diff (or a first-snapshot notice) as markdown.
    With level > 1 it is a sub-section (no User ID line, "Changes" title)."""
    since = f" — since {previous['date']}" if previous else ""
    title = f"My Planner Changes{since}" if level == 1 else f"Changes{since}"
    lines = [f"{'#' * level} {title}", ""]
    meta = []
    if level == 1:
        meta.append(f"**User ID:** {snapshot['user_id']}")
    if snapshot_path:
        meta.append(f"**Snapshot:** {snapshot_path}")
    if meta:
        lines += meta + [""]

    if previous is None or diff is None:
        count = len(snapshot["tasks"])
        if snapshot_path:
            note = f"Saved today's ({count} tasks); changes show from the next day."
        else:
            note = f"Found {count} tasks; saving is off, so nothing was recorded."
        lines += [f"_No earlier snapshot to compare with. {note}_", ""]
        return "\n".join(lines)

    if not any(diff.values()):
        lines += ["_(no changes)_", ""]
        return "\n".join(lines)

    label = PLANNER_PERIODS.get
    sections = [
        ("New", diff["added"], lambda t: f"{label(t['period'], t['period'])} · {_table_cell(t.get('stage_status'))}"),
        ("Left the planner", diff["removed"], lambda t: f"was {label(t['period'], t['period'])}"),
        ("Moved", diff["moved"], lambda t: f"{label(t['from'], t['from'])} → {label(t['to'], t['to'])}"),
        ("Changed", diff["changed"], lambda t: "; ".join(
            f"{name}: {_table_cell(a)} → {_table_cell(b)}" for name, a, b in t["changes"]
        )),
    ]
    for section, items, detail in sections:
        if not items:
            continue
        lines.append(f"{'#' * (level + 1)} {section} ({len(items)})")
        lines += [f"- {_planner_task_ref(t)} — {detail(t)}" for t in items]
        lines.append("")
    return "\n".join(lines)


def fetch_my_planner_changes(
    headers: Dict[str, str],
    user_id: Optional[Any] = None,
    bearer_token: Optional[str] = None,
    base_dir: Optional[str] = None,
    save: bool = True,
    today: Optional[date] = None,
    planners: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    now: Optional[datetime] = None,
    with_stages: bool = False,
) -> Dict[str, Any]:
    """
    Shared orchestration (MCP tool and CLI): fetch every planner period (unless
    `planners` is passed in), build the current snapshot for the user, diff it
    against the latest snapshot from an earlier day, and (if `save`) record it
    (see save_planner_snapshot). `with_stages` also looks up each task's workflow
    stages (one extra request per task) so stage progress is tracked; the
    dashboard turns it on, the MCP tools leave it off.
    Returns {"user_id", "snapshot", "previous", "diff", "snapshot_path",
    "markdown"}. Blocking I/O.
    """
    resolved = resolve_user_id(user_id, bearer_token=bearer_token)
    if planners is None:
        planners = fetch_all_planners(headers)
    snapshot = build_planner_snapshot(planners, resolved, today=today, now=now)
    if with_stages:
        attach_task_stages(snapshot, headers)
    previous = load_previous_planner_snapshot(resolved, snapshot["date"], base_dir)
    diff = diff_planner_snapshots(previous, snapshot) if previous else None
    path = save_planner_snapshot(snapshot, base_dir) if save else None
    return {
        "user_id": resolved,
        "snapshot": snapshot,
        "previous": previous,
        "diff": diff,
        "snapshot_path": path,
        "markdown": render_planner_changes_markdown(snapshot, previous, diff, path),
    }


def fetch_all_planners(
    headers: Dict[str, str],
    known: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """One request per planner period: {period: raw planner}. Periods already
    fetched can be passed in `known` and are not requested again. Blocking I/O."""
    known = known or {}
    return {period: known[period] if period in known else get_priority_tasks(headers, period)
            for period in PLANNER_PERIODS}


def planner_fingerprint(planner: List[Dict[str, Any]], user_id: Any) -> str:
    """A short hash of one user's tasks in one planner period (issue, order, stages,
    status, deadline): equal fingerprints mean nothing visible there changed, so a
    one-request check can decide whether the full set of calls is worth making."""
    try:
        tasks = extract_user_priority_tasks(planner, user_id)
    except LookupError:
        tasks = []
    rows = [[t.get("issue_number"), t.get("stage_status"), t.get("task_status"), t.get("deadline"), bool(t.get("support"))]
            for t in tasks]
    return hashlib.sha1(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def fetch_my_day(
    headers: Dict[str, str],
    user_id: Optional[Any] = None,
    bearer_token: Optional[str] = None,
    base_dir: Optional[str] = None,
    save: bool = True,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """
    Everything a daily brief needs from one fetch of the three planner periods
    (3 requests): the `now` table, `next` tasks that are overdue or due within
    DUE_SOON_DAYS, and the changes since the last saved day (saving today's
    snapshot unless `save` is false). Returns {"user_id", "now", "due_soon_next",
    "changes", "markdown"}. Blocking I/O.
    """
    today = today or date.today()
    resolved = resolve_user_id(user_id, bearer_token=bearer_token)
    planners = fetch_all_planners(headers)
    changes = fetch_my_planner_changes(
        headers, user_id=resolved, base_dir=base_dir, save=save, today=today, planners=planners
    )

    def period_tasks(period):
        try:
            return extract_user_priority_tasks(planners[period], resolved)
        except LookupError:
            return []

    now = period_tasks("now")
    due_soon_next = [t for t in period_tasks("next") if deadline_flag(t.get("deadline"), today)]

    sections = [
        f"# My Day — {today.isoformat()}\n\n**User ID:** {resolved}\n",
        render_priority_tasks_markdown(resolved, now, "now", today=today, title="Now", level=2),
    ]
    if due_soon_next:
        sections.append(render_priority_tasks_markdown(
            resolved, due_soon_next, "next", today=today,
            title=f"Next — due within {DUE_SOON_DAYS} days", level=2,
        ))
    changes_md = render_planner_changes_markdown(
        changes["snapshot"], changes["previous"], changes["diff"], changes["snapshot_path"], level=2
    )
    sections.append(changes_md)
    return {
        "user_id": resolved,
        "now": now,
        "due_soon_next": due_soon_next,
        "changes": changes,
        "markdown": "\n".join(sections),
    }


# --------------------------------------------------------------------------- #
# Dashboard data (dashboard.py): JSON-friendly views over the same data
# --------------------------------------------------------------------------- #
def task_url(issue_number: Any, code: Any) -> Optional[str]:
    """Timeline page of a task, or None without both its issue number and list code."""
    if issue_number and code:
        return f"{BASE_URL}/tasks#/list/{code}/edit/{issue_number}"
    return None


def planner_overview(
    planners: Dict[str, List[Dict[str, Any]]],
    user_id: int,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """
    One user's tasks in every planner period, in priority order, with deadline
    flags and task URLs: {"user_id", "today", "periods": {period: {"label",
    "tasks": [...]}}}. A period the user is absent from is empty. Raises
    LookupError if the user is absent from every period.
    """
    today = today or date.today()
    periods: Dict[str, Dict[str, Any]] = {}
    found = False
    for period, label in PLANNER_PERIODS.items():
        try:
            tasks = extract_user_priority_tasks(planners.get(period) or [], user_id)
            found = True
        except LookupError:
            tasks = []
        periods[period] = {
            "label": label,
            "tasks": [
                {
                    "issue_number": t.get("issue_number"),
                    "title": t.get("title") or "",
                    "stage_status": t.get("stage_status") or "",
                    "task_status": t.get("task_status") or "",
                    "deadline": _normalize_date(t.get("deadline") or "") or None,
                    "deadline_flag": deadline_flag(t.get("deadline"), today),
                    "support": bool(t.get("support")),
                    "code": str(t["code"]) if t.get("code") is not None else None,
                    "url": task_url(t.get("issue_number"), t.get("code")),
                }
                for t in tasks
            ],
        }
    if not found:
        raise LookupError(f"User {user_id} was not found in any planner period.")
    return {"user_id": user_id, "today": today.isoformat(), "periods": periods}


# The project tasks endpoint describes each stage as {"id", "name"} with its own ids.
PROJECT_STAGE_PROGRESS = {0: "not started", 1: "skipped", 4: "in progress", 5: "done", 6: "in progress"}


def _project_stages(task: Dict[str, Any]) -> Dict[str, str]:
    """A project-list task's six stages as progress words, keyed by TASK_STAGES label."""
    out = {}
    for field, label in TASK_STAGES.items():
        stage = task.get(field)
        out[label] = PROJECT_STAGE_PROGRESS.get(_stage_code(stage.get("id")) if isinstance(stage, dict) else None, "not started")
    return out


def fetch_project_tasks(
    project_id: Any,
    headers: Dict[str, str],
    user_id: Optional[Any] = None,
    code: Optional[Any] = None,
    tab: Optional[str] = None,
    today: Optional[date] = None,
    support: bool = False,
) -> Dict[str, Any]:
    """
    The user's open tasks in one project, as Timeline lists them: tabs (with
    counts) and, for the active tab, its groups of tasks. `project_id` is the
    API id (not the list code in task URLs); `code` is that list code, used for
    task URLs. Without `tab` Timeline answers with its first tab. With `support`
    the project's Support tab is asked for instead (Timeline's own request for
    it carries `support: true` and `tab_name: "Support"`; `tab` is ignored).
    Read-only, although the endpoint itself is a POST.
    {"project_id", "tab", "tabs": [{"name", "count"}], "groups": [{"name", "tasks"}]}
    """
    project_id = int(project_id)
    body: Dict[str, Any] = {"subscribed_by": resolve_user_id(user_id), "tag_id": 0, "status": "open"}
    if support:
        body.update({"support": True, "tab_name": "Support", "deadline_from": "", "deadline_to": ""})
    elif tab:
        body["tab_name"] = tab
    url = f"{BASE_URL}/api/project/{project_id}/tasks"
    _pace()
    response = timed_request("POST", url, lambda: _SESSION.post(url, headers=headers, json=body, timeout=30))
    if response.status_code in (401, 403):
        raise PermissionError(
            f"Authentication failed (HTTP {response.status_code}) when fetching project {project_id} tasks. "
            "Please verify your PANTEON_BEARER_TOKEN or PANTEON_COOKIE."
        )
    if response.status_code in _TASK_NOT_FOUND:
        raise LookupError(f"Project {project_id} was not found.")
    response.raise_for_status()
    data = (response.json() or {}).get("data") or {}
    tabs = sorted(data.get("tabs") or [], key=lambda t: t.get("tab_sort_order") or 0)
    today = today or date.today()
    groups = []
    for group in data.get("grouptasks") or []:
        tasks = []
        for t in group.get("tasks") or []:
            deadline = _normalize_date(t.get("deadline") or "") or None
            tasks.append({
                "issue_number": t.get("issue_number"),
                "title": t.get("title") or "",
                "stage_status": t.get("stage_status") or "",
                "stages": _project_stages(t),
                "deadline": deadline,
                "deadline_flag": deadline_flag(t.get("deadline"), today),
                "hours_spent": t.get("hours_spent_decimal"),
                "hours_estimated": t.get("estimated_hours"),
                "release": t.get("release"),
                "tags": [{"name": g.get("name") or "", "color": g.get("color")} for g in t.get("task_tags") or []],
                "subscribers": [{"name": s.get("name") or "", "photo": s.get("photo")} for s in t.get("task_subscribers") or []],
                "code": str(code) if code else None,
                "url": task_url(t.get("issue_number"), code),
            })
        groups.append({"name": group.get("name") or "", "tasks": tasks})
    active = tab or (data.get("tabs") and tabs[0].get("tab_name")) or None
    return {
        "project_id": project_id,
        "tab": active,
        "tabs": [{"name": t.get("tab_name") or "", "count": t.get("tab_task_count") or 0} for t in tabs],
        "groups": groups,
        "today": today.isoformat(),
    }


def list_planner_snapshots(user_id: int, base_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every stored snapshot for `user_id`, oldest first."""
    conn = _open_planner_db(base_dir, create=False)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT data FROM snapshots WHERE user_id = ? ORDER BY taken_at, id", (str(user_id),)
        ).fetchall()
    finally:
        conn.close()
    return [json.loads(data) for (data,) in rows]


def last_planner_check(user_id: int, base_dir: Optional[str] = None) -> Optional[str]:
    """When the planner was last fetched and recorded for `user_id` (ISO time)."""
    conn = _open_planner_db(base_dir, create=False)
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT checked_at FROM checks WHERE user_id = ?", (str(user_id),)).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def planner_history(user_id: int, base_dir: Optional[str] = None) -> Dict[str, Any]:
    """
    Planner changes between consecutive stored snapshots, newest first:
    {"user_id", "tracking_since", "last_checked", "changes": [{"taken_at",
    "previous_taken_at", "diff"}]}. Each task in a diff gets a "url".
    """
    snapshots = list_planner_snapshots(user_id, base_dir)

    def on_planner_since(index: int, key: str) -> Optional[str]:
        """When the task's unbroken stretch on the planner began, as of
        snapshots[index]; None if it isn't there."""
        if key not in snapshots[index]["tasks"]:
            return None
        while index > 0 and key in snapshots[index - 1]["tasks"]:
            index -= 1
        return snapshots[index]["taken_at"]

    changes = []
    for index in range(1, len(snapshots)):
        previous, current = snapshots[index - 1], snapshots[index]
        diff = diff_planner_snapshots(previous, current)
        if not any(diff.values()):
            continue
        for kind, items in diff.items():
            for task in items:
                task["url"] = task_url(task.get("issue_number"), task.get("code"))
                task["deadline_flag"] = deadline_flag(task.get("deadline"))
                if kind != "added":
                    # Removed tasks are looked up as of the snapshot they were last in.
                    since = on_planner_since(index - 1 if kind == "removed" else index, str(task.get("issue_number")))
                    task["on_planner_since"] = since
                    # The first snapshot is only where tracking began, not when the task arrived.
                    task["since_tracking_start"] = since == snapshots[0]["taken_at"]
        changes.append({"taken_at": current["taken_at"], "previous_taken_at": previous["taken_at"], "diff": diff})
    changes.reverse()
    return {
        "user_id": user_id,
        "tracking_since": snapshots[0]["taken_at"] if snapshots else None,
        "last_checked": last_planner_check(user_id, base_dir),
        "changes": changes,
    }


def fetch_task_data(
    issue_identifier: str,
    headers: Dict[str, str],
) -> Dict[str, Any]:
    """
    Runs the full read pipeline for a single task — resolve the issue id, call the
    two Panteon APIs, and parse the results — and returns the parsed pieces:

        {"issue_id", "task_id", "task_meta", "comments", "commits"}

    This is the shared orchestration used by both the MCP tool and the CLI. It is
    synchronous and performs blocking network I/O, so async callers should offload
    it to a thread (see server.py).
    """
    issue_id = extract_issue_id(issue_identifier)
    raw_task = get_task_by_issue(issue_id, headers)
    task_id = extract_task_id_from_data(raw_task)
    task_meta = extract_task_meta(raw_task)

    timeline_items = get_task_timeline(task_id, headers)
    comments = parse_conversation_comments(timeline_items)
    commits = parse_commits(timeline_items)

    return {
        "issue_id": issue_id,
        "task_id": task_id,
        "task_meta": task_meta,
        "comments": comments,
        "commits": commits,
    }


def fetch_and_save(
    issue_identifier: str,
    headers: Optional[Dict[str, str]] = None,
    base_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Convenience wrapper: fetch a task and write it to disk in one call, returning
    the parsed data plus the `saved_file` path. Builds headers from the environment
    if none are supplied. Does not perform any user elicitation (that lives in the
    MCP tool); intended for scripts and the CLI.
    """
    headers = headers or build_api_headers()
    result = fetch_task_data(issue_identifier, headers)
    result["saved_file"] = save_task_markdown(
        result["issue_id"], result["task_meta"], result["comments"], base_dir=base_dir
    )
    return result
