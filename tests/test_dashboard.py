"""Dashboard data helpers and HTTP layer. No network: the client is monkeypatched."""
import json
from datetime import date, datetime

import pytest
import requests
from starlette.testclient import TestClient

import dashboard
import panteon_client as pc


def _planners(tasks_by_period, user_id=135):
    return {p: [{"user_id": user_id, "data": tasks_by_period.get(p, [])}] for p in pc.PLANNER_PERIODS}


def _task(issue, order=1, **extra):
    return {"issue_number": issue, "code": "15033", "title": f"Task {issue}", "priority_sort_order": order,
            "stage_status": "QA in progress", "task_status": "open", "deadline": None, "support": 0, **extra}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("PANTEON_BEARER_TOKEN", "tok")
    app = dashboard.create_app(user_id=135, base_dir=str(tmp_path))
    return TestClient(app, base_url="http://127.0.0.1:8765")


class TestPlannerOverview:
    def test_shape_flags_and_urls(self):
        planners = _planners({"now": [_task(2, 2, deadline="2026-10-01", support=1), _task(1, 1)]})
        overview = pc.planner_overview(planners, 135, today=date(2026, 10, 16))
        now = overview["periods"]["now"]
        assert overview["today"] == "2026-10-16"
        assert now["label"] == "Now"
        assert [t["issue_number"] for t in now["tasks"]] == [1, 2]
        assert now["tasks"][1]["deadline_flag"] == "overdue" and now["tasks"][1]["support"] is True
        assert now["tasks"][0]["url"] == f"{pc.BASE_URL}/tasks#/list/15033/edit/1"
        assert now["tasks"][0]["code"] == "15033"
        assert overview["periods"]["commingup"]["tasks"] == []

    def test_absent_user_raises(self):
        with pytest.raises(LookupError):
            pc.planner_overview({p: [] for p in pc.PLANNER_PERIODS}, 135)

    def test_task_url_needs_both_parts(self):
        assert pc.task_url(5, None) is None and pc.task_url(None, "1") is None


class TestPlannerHistory:
    T1 = {"issue_number": 1, "code": "9", "title": "A", "period": "now", "position": 1,
          "stage_status": "QA", "task_status": "open", "deadline": None}

    def _save(self, tmp_path, taken_at, tasks, user=135):
        snap = {"user_id": user, "date": taken_at[:10], "taken_at": taken_at, "tasks": tasks}
        pc.save_planner_snapshot(snap, str(tmp_path))

    def test_history_newest_first_with_urls(self, tmp_path):
        t1, t2 = self.T1, {**self.T1, "issue_number": 2, "title": "B"}
        self._save(tmp_path, "2026-10-14T09:00:00", {"1": t1})
        self._save(tmp_path, "2026-10-14T09:15:00", {"1": t1})  # unchanged: no new row
        self._save(tmp_path, "2026-10-14T09:30:00", {"1": t1, "2": t2})
        self._save(tmp_path, "2026-10-14T09:45:00", {"1": {**t1, "stage_status": "Done"}, "2": t2})
        self._save(tmp_path, "2026-10-13T09:00:00", {}, user=999)
        history = pc.planner_history(135, str(tmp_path))
        assert history["tracking_since"] == "2026-10-14T09:00:00"
        assert history["last_checked"] == "2026-10-14T09:45:00"
        assert [c["taken_at"] for c in history["changes"]] == ["2026-10-14T09:45:00", "2026-10-14T09:30:00"]
        assert history["changes"][1]["previous_taken_at"] == "2026-10-14T09:00:00"
        added = history["changes"][1]["diff"]["added"]
        assert added[0]["issue_number"] == 2 and added[0]["url"].endswith("/list/9/edit/2")
        assert history["changes"][0]["diff"]["changed"][0]["changes"] == [("stage", "QA", "Done")]

    def test_position_only_change_is_not_stored(self, tmp_path):
        self._save(tmp_path, "2026-10-14T09:00:00", {"1": self.T1})
        self._save(tmp_path, "2026-10-14T09:15:00", {"1": {**self.T1, "position": 3}})
        assert len(pc.list_planner_snapshots(135, str(tmp_path))) == 1

    def test_on_planner_since_and_carried_stages(self, tmp_path):
        staged = {"stages": {"QA": "in progress"}, "release_state": "on qa", "qa_approved": False}
        self._save(tmp_path, "2026-10-14T09:00:00", {"1": {**self.T1, **staged}})
        # Taken without stage lookups (MCP tool): inherits the stored stages.
        self._save(tmp_path, "2026-10-15T09:00:00", {"1": {**self.T1, "deadline": "2026-10-30"}})
        assert pc.list_planner_snapshots(135, str(tmp_path))[1]["tasks"]["1"]["stages"] == staged["stages"]
        # Later check with stages sees the real change.
        self._save(tmp_path, "2026-10-16T09:00:00", {"1": {**self.T1, "deadline": "2026-10-30",
                   **{**staged, "stages": {"QA": "done"}}}})
        history = pc.planner_history(135, str(tmp_path))
        newest = history["changes"][0]["diff"]["changed"][0]
        assert newest["changes"] == [("QA", "in progress", "done")]
        assert newest["on_planner_since"] == "2026-10-14T09:00:00" and newest["since_tracking_start"] is True

    def test_stage_details_arriving_are_recorded_once(self, tmp_path):
        self._save(tmp_path, "2026-10-14T09:00:00", {"1": self.T1})
        staged = {"1": {**self.T1, "stages": {"QA": "in progress"}, "release_state": "on qa", "qa_approved": False}}
        self._save(tmp_path, "2026-10-14T09:30:00", staged)
        self._save(tmp_path, "2026-10-14T10:00:00", staged)
        assert len(pc.list_planner_snapshots(135, str(tmp_path))) == 2
        assert pc.planner_history(135, str(tmp_path))["changes"] == []  # a baseline, not a change

    def test_empty_history(self, tmp_path):
        assert pc.planner_history(135, str(tmp_path)) == {
            "user_id": 135, "tracking_since": None, "last_checked": None, "changes": []}
        assert not (tmp_path / "timeline").exists()

    def test_imports_earlier_json_snapshots_once(self, tmp_path):
        directory = tmp_path / "timeline" / "planner"
        directory.mkdir(parents=True)
        (directory / "2026-10-13.json").write_text(json.dumps(
            {"user_id": 135, "date": "2026-10-13", "tasks": {"1": self.T1}}))
        (directory / "notes.json").write_text("{}")
        self._save(tmp_path, "2026-10-14T09:00:00", {})
        snaps = pc.list_planner_snapshots(135, str(tmp_path))
        assert [s["taken_at"] for s in snaps] == ["2026-10-13T00:00:00", "2026-10-14T09:00:00"]
        assert [c["diff"]["removed"][0]["issue_number"] for c in pc.planner_history(135, str(tmp_path))["changes"]] == [1]


class TestPlannerCache:
    def test_serves_from_memory_until_expired_or_forced(self):
        clock, calls = [0.0], []
        cache = dashboard.PlannerCache(ttl=300, clock=lambda: clock[0])
        fetch = lambda: calls.append(1) or {"now": len(calls)}
        assert cache.get(fetch) == {"now": 1} and cache.get(fetch) == {"now": 1}
        clock[0] = 299
        assert cache.get(fetch) == {"now": 1}
        clock[0] = 301
        assert cache.get(fetch) == {"now": 2}
        assert cache.get(fetch, force=True) == {"now": 3}

    def test_update_swaps_one_period_without_refreshing_the_age(self):
        clock = [0.0]
        cache = dashboard.PlannerCache(ttl=300, clock=lambda: clock[0])
        cache.put({"now": "a", "next": "b"})
        clock[0] = 250
        cache.update("now", "c")
        assert cache.data == {"now": "c", "next": "b"}
        clock[0] = 301
        assert cache.get(lambda: {"fresh": True}) == {"fresh": True}


class TestPlannerEndpointCaching:
    def test_page_loads_do_not_call_timeline_again(self, client, monkeypatch):
        calls, planners = [], _planners({"now": [_task(1, 1)]})
        monkeypatch.setattr(pc, "get_priority_tasks", lambda headers, period: calls.append(period) or planners[period])
        for _ in range(3):
            assert client.get("/api/planner").status_code == 200
        assert len(calls) == 3                      # one fetch of the three periods, not three
        client.get("/api/planner?refresh=1")
        assert len(calls) == 6                      # Refresh forces a new one


class TestDashboardHttp:
    def test_index_and_static(self, client):
        page = client.get("/").text
        assert "<title>Timeline Desk · Panteon</title>" in page and "/static/favicon.svg" in page
        assert client.get("/static/favicon.svg").headers["content-type"].startswith("image/svg")
        assert client.get("/static/app.js").status_code == 200

    def test_accepts_timeline_localhost(self, tmp_path):
        app = dashboard.create_app(user_id=135, base_dir=str(tmp_path))
        named = TestClient(app, base_url="http://timeline.localhost:8765")
        assert named.get("/").status_code == 200

    def test_rejects_foreign_host(self, monkeypatch, tmp_path):
        app = dashboard.create_app(user_id=135, base_dir=str(tmp_path))
        evil = TestClient(app, base_url="http://evil.example:8765")
        assert evil.get("/api/changes").status_code == 400

    def test_planner(self, client, monkeypatch):
        monkeypatch.setattr(pc, "get_priority_tasks",
                            lambda headers, period="now": _planners({"now": [_task(7)]})[period])
        body = client.get("/api/planner").json()
        assert body["user_id"] == 135
        assert body["periods"]["now"]["tasks"][0]["issue_number"] == 7

    def test_missing_credentials_is_401(self, client, monkeypatch):
        monkeypatch.delenv("PANTEON_BEARER_TOKEN", raising=False)
        monkeypatch.delenv("PANTEON_COOKIE", raising=False)
        response = client.get("/api/planner")
        assert response.status_code == 401 and "error" in response.json()

    def test_upstream_error_is_502(self, client, monkeypatch):
        def boom(headers, period="now"):
            raise requests.HTTPError(response=type("R", (), {"status_code": 500})())
        monkeypatch.setattr(pc, "get_priority_tasks", boom)
        assert client.get("/api/planner").status_code == 502

    def test_statuses(self, client, monkeypatch):
        monkeypatch.setattr(pc, "get_task_by_issue",
                            lambda issue_id, headers, company_id=None: {"data": {"title": "T", "stage_qa": 4}})
        body = client.get("/api/statuses?ids=5,6 5").json()
        assert [s["issue_id"] for s in body["statuses"]] == ["5", "6"]
        assert body["statuses"][0]["release_state"] == "qa approved"

    def test_statuses_validation(self, client):
        assert client.get("/api/statuses").status_code == 400
        too_many = ",".join(str(i) for i in range(dashboard.MAX_STATUS_IDS + 1))
        assert client.get(f"/api/statuses?ids={too_many}").status_code == 400

    def test_about(self, client):
        body = client.get("/api/about").json()
        assert body["schedule"]["heartbeat_minutes"] == dashboard.DEFAULT_HEARTBEAT_MINUTES
        assert body["planner_cache_minutes"] == dashboard.PLANNER_CACHE_SECONDS / 60
        assert body["database"].endswith("timeline_task.db")

    def test_task(self, client, monkeypatch):
        monkeypatch.setattr(pc, "fetch_task_data", lambda ident, headers: {
            "issue_id": ident, "task_id": 1,
            "task_meta": {"title": "Hello", "creator": "Ann", "created_date": "2026-10-01", "status": "open",
                          "tags": ["x"], "description": "Body ![](/img/a.png)"},
            "comments": [{"author": "Ann", "created_at": "01.10.2026 10:00", "description": "Hi", "images": []}],
            "commits": [{"id": 7, "author": "Ann", "created_at": "01.10.2026 11:00", "message": "Fix it", "url": "https://github.com/o/r/commit/abcdef1234"}],
        })
        body = client.get("/api/task/42").json()
        assert body["commits"][0]["message"] == "Fix it"
        assert body["title"] == "Hello" and body["comments"][0]["author"] == "Ann"
        assert body["description"] == "Body ![](/img/a.png)"

    def test_images_proxied_without_credentials(self, client, monkeypatch):
        seen = {}
        def fake_get(url, **kwargs):
            seen.update(url=url, **kwargs)
            return type("R", (), {"status_code": 200, "content": b"JPEG", "headers": {"content-type": "image/jpeg"}})()
        monkeypatch.setattr(requests, "get", fake_get)
        r = client.get("/api/photo/1712642413.jpg")
        assert r.status_code == 200 and r.content == b"JPEG" and r.headers["content-type"] == "image/jpeg"
        assert seen["url"] == f"{pc.BASE_URL}/storage/users/1712642413.jpg"
        assert "headers" not in seen and seen["allow_redirects"] is False
        r = client.get("/api/attachment/image_1790233365.png")
        assert r.status_code == 200 and seen["url"] == f"{pc.BASE_URL}/storage/attachments/image_1790233365.png"

    def test_images_are_fetched_once_then_served_from_disk(self, client, monkeypatch, tmp_path):
        calls = []
        def fake_get(url, **kwargs):
            calls.append(url)
            return type("R", (), {"status_code": 200, "content": b"PNGDATA", "headers": {"content-type": "image/png"}})()
        monkeypatch.setattr(requests, "get", fake_get)
        first = client.get("/api/attachment/image_1.png")
        second = client.get("/api/attachment/image_1.png")
        assert len(calls) == 1 and second.content == b"PNGDATA" and second.headers["content-type"] == "image/png"
        assert "immutable" in second.headers["cache-control"] and "max-age=31536000" in first.headers["cache-control"]
        assert (tmp_path / "database" / "images" / "attachments" / "image_1.png").read_bytes() == b"PNGDATA"

    def test_failed_or_non_image_responses_are_not_stored(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr(requests, "get", lambda *a, **k: type("R", (), {
            "status_code": 200, "content": b"<svg/>", "headers": {"content-type": "image/svg+xml"}})())
        assert client.get("/api/photo/9.jpg").status_code == 404
        assert not (tmp_path / "database" / "images").exists()

    @pytest.mark.parametrize("name", ["..%2Fsecret.jpg", "a.php", "x.jpg%00.png", "a b.jpg"])
    def test_photo_rejects_odd_names(self, client, monkeypatch, name):
        monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("must not fetch"))
        assert client.get(f"/api/photo/{name}").status_code == 404

    def test_unknown_task_is_404(self, client, monkeypatch):
        def missing(ident, headers):
            raise requests.HTTPError(response=type("R", (), {"status_code": 422})())
        monkeypatch.setattr(pc, "fetch_task_data", missing)
        assert client.get("/api/task/999").status_code == 404

    def test_snapshot_requires_header(self, client, tmp_path):
        assert client.post("/api/snapshot").status_code == 403
        assert not (tmp_path / "timeline").exists()

    def test_snapshot_saves(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr(pc, "get_priority_tasks",
                            lambda headers, period="now": _planners({"now": [_task(7)]})[period])
        response = client.post("/api/snapshot", headers={"X-Timeline-Desk": "1"})
        assert response.status_code == 200 and response.json()["tasks"] == 1
        assert (tmp_path / "database" / "timeline_task.db").exists()
        snapshots = pc.list_planner_snapshots(135, str(tmp_path))
        assert len(snapshots) == 1 and list(snapshots[0]["tasks"]) == ["7"]

    def test_changes(self, client):
        assert client.get("/api/changes").json()["changes"] == []


class TestSnapshotScheduler:
    WEEKDAY = datetime(2026, 10, 27, 10, 0)   # Tuesday 10:00
    NIGHT = datetime(2026, 10, 27, 22, 0)
    SATURDAY = datetime(2026, 10, 31, 10, 0)

    def run(self, now, stops_after, **kw):
        import threading
        calls, stop, ticks = [], threading.Event(), [0.0]

        def take(full, stages):
            calls.append((full, stages))
            ticks[0] += 180
            if len(calls) == stops_after:
                stop.set()
            raise RuntimeError("offline")  # errors are swallowed

        dashboard.run_snapshot_scheduler(take, stop, heartbeat_seconds=0, full_seconds=600, stage_seconds=3600,
                                         off_hours_seconds=0, clock=lambda: ticks[0], now=lambda: now, jitter=0, **kw)
        return calls

    def test_full_and_stages_at_startup_then_heartbeats_until_due(self):
        calls = self.run(self.WEEKDAY, 6)
        # a failed attempt doesn't count as done, so every call stays a full one while offline
        assert len(calls) == 6 and all(full for full, _ in calls)

    def test_heartbeat_only_between_full_checks(self):
        import threading
        stop, ticks, calls = threading.Event(), [0.0], []

        def take(full, stages):
            calls.append((full, stages))
            ticks[0] += 180
            if len(calls) == 6:
                stop.set()

        dashboard.run_snapshot_scheduler(take, stop, heartbeat_seconds=0, full_seconds=600, stage_seconds=3600,
                                         clock=lambda: ticks[0], now=lambda: self.WEEKDAY, jitter=0)
        assert calls == [(True, True), (False, False), (False, False), (False, False), (True, False), (False, False)]

    def test_an_upgraded_heartbeat_resets_the_full_timer(self):
        import threading
        stop, ticks, calls = threading.Event(), [0.0], []

        def take(full, stages):
            calls.append((full, stages))
            ticks[0] += 180
            if len(calls) == 5:
                stop.set()
            return {"full": True, "stages": True} if len(calls) == 2 else None

        dashboard.run_snapshot_scheduler(take, stop, heartbeat_seconds=0, full_seconds=600, stage_seconds=3600,
                                         clock=lambda: ticks[0], now=lambda: self.WEEKDAY, jitter=0)
        assert calls == [(True, True), (False, False), (False, False), (False, False), (False, False)]

    def test_outside_active_hours_every_check_is_full_without_stages(self):
        for moment in (self.NIGHT, self.SATURDAY):
            calls = self.run(moment, 2, active_hours=(7, 19))
            assert calls == [(True, False), (True, False)]

    def test_parse_active_hours(self):
        assert dashboard.parse_active_hours("7-19") == (7, 19)
        assert dashboard.parse_active_hours("all") is None and dashboard.parse_active_hours("") is None
        with pytest.raises(ValueError):
            dashboard.parse_active_hours("19-7")


# ---- project task list -----------------------------------------------------
PROJECT_PAYLOAD = {"status": "success", "data": {
    "tabs": [{"tab_name": "DOCS", "tab_task_count": 1, "tab_sort_order": 5},
             {"tab_name": "Priority", "tab_task_count": 2, "tab_sort_order": 1}],
    "grouptasks": [{"name": "In progress", "tasks": [{
        "issue_number": 5916, "title": "Whitelabel", "stage_status": "Testing pending",
        "stage_rsd": {"id": 5, "name": "Completed"}, "stage_est": {"id": 1, "name": "Not needed"},
        "stage_dev": {"id": 4, "name": "In progress"}, "stage_tst": {"id": 0, "name": ""},
        "stage_qa": {"id": 6, "name": "To Discuss"}, "stage_liv": None,
        "deadline": "20.10.2026", "hours_spent_decimal": 3, "estimated_hours": None, "release": None,
        "task_tags": [{"name": "BO", "color": "#596780", "id": 21}],
        "task_subscribers": [{"name": "Ann", "photo": "1.jpg"}]}]}]}}


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


def test_fetch_project_tasks_shapes_and_posts(monkeypatch):
    sent = {}
    def fake_post(url, headers=None, json=None, timeout=None):
        sent.update(url=url, json=json)
        return FakeResponse(200, PROJECT_PAYLOAD)
    monkeypatch.setattr(pc._SESSION, "post", fake_post)
    out = pc.fetch_project_tasks(16, {}, 111, code=15033, tab="DOCS", today=__import__("datetime").date(2026, 10, 27))
    assert sent["url"].endswith("/api/project/16/tasks")
    assert sent["json"] == {"subscribed_by": 111, "tag_id": 0, "status": "open", "tab_name": "DOCS"}
    assert [t["name"] for t in out["tabs"]] == ["Priority", "DOCS"] and out["tab"] == "DOCS"
    task = out["groups"][0]["tasks"][0]
    assert task["stages"] == {"RSD": "done", "Estimate": "skipped", "Development": "in progress",
                              "Testing": "not started", "QA": "in progress", "Live": "not started"}
    assert task["deadline"] == "2026-10-20" and task["deadline_flag"] == "overdue"
    assert task["url"].endswith("/tasks#/list/15033/edit/5916") and task["tags"][0]["name"] == "BO"


def test_fetch_project_tasks_support_body(monkeypatch):
    sent = {}
    monkeypatch.setattr(pc._SESSION, "post", lambda url, headers=None, json=None, timeout=None: sent.update(json=json) or FakeResponse(200, PROJECT_PAYLOAD))
    pc.fetch_project_tasks(16, {}, 111, tab="DOCS", support=True)
    assert sent["json"] == {"subscribed_by": 111, "tag_id": 0, "status": "open", "support": True, "tab_name": "Support",
                            "deadline_from": "", "deadline_to": ""}


def test_fetch_project_tasks_default_tab_is_first(monkeypatch):
    monkeypatch.setattr(pc._SESSION, "post", lambda *a, **k: FakeResponse(200, PROJECT_PAYLOAD))
    assert pc.fetch_project_tasks(16, {}, 111)["tab"] == "Priority"


def test_fetch_project_tasks_auth_error(monkeypatch):
    monkeypatch.setattr(pc._SESSION, "post", lambda *a, **k: FakeResponse(401, {}))
    with pytest.raises(PermissionError):
        pc.fetch_project_tasks(16, {}, 111)


def test_project_endpoint_only_serves_configured_projects(monkeypatch, tmp_path):
    monkeypatch.setenv("PANTEON_BEARER_TOKEN", "tok")
    seen = {}
    monkeypatch.setattr(pc, "fetch_project_tasks", lambda pid, headers, user, code=None, tab=None, support=False: seen.update(
        pid=pid, code=code, tab=tab, support=support) or {"project_id": pid, "tabs": [], "groups": []})
    config = {"repos": {"15033": {"id": 16, "name": "duell-admin"}}}
    app = dashboard.create_app(user_id=111, base_dir=str(tmp_path), work_config=lambda: config)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    assert client.get("/api/project/16/tasks?tab=DOCS").status_code == 200
    assert seen == {"pid": 16, "code": "15033", "tab": "DOCS", "support": False}
    assert client.get("/api/project/16/tasks?support=1").status_code == 200
    assert seen["support"] is True
    assert client.get("/api/project/99/tasks").status_code == 404
    assert client.get("/api/project/abc/tasks").status_code == 404
    assert client.get("/api/work/config").json()["projects"] == [{"code": "15033", "id": 16, "name": "duell-admin"}]


def test_events_marker_endpoint(tmp_path):
    client = TestClient(dashboard.create_app(None, str(tmp_path)), base_url="http://127.0.0.1")
    assert client.get("/api/events/marker").json() == {"max_id": 0, "count": 0}


class TestCoverage:
    """GET /api/coverage reads the cache only; POST rebuilds through the (faked) builder, guarded by the header."""

    def app(self, tmp_path, builder, cache=None, authors=None):
        config = lambda: {"repos": {}, "claude": "claude", "terminal": "t", "me": None, "git_authors": authors or []}
        app = dashboard.create_app(user_id=135, base_dir=str(tmp_path), work_config=config, coverage_builder=builder,
                                   coverage_cache=cache, repo_dir="/admin", pos_dir="/pos")
        return TestClient(app, base_url="http://127.0.0.1:8765")

    def test_get_is_empty_until_built_and_never_runs_the_builder(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PANTEON_BEARER_TOKEN", "tok")
        calls = []
        client = self.app(tmp_path, lambda **kw: calls.append(kw))
        assert client.get("/api/coverage").json() == {"report": None, "step": None}
        assert calls == []

    def test_post_needs_the_header(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PANTEON_BEARER_TOKEN", "tok")
        calls = []
        client = self.app(tmp_path, lambda **kw: calls.append(kw))
        assert client.post("/api/coverage").status_code == 403
        assert calls == []

    def test_post_builds_caches_and_passes_the_settings(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PANTEON_BEARER_TOKEN", "tok")
        seen = {}

        def builder(**kw):
            seen.update(kw)
            kw["progress"]("Fetching duell-admin")
            return {"tickets": [{"issue": "4542", "state": "not_on_prod", "repos": {}, "unread": []}], "errors": {}}

        client = self.app(tmp_path, builder, authors=["a@example.com"])
        response = client.post("/api/coverage", headers={"X-Timeline-Desk": "1"})
        assert response.status_code == 200 and response.json()["report"]["tickets"][0]["issue"] == "4542"
        assert seen["dirs"] == {"duell-admin": "/admin", "duell-pos": "/pos"}
        assert seen["authors"] == ["a@example.com"] and seen["base_dir"] == str(tmp_path)
        assert callable(seen["status_lookup"])  # Timeline titles and flags
        assert callable(seen["subscribed"])  # only the tickets you are subscribed to
        assert client.get("/api/coverage").json() == {"report": response.json()["report"], "step": None}

    def test_each_ticket_gets_its_timeline_link_from_its_project(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PANTEON_BEARER_TOKEN", "tok")

        def builder(**kw):
            return {"tickets": [
                {"issue": "4542", "state": "x", "repos": {}, "timeline": {"project_id": 16}},
                {"issue": "1", "state": "x", "repos": {}, "timeline": {"project_id": 99}},  # a project not set up
                {"issue": "2", "state": "x", "repos": {}, "timeline": None},  # Timeline was not read
            ], "errors": {}}

        config = lambda: {"repos": {"15033": {"id": 16, "name": "duell-admin"}, "9": {"name": "no id"}}, "git_authors": []}
        app = dashboard.create_app(user_id=135, base_dir=str(tmp_path), work_config=config, coverage_builder=builder)
        client = TestClient(app, base_url="http://127.0.0.1:8765")
        tickets = client.post("/api/coverage", headers={"X-Timeline-Desk": "1"}).json()["report"]["tickets"]
        assert tickets[0]["url"] == f"{pc.BASE_URL}/tasks#/list/15033/edit/4542"
        assert "url" not in tickets[1] and "url" not in tickets[2]

    def test_git_failure_is_a_502_and_keeps_the_old_report(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PANTEON_BEARER_TOKEN", "tok")
        import release_check

        cache = dashboard.CoverageCache()
        cache.report = {"tickets": [], "errors": {}}

        def builder(**kw):
            raise release_check.ReleaseCheckError("git is not installed")

        client = self.app(tmp_path, builder, cache)
        response = client.post("/api/coverage", headers={"X-Timeline-Desk": "1"})
        assert response.status_code == 502 and "git is not installed" in response.json()["error"]
        assert client.get("/api/coverage").json()["report"] == {"tickets": [], "errors": {}}
        assert cache.step is None

    def test_scheduler_rebuilds_then_waits_and_survives_failure(self):
        import threading

        stop = threading.Event()
        calls = []

        def rebuild():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("offline")
            stop.set()

        dashboard.run_coverage_scheduler(rebuild, stop, active_hours=None, minutes=0, off_hours_minutes=0)
        assert len(calls) == 2


def test_config_reads_git_authors(tmp_path):
    import work_session

    path = tmp_path / "desk.json"
    path.write_text(json.dumps({"git_authors": [" a@example.com ", "", "b@example.com"]}))
    assert work_session.load_config(str(path))["git_authors"] == ["a@example.com", "b@example.com"]
    assert work_session.load_config(str(tmp_path / "none.json"))["git_authors"] == []
