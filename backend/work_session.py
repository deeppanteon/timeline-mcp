"""
The task's git workspace and the terminal launch behind the dashboard's task
workflows (workflow.py), and a workflow step opens an interactive Claude Code
session there in a terminal window.

Each repo's "workspace" setting in timeline-desk.json says where a task runs:
- "branch" (the default): the main checkout itself, switched to the task's
  branch (prepare_branch). Its vendor/, node_modules/, web server and IDE
  setup all apply, and the session's work is what you see.
- "worktree": a separate git worktree per task (prepare_worktree), for running
  several tasks at once. Gitignored dependencies aren't there.
Also lists and removes worktrees made in "worktree" mode.

Repos come from timeline-desk.json (see timeline-desk.example.json), keyed by
the Timeline board/list id (a planner task's `code`). A worktree's timeline/
is a link to the main checkout's, so task docs outlive the worktree
(link_timeline).

Safety: Claude runs with its normal permission prompts; nothing here pushes,
and removal uses `git worktree remove` / `git branch -d` without force, so git
refuses to drop uncommitted or unmerged work.
"""
import json
import os
import re
import filecmp
import shutil
import subprocess
import sys
import threading
import unicodedata
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from release_check import ReleaseCheckError, Runner, _subprocess_runner

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # the project root (backend/ is one level down)
CONFIG_NAME = "timeline-desk.json"
# The timeline MCP server every run gets from the desk itself (--mcp-config), under
# its own name: a project can disable "timeline-mcp" (duell-admin does), and Claude
# Code applies that to the repo's worktrees, so runs can't rely on the user's setup.
MCP_NAME = "timeline-desk"
_lock = threading.Lock()
# Where a task runs (see the module docstring); the first is the default.
WORKSPACES = ("branch", "worktree")

Launcher = Callable[[List[str], Dict[str, str]], None]


class WorkSessionError(ReleaseCheckError):
    """A worktree or launch step failed; the message is safe to show."""


# ---- config ----------------------------------------------------------------
def config_path() -> str:
    return os.environ.get("TIMELINE_DESK_CONFIG") or os.path.join(HERE, CONFIG_NAME)


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """
    {"repos": {code: {"id" (API project id), "name", "path", "base", "workspace", "worktrees"}}, "claude", "terminal", "me", "git_authors"}
    with paths made absolute (relative ones resolve against the config file).
    A missing file means no repos, so no Start buttons.
    """
    path = path or config_path()
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raw = {}
    except (OSError, ValueError) as exc:
        raise WorkSessionError(f"Can't read {path}: {exc}")
    base_dir = os.path.dirname(os.path.abspath(path))

    def absolute(p: str) -> str:
        return os.path.normpath(os.path.join(base_dir, os.path.expanduser(p)))

    repos = {}
    for code, repo in (raw.get("repos") or {}).items():
        repo_path = absolute(repo["path"])
        workspace = repo.get("workspace") or "branch"
        if workspace not in WORKSPACES:
            raise WorkSessionError(f"Repo {code} in {path}: \"workspace\" must be one of {', '.join(WORKSPACES)}.")
        repos[str(code)] = {
            "id": repo.get("id"),
            "name": repo.get("name") or os.path.basename(repo_path),
            "path": repo_path,
            "base": repo.get("base") or "master",
            "workspace": workspace,
            "worktrees": absolute(repo["worktrees"]) if repo.get("worktrees") else repo_path + "-worktrees",
        }
    return {
        "repos": repos,
        "claude": os.path.expanduser(raw.get("claude") or shutil.which("claude") or "claude"),
        "terminal": raw.get("terminal") or "gnome-terminal",
        # Your name as Timeline shows it, so your own comments aren't taken for QA findings.
        "me": (raw.get("me") or "").strip() or None,
        # Git emails that count as you for release coverage; empty = each checkout's own `git config user.email`.
        "git_authors": [str(a).strip() for a in (raw.get("git_authors") or []) if str(a).strip()],
    }


def board_for_project(config: Dict[str, Any], project_id: Any) -> Optional[str]:
    """The board whose repo has this Timeline project id; None when no board or several boards have it."""
    if project_id in (None, ""):
        return None
    codes = [code for code, repo in config["repos"].items() if str(repo.get("id")) == str(project_id)]
    return codes[0] if len(codes) == 1 else None


def public_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """What the page needs: which boards have a repo."""
    return {
        "boards": {code: repo["name"] for code, repo in config["repos"].items()},
        "projects": [{"code": code, "id": repo["id"], "name": repo["name"]}
                     for code, repo in config["repos"].items() if repo.get("id")],
    }


# ---- naming ----------------------------------------------------------------
# Git keeps a branch as a file under .git/refs/heads, and a file name can't pass
# 255 bytes; only a title that long is shortened (at a word break).
MAX_BRANCH = 240


def slugify(title: str, limit: Optional[int] = None) -> str:
    """The whole task title as a slug; only cut (at a word break) when `limit` is given."""
    text = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode()
    text = re.sub(r"\[[^\]]*\]", " ", text)  # drop [PRI2] [BO] style tags
    text = re.sub(r"^\s*(bug|crm|sa|qa|prod)\s*:\s*", "", text, flags=re.I)
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if limit is not None and len(slug) > limit:
        cut = slug[:limit + 1]
        slug = cut.rsplit("-", 1)[0] if "-" in cut else slug[:limit]
    return slug or "task"


def branch_name(issue_id: str, title: str) -> str:
    """`<issue>-<full-title-slug>`, the team's branch format."""
    return f"{issue_id}-{slugify(title, MAX_BRANCH - len(str(issue_id)) - 1)}"


def worktree_dir(repo: Dict[str, Any], issue_id: str) -> str:
    """A task's worktree folder: the repo name with the issue ID after it, e.g. duell-admin-5145."""
    return os.path.join(repo["worktrees"], f"{repo['name']}-{issue_id}")


# ---- git helpers -----------------------------------------------------------
class _Git:
    def __init__(self, repo: Dict[str, Any], runner: Runner):
        self.repo = repo
        self.runner = runner

    def run(self, *args: str, cwd: Optional[str] = None, timeout: int = 60, ok_codes=(0,)) -> Tuple[int, str]:
        code, out, err = self.runner(("git",) + args, cwd or self.repo["path"], timeout)
        if code not in ok_codes:
            detail = (err or out).strip().splitlines()
            raise WorkSessionError(f"`git {' '.join(args[:2])}` failed: {detail[-1] if detail else f'exit {code}'}")
        return code, out

    def worktrees(self) -> List[Dict[str, str]]:
        """`git worktree list --porcelain` as [{path, head, branch}]."""
        _, out = self.run("worktree", "list", "--porcelain")
        items, current = [], {}
        for line in out.splitlines() + [""]:
            if not line:
                if current:
                    items.append(current)
                current = {}
            elif line.startswith("worktree "):
                current["path"] = line[len("worktree "):]
            elif line.startswith("HEAD "):
                current["head"] = line[5:]
            elif line.startswith("branch "):
                current["branch"] = line[len("branch refs/heads/"):]
        return items

    def branches(self, pattern: str, remote: bool = False) -> List[str]:
        args = ["branch", "--format=%(refname:short)", "--list"]
        if remote:
            args = ["branch", "-r", "--format=%(refname:short)", "--list"]
        _, out = self.run(*args, pattern)
        return [b.strip() for b in out.splitlines() if b.strip()]


def _inside(path: str, folder: str) -> bool:
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return os.path.commonpath([path, folder]) == folder and path != folder


def find_worktree(git: "_Git", issue_id: str) -> Optional[Dict[str, Any]]:
    """The worktree already on a `<issue>-…` branch or in the task's folder, if any."""
    folder = os.path.realpath(worktree_dir(git.repo, issue_id))
    for wt in git.worktrees():
        if (wt.get("branch", "").startswith(f"{issue_id}-") or wt.get("branch") == issue_id
                or os.path.realpath(wt["path"]) == folder):
            return {"path": wt["path"], "branch": wt["branch"], "created": False}
    return None


def _merge_into(src: str, dst: str, prefix: str = "") -> List[str]:
    """
    Moves into `dst` whatever it lacks from `src`, folder by folder, drops
    files that are identical in both, and removes `src` once it's empty.
    Returns the paths (relative to src) that differ and were left in place.
    """
    clashes = []
    for name in sorted(os.listdir(src)):
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if not os.path.lexists(d):
            shutil.move(s, d)
        elif os.path.isdir(s) and os.path.isdir(d) and not os.path.islink(s) and not os.path.islink(d):
            clashes += _merge_into(s, d, f"{prefix}{name}/")
        elif os.path.isfile(s) and os.path.isfile(d) and not os.path.islink(s) and filecmp.cmp(s, d, shallow=False):
            os.remove(s)
        else:
            clashes.append(prefix + name)
    if not clashes:
        os.rmdir(src)
    return clashes


def link_timeline(git: "_Git", worktree: str) -> Optional[str]:
    """
    Makes <worktree>/timeline a symlink to the main checkout's timeline/, so
    the task docs a session writes (task.md, analysis.md, progress.md, …) are
    the ones the main checkout and later sessions see, and survive removing
    the worktree (`git worktree remove` unlinks the symlink, not its target).
    A real timeline/ folder from an older session is merged in first. Adds
    `/timeline` to the repo's shared info/exclude when git wouldn't ignore the
    link (a `timeline/` gitignore line matches folders only).
    Best effort: returns a note saying what it left alone, else None.
    """
    shared = os.path.join(git.repo["path"], "timeline")
    link = os.path.join(worktree, "timeline")
    if os.path.realpath(worktree) == os.path.realpath(git.repo["path"]) or not os.path.isdir(worktree):
        return None
    try:
        if os.path.islink(link):
            return None if os.path.realpath(link) == os.path.realpath(shared) else f"Left {link} alone: it links somewhere else."
        _, tracked = git.run("ls-files", "--", "timeline", cwd=worktree)
        if tracked.strip():
            return "timeline/ is tracked in git here, so task docs stay in the worktree."
        os.makedirs(shared, exist_ok=True)
        if os.path.isdir(link):
            clashes = _merge_into(link, shared)
            if clashes:
                return (f"Kept {link}: {', '.join(clashes[:3])}{' …' if len(clashes) > 3 else ''} "
                        "differ from the main checkout's copy. Merge them by hand, then start again.")
        elif os.path.lexists(link):
            return f"Left {link} alone: it's a file."
        code, _ = git.run("check-ignore", "-q", "timeline", cwd=worktree, ok_codes=(0, 1))
        if code == 1:
            _, common = git.run("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=worktree)
            exclude = os.path.join(common.strip(), "info", "exclude")
            os.makedirs(os.path.dirname(exclude), exist_ok=True)
            with open(exclude, "a", encoding="utf-8") as fh:
                fh.write("\n# task docs: worktrees link timeline/ to the main checkout (timeline desk)\n/timeline\n")
        os.symlink(shared, link)
    except (OSError, WorkSessionError) as exc:
        return f"Couldn't link timeline/ to the main checkout: {exc}"
    return None


def _with_timeline(git: "_Git", result: Dict[str, Any]) -> Dict[str, Any]:
    note = link_timeline(git, result["path"])
    return {**result, "note": note} if note else result


def prepare_worktree(repo: Dict[str, Any], issue_id: str, title: str, runner: Runner = _subprocess_runner) -> Dict[str, Any]:
    """
    Returns {path, branch, created} for the task, reusing what exists:
    a worktree already on a `<issue>-…` branch, else a local or remote
    `<issue>-…` branch, else a new `<issue>-<full title>` branch from
    origin/<base>. A new worktree goes to `<repo name>-<issue>` in the repo's
    worktrees folder (worktree_dir), whatever the branch is called. Either way the
    worktree's timeline/ is linked to the main checkout's (link_timeline);
    `note` is added when that link couldn't be made.
    """
    git = _Git(repo, runner)
    if not os.path.isdir(repo["path"]):
        raise WorkSessionError(f"No checkout at {repo['path']}. Fix the path in {CONFIG_NAME}.")
    existing = find_worktree(git, issue_id)
    if existing:
        return _with_timeline(git, existing)
    branch, start = _task_branch(git, issue_id, title)
    path = worktree_dir(repo, issue_id)
    if os.path.exists(path):
        raise WorkSessionError(f"{path} already exists but isn't a worktree of {repo['name']}. Move it away first.")
    os.makedirs(repo["worktrees"], exist_ok=True)
    if start is None:
        git.run("worktree", "add", path, branch)
    elif start.startswith("origin/") and start != f"origin/{repo['base']}":
        git.run("worktree", "add", "--track", "-b", branch, path, start)
    else:
        git.run("worktree", "add", "--no-track", "-b", branch, path, start)
    return _with_timeline(git, {"path": path, "branch": branch, "created": True})


def _task_branch(git: "_Git", issue_id: str, title: str) -> Tuple[str, Optional[str]]:
    """
    (branch, start) for the task: a local `<issue>-…` branch (start None), else
    one on origin (start `origin/<it>`), else a new `<issue>-<title>` branch
    (start `origin/<base>`). Fetches the base first.
    """
    prefix = f"{issue_id}-"
    git.run("fetch", "--quiet", "origin", git.repo["base"], timeout=120)
    local = git.branches(f"{prefix}*")
    remote = [b for b in git.branches(f"origin/{prefix}*", remote=True) if b.startswith("origin/")]
    if local:
        branch, start = local[0], None
    elif remote:
        branch, start = remote[0][len("origin/"):], remote[0]
    else:
        branch, start = branch_name(issue_id, title), f"origin/{git.repo['base']}"
    git.run("check-ref-format", "--branch", branch)
    return branch, start


def current_branch(repo: Dict[str, Any], runner: Runner = _subprocess_runner) -> Optional[str]:
    """The branch the main checkout is on; None when HEAD is detached."""
    code, out = _Git(repo, runner).run("symbolic-ref", "--quiet", "--short", "HEAD", ok_codes=(0, 1))
    return out.strip() if code == 0 and out.strip() else None


def prepare_branch(repo: Dict[str, Any], issue_id: str, title: str, runner: Runner = _subprocess_runner) -> Dict[str, Any]:
    """
    Returns {path, branch, created} with the main checkout itself on the task's
    branch (see _task_branch): already on a `<issue>-…` branch, it stays as it
    is; otherwise it checks the branch out. Refuses, without touching anything,
    when the checkout has uncommitted changes to tracked files (they belong to
    whatever it is on now), or when a worktree still holds the task's branch
    (git can't check one branch out twice; remove that worktree first).
    """
    git = _Git(repo, runner)
    if not os.path.isdir(repo["path"]):
        raise WorkSessionError(f"No checkout at {repo['path']}. Fix the path in {CONFIG_NAME}.")
    main = os.path.realpath(repo["path"])
    current = current_branch(repo, runner)
    if current and (current.startswith(f"{issue_id}-") or current == issue_id):
        return {"path": repo["path"], "branch": current, "created": False}
    held = next((wt for wt in git.worktrees() if os.path.realpath(wt["path"]) != main
                 and (wt.get("branch", "").startswith(f"{issue_id}-") or wt.get("branch") == issue_id)), None)
    if held:
        raise WorkSessionError(
            f"Branch {held['branch']} is still checked out in the worktree {held['path']}. Commit there, then remove "
            "that worktree on the Work tab (the branch stays) and start again: the task now runs in the main checkout.")
    _, dirty = git.run("status", "--porcelain", "--untracked-files=no")
    if dirty.strip():
        raise WorkSessionError(
            f"The main checkout {repo['path']} has uncommitted changes on {current or 'a detached HEAD'}. "
            "Commit or stash them, then start again.")
    branch, start = _task_branch(git, issue_id, title)
    if start is None:
        git.run("checkout", "--quiet", branch)
    elif start != f"origin/{repo['base']}":
        git.run("checkout", "--quiet", "--track", "-b", branch, start)
    else:
        git.run("checkout", "--quiet", "--no-track", "-b", branch, start)
    return {"path": repo["path"], "branch": branch, "created": start is not None}


def _popen_launcher(args: List[str], env: Dict[str, str]) -> None:
    try:
        subprocess.Popen(args, env=env, start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        raise WorkSessionError(f"`{args[0]}` is not installed. Set \"terminal\" in {CONFIG_NAME}.")


def terminal_command(config: Dict[str, Any], path: str, title: str, claude_args: Sequence[str],
                     exit_file: Optional[str] = None) -> List[str]:
    """
    A terminal running `claude <claude_args…>` in `path`, left open as a shell
    when Claude exits. With `exit_file`, Claude's exit code is written there
    when it exits, so the dashboard knows the session ended.
    """
    script = ('cd "$1" || { echo "No folder $1"; exec bash; }; exit_file="$2"; shift 2; "$@"; code=$?; '
              'if [ -n "$exit_file" ]; then mkdir -p "$(dirname "$exit_file")" && echo "$code" > "$exit_file"; fi; '
              'echo; echo "Claude exited. This shell is in the task'"'"'s checkout."; exec bash')
    return [config["terminal"], f"--title={title}", f"--working-directory={path}", "--",
            "bash", "-c", script, "bash", path, exit_file or "", config["claude"], *claude_args]


def session_alive(session_id: str) -> Optional[bool]:
    """
    Whether any process here still has the Claude session id on its command
    line (the terminal's bash wrapper and claude itself do, for `--session-id`
    and `--resume`). None where /proc isn't available, so callers can't tell.
    """
    if not os.path.isdir("/proc") or not session_id:
        return None
    needle = session_id.encode()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                if needle in fh.read():
                    return True
        except OSError:
            continue
    return False


def desk_python() -> str:
    """This repo's virtualenv python (the dashboard's own when it runs from there)."""
    venv = os.path.join(HERE, ".venv", "bin", "python")
    return venv if os.path.exists(venv) else sys.executable


def mcp_server(repo: Dict[str, Any]) -> Dict[str, Any]:
    """The timeline MCP server for a run in `repo`: task.md goes to the main checkout's timeline/."""
    return {"type": "stdio", "command": desk_python(), "args": [os.path.join(HERE, "backend", "server.py")],
            "env": {"PANTEON_TIMELINE_DIR": repo["path"]}}


def mcp_config(repo: Dict[str, Any]) -> str:
    """mcp_server as the JSON string `claude --mcp-config` takes."""
    return json.dumps({"mcpServers": {MCP_NAME: mcp_server(repo)}})


def launch_env(config: Dict[str, Any]) -> Dict[str, str]:
    """The environment for the terminal: an nvm-installed claude needs its node next to it."""
    env = dict(os.environ)
    claude_dir = os.path.dirname(config["claude"])
    if claude_dir:
        env["PATH"] = claude_dir + os.pathsep + env.get("PATH", "")
    return env


def worktree_for(config: Dict[str, Any], code: str, issue_id: str, title: str,
                 runner: Runner = _subprocess_runner) -> Dict[str, Any]:
    """
    Where the task runs on board `code`'s repo, under the lock: the main checkout
    on its branch (prepare_branch) or its own worktree (prepare_worktree), per
    the repo's "workspace". The result's `workspace` says which.
    """
    if not re.fullmatch(r"\d{1,7}", str(issue_id)):
        raise ValueError("Issue ID must be a number.")
    repo = config["repos"].get(str(code))
    if not repo:
        raise ValueError(f"Board {code} has no repo. Add it to {CONFIG_NAME}.")
    with _lock:
        prepare = prepare_worktree if repo.get("workspace") == "worktree" else prepare_branch
        return {**prepare(repo, str(issue_id), title, runner), "repo": repo["name"], "workspace": repo.get("workspace") or "branch"}
