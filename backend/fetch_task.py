#!/usr/bin/env python3
"""
Command-line fetcher for a single Panteon Timeline task.

Reuses the same orchestration as the MCP tool (panteon_client.fetch_and_save) so
the two stay in sync. Credentials come from the environment (.env) by default.

Examples:
    python3 fetch_task.py 5461
    python3 fetch_task.py "https://timeline.panteon.no/tasks#/list/15033/edit/4079"
    python3 fetch_task.py 5461 --print
    python3 fetch_task.py 5461 --base-dir /path/to/project
    python3 fetch_task.py --planner              # your priority tasks
    python3 fetch_task.py --planner --period next
    python3 fetch_task.py --planner --user-id 135
    python3 fetch_task.py --changes              # planner changes since last snapshot
    python3 fetch_task.py --my-day               # now + soon-due next + changes
    python3 fetch_task.py --status 4054 5627     # workflow stage / QA approval of tasks
"""
import argparse
import sys

# Importing panteon_client loads the .env next to it (see panteon_client.py).
import panteon_client


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "issue",
        nargs="?",
        help="Task URL (…/edit/<id>) or bare numeric issue ID.",
    )
    parser.add_argument(
        "--planner",
        action="store_true",
        help="Print your priority tasks from the employee planner instead of fetching a task.",
    )
    parser.add_argument(
        "--changes",
        action="store_true",
        help="Show planner changes since the last saved day and save today's snapshot.",
    )
    parser.add_argument(
        "--status",
        nargs="+",
        metavar="ISSUE",
        default=None,
        help="Print the workflow stage and QA approval of these tasks (IDs or URLs).",
    )
    parser.add_argument(
        "--my-day",
        action="store_true",
        help="Daily brief: now tasks, soon-due next tasks and changes (saves today's snapshot).",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="With --changes or --my-day: compare without saving today's snapshot.",
    )
    parser.add_argument(
        "--period",
        default="now",
        choices=list(panteon_client.PLANNER_PERIODS),
        help="Planner period for --planner (default: now).",
    )
    parser.add_argument(
        "--user-id",
        default=None,
        help="Planner user id; defaults to PANTEON_USER_ID, then the bearer token's `sub`.",
    )
    parser.add_argument(
        "--base-dir",
        default=None,
        help="Output root; defaults to PANTEON_TIMELINE_DIR or the current directory.",
    )
    parser.add_argument(
        "--print",
        dest="print_body",
        action="store_true",
        help="Also print the rendered task markdown to stdout.",
    )
    args = parser.parse_args(argv)

    planner_call = None
    if args.status:
        planner_call = lambda headers: {
            "markdown": panteon_client.render_task_statuses_markdown(
                panteon_client.get_task_statuses(args.status, headers)
            )
        }
    elif args.my_day:
        planner_call = lambda headers: panteon_client.fetch_my_day(
            headers, user_id=args.user_id, base_dir=args.base_dir, save=not args.no_save
        )
    elif args.changes:
        planner_call = lambda headers: panteon_client.fetch_my_planner_changes(
            headers, user_id=args.user_id, base_dir=args.base_dir, save=not args.no_save
        )
    elif args.planner:
        planner_call = lambda headers: panteon_client.fetch_my_priority_tasks(
            headers, user_id=args.user_id, period=args.period
        )
    if planner_call:
        try:
            result = planner_call(panteon_client.build_api_headers())
        except (ValueError, LookupError, PermissionError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(result["markdown"])
        return 0

    if not args.issue:
        parser.error("an issue URL/ID is required unless --planner, --changes, --my-day or --status is given")

    try:
        result = panteon_client.fetch_and_save(args.issue, base_dir=args.base_dir)
    except (ValueError, PermissionError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Task {result['issue_id']} saved to {result['saved_file']}")

    if args.print_body:
        print()
        print(
            panteon_client.render_task_markdown(
                result["issue_id"], result["task_meta"], result["comments"]
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
