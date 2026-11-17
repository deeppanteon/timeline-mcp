"""Unit tests for panteon_client's pure functions and networking error handling.

No live Panteon credentials or network access are required: HTTP calls are made
against a monkeypatched session.
"""
from datetime import date

import json

import pytest
import requests

import panteon_client as pc


# --------------------------------------------------------------------------- #
# extract_issue_id
# --------------------------------------------------------------------------- #
class TestExtractIssueId:
    def test_bare_numeric(self):
        assert pc.extract_issue_id("5461") == "5461"

    def test_bare_numeric_with_whitespace(self):
        assert pc.extract_issue_id("  5461  ") == "5461"

    def test_edit_url(self):
        url = "https://timeline.panteon.no/tasks#/list/15033/edit/5461"
        assert pc.extract_issue_id(url) == "5461"

    def test_edit_url_trailing_slash(self):
        url = "https://timeline.panteon.no/tasks#/list/15033/edit/5461/"
        assert pc.extract_issue_id(url) == "5461"

    def test_list_url_without_edit_raises(self):
        # Must NOT silently return the board/list id (15033).
        with pytest.raises(ValueError):
            pc.extract_issue_id("https://timeline.panteon.no/tasks#/list/15033")

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            pc.extract_issue_id("not-a-task")


# --------------------------------------------------------------------------- #
# _normalize_date
# --------------------------------------------------------------------------- #
class TestNormalizeDate:
    def test_iso(self):
        assert pc._normalize_date("2026-06-26T10:00:00") == "2026-06-26"

    def test_dmy(self):
        assert pc._normalize_date("26.06.2026 10:00") == "2026-06-26"

    def test_empty(self):
        assert pc._normalize_date("") == ""
        assert pc._normalize_date(None) == ""

    def test_unparseable_passthrough(self):
        assert pc._normalize_date("June 26") == "June 26"


# --------------------------------------------------------------------------- #
# _resolve_url
# --------------------------------------------------------------------------- #
class TestResolveUrl:
    def test_absolute_untouched(self):
        assert pc._resolve_url("https://x/y.png") == "https://x/y.png"

    def test_protocol_relative(self):
        assert pc._resolve_url("//cdn/y.png") == "https://cdn/y.png"

    def test_root_relative(self):
        assert pc._resolve_url("/storage/a.png") == f"{pc.BASE_URL}/storage/a.png"

    def test_bare_relative(self):
        assert pc._resolve_url("storage/a.png") == f"{pc.BASE_URL}/storage/a.png"

    def test_data_uri_untouched(self):
        data_uri = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="
        assert pc._resolve_url(data_uri) == data_uri


# --------------------------------------------------------------------------- #
# extract_images
# --------------------------------------------------------------------------- #
class TestExtractImages:
    def test_markdown_image_absolutized(self):
        assert pc.extract_images("![a](/storage/x.png)") == [f"{pc.BASE_URL}/storage/x.png"]

    def test_html_image(self):
        assert pc.extract_images('<img src="/storage/y.png">') == [f"{pc.BASE_URL}/storage/y.png"]

    def test_non_image_link_ignored(self):
        assert pc.extract_images("[report](/files/data.csv)") == []

    def test_dedup_preserves_order(self):
        text = "![a](/s/1.png) ![b](/s/2.png) ![c](/s/1.png)"
        assert pc.extract_images(text) == [f"{pc.BASE_URL}/s/1.png", f"{pc.BASE_URL}/s/2.png"]

    def test_empty(self):
        assert pc.extract_images("") == []
        assert pc.extract_images(None) == []

    def test_data_uri_image_untouched(self):
        data_uri = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="
        assert pc.extract_images(f"![a]({data_uri})") == [data_uri]


# --------------------------------------------------------------------------- #
# absolutize_markdown_images
# --------------------------------------------------------------------------- #
class TestAbsolutizeMarkdownImages:
    def test_rewrites_src(self):
        out = pc.absolutize_markdown_images("![alt](/storage/x.png)")
        assert out == f"![alt]({pc.BASE_URL}/storage/x.png)"

    def test_leaves_non_image_links(self):
        assert pc.absolutize_markdown_images("[x](/f.csv)") == "[x](/f.csv)"


# --------------------------------------------------------------------------- #
# collect_all_images
# --------------------------------------------------------------------------- #
def test_collect_all_images_dedups_across_sources():
    comments = [{"images": [f"{pc.BASE_URL}/s/1.png", f"{pc.BASE_URL}/s/3.png"]}]
    urls = pc.collect_all_images(
        "![a](/s/1.png)",
        comments,
        extra_descriptions=["![b](/s/2.png)"],
    )
    assert urls == [
        f"{pc.BASE_URL}/s/1.png",
        f"{pc.BASE_URL}/s/2.png",
        f"{pc.BASE_URL}/s/3.png",
    ]


# --------------------------------------------------------------------------- #
# _as_code_block
# --------------------------------------------------------------------------- #
def test_as_code_block_outgrows_inner_fences():
    text = "```\nnested\n```"
    out = pc._as_code_block(text)
    # Outer fence must be longer than any backtick run inside so it can't break out.
    assert out.startswith("````")
    assert out.endswith("````")


# --------------------------------------------------------------------------- #
# parse_conversation_comments
# --------------------------------------------------------------------------- #
def test_parse_conversation_comments():
    timeline = [
        {
            "type": "task_comment",
            "employee": {"name": "Alice"},
            "created_at": "2026-06-26",
            "task_comment": {"description": "Hello ![p](/s/a.png)"},
        },
        {"type": "status_change"},  # ignored: not a comment
        {
            "field_name": "Comment Added",
            "employee_name": "Bob",
            "description": "",  # empty & no images -> skipped
        },
    ]
    comments = pc.parse_conversation_comments(timeline)
    assert len(comments) == 1
    assert comments[0]["author"] == "Alice"
    assert comments[0]["images"] == [f"{pc.BASE_URL}/s/a.png"]


# --------------------------------------------------------------------------- #
# extract_task_meta
# --------------------------------------------------------------------------- #
def test_extract_task_meta():
    raw = {
        "data": {
            "issue_number": 5461,
            "title": "Label print",
            "description": "  body  ",
            "task_creator": {"name": "Carol"},
            "created_at": "2026-06-26T09:00:00",
            "task_status": "open",
            "task_tags": [{"name": "bug"}, {"nope": 1}],
        }
    }
    meta = pc.extract_task_meta(raw)
    assert meta["title"] == "Label print"
    assert meta["description"] == "body"
    assert meta["creator"] == "Carol"
    assert meta["created_date"] == "2026-06-26"
    assert meta["status"] == "open"
    assert meta["tags"] == ["bug"]


# --------------------------------------------------------------------------- #
# render_task_markdown
# --------------------------------------------------------------------------- #
class TestRenderTaskMarkdown:
    def _meta(self, **over):
        base = {
            "title": "Label print",
            "description": "Do the thing ![p](/s/a.png)",
            "created_date": "2026-06-26",
            "status": "open",
        }
        base.update(over)
        return base

    def test_uses_real_status_not_hardcoded(self):
        md = pc.render_task_markdown("5461", self._meta(status="closed"), [])
        assert "**Status:** closed" in md
        assert "In Analysis" not in md  # the old hardcoded value must be gone

    def test_status_unknown_fallback(self):
        md = pc.render_task_markdown("5461", self._meta(status=""), [])
        assert "**Status:** (unknown)" in md

    def test_includes_title_and_description(self):
        md = pc.render_task_markdown("5461", self._meta(), [])
        assert "# Task 5461: Label print" in md
        assert f"{pc.BASE_URL}/s/a.png" in md  # image absolutized

    def test_conversation_and_images_sections(self):
        comments = [{
            "author": "Alice",
            "created_at": "2026-06-26",
            "description": "hi",
            "images": [],
        }]
        md = pc.render_task_markdown("5461", self._meta(), comments)
        assert "## Conversation (1 comment(s))" in md
        assert "### Alice — 2026-06-26" in md

    def test_header_shows_creator_and_tags(self):
        md = pc.render_task_markdown(
            "5461", self._meta(creator="Carol", tags=["bug", "urgent"]), []
        )
        assert "**Creator:** Carol" in md
        assert "**Tags:** bug, urgent" in md

    def test_header_creator_and_tags_fallback(self):
        md = pc.render_task_markdown("5461", self._meta(), [])
        assert "**Creator:** —" in md
        assert "**Tags:** —" in md

    def test_creator_comments_are_marked(self):
        comments = [
            {"author": "carol", "created_at": "2026-06-26", "description": "a", "images": []},
            {"author": "Alice", "created_at": "2026-06-27", "description": "b", "images": []},
        ]
        md = pc.render_task_markdown("5461", self._meta(creator="Carol"), comments)
        assert "### carol (task creator) — 2026-06-26" in md
        assert "### Alice — 2026-06-27" in md

    def test_no_creator_marking_when_creator_unknown(self):
        comments = [{"author": "", "created_at": "", "description": "a", "images": []}]
        md = pc.render_task_markdown("5461", self._meta(creator=""), comments)
        assert "(task creator)" not in md

    def test_open_questions_is_a_placeholder_not_none(self):
        md = pc.render_task_markdown("5461", self._meta(), [])
        open_q = md.split("## Open Questions", 1)[1].split("## Images", 1)[0]
        assert "None." not in open_q
        assert "To be derived" in open_q


# --------------------------------------------------------------------------- #
# build_api_headers
# --------------------------------------------------------------------------- #
class TestBuildApiHeaders:
    def test_strips_bearer_prefix(self, monkeypatch):
        monkeypatch.delenv("PANTEON_BEARER_TOKEN", raising=False)
        monkeypatch.delenv("PANTEON_COOKIE", raising=False)
        headers = pc.build_api_headers(bearer_token="Bearer eyJabc")
        assert headers["Authorization"] == "Bearer eyJabc"

    def test_cookie(self, monkeypatch):
        monkeypatch.delenv("PANTEON_BEARER_TOKEN", raising=False)
        monkeypatch.delenv("PANTEON_COOKIE", raising=False)
        headers = pc.build_api_headers(cookie="a=b")
        assert headers["Cookie"] == "a=b"

    def test_missing_credentials_raises(self, monkeypatch):
        monkeypatch.delenv("PANTEON_BEARER_TOKEN", raising=False)
        monkeypatch.delenv("PANTEON_COOKIE", raising=False)
        with pytest.raises(PermissionError):
            pc.build_api_headers()


# --------------------------------------------------------------------------- #
# _api_get / networking error handling (monkeypatched session)
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class TestPollingHelpers:
    PLANNER = [{"user_id": 7, "data": [{"issue_number": 1, "stage_status": "Dev", "task_status": "open", "deadline": "2026-11-01"}]}]

    def test_fingerprint_changes_only_with_visible_fields(self):
        base = pc.planner_fingerprint(self.PLANNER, 7)
        assert base == pc.planner_fingerprint(self.PLANNER, 7)
        other = [{"user_id": 7, "data": [{**self.PLANNER[0]["data"][0], "stage_status": "QA"}]}]
        assert pc.planner_fingerprint(other, 7) != base
        assert pc.planner_fingerprint(self.PLANNER, 99) != base  # user absent -> empty list

    def test_known_periods_are_not_requested_again(self, monkeypatch):
        asked = []
        monkeypatch.setattr(pc, "get_priority_tasks", lambda headers, period: asked.append(period) or [])
        got = pc.fetch_all_planners({}, known={"now": self.PLANNER})
        assert got["now"] is self.PLANNER and asked == ["next", "commingup"]

    def test_pacing_spaces_out_requests(self, monkeypatch):
        sleeps, clock = [], [100.0]
        monkeypatch.setenv("PANTEON_MIN_REQUEST_GAP_MS", "200")
        monkeypatch.setattr(pc, "_next_slot", 0.0)
        monkeypatch.setattr(pc.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(pc.time, "sleep", sleeps.append)
        for _ in range(3):
            pc._pace()
        assert sleeps == [pytest.approx(0.2), pytest.approx(0.4)]  # first goes at once


class TestRequestLog:
    def _lines(self):
        files = list(pc.request_log_dir().glob("timeline-requests-*.jsonl"))
        assert len(files) == 1
        return [json.loads(line) for line in files[0].read_text().splitlines()]

    def test_logs_status_and_duration_without_query_or_headers(self, monkeypatch):
        monkeypatch.setattr(pc._SESSION, "get", lambda *a, **k: _FakeResponse(200))
        pc._api_get("https://timeline.panteon.no/api/task/9/timeline?token=secret", {"Authorization": "Bearer s3cret"})
        (entry,) = self._lines()
        assert entry["method"] == "GET" and entry["path"] == "/api/task/9/timeline"
        assert entry["status"] == 200 and isinstance(entry["ms"], int)
        assert "secret" not in json.dumps(entry)

    def test_logs_auth_failure_status(self, monkeypatch):
        monkeypatch.setattr(pc._SESSION, "get", lambda *a, **k: _FakeResponse(401))
        with pytest.raises(PermissionError):
            pc._api_get("http://x/api/a", {})
        assert self._lines()[0]["status"] == 401

    def test_logs_connection_error(self, monkeypatch):
        def boom(*a, **k):
            raise requests.ConnectionError("down")
        monkeypatch.setattr(pc._SESSION, "get", boom)
        with pytest.raises(requests.ConnectionError):
            pc._api_get("http://x/api/a", {})
        entry = self._lines()[0]
        assert entry["status"] is None and entry["error"] == "ConnectionError"


class TestApiGet:
    def test_401_becomes_permission_error(self, monkeypatch):
        monkeypatch.setattr(pc._SESSION, "get", lambda *a, **k: _FakeResponse(401))
        with pytest.raises(PermissionError):
            pc._api_get("http://x", {}, context=" ctx")

    def test_403_becomes_permission_error(self, monkeypatch):
        monkeypatch.setattr(pc._SESSION, "get", lambda *a, **k: _FakeResponse(403))
        with pytest.raises(PermissionError):
            pc._api_get("http://x", {})

    def test_timeline_401_is_permission_error(self, monkeypatch):
        # Regression: the timeline call used to leak a raw HTTP error on 401.
        monkeypatch.setattr(pc._SESSION, "get", lambda *a, **k: _FakeResponse(401))
        with pytest.raises(PermissionError):
            pc.get_task_timeline(123, {})

    def test_timeline_unwraps_data_list(self, monkeypatch):
        payload = {"data": [{"type": "task_comment", "task_comment": {"description": "x"}}]}
        monkeypatch.setattr(pc._SESSION, "get", lambda *a, **k: _FakeResponse(200, payload))
        items = pc.get_task_timeline(123, {})
        assert items == payload["data"]


# --------------------------------------------------------------------------- #
# save_task_markdown / task_markdown_path
# --------------------------------------------------------------------------- #
def test_save_and_path_roundtrip(tmp_path):
    meta = {"title": "T", "description": "d", "created_date": "2026-06-26", "status": "open"}
    saved = pc.save_task_markdown("5461", meta, [], base_dir=str(tmp_path))
    expected = pc.task_markdown_path("5461", base_dir=str(tmp_path))
    assert saved == expected
    with open(saved, encoding="utf-8") as fh:
        assert "# Task 5461: T" in fh.read()


# --------------------------------------------------------------------------- #
# Employee planner: resolve_user_id / get_priority_tasks / rendering
# --------------------------------------------------------------------------- #
def _jwt(payload):
    import base64, json
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"h.{body}.s"


_PLANNER = [
    {"user_id": 34, "sort_order": None, "data": [{"issue_number": 1, "title": "Other"}]},
    {"user_id": 135, "sort_order": None, "data": [
        {"issue_number": 6077, "code": "15033", "title": "Second | task", "task_status": "open",
         "stage_status": "Testing pending", "deadline": "2026-10-20", "priority_sort_order": 63, "support": 1},
        {"issue_number": 4107, "code": "15033", "title": "First\ntask", "task_status": "open",
         "stage_status": "QA in progress", "deadline": None, "priority_sort_order": 6, "support": 0},
    ]},
    {"user_id": 11, "sort_order": None, "data": []},
]


class TestResolveUserId:
    def test_explicit_argument_wins(self, monkeypatch):
        monkeypatch.setenv("PANTEON_USER_ID", "1")
        assert pc.resolve_user_id("42", bearer_token=_jwt({"sub": "7"})) == 42

    def test_env_var_beats_jwt(self, monkeypatch):
        monkeypatch.setenv("PANTEON_USER_ID", "99")
        assert pc.resolve_user_id(bearer_token=_jwt({"sub": "7"})) == 99

    def test_falls_back_to_jwt_sub(self, monkeypatch):
        monkeypatch.delenv("PANTEON_USER_ID", raising=False)
        assert pc.resolve_user_id(bearer_token="Bearer " + _jwt({"sub": "135"})) == 135

    def test_unresolvable_raises(self, monkeypatch):
        monkeypatch.delenv("PANTEON_USER_ID", raising=False)
        monkeypatch.delenv("PANTEON_BEARER_TOKEN", raising=False)
        with pytest.raises(ValueError):
            pc.resolve_user_id()


class TestPriorityTasks:
    def test_get_priority_tasks_unwraps_data(self, monkeypatch):
        calls = []

        def fake_get(url, **k):
            calls.append(url)
            return _FakeResponse(200, {"data": _PLANNER, "status": "success", "code": 200})

        monkeypatch.setattr(pc._SESSION, "get", fake_get)
        assert pc.get_priority_tasks({}) == _PLANNER
        assert calls == [f"{pc.BASE_URL}/api/employee-planner/get-priority-tasks/now"]

    def test_get_priority_tasks_401_is_permission_error(self, monkeypatch):
        monkeypatch.setattr(pc._SESSION, "get", lambda *a, **k: _FakeResponse(401))
        with pytest.raises(PermissionError):
            pc.get_priority_tasks({})

    def test_extract_filters_and_sorts_by_priority(self):
        tasks = pc.extract_user_priority_tasks(_PLANNER, 135)
        assert [t["issue_number"] for t in tasks] == [4107, 6077]

    def test_extract_user_with_no_tasks(self):
        assert pc.extract_user_priority_tasks(_PLANNER, 11) == []

    def test_extract_unknown_user_raises(self):
        with pytest.raises(LookupError):
            pc.extract_user_priority_tasks(_PLANNER, 555)

    def test_render_table(self):
        md = pc.render_priority_tasks_markdown(
            135, pc.extract_user_priority_tasks(_PLANNER, 135), today=date(2026, 9, 1)
        )
        assert "**Tasks:** 2" in md
        assert f"[#4107]({pc.BASE_URL}/tasks#/list/15033/edit/4107)" in md
        # Titles are flattened so they can't break the table.
        assert "| First task |" in md
        assert "Second \\| task" in md
        assert md.index("#4107") < md.index("#6077")
        assert "| 2026-10-20 | yes |" in md

    def test_render_empty(self):
        md = pc.render_priority_tasks_markdown(11, [])
        assert "_(no priority tasks)_" in md
        assert "Overdue" not in md


class TestDeadlineFlags:
    TODAY = date(2026, 10, 16)

    @pytest.mark.parametrize("deadline,expected", [
        ("2026-10-15", "overdue"),
        ("15.10.2026 09:00", "overdue"),
        ("2026-10-16T23:59:00", "due today"),
        ("2026-10-17", "due in 1d"),
        ("2026-10-23", "due in 7d"),
        ("2026-10-24", ""),
        (None, ""),
        ("", ""),
        ("soon", ""),
    ])
    def test_deadline_flag(self, deadline, expected):
        assert pc.deadline_flag(deadline, self.TODAY) == expected

    def test_render_flags_and_counts(self):
        tasks = [
            {"issue_number": 1, "deadline": "2026-10-01"},
            {"issue_number": 2, "deadline": "2026-10-20"},
            {"issue_number": 3, "deadline": "2026-12-01"},
            {"issue_number": 4, "deadline": None},
        ]
        md = pc.render_priority_tasks_markdown(135, tasks, today=self.TODAY)
        assert "**Overdue:** 1 · **Due within 7 days:** 1 (as of 2026-10-16)" in md
        assert "| 2026-10-01 **(overdue)** |" in md
        assert "| 2026-10-20 **(due in 4d)** |" in md
        assert "| 2026-12-01 |" in md

    def test_fetch_my_priority_tasks(self, monkeypatch):
        monkeypatch.delenv("PANTEON_USER_ID", raising=False)
        monkeypatch.setattr(pc, "get_priority_tasks", lambda headers, period="now": _PLANNER)
        result = pc.fetch_my_priority_tasks({}, bearer_token=_jwt({"sub": "135"}))
        assert result["user_id"] == 135
        assert len(result["tasks"]) == 2
        assert result["markdown"].startswith("# My Priority Tasks")


class TestPlannerPeriods:
    @pytest.mark.parametrize("period, segment", [
        ("now", "now"), ("next", "next"), ("commingup", "commingup"), (None, "now"),
    ])
    def test_normalize(self, period, segment):
        assert pc.normalize_planner_period(period) == segment

    @pytest.mark.parametrize("period", ["later", "comingup", "coming up"])
    def test_unknown_period_raises(self, period):
        # The API silently returns every task for unknown segments (including the
        # single-m "comingup"), so only the exact API spelling is accepted.
        with pytest.raises(ValueError):
            pc.normalize_planner_period(period)

    @pytest.mark.parametrize("period", ["next", "commingup"])
    def test_period_in_url(self, monkeypatch, period):
        calls = []
        monkeypatch.setattr(
            pc._SESSION, "get", lambda url, **k: calls.append(url) or _FakeResponse(200, {"data": []})
        )
        pc.get_priority_tasks({}, period)
        assert calls == [f"{pc.BASE_URL}/api/employee-planner/get-priority-tasks/{period}"]

    def test_render_heading_uses_period_label(self):
        assert pc.render_priority_tasks_markdown(1, [], "commingup").startswith(
            "# My Priority Tasks — Coming Up"
        )

    def test_fetch_passes_period(self, monkeypatch):
        seen = {}

        def fake(headers, period="now"):
            seen["period"] = period
            return _PLANNER

        monkeypatch.setattr(pc, "get_priority_tasks", fake)
        result = pc.fetch_my_priority_tasks({}, user_id=135, period="commingup")
        assert seen["period"] == "commingup"
        assert result["period"] == "commingup"
        assert "— Coming Up" in result["markdown"]


# --------------------------------------------------------------------------- #
# Planner change tracking: snapshots and diffs
# --------------------------------------------------------------------------- #
def _planner_for(user_id, tasks):
    return [{"user_id": 1, "data": []}, {"user_id": user_id, "data": tasks}]


def _task(issue, order, **extra):
    return {"issue_number": issue, "code": "15033", "title": f"Task {issue}",
            "priority_sort_order": order, "stage_status": "QA", "task_status": "open",
            "deadline": None, **extra}


class TestPlannerSnapshot:
    def test_build_snapshot_across_periods(self):
        planners = {
            "now": _planner_for(135, [_task(2, 2), _task(1, 1)]),
            "next": _planner_for(135, [_task(3, 1), _task(1, 5)]),  # 1 duplicated: now wins
            "commingup": _planner_for(135, []),
        }
        snap = pc.build_planner_snapshot(planners, 135, today=date(2026, 10, 16))
        assert snap["date"] == "2026-10-16"
        assert {k: (v["period"], v["position"]) for k, v in snap["tasks"].items()} == {
            "1": ("now", 1), "2": ("now", 2), "3": ("next", 1),
        }

    def test_build_snapshot_normalizes_deadline(self):
        planners = {"now": _planner_for(135, [_task(1, 1, deadline="20.10.2026 00:00")])}
        snap = pc.build_planner_snapshot(planners, 135)
        assert snap["tasks"]["1"]["deadline"] == "2026-10-20"

    def test_user_absent_from_one_period_is_fine(self):
        planners = {"now": _planner_for(135, [_task(1, 1)]), "next": [], "commingup": []}
        assert list(pc.build_planner_snapshot(planners, 135)["tasks"]) == ["1"]

    def test_user_absent_everywhere_raises(self):
        with pytest.raises(LookupError):
            pc.build_planner_snapshot({"now": [], "next": [], "commingup": []}, 135)

    def test_save_and_load_previous(self, tmp_path):
        for day, user in [("2026-10-14", 135), ("2026-10-15", 999), ("2026-10-16", 135)]:
            pc.save_planner_snapshot(
                {"user_id": user, "date": day, "taken_at": f"{day}T10:00:00", "tasks": {}}, str(tmp_path))
        prev = pc.load_previous_planner_snapshot(135, "2026-10-16", str(tmp_path))
        # Skips today's own snapshot and the other user's.
        assert prev["date"] == "2026-10-14"
        assert pc.load_previous_planner_snapshot(135, "2026-10-14", str(tmp_path)) is None

    def test_unchanged_snapshot_adds_no_row_but_updates_last_check(self, tmp_path):
        for at in ("2026-10-16T09:00:00", "2026-10-16T09:15:00"):
            pc.save_planner_snapshot({"user_id": 135, "date": "2026-10-16", "taken_at": at, "tasks": {}}, str(tmp_path))
        assert len(pc.list_planner_snapshots(135, str(tmp_path))) == 1
        assert pc.last_planner_check(135, str(tmp_path)) == "2026-10-16T09:15:00"

    def test_load_previous_without_directory(self, tmp_path):
        assert pc.load_previous_planner_snapshot(135, "2026-10-16", str(tmp_path)) is None


class TestPlannerDiff:
    OLD = {"user_id": 135, "date": "2026-10-15", "tasks": {
        "1": {"issue_number": 1, "code": "1", "title": "Stays", "period": "now", "position": 1,
              "stage_status": "QA", "task_status": "open", "deadline": None},
        "2": {"issue_number": 2, "code": "1", "title": "Moves", "period": "next", "position": 1,
              "stage_status": "QA", "task_status": "open", "deadline": None},
        "3": {"issue_number": 3, "code": "1", "title": "Gone", "period": "now", "position": 2,
              "stage_status": "QA", "task_status": "open", "deadline": None},
    }}
    NEW = {"user_id": 135, "date": "2026-10-16", "tasks": {
        "1": {**OLD["tasks"]["1"], "deadline": "2026-10-20", "stage_status": "Done"},
        "2": {**OLD["tasks"]["2"], "period": "now", "position": 2},
        "4": {"issue_number": 4, "code": "1", "title": "Fresh", "period": "now", "position": 3,
              "stage_status": "Dev", "task_status": "open", "deadline": None},
    }}

    def test_diff(self):
        diff = pc.diff_planner_snapshots(self.OLD, self.NEW)
        assert [t["issue_number"] for t in diff["added"]] == [4]
        assert [t["issue_number"] for t in diff["removed"]] == [3]
        assert [(t["issue_number"], t["from"], t["to"]) for t in diff["moved"]] == [(2, "next", "now")]
        assert diff["changed"] == [{**self.NEW["tasks"]["1"], "changes": [
            ("deadline", None, "2026-10-20"), ("stage", "QA", "Done"),
        ]}]

    def test_reorder_among_shared_tasks_is_a_priority_change(self):
        new = {"tasks": {**self.OLD["tasks"], "1": {**self.OLD["tasks"]["1"], "position": 9}}}
        diff = pc.diff_planner_snapshots(self.OLD, new)
        assert diff["changed"][0]["changes"] == [("priority", "#1", "#9")]

    def test_task_added_on_top_does_not_shift_the_others(self):
        tasks = {k: {**t, "position": t["position"] + 1} if t["period"] == "now" else t
                 for k, t in self.OLD["tasks"].items()}
        tasks["9"] = {**self.OLD["tasks"]["1"], "issue_number": 9, "position": 1}
        diff = pc.diff_planner_snapshots(self.OLD, {"tasks": tasks})
        assert diff["changed"] == [] and [t["issue_number"] for t in diff["added"]] == [9]

    def test_stage_and_release_changes_need_both_sides(self):
        old = {"tasks": {"1": {**self.OLD["tasks"]["1"], "stages": {"Testing": "in progress", "QA": "not started"},
                               "release_state": "before qa"}}}
        new = {"tasks": {"1": {**self.OLD["tasks"]["1"], "stages": {"Testing": "done", "QA": "not started"},
                               "release_state": "on qa"}}}
        assert pc.diff_planner_snapshots(old, new)["changed"][0]["changes"] == [
            ("Testing", "in progress", "done"), ("release", "before qa", "on qa")]
        bare = {"tasks": {"1": self.OLD["tasks"]["1"]}}
        assert not any(pc.diff_planner_snapshots(bare, new).values())  # no stage data before: nothing to compare

    def test_render_changes(self):
        diff = pc.diff_planner_snapshots(self.OLD, self.NEW)
        md = pc.render_planner_changes_markdown(self.NEW, self.OLD, diff, "/x/2026-10-16.json")
        assert md.startswith("# My Planner Changes — since 2026-10-15")
        assert "**Snapshot:** /x/2026-10-16.json" in md
        assert f"- [#4]({pc.BASE_URL}/tasks#/list/1/edit/4) Fresh — Now · Dev" in md
        assert "## Left the planner (1)" in md and "Gone — was Now" in md
        assert "Moves — Next → Now" in md
        assert "Stays — deadline: — → 2026-10-20; stage: QA → Done" in md

    def test_render_no_changes(self):
        md = pc.render_planner_changes_markdown(self.OLD, self.OLD, pc.diff_planner_snapshots(self.OLD, self.OLD))
        assert "_(no changes)_" in md

    def test_render_first_snapshot(self):
        md = pc.render_planner_changes_markdown(self.NEW, None, None, "/x/2026-10-16.json")
        assert "No earlier snapshot" in md and "Saved today's (3 tasks)" in md

    def test_render_first_snapshot_not_saved(self):
        md = pc.render_planner_changes_markdown(self.NEW, None, None)
        assert "Found 3 tasks; saving is off" in md


class TestFetchPlannerChanges:
    def _fake_planner(self, tasks_by_period):
        def fake(headers, period="now"):
            return _planner_for(135, tasks_by_period.get(period, []))
        return fake

    def test_first_run_then_second_day(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pc, "get_priority_tasks", self._fake_planner({"now": [_task(1, 1)]}))
        first = pc.fetch_my_planner_changes({}, user_id=135, base_dir=str(tmp_path), today=date(2026, 10, 15))
        assert first["previous"] is None
        assert (tmp_path / "database" / "timeline_task.db").exists()

        monkeypatch.setattr(pc, "get_priority_tasks", self._fake_planner({"now": [_task(1, 1), _task(2, 2)]}))
        second = pc.fetch_my_planner_changes({}, user_id=135, base_dir=str(tmp_path), today=date(2026, 10, 16))
        assert [t["issue_number"] for t in second["diff"]["added"]] == [2]
        assert "since 2026-10-15" in second["markdown"]

    def test_fetches_every_period(self, monkeypatch, tmp_path):
        seen = []

        def fake(headers, period="now"):
            seen.append(period)
            return _planner_for(135, [])

        monkeypatch.setattr(pc, "get_priority_tasks", fake)
        pc.fetch_my_planner_changes({}, user_id=135, base_dir=str(tmp_path))
        assert seen == list(pc.PLANNER_PERIODS)

    def test_no_save(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pc, "get_priority_tasks", self._fake_planner({}))
        result = pc.fetch_my_planner_changes({}, user_id=135, base_dir=str(tmp_path), save=False)
        assert result["snapshot_path"] is None
        assert not (tmp_path / "timeline").exists()


class TestFetchMyDay:
    def test_one_fetch_per_period_and_single_header(self, monkeypatch, tmp_path):
        seen = []
        tasks = {
            "now": [_task(1, 1, deadline="2026-10-10")],
            "next": [_task(2, 1, deadline="2026-10-18"), _task(3, 2, deadline="2026-12-01"), _task(4, 3)],
        }

        def fake(headers, period="now"):
            seen.append(period)
            return _planner_for(135, tasks.get(period, []))

        monkeypatch.setattr(pc, "get_priority_tasks", fake)
        result = pc.fetch_my_day({}, user_id=135, base_dir=str(tmp_path), today=date(2026, 10, 16))

        assert seen == list(pc.PLANNER_PERIODS)  # each period fetched exactly once
        assert [t["issue_number"] for t in result["due_soon_next"]] == [2]
        md = result["markdown"]
        assert md.startswith("# My Day — 2026-10-16")
        assert md.count("**User ID:**") == 1
        assert "## Now" in md and "## Next — due within 7 days" in md and "## Changes" in md
        assert "| 2026-10-10 **(overdue)** |" in md
        assert "#3" not in md.split("## Changes")[0]  # not due soon, so not listed
        assert (tmp_path / "database" / "timeline_task.db").exists()

    def test_next_section_omitted_when_nothing_due(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pc, "get_priority_tasks", lambda headers, period="now": _planner_for(135, []))
        md = pc.fetch_my_day({}, user_id=135, base_dir=str(tmp_path), save=False)["markdown"]
        assert "## Next" not in md
        assert not (tmp_path / "timeline").exists()

    def test_changes_reuse_prefetched_planners(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pc, "get_priority_tasks", lambda *a, **k: pytest.fail("should not fetch"))
        planners = {p: _planner_for(135, []) for p in pc.PLANNER_PERIODS}
        result = pc.fetch_my_planner_changes({}, user_id=135, base_dir=str(tmp_path), planners=planners)
        assert result["snapshot"]["tasks"] == {}

    def test_timeline_dir_resolution(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PANTEON_TIMELINE_DIR", str(tmp_path))
        assert pc.planner_snapshot_dir() == str(tmp_path / "timeline" / "planner")
        assert pc.task_markdown_path("5") == str(tmp_path / "timeline" / "5" / "task.md")


def test_changes_subsection_has_no_double_blank_line():
    md = pc.render_planner_changes_markdown({"user_id": 1, "date": "2026-10-16", "tasks": {}}, None, None, level=2)
    assert "\n\n\n" not in md


# --------------------------------------------------------------------------- #
# Workflow stage status
# --------------------------------------------------------------------------- #
def _stage_task(title="T", stage_status="QA in progress", **codes):
    base = {"stage_rsd": 4, "stage_est": 4, "stage_dev": 4, "stage_tst": 4, "stage_qa": 0, "stage_liv": 0}
    return {"data": {"title": title, "task_status": "open", "stage_status": stage_status, **base, **codes}}


class TestTaskStatuses:
    @pytest.mark.parametrize("qa,live,expected", [
        (0, 0, "before qa"), (2, 0, "in qa"), (3, 0, "qa blocked"),
        (4, 0, "qa approved"), (1, 4, "live"), (4, 4, "live"), (None, None, "before qa"),
    ])
    def test_release_state(self, qa, live, expected):
        assert pc.release_state({"stage_qa": qa, "stage_liv": live}) == expected

    def test_extract_task_status(self):
        status = pc.extract_task_status(_stage_task("Report", "Going live pending", stage_qa=4), "6027")
        assert status["issue_id"] == "6027"
        assert status["stage_status"] == "Going live pending"
        assert status["stages"]["QA"] == "done" and status["stages"]["Live"] == "not started"
        assert status["release_state"] == "qa approved" and status["qa_approved"] is True
        assert status["project_id"] is None
        assert pc.extract_task_status({"data": {"project_id": 16}}, "1")["project_id"] == 16

    def test_unknown_stage_code(self):
        status = pc.extract_task_status({"data": {"stage_qa": "x"}}, "1")
        assert status["stages"]["QA"] == "unknown" and status["qa_approved"] is False

    def test_bulk_lookup_order_dedupe_and_errors(self, monkeypatch):
        calls = []

        def fake(issue_id, headers, company_id=None):
            calls.append(issue_id)
            if issue_id == "404":
                raise requests.HTTPError(response=type("R", (), {"status_code": 422})())
            return _stage_task(f"Task {issue_id}", stage_qa=4 if issue_id == "2" else 2)

        monkeypatch.setattr(pc, "get_task_by_issue", fake)
        statuses = pc.get_task_statuses(
            ["1", f"{pc.BASE_URL}/tasks#/list/15033/edit/2", "1", "404", "abc"], {}
        )
        assert [s["issue_id"] for s in statuses] == ["1", "2", "404", "abc"]
        assert sorted(calls) == ["1", "2", "404"]  # duplicate and invalid ids not requested
        assert statuses[1]["qa_approved"] is True
        assert statuses[2]["error"] == "not found"
        assert statuses[3]["error"] == "invalid issue ID"

    def test_bulk_lookup_reraises_other_http_errors(self, monkeypatch):
        def fake(issue_id, headers, company_id=None):
            raise requests.HTTPError(response=type("R", (), {"status_code": 500})())

        monkeypatch.setattr(pc, "get_task_by_issue", fake)
        with pytest.raises(requests.HTTPError):
            pc.get_task_statuses(["1"], {})

    def test_bulk_lookup_auth_error_raises(self, monkeypatch):
        monkeypatch.setattr(pc._SESSION, "get", lambda *a, **k: _FakeResponse(401))
        with pytest.raises(PermissionError):
            pc.get_task_statuses(["1"], {})

    def test_render(self):
        statuses = [
            pc.extract_task_status(_stage_task("Live one", "Completed", stage_qa=1, stage_liv=4), "1"),
            pc.extract_task_status(_stage_task("In | QA", stage_qa=2), "2"),
            {"issue_id": "3", "error": "not found"},
        ]
        md = pc.render_task_statuses_markdown(statuses)
        assert "**Tasks:** 3 · **QA approved:** 1 · **Not found / invalid:** 1" in md
        assert "**By release state:** live 1 · in qa 1" in md
        assert "| #2 | In \\| QA | QA in progress | in progress | not started | in qa |" in md
        assert "| #3 | — | _not found_ |" in md


def test_rendered_comment_heading_has_timeline_comment_id():
    """A workflow points at one QA finding by Timeline's comment id, so task.md shows it."""
    comments = pc.parse_conversation_comments([
        {"id": 4242, "type": "task_comment", "employee": {"name": "Kari"}, "created_at": "2026-10-27 10:00",
         "task_comment": {"description": "Total is wrong"}},
    ])
    assert comments[0]["id"] == 4242
    md = pc.render_task_markdown("5001", {"title": "T", "description": "d"}, comments)
    assert "### Kari — 2026-10-27 10:00 · comment id 4242" in md


def test_parse_commits():
    items = [
        {"id": 1, "field_name": "Comment Added", "task_comment": {"description": "hi"}},
        {"id": 2, "field_name": "Commit Added", "created_at": "01.10.2026 11:00", "employee": {"name": "Ann"},
         "task_commit": {"description": "Fix it", "commit_url": "https://github.com/o/r/commit/abc1234"}},
    ]
    assert pc.parse_commits(items) == [
        {"id": 2, "author": "Ann", "created_at": "01.10.2026 11:00", "message": "Fix it",
         "url": "https://github.com/o/r/commit/abc1234"}]


def test_extract_task_status_deadline_and_hours():
    raw = {"data": {"deadline": "2026-12-01T00:00:00", "hours_spent": "108,42", "estimated_hours": "12"}}
    status = pc.extract_task_status(raw, "1")
    assert status["deadline"] == "2026-12-01"
    assert status["hours_spent"] == 108.42
    assert status["hours_estimated"] == 12.0
    empty = pc.extract_task_status({"data": {}}, "2")
    assert empty["deadline"] is None and empty["hours_spent"] is None
