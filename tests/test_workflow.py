"""workflow: the task flow, its runs and decisions, against real git repos in tmp (no network, recording launcher)."""
import json
import os
import subprocess
import sys
import time
from datetime import datetime

import pytest
from starlette.testclient import TestClient

import dashboard
import panteon_client as pc
import work_session as ws
import workflow as wf

NOW = datetime(2026, 10, 27, 10, 0, 0)


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def setup(tmp_path):
    """An 'origin' with master, a clone of it, a board config for it, and a base dir for the database."""
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "master")
    git(seed, "config", "user.email", "t@example.com")
    git(seed, "config", "user.name", "T")
    (seed / "a.txt").write_text("a\n")
    git(seed, "add", ".")
    git(seed, "commit", "-qm", "init")
    origin = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    repo = tmp_path / "duell-admin"
    git(tmp_path, "clone", "-q", str(origin), str(repo))
    config_file = tmp_path / "timeline-desk.json"
    config_file.write_text(json.dumps({
        "repos": {"15033": {"name": "duell-admin", "path": "duell-admin", "base": "master", "workspace": "worktree"}},
        "claude": "/opt/node/bin/claude",
        "terminal": "gnome-terminal",
    }))
    launched = []
    base = tmp_path / "desk"
    base.mkdir()
    return {
        "tmp": tmp_path, "repo": repo, "config": ws.load_config(str(config_file)), "base": str(base),
        "launched": launched, "launcher": lambda args, env: launched.append(args),
    }


def docs(setup, issue="5001"):
    folder = setup["repo"] / "timeline" / issue
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def view(setup, issue="5001", **kwargs):
    return wf.workflow_view(issue, setup["config"], setup["base"], **kwargs)


def run(setup, issue="5001", stages=None, **kwargs):
    """Runs the current step in a (recorded) terminal."""
    return wf.run_step(issue, setup["config"], setup["base"], stages, launcher=setup["launcher"], now=NOW, **kwargs)


def prompt_of(args):
    """The prompt in a recorded launch: right after the claude binary."""
    return args[args.index("/opt/node/bin/claude") + 1]


def flag(args, name):
    return args[args.index(name) + 1]


REAL_SESSION_ALIVE = ws.session_alive  # the autouse fixture below replaces it


@pytest.fixture(autouse=True)
def environment(monkeypatch, tmp_path):
    """
    Recorded launches start no process, so every terminal session counts as
    alive unless a test says otherwise, and Claude's session logs are looked
    for in an empty folder.
    """
    monkeypatch.setattr(wf.work_session, "session_alive", lambda session_id: True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-claude-home"))


def decide(setup, kind, issue="5001", **kwargs):
    return wf.decide(issue, kind, setup["base"], setup["config"], now=NOW, **kwargs)


def report(setup, started, issue="5001", **result):
    """What Claude does at the end of a run: write the result file (and the terminal, the exit file)."""
    path = os.path.join(started["path"], wf.result_file(issue, started["run_id"]))
    with open(path, "w") as fh:
        json.dump({"status": "done", "summary": "ok", **result}, fh)


def kinds(v):
    return [a["kind"] for a in v["actions"]]


def test_prompt_has_only_fixed_text_and_the_report_contract():
    repo = {"name": "duell-admin", "base": "master", "path": "/repos/duell-admin"}
    for key in (s["key"] for s in wf.FLOW if s["run"]):
        prompt = wf.build_prompt(key, "6034", repo, "6034-x", 7)
        assert "#6034" in prompt and "Never push" in prompt and wf.SKILLS_DIR in prompt
        assert "timeline/6034/runs/7.result.json" in prompt
    develop = wf.build_prompt("develop", "6034", repo, "6034-x", 7)
    assert "timeline-progress" in develop and "wait for my go-ahead" in develop and '"remaining"' in develop
    assert "phase qa-fix" in wf.build_prompt("qa_fix", "6034", repo, "6034-x", 7)
    intake = wf.build_prompt("intake", "6034", repo, "6034-x", 7)
    assert "timeline-intake" in intake and "Don't edit code" in intake and '"remaining"' not in intake


@pytest.mark.parametrize("stages, has_analysis, step", [
    ({}, False, "intake"),
    ({"QA": "done"}, False, "intake"),
    ({"Development": "in progress"}, True, "develop"),
    ({"Development": "done"}, True, "review"),
    ({"Development": "done", "Testing": "in progress"}, True, "qa"),
    ({"QA": "done"}, True, "release_prod"),
    ({"QA": "done", "Live": "done"}, True, "close"),
])
def test_propose_start(stages, has_analysis, step):
    assert wf.propose_start(stages, has_analysis)["step"] == step


def test_no_workflow_view(setup):
    v = view(setup, code="15033", stages={"Development": "in progress"})
    assert v["workflow"] is None and v["startable"] and v["actions"] == [] and v["status"]["code"] == "none"
    assert v["proposal"]["step"] == "intake"  # no analysis.md yet
    (docs(setup) / "analysis.md").write_text("plan\n")
    assert view(setup, code="15033", stages={"Development": "in progress"})["proposal"]["step"] == "develop"
    other = view(setup, code="999")
    assert other["startable"] is False and "no repo" in other["start_note"]
    assert not os.path.exists(pc.planner_db_path(setup["base"]))  # looking writes nothing


def test_whole_flow(setup):
    cfg, launched = setup["config"], setup["launched"]
    wf.start_workflow("5001", "15033", "intake", "New report", cfg, setup["base"], now=NOW)
    v = view(setup)
    assert v["workflow"]["step"] == "intake" and v["workflow"]["branch"] == "5001-new-report"
    assert kinds(v) == ["run", "skip", "reset", "stop"] and "timeline/5001/runs/<run>.result.json" in v["prompt_preview"]
    assert v["actions"][0]["label"] == "Run in terminal"
    assert v["status"] == {"code": "needs_you", "label": "Intake · ready", "short": "Intake · ready"}

    # Intake: run, the terminal opens with our session id, the result comes back, you approve it
    started = run(setup)
    args = launched[-1]
    assert flag(args, "--session-id") == started["session_id"] and "#5001" in prompt_of(args)
    assert os.path.join(started["path"], "timeline", "5001", "runs", f"{started['run_id']}.exit") in args
    assert "New report" not in prompt_of(args)  # task text never goes into the prompt
    # The desk brings its own timeline MCP server, so a project that disabled "timeline-mcp" doesn't matter
    servers = json.loads(flag(args, "--mcp-config"))["mcpServers"]
    assert list(servers) == ["timeline-desk"] and servers["timeline-desk"]["env"]["PANTEON_TIMELINE_DIR"] == str(setup["repo"])
    assert "mcp__timeline-desk__" in prompt_of(args) and "fetch_task.py 5001 --base-dir" in prompt_of(args)
    v = view(setup)
    assert v["runs"][0]["status"] == "running" and kinds(v) == ["abandon", "reset", "stop"]
    with pytest.raises(ValueError):
        run(setup)  # one open run at a time
    (docs(setup) / "analysis.md").write_text("plan\n")
    report(setup, started, questions=["Which tenant?"])
    v = view(setup)
    assert v["runs"][0]["status"] == "review" and v["runs"][0]["result"]["questions"] == ["Which tenant?"]
    assert kinds(v) == ["approve", "reject", "resume", "reset", "stop"]
    with pytest.raises(ValueError, match="isn't the one"):
        decide(setup, "approve", run_id=999)
    decide(setup, "approve", run_id=started["run_id"])
    assert view(setup)["workflow"]["step"] == "estimate"

    # Estimate is optional: skip needs a reason
    with pytest.raises(ValueError, match="reason"):
        decide(setup, "skip")
    decide(setup, "skip", note="Fixed price")

    # Develop repeats: one step per run, then you finish it
    first = run(setup, stages={"Development": "in progress"})
    report(setup, first, remaining=1, commits=["abc123 TL #5001 step 1"])
    decide(setup, "approve", run_id=first["run_id"])
    v = view(setup)
    assert v["workflow"]["step"] == "develop" and kinds(v) == ["run", "finish", "skip", "reset", "stop"]
    decide(setup, "finish")

    # Review, then back to Develop with a reason
    assert view(setup)["workflow"]["step"] == "review"
    decide(setup, "back_to_develop", note="Missing null check")
    with pytest.raises(ValueError, match="can't"):
        decide(setup, "finish")  # back in Develop: finishing needs a new approved run
    again = run(setup, stages={"Development": "in progress"})
    report(setup, again, remaining=0)
    decide(setup, "approve", run_id=again["run_id"])
    decide(setup, "finish")
    review = run(setup)
    report(setup, review)
    decide(setup, "approve", run_id=review["run_id"])

    # Release to QA, then QA's verdicts loop through Fix QA findings
    release = run(setup)
    report(setup, release, files=["timeline/5001/qa-plan.md"])
    decide(setup, "approve", run_id=release["run_id"])
    v = view(setup)
    assert v["workflow"]["step"] == "qa" and kinds(v) == ["qa_issues", "qa_passed", "reset", "stop"]
    assert v["status"]["code"] == "waiting" and v["status"]["label"] == "Waiting on QA"
    decide(setup, "qa_issues", note="2 comments from QA")
    fix = run(setup, stages={"Testing": "in progress"})
    report(setup, fix, remaining=0)
    decide(setup, "approve", run_id=fix["run_id"])
    decide(setup, "finish")
    v = view(setup)
    assert v["workflow"]["step"] == "qa" and v["workflow"]["qa_round"] == 1
    decide(setup, "qa_passed")

    prod = run(setup)
    report(setup, prod)
    decide(setup, "approve", run_id=prod["run_id"])
    decide(setup, "close")
    v = view(setup)
    assert v["workflow"]["state"] == "done" and v["status"]["code"] == "done" and kinds(v) == ["prod_fix", "reset"]
    assert all(s["state"] in ("done", "skipped") for s in v["steps"])
    assert {s["key"]: s["state"] for s in v["steps"]}["estimate"] == "skipped"
    assert [e["kind"] for e in reversed(v["events"])] == [
        "start", "run", "approve", "skip", "run", "approve", "finish", "back_to_develop", "run", "approve", "finish",
        "run", "approve",
        "run", "approve", "qa_issues", "run", "approve", "finish", "qa_passed", "run", "approve", "close"]
    assert len(v["runs"]) == 7 and all(r["status"] == "approved" for r in v["runs"])


def test_start_later_records_skips_and_needs_analysis(setup):
    with pytest.raises(ValueError, match="start at Intake"):
        wf.start_workflow("5001", "15033", "develop", "x", setup["config"], setup["base"], now=NOW)
    (docs(setup) / "analysis.md").write_text("plan\n")
    wf.start_workflow("5001", "15033", "qa", "x", setup["config"], setup["base"], now=NOW)
    v = view(setup)
    assert v["workflow"]["step"] == "qa"
    assert [e["step"] for e in v["events"] if e["kind"] == "skip"] == ["release_qa", "review", "develop", "estimate", "intake"]
    with pytest.raises(ValueError, match="already has a workflow"):
        wf.start_workflow("5001", "15033", "intake", "x", setup["config"], setup["base"], now=NOW)


def test_gates(setup):
    wf.start_workflow("5001", "15033", "intake", "x", setup["config"], setup["base"], now=NOW)
    decide(setup, "skip", note="done by hand")
    decide(setup, "skip", note="no estimate")
    v = view(setup)
    assert v["workflow"]["step"] == "develop" and "analysis.md is missing" in v["actions"][0]["blocked"]
    with pytest.raises(ValueError, match="analysis.md is missing"):
        run(setup)
    (docs(setup) / "analysis.md").write_text("plan\n")
    with pytest.raises(ValueError, match="Development blocked"):
        run(setup, stages={"Development": "blocked"})
    with pytest.raises(ValueError, match="can't"):
        decide(setup, "qa_passed")  # not at Waiting on QA
    with pytest.raises(ValueError, match="can't"):
        decide(setup, "finish")  # no approved Develop run yet
    assert len(setup["launched"]) == 0


def test_needs_input_resume_and_no_result(setup):
    wf.start_workflow("5001", "15033", "intake", "x", setup["config"], setup["base"], now=NOW)
    started = run(setup)
    report(setup, started, status="needs_input", questions=["Which report?"])
    v = view(setup)
    assert v["runs"][0]["status"] == "needs_input" and kinds(v) == ["resume", "reject", "reset", "stop"]
    with pytest.raises(ValueError, match="can't"):
        decide(setup, "approve", run_id=started["run_id"])  # nothing to approve yet

    wf.resume_run("5001", started["run_id"], setup["config"], setup["base"], launcher=setup["launcher"], now=NOW)
    args = setup["launched"][-1]
    assert flag(args, "--resume") == started["session_id"] and "write timeline/5001/runs/" in prompt_of(args)
    assert "--mcp-config" in args
    v = view(setup)
    assert v["runs"][0]["status"] == "running" and "Which report?" in v["events"][0]["note"]

    # Claude exits without writing a result: the terminal's exit file says so
    exit_file = os.path.join(started["path"], "timeline", "5001", "runs", f"{started['run_id']}.exit")
    with open(exit_file, "w") as fh:
        fh.write("0\n")
    v = view(setup)
    assert v["runs"][0]["status"] == "no_result" and v["runs"][0]["exit_code"] == 0
    assert v["status"]["code"] == "needs_you"
    decide(setup, "reject", run_id=started["run_id"], note="Closed the terminal by mistake")
    v = view(setup)
    assert v["runs"][0]["status"] == "rejected" and v["workflow"]["step"] == "intake" and kinds(v)[0] == "run"


def test_bad_result_file_and_stop_reopen(setup):
    wf.start_workflow("5001", "15033", "intake", "x", setup["config"], setup["base"], now=NOW)
    started = run(setup)
    with open(os.path.join(started["path"], wf.result_file("5001", started["run_id"])), "w") as fh:
        fh.write("{not json")
    v = view(setup)
    assert v["runs"][0]["status"] == "bad_result" and "can't be read" in v["runs"][0]["result"]["error"]
    decide(setup, "stop")  # no reason asked
    assert view(setup)["status"]["label"] == "Stopped at Intake" and kinds(view(setup)) == ["reopen", "reset"]
    decide(setup, "reopen")
    assert view(setup)["workflow"]["state"] == "active"


def test_clean_result():
    r = wf.clean_result({"status": "weird", "summary": 3, "files": "a.php", "remaining": True, "extra": 1})
    assert r["status"] == "needs_input" and r["summary"] == "3" and r["files"] == ["a.php"]
    assert "remaining" not in r and "extra" not in r and "weird" in r["questions"][0]
    assert wf.clean_result([1])["status"] == "bad_result"


def test_docs_and_transcript(setup, tmp_path):
    folder = docs(setup)
    (folder / "analysis.md").write_text("# Plan\n")
    (folder / "notes.bin").write_text("x")
    repo = setup["config"]["repos"]["15033"]
    assert [d["name"] for d in wf.list_docs(repo, "5001")] == ["analysis.md"]
    assert wf.read_doc(repo, "5001", "analysis.md")["text"] == "# Plan\n"
    for bad in ("../timeline-desk.json", "notes.bin", ".md"):
        with pytest.raises((ValueError, LookupError)):
            wf.read_doc(repo, "5001", bad)

    wf.start_workflow("5001", "15033", "intake", "x", setup["config"], setup["base"], now=NOW)
    started = run(setup)
    home = tmp_path / "claude-home"
    log = home / "projects" / "-some-folder" / f"{started['session_id']}.jsonl"
    log.parent.mkdir(parents=True)
    lines = [
        {"type": "user", "message": {"content": "Panteon task #5001 ..."}, "timestamp": "t1"},
        {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "hidden"},
                                                      {"type": "text", "text": "Reading the task."},
                                                      {"type": "tool_use", "name": "Bash", "input": {"command": "git status"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "clean"}]}},
        {"type": "user", "isMeta": True, "message": {"content": "meta"}},
        {"type": "summary"},
    ]
    log.write_text("\n".join(json.dumps(l) for l in lines) + "\nnot json\n")
    t = wf.read_transcript("5001", started["run_id"], setup["base"], str(home))
    assert t["found"] and [(i["who"], i["text"]) for i in t["items"]] == [
        ("you", "Panteon task #5001 ..."), ("claude", "Reading the task."), ("tool", "Bash: git status")]
    assert wf.read_transcript("5001", started["run_id"], setup["base"], str(tmp_path / "none"))["found"] is False


# ---- phase 2: following Timeline ---------------------------------------------
def add_events(setup, *rows, issue="5001"):
    """task_events rows as the heartbeat would store them: (id, kind, old, new, employee)."""
    conn = pc._open_planner_db(setup["base"], create=True)
    for event_id, kind, old, new, who in rows:
        field = {"status": "Status", "comment": "Comment Added", "commit": "Commit Added"}.get(kind, kind)
        conn.execute("INSERT INTO task_events (id, issue_number, occurred_at, kind, field_name, old_value, new_value, employee, raw) "
                     "VALUES (?, ?, '2026-10-27T10:00:00', ?, ?, ?, ?, ?, '{}')",
                     (event_id, issue, kind if kind in ("status", "comment", "commit") else "field", field, old, new, who))
    conn.commit()
    conn.close()


def test_parse_stage():
    assert wf.parse_stage("Testing pending") == {"stage": "testing", "progress": "pending", "rank": 3}
    assert wf.parse_stage("Going live in progress")["rank"] == 5
    assert wf.parse_stage("Something else") is None


def test_signals_suggest_and_ack(setup):
    setup["config"]["me"] = "Deep Panara"
    (docs(setup) / "analysis.md").write_text("plan\n")
    add_events(setup, (1, "comment", None, None, "Kari"))  # before the workflow: history
    wf.start_workflow("5001", "15033", "qa", "x", setup["config"], setup["base"], now=NOW)
    wf.reset_baseline("5001", setup["base"])
    assert pc._open_planner_db(setup["base"], create=False).execute(
        "SELECT active FROM watchlist WHERE issue_number = '5001'").fetchone()[0] == 1  # auto-watched
    v = view(setup)
    assert v["signals"] == [] and v["status"]["code"] == "waiting" and v["watched"]

    add_events(setup, (2, "status", "Testing pending", "QA in progress", "Kari"),
               (3, "comment", None, None, "Kari"), (4, "comment", None, None, "deep  panara"))
    v = view(setup)
    assert [s["kind"] for s in v["signals"]] == ["qa_comment", "stage"]  # newest first; my own comment is skipped
    assert v["signals"][0]["suggest"] == "qa_issues"
    assert v["status"]["code"] == "attention" and "1 new" in v["status"]["short"]
    acts = {a["kind"]: a for a in v["actions"]}
    assert acts["qa_issues"]["suggested"] and not acts["qa_passed"]["suggested"] and "ack" in acts
    decide(setup, "ack")
    v = view(setup)
    assert v["signals"] == [] and "ack" not in kinds(v)

    add_events(setup, (5, "status", "QA in progress", "QA done", "Kari"))
    v = view(setup)
    assert v["signals"][0]["kind"] == "qa_done" and {a["kind"]: a for a in v["actions"]}["qa_passed"]["suggested"]
    decide(setup, "qa_passed")  # acting on it marks the changes as seen
    assert view(setup)["signals"] == []
    listed = wf.list_workflows(setup["base"], "Deep Panara")
    assert listed[0]["step"] == "release_prod" and listed[0]["signals"] == 0


def test_signal_kinds_by_step():
    def sig(kind, new=None, step="develop", who="Kari", old="x"):
        return wf.signal_for({"id": 1, "occurred_at": "t", "kind": kind, "old_value": old, "new_value": new,
                              "employee": who, "field_name": "Deadline" if kind == "field" else None}, step)
    assert sig("status", "Testing pending")["kind"] == "qa_started"
    assert sig("status", "Development in progress", step="qa")["suggest"] == "qa_issues"
    assert sig("status", "Development blocked")["suggest"] == "explain_blocker"
    assert sig("status", "Live done", step="qa_fix")["kind"] == "qa_done"
    assert sig("comment")["kind"] == "comment" and sig("comment")["level"] == "info"
    assert sig("field", "2026-11-01")["level"] == "warn"


def test_blocked_side_run(setup):
    (docs(setup) / "analysis.md").write_text("plan\n")
    wf.start_workflow("5001", "15033", "develop", "x", setup["config"], setup["base"], now=NOW)
    assert "explain_blocker" not in kinds(view(setup))
    add_events(setup, (7, "status", "Development in progress", "Development blocked", "PM"))
    v = view(setup)
    assert v["blocked"] == "Development blocked" and "explain_blocker" in kinds(v)
    side = run(setup, side="unblock")
    assert "blocker.md" in prompt_of(setup["launched"][-1]) and "Read only" in prompt_of(setup["launched"][-1])
    report(setup, side)
    decide(setup, "approve", run_id=side["run_id"])
    v = view(setup)
    assert v["workflow"]["step"] == "develop" and v["runs"][0]["side"] and v["runs"][0]["step_title"] == "Explain the blocker"


# ---- phase 3: QA findings, replies, prod fixes ---------------------------------
def at_qa_fix(setup):
    setup["config"]["me"] = "Deep Panara"
    (docs(setup) / "analysis.md").write_text("plan\n")
    add_events(setup, (10, "comment", None, None, "Kari"))  # before QA: not a finding
    wf.start_workflow("5001", "15033", "release_qa", "x", setup["config"], setup["base"], now=NOW)
    wf.reset_baseline("5001", setup["base"])
    release = run(setup)
    report(setup, release)
    decide(setup, "approve", run_id=release["run_id"])  # into Waiting on QA: a new QA window
    add_events(setup, (20, "comment", None, None, "Kari"), (21, "comment", None, None, "Deep Panara"))
    v = view(setup)
    assert [(f["event_id"], f["status"]) for f in v["findings"]] == [(20, "new")]  # mine and older ones skipped
    decide(setup, "qa_issues")


def test_findings_checklist_and_fix_runs(setup):
    at_qa_fix(setup)
    v = view(setup)
    run_action = next(a for a in v["actions"] if a["kind"] == "run")
    assert "No finding is marked Bug" in run_action["blocked"]
    with pytest.raises(ValueError, match="No finding is marked Bug"):
        run(setup)
    f1 = v["findings"][0]["id"]
    with pytest.raises(ValueError, match="reason"):
        wf.classify_finding("5001", f1, "not_bug", setup["base"], setup["config"])
    wf.classify_finding("5001", f1, "bug", setup["base"], setup["config"], now=NOW)
    f2 = wf.add_finding("5001", "Total column misaligned", setup["base"], setup["config"], now=NOW)["finding"]

    first = run(setup)  # the oldest open bug
    prompt = prompt_of(setup["launched"][-1])
    assert f"QA finding F{f1}: the QA comment with comment id 20" in prompt and f'"finding": {f1}' in prompt
    report(setup, first, remaining=1)
    decide(setup, "approve", run_id=first["run_id"])
    with pytest.raises(ValueError, match="still new or open"):
        decide(setup, "finish")  # F2 is still an open bug
    second = run(setup, finding_id=f2)
    assert 'My label for it: "Total column misaligned"' in prompt_of(setup["launched"][-1])
    report(setup, second, remaining=0)
    decide(setup, "approve", run_id=second["run_id"])
    v = view(setup)
    assert {f["id"]: f["status"] for f in v["findings"]} == {f1: "fixed", f2: "fixed"}
    assert all(not f["can_fix"] for f in v["findings"])
    decide(setup, "finish")
    v = view(setup)
    assert v["workflow"]["step"] == "qa" and v["workflow"]["qa_round"] == 1
    assert [e["kind"] for e in v["events"]].count("finding") == 1 and "finding_added" in [e["kind"] for e in v["events"]]


def test_question_reply_side_run(setup):
    at_qa_fix(setup)
    f1 = view(setup)["findings"][0]["id"]
    wf.classify_finding("5001", f1, "question", setup["base"], setup["config"], note="Asks about rounding", now=NOW)
    assert view(setup)["findings"][0]["can_reply"]
    side = run(setup, side="qa_reply", finding_id=f1)
    assert f"replies/F{f1}.md" in prompt_of(setup["launched"][-1]) and "Don't post anything" in prompt_of(setup["launched"][-1])
    report(setup, side)
    decide(setup, "approve", run_id=side["run_id"])
    v = view(setup)
    assert v["workflow"]["step"] == "qa_fix" and v["findings"][0]["status"] == "question"
    with pytest.raises(ValueError, match="isn't a Question"):
        wf.classify_finding("5001", f1, "bug", setup["base"], setup["config"], now=NOW)
        run(setup, side="qa_reply", finding_id=f1)


def test_prod_fix_after_close(setup):
    (docs(setup) / "analysis.md").write_text("plan\n")
    wf.start_workflow("5001", "15033", "close", "x", setup["config"], setup["base"], now=NOW)
    assert all(s["hidden"] for s in view(setup)["steps"] if s.get("extra"))
    decide(setup, "close")
    with pytest.raises(ValueError, match="reason"):
        decide(setup, "prod_fix")
    decide(setup, "prod_fix", note="Wrong VAT on live")
    v = view(setup)
    assert v["workflow"]["step"] == "prod_fix" and v["workflow"]["prod_round"] == 1 and v["workflow"]["state"] == "active"
    assert not any(s["hidden"] for s in v["steps"])
    fix = run(setup)
    assert "phase prod-fix" in prompt_of(setup["launched"][-1]) and "merge --ff-only" in prompt_of(setup["launched"][-1])
    report(setup, fix, remaining=0)
    decide(setup, "approve", run_id=fix["run_id"])
    decide(setup, "finish")
    hot = run(setup)
    assert "target hotfix" in prompt_of(setup["launched"][-1])
    report(setup, hot)
    decide(setup, "approve", run_id=hot["run_id"])
    decide(setup, "close")
    assert view(setup)["workflow"]["state"] == "done"


# ---- dashboard endpoints ------------------------------------------------
@pytest.fixture
def client(setup, monkeypatch):
    monkeypatch.setenv("PANTEON_BEARER_TOKEN", "tok")
    stages = {"5001": {"Development": "in progress"}, "5002": {"Development": "blocked"}}
    monkeypatch.setattr(dashboard.task_events, "sync_watchlist", lambda headers, base_dir, issues=None, **kw: {"tasks": 0})
    monkeypatch.setattr(pc, "get_task_statuses", lambda ids, headers: [
        {"issue_id": i, "title": "From Timeline", "stages": stages.get(i, {})} if i != "404" else {"issue_id": i, "error": "not found"}
        for i in ids])
    app = dashboard.create_app(user_id=135, base_dir=setup["base"], work_config=lambda: setup["config"],
                               work_launcher=setup["launcher"],
                               claude_home=str(setup["tmp"] / "claude-home"))
    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_view_finds_the_board_from_the_project(client, setup, monkeypatch):
    # Opened by URL (no ?code=): Timeline's project id picks the board through the repo ids.
    monkeypatch.setattr(pc, "get_task_statuses", lambda ids, headers: [
        {"issue_id": i, "title": "T", "project_id": {"5001": 16, "5002": 99}.get(i), "stages": {}} for i in ids])
    v = client.get("/api/workflow/5001").json()
    assert v["code"] is None and not v["startable"] and "project id 16" in v["start_note"]  # no repo has an id yet
    setup["config"]["repos"]["15033"]["id"] = 16
    v = client.get("/api/workflow/5001").json()
    assert v["code"] == "15033" and v["startable"] and v["repo"] == "duell-admin"
    v = client.get("/api/workflow/5002").json()
    assert not v["startable"] and "project id 99" in v["start_note"]
    assert ws.board_for_project({"repos": {"1": {"id": 16}, "2": {"id": 16}}}, 16) is None  # ambiguous
    assert ws.board_for_project(setup["config"], "16") == "15033"


def test_workflow_endpoints(client, setup):
    h = {"X-Timeline-Desk": "1"}
    v = client.get("/api/workflow/5001?code=15033").json()
    assert v["workflow"] is None and v["proposal"]["step"] == "intake" and v["startable"]
    assert client.get("/api/workflows").json() == {"workflows": []}

    assert client.post("/api/workflow/5001/start", json={"code": "15033", "step": "intake"}).status_code == 403
    v = client.post("/api/workflow/5001/start", json={"code": "15033", "step": "intake"}, headers=h).json()
    assert v["workflow"]["branch"] == "5001-from-timeline" and v["workflow"]["title"] == "From Timeline"
    assert client.post("/api/workflow/404/start", json={"code": "15033", "step": "intake"}, headers=h).status_code == 404
    assert client.post("/api/workflow/5001/start", json={"code": "15033", "step": "intake"}, headers=h).status_code == 400

    assert client.post("/api/workflow/5001/run", headers={}).status_code == 403
    v = client.post("/api/workflow/5001/run", headers=h).json()
    run_id = v["started"]["run_id"]
    assert v["runs"][0]["status"] == "running" and len(setup["launched"]) == 1
    assert client.get("/api/workflows").json()["workflows"][0]["status"]["code"] == "running"

    report(setup, {"path": v["workflow"]["worktree"], "run_id": run_id})
    (docs(setup) / "analysis.md").write_text("# Plan\n")
    v = client.post("/api/workflow/5001/decide", json={"kind": "approve", "run_id": run_id}, headers=h).json()
    assert v["workflow"]["step"] == "estimate"
    assert client.post("/api/workflow/5001/decide", json={"kind": "skip"}, headers=h).status_code == 400  # no reason
    assert client.get("/api/workflow/5001/doc/analysis.md").json()["text"] == "# Plan\n"
    assert client.get("/api/workflow/5001/doc/..%2Fx.md").status_code in (400, 404)
    t = client.get(f"/api/workflow/5001/runs/{run_id}/transcript").json()
    assert t["found"] is False and t["session_id"] == v["runs"][0]["session_id"]

    # A blocked task can't run Develop
    (docs(setup, "5002") / "analysis.md").write_text("plan\n")
    client.post("/api/workflow/5002/start", json={"code": "15033", "step": "develop"}, headers=h)
    r = client.post("/api/workflow/5002/run", headers=h)
    assert r.status_code == 400 and "blocked" in r.json()["error"]


def test_finding_endpoints(client, setup):
    h = {"X-Timeline-Desk": "1"}
    (docs(setup) / "analysis.md").write_text("plan\n")
    client.post("/api/workflow/5001/start", json={"code": "15033", "step": "qa"}, headers=h)
    client.post("/api/workflow/5001/decide", json={"kind": "qa_issues"}, headers=h)
    assert client.post("/api/workflow/5001/findings", json={"label": "Totals off"}).status_code == 403
    v = client.post("/api/workflow/5001/findings", json={"label": "Totals off"}, headers=h).json()
    fid = v["findings"][0]["id"]
    assert v["findings"][0]["status"] == "bug" and v["findings"][0]["can_fix"]
    r = client.post(f"/api/workflow/5001/findings/{fid}", json={"status": "not_bug"}, headers=h)
    assert r.status_code == 400 and "reason" in r.json()["error"]
    v = client.post(f"/api/workflow/5001/findings/{fid}", json={"status": "wont_fix", "note": "By design"}, headers=h).json()
    assert v["findings"][0]["status"] == "wont_fix"
    v = client.post(f"/api/workflow/5001/findings/{fid}", json={"status": "bug"}, headers=h).json()
    v = client.post("/api/workflow/5001/run", json={"finding_id": fid}, headers=h).json()
    assert v["runs"][0]["status"] == "running" and f"F{fid}" in v["events"][0]["note"]


# ---- fixes after the first real run (#6110) ----------------------------------------
def test_terminal_run_closed_without_exit_file_ends(setup, monkeypatch):
    wf.start_workflow("5001", "15033", "intake", "x", setup["config"], setup["base"], now=NOW)
    started = run(setup)
    monkeypatch.setattr(wf.work_session, "session_alive", lambda session_id: False)
    assert view(setup)["runs"][0]["status"] == "running"  # just opened: the terminal gets a minute to start
    launched = os.path.join(started["path"], "timeline", "5001", "runs", f"{started['run_id']}.launched")
    old = time.time() - wf.TERMINAL_GRACE_SECONDS - 5
    os.utime(launched, (old, old))
    monkeypatch.setattr(wf.work_session, "session_alive", lambda session_id: None)
    assert view(setup)["runs"][0]["status"] == "running"  # can't tell (no /proc): leave it
    monkeypatch.setattr(wf.work_session, "session_alive", lambda session_id: False)
    v = view(setup)
    assert v["runs"][0]["status"] == "no_result" and kinds(v) == ["resume", "reject", "reset", "stop"]


def test_session_alive_reads_command_lines():
    import uuid as _uuid
    sid = str(_uuid.uuid4())
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)", sid])
    try:
        assert REAL_SESSION_ALIVE(sid) is True
    finally:
        proc.kill()
        proc.wait()
    assert REAL_SESSION_ALIVE(sid) is False


def test_stop_workflow_ends_the_open_run(setup):
    wf.start_workflow("5001", "15033", "intake", "x", setup["config"], setup["base"], now=NOW)
    started = run(setup)
    decide(setup, "stop", note="no need")
    v = view(setup)
    assert v["runs"][0]["status"] == "rejected" and v["runs"][0]["note"] == "Workflow stopped: no need"
    assert v["events"][0]["kind"] == "stop" and v["events"][0]["run_id"] == started["run_id"]
    decide(setup, "reopen")
    assert kinds(view(setup))[0] == "run"  # nothing left open


def test_reset_erases_workflow_and_task_docs(setup, monkeypatch):
    setup["config"]["me"] = "Deep Panara"
    (docs(setup) / "analysis.md").write_text("plan\n")
    wf.start_workflow("5001", "15033", "qa", "x", setup["config"], setup["base"], now=NOW)
    add_events(setup, (30, "comment", None, None, "Kari"))
    decide(setup, "qa_issues")
    wf.classify_finding("5001", view(setup)["findings"][0]["id"], "bug", setup["base"], setup["config"], now=NOW)
    started = run(setup)  # a terminal run, still open
    worktree_docs = os.path.join(started["path"], "timeline", "5001")
    assert os.path.isfile(os.path.join(worktree_docs, "analysis.md"))  # through the worktree's link
    v = view(setup)
    preview = v["reset_preview"]
    assert preview["runs"] == 1 and preview["findings"] == 1 and len(preview["folders"]) == 1
    assert "analysis.md" in preview["folders"][0]["files"] and f"runs/{started['run_id']}.launched" in preview["folders"][0]["files"]

    with pytest.raises(ValueError, match="Type 5001"):
        wf.reset_workflow("5001", "5002", setup["base"], setup["config"])
    with pytest.raises(ValueError, match="still open in a terminal"):
        wf.reset_workflow("5001", "5001", setup["base"], setup["config"])
    monkeypatch.setattr(wf.work_session, "session_alive", lambda session_id: False)
    os.utime(os.path.join(worktree_docs, "runs", f"{started['run_id']}.launched"), (0, 0))  # its terminal was closed long ago

    removed = wf.reset_workflow("5001", "#5001", setup["base"], setup["config"])
    assert removed["runs"] == 1 and removed["findings"] == 1 and removed["files"] >= 2
    assert not (setup["repo"] / "timeline" / "5001").exists() and os.path.islink(os.path.join(started["path"], "timeline"))
    assert (setup["repo"] / "timeline").is_dir()  # only the task's own folder goes
    conn = pc._open_planner_db(setup["base"], create=False)
    for table in ("workflows", "workflow_runs", "workflow_events", "qa_findings"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE issue_number = '5001'").fetchone()[0] == 0, table
    assert conn.execute("SELECT COUNT(*) FROM task_events WHERE issue_number = '5001'").fetchone()[0] == 1  # Timeline's history stays
    assert os.path.isdir(started["path"])  # the worktree stays
    v = view(setup, code="15033")
    assert v["workflow"] is None and v["proposal"]["step"] == "intake" and v["startable"]
    with pytest.raises(LookupError):
        wf.reset_workflow("5001", "5001", setup["base"], setup["config"])


def test_reset_endpoint(client, setup):
    h = {"X-Timeline-Desk": "1"}
    client.post("/api/workflow/5001/start", json={"code": "15033", "step": "intake"}, headers=h)
    (docs(setup) / "task.md").write_text("t\n")
    assert client.post("/api/workflow/5001/reset", json={"confirm": "5001"}).status_code == 403
    assert client.post("/api/workflow/5001/reset", json={"confirm": "1"}, headers=h).status_code == 400
    v = client.post("/api/workflow/5001/reset", json={"confirm": "5001", "code": "15033"}, headers=h).json()
    assert v["workflow"] is None and v["removed"]["files"] == 1 and v["startable"]
    assert not (setup["repo"] / "timeline" / "5001").exists()


# ---- what the terminal is doing ----------------------------------------------------
def write_session(tmp_path, session_id, entries, age=0):
    folder = tmp_path / "no-claude-home" / "projects" / "-some-worktree"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{session_id}.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    when = time.time() - age
    os.utime(path, (when, when))


def assistant(*blocks):
    return {"type": "assistant", "message": {"content": list(blocks)}}


def tool_use(name="Bash", **args):
    return {"type": "tool_use", "id": "t1", "name": name, "input": args}


def test_terminal_activity_follows_the_session_log(setup, tmp_path):
    sid = "11111111-2222-3333-4444-555555555555"
    r = {"session_id": sid}
    assert wf.terminal_activity(r)["state"] == "starting"  # no log yet

    prompt = {"type": "user", "message": {"content": "Panteon task #5001 …"}}
    write_session(tmp_path, sid, [prompt])
    assert wf.terminal_activity(r)["state"] == "working"

    write_session(tmp_path, sid, [prompt, assistant(tool_use(command="git status"))], age=1)
    a = wf.terminal_activity(r)
    assert a["state"] == "tool" and a["detail"] == "Bash: git status" and a["idle"] <= 3

    write_session(tmp_path, sid, [prompt, assistant(tool_use(command="git status"))], age=30)
    a = wf.terminal_activity(r)
    assert a["state"] == "waiting" and "approve it in the terminal" in a["detail"] and a["idle"] >= 30

    write_session(tmp_path, sid, [prompt, assistant(tool_use("AskUserQuestion", question="Which tenant?"))], age=30)
    assert wf.terminal_activity(r)["state"] == "question"

    result = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}
    write_session(tmp_path, sid, [prompt, assistant(tool_use(command="ls")), result], age=30)
    assert wf.terminal_activity(r)["state"] == "working"  # got the tool's output, thinking

    write_session(tmp_path, sid, [prompt, assistant({"type": "text", "text": "Plan: change a.txt. Go ahead?"})], age=1)
    assert wf.terminal_activity(r)["state"] == "working"  # still writing, perhaps
    write_session(tmp_path, sid, [prompt, assistant({"type": "text", "text": "Plan: change a.txt. Go ahead?"})], age=30)
    assert wf.terminal_activity(r)["state"] == "reply"


def test_open_run_shows_its_activity_and_asks_you(setup, tmp_path):
    wf.start_workflow("5001", "15033", "intake", "x", setup["config"], setup["base"], now=NOW)
    started = run(setup)
    v = view(setup)
    assert v["runs"][0]["activity"]["state"] == "starting" and v["status"]["code"] == "running"

    prompt = {"type": "user", "message": {"content": "go"}}
    write_session(tmp_path, started["session_id"], [prompt, assistant({"type": "text", "text": "Which tenant?"})], age=30)
    v = view(setup)
    assert v["runs"][0]["activity"]["state"] == "reply" and v["runs"][0]["status"] == "running"
    assert v["status"]["code"] == "needs_you" and v["status"]["short"].endswith("asks you")
    listed = wf.list_workflows(setup["base"])
    assert listed[0]["status"]["code"] == "needs_you"

    report(setup, started)  # Claude wrote its result and exited: the activity line goes away
    open(os.path.join(started["path"], "timeline", "5001", "runs", f"{started['run_id']}.exit"), "w").write("0\n")
    v = view(setup)
    assert v["runs"][0]["status"] == "review" and v["runs"][0]["activity"] is None
    decide(setup, "approve", run_id=started["run_id"])
    assert view(setup)["runs"][0]["activity"] is None


def test_no_background_run_modes_are_left(setup):
    wf.start_workflow("5001", "15033", "intake", "x", setup["config"], setup["base"], now=NOW)
    assert not hasattr(wf, "answer_run") and not hasattr(wf, "message_run") and not hasattr(wf, "stop_run")
    with pytest.raises(TypeError):
        wf.run_step("5001", setup["config"], setup["base"], launcher=setup["launcher"], mode="agent")
    started = run(setup)
    assert "-p" not in setup["launched"][-1] and "gnome-terminal" == setup["launched"][-1][0]
    assert "mode" not in started


def test_branch_mode_prompt_and_runs_in_main_checkout(setup, monkeypatch):
    cfg = setup["config"]
    cfg["repos"]["15033"]["workspace"] = "branch"
    repo = str(setup["repo"])
    wf.start_workflow("5001", "15033", "intake", "New report", cfg, setup["base"], now=NOW)
    started = run(setup)
    assert started["path"] == repo and started["branch"] == "5001-new-report"
    prompt = prompt_of(setup["launched"][-1])
    assert "main checkout of duell-admin" in prompt and "git worktree" not in prompt
    assert git(setup["repo"], "symbolic-ref", "--short", "HEAD").strip() == "5001-new-report"
    # a second task can't switch the shared checkout while 5001's terminal is still open
    with pytest.raises(ValueError, match="#5001 run .* is still open"):
        wf.start_workflow("5002", "15033", "intake", "Other", cfg, setup["base"], now=NOW)
    assert git(setup["repo"], "symbolic-ref", "--short", "HEAD").strip() == "5001-new-report"
    # once that terminal is gone, it can
    monkeypatch.setattr(wf.work_session, "session_alive", lambda session_id: False)
    wf.start_workflow("5002", "15033", "intake", "Other", cfg, setup["base"], now=NOW)
    assert git(setup["repo"], "symbolic-ref", "--short", "HEAD").strip() == "5002-other"


def test_worktree_mode_prompt_unchanged():
    repo = {"name": "duell-admin", "base": "master", "path": "/repos/duell-admin", "workspace": "worktree"}
    assert "git worktree of duell-admin" in wf.build_prompt("develop", "6034", repo, "6034-x", 7)
