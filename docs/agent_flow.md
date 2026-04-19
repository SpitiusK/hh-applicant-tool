# Agent flow

Как работает hh-applicant-tool после рефакторинга sprint/agent-rework-2026-04-17.

Три потока — `apply-vacancies`, `reply-employers` и `watch-events` —
каждый принимает решение автономно или эскалирует человеку в Telegram.
Вся синхронизация между cron-сервисами и мессенджер-ботом идёт через
SQLite (`pending_messages` / `ai_decisions` / `events`).

## 1. apply-vacancies

```mermaid
flowchart TD
    A[cron: apply-vacancies] --> B[HH API: поиск вакансий]
    B --> C{regex excluded_name / excluded_description?}
    C -- match --> C1[skipped_vacancies reason=excluded_filter]
    C -- no --> D{ai_filter включен?}
    D -- yes --> E[vacancy_filter_ai: suitable?]
    E -- False --> E1[skipped_vacancies reason=ai_rejected]
    E -- None AIError --> E2{ai_filter_on_error}
    E2 -- skip --> E3[skipped_vacancies reason=ai_error]
    E2 -- pass --> F
    E -- True --> F
    D -- no --> F[generate_with_self_assessment: cover letter]
    F --> G{should_escalate approval_cfg}
    G -- autonomous --> H[persist_ai_decision auto_dispatched + sanity sampling]
    H --> I[POST /negotiations]
    I --> J[✅ отклик отправлен]
    G -- escalate --> K[escalate_to_user: pending_messages pending]
    K --> L[TG: Approve / Modify / Reject]
    L -- Approve --> M[pending_messages approved]
    L -- Modify --> N[handle_modify: regenerate]
    N -- confidence_ok --> M
    N -- still_unsure --> K
    N -- max_iterations --> Z[pending_messages rejected reason=user_rejected_max_iter]
    L -- Reject --> Z
    M --> S[cron: send-approved]
    S --> I
```

Ключевые таблицы:
- `pending_messages` — очередь действий, ждущих approval.
- `ai_decisions` — audit-лог каждого AI-решения (`status`, `confidence`,
  `is_sentinel`, `sample_for_review`, `flagged`).
- `skipped_vacancies` — отфильтрованные вакансии с причиной (`excluded_filter`,
  `ai_rejected`, `ai_error`, `test_no_strategy`, `user_rejected`,
  `user_rejected_max_iter`).

## 2. reply-employers + form-filler с failover в jsonl

```mermaid
flowchart TD
    A[cron: reply-employers] --> B[HH API: /negotiations]
    B --> C[для каждого чата: employer_message?]
    C -- no --> C0[skip]
    C -- yes --> D[ReplyAgent Claude или OpenAI cover_letter]
    D --> E[AIResponse: reply + confirmations?]
    E --> F{should_escalate}
    F -- autonomous --> G[persist_ai_decision + sanity sampling]
    G --> H[POST /negotiations id messages]
    F -- escalate --> K[pending_messages action=reply_employer]
    K --> L[TG Approve/Modify/Reject]
    L -- Approve --> M[send-approved → POST messages]
    L -- Modify --> N[handle_modify]
    L -- Reject --> Z[rejected]

    C -- contains_form_url --> P[FormFiller: filler → reviewer]
    P -- approve --> P1[submit agent]
    P -- escalate --> Q{messaging доступен?}
    Q -- yes --> R[pending_messages action=form_field]
    R --> L
    Q -- no/error --> RQ[failover: review_queue.jsonl]
```

FormFiller failover: при ЛЮБОЙ ошибке в messaging-пути (нет storage/factory,
`pending_messages.create` упал, `send_approval_request` упал, messenger=None)
форма пишется в `data/review_queue.jsonl` через старый `append_confirmation`,
чтобы не потерять данные.

## 3. watch-events (три стадии) → events → export_events

```mermaid
flowchart TD
    A[cron: watch-events] --> S1[stage=state: SQL diff]
    S1 --> S1A[HH API /negotiations]
    S1A --> S1B{state изменился?}
    S1B -- yes --> E1[events.create type=negotiation_state_changed]
    S1B -- no --> S1C[skip]
    S1A --> S2[stage=messages]
    S2 --> S2A[HH API /negotiations/id/messages]
    S2A --> S2B[фильтр новых employer-msg по settings.watch_events_last_msg]
    S2B --> S2C[ai.complete_json EventClassification]
    S2C --> S2D[persist_ai_decision event_detect + sanity]
    S2D -- is_event + escalate --> S2K[escalate_to_user event_detect]
    S2D -- is_event autonomous --> E2[events.create type=interview/offer/deadline]
    S2A --> S3[stage=tasks]
    S3 --> S3A[ai.complete_json TaskClassification]
    S3A --> S3B[persist_ai_decision event_detect + sanity]
    S3B -- is_task + escalate --> S3K[escalate_to_user event_detect]
    S3B -- is_task autonomous --> E3[events.create type=task]

    E1 --> X[events status=detected]
    E2 --> X
    E3 --> X
    S2K --> Y[TG Approve → events status=confirmed вручную]
    S3K --> Y
    X --> Y
    Y --> Z[cron: export-events]
    Z --> Z1[config.events.calendar.exporter]
    Z1 -- ics --> Z2[VCALENDAR → data/calendar.ics]
    Z1 -- markdown --> Z3[append data/agenda.md]
```

Параметры в `config.events`: `watch_interval_minutes` (дефолт 15),
`timezone` (дефолт Europe/Moscow), `calendar.exporter` (ics|markdown),
`calendar.output_path`.

## Config-секции (сводно)

| Секция | Default helper | Назначение |
|--------|----------------|------------|
| `approval` | `get_approval_defaults()` | mode (never/on_escalation/always), confidence_threshold (0.7), always_escalate_actions, max_iterations (3), sanity_frequency (20) |
| `messaging` | `get_messaging_cfg()` | backend (telegram), telegram: bot_token/chat_id/allowed_user_id |
| `persona` | `get_persona_cfg()` | path, source_reports_dir |
| `events` | `get_events_cfg()` | watch_interval_minutes, timezone, calendar: exporter, output_path |

## Переменные окружения

- `HH_PROFILE_ID` — переключение профиля (для dev / prod изоляции).
- `CONFIG_DIR` — путь до config-каталога (дефолт `/app/config` в Docker).
- `OPENAI_PROXY` / `HH_PROXY` — прокси для сетевых вызовов.

## CLI-команды, появившиеся в этом рефакторинге

- `generate-persona --source /app/okami-reports` — разовая генерация persona.md.
- `run-messenger-bot` (alias `messenger-bot`, `bot`) — long-running Telegram-бот.
- `send-approved` (alias `dispatch`) — диспатч approved pending_messages в hh.ru.
- `watch-events --stage {state,messages,tasks,all}` — детектор событий.
- `export-events --format {ics,markdown}` — экспорт confirmed-events.
- `migrate-db` автоматически применяет новые миграции (`pending_messages`,
  `ai_decisions`, `events`).
