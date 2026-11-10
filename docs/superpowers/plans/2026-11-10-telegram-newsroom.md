# AGANT Telegram Newsroom Implementation Plan

**Goal:** Add a tested approval workflow to the existing Hermes Telegram bot.

**Architecture:** A native plugin registers scoped Telegram callbacks and reply
actions on the existing adapter. SQLite owns revisions and publication state;
the agent may prepare drafts but cannot call a publication tool.

**Tech stack:** Python 3.11, sqlite3, existing httpx and python-telegram-bot 22.8.

**Spec:** `docs/superpowers/specs/2026-11-10-telegram-newsroom-design.md`

## Global constraints

- Disabled unless `AGANT_NEWSROOM_ENABLED=true`; no live sends in development.
- Preserve the Hermes `v2026.9.24` pin and use no additional Telegram poller.
- Stable numeric identity, explicit group/channel binding and owner-only posting.
- Original X source/photo, attribution, and durable storage configuration.
- Ambiguous sends require reconciliation; no automatic publication retries.

## Review focus

- Stale callbacks after editing must never publish unseen text.
- Double clicks and a restart in mid-send must not cause a second post.
- A forged owner name, forwarded button or anonymous sender grants no authority.
- A source URL with tracking parameters must match the canonical existing draft.
- A failed preview edit must block publication until the preview is repaired.

## Task 1: Draft state and publication controller

Files: `plugins/agant-newsroom/core.py`, `tests/test_agant_newsroom.py`.

- [x] Add failing tests for persistence, canonical sources, actor checks, revision
  races, unknown outcomes and repair/reconciliation.
- [x] Implement config, caption validation, SQLite transitions, and an async
  controller accepting a small injected Telegram transport.
- [x] Run the focused tests, including concurrent publish attempts and restart.

## Task 2: Telegram and agent integration

Files: `plugins/agant-newsroom/__init__.py`, `telegram_ui.py`, `plugin.yaml`.

- [x] Add failing tests for HTTP payloads, scoped native registration, callback
  message binding, model-tool identity and owner-only reply publication.
- [x] Implement HTTP transport and register a pattern-scoped callback handler,
  exact reply actions and `/newsroom` status/reconciliation commands.
- [x] Register bounded create/get/list/revise/refresh agent actions. Preserve
  normal Hermes dispatch for ordinary revision instructions and other chats.

## Task 3: Runtime guidance, validation and review

Files: `HERMES.md`, `docs/AGANT_NEWSROOM.md`, `.env.example`.

- [x] Document configuration, durable storage, a private test group/channel,
  source checking, natural revision replies and uncertain-send reconciliation.
- [x] Run release validation, shell syntax, whitespace, ruff and the complete
  repository tests with Python 3.11. Check native registration against the pinned
  Hermes source; report any unavailable container/live tests separately.
- [ ] Review the branch and create a draft PR; keep production configuration and
  the deployed branch unchanged pending the user's setup of the test destination.

## Validation record

Python 3.11.18: 37 focused tests pass; the complete repository run reports
290 passed, 5 skipped, 119 subtests passed. Two warnings originate in existing
FastAPI/Starlette and JWT tests. Ruff, release validation, shell syntax and
dependency compatibility pass. The pinned Hermes checkout exposes all three
required public plugin registration methods. An independent review found no
remaining high or medium issues. Container build/CI and configured live Telegram
acceptance checks remain unverified locally.
