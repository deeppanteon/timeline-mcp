"""release_coverage: which of a ticket's commits are on qa-release and production, against real git repos in tmp."""
import datetime as dt
import os
import subprocess

import pytest

import release_check as rc
import release_coverage as cov


def git(cwd, *args, env=None):
    full = dict(os.environ, GIT_AUTHOR_DATE="", GIT_COMMITTER_DATE="")
    full.update(env or {})
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=full).stdout


class World:
    """An origin with a work base, qa-release and a production branch, a working clone to push from, and a checkout."""

    def __init__(self, tmp_path, repo_name, base, prod):
        self.name, self.base, self.prod = repo_name, base, prod
        tmp_path = tmp_path / repo_name  # a test can hold two worlds
        tmp_path.mkdir()
        self.origin = tmp_path / "origin.git"
        git(tmp_path, "init", "-q", "--bare", "-b", base, str(self.origin))
        self.work = tmp_path / "work"
        git(tmp_path, "clone", "-q", str(self.origin), str(self.work))
        git(self.work, "config", "user.email", "me@example.com")
        git(self.work, "config", "user.name", "Me")
        git(self.work, "checkout", "-q", "-b", base)
        self.n = 0
        self.commit("init")
        for branch in ("qa-release", prod):
            git(self.work, "branch", branch, base)
        git(self.work, "push", "-q", "origin", base, "qa-release", prod)
        self.checkout = tmp_path / repo_name
        git(tmp_path, "clone", "-q", str(self.origin), str(self.checkout))
        self.repo = rc._Repo(str(self.checkout), rc._subprocess_runner, rc.REPO_SPECS[repo_name]["slug"])

    def commit(self, message, author="Me <me@example.com>", date=None):
        self.n += 1
        path = self.work / f"f{self.n}.txt"
        path.write_text(f"{self.n}\n")
        git(self.work, "add", ".")
        env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date} if date else None
        git(self.work, "commit", "-q", "--author", author, "-m", message, env=env)
        return git(self.work, "rev-parse", "HEAD").strip()

    def branch(self, name, start=None):
        git(self.work, "checkout", "-q", "-b", name, start or self.base)

    def checkout_branch(self, name):
        git(self.work, "checkout", "-q", name)

    def merge(self, into, branch, message):
        git(self.work, "checkout", "-q", into)
        git(self.work, "merge", "-q", "--no-ff", "-m", message, branch)

    def push(self, *refs):
        git(self.work, "push", "-q", "origin", *refs)

    def sync(self):
        cov.fetch_branches(self.repo)


@pytest.fixture
def admin(tmp_path):
    return World(tmp_path, rc.ADMIN, "development", "master")


@pytest.fixture
def pos(tmp_path):
    return World(tmp_path, rc.POS, "v4-release", "prod-release")


def shas(row):
    return [c["sha"] for c in row["commits"]]


def test_released_missing_and_other_branch_fix(admin):
    """4542: two commits shipped to QA then production, a later one on the branch, a QA fix made on another branch."""
    admin.branch("4542-feature")
    c1 = admin.commit("TL #4542 first")
    c2 = admin.commit("TL #4542 second")
    admin.push("4542-feature")
    admin.merge("qa-release", "4542-feature", "Merge pull request #1 from org/4542-feature")  # PR merge commit
    admin.merge("master", "4542-feature", "Merge branch '4542-feature'")  # direct merge, no PR
    admin.push("qa-release", "master")
    admin.checkout_branch("4542-feature")
    c3 = admin.commit("TL #4542 later update")  # never merged
    admin.push("4542-feature")
    admin.branch("qa-fix", "qa-release")  # a fix made off qa-release, merged to QA only
    c4 = admin.commit("TL #4542 qa finding fix")
    admin.merge("qa-release", "qa-fix", "Merge branch 'qa-fix'")
    admin.push("qa-fix", "qa-release")
    admin.sync()

    sets = cov.release_sets(admin.repo, rc.ADMIN)
    row = cov.ticket_coverage(admin.repo, rc.ADMIN, "4542", sets)

    assert set(shas(row)) == {c1, c2, c3, c4}
    by = {c["sha"]: c for c in row["commits"]}
    assert (by[c1]["qa"], by[c1]["prod"]) == (True, True)
    assert (by[c2]["qa"], by[c2]["prod"]) == (True, True)
    assert (by[c3]["qa"], by[c3]["prod"]) == (False, False)
    assert (by[c4]["qa"], by[c4]["prod"]) == (True, False)
    assert by[c3]["branch"] == "4542-feature" and by[c4]["branch"] is None  # found by its message only
    assert row["missing_qa"] == [c3] and set(row["missing_prod"]) == {c3, c4}
    assert (row["total"], row["on_qa"], row["on_prod"]) == (4, 3, 2)
    assert row["state"] == "partial"
    assert row["branches"] == ["4542-feature"]


def test_branch_commits_ignore_qa_merged_into_the_branch(admin):
    """Merging qa-release into the ticket branch must not bring other people's commits in."""
    admin.branch("500-other")
    other = admin.commit("TL #500 someone else", author="Other <o@example.com>")
    admin.push("500-other")
    admin.merge("qa-release", "500-other", "Merge branch '500-other'")
    admin.push("qa-release")
    admin.branch("777-mine")
    mine = admin.commit("TL #777 mine")
    admin.merge("777-mine", "qa-release", "Merge branch 'qa-release' into 777-mine")
    admin.push("777-mine")
    admin.sync()

    commits = cov.branch_commits(admin.repo, "777-mine", "development")
    assert [c["sha"] for c in commits] == [mine]
    assert other not in [c["sha"] for c in commits]


def test_ticket_number_matches_whole_numbers_only(admin):
    admin.branch("14542-different")
    admin.commit("TL #14542 not ours")
    admin.push("14542-different")
    admin.branch("4542-ours")
    ours = admin.commit("TL #4542 ours")
    admin.push("4542-ours")
    admin.sync()

    assert cov.ticket_branches(admin.repo, "4542") == ["4542-ours"]
    assert [c["sha"] for c in cov.message_commits(admin.repo, "4542")] == [ours]
    assert cov.message_regex("4542") != cov.message_regex("454")


def test_two_authors_both_counted(admin):
    admin.branch("4542-two")
    a = admin.commit("TL #4542 one", author="Kashyap <kashyap@example.com>")
    b = admin.commit("TL #4542 two", author="Deep <deep@example.com>")
    admin.push("4542-two")
    admin.sync()

    row = cov.ticket_coverage(admin.repo, rc.ADMIN, "4542", cov.release_sets(admin.repo, rc.ADMIN))
    assert {c["email"] for c in row["commits"]} == {"kashyap@example.com", "deep@example.com"}
    assert set(shas(row)) == {a, b}
    assert row["state"] == "not_released"


def test_cherry_pick_counts_as_released(admin):
    admin.branch("4542-pick")
    c1 = admin.commit("TL #4542 picked")
    admin.push("4542-pick")
    git(admin.work, "checkout", "-q", "qa-release")
    git(admin.work, "cherry-pick", c1)
    admin.push("qa-release")
    admin.sync()

    row = cov.ticket_coverage(admin.repo, rc.ADMIN, "4542", cov.release_sets(admin.repo, rc.ADMIN))
    assert row["commits"][0]["sha"] == c1
    assert (row["commits"][0]["qa"], row["commits"][0]["prod"]) == (True, False)
    assert row["state"] == "on_qa"


def test_pos_uses_its_own_base_and_prod_branch(pos):
    """duell-pos: branches start from v4-release and production is prod-release; master is not used."""
    assert cov.work_base(rc.POS) == "v4-release" and cov.prod_branch(rc.POS) == "prod-release"
    pos.branch("4542-visma")
    c1 = pos.commit("TL #4542 check balance")
    pos.push("4542-visma")
    pos.merge("qa-release", "4542-visma", "Merge pull request #2 from org/4542-visma")
    pos.push("qa-release")
    pos.sync()

    row = cov.ticket_coverage(pos.repo, rc.POS, "4542", cov.release_sets(pos.repo, rc.POS))
    assert shas(row) == [c1]
    assert (row["on_qa"], row["on_prod"], row["state"]) == (1, 0, "on_qa")
    assert row["last_qa_commit_at"] and row["last_prod_commit_at"] is None


def test_coverage_state():
    assert cov.coverage_state(0, 0, 0) == "n/a"
    assert cov.coverage_state(3, 3, 3) == "shipped"
    assert cov.coverage_state(3, 3, 1) == "on_qa"
    assert cov.coverage_state(3, 2, 0) == "not_on_prod"  # some on QA, some not, none on production
    assert cov.coverage_state(3, 3, 0) == "on_qa"  # all on QA, waiting for production
    assert cov.coverage_state(3, 1, 0) == "not_on_prod"
    assert cov.coverage_state(3, 3, 3, dropped=True) == "dropped"
    assert cov.coverage_state(0, 0, 0, dropped=True) == "n/a"
    assert cov.coverage_state(3, 0, 0) == "not_released"
    assert cov.coverage_state(3, 1, 1) == "partial"
    assert cov.coverage_state(3, 2, 1) == "partial"  # some on production
    order = cov.STATE_ORDER
    assert order.index("dropped") < order.index("not_released") < order.index("not_on_prod") < order.index("partial")


def test_discover_issues_from_my_commits_only(admin):
    admin.branch("4542-mine")
    admin.commit("some work")  # the number comes from the branch name
    admin.push("4542-mine")
    admin.branch("88-subject")
    admin.commit("TL #8888 note", author="Me <me@example.com>")  # branch 88 is too short; the message names 8888
    admin.push("88-subject")
    admin.branch("999-theirs")
    admin.commit("TL #999 theirs", author="Other <other@example.com>")
    admin.push("999-theirs")
    admin.sync()

    assert cov.discover_issues(admin.repo, ["me@example.com"], "1 year ago") == ["4542", "8888"]
    assert cov.discover_issues(admin.repo, [], "1 year ago") == []


def test_repo_coverage_leaves_out_tickets_without_commits(admin):
    admin.branch("4542-here")
    admin.commit("TL #4542 here")
    admin.push("4542-here")
    admin.sync()

    rows = cov.repo_coverage(admin.repo, rc.ADMIN, ["4542", "6110", "4542"])
    assert list(rows) == ["4542"]


def test_combine_one_repo_is_not_missing_in_the_other(admin, pos):
    """A ticket with commits only in duell-pos is not flagged as missing in duell-admin; worst state first."""
    pos.branch("4542-visma")
    pos.commit("TL #4542 visma")
    pos.push("4542-visma")
    pos.sync()
    admin.branch("5000-done")
    admin.commit("TL #5000 done")
    admin.push("5000-done")
    admin.merge("qa-release", "5000-done", "Merge branch '5000-done'")
    admin.merge("master", "5000-done", "Merge branch '5000-done'")
    admin.push("qa-release", "master")
    admin.sync()

    result = cov.combine({
        rc.ADMIN: cov.repo_coverage(admin.repo, rc.ADMIN, ["4542", "5000"]),
        rc.POS: cov.repo_coverage(pos.repo, rc.POS, ["4542", "5000"]),
    })
    by = {t["issue"]: t for t in result["tickets"]}
    assert set(by["4542"]["repos"]) == {rc.POS}
    assert set(by["5000"]["repos"]) == {rc.ADMIN}
    assert by["4542"]["state"] == "not_released" and by["5000"]["state"] == "shipped"
    assert [t["issue"] for t in result["tickets"]] == ["4542", "5000"]  # worst first


def test_combine_takes_the_worst_repo_and_reports_unread_repos():
    row = lambda state: {"state": state}
    result = cov.combine({"duell-admin": {"1": row("shipped")}, "duell-pos": {"1": row("partial")}},
                         errors={"duell-extra": "no checkout"})
    assert result["tickets"][0]["state"] == "partial"
    assert result["tickets"][0]["unread"] == ["duell-extra"]
    assert result["errors"] == {"duell-extra": "no checkout"}


def test_missing_base_ref_is_an_error(admin):
    admin.branch("4542-x")
    admin.commit("TL #4542 x")
    admin.push("4542-x")
    admin.sync()
    with pytest.raises(rc.ReleaseCheckError):
        cov.branch_commits(admin.repo, "4542-x", "no-such-base")


# ---- history ---------------------------------------------------------------
def at(day):
    return dt.datetime(2026, 11, day, 9, 0, 0)


def test_the_4542_picture_is_not_on_prod(pos):
    """5 of 6 commits on QA, none on production: the loud state, not a quiet partial."""
    pos.branch("4542-visma")
    for i in range(5):
        pos.commit(f"TL #4542 step {i}")
    pos.push("4542-visma")
    pos.merge("qa-release", "4542-visma", "Merge pull request #3 from org/4542-visma")
    pos.push("qa-release")
    pos.checkout_branch("4542-visma")
    pos.commit("TL #4542 yesterday")
    pos.push("4542-visma")
    pos.sync()

    row = cov.ticket_coverage(pos.repo, rc.POS, "4542", cov.release_sets(pos.repo, rc.POS))
    assert (row["total"], row["on_qa"], row["on_prod"]) == (6, 5, 0)
    assert row["state"] == "not_on_prod"


def test_history_records_first_and_last_seen(admin, tmp_path):
    admin.branch("4542-h")
    c1 = admin.commit("TL #4542 one")
    admin.push("4542-h")
    admin.merge("qa-release", "4542-h", "Merge branch '4542-h'")
    admin.push("qa-release")
    admin.sync()
    base = str(tmp_path / "base")

    first = cov.build_repo_coverage(admin.repo, rc.ADMIN, ["4542"], base, now=at(1))
    c = first["4542"]["commits"][0]
    assert c["sha"] == c1 and c["qa"] and not c["prod"]
    assert c["qa_first_seen"] == "2026-11-01T09:00:00" and c["prod_first_seen"] is None

    admin.merge("master", "4542-h", "Merge branch '4542-h'")
    admin.push("master")
    admin.sync()
    second = cov.build_repo_coverage(admin.repo, rc.ADMIN, ["4542"], base, now=at(3))
    c = second["4542"]["commits"][0]
    assert c["qa_first_seen"] == "2026-11-01T09:00:00"  # kept from the first check
    assert c["prod_first_seen"] == "2026-11-03T09:00:00"
    assert second["4542"]["state"] == "shipped"
    assert cov.last_checks(base)[rc.ADMIN] == {"checked_at": "2026-11-03T09:00:00", "error": None}


def test_commit_dropped_from_qa_is_flagged(admin, tmp_path):
    """qa-release is reset without the commit: it was there once, so it is dropped, not just missing."""
    admin.branch("4542-d")
    c1 = admin.commit("TL #4542 kept on the branch")
    admin.push("4542-d")
    admin.merge("qa-release", "4542-d", "Merge branch '4542-d'")
    admin.push("qa-release")
    admin.sync()
    base = str(tmp_path / "base")
    cov.build_repo_coverage(admin.repo, rc.ADMIN, ["4542"], base, now=at(1))

    git(admin.work, "checkout", "-q", "qa-release")
    git(admin.work, "reset", "-q", "--hard", "development")  # rebuilt without the ticket
    git(admin.work, "push", "-q", "--force", "origin", "qa-release")
    admin.sync()
    row = cov.build_repo_coverage(admin.repo, rc.ADMIN, ["4542"], base, now=at(2))["4542"]

    assert row["dropped"] == [c1]
    assert row["commits"][0]["dropped_qa"] is True and row["commits"][0]["qa"] is False
    assert row["state"] == "dropped"
    result = cov.combine({rc.ADMIN: {"4542": row}})
    assert result["tickets"][0]["state"] == "dropped"


def test_never_on_qa_is_missing_not_dropped(admin, tmp_path):
    admin.branch("4542-n")
    admin.commit("TL #4542 never merged")
    admin.push("4542-n")
    admin.sync()
    base = str(tmp_path / "base")
    cov.build_repo_coverage(admin.repo, rc.ADMIN, ["4542"], base, now=at(1))
    row = cov.build_repo_coverage(admin.repo, rc.ADMIN, ["4542"], base, now=at(2))["4542"]
    assert row["dropped"] == [] and row["state"] == "not_released"


def test_stored_commits_survive_a_deleted_branch(admin, tmp_path):
    """The branch is deleted after merge; the next check still lists the ticket, judged by ancestry."""
    admin.branch("4542-gone")
    c1 = admin.commit("work without the ticket number in the message")
    admin.push("4542-gone")
    admin.merge("qa-release", "4542-gone", "Merge branch '4542-gone'")
    admin.push("qa-release")
    admin.sync()
    base = str(tmp_path / "base")
    cov.build_repo_coverage(admin.repo, rc.ADMIN, ["4542"], base, now=at(1))

    git(admin.work, "push", "-q", "origin", "--delete", "4542-gone")
    admin.sync()
    assert cov.repo_coverage(admin.repo, rc.ADMIN, ["4542"]) == {}  # nothing finds it any more
    rows = cov.build_repo_coverage(admin.repo, rc.ADMIN, ["4542"], base, now=at(2))
    assert [c["sha"] for c in rows["4542"]["commits"]] == [c1]
    assert rows["4542"]["state"] == "on_qa" and rows["4542"]["branches"] == []


def test_failed_read_is_recorded_and_drops_nothing(admin, tmp_path):
    admin.branch("4542-f")
    admin.commit("TL #4542 f")
    admin.push("4542-f")
    admin.merge("qa-release", "4542-f", "Merge branch '4542-f'")
    admin.push("qa-release")
    admin.sync()
    base = str(tmp_path / "base")
    cov.build_repo_coverage(admin.repo, rc.ADMIN, ["4542"], base, now=at(1))

    git(admin.checkout, "update-ref", "-d", "refs/remotes/origin/qa-release")  # the ref cannot be read
    with pytest.raises(rc.ReleaseCheckError):
        cov.build_repo_coverage(admin.repo, rc.ADMIN, ["4542"], base, now=at(2))
    assert cov.last_checks(base)[rc.ADMIN]["error"]
    admin.sync()
    row = cov.build_repo_coverage(admin.repo, rc.ADMIN, ["4542"], base, now=at(3))["4542"]
    assert row["dropped"] == [] and row["state"] == "on_qa"
    assert cov.last_checks(base)[rc.ADMIN]["error"] is None


def test_empty_release_set_never_calls_anything_dropped(admin, tmp_path):
    admin.branch("4542-e")
    admin.commit("TL #4542 e")
    admin.push("4542-e")
    admin.merge("qa-release", "4542-e", "Merge branch '4542-e'")
    admin.push("qa-release")
    admin.sync()
    base = str(tmp_path / "base")
    sets = cov.release_sets(admin.repo, rc.ADMIN)
    rows = cov.repo_coverage(admin.repo, rc.ADMIN, ["4542"], sets)
    cov.apply_history(base, rc.ADMIN, rows, sets, now=at(1))

    empty = {"qa": set(), "prod": set()}
    out = cov.apply_history(base, rc.ADMIN, cov.repo_coverage(admin.repo, rc.ADMIN, ["4542"], empty), empty, now=at(2))
    assert out["4542"]["dropped"] == []


def test_reading_history_creates_no_file(tmp_path):
    base = str(tmp_path / "nothing")
    assert cov.last_checks(base) == {}
    assert not os.path.exists(os.path.join(base, "database"))


# ---- Timeline cross-check ---------------------------------------------------
def _ticket(state, missing_qa=0, missing_prod=0, last="2026-11-05T10:00:00+01:00", issue="4542"):
    row = {"state": state, "missing_qa": ["x"] * missing_qa, "missing_prod": ["y"] * missing_prod, "last_commit_at": last}
    return {"issue": issue, "state": state, "repos": {"duell-pos": row}, "unread": [], "title": "", "timeline": None, "flags": []}


def _status(release_state, task_status="open", **extra):
    return {"issue_id": "4542", "title": "Visma NXT", "task_status": task_status, "stage_status": "",
            "release_state": release_state, **extra}


def keys(flags):
    return [f["key"] for f in flags]


def test_flag_live_in_timeline_but_not_on_prod():
    flags = cov.timeline_flags(_ticket("not_on_prod", missing_qa=1, missing_prod=6), _status("live"))
    assert keys(flags) == ["live_not_on_prod"] and flags[0]["level"] == "missing"
    assert "Timeline says live, but 6 commits not on production" in flags[0]["text"]


def test_flag_qa_approved_but_not_on_prod_or_qa():
    flags = cov.timeline_flags(_ticket("not_on_prod", missing_qa=1, missing_prod=6), _status("qa approved"))
    assert keys(flags) == ["approved_not_on_prod", "qa_not_on_qa"]
    assert all(f["level"] == "warn" for f in flags)
    only_prod = cov.timeline_flags(_ticket("on_qa", missing_prod=2), _status("qa approved"))
    assert keys(only_prod) == ["approved_not_on_prod"]


def test_flag_in_qa_but_code_missing_from_qa():
    assert keys(cov.timeline_flags(_ticket("not_released", missing_qa=1, missing_prod=1), _status("in qa"))) == ["qa_not_on_qa"]
    assert keys(cov.timeline_flags(_ticket("partial", missing_qa=1, missing_prod=2), _status("qa blocked"))) == ["qa_not_on_qa"]


def test_flag_shipped_but_timeline_not_live():
    flags = cov.timeline_flags(_ticket("shipped"), _status("qa blocked"))
    assert keys(flags) == ["shipped_not_live"] and "qa blocked" in flags[0]["text"]
    assert cov.timeline_flags(_ticket("shipped"), _status("live")) == []


def test_no_flags_when_they_agree_or_status_is_unknown():
    assert cov.timeline_flags(_ticket("on_qa"), _status("in qa")) == []
    assert cov.timeline_flags(_ticket("not_released", missing_qa=1, missing_prod=1), _status("before qa")) == []
    assert cov.timeline_flags(_ticket("not_on_prod", missing_qa=1, missing_prod=1), {"issue_id": "1", "error": "not found"}) == []
    assert cov.timeline_flags(_ticket("not_on_prod", missing_qa=1, missing_prod=1), {}) == []


def test_flag_counts_commits_over_all_repos():
    t = _ticket("partial", missing_prod=1)
    t["repos"]["duell-admin"] = {"state": "partial", "missing_qa": [], "missing_prod": ["a", "b"], "last_commit_at": None}
    assert "3 commits not on production" in cov.timeline_flags(t, _status("live"))[0]["text"]
    one = cov.timeline_flags(_ticket("partial", missing_prod=1), _status("live"))[0]["text"]
    assert "1 commit not on production" in one


def test_lookup_ids_skips_old_shipped_tickets():
    now = dt.datetime(2026, 11, 6, 12, 0, 0)
    tickets = [_ticket("not_on_prod", issue="1", last="2024-01-01T00:00:00+00:00"),
               _ticket("shipped", issue="2", last="2026-10-20T10:00:00+02:00"),
               _ticket("shipped", issue="3", last="2025-01-01T10:00:00+02:00"),
               _ticket("shipped", issue="4", last=None)]
    assert cov.lookup_ids(tickets, now) == ["1", "2"]


def test_attach_timeline_adds_title_status_and_flags():
    report = {"tickets": [_ticket("not_on_prod", missing_qa=1, missing_prod=6), _ticket("shipped", issue="9")], "errors": {}}
    asked = []

    def lookup(ids):
        asked.append(ids)
        return [_status("live"), {"issue_id": "9", "error": "not found"}]

    out = cov.attach_timeline(report, lookup, now=dt.datetime(2026, 11, 6))
    assert asked == [["4542", "9"]] and out["timeline_error"] is None
    t = out["tickets"][0]
    assert t["title"] == "Visma NXT" and t["timeline"]["release_state"] == "live" and keys(t["flags"]) == ["live_not_on_prod"]
    assert out["tickets"][1]["flags"] == [] and out["tickets"][1]["timeline"] is None


def test_attach_timeline_failure_keeps_the_git_report():
    report = {"tickets": [_ticket("not_on_prod", missing_qa=1, missing_prod=6)], "errors": {}}

    def lookup(ids):
        raise PermissionError("token expired")

    out = cov.attach_timeline(report, lookup)
    assert out["timeline_error"] == "token expired" and out["tickets"][0]["flags"] == []
    assert cov.attach_timeline({"tickets": [], "errors": {}}, lookup)["timeline_error"] is None  # nothing to ask, no call
    assert cov.attach_timeline({"tickets": [_ticket("shipped", last="2020-01-01T00:00:00+00:00")], "errors": {}},
                               lookup, now=dt.datetime(2026, 11, 6))["timeline_error"] is None


def test_combine_gives_every_ticket_empty_timeline_fields():
    t = cov.combine({"duell-admin": {"1": {"state": "shipped"}}})["tickets"][0]
    assert (t["title"], t["timeline"], t["flags"]) == ("", None, [])
    assert (t["section"], t["attention"]) == ("unknown", False)  # shipped, no Timeline status


# ---- sections ----------------------------------------------------------------
def test_section_for_timeline_status():
    assert cov.section_for(None) == "unknown"
    assert cov.section_for({"release_state": "before qa", "task_status": "open"}) == "dev"
    assert cov.section_for({"release_state": "in qa", "task_status": "open"}) == "qa"
    assert cov.section_for({"release_state": "qa blocked", "task_status": "open"}) == "qa_blocked"
    assert cov.section_for({"release_state": "qa approved", "task_status": "open"}) == "approved"
    assert cov.section_for({"release_state": "live", "task_status": "open"}) == "live"
    assert cov.section_for({"release_state": "???", "task_status": "open"}) == "unknown"
    assert cov.SECTION_ORDER == ["dev", "qa", "qa_blocked", "approved", "live", "unknown"]


def classified(code_state, release_state, task_status="open", missing_qa=0, missing_prod=0):
    report = {"tickets": [_ticket(code_state, missing_qa=missing_qa, missing_prod=missing_prod)], "errors": {}}
    out = cov.attach_timeline(report, lambda ids: [_status(release_state, task_status)], now=dt.datetime(2026, 11, 6))
    return out["tickets"][0]


def test_a_ticket_in_development_is_not_expected_on_qa_or_production():
    for code_state, qa, prod in (("not_released", 3, 3), ("not_on_prod", 1, 3), ("partial", 1, 2)):
        t = classified(code_state, "before qa", missing_qa=qa, missing_prod=prod)
        assert t["section"] == "dev" and t["flags"] == [] and t["attention"] is False


def test_code_ahead_of_a_development_stage_is_flagged():
    t = classified("on_qa", "before qa")
    assert keys(t["flags"]) == ["stage_behind"] and t["attention"] is True and "QA, but Timeline is still before QA" in t["flags"][0]["text"]
    shipped = classified("shipped", "before qa")["flags"]
    assert keys(shipped) == ["stage_behind"] and "production" in shipped[0]["text"]  # not also "not Live"
    assert classified("partial", "before qa", missing_qa=1, missing_prod=1)["flags"] == []  # rework: not flagged


def test_attention_per_section():
    assert classified("not_released", "in qa", missing_qa=2, missing_prod=2)["attention"] is True
    assert classified("on_qa", "in qa")["attention"] is False  # all on QA, as expected
    assert classified("on_qa", "qa approved", missing_prod=2)["attention"] is True  # approved, not on production
    assert classified("shipped", "live")["attention"] is False
    assert classified("not_released", "live", missing_qa=1, missing_prod=1)["attention"] is True
    assert classified("dropped", "before qa")["attention"] is True


def test_attention_without_a_timeline_status_uses_the_code_state():
    t = cov.combine({"duell-pos": {"1": {"state": "not_on_prod"}, "2": {"state": "on_qa"}, "3": {"state": "dropped"}}})["tickets"]
    by = {x["issue"]: x for x in t}
    assert by["1"]["attention"] is True and by["2"]["attention"] is False and by["3"]["attention"] is True
    assert all(x["section"] == "unknown" for x in t)


# ---- closed tickets are left out for good --------------------------------------
def test_attach_timeline_takes_closed_tickets_out_and_lists_them():
    report = {"tickets": [_ticket("not_on_prod", missing_qa=1, missing_prod=6, issue="1"), _ticket("on_qa", issue="2")], "errors": {}}
    rows = [_status("in qa", task_status="closed", issue_id="1"), _status("in qa", issue_id="2")]
    out = cov.attach_timeline(report, lambda ids: rows, now=dt.datetime(2026, 11, 6))
    assert [t["issue"] for t in out["tickets"]] == ["2"] and out["closed"] == ["1"]
    assert cov.attach_timeline({"tickets": [], "errors": {}}, None)["closed"] == []


def test_mark_closed_deletes_commits_and_is_remembered(admin, tmp_path):
    admin.branch("4542-c")
    admin.commit("TL #4542 c")
    admin.push("4542-c")
    admin.sync()
    base = str(tmp_path / "base")
    assert cov.closed_issues(base) == set() and not os.path.exists(os.path.join(base, "database"))
    cov.build_repo_coverage(admin.repo, rc.ADMIN, ["4542"], base, now=at(1))

    cov.mark_closed(base, ["4542"], now=at(2))
    assert cov.closed_issues(base) == {"4542"}
    again = cov.build_repo_coverage(admin.repo, rc.ADMIN, [], base, now=at(3))
    assert again == {}  # the stored commits are gone, so nothing brings the ticket back
    cov.mark_closed(base, ["4542"], now=at(4))  # twice is fine
    cov.mark_closed(base, [], now=at(4))


def test_build_coverage_skips_closed_tickets_from_the_second_check(admin, tmp_path, monkeypatch):
    for n, title in ((100, "closed one"), (200, "open one")):
        admin.branch(f"{n}-{title.split()[0]}")
        admin.commit(f"TL #{n} {title}")
        admin.push(f"{n}-{title.split()[0]}")
    admin.sync()
    base = str(tmp_path / "base")

    def fake_open(path, name, runner):
        if name == rc.POS:
            raise rc.ReleaseCheckError("no duell-pos checkout")
        return admin.repo

    monkeypatch.setattr(cov, "_open", fake_open)
    asked = []

    def lookup(ids):
        asked.append(list(ids))
        return [_status("in qa", task_status="closed" if i == "100" else "open", issue_id=i) for i in ids]

    first = cov.build_coverage({}, ["me@example.com"], base_dir=base, fetch=False, status_lookup=lookup, now=at(1))
    assert [t["issue"] for t in first["tickets"]] == ["200"] and first["closed"] == ["100"]
    assert sorted(asked[0]) == ["100", "200"] and first["errors"] == {rc.POS: "no duell-pos checkout"}
    assert cov.closed_issues(base) == {"100"}

    second = cov.build_coverage({}, ["me@example.com"], base_dir=base, fetch=False, status_lookup=lookup, now=at(2))
    assert asked[1] == ["200"]  # 100 is not looked up again
    assert [t["issue"] for t in second["tickets"]] == ["200"] and second["closed"] == []


# ---- old fully shipped tickets are hidden --------------------------------------
def ship(world, branch):
    world.merge("qa-release", branch, f"Merge branch '{branch}'")
    world.merge(world.prod, branch, f"Merge branch '{branch}'")
    world.push("qa-release", world.prod)


def test_discover_issue_dates_gives_the_newest_commit_per_ticket(admin):
    admin.branch("300-a")
    admin.commit("first", date="2026-03-01T10:00:00+00:00")
    admin.commit("second", date="2026-05-01T10:00:00+00:00")
    admin.push("300-a")
    admin.branch("400-b")
    admin.commit("TL #400 only", date="2026-02-01T10:00:00+00:00")
    admin.push("400-b")
    admin.sync()
    found = cov.discover_issue_dates(admin.repo, ["me@example.com"], "2000-01-01")
    assert {k: cov._parse_date(v) for k, v in found.items()} == {
        "300": cov._parse_date("2026-05-01T10:00:00+00:00"), "400": cov._parse_date("2026-02-01T10:00:00+00:00")}
    assert cov.discover_issues(admin.repo, ["me@example.com"], "2000-01-01") == ["300", "400"]


def test_retire_candidates_are_old_shipped_and_fully_read():
    now = dt.datetime(2026, 11, 6)
    old, recent = "2026-03-01T10:00:00+00:00", "2026-10-30T10:00:00+00:00"
    tickets = [_ticket("shipped", issue="1", last=old), _ticket("shipped", issue="2", last=recent),
               _ticket("on_qa", issue="3", last=old), _ticket("shipped", issue="4", last=None)]
    unread = _ticket("shipped", issue="5", last=old)
    unread["unread"] = ["duell-admin"]
    assert cov.retire_candidates(tickets + [unread], now) == {"1": old}
    two = _ticket("shipped", issue="6", last=old)
    two["repos"]["duell-admin"] = {"state": "shipped", "missing_qa": [], "missing_prod": [], "last_commit_at": recent}
    assert cov.retire_candidates([two], now) == {}  # one repo has something recent


def test_retired_storage_round_trip(admin, tmp_path):
    base = str(tmp_path / "base")
    assert cov.retired_issues(base) == {} and cov.hidden_counts(base) == {"closed": 0, "retired": 0}
    assert not os.path.exists(os.path.join(base, "database"))
    admin.branch("4542-r")
    admin.commit("TL #4542 r")
    admin.push("4542-r")
    admin.sync()
    cov.build_repo_coverage(admin.repo, rc.ADMIN, ["4542"], base, now=at(1))
    cov.mark_retired(base, {"4542": "2026-03-01T10:00:00+00:00"}, now=at(2))
    assert cov.retired_issues(base) == {"4542": "2026-03-01T10:00:00+00:00"}
    assert cov.build_repo_coverage(admin.repo, rc.ADMIN, [], base, now=at(3)) == {}  # its stored commits are gone
    cov.unretire(base, ["4542", "nothing"])
    assert cov.retired_issues(base) == {}
    cov.mark_retired(base, {}, now=at(3))
    cov.unretire(base, [])


def test_build_coverage_hides_old_shipped_and_brings_it_back_on_a_newer_commit(admin, pos, tmp_path, monkeypatch):
    admin.branch("300-old")
    admin.commit("TL #300 old work", date="2026-03-01T10:00:00+00:00")
    admin.push("300-old")
    ship(admin, "300-old")
    admin.branch("400-recent")
    admin.commit("TL #400 recent work", date="2026-10-30T10:00:00+00:00")
    admin.push("400-recent")
    ship(admin, "400-recent")
    admin.branch("500-pending")
    admin.commit("TL #500 not shipped", date="2026-03-01T10:00:00+00:00")
    admin.push("500-pending")
    admin.sync()
    base = str(tmp_path / "base")
    monkeypatch.setattr(cov, "_open", lambda path, name, runner: admin.repo if name == rc.ADMIN else (_ for _ in ()).throw(rc.ReleaseCheckError("no pos")))
    # a POS error keeps tickets unread, so nothing may be hidden yet
    blocked = cov.build_coverage({}, ["me@example.com"], since="2000-01-01", base_dir=base, fetch=False, now=at(1))
    assert {t["issue"] for t in blocked["tickets"]} == {"300", "400", "500"} and blocked["retired"] == []

    monkeypatch.setattr(cov, "_open", lambda path, name, runner: admin.repo if name == rc.ADMIN else pos.repo)
    first = cov.build_coverage({}, ["me@example.com"], since="2000-01-01", base_dir=base, fetch=False, now=at(2))
    assert {t["issue"] for t in first["tickets"]} == {"400", "500"} and first["retired"] == ["300"]
    assert first["hidden"] == {"closed": 0, "retired": 1}

    second = cov.build_coverage({}, ["me@example.com"], since="2000-01-01", base_dir=base, fetch=False, now=at(3))
    assert {t["issue"] for t in second["tickets"]} == {"400", "500"} and second["retired"] == []  # not read again
    assert cov.hidden_counts(base)["retired"] == 1

    admin.checkout_branch("300-old")
    admin.commit("TL #300 rework", date="2026-11-05T10:00:00+00:00")  # a newer commit of yours
    admin.push("300-old")
    admin.sync()
    third = cov.build_coverage({}, ["me@example.com"], since="2000-01-01", base_dir=base, fetch=False, now=at(4))
    by = {t["issue"]: t for t in third["tickets"]}
    assert "300" in by and by["300"]["repos"]["duell-admin"]["total"] == 2  # the whole ticket is back, judged afresh
    assert by["300"]["state"] != "shipped" and cov.hidden_counts(base)["retired"] == 0


def test_later_picks_the_newer_date_and_tolerates_missing():
    assert cov._later("2026-05-01T10:00:00Z", "2026-03-01T10:00:00+00:00") == "2026-05-01T10:00:00Z"
    assert cov._later("2026-03-01T10:00:00+00:00", "2026-05-01T10:00:00Z") == "2026-05-01T10:00:00Z"
    assert cov._later(None, "2026-03-01T10:00:00Z") == "2026-03-01T10:00:00Z"
    assert cov._later("2026-03-01T10:00:00Z", "nonsense") == "2026-03-01T10:00:00Z"
    assert cov._later(None, None) is None


# ---- only tickets you are subscribed to ----------------------------------------
def make_two_tickets(admin, pos):
    for n in (100, 200):
        admin.branch(f"{n}-work")
        admin.commit(f"TL #{n} work", date="2026-10-30T10:00:00+00:00")
        admin.push(f"{n}-work")
    admin.sync()
    pos.sync()


def test_only_subscribed_tickets_are_read_and_shown(admin, pos, tmp_path, monkeypatch):
    make_two_tickets(admin, pos)
    base = str(tmp_path / "base")
    monkeypatch.setattr(cov, "_open", lambda path, name, runner: admin.repo if name == rc.ADMIN else pos.repo)
    asked = []
    read = []
    real = cov.ticket_coverage
    monkeypatch.setattr(cov, "ticket_coverage", lambda repo, name, issue, sets: read.append(issue) or real(repo, name, issue, sets))

    everyone = cov.build_coverage({}, ["me@example.com"], since="2000-01-01", base_dir=base, fetch=False, now=at(1))
    assert {t["issue"] for t in everyone["tickets"]} == {"100", "200"} and everyone["subscribed_only"] is False

    read.clear()
    mine = cov.build_coverage({}, ["me@example.com"], since="2000-01-01", base_dir=base, fetch=False, now=at(2),
                              subscribed=lambda: asked.append(1) or [200, "999"])
    assert [t["issue"] for t in mine["tickets"]] == ["200"] and mine["subscribed_only"] is True
    assert set(read) == {"200"} and asked == [1]  # the other ticket was not even read from git


def test_stored_tickets_you_are_not_subscribed_to_stay_hidden_but_are_kept(admin, pos, tmp_path, monkeypatch):
    make_two_tickets(admin, pos)
    base = str(tmp_path / "base")
    monkeypatch.setattr(cov, "_open", lambda path, name, runner: admin.repo if name == rc.ADMIN else pos.repo)
    cov.build_coverage({}, ["me@example.com"], since="2000-01-01", base_dir=base, fetch=False, now=at(1))  # both stored
    # Nothing finds the tickets any more (empty author list), so only the stored rows could show them.
    gone = cov.build_coverage({}, ["nobody@example.com"], since="2000-01-01", base_dir=base, fetch=False, now=at(2),
                              subscribed=lambda: ["100"])
    assert [t["issue"] for t in gone["tickets"]] == ["100"]
    again = cov.build_coverage({}, ["nobody@example.com"], since="2000-01-01", base_dir=base, fetch=False, now=at(3),
                               subscribed=lambda: ["100", "200"])
    assert {t["issue"] for t in again["tickets"]} == {"100", "200"}  # kept, so subscribing again brings it back


def test_unknown_subscriptions_fail_the_build_instead_of_showing_everything(admin, pos, tmp_path, monkeypatch):
    make_two_tickets(admin, pos)
    monkeypatch.setattr(cov, "_open", lambda path, name, runner: admin.repo if name == rc.ADMIN else pos.repo)

    def broken():
        raise PermissionError("token expired")

    with pytest.raises(PermissionError):
        cov.build_coverage({}, ["me@example.com"], since="2000-01-01", base_dir=str(tmp_path / "b"), fetch=False,
                           subscribed=broken)


def test_repo_rows_carry_the_github_url_for_commit_links():
    commit = {"sha": "a" * 40, "short": "a" * 10, "date": "2026-11-05T10:00:00Z", "qa": True, "prod": True}
    row = cov.summarize("4542", rc.POS, [], [commit])
    assert row["url"] == "https://github.com/Panteon-norway/duell-pos"  # a commit is <url>/commit/<sha>
    assert cov.summarize("4542", rc.ADMIN, [], [commit])["url"] == "https://github.com/Panteon-norway/duell-admin"
    assert cov.summarize("4542", "unknown-repo", [], [commit])["url"] is None
