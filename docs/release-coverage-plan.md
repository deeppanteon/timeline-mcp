# Release coverage: implementation plan

Status: plan, nothing built yet. Written 2026-11-06.

## Why

Ticket 4542 was released to QA in August. A later QA finding and a later update were committed afterwards and never
reached QA or master. The ticket was no longer in the planner or watchlist, Timeline's stage was never updated by QA,
and nobody noticed for a month. The existing PR guard cannot see this: it only looks at *now*, only at tickets already
in the planner/watchlist/workflows, and it reasons from PRs and Timeline stages.

## Goal

A **read-only** view that answers, for every ticket I have committed code for: *which of my commits are on
`qa-release`, which are on production, and which are missing*, in both `duell-admin` and `duell-pos`.
No buttons that change anything, nothing sent, nothing created.

## Rules this is built on (from the team's habits)

- A ticket's branch name contains its ticket number (`task_id_from_branch` already parses it).
- A ticket can have a branch in one repo or both. Only repos where it has commits count; the other is `n/a`.
- Merges are PR merge commits or direct merges without a PR. **No squash.** So git ancestry is exact; PRs are
  context only and are never required.
- Production base: `master` for duell-admin, `prod-release` for duell-pos (`PROD_BASES`, `REPO_SPECS`).

## Design

### 1. New module `backend/release_coverage.py`

Git-specific, like `release_check.py`, so it stays out of `panteon_client.py`. Reuse `_Repo`, `open_repo`,
`_subprocess_runner`/`Runner` (injectable for tests), `REPO_SPECS`, `task_id_from_branch`, `parse_branch_index`.
Only fixed argument lists, no shell. Timeline statuses come through an injected `status_lookup`, as in `build_report`.

### 2. Finding "my" commits per repo

Author identity: new optional `git_authors` list (emails) in `timeline-desk.json`; default is `git config user.email`
of each checkout. (`"me"` there is the Timeline name, not a git identity, so it cannot be reused.)

A commit belongs to ticket N when it is authored by me and either
- it is on a branch whose name carries N: `git log --first-parent --no-merges origin/<branch> --not origin/development`
  (first-parent keeps merges of `qa-release` *into* the branch from pulling other people's commits in), or
- its message mentions `#N` / N on any remote branch (`git log --remotes --grep`): this catches a QA fix committed
  on another branch or directly on `qa-release`.

Match the number on word boundaries (`4542` must not match `14542`); reuse `_BRANCH_ID` / `_TITLE_ID`.

### 3. Is a commit released?

Per repo, once per run: `git rev-list origin/qa-release` and `git rev-list <prod base>` into sets (as `build_report`
already does for master). A commit is on QA / on production when its sha is in the set. Fallback for odd cherry-picks:
`git cherry` (patch-id), applied only to commits the sha check calls missing.

### 4. Memory, to detect "dropped"

New SQLite tables in the planner DB (schema version 11 in `_open_planner_db`, same migration style):

- `coverage_commits(repo, issue, sha, branch, author, committed_at, subject, first_seen, in_qa, in_prod,
  qa_first_seen, qa_last_seen, prod_first_seen, prod_last_seen)`, primary key `(repo, sha)`.
- `coverage_checks(repo, checked_at, error)`: last check time and last failure per repo.

A commit with `qa_last_seen` set but `in_qa` now false is **dropped from qa-release** (qa-release was reset or
rebuilt). A commit never seen there is plain **missing**. Rows are updated in place; reads never create the DB.

### 5. Per ticket, per repo result

```
{issue, repo, branches, total, on_qa, on_prod, missing_qa, missing_prod, dropped_qa,
 last_qa_at, last_prod_at, commits: [{sha, subject, author, date, qa, prod, dropped}], state}
```

`state`: `shipped` (all on prod), `on_qa` (all on QA, some not on prod), `partial` (some on QA, some not),
`not_released` (none on QA), `dropped`, `n/a`. The ticket row is the worst of its repos.

### 6. Timeline cross-check (flags only)

One `get_task_statuses` call for the listed tickets, using the existing `release_state` / `qa_approved`. Flags:
- Timeline closed/live/QA-approved but commits missing on prod,
- all commits on prod but Timeline Live not set,
- Timeline blocked/stuck at QA while the code is already live.

A Timeline failure sets `timeline_error` and still returns the git part (same contract as `build_report`).

### 7. Fetching

Coverage needs the tips of ticket branches, so it runs
`git fetch --quiet origin '+refs/heads/*:refs/remotes/origin/*'` in both checkouts. This only updates
remote-tracking refs (no merge, no push, no worktree change). It is new for duell-pos, which so far uses only
`gh` and `ls-remote`, so it is the one new side effect; keep it behind the header-guarded POST and the scheduler,
never on a plain GET.

### 8. API and scheduler (`dashboard.py`)

- `GET /api/coverage`: cached report or null (no git, no network).
- `POST /api/coverage`: header-guarded (`X-Timeline-Desk: 1`), rebuilds. Errors map through `_respond`.
- Scheduler: rebuild hourly in active hours (like the stage lookups), because "dropped" can only be seen if a snapshot
  was taken while the commit was still there. Failures are logged, not fatal.
- Add the cached report to the same in-memory cache pattern the release report uses.

### 9. Page

New **Coverage** tab (`web/app.js`, `web/help.js`, `PAGE_SOURCES`; update the Help page text since data sources
changed): one card per ticket, worst first, complete tickets green and collapsed. Per repo: counts, last date on QA
and on prod, then only the missing/dropped/recent commits expanded; older commits collapse into "N commits released".
Timeline flags shown beside the state. All text via `h()` / `textContent`, no `innerHTML`.

### 10. Config and docs

- `timeline-desk.example.json`: add `git_authors`.
- Update `CLAUDE.md` (Architecture + Dashboard sections) and the Help page once built.

## Build order

1. **Spike (read-only script, no code kept):** run steps 2-3 against the real checkouts for 4542 and a few tickets
   known to be complete. Confirms the first-parent rule, the `--not origin/development` base, and the author filter
   find the right commits. Do this before writing the module.
2. `release_coverage.py` core (steps 2, 3, 5) + `tests/test_release_coverage.py` with a fake runner and, for the
   ancestry parts, real git in tmp repos (as `test_work_session.py` does): released, QA-only, missing, post-release
   commit, direct merge without PR, branch in one repo only, merge of qa-release into the branch.
3. Persistence + dropped detection (step 4), tested by running two snapshots with a reset qa-release between.
4. Timeline flags (step 6) with a fake `status_lookup`, incl. `timeline_error`.
5. API + scheduler (step 8), tests in `tests/test_dashboard.py` (TestClient, fake runner).
6. Page and Help text (step 9), `node --check web/app.js`, check in the preview.
7. Docs (step 10), `scripts/dashboard-service.sh restart`.

## Open points to settle in the spike

- Is `origin/development` the right "base" for both repos, or should it be the merge-base with `qa-release`?
- Do direct hotfix commits on `qa-release`/`master` by others mention the ticket? They would show as released with
  another author; acceptable, but confirm it is not noisy.
- Branches that were deleted before the first scan are found only through commit messages. A one-time backfill
  (`--since`) bounds the cost; pick the window (suggest 12 months).
- Cost on the larger repo: two `rev-list` sets plus one `git log` per ticket branch; measure in the spike.

## Out of scope

Any action (nudges, PR creation, merging, pushing, Timeline status changes), other people's tickets, and
squash-merge handling (the team does not squash).

## Spike findings (2026-11-06, ticket 4542, local refs fetched 5-6 Nov, nothing modified)

Ticket 4542 has a branch only in **duell-pos** (`4542-visma-nxt-gbat-check-balance`); duell-admin has no commits for it.

Result with the rules above, per commit (qa = in `origin/qa-release`, prod = in `origin/prod-release`):

| Commit | Date | Author | qa | prod |
|---|---|---|---|---|
| 2360d23cd5 check balance | 2026-01-13 | kashyap.gandhi | yes | no |
| ed3f748e28 account check field | 2026-01-27 | kashyap.gandhi | yes | no |
| 4e18c02232 step1 code submitted | 2026-06-17 | kashyap.gandhi | yes | no |
| 56e8d01686 Z-report validation | 2026-06-19 | deep.panara | yes | no |
| 5fa93b44a9 cash settlement errors | 2026-08-26 | deep.panara | yes | no |
| 92367f9d19 validation (yesterday) | 2026-11-05 | deep.panara | **no** | no |

So: 5 of 6 commits are on QA, **none is on production**, and yesterday's commit is not even on QA. This is the forgotten-ticket picture.

What this changes in the design:

1. **Base branch is per repo.** duell-pos branches from and merges `v4-release` (its `master` is stale, last commit 2025-07-01);
   duell-admin uses `development`. Put the "own commits" base in `REPO_SPECS` (`work_base`), do not hard-code `development`.
   `git log --first-parent --no-merges <branch> --not origin/v4-release` returned exactly the ticket's own commits.
2. **First-parent alone misses one.** `4e18c02232` is on the branch but not on its first-parent line; only the
   message rule (`--grep`) found it. Keep both rules and union them.
3. **Do not filter by one author.** The ticket has commits under two identities (kashyap.gandhi@ and deep.panara@).
   For the branch rule, drop the author filter; use `git_authors` only for the message rule on other branches, and
   list both emails by default. Show the author per commit.
4. **Commit convention helps.** Messages are `TL #<id> …`; the grep can match `TL #<id>` precisely, with a word boundary.
5. **PR merge commits and direct merges both appear** (`Merge pull request #1343`, `Merge branch '…'`); ancestry handled
   both, no PR data was needed.
6. Not tested yet: dropped detection (needs two snapshots over time) and cost on a full run; the local refs were fresh
   enough here, but the real run fetches first.

---

# Phase 2: PR trail and release notes (plan, nothing built yet; written 2026-11-06)

## Why

Two more ways a ticket gets forgotten, both seen in practice:
1. **No PR.** The developer never creates one. The release maker builds the note by copying PR descriptions, so a ticket without a PR is never in the note.
2. **In git, not in the note.** The commits are on QA (or production) but the release maker left the ticket out of the QA or master note. Sometimes tickets are added to a note 1 or 2 days after the release.

A third, process-level failure sits behind them: a ticket that reaches QA but is not approved stays on QA through every later production release until someone touches it again.

## How releases work here (from the user, confirmed against GitHub on 2026-11-06)

- The release maker copies PR descriptions into one complete note, creates it as a **draft GitHub Release**, and publishes it the next day. A forgotten ticket is sometimes added to the published release for 1-2 days.
- Each repo (duell-admin, duell-pos) has **separate notes for QA and for production** (master).
- Weekly cycle: **Tuesday** the QA release (qa-release branch goes to the QA server); **Friday and Monday** QA testing and client approval, one ticket at a time; what is approved by **Monday afternoon** goes to master on **Tuesday**. What is not approved stays on QA, and is forgotten.
- GitHub data seen: QA release = pre-release, tag `v1.0.3.7-qa` (duell-pos: `v2.2.4.8-qa`, once `2.2.4.1-qa`), target `qa-release`. Production release = normal release, tag `v1.0.3.6`, target `master`. Bodies list `- #6200 - Title` entries with sub-bullets. Tags are present in the local checkouts; `v1.0.3.6` is on both qa-release and master, `v1.0.3.7-qa` only on qa-release.

## What to check, per ticket (still only your subscribed tickets)

| Key | Rule | Level |
|---|---|---|
| `no_pr` | The branch has commits that are not on QA or production, and no PR (open, merged or closed) exists for the branch. | amber at 1 day, red at 3 |
| `not_in_qa_note` | The ticket's commits are in `prevQAtag..QAtag`, and `#id` is not in that QA release's body. | by release state, below |
| `not_in_prod_note` | Same for production tags. | by release state, below |
| `skipped_prod` | The ticket reached QA in release R, and N production releases published after R did not include it. | N = 1: amber (red if Timeline says QA approved, because approved tickets go out on Tuesday); N >= 2: red |
| `note_without_code` (later) | A note lists `#id` but no commit of it is in that release's range. | amber |

Release state decides how loud a missing entry is: a **draft** note is red and says "add it before it is published"; a note published **less than 2 days ago** is amber ("can still be added"); older is red ("missing from the published note").

## Design

### Data
- **Releases** (new `backend/release_notes.py`, git/gh-specific like `release_check`): `gh release list --json tagName,name,isDraft,isPrerelease,publishedAt,createdAt,url` per repo (1 call), classified into families: QA (pre-release or tag ends `-qa`) and production (everything else). `gh release view <tag> --json body,...` only for drafts, releases from the last 3 days, and ones not stored yet; older bodies are stored and never fetched again.
- **Entries**: ids from lines matching the existing `_ENTRY` pattern in `release_check.py` (`- #id - title`); only top-level lines.
- **Expected tickets of a release**: commits in `prev..tag`, where `prev` is the previous release of the same family by creation date, mapped to ticket numbers by `merged_branch`/`task_id_from_branch` on merge commits and `TL #N` in messages. For a draft (its tag may not exist yet) the range ends at `origin/<target branch>`.
- **PR trail**: `gh pr list --state all --json number,headRefName,baseRefName,state,url,body` indexed by branch (reuse `build_pr_index` where possible). A ticket "has a PR" when any PR has its branch as head.
- **First reached QA / production**: the first release (tag) whose range contains the commit, which also gives `skipped_prod` without relying on first-seen dates.

### Storage (SQLite schema 14)
`coverage_releases(repo, tag, kind, created_at, published_at, is_draft, url, body_fetched_at)` and `coverage_release_entries(repo, tag, issue_number)`. Bodies are not stored, only the ids and the fetch time.

### Report and page
- Each ticket gets `trail`: per repo `{pr: {state, url}, qa_note: {tag, state, listed}, prod_note: {...}, skipped_prod: n}`.
- New lines in the existing Mismatch/Check list, and a "Paper trail" row of three small chips in each repo panel (PR, QA note, Prod note: listed / not listed / draft), with the release as a link.
- Flags feed `attention`, so a forgotten ticket lands in "needs attention" automatically.
- Optional: a Copy button that puts the ready-to-paste entry (`- #id - title`, plus the PR description or commit subjects) on your clipboard to send to the release maker. The desk still never posts or edits anything.

### Build changes to existing code
- `fetch_branches` also fetches tags (`--tags`), so new release tags exist locally.
- `report` gets `releases` (latest per family) for the page header.

## Build order
1. **Spike (read-only):** for the last 4 QA and 4 production releases in each repo, compute expected vs listed tickets and compare with reality; confirm that ticket 4542 shows as missing from the August QA note, and see how many false alarms an ordinary release produces.
2. `release_notes.py` core with tests (real git in tmp for ranges, a fake `gh` runner for release list/view and PRs).
3. Persistence and the 3-day refresh window.
4. Wire into `build_coverage`: `trail`, flags, `attention`.
5. Page chips and lines; Help text; `CLAUDE.md`.

## Open points
- Does a branch merged directly into qa-release (no PR) count as "has a PR"? The release maker copies from PRs, so I plan to treat it as missing a PR, but flag it amber only.
- Hotfix releases the same day (for example `v1.0.3.3` on the day of `v1.0.3.4-qa`) are fine for ranges, since families are ordered by creation date; check them in the spike.
- Tickets deliberately left out of notes (internal work) will show as amber Check lines, never as red errors, unless the ticket is QA approved in Timeline.
