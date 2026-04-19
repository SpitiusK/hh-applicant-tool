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

### Approval loop (Block 2 rework, 2026-04-19)

Two new SQLite tables queue the sync↔async boundary between operations and the Telegram bot:

- `pending_messages` (`storage/queries/migrations/20260419_pending_messages.sql`): every draft action the AI wants to take. Columns cover routing (`messenger_type`, `messenger_message_id`), payload (`action_type ∈ {apply_vacancy, reply_employer, form_field}`, `draft_payload JSON`, `draft_history JSON`), state (`status ∈ {pending, approved, modified, rejected, dispatched, error}`, `iterations`), escalation metadata (`confidence`, `escalation_reason`, `question_for_user`, `context_summary`). Sole communication channel between cron-ops and the long-running bot — no IPC.
- `ai_decisions` (`20260419_ai_decisions.sql`): audit log for every AI call. Critical field `is_sentinel` (mirrors `AIResponse.is_sentinel`) — tells deliberate escalation apart from a technical sentinel fallback, so the calibration signal stays clean. `operations/query.py` exposes `--ai-stats`, `--escalation-rate [DAYS]`, `--sentinel-rate [DAYS]`, `--flagged`.

Approval policy lives in `src/hh_applicant_tool/approval.py`:

- `should_escalate(ai_response, action_type, approval_cfg)` — single decision point. `mode=never` short-circuits to False, `mode=always` to True; `on_escalation` (default) fires on any of `escalate=True`, `confidence < threshold`, `action_type in always_escalate_actions`.
- `persist_ai_decision(...)` writes an `ai_decisions` row; on `status='auto_dispatched'` it additionally rolls sanity sampling (`messaging/sanity.py`) and posts a retrospective summary to the user via MessengerClient.
- `escalate_to_user(...)` creates a `pending_messages(status='pending')`, sends the inline-button message through MessengerClient, and stores `messenger_message_id` for later correlation.
- `generate_with_self_assessment(ai_client, prompt)` wraps any AI call in `AIResponse`. Falls back to `complete()` when the backend raises `NotImplementedError` (keeps `ChatOpenAI` working until its `complete_json` is filled in).

CLI flag `--approval-mode={never|on_escalation|always}` on `apply-vacancies` and `reply-employers`. Default resolves from `config["approval"]` overlaid on `_APPROVAL_DEFAULTS` (`tool.get_approval_defaults()` in `main.py`). Defaults: `mode=on_escalation`, `confidence_threshold=0.7`, `always_escalate_actions=[]`, `max_iterations=3`, `sanity_frequency=20`.

Messaging is a pluggable abstraction (`src/hh_applicant_tool/messaging/`):

- `base.py` — `MessengerClient` ABC + `ApprovalRequest` / `IncomingCommand` dataclasses.
- `telegram_client.py` — aiogram 3 implementation. One module-level long-lived event loop + single `Bot` instance, reused across every sync `send_*` call via `asyncio.run_coroutine_threadsafe` (no `asyncio.run()` per message — critical for the `apply-vacancies` hot loop). Long-running bot Dispatcher lives on a separate `asyncio.run()` inside `operations/run_messenger_bot.py`. Handlers: approve/reject (`update_status`), modify (FSM — awaits follow-up text, calls `messaging/modify_handler.py::handle_modify` via `asyncio.to_thread` so the 10–60 s `claude -p` call doesn't block the aiogram loop), commands `/start /stats /pending /events /skipped /sanity /flag`.
- `factory.py` — `get_messenger_client(config, storage_facade)` dispatched by `config["messaging"]["backend"]`. `aiogram` import is lazy so environments without Telegram don't pay for it.

New ops:

- `operations/send_approved.py` (cron) — drains `pending_messages` with `status='approved'` into real HH API calls (`POST /negotiations`, `POST /messages`). Failures go to `status='error'` and notify the user. `--dry-run` for inspection. `form_field` is intentionally a stub here (lands in Block 3 П.21).
- `operations/run_messenger_bot.py` — long-running bot service. Shipped as its own container in `docker-compose.yml` (`messenger-bot`); user adds `network_mode: "service:wg-amnezia"` in `docker-compose.override.yml` for Telegram access from RF.

New deps in `pyproject.toml`: `aiogram ^3.4`, `asgiref ^3.7` (explicit, not through extras).

Config:
```json
"messaging": {"backend": "telegram", "telegram": {"bot_token": "…", "chat_id": 123, "allowed_user_id": 123}}
"approval": {"mode": "on_escalation", "confidence_threshold": 0.7, "always_escalate_actions": [], "max_iterations": 3, "sanity_frequency": 20}
```
Old `form_user_data` / `claude` sections unchanged. `excluded_*` sections untouched by this block (parallel session owns them).

`config/config.json` with the bot token stays in `.gitignore` (pattern already in place since the OAuth token).

### Persona + event detector + calendar (Block 3 rework, 2026-04-19)

Three new pieces round out the agent: a static persona injected into every generation prompt, an event detector writing to a dedicated table, and a calendar exporter.

**Persona** — `operations/generate_persona.py` reads the sibling `okami-reports` repo through `claude -p` with Glob/Read/Grep and writes `<CONFIG_DIR>/<profile>/persona.md` (3–5 KB markdown: Role / Skills / Achievements / Tone & voice / Domain context). Manual op, not cron. `config/persona.example.md` is the template. `ai/context.py::get_persona_context(config, config_dir)` reads the file (empty string on miss, logged warning — agent keeps working before the persona file exists). `ai/prompts.py::build_system_prompt(base_rules, persona)` concatenates them for reply/cover-letter system prompts; `ReplyAgent` and `apply-vacancies` cover-letter path both pull it, `_pick_test_solution_id` intentionally does not (would bias id picks). `docker-compose.yml` has a commented volume for `../okami-reports:ro`.

**Event detector** — `operations/watch_events.py` with `--stage {state, messages, tasks, all}`:
- **22a `state`** (SQL-only, no AI): diffs `negotiation.state` from hh.ru against the local `negotiations` table. Transitions + brand-new non-"response" negotiations emit `events(type='negotiation_state_changed')`. Reliable signal — most interview invitations already ride on state.
- **22b `messages`** (AI): reads unseen employer chat messages (cursor per negotiation lives in `settings` under `watch_events_last_msg:{neg_id}`). `EventClassification(AIResponse)` with fields `is_event`, `event_type ∈ {interview, offer, deadline}`, `when_iso`, `title`, `notes`. Confident → `events.create(type=event_type, status='detected')`; low-confidence / escalate → `pending_messages(action_type='event_detect')` + MessengerClient approval. Every call writes to `ai_decisions(operation='event_detect')` with sanity sampling (p15).
- **22c `tasks`** (AI, second pass over the same messages with a dedicated prompt): `TaskClassification(AIResponse)` with `is_task`, `task_description`, `deadline_iso`, `difficulty_estimate ∈ {small, medium, large}`. Separate cursor `watch_events_last_task:{neg_id}`. Writes `events(type='task', when_ts=parsed deadline)`. Stages are separate because a single combined prompt hurts recall on both.

**Events table** (`20260419_events.sql`): `type ∈ {negotiation_state_changed, interview, offer, task, deadline}`, `title`, `when_ts` (nullable — not every event has a concrete time), `source_msg_id`, `raw_text`, `confidence`, `status ∈ {detected, confirmed, rejected, done}`.

**Calendar export** — `operations/export_events.py` (aliases `export`). Drains `events(status='confirmed')` into either `.ics` (hand-rolled RFC-5545 subset, DTSTART/DTEND+1h, CATEGORIES, proper escape of `;`, `,`, `\`, newline) or append-only `data/agenda.md`. Format + output picked from `config.events.calendar.{exporter, output_path}` with CLI override. Hourly `crontab` entry is provided but commented until end-to-end verification.

**Form-filler rewired** — `forms/filler.py` now escalates form reviews through `pending_messages(action_type='form_field')` + MessengerClient. ANY failure in the messaging path (no storage, no messenger, send_approval_request crash) falls back to the legacy `review_queue.jsonl` via `append_confirmation` — forms are never lost. `forms/journal.py` docstring reclassifies `append_confirmation` as failover-only.

**Config finalisation** — `HHApplicantTool` gains `get_persona_cfg()`, `get_events_cfg()` (with deep-merge of the nested `calendar` sub-dict), `get_messaging_cfg()` (same for `telegram` sub-dict), alongside the pre-existing `get_approval_defaults()`. Absent sections → pure defaults, no surprises. `excluded_*` sections are deliberately untouched (parallel session owns them).

**Docs** — `docs/agent_flow.md` has three mermaid `flowchart TD` diagrams covering apply / reply+form / watch+export flows, a config-sections summary, env-var list, and the new CLI commands. README has a short "Agent flow" section pointing at the doc.

**New CLI this block**: `generate-persona` (`persona-gen`), `watch-events` (`events-watch`), `export-events` (`export`). Added alongside block-2 ops (`send-approved` / `run-messenger-bot`).

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
