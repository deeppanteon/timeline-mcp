# Task workflow — roadmap and history

This file records why the dashboard has task workflows, what phase 1 built, what is
known to be wrong or unverified in it, and the detailed plan for phases 2–5. It is
meant to be read at the start of a new chat before continuing this work.

To continue in a new chat, say for example:

> Read `docs/workflow-roadmap.md` and `CLAUDE.md`. Continue with phase 1 corrections (or: implement phase 2).

---

## 1. Why this exists (the requirement)

The dashboard first had a **"Start in Claude"** button on each task. It guessed an
action from the stage text in the browser, the server decided again on click (often
differently), opened a terminal with a prompt, and then forgot about the task. The
user called this "prompting from the UI" and asked for something else:

- **A whole flow per task**, where each Timeline state uses its own kind of workflow
  (start → analysis and task docs with the timeline skills; QA findings → fixes in the
  QA-fix form of `timeline-progress`; and so on).
- **Status that is kept**, and **everything kept intact** (every run, decision and
  file stays as history).
- **Everything on screen**, nothing hidden: why a step was chosen, what it will do,
  the prompt, the result, the conversation.
- **Clear confirmation**: as many approval points as possible.

Decisions the user made (do not re-ask):

| Question | Decision |
|---|---|
| Where do code-changing steps run? | **Option 1: in a terminal**, interactively, with Claude Code's normal permission prompts. The dashboard tracks the session (session id, result file, transcript). |
| How many approval points? | **As many as possible** (list in §3.6). |
| Who sees the status? | **Only the user.** Local SQLite database, as for the rest of the desk. No server, no sharing. |
| QA findings skill? | **No new skill.** QA findings are `timeline-progress` work in the `qa-fix` phase ("same working, in correction form"). |
| Read-only steps on the page? | Tried in phase 4, **removed 2026-10-28**: every step runs in a terminal (§10.9). |

Ground rules that every phase must keep:

- Nothing that edits code, commits or writes to Timeline ever starts without a click.
  Automation **suggests**; it never runs a step by itself.
- The prompt holds only the issue ID and fixed text — never task text (task text is
  data from other people; the session fetches it itself and treats it as data).
- Sessions never push, open PRs or merge. No permission-bypass flags.
- All dashboard rules in `CLAUDE.md` (127.0.0.1 only, trusted hosts, `X-Timeline-Desk`
  header on every POST, DOM text via `h()`/`textContent`, never `innerHTML`).
- The decision rules live on the server (`workflow.actions`). The page shows exactly
  the actions the server lists; every POST is checked against the same list.

---

## 2. Phase overview

| Phase | Name | Status |
|---|---|---|
| 0 | Groundwork: lifecycle skills, shared `timeline/` folder, intake gate | Done |
| 1 | Flow, status and history; task workflow page; runs in terminal | Built, **needs corrections** (§3.9) |
| 2 | Follow Timeline: auto-watch, signals, suggestions, "Needs you" board | **Built** 2026-10-28 (§4, as built §10) |
| 3 | QA findings checklist, per-finding fixes, release integration, prod fixes | **Built** 2026-10-28 (§5, as built §10) |
| 4 | Read-only steps run inside the dashboard with live output | **Built** 2026-10-28 (§6, as built §10) |
| 5 | (Optional) In-dashboard approvals for every tool call (Agent SDK) | **Built, then removed 2026-10-28 (§10.9)**; SDK installed and smoke-tested 2026-10-28 (§7, as built §10) |

Phases 2–5 were built in one go on 2026-10-28 ("implement all phases, fix and
optimise later"). Their designs below are the plan; §10 says what was actually
built, where it differs, and what is still unverified.

Each phase is usable on its own. Do them in order: 2 needs 1's tables, 3 needs 2's
event signals, 4 needs 1's run model.

---

## Phase 0 — groundwork (done)

1. **One document format.** The dashboard uses the timeline lifecycle skills
   (`timeline-intake` → `task.md`, `analysis.md`, `progress.md`; `timeline-progress`;
   `timeline-release`), not `task-spec`/`spec.md`. Skills are referenced **by path**
   under this repo's `skills/` because duell-admin has an older installed duell plugin
   (mega-skills 0.2.0) with the same skill names and the old behaviour.
2. **Shared task docs.** A worktree's `timeline/` is a symlink to the main checkout's
   `timeline/` (`work_session.link_timeline`). An old real folder is merged in
   (identical files dropped, differing ones left with a note). `/timeline` is added to
   the repo's shared `.git/info/exclude` when needed, because a `timeline/` gitignore
   line matches folders only, not a symlink. `git worktree remove` deletes the link,
   never its target (verified).
3. **Intake gate.** `timeline-intake` refuses only when `analysis.md` exists (a folder
   with just a fetched `task.md` or an old `spec.md` is fine). `timeline-progress`,
   `timeline-release`, `timeline-estimate` and `upgrade-implementation` check
   `analysis.md` too. `timeline-progress` falls back to Timeline's stages for the
   phase when `progress.md` has no release rows (tasks released through normal PRs).

Note: the installed duell plugin inside duell-admin still has the old intake check.
Typing `/timeline-intake` by hand there uses that old copy until the plugin is updated.

---

## 3. Phase 1 — flow, status, history (built; needs corrections)

### 3.1 Files

| File | Role |
|---|---|
| `workflow.py` | Flow definition, storage, prompts, run status, actions, start/run/resume/decide, docs and transcript reading |
| `work_session.py` | Git worktrees (`worktree_for`, `prepare_worktree`, `link_timeline`), `terminal_command`, `launch_env`, Work tab listing/removal |
| `panteon_client._open_planner_db` | Schema version 3 adds the workflow tables |
| `dashboard.py` | Thin endpoints (§3.5) |
| `web/app.js` | Badges (`workflowLink`, `getWorkflows`) and the panel (`loadWorkflow`, `renderWorkflow`, `stepCard`, `runCard`, `actionBar`, `startForm`, `docsList`, `conversation`) |
| `web/style.css` | `.wf*`, `.pill`, `.start.wf-*` |
| `tests/test_workflow.py` | Flow, gates, resume, bad results, docs, transcript, endpoints (real git in tmp, recording launcher) |

The old launcher was removed: `start_session`, `choose_action`, `build_prompt` in
`work_session.py`, `POST /api/work/start`, and `ACTIONS`/`actionFor`/`startButton` in
`app.js`.

### 3.2 The flow (`workflow.FLOW`)

```
intake → estimate (optional) → develop (repeats) → review → release_qa
  → qa (decision) → qa_fix (repeats) → back to qa (qa_round + 1) … → release_prod → close (decision)
```

| Key | Title | Kind | Skill(s) | Writes | You approve |
|---|---|---|---|---|---|
| intake | Intake | run | timeline-intake | task.md, analysis.md, progress.md | analysis.md is right, questions answered |
| estimate | Estimate | run, optional | timeline-estimate | analysis.md Time Analysis | the hours |
| develop | Develop | run, repeat, needs analysis, checks blocked | timeline-progress (dev) + specialist | one step's code, one progress row, no commit unless you confirm | this step's change |
| review | Self-review | run, needs analysis | upgrade-verification | review.md | review right, findings handled |
| release_qa | Release to QA | run, needs analysis | timeline-release qa | release-note.md section, qa-plan.md | release note and QA plan |
| qa | Waiting on QA | decision | — | — | QA found issues / QA approved |
| qa_fix | Fix QA findings | run, repeat, needs analysis, checks blocked | timeline-progress (qa-fix) + specialist | one finding's fix, one row, no commit unless you confirm | this finding's fix |
| release_prod | Release to prod | run, needs analysis | timeline-release prod | release-note.md section | the prod release note |
| close | Close | decision | — | — | the task is done |

`NEXT` gives the step after approve/finish/skip. `back_to_develop` is offered at
review and release_qa.

### 3.3 Storage (SQLite `database/timeline_task.db`, schema v3)

```sql
workflows(issue_number PK, code, title, repo, branch, worktree, step, state /*active|done|stopped*/,
          qa_round, started_at, updated_at)
workflow_runs(id PK, issue_number, step, session_id, cwd, prompt, started_at, ended_at, exit_code,
              result /*cleaned JSON*/, result_at, decision /*approved|rejected*/, decided_at, note)
workflow_events(id PK, issue_number, at, step, run_id, kind, note)
```

Event kinds: start, run, resume, approve, reject, abandon, skip, finish,
back_to_develop, qa_issues, qa_passed, close, stop, reopen.

### 3.4 Run lifecycle

1. **Run** (`run_step`): checks `_allowed("run")`; for develop/qa_fix also refuses when
   Timeline (fresh `get_task_statuses`) shows a blocked stage; makes/reuses the
   worktree; inserts the run; builds the prompt with the real run id; opens
   `terminal claude --session-id <uuid> <prompt>` via the launcher; the terminal script
   writes Claude's exit code to `timeline/<id>/runs/<run>.exit` when Claude exits.
2. **Report contract** (end of every prompt): whenever Claude stops it writes
   `timeline/<id>/runs/<run>.result.json`:
   `{"status": "done"|"needs_input"|"blocked"|"failed", "summary", "files", "commits", "questions", "remaining"?}`.
3. **Status** (`refresh_run` on every read copies both files into the row;
   `run_status`): running → review (done) / needs_input / blocked / failed /
   bad_result (unreadable) / no_result (exited without a result) → approved / rejected.
4. **Resume** (`resume_run`): deletes both files (old result goes into the event
   note), runs `claude --resume <session> "<carry on…>"`.
5. **Conversation** (`read_transcript`): reads Claude Code's own log
   `<CLAUDE_CONFIG_DIR or ~/.claude>/projects/*/<session>.jsonl`; shows text and tool
   calls, skips thinking/tool output/meta.

### 3.5 Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/api/workflows` | badges: every workflow with status code/label/short |
| GET | `/api/workflow/{issue}?code=` | the whole view; one taskbyissue call only when no workflow exists (start suggestion) |
| POST | `/api/workflow/{issue}/start` `{code, step}` | guarded; status lookup gives the title |
| POST | `/api/workflow/{issue}/run` | guarded; fresh stages for the blocked check |
| POST | `/api/workflow/{issue}/resume` `{run_id}` | guarded |
| POST | `/api/workflow/{issue}/decide` `{kind, run_id?, note?}` | guarded |
| GET | `/api/workflow/{issue}/doc/{name}` | `timeline/<id>/*.md|sql|txt` from the main checkout |
| GET | `/api/workflow/{issue}/runs/{run_id}/transcript` | session log |

### 3.6 Approval points (user asked for as many as possible)

| # | Approval | Where it happens in phase 1 |
|---|---|---|
| 1 | Start workflow (repo, branch, worktree, start step) | page: Start workflow + confirm |
| 2 | Run each step | page: Run in terminal (card shows skills, files, prompt) |
| 3 | Intake result | page: Approve result |
| 4 | Estimate | page: Approve result (or Skip with reason) |
| 5 | Each development step's plan | terminal: prompt tells Claude to state the step and wait for go-ahead |
| 6 | Each file edit and command | terminal: Claude Code permission prompts |
| 7 | Each development step's result | page: Approve result (one step per run) |
| 8 | Self-review result | page: Approve result / Back to Develop |
| 9 | Release to QA | page: Approve result |
| 10 | Each QA finding (bug / question / not a bug) | terminal only in phase 1 (prompt asks); page checklist in phase 3 |
| 11 | Each QA fix | page: Approve result (one finding per run) |
| 12 | Release to prod | page: Approve result |
| 13 | Close workflow | page: Close |

Notes are required for skip, reject, abandon, stop, back_to_develop.

### 3.7 Page

- Badges on board cards, task rows and the Work tab (`+ Workflow` when none). They only
  link to the task page; nothing on the board launches Claude.
- Task page panel: status pill and facts, stepper, current step card (skills, reads,
  writes, won't, runs in, you approve, exact prompt), the open run (result, prompt and
  session, conversation), action buttons from the server, runs history, decisions log,
  task docs (click to read). Polls every 10 s while a run is running.

### 3.8 How phase 1 was verified

- `pytest`: 252 passed (17 in `tests/test_workflow.py`).
- A throwaway dashboard (not committed) on port 8766 with a temp git repo, `terminal`
  and `claude` set to `/bin/true` and no scheduler: Start → Run → (hand-written
  result file) → Approve → Skip, clicked through in the browser. It found and fixed
  one bug (`factList` printed `[object HTMLElement]` — `h()` flattens one level only,
  use `flatMap`). Recipe for later phases:

  ```python
  # scratchpad/serve.py — temp repo, terminal=/bin/true, no scheduler
  # build seed → origin.git → repo with git; write desk.json with
  #   {"repos": {"15033": {"id": 16, "name": "sandbox-repo", "path": <repo>, "base": "master"}},
  #    "claude": "/bin/true", "terminal": "/bin/true"}
  # os.environ["TIMELINE_DESK_CONFIG"] = desk.json
  # uvicorn.run(dashboard.create_app(base_dir=<tmp base>), host="127.0.0.1", port=8766)
  ```
  Add a temporary `.claude/launch.json` entry to start it with the preview tool, and
  remove it afterwards. Clicks that ask `confirm()`/`prompt()` need those stubbed in
  the test tab.
- **Not verified:** a real Claude session (terminal never opened), whether Claude
  reliably writes the result file, the transcript view against a real session log.

### 3.9 Phase 1 — known issues and corrections to make

The user said phase 1 is not fully correct and will continue correcting it. Review
these first, then whatever the user reports.

**Must check with a real run**
- [ ] Run one real Intake end to end on a real task. Check: terminal opens, session id
      matches `~/.claude/projects/*/<id>.jsonl`, Claude writes the result file at the
      right path (through the symlink), the page moves to "Waiting for your review".
- [ ] Does Claude keep the report contract in long sessions / after compaction? If not,
      move the contract into `--append-system-prompt` (it survives compaction better)
      and keep the short reminder in the prompt.
- [ ] `claude --resume <id> "<message>"` really continues that session in the worktree.

**Design gaps**
- [x] The old **Blocked** action is back as the side run "Explain the blocker"
      (phase 2), offered when Timeline's latest stage says blocked.
- [ ] Prompt preview shows `<run>`; the real run id only exists after insert. Fine,
      but label it clearly or reserve the id first.
- [ ] Start creates a worktree even for Intake (read-only). Consider creating it at the
      first code step instead.
- [x] `finish` now counts only runs approved since the step was (re)entered
      (`workflows.step_run_floor`, phase 2).
- [ ] Removing a worktree on the Work tab while its workflow is active is allowed;
      `run_step` silently recreates it. Warn or block on the Work tab.
- [ ] Workflow `title` comes from `get_task_statuses` at start and never refreshes.
- [ ] Starting again after `done` resets the row (history stays). Phase 3 replaces this
      with a proper prod-fix path.
- [ ] Approve is allowed only for result status `done`. A `needs_input` that you want
      to accept anyway must be resumed or rejected — confirm that's what the user wants.
- [ ] `read_transcript` depends on Claude Code's internal JSONL format; keep the parser
      tolerant and show "not found" rather than failing.
- [ ] `workflow_view` writes to the DB on GET (it stores newly found result/exit
      files). Intentional caching, but note it if GET purity matters.
- [ ] Page confirmations use `window.confirm`/`window.prompt`. Replace with inline
      confirm/notes UI if the user prefers (also easier to test).
- [ ] Docs list shows only top-level `timeline/<id>/` files; `runs/` (result files) are
      not listed. Decide whether to show them.

**Documentation**
- [ ] README "Task workflows" and CLAUDE.md `workflow.py` paragraph must follow any
      correction.

---

## 4. Phase 2 — follow Timeline: signals, suggestions, "Needs you" board

### Goal
The desk notices what happens in Timeline for tasks with a workflow and tells the user
what needs them — with the reason — without running anything.

### What already exists to build on
- `task_events.py`: a watchlist; `sync_watchlist` copies every Timeline event of a
  watched task into `task_events` (kinds `status`, `comment`, `commit`, `field`).
  Status events carry old → new stage text, e.g. `Development in progress → Testing pending`,
  `Testing pending → QA in progress`. Comments carry `employee`, time and body.
- The scheduler in `dashboard.main` (`take_snapshot`) already runs `sync_watchlist`
  every heartbeat (3 min in active hours) at one request per watched task.
- Planner snapshots store each task's stages (`with_stages`), refreshed hourly.

### Design
1. **Auto-watch.** `start_workflow` calls `task_events.watch_task(issue, code, title)`
   so every workflow task is synced by the existing heartbeat. No new polling.
   (`find_my_open_task` restricts manual watching to the user's own open tasks; decide
   whether workflow tasks bypass that or must also be own tasks.)
2. **Signals** — a pure function `workflow_signals(wf, events, me)` computed on read
   (not stored), from `task_events` rows newer than the workflow's last acknowledged
   event id:

   | Signal | Rule | Suggests |
   |---|---|---|
   | qa_started | status event whose new value starts with `Testing` or `QA` while step is release_qa (approved) or qa | go to / stay in Waiting on QA |
   | qa_comments | comments by someone other than `me` after the qa step was entered | "QA found issues" (list the comments: author, time, link) |
   | qa_done | status new value `QA done` / `Going live …` / `Live …` | "QA approved" / Release to prod / Close |
   | blocked | status new value ends with `blocked` | warning + the Blocked side action (§3.9) |
   | moved_back | status goes back to Development while step ≥ qa | "Back to Develop" |
   | mismatch | Timeline stage is ahead of / behind the workflow step (e.g. Testing while step is develop) | warning only |
   | deadline | `field` Deadline changed | info |
   | new_commit | `commit` events (someone else pushed TL #id) | info |

3. **Acknowledge.** New column `workflows.seen_event_id` (schema v4). A signal is
   "new" when its event id is greater. Action `ack` (no note) sets it; acting on a
   suggestion (qa_issues etc.) also acks. Record `ack` in `workflow_events`.
4. **Who is "me".** Comments by the user must not count as QA findings. Add `"me":
   "<Timeline display name>"` to `timeline-desk.json` (or resolve from the user id if
   the API offers it). Without it, show all comments and say so.
5. **Status.** `workflow_status` gains `code: "attention"` (or reuses `needs_you`)
   when there are new signals, with the newest signal as the label.
6. **"Needs you" board.** New tab **Workflows** (`#/workflows`): every workflow grouped
   by Needs you / Running / Waiting on QA / Stopped / Done, each row with task, step,
   status, newest signal and its age, link to the task page. Endpoint
   `GET /api/workflows` gains `signals` per workflow.
7. **On the task page.** A "What changed in Timeline" box above the step card: each new
   signal with its event (author, time, old → new), the suggested decision as a button
   (still the same server actions), and "Mark as seen".
8. **Optional desktop notification** when a new signal arrives — only if the user wants
   it; the page can use the Notification API while open. Nothing leaves the machine.

### Tasks
- [ ] Schema v4: `workflows.seen_event_id INTEGER`, maybe `workflows.me` not needed.
- [ ] `start_workflow` → `watch_task`; `decide(close|stop)` → leave watching on or
      `unwatch_task` (decide).
- [ ] `workflow_signals` + tests with fake `task_events` rows (use real stage texts above).
- [ ] `actions` adds `ack` when there are unseen signals; Blocked side action.
- [ ] `/api/workflows` and `/api/workflow/{issue}` include signals.
- [ ] Workflows tab in `index.html` + `app.js` (`showWorkflows`), styles.
- [ ] CLAUDE.md + README.

### Acceptance
- A status change or QA comment in Timeline shows on the task page and the Workflows
  tab within one heartbeat, with the reason, and nothing runs by itself.
- No extra Timeline requests beyond the existing watchlist sync.

---

## 5. Phase 3 — QA findings checklist, per-finding fixes, releases, prod fixes

### Goal
QA findings become a visible checklist the user classifies one by one; each fix run is
tied to one finding; release steps connect to the Release view; a live bug reopens the
flow properly.

### Design
1. **Comment ids in task docs.** `parse_conversation_comments` drops Timeline's event
   id. Add `id` to each comment and show it in the rendered heading in
   `render_task_markdown` (e.g. `… · id 123456`), so prompts and findings can refer to a
   comment by id instead of copying its text. Update the markdown contract tests.
2. **`qa_findings` table** (schema v5):
   `qa_findings(id PK, issue_number, event_id /*Timeline comment id*/, author, posted_at,
   qa_round, status /*new|bug|question|not_bug|fixed|wont_fix*/, run_id, note, decided_at)`.
3. **Candidates** come from `task_events` comments (phase 2) posted after the qa step
   was entered, by someone other than `me` — deterministic, no AI. One comment may hold
   several findings: allow "split" (several findings for one event id, with a short
   user-written label).
4. **Classify (approval #10 on the page).** Each candidate: Bug / Question / Not a bug
   (note required for the last two). Only `bug` findings can be fixed.
5. **Per-finding run.** `qa_fix` Run asks which finding (default: oldest open bug). The
   prompt names the finding by **event id** only: "fix the QA finding in comment id N of
   task #id" — Claude reads it from `get_task_conversation`. The result contract gains
   `"finding": <id>` and the outcome. Approving the run marks the finding `fixed`.
6. **Finish QA fixes** only when no `bug` finding is open (or with a note).
7. **Questions.** For `question` findings, a read-only run drafts a reply for the task
   thread (the user posts it; the desk never writes to Timeline).
8. **Release integration.**
   - Release tab (`release_check.py`): show each ticket's workflow step/status next to
     its PR; flag tickets on qa-release whose workflow isn't approved at release_qa.
   - After release_qa approval, offer "Copy PR body" built from the new
     release-note.md section (copy only; never opens a PR).
9. **Prod fixes.** New steps after close, or a new flow entry: when a done workflow's
   task is reopened / gets a live bug, offer "Start prod fix" → `prod_fix` step
   (`timeline-progress` phase prod-fix, or `hotfix` for a file:line) → `release_hotfix`
   (`timeline-release hotfix`) → close. Keep the old history; increment a `round`.
10. **Estimate draft.** After Estimate approval, "Copy estimate for Timeline" (copy only).

### Tasks
- [ ] Comment ids in `parse_conversation_comments` / `render_task_markdown` + tests.
- [ ] Schema v5 `qa_findings`; candidate builder from `task_events`; classify endpoint
      `POST /api/workflow/{issue}/findings/{id}` `{status, note}`.
- [ ] `build_prompt("qa_fix", …, finding=event_id)`; result contract `finding`.
- [ ] Checklist UI in the qa / qa_fix step card.
- [ ] Release tab columns; copy-PR-body; prod_fix / release_hotfix steps in `FLOW`.
- [ ] Tests for each; docs.

### Acceptance
- Every QA comment after release is visible as a candidate, classified by the user,
  and each fix run is linked to exactly one finding, visible in the history.

---

## 6. Phase 4 — read-only steps run inside the dashboard

### Goal
Steps that don't change code run on the page with live output, no terminal window:
Intake, Estimate, Self-review, Release to QA, Release to prod, the Blocked side action,
QA question drafts. Code steps (Develop, Fix QA) stay in the terminal (user's decision).

### Design
1. **Per-step runner** in `FLOW`: `"runner": "terminal" | "desk"`, with a per-run
   override ("Run here" / "Run in terminal").
2. **Headless launch** (verify flags against `claude --help` at implementation time;
   all of these were present when phase 1 was written):
   `claude -p --output-format stream-json --verbose --session-id <uuid>
   --allowedTools <read-only list + writes under timeline/<id>/> "<prompt>"` started with
   `subprocess.Popen` in the worktree, its own process group, stdout to
   `timeline/<id>/runs/<run>.stream.jsonl`, exit code to `<run>.exit` by a watcher thread.
   In `-p` mode tools that aren't allowed are denied (no prompt), so the allow list is
   the safety boundary: Read/Grep/Glob, read-only git (`git log`, `git diff`,
   `git status`), the timeline MCP read tools, and Write/Edit limited to
   `timeline/<id>/**`. Check how path rules in `--allowedTools` resolve before relying
   on them; add a test that a write outside is refused.
3. **Schema**: `workflow_runs.mode` (`terminal|desk`), `pid`.
4. **Live output**: the page polls `GET …/runs/{id}/transcript` (the session log is
   written either way) or a new `GET …/runs/{id}/stream?after=<offset>` reading the
   stream file. Polling every 2 s while a desk run is running is enough.
5. **Stop**: `POST …/runs/{id}/stop` (guarded) → SIGTERM to the process group, record
   `abandon`.
6. **Needs input**: a desk run that needs an answer ends with `needs_input`; "Answer"
   on the page opens a text box and starts `claude -p --resume <session> "<answer>"`
   as a continuation (same run), or "Continue in terminal" (`--resume` interactively).
   The answer is the user's own text, so it may go into the prompt.
7. **Limits**: one desk run at a time per workflow (already enforced by "one open run"),
   a global cap (e.g. 2) for the desk, and a timeout.

### Tasks
- [ ] `runner` in FLOW; `launch_headless` in `work_session.py` (injectable for tests).
- [ ] Schema `mode`, `pid`; stop endpoint; answer endpoint.
- [ ] Allow-list per step; test that disallowed tools/writes are refused (fake runner).
- [ ] Live output UI; Stop/Answer buttons.
- [ ] Docs.

### Acceptance
- An Intake run on the page shows progress live, writes only under `timeline/<id>/`,
  and ends in "Waiting for your review" exactly like a terminal run.

---

## 7. Phase 5 — (optional) in-dashboard approvals for every tool call

### Goal
Even code steps run in the dashboard, and every edit/command Claude wants appears as
Approve / Deny on the page. Only if the user wants to leave the terminal entirely.

### Design
- Claude Agent SDK for Python (`claude-agent-sdk`; check the current package name and
  API in its docs before starting). Run a client per run in a worker thread; its
  permission callback (`can_use_tool` or current equivalent) puts each request into a
  `workflow_permissions(id, run_id, tool, input_summary, decision, decided_at)` table and
  waits; the page shows pending requests (poll or server-sent events) with Approve /
  Deny (+ "always allow this command in this run").
- Chat box on the page for messages to the session.
- Security: this makes the dashboard able to edit code. Keep 127.0.0.1-only, header
  guard, no CORS; consider a per-session secret token in the page for POSTs; time out
  pending permissions to Deny.
- New dependency → `requirements.txt`, plugin packaging notes in CLAUDE.md.

### Acceptance
- A Develop run completes from the page alone, with every tool call approved on the
  page and recorded in the history.

---

## 8. Cross-cutting notes

- **Schema versions** live in `panteon_client._open_planner_db`; add a new
  `if user_version < N` block per change, never edit an old block.
- **Tests**: real git in tmp + recording launcher (see `tests/test_workflow.py`); never
  live Timeline credentials; mock `get_task_statuses` in endpoint tests.
- **After code changes**: `scripts/dashboard-service.sh restart`; the `web/` files are
  served no-cache (reload the page).
- **Verify UI** with the sandbox recipe in §3.8, not on the user's real repos (Start
  creates real worktrees; Run opens real terminals).
- **Keep the two docs in step**: README "Task workflows" for the user, CLAUDE.md
  `workflow.py` paragraph for the next Claude session, and this file for history.

## 9. History

| Date | What |
|---|---|
| 2026-10-27 | Proposal: playbooks per stage, everything on screen. Groundwork fixes 1–3 (lifecycle skills, shared timeline/, intake gate; qa-fix phase fallback). |
| 2026-10-27 | Decisions: code steps in terminal (option 1), max approvals, me-only local DB. |
| 2026-10-27 | Phase 1 built: `workflow.py`, schema v3, task workflow panel, badges; old Start launcher removed. 252 tests; sandbox click-through; real Claude run not yet tried. |
| 2026-10-28 | Phases 2–5 built (§10). 269 tests; sandbox click-through of every phase; real Claude runs and the real Agent SDK not tried yet. |
| 2026-10-28 | Phases 4 and 5 removed: terminal only, plus a live status of what the terminal is doing (§10.9). |
| next | Phase 1 corrections (§3.9) and the unverified items in §10.6. |


---

## 10. Phases 2–5 as built (2026-10-28)

### 10.1 Storage (schema versions in `panteon_client._open_planner_db`)

| Version | Adds |
|---|---|
| 4 | `workflows.seen_event_id`, `step_since`, `step_run_floor`, `step_event_floor` |
| 5 | `qa_findings` table; `workflow_runs.finding_id`; `workflows.qa_event_floor`, `prod_round` |
| 6 | `workflow_runs.mode` (`terminal` / `desk` / `agent`), `workflow_runs.pid` |
| 7 | `workflow_permissions` table (tool calls waiting for / decided on the page) |

`_set_workflow` sets the three step floors whenever the step changes; `_enter`
also opens a new QA window (`qa_event_floor`) when coming into `qa` from outside QA.

### 10.2 Phase 2 — following Timeline

- `start_workflow` puts the task on the watchlist (`task_events.watch_task`); the
  start endpoint syncs its history once (`sync_watchlist`) and `reset_baseline`
  marks it seen, so only later events are news. The existing heartbeat keeps it synced.
- `signal_for(event, step, me)` / `workflow_signals` (computed on read, not stored):
  `blocked` (suggests Explain the blocker), `qa_done` (suggests QA approved),
  `qa_started` (warning: Timeline in testing, workflow still before QA),
  `moved_back` (suggests QA found issues), `qa_comment` (suggests QA found issues),
  plus info for other stage changes, comments, commits and field changes (Deadline
  is a warning). Stage text is parsed by `parse_stage` (`"Testing pending"`).
- `ack` ("Mark Timeline changes as seen") sets `seen_event_id`; acting on
  qa_issues / qa_passed / back_to_develop also marks them seen.
- `"me"` in `timeline-desk.json` (your Timeline display name) keeps your own comments
  out. The bearer token has no name claim, so it can't be derived.
- Status code `attention` when there are unseen warn/action signals.
- Side run `unblock` (SIDE steps don't move the flow; approving just records it).
- **Workflows** tab (`#/workflows`): Needs you / Running / Waiting on QA / Stopped / Done.
- Task panel: "New in Timeline" box; suggested buttons are outlined.

### 10.3 Phase 3 — QA findings, replies, releases, prod fixes

- `task.md` comment headings now end with `· comment id <Timeline id>`
  (`parse_conversation_comments` keeps `id`), so prompts point at a comment by id.
- `_sync_findings` (on every read while at qa / qa_fix) adds each comment after the
  QA window opened, not by `me`, as a `new` finding (unique per comment).
  `add_finding` adds one in your words (status `bug`).
- `classify_finding`: new / bug / question / not_bug / wont_fix; a note is needed for
  question, not_bug, wont_fix and for reopening a fixed one.
- Fix QA findings: Run is blocked until a finding is Bug (when findings exist); each
  run fixes one finding (default the oldest open bug, or "Fix this" on a finding);
  approving the run marks it `fixed`. Finish is blocked while any finding is new or bug.
  Without any tracked findings it falls back to the free-form prompt.
- Side run `qa_reply` drafts a reply for a Question into `timeline/<id>/replies/F<n>.md`.
- Prod fixes: a done workflow offers "Start a prod fix" (needs a note) →
  `prod_fix` (repeats; `timeline-progress` prod-fix / `hotfix`) → `release_hotfix`
  (`timeline-release hotfix`) → close. Those two steps are hidden in the stepper
  until used (`extra`, `prod_round`).
- Release tab: each PR shows its ticket's workflow badge and a note when the workflow
  isn't at the matching release step. Task docs have a Copy button (release notes,
  estimates, reply drafts).

### 10.4 Phase 4 — read-only steps on the page (removed, see §10.9)

- `runner` per step: `desk` for intake, estimate, review, release_qa, release_prod,
  release_hotfix and both side runs; `terminal` for develop, qa_fix, prod_fix (never desk).
  Desk steps also offer "Run in terminal instead".
- `work_session.headless_command` runs
  `claude -p <prompt> --output-format stream-json --verbose --session-id <id>
  --add-dir <skills> <main checkout timeline/<id>> --allowedTools <desk_tools>` in the
  worktree via bash (stream → `runs/<run>.stream.jsonl`, stderr → `.err`, exit code →
  `.exit`), in its own process group (`popen_headless`, pid stored).
- `desk_tools`: Read, Grep, Glob, read-only git, the two timeline MCP read tools, and
  Write/Edit only under `timeline/<id>/` (relative and the resolved main-checkout path).
- Live output: the page reads the stream file (`read_transcript` → `live: true`) and
  refreshes every 3 s while a desk run runs.
- Stop run (kills the group, records rejected); Answer (your text + the report reminder,
  `claude -p --resume`, appended to the same stream).
- At most `MAX_DESK_RUNS` (2) desk runs at once; a desk run whose process is gone
  without an exit file shows "Ended without a result".

### 10.5 Phase 5 — approvals on the page (Agent SDK) (removed, see §10.9)

- `agent_runner.py`: a run in a thread of the dashboard with its own asyncio loop.
  `sdk_backend` is the only SDK code: `ClaudeSDKClient` with `can_use_tool`,
  `permission_mode="default"`, `setting_sources=["user","project","local"]`,
  `system_prompt={"type":"preset","preset":"claude_code"}`, `session_id` (or
  `extra_args["session-id"]`), `add_dirs`, `cli_path`. Options are filtered by the
  installed `ClaudeAgentOptions` fields. Messages are written as stream-json lines.
- Every tool call except reading (`ALWAYS_ALLOWED`) becomes a `workflow_permissions`
  row; the run polls it, and it expires after 15 min or when the run stops.
- Page: "Run here with approvals" (any run step, code steps included) appears only when
  `claude_agent_sdk` is importable; Approvals card with the command or the file change
  (old/new); Allow / Deny (deny needs a reason Claude reads); a message box to write to
  the session; refresh every 2 s. Approve / Reject / Stop close the session.
- A dashboard restart ends these runs ("Ended without a result"; pending requests expire);
  Continue in terminal resumes the session id.

### 10.6 Not verified yet (do these first)

- [x] SDK installed in `.venv` (claude-agent-sdk 0.2.174, 2026-10-28; optional, not in
      `requirements.txt`, so each machine installs it). Its `ClaudeAgentOptions`,
      `PermissionResultAllow/Deny`, client methods and `SystemPromptPreset` match the adapter;
      `session_id` becomes `--session-id`. A real smoke run of `agent_runner` in a scratch
      folder: Claude asked to Write a file, the request went through `workflow_permissions`,
      was allowed, the file was written, the session closed with exit 0, and the user's
      settings (hooks) loaded.
- [ ] A real "Run here with approvals" from the task page on a real task (worktree, MCP,
      the step prompt, the result file).
- [x] One real desk run (#6110 Intake, run 2, 2026-10-28): `mcp__timeline-desk__get_task_conversation`
      worked, Write through the worktree's `timeline/` link worked (task.md, analysis.md,
      progress.md, result file), exit 0, 29 turns, $0.91.
- [ ] Desk runs can't reach the legacy repo: the skills say `../duell-pos`, which from a
      worktree is `duell-admin-worktrees/duell-pos` (missing), and `ls` outside the allowed
      folders is refused. Add the legacy checkout as an `--add-dir` (a `legacy` path per repo
      in `timeline-desk.json`) and tell the prompt its absolute path.
- [ ] One real terminal run (phase 1 item): result file written, session log found.
- [ ] Set `"me"` in `timeline-desk.json`.
- [ ] Tasks that started a workflow before phase 2 aren't on the watchlist (the page
      says so). Watch them from the Task tab or restart the workflow.
- [ ] systemd restarts of `timeline-desk` kill desk and agent runs (same cgroup).
- [ ] UX: confirmations, reasons and answers use `window.confirm` / `window.prompt`.

### 10.7 Fixes after the first real run (#6110, 2026-10-28)

The first real terminal run (Intake on #6110) stayed "Running" after its terminal
was closed, and the session had no timeline MCP tools.

- **Dead terminal runs end.** Closing the window kills the wrapper before it writes
  `<run>.exit`. `refresh_run` now also ends a terminal run when no process has its
  session id on its command line (`work_session.session_alive`, a `/proc` scan; `None`
  where `/proc` doesn't exist, which leaves the run alone), after a 60 s grace from the
  last launch (`runs/<run>.launched`, touched by run and resume).
- **Stop workflow ends the open run** (rejected, note "Workflow stopped." or "Workflow stopped: <reason>"; stop asks for no reason),
  stopping a desk process or an approvals session. `stop_group` never signals the
  dashboard's own process group.
- **Every run brings its own timeline MCP server.** Cause: `~/.claude.json` has
  `disabledMcpServers: ["timeline-mcp"]` for duell-admin, Claude Code applies that to the
  repo's worktrees, and duell-admin's own `timeline` server comes from an untracked
  `.mcp.json` that worktrees don't have. Runs now get
  `--mcp-config {"mcpServers": {"timeline-desk": …}}` (this repo's `server.py`,
  `PANTEON_TIMELINE_DIR` = the main checkout; the SDK gets the same as `mcp_servers`).
  Verified in the #6110 worktree: `timeline-mcp` disabled, `timeline-desk` connected with
  all six tools. Prompts name `mcp__timeline-desk__*` and give the `fetch_task.py` CLI as
  a fallback; desk runs may run that CLI. In terminal runs the prompt now comes first,
  because `--mcp-config` takes several values.

### 10.8 Reset (2026-10-28)

Reset means **erase everything** for the task (the user's definition): "Reset…" (offered
in every state) opens a form that lists what goes: the workflow row and every run,
decision, QA finding and approval request, and every file in `timeline/<id>/` (the main
checkout's folder, and the worktree's only if its `timeline/` is a real folder). You
confirm by typing the task number. `reset_workflow(issue, confirm)` stops a run working
on the page (desk process, approvals session, waiting for the session to finish writing),
refuses while a terminal run is still open, deletes the rows, then `shutil.rmtree`s the
folder(s). Kept: the worktree and its branch (Work tab), and the copied Timeline history
(`task_events`, `watchlist`). Afterwards the task page shows "Start a workflow" again.
`POST /api/workflow/{issue}/reset {confirm}` (guarded). Nothing records the reset itself,
because nothing of the workflow is left.

### 10.9 Terminal only (2026-10-28)

Three ways to run a step ("Run here", "Run in terminal instead", "Run here with approvals")
behaved differently and two of them were invisible background processes. Decision: **every
run is an interactive Claude Code session in a terminal; nothing runs in the background.**

- Removed: `agent_runner.py`, `desk_tools`, `DESK_STEPS` / step `runner`, `run_step`'s
  `mode`, `headless` and `agent_backend`, `answer_run`, `message_run`, `decide_permission`,
  `stop_run`, the endpoints `answer`, `stop-run`, `message`, `permissions/{id}`,
  `work_session.headless_command` / `popen_headless` / `stop_group` / `alive`, the page's
  Approvals card and message box, the actions `run_terminal`, `run_agent`, `answer`,
  `stop_run`, `message`. The Agent SDK is no longer used. Schema stays at version 7
  (`workflow_runs.mode` / `pid` and `workflow_permissions` are unused; old rows are read as
  terminal runs, and an old open run ends by the usual "no process has its session id" rule).
- Added: `terminal_activity(run)` reads the tail of the run's Claude session log and says what
  the terminal is doing: **starting**, **working**, **running a tool** (with the tool call),
  **waiting on a tool call** (same call for 5 s+: still running or asking you to approve it),
  **asking a question** (`AskUserQuestion`) and **waiting for your message** (Claude ended its
  turn with text). Each carries the log's age ("last activity 40 s ago"). The run card shows
  it as a status line; `question` and `reply` turn the workflow badge to "asks you" and put
  the task in the needs-you group; the page re-reads every 5 s while a run is open.
- Limits: the log can't tell a permission prompt from a long-running command, so "waiting on
  a tool call" says both; it also lags a few seconds behind the terminal. Nothing is sent to
  the terminal from the page.
