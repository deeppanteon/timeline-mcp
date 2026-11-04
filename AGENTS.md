# Repository Guidelines

## Project Structure & Module Organization

This repository contains a small Python MCP server for Panteon Timeline.

- `server.py` defines the FastMCP server, exposes the `get_task_conversation` tool, and registers the `timeline://<issue_id>/task.md` resource.
- `panteon_client.py` contains URL parsing, API calls, response parsing, image extraction, markdown rendering, and the shared `fetch_task_data`/`fetch_and_save` orchestration.
- `fetch_task.py` is a thin CLI over `fetch_and_save` for fetching a single task from the shell.
- `requirements.txt` lists runtime dependencies; `requirements-dev.txt` adds the test tooling.
- `.env.example` documents expected local configuration; `.env` is local-only and must not be committed. `.gitignore` enforces that plus `timeline/`, `.venv/`, and caches.
- Generated task output is written to `timeline/<issue_id>/task.md` by default, or under `PANTEON_TIMELINE_DIR` when set.

Unit tests live in `tests/` (`test_panteon_client.py`). Add tests there when introducing new behavior.

## Build, Test, and Development Commands

- `pip install -r requirements.txt` installs FastMCP, Requests, and dotenv support.
- `pip install -r requirements-dev.txt` additionally installs pytest for the test suite.
- `python3 backend/server.py` runs the MCP server over stdio for local MCP client integration.
- `python3 backend/fetch_task.py <url-or-id>` fetches a single task from the shell (add `--print` to echo the markdown).
- `python3 -m py_compile backend/server.py backend/panteon_client.py backend/fetch_task.py` performs a quick syntax check.
- `pytest` runs the unit tests (no network or credentials required).

Authentication is required for real API calls. Use `PANTEON_BEARER_TOKEN` or `PANTEON_COOKIE` in `.env`, or pass credentials through the MCP tool arguments.

## Coding Style & Naming Conventions

Use Python 3.10+ conventions with 4-space indentation and type hints for public helper functions. Keep API interaction, parsing, and rendering logic in `panteon_client.py`; keep MCP tool registration in `server.py`. Use `snake_case` for functions, variables, and environment-derived settings. Prefer small, deterministic helpers for parsing and formatting so they can be tested without network access.

## Testing Guidelines

When adding tests, use `pytest` and place files under `tests/` with names like `test_panteon_client.py`. Prioritize unit tests for pure functions such as `extract_issue_id`, `_normalize_date`, `extract_images`, `absolutize_markdown_images`, and `render_task_markdown`. Mock `requests.get` for API behavior; do not require live Panteon credentials in automated tests.

Run tests with:

```bash
pytest
```

## Commit & Pull Request Guidelines

Git history is not available in this checkout, so no repository-specific commit convention can be inferred. Use concise, imperative commit subjects such as `Add timeline image extraction tests`.

Pull requests should include a short summary, testing performed, and any configuration or credential requirements. Link related issues when available. For changes that alter generated markdown output, include a before/after snippet or sample output path.

## Security & Configuration Tips

Never commit `.env`, bearer tokens, cookies, generated private task exports, or files under `timeline/` that contain customer data. Keep defaults safe for local development, and document any new environment variables in `.env.example` and `README.md`.
