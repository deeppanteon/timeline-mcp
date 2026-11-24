"""The Jira board: cards and their change log in SQLite. No network: the client is monkeypatched."""
from datetime import datetime

import pytest
import requests
from starlette.testclient import TestClient

import dashboard
import jira_board as jb
import panteon_client as pc

NOW = datetime(2026, 10, 28, 9, 0, 0)


class TestStore:
    def test_empty_board_creates_no_database(self, tmp_path):
        assert jb.board(str(tmp_path))["cards"] == []
        assert not (tmp_path / "database").exists()

    def test_add_and_list_keep_only_number_title_status(self, tmp_path):
        card = jb.add_card(5, "Task 5", "To Do", str(tmp_path), NOW)
        assert card == {"issue_number": "5", "title": "Task 5", "status": "To Do", "url": None, "support": False,
                        "pr_created": False}
        assert jb.list_cards(str(tmp_path)) == [card]

    def test_card_links_to_its_timeline_page_when_the_code_is_known(self, tmp_path):
        card = jb.add_card(5, "Task 5", "To Do", str(tmp_path), NOW, code="15033")
        assert card["url"] == f"{pc.BASE_URL}/tasks#/list/15033/edit/5"
        assert jb.list_cards(str(tmp_path))[0]["url"] == card["url"]

    def test_rejects_unknown_status_duplicates_and_bad_numbers(self, tmp_path):
        jb.add_card(5, "Task 5", "To Do", str(tmp_path), NOW)
        with pytest.raises(ValueError):
            jb.add_card(6, "Task 6", "Nope", str(tmp_path), NOW)
        with pytest.raises(ValueError, match="already"):
            jb.add_card(5, "Task 5", "Done", str(tmp_path), NOW)
        with pytest.raises(ValueError):
            jb.add_card("abc", "x", "To Do", str(tmp_path), NOW)

    def test_move_updates_the_card(self, tmp_path):
        jb.add_card(5, "Task 5", "To Do", str(tmp_path), NOW)
        jb.move_card(5, "On QA", str(tmp_path), NOW)
        assert jb.list_cards(str(tmp_path))[0]["status"] == "On QA"

    def test_pr_created_flag_is_per_column_and_local(self, tmp_path):
        base = str(tmp_path)
        jb.add_card(5, "Task 5", "Ready for QA", base, NOW)
        assert jb.list_cards(base)[0]["pr_created"] is False
        jb.move_card(5, "Ready for QA", base, NOW, pr_created=True)
        assert jb.list_cards(base)[0]["pr_created"] is True
        jb.move_card(5, "Ready for QA", base, NOW)  # a plain drop in the same column keeps it
        assert jb.list_cards(base)[0]["pr_created"] is True
        assert len(jb.pending_updates(base)) == 1  # flipping the flag is not queued for the bot
        jb.move_card(5, "Ready for QA", base, NOW, pr_created=False)
        assert jb.list_cards(base)[0]["pr_created"] is False
        jb.move_card(5, "Ready for QA", base, NOW, pr_created=True)
        jb.move_card(5, "On QA", base, NOW)  # leaving the column clears it
        assert jb.list_cards(base)[0]["pr_created"] is False
        jb.move_card(5, "Ready for Release", base, NOW, pr_created=True)  # a move can set it at once
        assert jb.list_cards(base)[0]["pr_created"] is True
        with pytest.raises(ValueError):
            jb.move_card(5, "Done", base, NOW, pr_created=True)

    def test_move_unknown_card_or_status(self, tmp_path):
        with pytest.raises(LookupError):
            jb.move_card(5, "Done", str(tmp_path), NOW)
        jb.add_card(5, "Task 5", "To Do", str(tmp_path), NOW)
        with pytest.raises(ValueError):
            jb.move_card(5, "Nope", str(tmp_path), NOW)

    def test_remove_deletes_the_card_and_tells_the_bot(self, tmp_path):
        base = str(tmp_path)
        jb.add_card(5, "Task 5", "To Do", base, NOW)
        jb.move_card(5, "On QA", base, NOW)
        assert jb.remove_card(5, base, NOW) == {"issue_number": "5", "title": "Task 5"}
        assert jb.list_cards(base) == []
        assert [(e["from_status"], e["to_status"]) for e in jb.pending_updates(base)][-1] == ("On QA", "Removed")
        with pytest.raises(LookupError):
            jb.remove_card(5, base, NOW)
        jb.add_card(5, "Task 5", "To Do", base, NOW)  # can be added again afterwards

    def test_every_change_is_queued_for_the_bot_once(self, tmp_path):
        base = str(tmp_path)
        jb.add_card(5, "Task 5", "To Do", base, NOW)
        jb.move_card(5, "In Dev", base, NOW)
        jb.move_card(5, "In Dev", base, NOW)  # dropped where it already was: not a change
        pending = jb.pending_updates(base)
        assert [(e["from_status"], e["to_status"]) for e in pending] == [(None, "To Do"), ("To Do", "In Dev")]
        assert jb.mark_delivered([pending[0]["id"]], base, NOW) == 1
        assert jb.mark_delivered([pending[0]["id"]], base, NOW) == 0  # already delivered
        assert [e["to_status"] for e in jb.pending_updates(base)] == ["In Dev"]


class TestFillCodes:
    def _fake(self, monkeypatch, issues):
        monkeypatch.setattr(pc, "fetch_project_tasks", lambda pid, h, u=None, code=None, tab=None, today=None: {
            "tab": "Open", "tabs": [{"name": "Open", "count": len(issues)}],
            "groups": [{"name": "g", "tasks": [{"issue_number": i, "title": f"Task {i}"} for i in issues]}]})

    def test_old_cards_get_their_code_and_gone_tasks_are_not_retried(self, tmp_path, monkeypatch):
        base = str(tmp_path)
        jb.add_card(5, "Task 5", "To Do", base, NOW)
        jb.add_card(6, "Task 6", "To Do", base, NOW)
        jb.add_card(7, "Task 7", "To Do", base, NOW, code="15033")
        self._fake(monkeypatch, [5])  # 6 is no longer one of my open tasks
        project = [{"id": 16, "code": "15033"}]
        assert jb.fill_missing_codes({}, project, 135, base) == 2
        urls = {c["issue_number"]: c["url"] for c in jb.list_cards(base)}
        assert urls == {"5": f"{pc.BASE_URL}/tasks#/list/15033/edit/5", "6": None, "7": f"{pc.BASE_URL}/tasks#/list/15033/edit/7"}
        assert jb.fill_missing_codes({}, project, 135, base) == 0  # nothing left to look up

    def test_nothing_to_fill_makes_no_request(self, tmp_path, monkeypatch):
        jb.add_card(7, "Task 7", "To Do", str(tmp_path), NOW, code="15033")
        monkeypatch.setattr(pc, "fetch_project_tasks", lambda *a, **k: pytest.fail("fetched"))
        assert jb.fill_missing_codes({}, [{"id": 16, "code": "15033"}], 135, str(tmp_path)) == 0


class TestSupportTasks:
    def test_asks_timeline_for_the_support_tab_and_lists_each_task_once(self, monkeypatch):
        sent = []

        def fake_post(url, headers=None, json=None, timeout=None):
            sent.append((url, json))
            tasks = [{"issue_number": 5399, "title": "Support A"}, {"issue_number": 5691, "title": "Support B"},
                     {"issue_number": 5399, "title": "Support A"}]
            return type("R", (), {"status_code": 200, "raise_for_status": lambda self: None, "json": lambda self: {
                "data": {"tabs": [{"tab_name": "Support", "tab_task_count": 3, "tab_sort_order": 1}],
                         "grouptasks": [{"name": "Support", "tasks": tasks}]}}})()
        monkeypatch.setattr(pc._SESSION, "post", fake_post)
        tasks = jb.list_support_tasks({}, [{"id": 16, "code": "15033"}], 111)
        assert sent == [(f"{pc.BASE_URL}/api/project/16/tasks", {
            "subscribed_by": 111, "tag_id": 0, "status": "open", "support": True, "tab_name": "Support",
            "deadline_from": "", "deadline_to": ""})]
        assert tasks == [{"issue_number": "5399", "title": "Support A", "code": "15033"},
                         {"issue_number": "5691", "title": "Support B", "code": "15033"}]

    def test_a_card_remembers_that_it_is_support(self, tmp_path):
        card = jb.add_card(5399, "Support A", "To Do", str(tmp_path), NOW, code="15033", support=True)
        assert card["support"] is True and jb.list_cards(str(tmp_path))[0]["support"] is True
        jb.add_card(6, "Task 6", "To Do", str(tmp_path), NOW)
        assert [c["support"] for c in jb.list_cards(str(tmp_path))] == [True, False]


class TestMyTasks:
    def test_every_tab_once_each(self, monkeypatch):
        pages = {None: ("Open", [5, 6]), "Later": ("Later", [6, 7])}

        def fake(pid, headers, user_id=None, code=None, tab=None, today=None):
            name, issues = pages[tab]
            return {"tab": name, "tabs": [{"name": "Open", "count": 2}, {"name": "Later", "count": 2}],
                    "groups": [{"name": "g", "tasks": [{"issue_number": i, "title": f"Task {i}"} for i in issues]}]}
        monkeypatch.setattr(pc, "fetch_project_tasks", fake)
        tasks = jb.list_my_tasks({}, [{"id": 16, "code": "15033"}], 135)
        assert [t["issue_number"] for t in tasks] == ["5", "6", "7"]
        assert tasks[0] == {"issue_number": "5", "title": "Task 5"}


class TestDashboard:
    G = {"X-Timeline-Desk": "1"}

    @pytest.fixture
    def client(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PANTEON_BEARER_TOKEN", "tok")
        monkeypatch.setattr(dashboard.panteon_client, "resolve_user_id", lambda u=None: 135)
        project = {"id": 16, "code": "15033", "name": "Duell"}
        monkeypatch.setattr(dashboard.work_session, "public_config", lambda c: {"boards": {}, "projects": [project]})
        def fake_project(pid, h, u=None, code=None, tab=None, today=None, support=False):
            tasks = [{"issue_number": 90, "title": "Support 90"}, {"issue_number": 91, "title": "Support 91"}] if support \
                else [{"issue_number": 5, "title": "Task 5"}, {"issue_number": 6, "title": "Task 6"}]
            return {"tab": "Support" if support else "Open", "tabs": [{"name": "Open", "count": 2}],
                    "groups": [{"name": "g", "tasks": tasks}]}
        monkeypatch.setattr(pc, "fetch_project_tasks", fake_project)

        def by_issue(issue, headers, company_id=None):  # any task that exists in Timeline, not just yours
            if issue == "99":
                raise requests.HTTPError(response=type("R", (), {"status_code": 422})())
            title = f"Support {issue}" if int(issue) >= 90 else f"Task {issue}"
            return {"data": {"issue_number": int(issue), "title": title, "project_id": 16,
                             "task_status": "closed" if issue == "55" else "open"}}
        monkeypatch.setattr(pc, "get_task_by_issue", by_issue)
        app = dashboard.create_app(user_id=135, base_dir=str(tmp_path), work_config=lambda: {"repos": {}})
        return TestClient(app, base_url="http://127.0.0.1:8765")

    def test_writes_need_the_header(self, client):
        assert client.post("/api/jira/cards", json={"issue": "5", "status": "To Do"}).status_code == 403
        assert client.post("/api/jira/cards/5/move", json={"status": "Done"}).status_code == 403
        assert client.delete("/api/jira/cards/5").status_code == 403

    def test_board_starts_empty_and_lists_statuses(self, client):
        body = client.get("/api/jira/board").json()
        assert body["cards"] == [] and body["statuses"][0] == "To Do" and "On QA" in body["statuses"]

    def test_my_tasks(self, client):
        assert client.get("/api/jira/my-tasks").json()["tasks"] == [
            {"issue_number": "5", "title": "Task 5"}, {"issue_number": "6", "title": "Task 6"}]

    def test_add_takes_the_title_from_timeline(self, client):
        response = client.post("/api/jira/cards", json={"issue": "5", "status": "In Dev", "title": "ignored"}, headers=self.G)
        assert response.json()["card"] == {"issue_number": "5", "title": "Task 5", "status": "In Dev",
                                           "url": f"{pc.BASE_URL}/tasks#/list/15033/edit/5", "support": False, "pr_created": False}
        assert client.get("/api/jira/board").json()["cards"][0]["title"] == "Task 5"

    def test_support_tasks_list_and_can_be_added_highlighted(self, client):
        assert [t["issue_number"] for t in client.get("/api/jira/support-tasks").json()["tasks"]] == ["90", "91"]
        response = client.post("/api/jira/cards", json={"issues": ["90", "5"], "status": "To Do"}, headers=self.G)
        assert [(c["issue_number"], c["support"]) for c in response.json()["cards"]] == [("90", True), ("5", False)]
        board = client.get("/api/jira/board").json()["cards"]
        assert {c["issue_number"]: c["support"] for c in board} == {"90": True, "5": False}
        assert {c["issue_number"]: c["title"] for c in board}["90"] == "Support 90"

    def test_add_any_task_that_exists_in_timeline(self, client):
        # #7 is on none of your lists and is not a support task: it can still go on the board.
        card = client.post("/api/jira/cards", json={"issue": "7", "status": "To Do"}, headers=self.G).json()["card"]
        assert card["title"] == "Task 7" and card["support"] is False and card["url"].endswith("/list/15033/edit/7")

    def test_a_closed_task_cannot_be_added(self, client):
        response = client.post("/api/jira/cards", json={"issues": ["7", "55"], "status": "To Do"}, headers=self.G)
        assert response.status_code == 400 and "#55 is closed" in response.json()["error"]
        assert client.get("/api/jira/board").json()["cards"] == []  # all or nothing

    def test_add_a_task_that_does_not_exist_once_with_a_known_status(self, client):
        assert client.post("/api/jira/cards", json={"issue": "99", "status": "To Do"}, headers=self.G).status_code == 404
        assert client.post("/api/jira/cards", json={"issue": "abc", "status": "To Do"}, headers=self.G).status_code == 400
        assert client.post("/api/jira/cards", json={"issue": "5", "status": "Nope"}, headers=self.G).status_code == 400
        assert client.post("/api/jira/cards", json={"issue": "5", "status": "To Do"}, headers=self.G).status_code == 200
        assert client.post("/api/jira/cards", json={"issue": "5", "status": "Done"}, headers=self.G).status_code == 400

    def test_add_several_at_once(self, client):
        response = client.post("/api/jira/cards", json={"issues": ["5", "6", "5"], "status": "To Do"}, headers=self.G)
        assert [c["issue_number"] for c in response.json()["cards"]] == ["5", "6"]
        assert [c["status"] for c in client.get("/api/jira/board").json()["cards"]] == ["To Do", "To Do"]

    def test_a_batch_is_all_or_nothing(self, client):
        client.post("/api/jira/cards", json={"issue": "5", "status": "Done"}, headers=self.G)
        again = client.post("/api/jira/cards", json={"issues": ["6", "5"], "status": "To Do"}, headers=self.G)
        assert again.status_code == 400 and "#5" in again.json()["error"]
        unknown = client.post("/api/jira/cards", json={"issues": ["6", "99"], "status": "To Do"}, headers=self.G)
        assert unknown.status_code == 404 and "#99" in unknown.json()["error"]
        assert [c["issue_number"] for c in client.get("/api/jira/board").json()["cards"]] == ["5"]  # 6 was not added
        assert client.post("/api/jira/cards", json={"issues": [], "status": "To Do"}, headers=self.G).status_code == 400

    def test_move(self, client):
        client.post("/api/jira/cards", json={"issue": "5", "status": "To Do"}, headers=self.G)
        assert client.post("/api/jira/cards/5/move", json={"status": "On QA"}, headers=self.G).status_code == 200
        assert client.get("/api/jira/board").json()["cards"][0]["status"] == "On QA"
        assert client.post("/api/jira/cards/6/move", json={"status": "Done"}, headers=self.G).status_code == 404
        assert client.post("/api/jira/cards/5/move", json={"status": "Nope"}, headers=self.G).status_code == 400

    def test_remove(self, client):
        client.post("/api/jira/cards", json={"issue": "5", "status": "To Do"}, headers=self.G)
        assert client.delete("/api/jira/cards/5", headers=self.G).json()["card"]["issue_number"] == "5"
        assert client.get("/api/jira/board").json()["cards"] == []
        assert client.delete("/api/jira/cards/5", headers=self.G).status_code == 404


def test_task_overview_collects_status_details_and_newest_events(monkeypatch):
    raw = {"data": {"id": 7, "issue_number": 5, "title": "T", "description": "d" * 2000, "project_id": 1,
                    "deadline": "2026-12-01", "hours_spent": "2,5", "estimated_hours": "5", "stage_dev": 2,
                    "task_creator": {"name": "Ann"}, "created_at": "2026-11-01T10:00:00"}}
    items = [
        {"id": 1, "created_at": "01.11.2026 10:00", "field_name": "Status", "original_value": "a", "updated_value": "b",
         "employee": {"name": "Bo"}},
        {"id": 2, "created_at": "02.11.2026 09:00", "field_name": "Comment Added",
         "task_comment": {"description": "x" * 900}, "employee": {"name": "Cy"}},
        {"id": 3, "created_at": "03.11.2026 09:00", "field_name": "Commit Added",
         "task_commit": {"description": "TL #5 fix", "commit_url": "https://github.com/o/r/commit/abcdef1"}, "employee": {"name": "Bo"}},
    ]
    monkeypatch.setattr(pc, "get_task_by_issue", lambda issue, headers: raw)
    monkeypatch.setattr(pc, "get_task_timeline", lambda task_id, headers: items)
    out = jb.task_overview("5", {})
    assert out["stages"]["Development"] == "in progress"
    assert out["hours_spent"] == 2.5 and out["hours_estimated"] == 5.0 and out["deadline"] == "2026-12-01"
    assert [e["id"] for e in out["events"]] == [3, 2, 1]  # newest first
    assert out["events"][1]["body"].endswith("…") and len(out["events"][1]["body"]) <= jb.OVERVIEW_TEXT + 1
    assert out["events"][2]["old"] == "a" and out["events"][2]["new"] == "b"
    assert len(out["description"]) == 2000  # the description is shown whole
    assert [c["author"] for c in out["comments"]] == ["Cy"] and len(out["comments"][0]["description"]) == 900  # not clipped
    assert [c["url"] for c in out["commits"]] == ["https://github.com/o/r/commit/abcdef1"]
    assert out["project_id"] == 1 and out["task_id"] == 7


def test_board_cards_carry_when_they_entered_their_column(tmp_path):
    base = str(tmp_path)
    jb.add_card("9", "T", "To Do", base, now=datetime(2026, 11, 1, 9, 0))
    jb.move_card("9", "In Dev", base, now=datetime(2026, 11, 5, 10, 30))
    assert jb.board(base)["cards"][0]["since"] == "2026-11-05T10:30:00"
