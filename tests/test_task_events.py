"""Event history of watched tasks. No network: the client calls are monkeypatched."""
from datetime import datetime

import pytest
import requests
from starlette.testclient import TestClient

import dashboard
import panteon_client as pc
import task_events as te

NOW = datetime(2026, 10, 27, 12, 0, 0)


def _item(id, field, created, old=None, new=None, **extra):
    return {"id": id, "field_name": field, "original_value": old, "updated_value": new, "created_at": created,
            "employee": {"id": 3, "name": "Peter Hamre", "photo": "p.jpg"}, "task_comment": None, "task_commit": None,
            "checklist_title": None, **extra}


TIMELINE = [
    _item(2, "Status", "26.06.2026 01:54", "RSD in progress", "Estimate pending"),
    _item(1, "Comment Added", "26.06.2026 01:54", "Old value", "New value",
          task_comment={"description": "## Design\nsee figma"}),
    _item(3, "Commit Added", "21.07.2026 12:35",
          task_commit={"description": "TL #5 feat: x", "commit_url": "https://github.com/o/r/commit/abc"}),
    _item(4, "Deadline", "02.08.2026 09:00", None, "2026-10-20"),
]


@pytest.fixture
def fake_api(monkeypatch):
    state = {"status": "open", "items": list(TIMELINE), "calls": []}

    def by_issue(issue, headers, company_id=None):
        state["calls"].append(("task", issue))
        if issue == "404":
            raise requests.HTTPError(response=type("R", (), {"status_code": 422})())
        return {"data": {"task_id": int(issue) + 1000, "issue_number": int(issue), "title": f"Task {issue}", "task_status": state["status"],
                         "created_at_string": "20.06.2026 09:00", "description": "Spec", "task_creator": {"name": "Ola"}}}

    def timeline(task_id, headers):
        state["calls"].append(("timeline", task_id))
        # Real event ids are unique across tasks, so offset the fake ones per task.
        return [{**i, "id": i["id"] + task_id * 100} for i in state["items"]]

    monkeypatch.setattr(pc, "get_task_by_issue", by_issue)
    monkeypatch.setattr(pc, "get_task_timeline", timeline)
    return state


class TestParsing:
    def test_time_formats(self):
        assert te.parse_event_time("26.06.2026 01:54") == "2026-06-26T01:54:00"
        assert te.parse_event_time("2026-06-26T01:54:30") == "2026-06-26T01:54:30"
        assert te.parse_event_time("garbage") == ""

    def test_comment_drops_placeholder_values(self):
        e = te.parse_event(TIMELINE[1], 5)
        assert e["kind"] == "comment" and e["body"].startswith("## Design")
        assert e["old_value"] is None and e["new_value"] is None
        assert e["employee"] == "Peter Hamre" and e["photo"] == "p.jpg"

    def test_commit_and_field(self):
        assert te.parse_event(TIMELINE[2], 5)["url"] == "https://github.com/o/r/commit/abc"
        field = te.parse_event(TIMELINE[3], 5)
        assert (field["kind"], field["new_value"]) == ("field", "2026-10-20")

    def test_unusable_items_are_skipped(self):
        assert te.parse_event({"field_name": "Status"}, 5) is None
        assert te.parse_event(_item(9, "Status", "nonsense"), 5) is None


class TestSync:
    def test_backfill_then_idempotent(self, fake_api, tmp_path):
        base = str(tmp_path)
        te.watch_task(5, "15033", "Five", base, now=NOW)
        first = te.sync_watchlist({}, base, now=NOW)
        assert first["new_events"] == 5 and first["errors"] == {}
        assert te.sync_watchlist({}, base, now=NOW)["new_events"] == 0
        fake_api["items"].append(_item(5, "Comment Added", "27.10.2026 10:00", task_comment={"description": "hi"}))
        assert te.sync_watchlist({}, base, now=NOW)["new_events"] == 1
        row = te.list_watchlist(base)[0]
        assert row["events"] == 6 and row["title"] == "Task 5" and row["last_synced_at"] == "2026-10-27T12:00:00"

    def test_creation_with_description_is_the_first_event(self, fake_api, tmp_path):
        base = str(tmp_path)
        fake_api["items"].clear()                       # a task nobody has touched yet
        te.watch_task(5, "15033", "Five", base, now=NOW)
        te.sync_watchlist({}, base, now=NOW)
        (e,) = te.list_events(base)["events"]
        assert (e["kind"], e["body"], e["employee"], e["occurred_at"]) == ("created", "Spec", "Ola", "2026-06-20T09:00:00")
        assert te.sync_watchlist({}, base, now=NOW)["new_events"] == 0

    def test_known_task_skips_taskbyissue_unless_meta_is_refreshed(self, fake_api, tmp_path):
        base = str(tmp_path)
        te.watch_task(5, "15033", "Five", base, now=NOW)
        te.sync_watchlist({}, base, now=NOW)           # first sync learns the task id
        fake_api["calls"].clear()
        te.sync_watchlist({}, base, now=NOW, refresh_meta=False)
        assert fake_api["calls"] == [("timeline", 1005)]
        fake_api["calls"].clear()
        te.sync_watchlist({}, base, now=NOW)
        assert [c[0] for c in fake_api["calls"]] == ["task", "timeline"]

    def test_unknown_task_id_still_resolved_without_refresh(self, fake_api, tmp_path):
        te.watch_task(5, "15033", "Five", str(tmp_path), now=NOW)
        te.sync_watchlist({}, str(tmp_path), now=NOW, refresh_meta=False)
        assert [c[0] for c in fake_api["calls"]] == ["task", "timeline"]

    def test_only_watched_tasks_are_fetched(self, fake_api, tmp_path):
        te.watch_task(5, None, "", str(tmp_path), now=NOW)
        te.watch_task(6, None, "", str(tmp_path), now=NOW)
        te.unwatch_task(6, str(tmp_path))
        te.sync_watchlist({}, str(tmp_path), now=NOW)
        assert {c[1] for c in fake_api["calls"] if c[0] == "task"} == {"5"}

    def test_closed_task_is_removed_with_its_events(self, fake_api, tmp_path):
        fake_api["status"] = "closed"
        te.watch_task(5, None, "", str(tmp_path), now=NOW)
        result = te.sync_watchlist({}, str(tmp_path), now=NOW)
        assert result["removed"] == ["5"] and result["new_events"] == 0
        assert te.list_watchlist(str(tmp_path)) == []
        assert te.list_events(str(tmp_path))["events"] == []

    def test_a_task_paused_by_an_older_version_is_purged(self, fake_api, tmp_path):
        te.watch_task(5, None, "", str(tmp_path), now=NOW)
        te.sync_watchlist({}, str(tmp_path), now=NOW)
        conn = te.pc._open_planner_db(str(tmp_path), create=False)
        conn.execute("UPDATE watchlist SET active = 0")
        conn.commit()
        conn.close()
        assert te.sync_watchlist({}, str(tmp_path), now=NOW)["removed"] == ["5"]
        assert te.list_watchlist(str(tmp_path)) == [] and te.list_events(str(tmp_path))["events"] == []

    def test_one_failure_does_not_stop_the_others(self, fake_api, tmp_path):
        for i in (404, 5):
            te.watch_task(i, None, "", str(tmp_path), now=NOW)
        result = te.sync_watchlist({}, str(tmp_path), now=NOW)
        assert result["errors"] == {"404": "not found in Timeline"} and result["new_events"] == 5

    def test_unwatch_deletes_task_and_events(self, fake_api, tmp_path):
        te.watch_task(5, None, "", str(tmp_path), now=NOW)
        te.sync_watchlist({}, str(tmp_path), now=NOW)
        assert te.unwatch_task(5, str(tmp_path)) is True
        assert te.list_events(str(tmp_path))["events"] == []
        assert te.list_watchlist(str(tmp_path)) == []
        assert te.unwatch_task(5, str(tmp_path)) is False


class TestListEvents:
    @pytest.fixture
    def base(self, fake_api, tmp_path):
        for i in (5, 6):
            te.watch_task(i, "15033", f"T{i}", str(tmp_path), now=NOW)
        te.sync_watchlist({}, str(tmp_path), now=NOW)
        return str(tmp_path)

    def test_newest_first_across_tasks_with_id_tiebreak(self, base):
        events = te.list_events(base)["events"]
        stamps = [(e["occurred_at"], e["id"]) for e in events]
        assert stamps == sorted(stamps, reverse=True)
        assert events[0]["occurred_at"] == "2026-08-02T09:00:00"

    def test_filters(self, base):
        only = te.list_events(base, issues=[6], kinds=["comment", "status"])
        assert {e["issue_number"] for e in only["events"]} == {"6"}
        assert {e["kind"] for e in only["events"]} == {"comment", "status"}
        assert set(only["tasks"]) == {"6"} and only["tasks"]["6"]["url"].endswith("/list/15033/edit/6")

    def test_paging_with_cursor(self, base):
        seen, cursor = [], None
        while True:
            page = te.list_events(base, limit=3, before=cursor)
            seen += [(e["issue_number"], e["id"]) for e in page["events"]]
            cursor = page["next"]
            if not cursor:
                break
        assert len(seen) == len(set(seen)) == 10

    def test_bad_cursor_and_empty_db(self, base, tmp_path):
        with pytest.raises(ValueError):
            te.list_events(base, before="x")
        assert te.list_events(str(tmp_path / "none"))["events"] == []


class TestDashboard:
    @pytest.fixture
    def client(self, monkeypatch, tmp_path, fake_api):
        monkeypatch.setenv("PANTEON_BEARER_TOKEN", "tok")
        monkeypatch.setattr(dashboard.panteon_client, "resolve_user_id", lambda u=None: 135)
        project = {"id": 16, "code": "15033", "name": "Duell"}
        config = lambda: {"repos": {"15033": {"id": 16, "name": "Duell"}}}
        monkeypatch.setattr(dashboard.work_session, "public_config", lambda c: {"boards": {}, "projects": [project]})
        monkeypatch.setattr(pc, "fetch_project_tasks", lambda pid, h, u=None, code=None, tab=None, today=None: {
            "tab": "Open", "tabs": [{"name": "Open", "count": 1}],
            "groups": [{"name": "g", "tasks": [{"issue_number": 5, "title": "Task 5"}]}]})
        app = dashboard.create_app(user_id=135, base_dir=str(tmp_path), work_config=config)
        return TestClient(app, base_url="http://127.0.0.1:8765")

    G = {"X-Timeline-Desk": "1"}

    def test_writes_need_the_header(self, client):
        assert client.post("/api/watchlist", json={"issue": "5"}).status_code == 403
        assert client.delete("/api/watchlist/5").status_code == 403
        assert client.post("/api/events/sync").status_code == 403

    def test_watch_needs_a_task_that_exists_in_timeline(self, client):
        assert client.post("/api/watchlist", json={"issue": "404"}, headers=self.G).status_code == 404
        assert client.post("/api/watchlist", json={"issue": "abc"}, headers=self.G).status_code == 400
        assert client.get("/api/watchlist").json()["watchlist"] == []

    def test_a_closed_task_cannot_be_watched(self, client, fake_api):
        fake_api["status"] = "closed"
        response = client.post("/api/watchlist", json={"issue": "77"}, headers=self.G)
        assert response.status_code == 400 and "closed" in response.json()["error"]
        assert client.get("/api/watchlist").json()["watchlist"] == []

    def test_watch_a_task_that_is_not_on_my_lists(self, client):
        # The fixture's project lists only #5; #77 exists in Timeline but is on none of my lists (e.g. a support task).
        body = client.post("/api/watchlist", json={"issue": "77"}, headers=self.G).json()
        assert [w["issue_number"] for w in body["watchlist"]] == ["77"]

    def test_watch_backfills_and_feed_reads(self, client):
        body = client.post("/api/watchlist", json={"issue": "5"}, headers=self.G).json()
        assert body["sync"]["new_events"] == 5 and body["watchlist"][0]["issue_number"] == "5"
        feed = client.get("/api/events?kinds=comment,status").json()
        assert [e["kind"] for e in feed["events"]] == ["status", "comment"] or {e["kind"] for e in feed["events"]} == {"status", "comment"}
        assert feed["watchlist"][0]["events"] == 5
        assert client.delete("/api/watchlist/5", headers=self.G).status_code == 200
        assert client.delete("/api/watchlist/5", headers=self.G).status_code == 404



def test_events_marker_changes_when_events_are_stored(fake_api, tmp_path):
    base = str(tmp_path)
    assert te.events_marker(base) == {"max_id": 0, "count": 0}  # no database yet, and none is created
    assert not (tmp_path / "database").exists()
    te.watch_task(5, "15033", "Five", base, now=NOW)
    te.sync_watchlist({}, base, now=NOW)
    before = te.events_marker(base)
    assert before["count"] == 5
    assert te.events_marker(base) == before  # unchanged while nothing is stored
    fake_api["items"].append(_item(5, "Comment Added", "27.10.2026 10:00", task_comment={"description": "hi"}))
    te.sync_watchlist({}, base, now=NOW)
    after = te.events_marker(base)
    assert after["count"] == 6 and after["max_id"] > before["max_id"]


class TestMySubscribedIssues:
    """The open tasks you are subscribed to: every tab and the Support tab of every configured project."""

    def test_union_of_tabs_and_support_over_projects(self, monkeypatch):
        calls = []

        def fake(pid, headers, user_id=None, code=None, tab=None, today=None, support=False):
            calls.append((pid, tab, support))
            if support:
                return {"tabs": [], "tab": None, "groups": [{"name": "s", "tasks": [{"issue_number": 900 + pid}]}]}
            tabs = [{"name": "Dev"}, {"name": "QA"}]
            tasks = {None: [1, 2], "Dev": [1, 2], "QA": [3]}[tab]
            return {"tabs": tabs, "tab": "Dev", "groups": [{"name": "g", "tasks": [{"issue_number": n, "title": ""} for n in tasks]}]}

        monkeypatch.setattr(pc, "fetch_project_tasks", fake)
        got = te.my_subscribed_issues({}, [{"id": 16, "code": "1"}, {"id": 17, "code": "2"}], 111)
        assert got == {"1", "2", "3", "916", "917"}  # numbers as strings, support included
        assert (16, None, True) in calls and (17, None, True) in calls

    def test_no_project_is_an_error_not_an_empty_set(self):
        with pytest.raises(ValueError, match="No Timeline project"):
            te.my_subscribed_issues({}, [], 111)
