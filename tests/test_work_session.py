"""work_session: worktrees and the Claude launch, against real git repos in tmp (no network)."""
import json
import os
import subprocess

import pytest
from starlette.testclient import TestClient

import dashboard
import panteon_client as pc
import work_session as ws


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def setup(tmp_path):
    """An 'origin' with master and 6034-existing, a clone of it, and a config for board 15033."""
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "master")
    git(seed, "config", "user.email", "t@example.com")
    git(seed, "config", "user.name", "T")
    (seed / "a.txt").write_text("a\n")
    git(seed, "add", ".")
    git(seed, "commit", "-qm", "init")
    git(seed, "checkout", "-qb", "6034-existing")
    (seed / "b.txt").write_text("b\n")
    git(seed, "add", "b.txt")
    git(seed, "commit", "-qm", "x")
    git(seed, "checkout", "-q", "master")
    origin = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    repo = tmp_path / "duell-admin"
    git(tmp_path, "clone", "-q", str(origin), str(repo))
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    config_file = tmp_path / "timeline-desk.json"
    config_file.write_text(json.dumps({
        "repos": {"15033": {"name": "duell-admin", "path": "duell-admin", "base": "master", "workspace": "worktree"}},
        "claude": "/opt/node/bin/claude",
        "terminal": "gnome-terminal",
    }))
    launched = []
    return {
        "tmp": tmp_path, "repo": repo, "config": ws.load_config(str(config_file)),
        "launched": launched, "launcher": lambda args, env: launched.append((args, env)),
    }


def test_slug_and_prompt():
    assert ws.branch_name("5975", "[PRI2] [BO] [Billakksenteret AS]  Unused product number") == "5975-unused-product-number"
    assert ws.branch_name("6034", "BUG: Cannot delete Course") == "6034-cannot-delete-course"
    assert ws.slugify("Søk på ordre") == "sk-pa-ordre"  # ascii only
    assert ws.slugify("!!!") == "task"
    long = "Serial number does not get saved if order has packing slip generated and customer is invoiced later"
    assert ws.branch_name("6117", long) == "6117-" + long.lower().replace(" ", "-")  # the whole title, never cut short
    huge = ws.branch_name("6117", "word " * 100)
    assert len(huge) <= ws.MAX_BRANCH and huge.endswith("-word")  # only past the file-name limit, at a word break


def test_load_config_defaults(tmp_path):
    assert ws.load_config(str(tmp_path / "missing.json"))["repos"] == {}
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"repos": {"1": {"path": "../x"}}}))
    repo = ws.load_config(str(cfg))["repos"]["1"]
    assert repo["path"] == os.path.normpath(str(tmp_path / "../x"))
    assert repo["worktrees"] == repo["path"] + "-worktrees" and repo["base"] == "master" and repo["name"] == "x"
    assert repo["workspace"] == "branch"  # tasks run in the main checkout unless the repo opts into worktrees
    cfg.write_text(json.dumps({"repos": {"1": {"path": "../x", "workspace": "folder"}}}))
    with pytest.raises(ws.WorkSessionError):
        ws.load_config(str(cfg))
    cfg.write_text("{bad")
    with pytest.raises(ws.WorkSessionError):
        ws.load_config(str(cfg))


def test_worktree_created_then_reused(setup):
    cfg = setup["config"]
    result = ws.worktree_for(cfg, "15033", "5001", "New report")
    path = os.path.join(cfg["repos"]["15033"]["worktrees"], "duell-admin-5001")  # the folder is named by the ID only
    assert result == {"path": path, "branch": "5001-new-report", "created": True, "repo": "duell-admin",
                      "workspace": "worktree"}
    assert os.path.isfile(os.path.join(path, "a.txt"))
    again = ws.worktree_for(cfg, "15033", "5001", "Renamed")
    assert again["created"] is False and again["path"] == path
    # Found by its folder too, even after the branch is renamed.
    git(path, "branch", "-m", "renamed-by-hand")
    assert ws.worktree_for(cfg, "15033", "5001", "x")["path"] == path


def test_worktree_reuses_remote_branch(setup):
    result = ws.worktree_for(setup["config"], "15033", "6034", "Whatever")
    assert result["branch"] == "6034-existing" and result["created"] is True
    assert result["path"].endswith(os.sep + "duell-admin-6034")
    assert os.path.isfile(os.path.join(result["path"], "b.txt"))
    assert git(result["path"], "rev-parse", "--abbrev-ref", "@{u}").strip() == "origin/6034-existing"


def test_worktree_validates(setup):
    with pytest.raises(ValueError, match="no repo"):
        ws.worktree_for(setup["config"], "999", "5001", "x")
    with pytest.raises(ValueError, match="number"):
        ws.worktree_for(setup["config"], "15033", "50; rm", "x")


def test_terminal_command_passes_args_and_exit_file(setup):
    args = ws.terminal_command(setup["config"], "/w", "#1", ["--session-id", "abc", "the prompt"], "/w/x.exit")
    assert args[0] == "gnome-terminal" and "--working-directory=/w" in args
    assert args[-6:] == ["/w", "/w/x.exit", "/opt/node/bin/claude", "--session-id", "abc", "the prompt"]
    assert ws.launch_env(setup["config"])["PATH"].startswith("/opt/node/bin" + os.pathsep)


def test_worktree_timeline_links_to_main_checkout(setup):
    cfg, repo = setup["config"], setup["repo"]
    path = ws.worktree_for(cfg, "15033", "5001", "New report")["path"]
    link = os.path.join(path, "timeline")
    assert os.path.islink(link) and os.path.realpath(link) == os.path.realpath(repo / "timeline")
    (repo / "timeline" / "5001").mkdir()
    (repo / "timeline" / "5001" / "analysis.md").write_text("plan\n")
    assert open(os.path.join(link, "5001", "analysis.md")).read() == "plan\n"  # same files on both sides

    # git ignores the link (a `timeline/` gitignore line alone wouldn't), so the worktree stays clean
    assert git(path, "status", "--porcelain") == ""
    excluded = open(repo / ".git" / "info" / "exclude").read()
    ws.worktree_for(cfg, "15033", "5001", "New report")
    assert open(repo / ".git" / "info" / "exclude").read() == excluded  # added once

    # removing the worktree drops the link, never the task docs
    git(repo, "worktree", "remove", path)
    assert not os.path.exists(path) and (repo / "timeline" / "5001" / "analysis.md").read_text() == "plan\n"


def test_old_worktree_timeline_is_merged(setup):
    cfg, repo = setup["config"], setup["repo"]
    path = ws.worktree_for(cfg, "15033", "5001", "x")["path"]
    os.remove(os.path.join(path, "timeline"))  # a worktree from before the link existed
    old = os.path.join(path, "timeline", "5001")
    os.makedirs(old)
    for name, text in {"spec.md": "spec\n", "task.md": "same\n", "progress.md": "mine\n"}.items():
        with open(os.path.join(old, name), "w") as fh:
            fh.write(text)
    (repo / "timeline" / "5001").mkdir(parents=True)
    (repo / "timeline" / "5001" / "task.md").write_text("same\n")
    (repo / "timeline" / "5001" / "progress.md").write_text("theirs\n")

    result = ws.worktree_for(cfg, "15033", "5001", "x")
    assert "5001/progress.md" in result["note"] and not os.path.islink(os.path.join(path, "timeline"))
    assert (repo / "timeline" / "5001" / "spec.md").read_text() == "spec\n"  # moved over
    assert sorted(os.listdir(old)) == ["progress.md"]  # identical task.md dropped, the clash kept

    os.remove(os.path.join(old, "progress.md"))
    result = ws.worktree_for(cfg, "15033", "5001", "x")
    assert "note" not in result and os.path.islink(os.path.join(path, "timeline"))
    assert (repo / "timeline" / "5001" / "progress.md").read_text() == "theirs\n"


# ---- dashboard endpoints ------------------------------------------------
@pytest.fixture
def client(setup, monkeypatch, tmp_path):
    monkeypatch.setenv("PANTEON_BEARER_TOKEN", "tok")
    monkeypatch.setattr(pc, "get_task_statuses", lambda ids, headers: [
        {"issue_id": i, "title": "From Timeline", "stages": {"Estimate": "done", "Development": "not started"}} if i != "404" else {"issue_id": i, "error": "not found"} for i in ids])
    app = dashboard.create_app(user_id=135, base_dir=str(tmp_path), work_config=lambda: setup["config"],
                               work_launcher=setup["launcher"])
    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_work_endpoints(client, setup):
    assert client.get("/api/work/config").json() == {"boards": {"15033": "duell-admin"}, "projects": []}
    assert client.post("/api/work/start", json={"issue": "5001", "code": "15033"}).status_code in (404, 405)  # gone


def branch_mode(setup):
    cfg = setup["config"]
    cfg["repos"]["15033"]["workspace"] = "branch"
    return cfg


def test_branch_mode_checks_out_in_main_checkout(setup):
    cfg, repo = branch_mode(setup), setup["repo"]
    result = ws.worktree_for(cfg, "15033", "5001", "New report")
    assert result["path"] == str(repo) and result["workspace"] == "branch" and result["created"]
    assert result["branch"] == "5001-new-report"
    assert git(repo, "symbolic-ref", "--short", "HEAD").strip() == "5001-new-report"
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1  # no extra folder
    assert not os.path.exists(cfg["repos"]["15033"]["worktrees"])
    # already on it: kept as is, even with uncommitted work (it's this task's)
    (repo / "a.txt").write_text("changed\n")
    again = ws.worktree_for(cfg, "15033", "5001", "Renamed")
    assert again["branch"] == "5001-new-report" and not again["created"]


def test_branch_mode_reuses_remote_branch(setup):
    cfg, repo = branch_mode(setup), setup["repo"]
    result = ws.worktree_for(cfg, "15033", "6034", "Whatever")
    assert result["branch"] == "6034-existing" and result["path"] == str(repo)
    assert git(repo, "rev-parse", "--abbrev-ref", "6034-existing@{upstream}").strip() == "origin/6034-existing"
    assert (repo / "b.txt").exists()


def test_branch_mode_refuses_uncommitted_changes(setup):
    cfg, repo = branch_mode(setup), setup["repo"]
    (repo / "a.txt").write_text("someone's work\n")
    with pytest.raises(ws.WorkSessionError, match="uncommitted changes on master"):
        ws.worktree_for(cfg, "15033", "5001", "x")
    assert git(repo, "symbolic-ref", "--short", "HEAD").strip() == "master"
    assert (repo / "a.txt").read_text() == "someone's work\n"  # untouched
    (repo / "a.txt").write_text("a\n")
    (repo / "new.txt").write_text("untracked\n")  # untracked files don't block a switch
    assert ws.worktree_for(cfg, "15033", "5001", "x")["branch"] == "5001-x"


def test_branch_mode_refuses_branch_held_by_worktree(setup):
    cfg = setup["config"]
    old = ws.worktree_for(cfg, "15033", "5001", "x")  # made while the repo used worktrees
    branch_mode(setup)
    with pytest.raises(ws.WorkSessionError, match="still checked out in the worktree"):
        ws.worktree_for(cfg, "15033", "5001", "x")
    git(setup["repo"], "worktree", "remove", old["path"])
    result = ws.worktree_for(cfg, "15033", "5001", "x")
    assert result["path"] == str(setup["repo"]) and result["branch"] == old["branch"] and result["created"] is False
