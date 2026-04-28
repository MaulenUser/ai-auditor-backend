# Project Guide for AI Agents

This repository contains a Python port of several Bitrix24 export scripts. It is intended to be safe for both Claude-style agents and Codex-style agents to operate on without rediscovering the architecture from scratch.

## Project Summary

- Package name: `bitrix-ingest`
- Python: `>=3.11`
- Packaging: `src/` layout via `pyproject.toml`
- Main dependency: `requests`
- Test runner: `pytest`
- Shell environment in this workspace is typically PowerShell on Windows

The project exports data from Bitrix24 and exposes an HTTP API for the frontend:

1. CRM base snapshot
2. Call-record scan via VoxImplant statistics
3. WhatsApp timeline export
4. AI audit pipeline (WhatsApp → OpenAI features → recommendations)
5. Business profile settings persisted in SQLite

## Current Architecture

The codebase intentionally uses a layered structure. Do not reintroduce top-level compatibility shims under `src/bitrix_ingest/`.

```text
src/bitrix_ingest/
  __init__.py
  api/
  application/
  cli/
  domain/
  infrastructure/
    database/
    http/
    openai/
    persistence/
```

### Layer responsibilities

- `domain/`
  Pure entities and domain rules. No I/O, no `requests`, no filesystem writes.

- `application/`
  Use-cases and orchestration. Depends on ports/protocols, not concrete HTTP or filesystem code.

- `infrastructure/`
  Concrete adapters: HTTP client, retry policy, paginator, filesystem JSON writer, logging config.
  - `infrastructure/database/` — SQLite repository for persistent settings (BusinessProfile).

- `cli/`
  Thin command-line entry points. Parse args, wire adapters, call application services.

- `api/`
  FastAPI application (`app.py`). Thin HTTP layer — parses requests, wires infrastructure, calls application services. Also exposes `GET /api/app-state` and `POST /api/setup-profile` for the frontend settings page.

## Architectural Rules

Follow these rules unless the user explicitly asks for a different architecture:

1. Keep CLI thin.
   CLI modules should parse arguments, instantiate infrastructure adapters, and call one application service.

2. Keep application services framework-agnostic.
   Services should depend on `BitrixGateway` and `JsonSink` from `src/bitrix_ingest/application/ports.py`.

3. Keep domain pure.
   Domain modules must not import `requests`, `argparse`, or filesystem helpers.

4. Prefer composition over inheritance.
   The HTTP client is assembled from transport, retry policy, paginator, and webhook value object.

5. Do not recreate backward-compat wrappers.
   Older root-level modules such as `client.py`, `models.py`, `export_crm.py`, etc. were intentionally removed.

6. Add new behavior in the correct layer.
   - New export orchestration -> `application/`
   - New Bitrix HTTP behavior -> `infrastructure/http/`
   - New value objects/entities -> `domain/`
   - New command-line command -> `cli/`
   - New HTTP endpoint -> `api/app.py`
   - New persistent settings -> `domain/` entity + `infrastructure/database/` repository

## Important Files

- Project config: `pyproject.toml`
- Migration notes / preserved behaviors: `MIGRATION.md`
- Ports: `src/bitrix_ingest/application/ports.py`
- **FastAPI app**: `src/bitrix_ingest/api/app.py`
- **Business profile entity**: `src/bitrix_ingest/domain/business_profile.py`
- **SQLite repository**: `src/bitrix_ingest/infrastructure/database/repository.py`
- CRM export service: `src/bitrix_ingest/application/crm/export_service.py`
- Call records scan service: `src/bitrix_ingest/application/call_records/scan_service.py`
- WhatsApp export service: `src/bitrix_ingest/application/whatsapp/export_service.py`
- HTTP client: `src/bitrix_ingest/infrastructure/http/client.py`
- Retry policy: `src/bitrix_ingest/infrastructure/http/retry_policy.py`
- JSON writer: `src/bitrix_ingest/infrastructure/persistence/json_writer.py`

## Commands

Run commands from the repository root.

### Install

Runtime only:

```powershell
python -m pip install -e .
```

With dev dependencies:

```powershell
python -m pip install -e ".[dev]"
```

### Run tests

```powershell
pytest -q
```

### Run the FastAPI server

```powershell
uvicorn bitrix_ingest.api.app:app --reload
```

The server exposes Swagger UI at `http://localhost:8000/docs`.

Environment variables:

| Variable | Default | Description |
|---|---|---|
| `BITRIX_DB_PATH` | `data/app.db` | Path to the SQLite database file |

### Run exporters via installed console scripts

CRM export:

```powershell
bitrix-export-crm --webhook-base-url "https://your-portal.bitrix24.ru/rest/1/token/"
```

Call-record scan:

```powershell
bitrix-scan-call-records --webhook-base-url "https://your-portal.bitrix24.ru/rest/1/token/"
```

WhatsApp export:

```powershell
bitrix-export-whatsapp --webhook-base-url "https://your-portal.bitrix24.ru/rest/1/token/"
```

### Run without installation

If the package is not installed, set `PYTHONPATH=src` and run modules directly:

```powershell
$env:PYTHONPATH = "src"
python -m bitrix_ingest.cli.export_crm --webhook-base-url "https://your-portal.bitrix24.ru/rest/1/token/"
python -m bitrix_ingest.cli.scan_call_records --webhook-base-url "https://your-portal.bitrix24.ru/rest/1/token/"
python -m bitrix_ingest.cli.export_whatsapp --webhook-base-url "https://your-portal.bitrix24.ru/rest/1/token/"
```

## Behavioral Constraints to Preserve

These behaviors are intentional. If changing them, update tests and `MIGRATION.md`.

### 1. Call-record scan is intentionally single-page

In `application/call_records/scan_service.py`, the activity fetch uses one `crm.activity.list` call and then slices to `limit`.

Implication:
- effective ceiling is Bitrix's first page size, typically 50 rows
- this matches the original PowerShell behavior

If you intentionally change this to full pagination, treat it as a behavior change, not a refactor.

### 2. WhatsApp `--limit` is client-side after full deal fetch

The WhatsApp exporter:

1. fetches all matching deals via pagination
2. filters for WhatsApp deals
3. sorts by `DATE_MODIFY DESC`
4. applies `limit`

This is deliberate PS parity.

### 3. WhatsApp deal detection rules are local and explicit

A deal is considered WhatsApp when:

- `SOURCE_ID` starts with `WZ`, or
- `TITLE` contains `WhatsApp` case-insensitively

The logic lives in `application/whatsapp/deal_filter.py`.

### 4. `voximplant.raw.json` stores the full API response

Do not silently flatten it unless the user explicitly wants a contract change.

### 5. Business profile is a singleton row in SQLite

`GET /api/app-state` returns `{"setup": {"business_profile": {...}, "integrations": []}}`.
`POST /api/setup-profile` upserts the single row (id=1, enforced by `CHECK (id = 1)`).

The response shape of these endpoints is the contract the frontend (`ai-auditor-front-main`) depends on. Do not rename keys without updating the frontend.

DB path is configurable via `BITRIX_DB_PATH` env var (default `data/app.db`).

### 6. JSON output formatting is stable

- UTF-8
- no BOM
- indent = 2

## Testing Expectations

When changing behavior:

1. update or add tests under `tests/`
2. run `pytest -q`
3. mention any preserved or intentionally changed output contract

Current tests are written against the new layered architecture, not against deleted compatibility wrappers.

## Coding Guidance

- Use ASCII unless the file already requires Unicode
- Keep comments sparse and high-signal
- Prefer explicit dataclasses and small collaborators over large god objects
- Add new cross-cutting abstractions only when there is a real second use case
- Keep filenames descriptive and aligned with one responsibility

## Repository Hygiene

These paths are local/runtime artifacts and should usually stay untracked:

- `export/`
- `data/`          ← SQLite database (`data/app.db`)
- `.pytest_cache/`
- `__pycache__/`
- `.claude/`
- `.deepeval/`
- `*.egg-info/`

See `.gitignore`.

## If You Need to Extend the Project

### Add a new export

Recommended shape:

1. Add domain entities if needed
2. Add an application service and request DTO
3. Reuse `BitrixGateway` and `JsonSink` ports where possible
4. Add a CLI module in `src/bitrix_ingest/cli/`
5. Register a script in `pyproject.toml`
6. Add tests

### Change HTTP behavior

Work in:

- `infrastructure/http/transport.py`
- `infrastructure/http/retry_policy.py`
- `infrastructure/http/client.py`
- `infrastructure/http/paginator.py`

Be careful: HTTP behavior affects all exporters.

### Add a new HTTP endpoint

1. Add the route in `src/bitrix_ingest/api/app.py` under the relevant tag
2. Keep the handler thin — delegate to an application service
3. Use `Security(_webhook_header)` / `Security(_openai_key_header)` for credentials
4. Return `{"status": "ok", "data": ...}` for consistency with existing endpoints

### Add persistent settings (SQLite)

1. Add a domain entity in `domain/` (pure dataclass, `to_dict()` / `from_dict()`)
2. Add a repository in `infrastructure/database/` using stdlib `sqlite3`
3. Wire the repository in `api/app.py` via a factory function (see `_profile_repo()` pattern)
4. Do **not** add SQLAlchemy or other ORM — keep it stdlib for now

### Change output schemas

Treat output filenames and JSON shapes as external contracts. Update:

- domain entity `to_dict()` methods when applicable
- service logic
- tests
- `MIGRATION.md`

## Agent Checklist Before Finishing

- Did you keep code in the correct layer?
- Did you avoid reintroducing root-level shims?
- Did you preserve documented behavioral quirks unless intentionally changing them?
- Did you update tests if behavior changed?
- Did you run `pytest -q` if code changed?

