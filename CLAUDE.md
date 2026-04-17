# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

CLI tool (`hh-applicant-tool`) that automates job-hunting on hh.ru (HeadHunter). Authenticates as an Android app, auto-applies to vacancies matching filters, refreshes resumes, replies to employer chats, and can fill external questionnaire links. Runs primarily via cron inside Docker for 24/7 operation.

## Commands

```bash
# Install (editable)
pip install -e '.[playwright,pillow]'

# Lint (ignore deprecated top-level warning; new rules live under [tool.ruff.lint])
ruff check src/

# Run a single operation locally
python -m hh_applicant_tool <operation> [flags]

# Help on available operations
python -m hh_applicant_tool --help

# Inside Docker: everything runs as user `docker` against /app/config
docker compose up -d
docker exec -u docker hh_applicant_tool python -m hh_applicant_tool <op> [flags]

# Build (heavy — playwright install-deps chromium needs ≥6 GB Docker memory)
docker compose build
```

Tests: `pyproject.toml` declares pytest, but top-level `test_script.py` is broken (passes args to `HHApplicantTool.__init__`). Don't rely on `pytest` to gate changes — use `ruff` + manual smoke tests.

## Architecture

### Operation dispatch pattern

`HHApplicantTool` (in `src/hh_applicant_tool/main.py`) auto-discovers every module under `src/hh_applicant_tool/operations/` at startup and wires each as a subcommand. An operation module must expose:

- `class Operation(BaseOperation)` with `setup_parser(parser)` and `run(tool, args)`
- `class Namespace(BaseNamespace)` declaring all CLI args as typed attributes
- Optional `__aliases__` tuple for short command names (e.g. `apply`, `reall`)

To add a new command: create `operations/<name>.py` with these two classes. No registration needed.

### Config & profiles

Config is a JSONC file at `CONFIG_DIR/config.json` (default `~/.config/hh-applicant-tool/`, overridable via `-c`). `HHApplicantTool.config` is a lazy `utils.Config` dict-like wrapper. Profiles are sibling directories selected via `--profile-id` / `HH_PROFILE_ID`. The config holds:

- `token` — OAuth refresh/access token (auto-refreshed on any request)
- `openai_cover_letter` / `openai_vacancy_filter` / `openai_captcha` — per-purpose OpenAI-compatible API config (each section needs `api_key`, `base_url`, `model`)
- `claude` — Claude CLI backend config (`model`, `timeout`, `rate_limit`)
- `form_user_data` — candidate profile injected into reply/form-filler prompts
- `openai_session` is shared across all AI clients for connection reuse

### AI self-assessment contract (Block 1 rework, 2026-04-17)

Structured AI outputs flow through `ai/schema.py::AIResponse` (pydantic v2) — fields `answer / confidence / escalate / escalation_reason / question_for_user / context_summary / is_sentinel`. Callers use `ChatClaude.complete_json(prompt, response_model=AIResponse)`, which on `ClaudeError / JSONDecodeError / ValidationError` returns a sentinel `AIResponse(is_sentinel=True, escalate=True, escalation_reason="ai_unclear")` instead of raising. `is_sentinel` distinguishes a technical fallback from a deliberate model escalation — logged separately for prompt calibration. `ChatOpenAI.complete_json` is a `NotImplementedError` stub today; OpenAI backend does not use self-assessment path yet.

`apply-vacancies` behaviour changes:
- `_solve_vacancy_test`: fallback guessing removed (no more "middle option", no more default `answer="Да"`). AI branch goes through `complete_json(response_model=TestSolution)` with id-set validation and one retry before raising `_TestUnsolvable` → `_save_skipped_vacancy(reason="test_no_strategy")`.
- `_ask_ai_suitability` returns `bool | None`. `None` == AI backend failure or unparseable JSON after retries. New flag `--ai-filter-on-error={skip,pass}` (default `skip` — records `reason=ai_error`). Legacy "silently pass as suitable" is now opt-in.

Per-purpose temperature defaults in `main.py::_init_ai_client`: `vacancy_filter=0.0`, `cover_letter=0.4`, `reply=0.5`, `captcha=0.0`. User config `openai_<purpose>.temperature` still wins. `ChatClaude` ignores sampling params — `claude -p` is controlled by the subscription.

All `claude -p` invocations now route through `ChatClaude.complete()` / `.complete_json()` so the rate-limit lock applies. `forms/filler.py` previously spawned `subprocess` directly in filler/reviewer/submit stages — now uses per-stage `ChatClaude` instances (note: locks are per-stage, not global; fine for the sequential form pipeline).

New `skipped_vacancies.reason` codes introduced this block: `test_no_strategy`, `ai_error`. Existing: `excluded_filter`, `ai_rejected`.

### AI backends (`src/hh_applicant_tool/ai/`)

Two parallel `@dataclass` backends share the same `complete(message: str) -> str` interface (duck-typed, no ABC):

- `ChatOpenAI` (`openai.py`) — HTTP against any OpenAI-compatible endpoint. Has `solve_captcha()` using vision models. Rate-limited via lock.
- `ChatClaude` (`claude.py`) — wraps `claude -p` subprocess (user's subscription, not API key). Has `complete_json()` helper that strips ```json fences.

Instantiate via `tool.get_cover_letter_ai(prompt)`, `tool.get_cover_letter_claude(prompt)`, `tool.get_vacancy_filter_ai(prompt)`, `tool.get_captcha_ai()`. All share one `requests.Session`.

`AIError` is the common exception base (in `ai/base.py`) — catch this, not the concrete types.

### Storage (`src/hh_applicant_tool/storage/`)

SQLite via `StorageFacade` with repository-per-model layout. Migrations live in `storage/queries/migrations/` and apply automatically via `migrate-db` operation (also called at startup). Models: `employer`, `employer_site`, `contacts`, `negotiation`, `resume`, `skipped_vacancy`, `vacancy`, `setting`. Skipped vacancies (filtered out by regex or AI) are retained with reason for later SQL analysis (`query` / `sql` operation).

### API client (`src/hh_applicant_tool/api/`)

`ApiClient` (`client.py`) talks to `api.hh.ru` with `client_keys.py` spoofing the official Android app's credentials. Auto-refreshes tokens on 401. All operations get `tool.api_client`. Data types in `datatypes.py` are `TypedDict`s.

### Forms sub-system (`src/hh_applicant_tool/forms/`)

`FormFiller` handles external questionnaires (Google Forms etc.) detected in employer messages. Three-stage pipeline, each stage a separate `claude -p` subprocess:

1. **Filler agent** — navigates form via playwright-skill plugin, proposes answers as JSON, does NOT submit
2. **Reviewer agent** — validates proposed answers (AI identity leaks, confidential data, Q/A mismatch)
3. **Submit agent** — only runs after `approve` verdict

Escalated forms land in `data/review_queue.jsonl` (one JSON object per line). `ReviewVerdict` and `FormResult` live in `forms/reviewer.py`. URL detection + dispatch lives inside `operations/reply_employers.py::_process_form_urls` (matches domains in `_FORM_DOMAINS` tuple).

### Key operations

- `apply_vacancies.py` — biggest module (~1500 lines). Three regex excluded-filters can be combined: `--excluded-filter` (name+description, legacy), `--excluded-name-filter`, `--excluded-description-filter`. Regexes are pre-compiled once in `run()`. Full vacancy description is fetched via `_fetch_full_description()` only when snippet check misses. `_is_excluded()` returns on first match.
- `reply_employers.py` — iterates `/negotiations` pages, builds `message_history` per chat, feeds it to the AI with candidate data from `form_user_data`. `--use-ai` and `--use-claude` are mutually exclusive (argparse group). The default system prompt enforces first-person voice, forbids AI-identity reveals, and hard-codes the work-format / salary rules for *this specific user* — edit it if the tool is re-used.
- `authorize.py` — uses Playwright to drive hh.ru's OAuth flow (solves captcha via vision AI if configured).
- `apply-vacancies` internally calls `get_vacancy_filter_ai` (heavy/light modes) and `get_captcha_ai` — these require `openai_*` config sections even if only Claude is set up for chat replies.

### Docker deployment

`Dockerfile` installs playwright browsers + Node.js + `@anthropic-ai/claude-code`. `docker-compose.yml` mounts:

- `.:/app` — live source
- `${HOME}/.claude:/home/docker/.claude` + `${HOME}/.claude.json` — **propagates host's Claude CLI login + plugins (including `playwright@claude-plugins-official`) into the container**. No separate auth needed in Docker.

Cron (from `crontab`) runs: `update-resumes` every 5h, `apply-vacancies` hourly 5-18, `reply-employers --use-claude --fill-forms` hourly 9-20, `refresh-token` every minute. All have random 1-5 min sleep to look human-like. `startup.sh` runs token refresh + resume update on container boot.

## Gotchas

- **Git Bash on Windows mangles `/app/...` paths when calling docker exec.** Prefix with `MSYS_NO_PATHCONV=1` or use single-quoted paths.
- **`negotiation["resume"]` can be `None`** when the candidate deleted the resume; guard with `.get("resume")` before subscripting.
- **`claude -p` cold-starts the Node.js runtime per call (~10-30 s).** The filler→review→submit pipeline is therefore 30-90 s per form. Fine for async cron, not for interactive use.
- **ruff config uses the deprecated top-level `select`/`ignore` keys.** Warnings are harmless; don't "fix" without migrating the whole block under `[tool.ruff.lint]`.
- **Two AI backends, duck-typed.** If you add a third, make sure `complete(message: str) -> str` and exception type inherit `AIError`.
- **Don't commit `config/config.json`** — it contains the OAuth token. Already gitignored via pattern.
- README is in Russian and full of colourful commentary; treat feature descriptions there as authoritative, tone as optional.
