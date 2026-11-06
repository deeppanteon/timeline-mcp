"""
Git worktrees and the terminal launch behind the dashboard's task workflows
(workflow.py): each task gets its own git worktree of the right repo, and a
workflow step opens an interactive Claude Code session there in a terminal
window. Also lists and removes those worktrees.

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

Launcher = Callable[[List[str], Dict[str, str]], None]


class WorkSessionError(ReleaseCheckError):
    """A worktree or launch step failed; the message is safe to show."""


# ---- config ----------------------------------------------------------------
def config_path() -> str:
    return os.environ.get("TIMELINE_DESK_CONFIG") or os.path.join(HERE, CONFIG_NAME)


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """
    {"repos": {code: {"id" (API project id), "name", "path", "base", "worktrees"}}, "claude", "terminal", "me", "git_authors"}
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
        repos[str(code)] = {
            "id": repo.get("id"),
            "name": repo.get("name") or os.path.basename(repo_path),
            "path": repo_path,
            "base": repo.get("base") or "master",
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
    prefix = f"{issue_id}-"

    git.run("fetch", "--quiet", "origin", repo["base"], timeout=120)
    local = git.branches(f"{prefix}*")
    remote = [b for b in git.branches(f"origin/{prefix}*", remote=True) if b.startswith("origin/")]
    if local:
        branch, start = local[0], None
    elif remote:
        branch, start = remote[0][len("origin/"):], remote[0]
    else:
        branch, start = branch_name(issue_id, title), f"origin/{repo['base']}"
    git.run("check-ref-format", "--branch", branch)
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
              'echo; echo "Claude exited. This shell is in the task worktree."; exec bash')
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
    """The task's worktree on board `code`'s repo (see prepare_worktree), under the lock."""
    if not re.fullmatch(r"\d{1,7}", str(issue_id)):
        raise ValueError("Issue ID must be a number.")
    repo = config["repos"].get(str(code))
    if not repo:
        raise ValueError(f"Board {code} has no repo. Add it to {CONFIG_NAME}.")
    with _lock:
        return {**prepare_worktree(repo, str(issue_id), title, runner), "repo": repo["name"]}


# ---- listing and removal ---------------------------------------------------
def _dir_size(path: str) -> int:
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def list_worktrees(config: Dict[str, Any], runner: Runner = _subprocess_runner) -> List[Dict[str, Any]]:
    """Task worktrees (those inside each repo's worktrees folder), with cleanup facts."""
    items = []
    for code, repo in config["repos"].items():
        if not os.path.isdir(repo["path"]):
            continue
        git = _Git(repo, runner)
        for wt in git.worktrees():
            if not _inside(wt["path"], repo["worktrees"]):
                continue
            path, branch = wt["path"], wt.get("branch")
            exists = os.path.isdir(path)
            dirty = unpushed = None
            merged = False
            if exists:
                _, status = git.run("status", "--porcelain", cwd=path)
                dirty = len(status.splitlines())
                _, out = git.run("rev-list", "--count", "HEAD", "--not", "--remotes", cwd=path)
                unpushed = int(out.strip() or 0)
                code_rc, _ = git.run("merge-base", "--is-ancestor", "HEAD", f"origin/{repo['base']}", cwd=path, ok_codes=(0, 1))
                merged = code_rc == 0
            m = re.match(r"(\d+)-", branch or "") or re.fullmatch(rf"{re.escape(repo['name'])}-(\d+)", os.path.basename(path))
            items.append({
                "repo": repo["name"], "code": code, "path": path, "branch": branch,
                "issue_id": m.group(1) if m else None,
                "exists": exists, "size": _dir_size(path) if exists else 0,
                "dirty": dirty, "unpushed": unpushed, "merged": merged,
            })
    return items


def remove_worktree(
    config: Dict[str, Any],
    path: str,
    delete_branch: bool = False,
    runner: Runner = _subprocess_runner,
) -> Dict[str, Any]:
    """
    `git worktree remove` (refuses with uncommitted changes) and optionally
    `git branch -d` (refuses unmerged commits). Never forces.
    """
    for repo in config["repos"].values():
        if not _inside(path, repo["worktrees"]):
            continue
        git = _Git(repo, runner)
        match = next((wt for wt in git.worktrees() if os.path.realpath(wt["path"]) == os.path.realpath(path)), None)
        if not match:
            break
        with _lock:
            if os.path.isdir(path):
                git.run("worktree", "remove", path)
            else:
                git.run("worktree", "prune")
            result = {"removed": path, "branch": match.get("branch"), "branch_deleted": False}
            if delete_branch and match.get("branch"):
                code, out, err = runner(("git", "branch", "-d", match["branch"]), repo["path"], 30)
                result["branch_deleted"] = code == 0
                if code != 0:
                    result["branch_note"] = "Kept the branch: it has commits not merged anywhere yet."
        return result
    raise ValueError("Not a task worktree from this dashboard.")
