"""release_check: parsing helpers and the full report over a fake git/gh runner."""
import json
from datetime import date

import pytest
from starlette.testclient import TestClient

import dashboard
import release_check as rc


class TestParsing:
    @pytest.mark.parametrize("subject,branch", [
        ("Merge pull request #671 from Panteon-norway/5627-product-search", "5627-product-search"),
        ("Merge branch '4107-serial-number' into qa-release", "4107-serial-number"),
        ("Merge remote-tracking branch 'origin/3627-loomis' into qa-release", "3627-loomis"),
        ("Merge 4054 In qa-release", "4054"),
        ("Merge branch 'master' into qa-release", None),
        ("Merge remote-tracking branch 'origin/qa-release' into qa-release", None),
        ("Merge branch 'qa-release' of https://github.com/Panteon-norway/duell-admin into qa-release", None),
        ("Fix typo", None),
    ])
    def test_merged_branch(self, subject, branch):
        assert rc.merged_branch(subject) == branch

    def test_task_ids(self):
        assert rc.task_id_from_branch("temp-4608-general-settings") == "4608"
        assert rc.task_id_from_branch("pos-settings-error-logs-fixes") is None
        assert rc.task_id_from_title("PROD: #6034 - BUG: x") == "6034"
        assert rc.task_id_from_title("PROD : 4128 event channel") == "4128"
        assert rc.pr_task_id({"title": "PROD: fix", "headRefName": "5749-bug"}) == "5749"

    def test_release_entry_normalizes_levels(self):
        body = " - #4128 - Event channel\n    - Implement UI as per figma\n        - Sub point here\n - #1 - Other\n"
        entry = rc.release_entry(body, "4128")
        assert entry["title"] == "Event channel"
        assert entry["bullets"] == [(1, "Implement UI as per figma"), (2, "Sub point here")]
        assert entry["indent_ok"] is False
        assert rc.release_entry("- #4128 - Event channel\n  - Added the form tabs\n", "4128")["indent_ok"] is True
        assert rc.release_entry(body, "999") is None

    def test_note_problems(self):
        good = {"title": "PROD: #6034 - BUG: x", "body": "- #6034 - BUG: x\n  - Fixed course delete on update\n"}
        assert rc.note_problems(good, "master", "6034", "BUG: x") == []
        bad = {"title": "6034 fix", "body": "## Summary\n- [ ] tested\n- #6034 - Old title\n  - fixed\n"}
        problems = rc.note_problems(bad, "master", "6034", "BUG: x")
        assert "Title should start with PROD:" in problems
        assert "Description has headings or checklists" in problems
        assert "No change bullets under the entry" in problems
        assert any(p.startswith("Entry title differs") for p in problems)
        assert rc.note_problems({"title": "QA: 5", "body": ""}, "qa-release", "5555", None) == [
            "No `- #5555 - <title>` entry in the description"]

    def test_checks_summary_treats_snyk_as_warning(self):
        summary = rc.summarize_checks([
            {"__typename": "CheckRun", "name": "CI", "status": "COMPLETED", "conclusion": "FAILURE"},
            {"__typename": "CheckRun", "name": "Lint", "status": "IN_PROGRESS", "conclusion": ""},
            {"__typename": "StatusContext", "context": "security/snyk (phpanteon)", "state": "FAILURE"},
            {"__typename": "StatusContext", "context": "other", "state": "SUCCESS"},
        ])
        assert summary["failed"] == ["CI"] and summary["pending"] == ["Lint"]
        assert len(summary["warnings"]) == 1 and summary["total"] == 4

    def test_versions(self):
        assert rc.bump_version("v1.0.2.9") == "v1.0.3.0"
        assert rc.bump_version("v1.0.3.1") == "v1.0.3.2"
        releases = [
            {"tagName": "v1.0.3.1", "isDraft": False, "publishedAt": "2026-10-16T01:47:17Z"},
            {"tagName": "v1.0.3.1-qa", "isDraft": False, "publishedAt": "2026-10-15T05:22:56Z"},
            {"tagName": "v1.0.3.0", "isDraft": False, "publishedAt": "2026-10-14T03:09:56Z"},
        ]
        v = rc.next_versions(releases)
        assert v["latest_prod"] == {"tag": "v1.0.3.1", "date": "2026-10-16"}
        assert v["next_prod"] == "v1.0.3.2" and v["next_qa"] == "v1.0.3.2-qa" and v["draft"] is None
        v = rc.next_versions(releases + [{"tagName": "v1.0.3.2-qa", "isDraft": False}, {"tagName": "v1.0.3.2", "isDraft": True}])
        assert v["draft"] == "v1.0.3.2" and v["next_prod"] == "v1.0.3.2" and v["next_qa"] == "v1.0.3.3-qa"


# ---- full report over a fake repo ---------------------------------------
LOG = "\n".join([
    "m4\x1fq3 b4\x1f2026-10-16T12:00:00+02:00\x1fMerge pull request #9 from Panteon-norway/2222-two",
    "m3\x1fq2 b3\x1f2026-10-15T12:00:00+02:00\x1fMerge branch 'hotfix-logs' into qa-release",
    "m2\x1fq1 b2\x1f2026-10-14T12:00:00+02:00\x1fMerge branch 'master' into qa-release",
    "m1\x1fq0 b1\x1f2026-10-13T12:00:00+02:00\x1fMerge remote-tracking branch 'origin/1111-one' into qa-release",
    "m0\x1fqx b0\x1f2026-10-12T12:00:00+02:00\x1fMerge branch '3333-shipped' into qa-release",
])


def _pr(number, title, branch, body, oid="tip", **extra):
    return {"number": number, "title": title, "headRefName": branch, "headRefOid": oid, "body": body,
            "isDraft": False, "createdAt": "2026-10-10T08:00:00Z", "reviewDecision": "", "mergeable": "MERGEABLE",
            "statusCheckRollup": [], "url": f"https://github.com/x/pull/{number}", "author": {"login": "dev"}, **extra}


def _gh_outputs():
    return {
        ("pr", "list", "--base", "qa-release", "--state", "open"): [
            _pr(10, "QA: 4444 four", "4444-four", "- #4444 - Four\n  - Added the fourth thing\n")],
        ("pr", "list", "--base", "master", "--state", "open"): [
            _pr(11, "PROD: #1111 - One", "1111-one", "- #1111 - One\n  - Changed the first screen\n", oid="b1"),
            _pr(12, "PROD: #5555 - Five", "5555-five", "- #5555 - Five\n  - Direct fix for five\n", oid="z9"),
            _pr(13, "PROD: quick css fix", "css-fix", "", oid="z8", isDraft=True),
            _pr(14, "PROD: #2222 - Two", "2222-two", "- #2222 - Two\n  - Updated the second report\n", oid="z7")],
        ("pr", "list", "--base", "qa-release", "--state", "merged"): [
            {"number": 9, "headRefName": "2222-two", "title": "QA: 2222", "body": "- #2222 - Two\n  - Updated the second report\n"}],
        ("pr", "list", "--base", "master", "--state", "merged"): [
            {"number": 7, "headRefName": "3333-shipped", "title": "PROD: #3333 - Shipped", "url": "https://github.com/x/pull/7"}],
        ("release", "list"): [{"tagName": "v1.0.3.1", "isDraft": False, "publishedAt": "2026-10-16T01:00:00Z"}],
    }


def _gh_pos_outputs():
    return {
        ("pr", "list", "--base", "qa-release", "--state", "open"): [
            _pr(1540, "QA: #9998 - Pos open", "9998-pos-open", "- #9998 - Pos open\n  - x\n")],
        ("pr", "list", "--base", "qa-release", "--state", "merged"): [
            {"number": 1530, "headRefName": "9999-Pos-work", "title": "QA: #9999 - Pos work", "url": "https://github.com/p/pull/1530"}],
    }


POS_HEADS = ["9999-Pos-work", "9998-pos-open", "master", "qa-release", "prod-release", "7001-shipped-direct", "7002-not-shipped"]
# branches GitHub reports as already merged into a base (compare status "behind")
COMPARE_BEHIND = {"prod-release": {"7001-shipped-direct"}, "qa-release": {"7001-shipped-direct"}}


def fake_runner(calls, pos_ok=True):
    gh = _gh_outputs()
    gh_pos = _gh_pos_outputs()

    def run(args, cwd, timeout):
        calls.append(tuple(args))
        args = tuple(args)
        is_pos = cwd.endswith("duell-pos")
        if args[:3] == ("git", "remote", "get-url"):
            if is_pos:
                return 0, "git@github.com:Panteon-norway/duell-pos.git\n", ""
            return 0, "https://github.com/Panteon-norway/duell-admin.git\n", ""
        if is_pos and not pos_ok:
            return 128, "", "fatal: pos is unreachable"
        if args[:2] == ("gh", "api"):
            branch = args[2].split("...")[-1]
            return 0, ("behind\n" if branch in COMPARE_BEHIND.get(args[2].split("/compare/")[1].split("...")[0], ()) else "diverged\n"), ""
        if is_pos and args == ("git", "ls-remote", "--heads", "origin"):
            return 0, "".join(f"abc\trefs/heads/{b}\n" for b in POS_HEADS), ""
        if args[0] == "gh" and "Panteon-norway/duell-pos" in args:
            for key, value in gh_pos.items():
                if args[1:1 + len(key)] == key:
                    return 0, json.dumps(value), ""
            return 0, "[]", ""
        if args[:2] == ("git", "fetch"):
            return 0, "", ""
        if args[:2] == ("git", "log"):
            return 0, LOG + "\n", ""
        if args == ("git", "ls-remote", "--heads", "origin"):
            heads = ["1111-one", "2222-two", "6666-six", "master", "qa-release", "development", "feature-without-ticket"]
            return 0, "".join(f"abc\trefs/heads/{b}\n" for b in heads), ""
        if args == ("git", "rev-list", "origin/master"):
            return 0, "b0\nb2\nmaster1\n", ""  # 3333-shipped (b0) is already in master
        if args[:3] == ("git", "rev-list", "--no-merges"):
            counts = {"z9": "2\n", "z7": "0\n"}  # z7 differs from QA only by merges of master
            return (0, counts[args[4]], "") if args[4] in counts else (128, "", "fatal: bad object")
        if args == ("git", "rev-list", "origin/master..origin/qa-release"):
            return 0, "m4\nm3\nm1\nb1\nb3\nb4\n", ""
        if args[0] == "gh":
            for key, value in gh.items():
                if args[1:1 + len(key)] == key:
                    return 0, json.dumps(value), ""
        return 1, "", f"unexpected {args}"
    return run


STATUSES = {
    "1111": {"issue_id": "1111", "title": "One", "stage_status": "Going live pending", "release_state": "qa approved", "qa_approved": True},
    "2222": {"issue_id": "2222", "title": "Two", "stage_status": "QA in progress", "release_state": "in qa", "qa_approved": False},
    "4444": {"issue_id": "4444", "title": "Four", "stage_status": "Testing", "release_state": "before qa", "qa_approved": False},
    "5555": {"issue_id": "5555", "title": "Five", "stage_status": "Testing", "release_state": "before qa", "qa_approved": False},
    # Not in any PR or on qa-release: only reached through extra_ids.
    "6666": {"issue_id": "6666", "title": "Six", "project_id": 16, "stage_status": "QA in progress", "release_state": "in qa", "qa_approved": False},
    "7777": {"issue_id": "7777", "title": "Seven", "project_id": 99, "stage_status": "QA in progress", "release_state": "in qa", "qa_approved": False},
    "9999": {"issue_id": "9999", "title": "Pos work", "project_id": 16, "stage_status": "QA in progress", "release_state": "in qa", "qa_approved": False},
    "9998": {"issue_id": "9998", "title": "Pos open", "project_id": 16, "stage_status": "QA in progress", "release_state": "in qa", "qa_approved": False},
    "7001": {"issue_id": "7001", "title": "Shipped without a PR", "project_id": 16, "stage_status": "Live", "release_state": "live", "qa_approved": True},
    "7002": {"issue_id": "7002", "title": "Live pending", "project_id": 16, "stage_status": "Going live pending", "release_state": "qa approved", "qa_approved": True},
    "8888": {"issue_id": "8888", "title": "Eight", "project_id": 16, "stage_status": "QA in progress", "release_state": "in qa", "qa_approved": False},
    "3333": {"issue_id": "3333", "title": "Shipped", "project_id": 16, "stage_status": "Live", "release_state": "live", "qa_approved": True},
}


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return str(tmp_path)


@pytest.fixture
def pos(tmp_path):
    """A duell-pos checkout (the fake runner recognises it by its folder name)."""
    path = tmp_path / "duell-pos"
    (path / ".git").mkdir(parents=True)
    return str(path)


def test_full_report(repo):
    calls, looked_up = [], []

    def lookup(ids):
        looked_up.append(list(ids))
        return [STATUSES[i] for i in ids]

    report = rc.build_report(repo, status_lookup=lookup, runner=fake_runner(calls), today=date(2026, 10, 16))

    assert looked_up == [["1111", "2222", "4444", "5555"]]  # one lookup for every ticket
    assert [t["issue_id"] for t in report["tickets"]] == ["1111", "2222"]  # oldest first, shipped dropped
    assert report["no_ticket"][0]["branches"] == ["hotfix-logs"]
    assert report["approval"] == {"approved": 1, "total": 3}
    assert report["pending_qa"] == [{"issue_id": "4444", "number": 10, "title": "Four"}]

    master = {p["number"]: p for p in report["prs"]["master"]}
    assert master[11]["via_qa"] == "yes" and master[11]["verdict"] == "ready"
    assert (master[12]["via_qa"], master[12]["commits_not_on_qa"]) == ("no", 2)
    assert master[13]["via_qa"] == "no" and master[13]["commits_not_on_qa"] is None  # tip not fetched
    assert master[14]["via_qa"] == "yes"
    assert master[11]["body"] == "- #1111 - One\n  - Changed the first screen"
    assert master[13]["verdict"] == "blocked" and master[13]["issue_id"] is None
    qa_pr = report["prs"]["qa-release"][0]
    assert qa_pr["problems"] == [] and qa_pr["verdict"] == "ready"  # no PROD: needed into qa-release
    assert report["mismatches"] == []

    notes = report["notes"]
    assert notes["master"]["name"] == "Release note 16.10.2026 v1.0.3.2"
    assert notes["master"]["body"].splitlines() == [
        "- #1111 - One", "  - Changed the first screen",
        "- #5555 - Five", "  - Direct fix for five",
        "- Hotfixes", "  - PROD: quick css fix",
        "- Not approved (held back)", "  - #2222 - Two — QA in progress",
    ]
    assert notes["qa"]["name"] == "QA Release note 16.10.2026 v1.0.3.2-qa"
    assert notes["qa"]["body"].splitlines() == [
        "- #1111 - One", "  - Changed the first screen",
        "- #2222 - Two", "  - Updated the second report",
        "- Hotfixes", "  - hotfix-logs",
        "- Not approved (held back)", "  - #4444 - Four — not on QA yet",
    ]
    assert notes["drafted"] == []
    # Read-only: the only commands are these.
    assert {c[:2] for c in calls} <= {("git", "remote"), ("git", "fetch"), ("git", "log"), ("git", "rev-list"), ("git", "ls-remote"), ("gh", "pr"), ("gh", "release")}
    assert all(c[2] == "list" for c in calls if c[0] == "gh")


def _guard_report(repo, pos, extra, runner=None, **kwargs):
    return rc.build_report(repo, status_lookup=lambda ids: [STATUSES[i] for i in ids], runner=runner or fake_runner([]),
                           today=date(2026, 10, 16), extra_ids=extra, project_ok=lambda pid: pid == 16, pos_dir=pos, **kwargs)


def _sections(report):
    return {r["issue_id"]: {s["repo"]: s for s in r["sections"]} for r in report["guard"]["rows"]}


def test_report_guard_covers_release_tickets_and_extras_of_this_project(repo, pos):
    looked_up = []

    def lookup(ids):
        looked_up.append(list(ids))
        return [STATUSES[i] for i in ids]

    report = rc.build_report(repo, status_lookup=lookup, runner=fake_runner([]), today=date(2026, 10, 16), pos_dir=pos,
                             extra_ids=["6666", "7777", "3333", "1111"], project_ok=lambda pid: pid == 16)
    assert looked_up == [["1111", "2222", "4444", "5555", "6666", "7777", "3333"]]  # still one lookup, no duplicates
    rows = {r["issue_id"]: r for r in report["guard"]["rows"]}
    assert set(rows) == {"1111", "2222", "6666", "3333"}  # 7777 is another project; 4444/5555 are before QA
    assert rows["1111"]["level"] == "ok" and rows["2222"]["level"] == "ok"
    admin = _sections(report)["6666"][rc.ADMIN]
    assert rows["6666"]["level"] == "missing" and admin["checks"][0]["can_create"] is True and admin["checks"][0]["repo"] == rc.ADMIN
    assert _sections(report)["3333"][rc.ADMIN]["checks"][0]["pr"]["number"] == 7  # live: merged into master
    assert report["guard"]["counts"] == {"ok": 3, "warn": 0, "missing": 1}
    assert report["guard"]["rows"][0]["issue_id"] == "6666"  # worst first


def test_only_mine_keeps_other_peoples_tickets_out_of_the_guard(repo, pos):
    report = _guard_report(repo, pos, ["6666", "9999", "7777"], only_mine=True)
    assert {r["issue_id"] for r in report["guard"]["rows"]} == {"6666", "9999"}  # not 1111/2222 (on qa-release), 7777 is another project
    assert report["approval"]["total"] == 3 and len(report["tickets"]) == 2  # the rest of the report still covers every ticket
    everyone = _guard_report(repo, pos, ["6666"])
    assert {"1111", "2222", "6666"} <= {r["issue_id"] for r in everyone["guard"]["rows"]}
    nobody = _guard_report(repo, pos, [], only_mine=True)
    assert nobody["guard"]["rows"] == [] and nobody["guard"]["checked"] == 0


def test_ticket_with_work_in_only_one_repo_is_not_red_in_the_other(repo, pos):
    report = _guard_report(repo, pos, ["9999"])
    row = {r["issue_id"]: r for r in report["guard"]["rows"]}["9999"]
    sections = _sections(report)["9999"]
    assert row["level"] == "ok" and row["summary"] == "PRs in place"
    assert sections[rc.POS]["level"] == "ok" and sections[rc.POS]["checks"][0]["pr"]["number"] == 1530
    assert sections[rc.POS]["branches"] == ["9999-Pos-work"]
    assert sections[rc.ADMIN]["level"] == "none" and "the work is in duell-pos" in sections[rc.ADMIN]["note"]
    assert sections[rc.ADMIN]["checks"] == []


def test_ticket_with_no_branch_or_pr_anywhere_is_highlighted_in_both(repo, pos):
    report = _guard_report(repo, pos, ["8888"])
    row = {r["issue_id"]: r for r in report["guard"]["rows"]}["8888"]
    sections = _sections(report)["8888"]
    assert row["level"] == "missing"
    assert row["summary"] == "No branch or PR with 8888 in its name in duell-admin or duell-pos"
    assert sections[rc.ADMIN]["level"] == sections[rc.POS]["level"] == "missing"
    assert not any(c["can_create"] for sec in sections.values() for c in sec["checks"])  # nothing to create a PR from


def test_branch_in_admin_without_a_pr_offers_create_there(repo, pos):
    report = _guard_report(repo, pos, ["6666"])
    sections = _sections(report)["6666"]
    check = sections[rc.ADMIN]["checks"][0]
    assert check["can_create"] is True and check["branches"] == ["6666-six"] and not check["no_branch"]
    assert sections[rc.POS]["level"] == "none"


def test_open_pr_only_in_pos_counts_as_work_there(repo, pos):
    STATUSES_BACKUP = STATUSES["9998"]
    report = _guard_report(repo, pos, ["9998"])
    sections = _sections(report)["9998"]
    assert sections[rc.POS]["level"] == "warn" and "still open" in sections[rc.POS]["checks"][0]["message"]
    assert sections[rc.ADMIN]["level"] == "none" and STATUSES_BACKUP


def test_pos_branch_merged_straight_into_prod_release_is_not_flagged(repo, pos):
    calls = []
    report = _guard_report(repo, pos, ["7001", "7002"], runner=fake_runner(calls))
    sections = _sections(report)
    assert sections["7001"][rc.POS]["level"] == "ok"
    assert [c["message"] for c in sections["7001"][rc.POS]["checks"]] == ["Already in prod-release"]
    # live pending, branch not merged anywhere and no PR: the Prod PR is what is missing, and it can be created
    prod = sections["7002"][rc.POS]["checks"][-1]
    assert (prod["base"], prod["level"], prod["can_create"]) == ("prod-release", "missing", True)
    api = [c for c in calls if c[:2] == ("gh", "api")]
    assert api and len(api) == len(set(api))  # each branch/base pair is asked once, even with the dry pass and the real one
    assert all(c[2].startswith("repos/Panteon-norway/duell-pos/compare/") for c in api)  # never for duell-admin


def test_unreadable_pos_is_reported_not_blamed(repo, pos):
    report = _guard_report(repo, pos, ["8888", "6666"], runner=fake_runner([], pos_ok=False))
    guard = report["guard"]
    assert "duell-pos" in guard["errors"] and "unreachable" in guard["errors"]["duell-pos"]
    sections = _sections(report)
    # 8888 has nothing in admin, but the work may be in the repo that couldn't be read: not "missing everywhere"
    assert sections["8888"][rc.POS]["level"] == "unknown" and sections["8888"][rc.ADMIN]["level"] == "none"
    row = {r["issue_id"]: r for r in guard["rows"]}["8888"]
    assert row["level"] == "warn" and "duell-pos" in row["summary"]
    assert {r["issue_id"]: r for r in guard["rows"]}["6666"]["level"] == "missing"  # admin side is still judged


def test_missing_pos_checkout_is_an_error_entry(repo, tmp_path):
    report = _guard_report(repo, str(tmp_path / "nope"), ["6666"])
    assert "No duell-pos checkout" in report["guard"]["errors"]["duell-pos"]
    assert report["guard"]["rows"][0]["level"] == "missing"  # duell-admin still checked


def test_report_guard_keeps_the_button_when_branches_cannot_be_listed(repo, pos):
    inner = fake_runner([])

    def runner(args, cwd, timeout):
        if tuple(args)[:2] == ("git", "ls-remote") and not cwd.endswith("duell-pos"):
            return 128, "", "fatal: unable to access"
        return inner(args, cwd, timeout)

    report = _guard_report(repo, pos, ["8888"], runner=runner)
    check = _sections(report)["8888"][rc.ADMIN]["checks"][0]
    assert check["can_create"] is True and check["no_branch"] is False and check["message"] == "No PR into qa-release"


def test_report_guard_without_a_filter_keeps_every_extra(repo, pos):
    report = rc.build_report(repo, status_lookup=lambda ids: [STATUSES[i] for i in ids], runner=fake_runner([]),
                             today=date(2026, 10, 16), extra_ids=["7777"], pos_dir=pos)
    assert "7777" in {r["issue_id"] for r in report["guard"]["rows"]}


def test_timeline_failure_keeps_git_side(repo):
    def lookup(ids):
        raise PermissionError("Panteon credentials rejected")

    report = rc.build_report(repo, status_lookup=lookup, runner=fake_runner([]), today=date(2026, 10, 16))
    assert report["timeline_error"] == "Panteon credentials rejected"
    assert report["approval"]["approved"] == 0 and len(report["tickets"]) == 2
    assert report["guard"]["rows"] == [] and report["guard"]["timeline_error"] == "Panteon credentials rejected"


def test_no_fetch_skips_git_fetch(repo):
    calls = []
    rc.build_report(repo, status_lookup=None, fetch=False, runner=fake_runner(calls), today=date(2026, 10, 16))
    assert not any(c[:2] == ("git", "fetch") for c in calls)


def test_rejects_other_repos(repo, tmp_path):
    def runner(args, cwd, timeout):
        return 0, "git@github.com:someone/else.git\n", ""
    with pytest.raises(rc.ReleaseCheckError, match="not a duell-admin checkout"):
        rc.build_report(repo, runner=runner)
    with pytest.raises(rc.ReleaseCheckError, match="No duell-admin checkout"):
        rc.build_report(str(tmp_path / "missing"), runner=runner)


def test_git_failure_raises(repo):
    def runner(args, cwd, timeout):
        if args[:2] == ("git", "fetch"):
            return 128, "", "fatal: could not read from remote\n"
        return fake_runner([])(args, cwd, timeout)
    with pytest.raises(rc.ReleaseCheckError, match="could not read from remote"):
        rc.build_report(repo, runner=runner)


# ---- dashboard endpoints ------------------------------------------------
@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("PANTEON_BEARER_TOKEN", "tok")
    seen = {}

    def builder(repo_dir=None, status_lookup=None, **kwargs):
        seen["repo_dir"] = repo_dir
        return {"approval": {"approved": 1, "total": 2}}

    app = dashboard.create_app(user_id=135, base_dir=str(tmp_path), repo_dir="/x/duell-admin", release_builder=builder)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    client.seen = seen
    return client


def test_release_get_is_cache_only(client):
    assert client.get("/api/release").json() == {"report": None}
    assert client.seen == {}


def test_release_refresh_needs_header(client):
    assert client.post("/api/release").status_code == 403
    response = client.post("/api/release", headers={"X-Timeline-Desk": "1"})
    assert response.status_code == 200 and response.json()["report"]["approval"]["approved"] == 1
    assert client.seen["repo_dir"] == "/x/duell-admin"
    assert client.get("/api/release").json()["report"]["approval"]["total"] == 2


def test_release_errors_map_to_502(monkeypatch, tmp_path):
    def builder(**kwargs):
        raise rc.ReleaseCheckError("`gh` is not installed.")
    app = dashboard.create_app(user_id=135, base_dir=str(tmp_path), release_builder=builder)
    response = TestClient(app, base_url="http://127.0.0.1:8765").post("/api/release", headers={"X-Timeline-Desk": "1"})
    assert response.status_code == 502 and response.json()["error"] == "`gh` is not installed."


def test_web_files_revalidate(client):
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"


# ---- stage vs PR guard --------------------------------------------------
def _found(number, state, base="master"):
    return {"number": number, "url": f"https://github.com/x/pull/{number}", "title": "", "branch": "b", "state": state}


HAS_BRANCH = {"5000": ["5000-fix"]}


def _facts(prs=None, branches=None, on_qa=False):
    return {"pr_index": {"5000": prs} if prs else {}, "branch_index": branches, "on_qa": {"5000"} if on_qa else set()}


def _guard(state, qa=(), master=(), on_qa=False, branches=HAS_BRANCH, pos=None, errors=None):
    """The one ticket's row, with duell-admin's checks at row["checks"] (duell-pos is empty unless given)."""
    admin = _facts({"qa-release": list(qa), "master": list(master)}, branches, on_qa)
    ticket = {"issue_id": "5000", "title": "T", "release_state": state, "stage_status": "x"}
    result = rc.pr_guard([ticket], {rc.ADMIN: admin, rc.POS: pos if pos is not None else _facts(branches={})}, errors)
    if not result["rows"]:
        return None
    row = result["rows"][0]
    row["checks"] = row["sections"][0]["checks"]
    return row


class TestPrGuard:
    def test_before_qa_is_not_checked(self):
        result = rc.pr_guard([{"issue_id": "5000", "release_state": "before qa"}], {}, {})
        assert result == {"rows": [], "counts": {"ok": 0, "warn": 0, "missing": 0}, "checked": 0, "skipped": 1, "errors": {}}

    @pytest.mark.parametrize("state", ["in qa", "qa blocked"])
    def test_in_qa_needs_a_qa_pr(self, state):
        row = _guard(state)
        assert row["level"] == "missing" and row["summary"] == "duell-admin: No PR into qa-release"
        assert row["checks"][0]["can_create"] is True
        assert _guard(state, qa=[_found(1, "merged")])["level"] == "ok"

    def test_create_needs_a_branch_with_the_ticket_number(self):
        none = _guard("in qa", branches={"4999": ["4999-x"]})  # other tickets' branches don't count
        assert none["level"] == "missing" and none["checks"] == []  # nowhere: both repos highlighted, nothing to create from
        assert none["summary"] == "No branch or PR with 5000 in its name in duell-admin or duell-pos"
        has = _guard("in qa")
        assert has["checks"][0]["can_create"] is True and has["checks"][0]["branches"] == ["5000-fix"]
        unknown = _guard("in qa", branches=None)  # not listed (offline): a branch is assumed, the draft checks again
        assert unknown["checks"][0]["can_create"] is True and unknown["summary"] == "duell-admin: No PR into qa-release"

    def test_a_later_pr_without_a_branch_cannot_be_created(self):
        # the QA PR is merged (so the ticket is known here), but the prod PR has no branch left to be made from
        row = _guard("qa approved", qa=[_found(1, "merged")], branches={})
        master = row["checks"][1]
        assert row["level"] == "missing" and master["no_branch"] is True and master["can_create"] is False
        assert master["message"] == "Live pending, but there is no PR into master, and no branch with 5000 in its name on GitHub yet"

    def test_in_qa_with_an_open_qa_pr_is_a_warning(self):
        row = _guard("in qa", qa=[_found(1, "open")])
        assert row["level"] == "warn" and "still open" in row["summary"]
        assert row["checks"][0]["can_create"] is False

    def test_merged_into_qa_release_by_git_counts(self):
        row = _guard("in qa", on_qa=True)
        assert row["level"] == "ok" and row["checks"][0]["message"] == "On qa-release"

    def test_live_pending_needs_a_prod_pr(self):
        row = _guard("qa approved", qa=[_found(1, "merged")])
        assert row["level"] == "missing"
        assert row["summary"] == "duell-admin: Live pending, but there is no PR into master"
        assert [c["base"] for c in row["checks"] if c["can_create"]] == ["master"]
        row = _guard("qa approved", qa=[_found(1, "merged")], master=[_found(2, "open")])
        assert row["level"] == "ok" and "Live pending: PR #2" in row["checks"][1]["message"]

    def test_qa_approved_without_any_qa_pr_flags_qa_release(self):
        row = _guard("qa approved", master=[_found(2, "open")])
        assert row["level"] == "missing" and row["checks"][0]["base"] == "qa-release" and row["checks"][0]["can_create"]

    def test_straight_to_master_waives_the_qa_pr(self):
        assert _guard("qa approved", master=[_found(2, "merged")])["level"] == "ok"

    def test_live_needs_a_merged_prod_pr(self):
        assert _guard("live", master=[_found(2, "merged")])["level"] == "ok"
        row = _guard("live", master=[_found(2, "open")])
        assert row["level"] == "warn" and "isn't merged" in row["summary"]
        row = _guard("live")
        assert row["level"] == "missing" and not row["checks"][0]["can_create"]  # already shipped: a reminder, no button
        assert "no branch" not in row["summary"]

    def test_merged_beats_open_and_rows_sort_worst_first(self):
        assert _guard("live", master=[_found(9, "open"), _found(3, "merged")])["checks"][0]["pr"]["number"] == 3
        facts = {rc.ADMIN: {"pr_index": {"1": {"qa-release": [_found(1, "merged")]}}, "branch_index": {"1": ["1-a"], "2": ["2-a"]}, "on_qa": set()},
                 rc.POS: _facts(branches={})}
        tickets = [{"issue_id": "1", "release_state": "in qa"}, {"issue_id": "2", "release_state": "in qa"}]
        assert [r["issue_id"] for r in rc.pr_guard(tickets, facts)["rows"]] == ["2", "1"]

    def test_duell_pos_uses_qa_release_for_qa_and_prod_release_for_production(self):
        pos = _facts({"qa-release": [_found(1530, "merged")], "prod-release": []}, {"5000": ["5000-Pos"]})
        row = _guard("qa approved", pos=pos, branches={})  # live pending: the Prod server still needs its PR
        section = row["sections"][1]
        assert [(c["base"], c["level"]) for c in section["checks"]] == [("qa-release", "ok"), ("prod-release", "missing")]
        assert section["checks"][1]["can_create"] is True and row["summary"] == "duell-pos: Live pending, but there is no PR into prod-release"
        done = _facts({"qa-release": [_found(1530, "merged")], "prod-release": [_found(1600, "open")]}, {"5000": ["5000-Pos"]})
        assert _guard("qa approved", pos=done, branches={})["level"] == "ok"
        live = _guard("live", pos=_facts({"prod-release": [_found(1600, "merged")]}, {"5000": ["5000-Pos"]}), branches={})
        assert live["level"] == "ok" and live["sections"][1]["checks"][0]["message"] == "PR #1600 merged into prod-release"
        assert row["sections"][0]["level"] == "none"

    def test_a_branch_already_merged_into_the_target_counts_without_a_pr(self):
        asked = []

        def contains(base, branch):
            asked.append((base, branch))
            return base == "prod-release"  # merged straight into prod-release, no PR ever opened

        pos = {**_facts({"qa-release": [_found(1530, "merged")]}, {"5000": ["5000-Pos"]}), "contains": contains}
        live = _guard("live", pos=pos, branches={})
        assert live["level"] == "ok" and live["sections"][1]["checks"][0]["message"] == "Already in prod-release"
        pending = _guard("qa approved", pos=pos, branches={})
        assert pending["level"] == "ok" and [c["message"] for c in pending["sections"][1]["checks"]] == ["PR #1530 merged into qa-release", "Already in prod-release"]
        assert ("qa-release", "5000-Pos") not in asked  # a merged PR already answers; GitHub isn't asked

    def test_a_branch_not_merged_yet_still_needs_its_pr(self):
        pos = {**_facts({}, {"5000": ["5000-Pos"]}), "contains": lambda base, branch: False}
        row = _guard("in qa", pos=pos, branches={})
        assert row["level"] == "missing" and row["sections"][1]["checks"][0]["can_create"] is True

    def test_pos_branch_without_pr_offers_create_in_pos(self):
        pos = _facts({"qa-release": [], "master": []}, {"5000": ["5000-Pos"]})
        row = _guard("in qa", pos=pos, branches={})
        check = row["sections"][1]["checks"][0]
        assert row["level"] == "missing" and check["can_create"] is True and check["repo"] == rc.POS
        assert row["summary"] == "duell-pos: No PR into qa-release"

    def test_pr_index_matches_open_and_merged_by_ticket(self):
        index = rc.build_pr_index(
            {"qa-release": [{"number": 1, "title": "QA: 5000 x", "headRefName": "5000-x"}], "master": []},
            {"qa-release": [], "master": [{"number": 2, "title": "PROD: #5000 x", "headRefName": "5000-x"},
                                          {"number": 3, "title": "no ticket", "headRefName": "css"}]})
        assert [(p["number"], p["state"]) for p in index["5000"]["qa-release"]] == [(1, "open")]
        assert [(p["number"], p["state"]) for p in index["5000"]["master"]] == [(2, "merged")]
        assert list(index) == ["5000"]

    def test_branch_index_from_ls_remote(self):
        text = "a\trefs/heads/6110-fix-x\nb\trefs/heads/temp-6110-y\nc\trefs/heads/6110\nd\trefs/heads/16110-z\ne\trefs/heads/master\nf\trefs/heads/misc-fix\n"
        assert rc.parse_branch_index(text) == {"6110": ["6110", "6110-fix-x", "temp-6110-y"], "16110": ["16110-z"]}


# ---- draft and create a PR ----------------------------------------------
def pr_runner(calls, branches=("5000-fix",), open_prs=None, other_prs=None, log="Fix the thing\nFix the thing\nAdd a test\n", ai=None,
              names="", env_diff="", entity_diffs=None):
    ai = ai or {}

    def run(args, cwd, timeout):
        args = tuple(args)
        calls.append(args)
        if args[:3] == ("git", "remote", "get-url"):
            name = "duell-pos" if cwd.endswith("duell-pos") else "duell-admin"
            return 0, f"git@github.com:Panteon-norway/{name}.git\n", ""
        if args[:3] == ("git", "ls-remote", "--heads"):
            lines = [f"abc\trefs/heads/{b}" for b in (*branches, "master", "55000-other", "qa-release")]
            return 0, "\n".join(lines) + "\n", ""
        if args[:2] == ("git", "fetch"):
            return 0, "", ""
        if args[:2] == ("git", "log"):
            return 0, log, ""
        if args[:2] == ("git", "diff"):
            if "--name-status" in args:
                return 0, names, ""
            if "-U0" in args:
                return 0, env_diff, ""
            return 0, (entity_diffs or {}).get(args[-1], ""), ""
        if args[0].endswith("claude"):
            return ai.get("code", 0), ai.get("out", "- #5000 - Fix the thing\n  - Fixed the thing\n"), ai.get("err", "")
        if args[:3] == ("gh", "pr", "create"):
            return 0, "Creating pull request...\nhttps://github.com/Panteon-norway/duell-admin/pull/77\n", ""
        if args[:3] == ("gh", "pr", "list"):
            state = args[args.index("--state") + 1]
            if state == "open" and "body" not in args[-3]:
                return 0, json.dumps(open_prs or []), ""
            return 0, json.dumps((other_prs or {}).get(state, [])), ""
        return 1, "", f"unexpected {args}"
    return run


class TestDuellPos:
    BODY = "- #5000 - x\n  - y\n"

    def test_draft_reads_commits_but_no_env_or_entity_analysis(self, pos):
        calls = []
        draft = rc.draft_pr(pos, "5000", "qa-release", "Fix", runner=pr_runner(calls), repo_name=rc.POS)
        assert draft["repo"] == rc.POS and draft["title"] == "QA: #5000 Fix" and draft["source"] == "commits"
        assert draft["analysis"] is None
        assert not any(c[:2] == ("git", "diff") for c in calls)  # .env.dist and shared/src/Entity are duell-admin's
        assert all("duell-admin" not in " ".join(c) for c in calls if c[0] == "gh")

    def test_pos_bases_are_qa_release_and_prod_release(self, pos):
        with pytest.raises(ValueError, match="duell-pos can only go into qa-release or prod-release"):
            rc.draft_pr(pos, "5000", "master", "Fix", runner=pr_runner([]), repo_name=rc.POS)
        draft = rc.draft_pr(pos, "5000", "prod-release", "Fix", runner=pr_runner([]), repo_name=rc.POS)
        assert draft["title"] == "PROD: #5000 Fix" and draft["base"] == "prod-release"
        with pytest.raises(ValueError, match="Unknown repository"):
            rc.draft_pr(pos, "5000", "qa-release", "Fix", runner=pr_runner([]), repo_name="duell-other")

    def test_create_targets_the_pos_repo(self, pos):
        calls = []
        result = rc.create_pr(pos, "5000", "qa-release", "5000-fix", "QA: #5000 x", self.BODY, runner=pr_runner(calls), repo_name=rc.POS)
        assert result["repo"] == rc.POS
        (create,) = [c for c in calls if c[:3] == ("gh", "pr", "create")]
        assert create[create.index("--repo") + 1] == "Panteon-norway/duell-pos"

    def test_the_checkout_must_be_pos(self, repo):
        with pytest.raises(rc.ReleaseCheckError, match="not a duell-pos checkout"):
            rc.create_pr(repo, "5000", "qa-release", "5000-fix", "QA: #5000 x", self.BODY, runner=pr_runner([]), repo_name=rc.POS)

    def test_ai_description_has_no_sql_part_for_pos(self, pos):
        calls = []
        result = rc.ai_pr_body(pos, "5000", "qa-release", "5000-fix", "Fix", runner=pr_runner(calls), repo_name=rc.POS)
        assert result["sql"] == 0 and not any(c[:2] == ("git", "diff") for c in calls)


class TestDraftAndCreate:
    def test_draft_from_commits(self, repo):
        calls = []
        draft = rc.draft_pr(repo, "5000", "qa-release", "Fix the thing", runner=pr_runner(calls))
        assert draft["title"] == "QA: #5000 Fix the thing"
        assert draft["body"] == "- #5000 - Fix the thing\n  - Fix the thing\n  - Add a test\n"  # duplicates dropped
        assert draft["source"] == "commits" and draft["branches"] == ["5000-fix"] and draft["head"] == "5000-fix"
        assert draft["existing"] == []
        assert ("git", "log", "--no-merges", "--format=%s", "origin/qa-release..origin/5000-fix") in calls
        assert not any(c[:2] == ("git", "push") for c in calls)

    def test_prod_draft_reuses_the_qa_pr_entry(self, repo):
        other = {"merged": [{"number": 9, "title": "QA: 5000", "headRefName": "5000-fix",
                             "body": "- #5000 - Old title\n  - Changed the report\n    - Sub point\n"}]}
        draft = rc.draft_pr(repo, "5000", "master", "Fix the thing", runner=pr_runner([], other_prs=other))
        assert draft["title"] == "PROD: #5000 Fix the thing"
        assert draft["body"] == "- #5000 - Fix the thing\n  - Changed the report\n    - Sub point\n"
        assert draft["source"] == "PR #9"

    def test_draft_reports_an_open_pr_and_a_missing_branch(self, repo):
        draft = rc.draft_pr(repo, "5000", "master", "x", runner=pr_runner(
            [], branches=(), open_prs=[{"number": 5, "title": "PROD: #5000 x", "headRefName": "5000-fix", "url": "u"}]))
        assert draft["branches"] == [] and draft["head"] is None and [p["number"] for p in draft["existing"]] == [5]
        assert draft["body"] == "- #5000 - x\n"

    def test_branches_must_belong_to_the_ticket(self, repo):
        draft = rc.draft_pr(repo, "5000", "master", "x", runner=pr_runner([], branches=("5000-a", "temp-5000-b")))
        assert draft["branches"] == ["5000-a", "temp-5000-b"] and draft["head"] is None  # 55000-other is not #5000

    def test_create_runs_gh_pr_create_with_fixed_arguments(self, repo):
        calls = []
        body = "- #5000 - Fix the thing\n  - Fixed it\n"
        result = rc.create_pr(repo, "5000", "qa-release", "5000-fix", "QA: #5000 Fix the thing", body, runner=pr_runner(calls))
        assert result == {"url": "https://github.com/Panteon-norway/duell-admin/pull/77", "number": 77,
                          "base": "qa-release", "head": "5000-fix", "title": "QA: #5000 Fix the thing", "repo": "duell-admin"}
        create = [c for c in calls if c[:3] == ("gh", "pr", "create")]
        assert create == [("gh", "pr", "create", "--repo", "Panteon-norway/duell-admin", "--base", "qa-release",
                           "--head", "5000-fix", "--title", "QA: #5000 Fix the thing", "--body", body.strip())]
        assert not any(c[0] == "git" and c[1] in ("push", "merge", "commit") for c in calls)

    @pytest.mark.parametrize("kwargs,message", [
        ({"issue": "abc"}, "Issue ID must be a number"),
        ({"base": "development"}, "can only go into"),
        ({"title": "  "}, "Give the PR a title"),
        ({"body": "no entry"}, "needs a `- #5000 - <title>` entry"),
        ({"head": "someone-elses"}, "isn't one of #5000's branches"),
    ])
    def test_create_refuses_bad_input(self, repo, kwargs, message):
        args = {"issue": "5000", "base": "master", "head": "5000-fix", "title": "PROD: #5000 x", "body": "- #5000 - x\n  - y\n", **kwargs}
        calls = []
        with pytest.raises(ValueError, match=message):
            rc.create_pr(repo, args["issue"], args["base"], args["head"], args["title"], args["body"], runner=pr_runner(calls))
        assert not any(c[:3] == ("gh", "pr", "create") for c in calls)

    def test_create_refuses_when_the_branch_is_not_on_origin(self, repo):
        with pytest.raises(ValueError, match="Push it first"):
            rc.create_pr(repo, "5000", "master", "5000-fix", "PROD: #5000 x", "- #5000 - x\n  - y\n", runner=pr_runner([], branches=()))

    def test_create_refuses_a_duplicate(self, repo):
        calls = []
        runner = pr_runner(calls, open_prs=[{"number": 5, "title": "PROD: #5000 x", "headRefName": "5000-fix", "url": "u"}])
        with pytest.raises(ValueError, match="PR #5 into master is already open"):
            rc.create_pr(repo, "5000", "master", "5000-fix", "PROD: #5000 x", "- #5000 - x\n  - y\n", runner=runner)
        assert not any(c[:3] == ("gh", "pr", "create") for c in calls)

    def test_create_rejects_other_repos(self, repo):
        def runner(args, cwd, timeout):
            return 0, "git@github.com:someone/else.git\n", ""
        with pytest.raises(rc.ReleaseCheckError, match="not a duell-admin checkout"):
            rc.create_pr(repo, "5000", "master", "5000-fix", "PROD: #5000 x", "- #5000 - x\n  - y\n", runner=runner)


# ---- PR guard and PR endpoints -------------------------------------------
@pytest.fixture
def pr_client(monkeypatch, tmp_path):
    monkeypatch.setenv("PANTEON_BEARER_TOKEN", "tok")
    (tmp_path / ".git").mkdir()
    seen = {"builds": [], "calls": []}

    def builder(repo_dir=None, status_lookup=None, **kwargs):
        seen["builds"].append(kwargs)
        return {"generated_at": "2026-10-16T12:00:00+02:00",
                "guard": {"rows": [{"issue_id": "5000", "level": "missing"}], "counts": {"ok": 0, "warn": 0, "missing": 1},
                          "checked": 1, "skipped": 0}}

    config = {"claude": "claude",
              "repos": {"15033": {"id": 16, "name": "duell-admin", "path": "/x", "base": "master", "worktrees": "/x-wt"},
                        "999": {"id": 77, "name": "other-repo", "path": "/y", "base": "main", "worktrees": "/y-wt"}}}
    pos_path = tmp_path / "duell-pos"
    (pos_path / ".git").mkdir(parents=True)
    app = dashboard.create_app(user_id=135, base_dir=str(tmp_path), repo_dir=str(tmp_path), release_builder=builder,
                               work_config=lambda: config, pr_runner=pr_runner(seen["calls"]), pos_dir=str(pos_path))
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    client.seen = seen
    return client


def test_pr_guard_is_cache_only(pr_client):
    assert pr_client.get("/api/pr-guard").json() == {"guard": None, "generated_at": None}
    pr_client.post("/api/release", headers={"X-Timeline-Desk": "1"})
    body = pr_client.get("/api/pr-guard").json()
    assert body["guard"]["counts"]["missing"] == 1 and body["generated_at"].startswith("2026-10-16")


def test_release_build_gets_only_duell_admin_projects_for_the_guard(pr_client, monkeypatch):
    import task_events
    monkeypatch.setattr(task_events, "list_watchlist", lambda base_dir, active_only=False: [{"issue_number": 8001}])
    pr_client.post("/api/release", headers={"X-Timeline-Desk": "1"})
    kwargs = pr_client.seen["builds"][-1]
    assert "8001" in kwargs["extra_ids"] and kwargs["only_mine"] is True
    assert kwargs["project_ok"](16) and kwargs["project_ok"]("16")
    assert not kwargs["project_ok"](77) and not kwargs["project_ok"](None)


def test_pr_draft_endpoint(pr_client):
    response = pr_client.get("/api/pr/draft", params={"issue": "5000", "base": "qa-release", "title": "Fix the thing"})
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "QA: #5000 Fix the thing" and body["head"] == "5000-fix"
    assert pr_client.get("/api/pr/draft", params={"issue": "x", "base": "master"}).status_code == 400
    assert pr_client.get("/api/pr/draft", params={"issue": "5000", "base": "main"}).status_code == 400


def test_pr_create_needs_the_header_and_creates_nothing_without_it(pr_client):
    payload = {"issue": "5000", "base": "master", "head": "5000-fix", "title": "PROD: #5000 x", "body": "- #5000 - x\n  - y\n"}
    assert pr_client.post("/api/pr/create", json=payload).status_code == 403
    assert not any(c[:3] == ("gh", "pr", "create") for c in pr_client.seen["calls"])


def test_pr_create_endpoint(pr_client):
    payload = {"issue": "5000", "base": "master", "head": "5000-fix", "title": "PROD: #5000 x", "body": "- #5000 - x\n  - y\n"}
    response = pr_client.post("/api/pr/create", json=payload, headers={"X-Timeline-Desk": "1"})
    assert response.status_code == 200 and response.json()["number"] == 77
    bad = pr_client.post("/api/pr/create", json={**payload, "head": "other"}, headers={"X-Timeline-Desk": "1"})
    assert bad.status_code == 400 and "isn't one of #5000's branches" in bad.json()["error"]
    assert pr_client.post("/api/pr/create", json={}, headers={"X-Timeline-Desk": "1"}).status_code == 400


# ---- AI-written description ---------------------------------------------
class TestAiBody:
    def test_runs_claude_with_no_tools_and_only_title_and_commits(self, repo):
        calls = []
        result = rc.ai_pr_body(repo, "5000", "qa-release", "5000-fix", "Fix the thing", claude="/bin/claude", runner=pr_runner(calls))
        assert result["body"] == "- #5000 - Fix the thing\n  - Fixed the thing\n"
        assert (result["source"], result["commits"], result["sql"], result["warnings"]) == ("ai", 2, 0, [])
        (call,) = [c for c in calls if c[0] == "/bin/claude"]
        assert call[1] == "-p" and call[3:5] == ("--tools", "")  # the prompt first, tools off
        assert "--no-session-persistence" in call and "--strict-mcp-config" in call
        prompt = call[2]
        assert "Ticket title: Fix the thing" in prompt and "- Fix the thing\n- Add a test" in prompt  # duplicates dropped
        diffs = [c for c in calls if c[:2] == ("git", "diff")]
        assert [c[2] for c in diffs] == ["--name-status"]  # nothing but the file list: no entity or env file changed
        assert not any(c[:3] == ("gh", "pr", "create") for c in calls)

    def test_answer_is_cleaned(self, repo):
        fenced = "```markdown\n- #5000 - Fix the thing\n  - Fixed the thing\n```\n"
        assert rc.ai_pr_body(repo, "5000", "master", "5000-fix", "Fix the thing", runner=pr_runner([], ai={"out": fenced}))["body"] == \
            "- #5000 - Fix the thing\n  - Fixed the thing\n"
        bare = "- Fixed the thing\n* Added a test\n"
        assert rc.ai_pr_body(repo, "5000", "master", "5000-fix", "Fix the thing", runner=pr_runner([], ai={"out": bare}))["body"] == \
            "- #5000 - Fix the thing\n  - Fixed the thing\n  - Added a test\n"

    def test_failures(self, repo):
        with pytest.raises(rc.ReleaseCheckError, match="`claude` failed: not logged in"):
            rc.ai_pr_body(repo, "5000", "master", "5000-fix", "x", runner=pr_runner([], ai={"code": 1, "err": "oops\nnot logged in\n"}))
        with pytest.raises(rc.ReleaseCheckError, match="empty description"):
            rc.ai_pr_body(repo, "5000", "master", "5000-fix", "x", runner=pr_runner([], ai={"out": "  \n"}))
        with pytest.raises(ValueError, match="nothing to describe"):
            rc.ai_pr_body(repo, "5000", "master", "5000-fix", "x", runner=pr_runner([], log=""))
        calls = []
        with pytest.raises(ValueError, match="isn't one of #5000's branches"):
            rc.ai_pr_body(repo, "5000", "master", "other", "x", runner=pr_runner(calls))
        assert not any(c[0].endswith("claude") for c in calls)


def test_pr_describe_endpoint(pr_client):
    payload = {"issue": "5000", "base": "master", "head": "5000-fix", "title": "Fix the thing"}
    assert pr_client.post("/api/pr/describe", json=payload).status_code == 403
    assert not any(c[0].endswith("claude") for c in pr_client.seen["calls"])
    response = pr_client.post("/api/pr/describe", json=payload, headers={"X-Timeline-Desk": "1"})
    assert response.status_code == 200 and response.json()["body"].startswith("- #5000 - Fix the thing")
    assert pr_client.post("/api/pr/describe", json={}, headers={"X-Timeline-Desk": "1"}).status_code == 400


# ---- environment and database changes of a branch ------------------------
ENV_DIFF = """--- a/.env.dist
+++ b/.env.dist
@@ -20,2 +20,6 @@
+ZENDESK_API_EMAIL=
+ZENDESK_API_URL="https://x.zendesk.com"
+# SERVER_PHP_PATH: php
-APP_LOCALE=en
+APP_LOCALE=nb
-OLD_FLAG=1
"""
ENTITY_DIFF = """diff --git a/shared/src/Entity/Client/Orders.php b/shared/src/Entity/Client/Orders.php
index 1..2 100644
--- a/shared/src/Entity/Client/Orders.php
+++ b/shared/src/Entity/Client/Orders.php
@@ -100,3 +100,6 @@ class Orders
+    #[ORM\Column(name: 'one_pager_type', type: 'string', length: 50, nullable: true)]
+    private ?string $onePagerType = null;
"""
NAMES = "\n".join([
    "M\t.env.dist", "M\tshared/src/Entity/Client/Orders.php", "M\tshared/src/Entity/Client/Product.php",
    "A\tshared/src/Entity/Admin/Gadget.php", "M\tbackoffice/src/Controller/OrderController.php", "M\tREADME.md"])
ENTITY_DIFFS = {
    "shared/src/Entity/Client/Orders.php": ENTITY_DIFF,
    "shared/src/Entity/Client/Product.php": "diff --git a/p b/p\nold mode 100755\nnew mode 100644\n",  # permissions only
    "shared/src/Entity/Admin/Gadget.php": "@@ -0,0 +1,5 @@\n+#[ORM\\Entity]\n+class Gadget {}\n",
}


class TestBranchChanges:
    def test_parse_env_changes(self):
        assert rc.parse_env_changes(ENV_DIFF) == {"added": ["ZENDESK_API_EMAIL", "ZENDESK_API_URL"],
                                                  "changed": ["APP_LOCALE"], "removed": ["OLD_FLAG"]}

    def test_branch_changes_finds_env_and_mapping_changes_only(self, repo):
        calls = []
        runner = pr_runner(calls, names=NAMES, env_diff=ENV_DIFF, entity_diffs=ENTITY_DIFFS)
        changes = rc.branch_changes(rc.open_repo(repo, runner), "master", "5000-fix")
        assert changes["env"]["files"] == [".env.dist"] and changes["env"]["added"] == ["ZENDESK_API_EMAIL", "ZENDESK_API_URL"]
        assert [(e["path"], e["db"], e["status"]) for e in changes["entities"]] == [
            ("shared/src/Entity/Client/Orders.php", "Client DB", "M"),
            ("shared/src/Entity/Admin/Gadget.php", "Admin DB", "A")]  # Product.php: mode change only; controller: not an entity
        assert ("git", "diff", "--name-status", "--no-renames", "origin/master...origin/5000-fix") in calls
        assert not any("Controller" in " ".join(c) for c in calls)  # no other file's diff is read

    def test_sql_part_parsing(self):
        text = "CLIENT: ALTER TABLE orders ADD one_pager_type VARCHAR(50) DEFAULT NULL;\nADMIN: create table gadget (id INT)\n" \
               "client: drop table x\nCLIENT: SELECT 1\nsome chatter\nCLIENT: ALTER TABLE orders ADD one_pager_type VARCHAR(50) DEFAULT NULL\n"
        assert rc.parse_ai_sql(text) == {"CLIENT": ["ALTER TABLE orders ADD one_pager_type VARCHAR(50) DEFAULT NULL", "drop table x"],
                                         "ADMIN": ["create table gadget (id INT)"]}

    def test_draft_adds_env_variables_with_empty_values(self, repo):
        draft = rc.draft_pr(repo, "5000", "master", "Fix", runner=pr_runner([], names=NAMES, env_diff=ENV_DIFF, entity_diffs=ENTITY_DIFFS))
        assert draft["body"].splitlines()[-5:] == [
            "  - Environment variables (.env.dist), set before release",
            "    - ZENDESK_API_EMAIL=", "    - ZENDESK_API_URL=", "    - APP_LOCALE=", "    - OLD_FLAG (removed)"]
        assert [e["path"] for e in draft["analysis"]["entities"]] == ["shared/src/Entity/Client/Orders.php", "shared/src/Entity/Admin/Gadget.php"]
        assert rc.release_entry(draft["body"], "5000")["bullets"][-1] == (2, "OLD_FLAG (removed)")  # survives into the release note

    def test_draft_from_an_earlier_pr_keeps_its_reviewed_text(self, repo):
        other = {"merged": [{"number": 9, "title": "QA: 5000", "headRefName": "5000-fix", "body": "- #5000 - T\n  - Changed the report\n"}]}
        draft = rc.draft_pr(repo, "5000", "master", "Fix", runner=pr_runner([], other_prs=other, names=NAMES, env_diff=ENV_DIFF))
        assert "ZENDESK" not in draft["body"] and draft["analysis"]["env"]["added"]  # reported, not appended


class TestAiSql:
    SQL_OUT = ("- #5000 - Fix the thing\n  - Added the one pager type to orders\n---SQL---\n"
               "CLIENT: ALTER TABLE orders ADD one_pager_type VARCHAR(50) DEFAULT NULL\n"
               "ADMIN: CREATE TABLE gadget (id INT AUTO_INCREMENT NOT NULL, PRIMARY KEY(id))\n")

    def run_ai(self, repo, out, calls=None, **kwargs):
        runner = pr_runner(calls if calls is not None else [], names=NAMES, env_diff=ENV_DIFF, entity_diffs=ENTITY_DIFFS, ai={"out": out}, **kwargs)
        return rc.ai_pr_body(repo, "5000", "master", "5000-fix", "Fix the thing", runner=runner)

    def test_body_gets_env_variables_and_sql_sections(self, repo):
        calls = []
        result = self.run_ai(repo, self.SQL_OUT, calls)
        assert result["body"].splitlines() == [
            "- #5000 - Fix the thing", "  - Added the one pager type to orders",
            "  - Environment variables (.env.dist), set before release",
            "    - ZENDESK_API_EMAIL=", "    - ZENDESK_API_URL=", "    - APP_LOCALE=", "    - OLD_FLAG (removed)",
            "  - SQL changes (Client DB)", "    - ALTER TABLE orders ADD one_pager_type VARCHAR(50) DEFAULT NULL",
            "  - SQL changes (Admin DB)", "    - CREATE TABLE gadget (id INT AUTO_INCREMENT NOT NULL, PRIMARY KEY(id))"]
        assert result["sql"] == 2 and result["warnings"] == []
        prompt = [c for c in calls if c[0].endswith("claude")][0][2]
        assert "one_pager_type" in prompt and "=== shared/src/Entity/Client/Orders.php (Client DB, changed) ===" in prompt
        assert "(Admin DB, new file)" in prompt and "OrderController" not in prompt and "ZENDESK" not in prompt  # env is read locally
        assert rc.release_entry(result["body"], "5000")["indent_ok"] is True

    def test_none_and_missing_sql(self, repo):
        none = self.run_ai(repo, "- #5000 - T\n  - Did it\n---SQL---\nNONE\n")
        assert "SQL changes" not in none["body"] and none["warnings"] == []
        missing = self.run_ai(repo, "- #5000 - T\n  - Did it\n")
        assert "SQL changes" not in missing["body"] and "wrote no SQL" in missing["warnings"][0]

    def test_no_entity_changes_means_no_sql_prompt(self, repo):
        calls = []
        runner = pr_runner(calls, names="M\tREADME.md\n", ai={"out": "- #5000 - T\n  - Did it\n---SQL---\nCLIENT: DROP TABLE orders\n"})
        result = rc.ai_pr_body(repo, "5000", "master", "5000-fix", "T", runner=runner)
        assert "SQL" not in result["body"] and result["warnings"] == []  # even if the model offers some
        assert "Entity file diffs" not in [c for c in calls if c[0].endswith("claude")][0][2]

    def test_unreadable_diff_is_a_warning_not_a_failure(self, repo):
        def runner(args, cwd, timeout):
            if args[:2] == ("git", "diff"):
                return 128, "", "fatal: bad revision"
            return pr_runner([])(args, cwd, timeout)
        result = rc.ai_pr_body(repo, "5000", "master", "5000-fix", "Fix the thing", runner=runner)
        assert result["body"].startswith("- #5000 - Fix the thing") and "Couldn't read the branch diff" in result["warnings"][0]


def test_pr_endpoints_pick_the_repository(pr_client):
    ok = pr_client.get("/api/pr/draft", params={"issue": "5000", "base": "qa-release", "title": "T", "repo": "duell-pos"})
    assert ok.status_code == 200 and ok.json()["repo"] == "duell-pos" and ok.json()["analysis"] is None
    assert pr_client.get("/api/pr/draft", params={"issue": "5000", "base": "master", "repo": "duell-pos"}).status_code == 400
    assert pr_client.get("/api/pr/draft", params={"issue": "5000", "base": "master", "repo": "nope"}).status_code == 400
    payload = {"issue": "5000", "base": "qa-release", "head": "5000-fix", "title": "QA: #5000 x", "body": "- #5000 - x\n  - y\n", "repo": "duell-pos"}
    made = pr_client.post("/api/pr/create", json=payload, headers={"X-Timeline-Desk": "1"})
    assert made.status_code == 200 and made.json()["repo"] == "duell-pos"
    create = [c for c in pr_client.seen["calls"] if c[:3] == ("gh", "pr", "create")][-1]
    assert create[create.index("--repo") + 1] == "Panteon-norway/duell-pos"
    described = pr_client.post("/api/pr/describe", json={**payload, "title": "T"}, headers={"X-Timeline-Desk": "1"})
    assert described.status_code == 200 and described.json()["sql"] == 0
