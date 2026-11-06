#!/usr/bin/env python3
"""
Local web dashboard for Panteon Timeline: your planner board, planner changes
over time, release state of any tasks, and a readable task view.

Serves only on 127.0.0.1 and accepts only 127.0.0.1, localhost and
timeline.localhost as Host headers, because the
pages show customer data. Credentials come from the same .env as the MCP server.

Examples:
    python3 dashboard.py                 # http://timeline.localhost:8765
    python3 dashboard.py --port 9000
    python3 dashboard.py --user-id 135
"""
import argparse
import os
import random
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

# Importing panteon_client loads the .env next to it (see panteon_client.py).
import jira_board
import panteon_client
import release_check
import release_coverage
import task_events
import timeline_socket
import work_session
import workflow

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
# *.localhost names resolve to loopback without /etc/hosts (RFC 6761), so
# timeline.localhost works out of the box in browsers and systemd-resolved.
HOSTNAME = "timeline.localhost"
ALLOWED_HOSTS = ["127.0.0.1", "localhost", HOSTNAME]
MAX_STATUS_IDS = 100
MAX_GUARD_IDS = 60  # extra tickets (planner, watchlist, workflows) looked up for the PR guard
# Polling is tiered so it feels live without spikes. Every HEARTBEAT the `now`
# planner period (1 request) and the watched tasks' timelines (1 each) are read;
# the full planner check (3 periods) runs when the heartbeat sees `now` change, or
# at least every DEFAULT_SNAPSHOT_MINUTES. Stage lookups (1 request per task) run
# only when something changed, or every STAGE_REFRESH_MINUTES. Outside the active
# hours (weekdays, TIMELINE_ACTIVE_HOURS "7-19"; "all" turns it off) there is one
# full check per OFF_HOURS_MINUTES.
DEFAULT_HEARTBEAT_MINUTES = 3
DEFAULT_SNAPSHOT_MINUTES = 15
STAGE_REFRESH_MINUTES = 60
OFF_HOURS_MINUTES = 60
DEFAULT_ACTIVE_HOURS = "7-19"
COVERAGE_MINUTES = 60  # release coverage is rebuilt this often in the active hours (it must be seen to notice a drop)
COVERAGE_OFF_HOURS_MINUTES = 180
JITTER = 0.1
WAKE_GAP_SECONDS = 15.0  # least time between two checks started by socket events
WAKE_DEBOUNCE_SECONDS = 2.0  # a burst of events becomes one check
# What the background scheduler actually runs with (main() overwrites it from the command line / env);
# the Help page shows it so the numbers can be checked.
SCHEDULE = {"heartbeat_minutes": DEFAULT_HEARTBEAT_MINUTES, "full_minutes": DEFAULT_SNAPSHOT_MINUTES,
            "stage_minutes": STAGE_REFRESH_MINUTES, "off_hours_minutes": OFF_HOURS_MINUTES,
            "active_hours": DEFAULT_ACTIVE_HOURS, "jitter_percent": int(JITTER * 100),
            "coverage_minutes": COVERAGE_MINUTES, "coverage_off_hours_minutes": COVERAGE_OFF_HOURS_MINUTES}


PLANNER_CACHE_SECONDS = 300


class PlannerCache:
    """The latest raw planner ({period: planner}) kept in memory, so opening or
    refreshing a page reads it instead of calling Timeline again. The scheduler
    keeps it current; the Refresh button forces a new fetch."""

    def __init__(self, ttl: float = PLANNER_CACHE_SECONDS, clock: Callable[[], float] = time.monotonic):
        self.ttl, self.clock = ttl, clock
        self.lock = threading.Lock()
        self.data: Optional[Dict[str, Any]] = None
        self.at = 0.0

    def get(self, fetch: Callable[[], Dict[str, Any]], force: bool = False) -> Dict[str, Any]:
        with self.lock:
            if force or self.data is None or self.clock() - self.at >= self.ttl:
                self.data, self.at = fetch(), self.clock()
            return self.data

    def put(self, planners: Dict[str, Any]) -> None:
        with self.lock:
            self.data, self.at = planners, self.clock()

    def update(self, period: str, planner: Any) -> None:
        """Replaces one period (a heartbeat's fresh `now`) without making the rest look fresher."""
        with self.lock:
            if self.data is not None:
                self.data = {**self.data, period: planner}


class _FreshStaticFiles(StaticFiles):
    """web/ files with Cache-Control: no-cache, so an edit shows on the next reload (still 304 when unchanged)."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


STORAGE_NAME = re.compile(r"[A-Za-z0-9_-]+\.(?:jpe?g|png|gif|webp)")
IMAGE_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}
# Timeline names these files after a timestamp and never rewrites them, so a copy
# is good forever: browsers keep it for a year and the server keeps one on disk.
IMMUTABLE = {"Cache-Control": "private, max-age=31536000, immutable"}


class CoverageCache:
    """The latest release coverage report, kept in memory. The scheduler and the Check again button rebuild it;
    reading it never touches git. `step` says what a running rebuild is doing."""

    def __init__(self):
        self.report: Optional[Dict[str, Any]] = None
        self.step: Optional[str] = None
        self.lock = threading.Lock()


def _respond(call: Callable[[], Any]) -> JSONResponse:
    """Runs a blocking client call and maps its failures to JSON errors."""
    try:
        return JSONResponse(call())
    except release_check.ReleaseCheckError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=401)
    except LookupError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else None
        if code in (404, 422):
            return JSONResponse({"error": "Not found in Panteon Timeline."}, status_code=404)
        return JSONResponse({"error": f"Panteon API request failed (HTTP {code})."}, status_code=502)
    except requests.RequestException as exc:
        return JSONResponse({"error": f"Could not reach Panteon Timeline: {exc}"}, status_code=502)


def create_app(
    user_id: Optional[Any] = None,
    base_dir: Optional[str] = None,
    repo_dir: Optional[str] = None,
    release_builder: Callable[..., Any] = release_check.build_report,
    work_config: Callable[[], Dict[str, Any]] = work_session.load_config,
    work_runner: Any = None,
    work_launcher: Any = None,
    planner_cache: Optional[PlannerCache] = None,
    claude_home: Optional[str] = None,
    pr_runner: Any = None,
    pos_dir: Optional[str] = None,
    coverage_builder: Callable[..., Any] = release_coverage.build_coverage,
    coverage_cache: Optional[CoverageCache] = None,
) -> Starlette:
    """
    Builds the app. `user_id` and `base_dir` work as in fetch_my_day;
    `repo_dir` is the duell-admin checkout (default: DUELL_ADMIN_DIR, then
    ../duell-admin). `release_builder`, `work_config`, `work_runner` and
    `work_launcher` are swappable for tests; the work config is re-read on
    every request, so edits to timeline-desk.json need no restart.
    `claude_home` is where Claude Code keeps its session logs (default
    ~/.claude), read for a run's conversation. `pr_runner` replaces the
    git/gh runner of the PR draft and create endpoints for tests. `pos_dir`
    is the duell-pos checkout (default: DUELL_POS_DIR, then ../duell-pos).
    """
    git_kwargs = {"runner": work_runner} if work_runner else {}
    pr_kwargs = {"runner": pr_runner} if pr_runner else {}
    start_kwargs = {**git_kwargs, **({"launcher": work_launcher} if work_launcher else {})}
    release = {"report": None, "step": None, "lock": threading.Lock()}
    planners = planner_cache or PlannerCache()
    coverage = coverage_cache or CoverageCache()
    image_dir = Path(panteon_client.planner_db_path(base_dir)).parent / "images"

    def headers():
        return panteon_client.build_api_headers()

    def resolved_user() -> int:
        return panteon_client.resolve_user_id(user_id)

    def index(request: Request):
        return FileResponse(os.path.join(WEB_DIR, "index.html"), headers={"Cache-Control": "no-cache"})

    def api_planner(request: Request):
        force = request.query_params.get("refresh") == "1"
        return _respond(lambda: panteon_client.planner_overview(
            planners.get(lambda: panteon_client.fetch_all_planners(headers()), force), resolved_user()
        ))

    def api_changes(request: Request):
        return _respond(lambda: panteon_client.planner_history(resolved_user(), base_dir))

    def api_snapshot(request: Request):
        # A cross-site form can POST here but can't set this header without a
        # CORS preflight, which this app never allows.
        if request.headers.get("x-timeline-desk") != "1":
            return JSONResponse({"error": "Missing X-Timeline-Desk header."}, status_code=403)

        def save():
            result = panteon_client.fetch_my_planner_changes(
                headers(), user_id=resolved_user(), base_dir=base_dir, save=True, with_stages=True
            )
            return {"taken_at": result["snapshot"]["taken_at"], "tasks": len(result["snapshot"]["tasks"])}
        return _respond(save)

    def api_statuses(request: Request):
        ids = [i for i in request.query_params.get("ids", "").replace(",", " ").split() if i]

        def lookup():
            if not ids:
                raise ValueError("Add at least one issue ID.")
            if len(ids) > MAX_STATUS_IDS:
                raise ValueError(f"Check at most {MAX_STATUS_IDS} issues at a time.")
            return {"statuses": panteon_client.get_task_statuses(ids, headers())}
        return _respond(lookup)

    def api_task(request: Request):
        def load():
            data = panteon_client.fetch_task_data(request.path_params["issue"], headers())
            meta = data["task_meta"]
            try:  # the board (Timeline list) comes from the project's repo in timeline-desk.json
                board = work_session.board_for_project(work_config(), meta.get("project_id"))
            except work_session.WorkSessionError:
                board = None
            return {
                "issue_id": data["issue_id"],
                "url": panteon_client.task_url(data["issue_id"], board) if board else None,
                "title": meta.get("title"),
                "creator": meta.get("creator"),
                "created_date": meta.get("created_date"),
                "status": meta.get("status"),
                "tags": meta.get("tags") or [],
                "description": meta.get("description") or "",
                "comments": data["comments"],
                "commits": data.get("commits") or [],
            }
        return _respond(load)

    def storage_proxy(folder: str):
        # Timeline's profile photos and attachments are public, but the page
        # loads nothing external, so they are proxied: no credentials sent,
        # bare filename only, fixed folder, no redirects.
        def proxy(request: Request):
            name = request.path_params["name"]
            if not STORAGE_NAME.fullmatch(name):
                return JSONResponse({"error": "Not found."}, status_code=404)
            cached = image_dir / folder / name
            if cached.is_file():
                return Response(cached.read_bytes(), media_type=IMAGE_TYPES[name.rsplit(".", 1)[1].lower()], headers=IMMUTABLE)
            url = f"{panteon_client.BASE_URL}/storage/{folder}/{name}"
            try:
                upstream = panteon_client.timed_request(
                    "GET", url, lambda: requests.get(url, timeout=10, allow_redirects=False))
            except requests.RequestException:
                return JSONResponse({"error": "Image unavailable."}, status_code=502)
            content_type = upstream.headers.get("content-type", "").split(";")[0]
            if upstream.status_code != 200 or not content_type.startswith("image/") or "svg" in content_type:
                return JSONResponse({"error": "Not found."}, status_code=404)
            try:  # best effort: a failed write only means asking Timeline again next time
                cached.parent.mkdir(parents=True, exist_ok=True)
                temp = cached.with_name(f".{name}.{os.getpid()}.tmp")
                temp.write_bytes(upstream.content)
                temp.replace(cached)
            except OSError:
                pass
            return Response(upstream.content, media_type=content_type, headers=IMMUTABLE)
        return proxy

    def api_release(request: Request):
        return JSONResponse({"report": release["report"]})

    def api_release_progress(request: Request):
        return JSONResponse({"step": release["step"]})

    def api_release_refresh(request: Request):
        # Runs git fetch, so it is a POST with the same header guard as snapshots.
        if request.headers.get("x-timeline-desk") != "1":
            return JSONResponse({"error": "Missing X-Timeline-Desk header."}, status_code=403)

        def build():
            with release["lock"]:
                release["step"] = "Collecting your tickets (planner, watchlist, workflows)"
                extra_ids = guard_candidates()
                release["report"] = release_builder(
                    repo_dir=repo_dir,
                    pos_dir=pos_dir,
                    status_lookup=lambda ids: panteon_client.get_task_statuses(ids, headers()),
                    extra_ids=extra_ids,
                    project_ok=guard_project_ok(),
                    only_mine=True,
                    progress=lambda text: release.update(step=text),
                )
                release["step"] = None
            return {"report": release["report"]}
        return _respond(build)

    def api_coverage(request: Request):
        return JSONResponse({"report": coverage.report, "step": coverage.step})

    def api_coverage_refresh(request: Request):
        # Runs git fetch in both checkouts, so it is a POST with the header guard.
        if request.headers.get("x-timeline-desk") != "1":
            return JSONResponse({"error": "Missing X-Timeline-Desk header."}, status_code=403)
        return _respond(lambda: {"report": rebuild_coverage()})

    def rebuild_coverage() -> Dict[str, Any]:
        return run_coverage(coverage, coverage_builder, work_config(), base_dir, repo_dir, pos_dir,
                            lambda ids: panteon_client.get_task_statuses(ids, headers()),
                            lambda: task_events.my_subscribed_issues(headers(), jira_projects(), resolved_user()))

    def guard_candidates() -> List[str]:
        """The user's own tickets, whose stage should match their PRs: planner, watchlist (watched), active workflows."""
        ids: List[str] = []
        try:
            overview = panteon_client.planner_overview(
                planners.get(lambda: panteon_client.fetch_all_planners(headers())), resolved_user())
            ids += [str(t["issue_number"]) for period in overview["periods"].values() for t in period["tasks"]
                    if t.get("issue_number")]
        except Exception:  # no planner (auth, network): the watchlist and workflows still count
            pass
        ids += [str(w["issue_number"]) for w in task_events.list_watchlist(base_dir, active_only=True)]
        ids += [str(w["issue"]) for w in workflow.list_workflows(base_dir, None) if w["state"] == "active"]
        return list(dict.fromkeys(ids))[:MAX_GUARD_IDS]

    def guard_project_ok() -> Callable[[Any], bool]:
        """Only duell-admin tickets have PRs here; the repo's Timeline project id is in timeline-desk.json."""
        name = release_check.REPO.split("/")[1]
        wanted = {str(repo["id"]) for repo in work_config()["repos"].values() if repo.get("id") and repo["name"] == name}
        return lambda project_id: str(project_id) in wanted

    def api_pr_guard(request: Request):
        report = release["report"]
        return JSONResponse({"guard": report.get("guard") if report else None,
                             "generated_at": report.get("generated_at") if report else None})

    def repo_args(name: Any) -> Dict[str, Any]:
        """Which checkout a PR endpoint works in: duell-admin (default) or duell-pos."""
        name = name or release_check.ADMIN
        if name not in release_check.REPO_SPECS:
            raise ValueError(f"Unknown repository: {name!r}.")
        return {"repo_dir": pos_dir if name == release_check.POS else repo_dir, "repo_name": name}

    def api_pr_draft(request: Request):
        params = request.query_params

        def draft():
            where = repo_args(params.get("repo"))
            return release_check.draft_pr(where["repo_dir"], params.get("issue"), params.get("base"),
                                          params.get("title") or "", repo_name=where["repo_name"], **pr_kwargs)
        return _respond(draft)

    async def api_pr_describe(request: Request):
        # Runs `claude -p` (your Claude usage), so it is a POST with the header guard like the other actions.
        denied = guarded(request)
        if denied:
            return denied
        body = await json_body(request)

        def describe():
            where = repo_args(body.get("repo"))
            return release_check.ai_pr_body(
                where["repo_dir"], body.get("issue"), body.get("base"), body.get("head"), str(body.get("title") or ""),
                claude=work_config()["claude"], repo_name=where["repo_name"], **pr_kwargs)
        return await run_in_threadpool(_respond, describe)

    async def api_pr_create(request: Request):
        denied = guarded(request)
        if denied:
            return denied
        body = await json_body(request)

        def create():
            where = repo_args(body.get("repo"))
            return release_check.create_pr(
                where["repo_dir"], body.get("issue"), body.get("base"), body.get("head"), body.get("title"), body.get("body"),
                repo_name=where["repo_name"], **pr_kwargs)
        return await run_in_threadpool(_respond, create)

    def guarded(request: Request) -> Optional[JSONResponse]:
        if request.headers.get("x-timeline-desk") != "1":
            return JSONResponse({"error": "Missing X-Timeline-Desk header."}, status_code=403)
        return None

    async def json_body(request: Request) -> Dict[str, Any]:
        try:
            body = await request.json()
        except ValueError:
            body = None
        return body if isinstance(body, dict) else {}

    def api_project_tasks(request: Request):
        # Only projects listed (with an "id") in timeline-desk.json can be read.
        def load():
            try:
                wanted = int(request.path_params["project"])
            except ValueError:
                raise LookupError("Unknown project.")
            projects = {p["id"]: p for p in work_session.public_config(work_config())["projects"]}
            if wanted not in projects:
                raise LookupError("Unknown project.")
            return panteon_client.fetch_project_tasks(
                wanted, headers(), resolved_user(), code=projects[wanted]["code"],
                tab=request.query_params.get("tab") or None, support=request.query_params.get("support") == "1")
        return _respond(load)

    def api_about(request: Request):
        return _respond(lambda: {
            "schedule": SCHEDULE,
            "socket": timeline_socket.STATUS,
            "planner_cache_minutes": PLANNER_CACHE_SECONDS / 60,
            "request_gap_ms": int(os.getenv("PANTEON_MIN_REQUEST_GAP_MS") or 200),
            "database": panteon_client.planner_db_path(base_dir),
            "last_planner_check": panteon_client.last_planner_check(resolved_user(), base_dir),
            "now": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "workflow_steps": [{k: step.get(k) for k in ("key", "title", "optional", "repeat", "extra", "run", "about",
                                                         "skills", "reads", "writes", "wont", "approve")}
                               for step in [*workflow.FLOW, *workflow.SIDE.values()]],
            "workflow_buttons": [{"key": key, "label": label, "help": text, "needs_note": key in workflow.NEEDS_NOTE}
                                 for key, (label, text) in workflow.ACTION_TEXT.items()],
            "run_statuses": [{"key": key, "label": label} for key, label in workflow.RUN_LABELS.items()],
        })

    def api_work_config(request: Request):
        return _respond(lambda: work_session.public_config(work_config()))

    # ---- task workflows (workflow.py) ----
    def stages_of(issue: str):
        """Fresh stages for one task from Timeline (one taskbyissue call)."""
        row = panteon_client.get_task_statuses([issue], headers())[0]
        if row.get("error"):
            raise LookupError(f"Task #{issue}: {row['error']}.")
        return row

    def api_workflows(request: Request):
        return _respond(lambda: {"workflows": workflow.list_workflows(base_dir, work_config().get("me"))})

    def view_of(issue: str, code: Optional[str] = None) -> Dict[str, Any]:
        config = work_config()
        current = workflow.workflow_view(issue, config, base_dir, code=code)
        if current["workflow"]:
            return current
        # No workflow yet: one taskbyissue call gives Timeline's stages (they pick the
        # suggested first step) and, when the page didn't know it, the task's board
        # (its project id, matched against the repo ids in timeline-desk.json).
        try:
            row = stages_of(issue)
        except (LookupError, PermissionError, requests.RequestException) as exc:
            return workflow.workflow_view(issue, config, base_dir, code=code, timeline_error=str(exc))
        board = code or work_session.board_for_project(config, row.get("project_id"))
        if not board:
            pid = row.get("project_id")
            if pid:
                current["start_note"] = (f"No single repo in timeline-desk.json has Timeline project id {pid}, so the desk "
                                         f"can't tell this task's board. Give its repo \"id\": {pid}, or open the task "
                                         "from your planner or task list.")
            return current
        return workflow.workflow_view(issue, config, base_dir, code=board, stages=row.get("stages"))

    def api_workflow(request: Request):
        return _respond(lambda: view_of(request.path_params["issue"], request.query_params.get("code") or None))

    async def api_workflow_start(request: Request):
        denied = guarded(request)
        if denied:
            return denied
        body = await json_body(request)
        issue = request.path_params["issue"]

        def start():
            row = stages_of(workflow._issue(issue))
            result = workflow.start_workflow(issue, body.get("code"), str(body.get("step") or ""), row.get("title") or "",
                                             work_config(), base_dir, **git_kwargs)
            # Copy the task's Timeline history now (the heartbeat keeps it current), and
            # count it as seen: only what happens from here on is news.
            note = result.get("note")
            try:
                task_events.sync_watchlist(headers(), base_dir, [issue])
            except Exception as exc:  # the workflow exists either way
                note = f"{note + ' ' if note else ''}Couldn't read the task's Timeline history yet: {exc}"
            workflow.reset_baseline(issue, base_dir)
            return {**view_of(issue), "note": note}
        return await run_in_threadpool(_respond, start)

    async def api_workflow_run(request: Request):
        denied = guarded(request)
        if denied:
            return denied
        body = await json_body(request)
        issue = request.path_params["issue"]

        def run():
            stages = stages_of(workflow._issue(issue)).get("stages")
            result = workflow.run_step(issue, work_config(), base_dir, stages, side=body.get("side") or None,
                                       finding_id=body.get("finding_id"), **start_kwargs)
            return {**view_of(issue), "note": result.get("note"), "started": result}
        return await run_in_threadpool(_respond, run)

    async def api_workflow_resume(request: Request):
        denied = guarded(request)
        if denied:
            return denied
        body = await json_body(request)
        issue = request.path_params["issue"]

        def resume():
            launch = {"launcher": work_launcher} if work_launcher else {}
            workflow.resume_run(issue, body.get("run_id"), work_config(), base_dir, **launch)
            return view_of(issue)
        return await run_in_threadpool(_respond, resume)

    async def api_workflow_decide(request: Request):
        denied = guarded(request)
        if denied:
            return denied
        body = await json_body(request)
        issue = request.path_params["issue"]

        def decide():
            workflow.decide(issue, str(body.get("kind") or ""), base_dir, work_config(),
                            run_id=body.get("run_id"), note=str(body.get("note") or ""))
            return view_of(issue)
        return await run_in_threadpool(_respond, decide)

    async def api_workflow_reset(request: Request):
        denied = guarded(request)
        if denied:
            return denied
        body = await json_body(request)
        issue = request.path_params["issue"]

        def reset():
            removed = workflow.reset_workflow(issue, body.get("confirm"), base_dir, work_config())
            return {**view_of(issue, body.get("code") or None), "removed": removed}
        return await run_in_threadpool(_respond, reset)

    async def api_finding_add(request: Request):
        denied = guarded(request)
        if denied:
            return denied
        body = await json_body(request)
        issue = request.path_params["issue"]

        def add():
            workflow.add_finding(issue, str(body.get("label") or ""), base_dir, work_config(), event_id=body.get("event_id"))
            return view_of(issue)
        return await run_in_threadpool(_respond, add)

    async def api_finding_classify(request: Request):
        denied = guarded(request)
        if denied:
            return denied
        body = await json_body(request)
        issue = request.path_params["issue"]

        def classify():
            workflow.classify_finding(issue, request.path_params["finding"], str(body.get("status") or ""), base_dir,
                                      work_config(), note=str(body.get("note") or ""))
            return view_of(issue)
        return await run_in_threadpool(_respond, classify)

    def api_workflow_doc(request: Request):
        def read():
            issue = workflow._issue(request.path_params["issue"])
            code = request.query_params.get("code") or None
            current = workflow.workflow_view(issue, work_config(), base_dir, code=code)
            repo = work_config()["repos"].get(str(current["workflow"]["code"] if current["workflow"] else code))
            if not repo:
                raise LookupError("No repo for this task's board.")
            return workflow.read_doc(repo, issue, request.path_params["name"])
        return _respond(read)

    def api_workflow_transcript(request: Request):
        return _respond(lambda: workflow.read_transcript(request.path_params["issue"], int(request.path_params["run_id"]),
                                                          base_dir, claude_home))

    def api_work_list(request: Request):
        return _respond(lambda: {"worktrees": work_session.list_worktrees(work_config(), **git_kwargs)})

    async def api_work_remove(request: Request):
        denied = guarded(request)
        if denied:
            return denied
        body = await json_body(request)

        def remove():
            path = body.get("path")
            if not isinstance(path, str) or not path:
                raise ValueError("Which worktree? Pass its path.")
            return work_session.remove_worktree(work_config(), path, bool(body.get("delete_branch")), **git_kwargs)
        return await run_in_threadpool(_respond, remove)

    def api_events(request: Request):
        params = request.query_params
        split = lambda key: [v for v in params.get(key, "").replace(",", " ").split() if v]
        return _respond(lambda: {
            **task_events.list_events(base_dir, split("issues"), split("kinds"),
                                      int(params.get("limit") or 200), params.get("before") or None),
            "watchlist": task_events.list_watchlist(base_dir),
        })

    def api_events_marker(request: Request):
        return _respond(lambda: task_events.events_marker(base_dir))

    def api_events_sync(request: Request):
        denied = guarded(request)
        if denied:
            return denied
        return _respond(lambda: task_events.sync_watchlist(headers(), base_dir))

    async def api_watch(request: Request):
        denied = guarded(request)
        if denied:
            return denied
        body = await json_body(request)

        def watch():
            issue = str(body.get("issue") or "").strip()
            if not issue.isdigit():
                raise ValueError("Issue ID must be a number.")
            # Any task that exists in Timeline can be watched (one taskbyissue call for its title and project).
            row = task_lookup([issue])[issue]
            task_events.watch_task(issue, row["code"], row["title"], base_dir)
            return {"sync": task_events.sync_watchlist(headers(), base_dir, [issue]),
                    "watchlist": task_events.list_watchlist(base_dir)}
        return await run_in_threadpool(_respond, watch)

    def api_unwatch(request: Request):
        denied = guarded(request)
        if denied:
            return denied

        def unwatch():
            issue = request.path_params["issue"]
            events = next((w["events"] for w in task_events.list_watchlist(base_dir) if w["issue_number"] == issue), 0)
            if not task_events.unwatch_task(issue, base_dir):
                raise LookupError("That task isn't being watched.")
            return {"deleted_events": events, "watchlist": task_events.list_watchlist(base_dir)}
        return _respond(unwatch)

    # ---- Jira board (jira_board.py) ----
    def api_jira_board(request: Request):
        def load():
            try:  # older cards have no list code yet; a Timeline hiccup must not hide the board
                jira_board.fill_missing_codes(headers(), work_session.public_config(work_config())["projects"],
                                              resolved_user(), base_dir)
            except (PermissionError, LookupError, ValueError, requests.RequestException):
                pass
            data = jira_board.board(base_dir)
            closed = closed_issues([c["issue_number"] for c in data["cards"]])
            for card in data["cards"]:
                card["closed"] = card["issue_number"] in closed
            return data
        return _respond(load)

    closed_cache: Dict[str, Any] = {}  # issue -> (checked_at, is_closed); a closed task rarely reopens

    def closed_issues(issues):
        """The issues Timeline reports as closed (task_status), one taskbyissue call each, remembered for 10 minutes.
        Best effort: a Timeline failure just means no closed pill."""
        now = time.time()
        stale = [i for i in issues if i not in closed_cache or now - closed_cache[i][0] > 600]
        if stale:
            try:
                for row in panteon_client.get_task_statuses(stale, headers()):
                    if not row.get("error"):
                        closed_cache[str(row["issue_id"])] = (now, row.get("task_status") == "closed")
            except (PermissionError, LookupError, ValueError, requests.RequestException):
                pass
        return {i for i in issues if closed_cache.get(i, (0, False))[1]}

    def jira_projects():
        return work_session.public_config(work_config())["projects"]

    def task_lookup(issues):
        """{issue: {"title", "code"}} for tasks that exist in Timeline and are not closed (one taskbyissue call
        each); `code` is the list code of its project when that project is set up in timeline-desk.json, else None."""
        codes = {str(p["id"]): p["code"] for p in jira_projects()}
        found = {}
        for row in panteon_client.get_task_statuses(list(issues), headers()):
            if row.get("error"):
                raise LookupError(f"Task #{row['issue_id']}: {row['error']}.")
            if row.get("task_status") == "closed":  # the one status that rules a task out
                raise ValueError(f"Task #{row['issue_id']} is closed.")
            found[str(row["issue_id"])] = {"title": row.get("title") or "", "code": codes.get(str(row.get("project_id")))}
        return found

    def api_jira_my_tasks(request: Request):
        return _respond(lambda: {"tasks": jira_board.list_my_tasks(headers(), jira_projects(), resolved_user())})

    def api_jira_support_tasks(request: Request):
        return _respond(lambda: {"tasks": jira_board.list_support_tasks(headers(), jira_projects(), resolved_user())})

    async def api_jira_add(request: Request):
        denied = guarded(request)
        if denied:
            return denied
        body = await json_body(request)

        def add():
            raw = body.get("issues") if isinstance(body.get("issues"), list) else [body.get("issue")]
            issues = []
            for value in raw:
                value = str(value or "").strip()
                if not value.isdigit():
                    raise ValueError("Issue ID must be a number.")
                if value not in issues:
                    issues.append(value)
            if not issues:
                raise ValueError("Pick at least one task.")
            if body.get("status") not in jira_board.STATUSES:
                raise ValueError(f"Unknown status {body.get('status')!r}.")
            on_board = {c["issue_number"] for c in jira_board.list_cards(base_dir)}
            if on_board.intersection(issues):
                raise ValueError("Already on the board: " + ", ".join(f"#{i}" for i in issues if i in on_board) + ".")
            # Any task that exists in Timeline can be added. Titles come from Timeline; the support list only
            # decides which cards carry the "support" tag, so a failed support request is not an error.
            try:
                support = {t["issue_number"] for t in jira_board.list_support_tasks(headers(), jira_projects(), resolved_user())}
            except (LookupError, requests.RequestException):
                support = set()
            found = task_lookup(issues)
            tasks = [{"issue_number": i, "title": found[i]["title"], "code": found[i]["code"], "support": i in support}
                     for i in issues]
            cards = jira_board.add_cards(tasks, body.get("status"), base_dir)
            return {"card": cards[0], "cards": cards}
        return await run_in_threadpool(_respond, add)

    async def api_jira_move(request: Request):
        denied = guarded(request)
        if denied:
            return denied
        body = await json_body(request)
        return _respond(lambda: {"card": jira_board.move_card(request.path_params["issue"], body.get("status"), base_dir)})

    def api_jira_remove(request: Request):
        denied = guarded(request)
        if denied:
            return denied
        return _respond(lambda: {"card": jira_board.remove_card(request.path_params["issue"], base_dir)})

    routes = [
        Route("/", index),
        Route("/api/jira/board", api_jira_board),
        Route("/api/jira/my-tasks", api_jira_my_tasks),
        Route("/api/jira/support-tasks", api_jira_support_tasks),
        Route("/api/jira/cards", api_jira_add, methods=["POST"]),
        Route("/api/jira/cards/{issue}/move", api_jira_move, methods=["POST"]),
        Route("/api/jira/cards/{issue}", api_jira_remove, methods=["DELETE"]),
        Route("/api/events", api_events),
        Route("/api/events/marker", api_events_marker),
        Route("/api/events/sync", api_events_sync, methods=["POST"]),
        Route("/api/watchlist", lambda request: _respond(lambda: {"watchlist": task_events.list_watchlist(base_dir)})),
        Route("/api/watchlist", api_watch, methods=["POST"]),
        Route("/api/watchlist/{issue}", api_unwatch, methods=["DELETE"]),
        Route("/api/planner", api_planner),
        Route("/api/changes", api_changes),
        Route("/api/snapshot", api_snapshot, methods=["POST"]),
        Route("/api/statuses", api_statuses),
        Route("/api/task/{issue}", api_task),
        Route("/api/photo/{name}", storage_proxy("users")),
        Route("/api/attachment/{name}", storage_proxy("attachments")),
        Route("/api/release", api_release),
        Route("/api/release", api_release_refresh, methods=["POST"]),
        Route("/api/release/progress", api_release_progress),
        Route("/api/coverage", api_coverage),
        Route("/api/coverage", api_coverage_refresh, methods=["POST"]),
        Route("/api/pr-guard", api_pr_guard),
        Route("/api/pr/draft", api_pr_draft),
        Route("/api/pr/describe", api_pr_describe, methods=["POST"]),
        Route("/api/pr/create", api_pr_create, methods=["POST"]),
        Route("/api/project/{project}/tasks", api_project_tasks),
        Route("/api/about", api_about),
        Route("/api/work/config", api_work_config),
        Route("/api/workflows", api_workflows),
        Route("/api/workflow/{issue}", api_workflow),
        Route("/api/workflow/{issue}/start", api_workflow_start, methods=["POST"]),
        Route("/api/workflow/{issue}/run", api_workflow_run, methods=["POST"]),
        Route("/api/workflow/{issue}/resume", api_workflow_resume, methods=["POST"]),
        Route("/api/workflow/{issue}/decide", api_workflow_decide, methods=["POST"]),
        Route("/api/workflow/{issue}/reset", api_workflow_reset, methods=["POST"]),
        Route("/api/workflow/{issue}/findings", api_finding_add, methods=["POST"]),
        Route("/api/workflow/{issue}/findings/{finding:int}", api_finding_classify, methods=["POST"]),
        Route("/api/workflow/{issue}/doc/{name}", api_workflow_doc),
        Route("/api/workflow/{issue}/runs/{run_id:int}/transcript", api_workflow_transcript),
        Route("/api/work/worktrees", api_work_list),
        Route("/api/work/remove", api_work_remove, methods=["POST"]),
        Mount("/static", _FreshStaticFiles(directory=WEB_DIR), name="static"),
    ]
    middleware = [
        # Blocks DNS-rebinding: another site can't reach this API through a
        # hostname that resolves to 127.0.0.1.
        Middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS),
    ]
    return Starlette(routes=routes, middleware=middleware)


def run_coverage(cache: CoverageCache, builder: Callable[..., Any], config: Dict[str, Any], base_dir: Optional[str],
                 repo_dir: Optional[str] = None, pos_dir: Optional[str] = None,
                 status_lookup: Optional[Callable[[List[str]], List[Dict[str, Any]]]] = None,
                 subscribed: Optional[Callable[[], Any]] = None) -> Dict[str, Any]:
    """Rebuilds the coverage report into `cache`; one rebuild at a time (a second caller waits, then gets that result)."""
    with cache.lock:
        cache.step = "Starting"
        try:
            cache.report = builder(
                dirs={release_check.ADMIN: repo_dir, release_check.POS: pos_dir},
                authors=config.get("git_authors") or None,
                base_dir=base_dir,
                status_lookup=status_lookup,
                subscribed=subscribed,
                progress=lambda text: setattr(cache, "step", text),
            )
        finally:
            cache.step = None
        # Each ticket's Timeline page: its project's list code is in timeline-desk.json (repos keyed by that code).
        codes = {str(repo["id"]): code for code, repo in (config.get("repos") or {}).items() if repo.get("id")}
        for ticket in (cache.report or {}).get("tickets", []):
            code = codes.get(str((ticket.get("timeline") or {}).get("project_id")))
            if code:
                ticket["url"] = panteon_client.task_url(ticket["issue"], code)
        return cache.report


def run_coverage_scheduler(rebuild: Callable[[], Any], stop: threading.Event, active_hours: Optional[tuple] = None,
                           minutes: float = COVERAGE_MINUTES, off_hours_minutes: float = COVERAGE_OFF_HOURS_MINUTES,
                           now: Callable[[], datetime] = datetime.now, jitter: float = JITTER) -> None:
    """Rebuilds the coverage report at startup and then hourly (every few hours outside the active hours), because a
    commit can only be seen dropping from qa-release if a check ran while it was still there. Failures are logged."""
    while True:
        try:
            rebuild()
        except Exception as exc:  # keep the loop alive whatever fails
            print(f"Release coverage check failed: {exc}", flush=True)
        wait = (minutes if is_active(now(), active_hours) else off_hours_minutes) * 60
        if stop.wait(wait * (1 + random.uniform(-jitter, jitter))):
            return


def parse_active_hours(text: Optional[str]) -> Optional[tuple]:
    """"7-19" -> (7, 19); "all"/"" -> None (always active)."""
    text = (text or "").strip().lower()
    if text in ("", "all", "always"):
        return None
    match = re.fullmatch(r"(\d{1,2})\s*-\s*(\d{1,2})", text)
    if not match or not (0 <= int(match[1]) < int(match[2]) <= 24):
        raise ValueError(f"Active hours must look like 7-19 or 'all', not {text!r}.")
    return int(match[1]), int(match[2])


def is_active(moment: datetime, hours: Optional[tuple]) -> bool:
    return hours is None or (moment.weekday() < 5 and hours[0] <= moment.hour < hours[1])


def run_snapshot_scheduler(
    take: Callable[..., Any],
    stop: threading.Event,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_MINUTES * 60,
    full_seconds: float = DEFAULT_SNAPSHOT_MINUTES * 60,
    stage_seconds: float = STAGE_REFRESH_MINUTES * 60,
    off_hours_seconds: float = OFF_HOURS_MINUTES * 60,
    active_hours: Optional[tuple] = None,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = datetime.now,
    jitter: float = JITTER,
    wake: Optional[threading.Event] = None,
    wake_gap_seconds: float = WAKE_GAP_SECONDS,
    wake_debounce_seconds: float = WAKE_DEBOUNCE_SECONDS,
) -> None:
    """Calls `take(full=…, stages=…)` at startup and then every heartbeat (hourly
    outside the active hours) until `stop` is set; each call says whether a full
    planner check and stage lookups are due. `take` may return {"full", "stages"}
    for what it actually did (a heartbeat that saw a change upgrades itself).
    A failed attempt (offline, expired token) is logged and the loop carries on.
    Waits are jittered so the calls never land on a fixed beat. `wake` (set by the socket
    listener when Timeline pushes an event) ends the wait early during the active hours:
    a burst is coalesced for `wake_debounce_seconds` and runs are at least `wake_gap_seconds`
    apart; outside the active hours it is ignored."""
    last_full = last_stages = None
    while True:
        active = is_active(now(), active_hours)
        t = clock()
        full = not active or last_full is None or t - last_full >= full_seconds
        stages = active and (last_stages is None or t - last_stages >= stage_seconds)
        try:
            done = take(full=full, stages=stages) or {"full": full, "stages": stages}
            if done.get("full"):
                last_full = t
            if done.get("stages"):
                last_stages = t
        except Exception as exc:  # keep the scheduler alive whatever fails
            print(f"Planner check failed: {exc}", flush=True)
        wait = (heartbeat_seconds if active else off_hours_seconds) * (1 + random.uniform(-jitter, jitter))
        if wake is None:
            if stop.wait(wait):
                return
            continue
        end = time.monotonic() + wait
        while (left := end - time.monotonic()) > 0:
            if stop.is_set():
                return
            if not wake.wait(min(left, 1.0)):  # short slices: `stop` can't wake this wait itself
                continue
            wake.clear()
            if not active:
                continue
            if stop.wait(wake_debounce_seconds):
                return
            wake.clear()
            gap = wake_gap_seconds - (clock() - t)
            if gap > 0 and stop.wait(gap):
                return
            break
        if stop.is_set():
            return


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765, help="Port on 127.0.0.1 (default 8765).")
    parser.add_argument("--user-id", default=None, help="Planner user id; defaults to PANTEON_USER_ID, then the bearer token's `sub`.")
    parser.add_argument("--repo-dir", default=None, help="duell-admin checkout for the Release view; defaults to DUELL_ADMIN_DIR, then ../duell-admin.")
    parser.add_argument("--pos-dir", default=None, help="duell-pos checkout for the PR guard; defaults to DUELL_POS_DIR, then ../duell-pos.")
    parser.add_argument("--base-dir", default=None, help="Where database/timeline_task.db (the planner snapshots) lives; defaults to PANTEON_TIMELINE_DIR or the current directory.")
    parser.add_argument("--interval", type=float, default=float(os.getenv("TIMELINE_SNAPSHOT_MINUTES") or DEFAULT_SNAPSHOT_MINUTES),
                        help=f"Most minutes between full planner checks (default {DEFAULT_SNAPSHOT_MINUTES}, or TIMELINE_SNAPSHOT_MINUTES).")
    parser.add_argument("--heartbeat", type=float, default=float(os.getenv("TIMELINE_HEARTBEAT_MINUTES") or DEFAULT_HEARTBEAT_MINUTES),
                        help=f"Minutes between cheap change checks (default {DEFAULT_HEARTBEAT_MINUTES}, or TIMELINE_HEARTBEAT_MINUTES).")
    parser.add_argument("--active-hours", default=os.getenv("TIMELINE_ACTIVE_HOURS") or DEFAULT_ACTIVE_HOURS,
                        help="Weekday hours to poll at the heartbeat, e.g. 7-19, or 'all' (default 7-19, or TIMELINE_ACTIVE_HOURS).")
    parser.add_argument("--no-socket", action="store_true",
                        help="Don't listen on Timeline's live socket; rely on polling alone (or TIMELINE_SOCKET=0).")
    args = parser.parse_args(argv)

    import uvicorn

    print(f"Timeline desk: http://{HOSTNAME}:{args.port}  (or http://127.0.0.1:{args.port})", flush=True)
    last_now = {"fingerprint": None}

    planner_cache = PlannerCache()

    def take_snapshot(full: bool, stages: bool) -> Dict[str, bool]:
        headers = panteon_client.build_api_headers()
        user = panteon_client.resolve_user_id(args.user_id)
        known = {}
        if not full:  # heartbeat: one request decides whether the rest is worth making
            known["now"] = panteon_client.get_priority_tasks(headers, "now")
            planner_cache.update("now", known["now"])
            if panteon_client.planner_fingerprint(known["now"], user) != last_now["fingerprint"]:
                full = stages = True
        if full:
            planners = panteon_client.fetch_all_planners(headers, known)
            result = panteon_client.fetch_my_planner_changes(
                headers, user_id=user, base_dir=args.base_dir, save=True, with_stages=stages, planners=planners)
            planner_cache.put(planners)
            last_now["fingerprint"] = panteon_client.planner_fingerprint(planners["now"], user)
            print(f"{result['snapshot']['taken_at']} planner checked: {len(result['snapshot']['tasks'])} tasks"
                  + (" (with stages)" if stages else ""), flush=True)
        try:
            synced = task_events.sync_watchlist(headers, args.base_dir, refresh_meta=full)
            if synced["new_events"] or synced["errors"]:
                print(f"watchlist synced: {synced['tasks']} tasks, {synced['new_events']} new events"
                      + (f", errors: {synced['errors']}" if synced["errors"] else ""), flush=True)
        except Exception as exc:  # the planner check above already succeeded
            print(f"Watchlist sync failed: {exc}", flush=True)
        return {"full": full, "stages": stages}

    SCHEDULE.update(heartbeat_minutes=max(args.heartbeat, 1), full_minutes=max(args.interval, args.heartbeat, 1),
                    active_hours=args.active_hours)
    stop = threading.Event()
    wake = threading.Event()
    if not args.no_socket and timeline_socket.socket_enabled():
        threading.Thread(target=timeline_socket.run_socket_listener, args=(wake, stop), daemon=True, kwargs={
            "events": timeline_socket.parse_event_filter(os.getenv("TIMELINE_SOCKET_EVENTS")),
            "log_payloads": os.getenv("TIMELINE_SOCKET_LOG_PAYLOADS") == "1",
        }).start()
    threading.Thread(target=run_snapshot_scheduler, args=(take_snapshot, stop), daemon=True, kwargs={
        "wake": wake,
        "heartbeat_seconds": max(args.heartbeat, 1) * 60,
        "full_seconds": max(args.interval, args.heartbeat, 1) * 60,
        "active_hours": parse_active_hours(args.active_hours),
    }).start()
    coverage_cache = CoverageCache()
    threading.Thread(target=run_coverage_scheduler, daemon=True, args=(
        lambda: run_coverage(coverage_cache, release_coverage.build_coverage, work_session.load_config(), args.base_dir,
                             args.repo_dir, args.pos_dir,
                             lambda ids: panteon_client.get_task_statuses(ids, panteon_client.build_api_headers()),
                             lambda: task_events.my_subscribed_issues(
                                 panteon_client.build_api_headers(),
                                 work_session.public_config(work_session.load_config())["projects"],
                                 panteon_client.resolve_user_id(args.user_id))),
        stop, parse_active_hours(args.active_hours))).start()
    try:
        uvicorn.run(create_app(args.user_id, args.base_dir, args.repo_dir, planner_cache=planner_cache, pos_dir=args.pos_dir,
                               coverage_cache=coverage_cache), host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        stop.set()
        wake.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
