// The text of the Help page (see showHelp in app.js). Plain data, written for people who have never seen the desk.
// Keep it in step with the code: what a page reads, when it refreshes. The numbers that can be configured
// (background check intervals) and the workflow steps are read from /api/about, so they cannot go stale.
"use strict";

const HELP = {
  intro: [
    "The Timeline Desk is a private dashboard that runs on your own computer. It reads your tasks from Panteon Timeline (timeline.panteon.no), keeps its own history of what changed, and helps you take a task from first reading to release with Claude Code.",
    "It only reads from Timeline. It never posts a comment, changes a status or edits a task there. Everything it writes goes into files and a database on this computer.",
    "Nothing here is reachable from other computers: the page only answers on 127.0.0.1 (localhost).",
  ],

  glossary: [
    ["Issue number", "The number people use for a task, like 5691. It is what you see after # everywhere in the desk."],
    ["Board (list)", "A Timeline project list. Its code (for example 15033) is part of every task link: /tasks#/list/<board>/edit/<issue>. The Open in Timeline buttons need it."],
    ["Planner period", "Your priority list is split into Now, Next and Coming up."],
    ["Stage", "Timeline tracks six stages per task: RSD (requirements), Estimate, Development, Testing, QA and Live. The coloured bar under a task shows them in that order."],
    ["Stage colours", "Green = done, amber = in progress, red = blocked, hatched = skipped, empty = not started."],
    ["Release state", "Worked out from the QA and Live stages: before QA, in QA, QA blocked, QA approved or live."],
    ["Deadline flags", "Overdue = the date has passed. Due today. Due in N days = within the next 7 days."],
    ["Support task", "A task from a project's Support list rather than its normal tabs. It gets an amber support tag."],
    ["Watch", "Tell the desk to copy a task's full history (comments, status changes, commits …) into its database. Only watched tasks appear on the Timeline page. Tasks with a workflow are watched automatically."],
    ["Snapshot", "A saved copy of your planner at one moment. The Changes page compares snapshots."],
    ["Heartbeat", "The short background check that runs every few minutes. See Background checks."],
    ["Worktree", "A separate checkout of a repository for one task, so work on a task never disturbs your main checkout. Created as <worktrees folder>/<repo>-<issue>, on a branch named <issue>-<task title>."],
    ["Workflow", "The fixed route a task follows in the desk: intake, estimate, develop, self-review, release to QA, QA, fix QA findings, release to production, close."],
    ["Run", "One Claude Code session started by a workflow step, in a terminal, in the task's worktree."],
    ["Finding", "One point QA raised that needs a fix. The Fix QA step handles one finding per run."],
  ],

  pages: [
    { key: "planner", title: "Planner", sources: ["live", "cache", "db"],
      purpose: "Your priority tasks at a glance, in three columns: Now, Next and Coming up.",
      from: [
        ["Your tasks (Now, Next, Coming up)", "cache", "Timeline's employee planner. The server keeps the last answer in memory for up to {planner_cache} minutes; the background checks keep it current, so opening the page does not call Timeline each time."],
        ["Stage bars (RSD … Live)", "live", "Timeline, one request per task, when the page opens. Kept in the browser until you press Refresh."],
        ["Workflow badges", "db", "Our database (the Workflows page's data)."],
        ["PR badges (PRs ok, Check PR, No PR)", "git", "The last Release report, kept in the server's memory. If it is older than 30 minutes the page rebuilds it once in the background (git fetch and gh, about 10 seconds), and tries again no sooner than 10 minutes after a failure."],
      ],
      use: [
        "Click a card to open the task. The ↗ button next to the title opens it in Timeline.",
        "The PR badge appears for tickets in QA, QA done or live: No PR (red) means the PR their stage needs does not exist yet, Check PR (amber) means it is open but should be merged. Click it for the details and the Create PR button.",
        "The badge on a card shows the task's workflow (for example + Workflow, or the current step). Click it to open the task page, where its workflow panel is.",
        "Deadline colours: red = overdue, amber = due within 7 days.",
      ],
      refresh: "No automatic refresh while the page is open. The Refresh button fetches the planner and the stage bars from Timeline again.",
      how: "The server asks Timeline for every employee's planner and keeps only yours (your user id comes from the setting or from your login token).",
      know: [
        "Whoever is missing from the planner entirely gives an error (wrong user id), which is different from a planner with no tasks.",
        "The top bar shows your user id, today's date and how many tasks are in Now.",
      ] },
    { key: "workflows", title: "Workflows", sources: ["live", "cache", "db"],
      purpose: "Every task that has a workflow, grouped by what needs you: Needs you, Running, Waiting on QA, Stopped, Done.",
      from: [
        ["Workflows, steps, runs, decisions, findings", "db", "Our database. Nothing moves without a decision you make."],
        ["\"New in Timeline\" signals", "db", "Events of the tasks the desk watches, copied into our database by the background checks."],
        ["Stage bars", "live", "Timeline, per task, when the page opens."],
        ["Deadline, period, support chips", "cache", "The same in-memory planner copy as the Planner page."],
        ["What a terminal run is doing", "git", "Claude Code's session log on this computer; no Timeline call."],
      ],
      use: [
        "Pick a workflow on the left; its panel opens on the right. The panel is the same as on the task's own page.",
        "The panel shows only the buttons that are possible right now (see Workflow steps and rules below).",
        "A \"new in Timeline\" signal means someone commented or changed the task since you last marked it seen. The desk suggests an action; it never runs one by itself.",
      ],
      refresh: "The list re-reads our database every 15 seconds while the page is open. An open workflow panel re-reads every 5 seconds while a run is open. Changes made in Timeline arrive with the background checks.",
      how: "A step opens an interactive Claude Code session in a terminal in the task's worktree. The desk records the run, the result file Claude writes there and your decisions, each with its time.",
      know: [
        "Runs are normal Claude Code sessions. Claude's own permission prompts in the terminal are the only approvals; the page never approves anything for it.",
        "If you close the terminal without finishing, the desk notices when no process carries the session any more, and shows the run as ended without a result.",
      ] },
    { key: "changes", title: "Changes", sources: ["db"],
      purpose: "What changed in your planner: tasks that are new, left, moved between periods, or changed deadline or stage.",
      from: [
        ["Planner changes", "db", "Snapshots of your planner stored in our database. The page shows differences between snapshots; it does not call Timeline when it opens."],
      ],
      use: [
        "Newest change first. Entries found since your last visit are marked \"since your last visit\".",
        "Use the buttons at the top to show only one kind of change (New, Left, Moved, Changed).",
        "The Refresh button checks Timeline right now instead of waiting for the background check.",
      ],
      refresh: "The page re-reads the database every 60 seconds while open. The Refresh button first fetches the planner from Timeline (with stages), saves a snapshot if anything changed, then shows the result.",
      how: "A snapshot is saved only when your tasks differ from the latest stored one, so every entry is a real change. The time of each check is kept separately as \"last checked\".",
      know: [
        "Moving a task up or down inside the same period is not a change. A task added on top does not count as a change for those below it.",
        "Tracking starts when the database was first created; the page says since when.",
      ] },
    { key: "timeline", title: "Timeline", sources: ["db"],
      purpose: "One feed of everything that happened on the tasks you watch: comments, status changes, field changes and (if you switch them on) commits.",
      from: [
        ["Events of watched tasks", "db", "Our database. Timeline already stores every event with an id, author and time, so the desk copies them; nothing is guessed."],
      ],
      use: [
        "Watch a task with the star button on the Task page's lists, or in the watchlist dialog at the top of this page.",
        "Filter by event type, by task, or search the loaded events. Your choices are remembered in this browser.",
        "Commits are hidden until you switch them on. Click the task name on a card to open the task.",
        "The watchlist dialog shows every watched task; Stop watching removes the task and deletes its stored events.",
      ],
      refresh: "The page updates itself: it checks the local database every 2 seconds and adds new events at the top with a brief glow, keeping your filters and scroll position. Events reach the database when the live socket tells the background check to run (within a few seconds), or at the latest on the next heartbeat (one request per watched task). The Refresh button syncs the watched tasks right away, then reads the database.",
      how: "The first sync of a task copies its whole history. After that only new events are added. A task is paused automatically once it is closed in Timeline.",
      know: [
        "Times are Timeline's own clock (not converted).",
        "An event that happened in Timeline but is not shown yet probably has not been synced: press Refresh.",
      ] },
    { key: "release", title: "Release", sources: ["cache", "git"],
      purpose: "Release readiness (does every ticket in QA or going live have the PR its stage needs?) and the pull requests waiting to go into qa-release and master (duell-admin), grouped by branch, with their release notes.",
      from: [
        ["Pull requests with their release notes", "git", "GitHub (gh) and the duell-admin checkout on this computer: git fetch, git log, gh pr list."],
        ["Branches and PRs in duell-pos", "git", "GitHub (gh pr list, git ls-remote) through the duell-pos checkout on this computer. No fetch."],
        ["QA approval and stage of each ticket", "live", "Timeline, one request per ticket, at the moment the report is built. The readiness list covers the tickets of your planner, the watchlist and active workflows that belong to the duell-admin project; other people's tickets only appear in the pending PR lists below it."],
      ],
      use: [
        "Release readiness lists only your own tickets (the ones in your planner, on your watchlist or with an active workflow) whose stage promises a PR, with one section per repository: duell-admin and duell-pos. In duell-admin, in QA needs a PR into qa-release; QA done or live pending needs one into master; live needs it merged. duell-pos works the same way with its own branches: qa-release updates the QA server and prod-release the Prod server. A branch that is already merged into the branch it should be in counts as done even without a PR (GitHub is asked, only when no PR answers). A ticket normally lives in one repository: the other is greyed out (No work here). A ticket with no branch and no PR with its number in either repository is red in both. Missing is red, an open PR that should be merged is amber.",
        "Create PR only appears in a repository's section when GitHub already has a branch there with the ticket number in its name (one git ls-remote per repository when the report is built); without one the row stays red and says so. Create PR opens a dialog with the title (QA: or PROD: #id title), the branch and a description drafted from the ticket's earlier PR or the branch's commits. You can edit all of it, or press Write with AI to have Claude word the description (it runs claude -p with every tool off and sees only the ticket title, the branch's commit messages and the diffs of the changed entity files under shared/src/Entity, never other code or task text). The dialog also compares the branch with its base: new or changed variables in .env.dist are added to the description with empty values to fill in, and changed Doctrine entities (Client or Admin DB) get SQL statements written by Claude, since the team does not write migrations. The PR is only created when you confirm. The branch must already be on GitHub: the desk never pushes.",
        "Press Check again to build a new report. It takes about 10 seconds because it fetches from GitHub.",
        "The page shows when the report was built. A report older than 10 minutes is rebuilt when you open the page; after a restart there is none until then.",
      ],
      refresh: "Built when you open the page (if the last one is over 10 minutes old) or press Check again. The Planner and Task pages also rebuild it in the background when it is over 30 minutes old. It is kept in the server's memory until the service restarts.",
      how: "The report is read only: fixed git and gh commands (no shell), only git remote/fetch/log/rev-list/ls-remote and gh pr list / release list. The one write is Create PR (gh pr create), after your confirmation; it refuses a branch that is not on GitHub, a ticket that already has an open PR into that branch, and a description without the \"- #id - title\" entry. A ticket is on QA when its merge is in qa-release but not in master. A master PR went \"via QA\" when it carries no commits that are outside qa-release and master. The guard looks at the 200 newest merged qa-release PRs and the 300 newest merged master PRs, so a very old ticket can show as missing.",
      know: [
        "The checkout comes from DUELL_ADMIN_DIR, or --repo-dir, or ../duell-admin, and its origin must be Panteon-norway/duell-admin. If the duell-pos checkout (DUELL_POS_DIR, --pos-dir or ../duell-pos) is missing or unreachable, the page says so and judges the tickets by duell-admin only; a ticket with nothing in duell-admin is then not called missing, because its work may be in duell-pos.",
        "If Timeline cannot be reached, the report still appears, with a note that QA approval is missing.",
      ] },
    { key: "task", title: "Task", sources: ["live", "db"],
      purpose: "Look up any task, see its conversation and commits, and run its workflow.",
      from: [
        ["Task list (My tasks, Archive, Support …)", "live", "Timeline, when the page opens. The browser keeps each tab's list until you press Refresh."],
        ["One task: description, comments, commits", "live", "Timeline, every time you open it. Nothing is saved from this."],
        ["Workflow panel and Watch buttons", "db", "Our database."],
        ["Pull requests box (duell-admin tickets in QA or going live)", "git", "The last Release report, in the server's memory. Create PR there works as on the Release page."],
        ["Open in Timeline buttons", "db", "Built from the task's board code, which comes from the lists you have loaded."],
      ],
      use: [
        "Choose a project, then a tab (Priority, Archive, Support …). Filter by tag, stage or text.",
        "Type an issue number or paste a Timeline link in the box and press Open task.",
        "A commit appears in the conversation at the time it was made, between the comments, and again in the Commits list below.",
        "The copy icon on a comment copies its text.",
      ],
      refresh: "No automatic refresh. Open the task again, or press Refresh, to fetch it again.",
      how: "Two Timeline calls per task: one resolves the issue number to Timeline's internal id, the second fetches the whole timeline, from which comments and commits are taken.",
      know: [
        "Only a Timeline link ending in /edit/<number> or a plain number is accepted; a list link is rejected on purpose, because it would point at the wrong task.",
        "A task with no description and no comments just shows that, nothing is invented.",
      ] },
    { key: "work", title: "Work", sources: ["git"],
      purpose: "The task worktrees on this computer, how big they are and whether they hold unsaved work.",
      from: [
        ["Worktrees, their branch, size and state", "git", "git on this computer, read when the page opens. No Timeline call."],
      ],
      use: [
        "State shows uncommitted changes, commits that exist only on this computer, or \"no new commits vs base\".",
        "Remove deletes the worktree folder. Remove + branch also deletes its branch.",
      ],
      refresh: "No automatic refresh. Open the page or press Refresh.",
      how: "Removal never forces: git keeps a worktree with uncommitted changes and a branch with commits that are not merged anywhere. Only folders inside a configured worktrees folder can be removed.",
      know: [
        "A worktree with uncommitted changes cannot be removed from here; commit or discard first.",
      ] },
    { key: "jira", title: "Jira", sources: ["live", "db"],
      purpose: "A small board of the tasks you pick, in Jira-style columns.",
      from: [
        ["Board cards and their columns", "db", "Our database. Only the task number and title are kept."],
        ["The list of tasks you can add", "live", "Timeline, when you open the add picker."],
      ],
      use: [
        "Use + in a column to open the picker: your assigned tasks (and support tasks) not yet on the board. Tick tasks, then add.",
        "Drag a card to another column to move it. Search filters the cards by title.",
      ],
      refresh: "No automatic refresh. Adding, moving and removing a card is saved immediately.",
      how: "Each change also adds a row to a queue that a Jira bot reads and marks as delivered. This page itself never talks to Jira.",
      know: [] },
  ],

  setup: [
    ["Credentials", "A Timeline bearer token or cookie in the .env file next to the code (PANTEON_BEARER_TOKEN or PANTEON_COOKIE). A 401/403 means it is missing or has expired: sign in to Timeline again and update it."],
    ["Your user id", "Taken from --user-id, then PANTEON_USER_ID, then from your login token."],
    ["Repos and boards", "timeline-desk.json lists each Timeline board with its repository, base branch and worktrees folder, plus your name (\"me\"). It is read again on every request, so edits apply at once. Without it the Work page and the Start buttons are empty."],
    ["Release checkout", "DUELL_ADMIN_DIR (or --repo-dir) points to the duell-admin checkout used by the Release page; DUELL_POS_DIR (or --pos-dir) to the duell-pos checkout the PR guard also reads (default ../duell-pos)."],
    ["Address and service", "http://timeline.localhost:8765. It runs as the systemd user service timeline-desk: scripts/dashboard-service.sh install | restart | status | uninstall. After changing Python code, restart it. Page files (web/) apply on reload."],
  ],

  safety: [
    "Listens on 127.0.0.1 only; requests for any other host name are refused (blocks DNS rebinding). There is no CORS.",
    "Every action that changes something (start a run, decide, snapshot, watch, remove a worktree, rebuild the release report, create a PR) needs a special header a web page from another site cannot send.",
    "The page loads no outside fonts or scripts and sends no referrer. Text from Timeline is always inserted as plain text, never as HTML.",
    "Claude sessions are told never to push, open pull requests or merge, and the desk starts them with the normal permission prompts (no bypass flags).",
    "Worktree removal never uses force. Credentials are never written to the request log, which keeps only the time, path, status and duration of each call.",
    "Generated task files (timeline/) and the database can contain customer data and are kept out of git.",
  ],

  trouble: [
    ["The page says authentication failed", "Your token or cookie has expired or is missing. Update .env and restart the service. (The check_auth tool of the timeline MCP server says which.)"],
    ["Planner is empty or says the user was not found", "Empty = you really have no tasks there. \"Not found\" = the user id is wrong: set --user-id or PANTEON_USER_ID."],
    ["A task I know about is not on the Timeline page", "Only watched tasks are copied. Watch it first; its history is copied when you do."],
    ["Something changed in Timeline but not here", "Pages that read our database (Changes, Timeline, Workflows) trail Timeline by up to one heartbeat. Press Refresh to check right now."],
    ["Task not found", "Timeline does not know that issue number, or you typed a list link instead of a task link."],
    ["No Open in Timeline button on a task", "The button needs the task's board code. It is learned from the Planner and the lists you have opened. Open the Task page's list first."],
    ["Release is empty", "The report is only built when you press Check again. Make sure the duell-admin checkout exists and its origin is right."],
    ["Work is empty", "No repos are set up in timeline-desk.json, or no workflow has made a worktree yet."],
    ["Timeline seems slow or rate limited", "The desk spaces its calls out and logs each one in logs/timeline-requests-<date>.jsonl; count the lines to see how many calls it made."],
  ],
};
