# Nexa — Executive AI Coach

Nexa is a Flask-based, AI-powered executive leadership coaching web application built for the [We-Ace](https://we-ace.com) platform. Authenticated users chat with an AI coach that personalises its guidance from their professional profile and past sessions, while corporate and platform admins get an analytics dashboard powered by an automated psycholinguistic **sentiment analysis** pipeline.

---

## Table of Contents

1. [Feature Overview](#feature-overview)
2. [Tech Stack](#tech-stack)
3. [Architecture](#architecture)
4. [Project Structure](#project-structure)
5. [Authentication & Sessions](#authentication--sessions)
6. [Roles & Access Control](#roles--access-control)
7. [AI Coaching Engine](#ai-coaching-engine)
8. [Suggestion Bubbles](#suggestion-bubbles-follow-up-nudges)
9. [Sentiment Analysis Pipeline](#sentiment-analysis-pipeline)
10. [Database Schema](#database-schema)
11. [HTTP API Reference](#http-api-reference)
12. [Background Jobs & Scheduler](#background-jobs--scheduler)
13. [Configuration](#configuration)
14. [Local Development](#local-development)
15. [Deployment](#deployment)
16. [Utility Scripts](#utility-scripts)

---

## Feature Overview

- **Conversational AI coaching** — a warm, ICF-aligned executive coach persona backed by OpenAI or Anthropic Claude (provider-swappable via env var).
- **Personalised prompts** — each session is seeded with the user's profile (role, industry, seniority, functional background) and concise highlights from their previous sessions.
- **Suggestion bubbles** — the coach appends tappable follow-up prompts under each reply so the user can continue the conversation with one click.
- **Persistent chat history** — messages are stored in MySQL and recent history is replayed on reload.
- **Sentiment dashboard** — corporate admins see org-level wellbeing analytics across 8 psychological dimensions, with score bands, trends, and LLM-generated insights.
- **Admin console** — platform (`weace_super_admin`) admins manage per-user Nexa access and expiry dates across all organisations.
- **Access control & expiry** — Nexa access is a per-user flag with a rolling access window; inactive users are auto-disabled by a scheduled job.
- **We-Ace SSO integration** — authentication is delegated to the We-Ace platform auth API; the app never stores passwords.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.12, Flask 3 |
| WSGI server | Gunicorn (2 workers, 120s timeout) |
| Database | MySQL (via PyMySQL, `DictCursor`) |
| AI providers | OpenAI (`gpt-4o` default) or Anthropic Claude (`claude-sonnet-4-6` default) |
| Scheduling | APScheduler (background cron jobs) |
| Frontend | Vanilla JS, Jinja2 templates, `marked` for Markdown rendering |
| Auth source | We-Ace platform API (`WEACE_API_URL`) |
| Packaging / deploy | Docker, AWS ECR, Makefile |

There is **no build step** for the frontend — it is served as static assets plus server-rendered Jinja2 templates.

---

## Architecture

```
                    ┌──────────────────────────────────────────────┐
                    │                  Browser                      │
                    │  index.html + script.js  (chat UI)            │
                    │  dashboard.html          (sentiment analytics)│
                    │  admin.html              (user management)    │
                    └───────────────┬──────────────────────────────┘
                                    │  Bearer <we-ace access_token>
                                    ▼
        ┌───────────────────────────────────────────────────────────────┐
        │                        Flask app (app.py)                      │
        │                                                                │
        │  @require_weace_token ──► verifies token against We-Ace API    │
        │                                                                │
        │  /session   build session, load profile + highlights          │
        │  /chat      call LLM, parse suggestions, persist messages      │
        │  /api/*     analytics, sentiment, admin, cohorts               │
        │                                                                │
        │  sessions_cache{}  in-memory conversation history per session  │
        │  auth_tokens{}     in-memory user state keyed by access_token  │
        └──────┬───────────────────────┬──────────────────────┬─────────┘
               │                        │                      │
               ▼                        ▼                      ▼
      ┌─────────────────┐    ┌──────────────────┐   ┌────────────────────┐
      │ AI Provider      │    │  MySQL (database │   │  We-Ace Platform   │
      │ OpenAI / Claude  │    │  .py)            │   │  API               │
      │ (ai_coach.py     │    │  sessions,       │   │  auth / profile /  │
      │  prompt.py)      │    │  messages,       │   │  cohorts           │
      └─────────────────┘    │  sentiment, etc. │   └────────────────────┘
                             └────────┬─────────┘
                                      ▲
                    APScheduler       │  daily writes
              ┌───────────────────────┴────────────────┐
              │  sentiment_job.py (02:00 UTC)           │
              │  access expiry sweep (03:00 UTC)        │
              └─────────────────────────────────────────┘
```

**Key runtime state (in-memory, per process):**

- `sessions_cache: dict[session_uuid → list[message]]` — the full conversation history (including the system prompt) for each active chat session. Rebuilt from the DB on cache miss via `_get_or_rebuild_history`.
- `auth_tokens: dict[access_token → user_state]` — cached user identity, role, and Nexa access info, populated at `/session`. Stale entries for a user are evicted on re-login (token rotation).

> **Note:** Because state is in-memory, a multi-worker/multi-host deployment does not share caches. On a cache miss the history is transparently rebuilt from MySQL, so correctness is preserved; only the system-prompt personalisation is re-derived.

---

## Project Structure

```
.
├── app.py                  # Flask app: routes, auth decorator, session & chat logic
├── ai_coach.py             # Standalone CLI coach + exports SYSTEM_INSTRUCTION
├── prompt.py               # The coaching system prompt (persona + suggestion format)
├── database.py             # All MySQL access: schema init, CRUD, analytics queries
├── sentiment_job.py        # LLM-based psycholinguistic sentiment analysis pipeline
│
├── templates/
│   ├── index.html          # Chat UI (login overlay + chat + input)
│   ├── dashboard.html      # Corporate sentiment / cohort analytics dashboard
│   └── admin.html          # Platform admin user-management console
├── static/
│   ├── script.js           # Frontend: auth flow, chat, suggestion bubbles
│   ├── style.css           # Global styles (CSS variables, glass-morphism)
│   ├── we-ace-logo.png
│   └── favicon-wit.png
│
├── requirements.txt        # Python dependencies
├── Dockerfile              # Production image (gunicorn)
├── Makefile                # build / tag / push / deploy to AWS ECR
├── VERSION                 # Auto-incremented semver (patch bump on build)
│
├── migrate_sentiment.py    # One-off sentiment schema migration helper
├── reset_password.py       # Admin utility: reset a user's password
├── delete_cognito_users.py # Admin utility: bulk-delete Cognito users
├── signout_cognito_user.py # Admin utility: force a Cognito global sign-out
└── custom_email.py         # Email helper (gitignored)
```

---

## Authentication & Sessions

Nexa **does not manage passwords**. Authentication is delegated to the We-Ace platform API. The browser holds We-Ace `accessToken` / `refreshToken` values in `localStorage`, and every protected backend call is verified server-side.

### The `@require_weace_token` decorator

Every protected route is wrapped by `require_weace_token` ([app.py](app.py)). It:

1. Reads the `Authorization: Bearer <token>` header.
2. Verifies the token by calling `GET {WEACE_API_URL}/api/v1/users/profile`.
3. On success, populates Flask's `g`:
   - `g.access_token` — the verified token
   - `g.token_data` — raw profile JSON
   - `g.user` — normalised `{ user_id, user_name, email, role, first_name, last_name, org_id }`
4. Returns `401` if missing/invalid, `502` if the verify service is unreachable.

### Frontend auth flow (`static/script.js`)

The client attempts authentication in priority order on page load:

1. **`?refresh_token=` in URL** — exchanged for a fresh token pair via `/api/v1/auth/refresh`, then the URL is scrubbed with `history.replaceState`.
2. **`?access_token=` in URL** — external platform redirect (e.g. deep link from the We-Ace app).
3. **Stored refresh token** in `localStorage` — primary persistent auth; refreshed on each load.
4. **Stored access token** fallback — used on plain page reloads.
5. Falls back to the **login form**, which authenticates directly against the We-Ace auth API.

After obtaining tokens, the client calls **`POST /session`** to establish the backend session.

### `/session` — session bootstrap

`create_session` ([app.py](app.py)) does the heavy lifting:

- Fetches `/api/v1/users/profile` and `/api/v1/personal-info/{id}` from the We-Ace API to assemble a full profile (role, company, seniority, functional areas, industry, gender, country, cohort).
- Builds a `profile_context` dict used for prompt personalisation.
- Creates a DB chat session (`create_chat_session`) with a fresh `session_uuid`.
- Seeds `sessions_cache[session_uuid]` with a personalised system prompt.
- Upserts the user into `user_settings` (`upsert_user_login`), enabling `nexa_access` and setting a rolling 7-day `access_last_date`.
- Also populates the **Flask session** (cookie) so the server-rendered `/dashboard` and `/admin` pages can authorise without a Bearer token.
- Returns everything the UI needs, including `recent_messages` (last 15) for history replay and `remaining_days` for the expiry banner.

> There are effectively **two auth surfaces**: Bearer-token auth (JSON APIs, `g.user`) and Flask-session cookie auth (server-rendered admin pages, `session[...]`). Both are established in `/session`.

---

## Roles & Access Control

Roles come from the We-Ace profile as an **array of role objects** (`[{slug: "..."}]`), with a legacy string form also supported. The helper `_has_role(role_val, *slugs)` normalises both.

| Role slug | Capabilities |
|-----------|--------------|
| `user` | Chat with the coach (subject to Nexa access). |
| `corporate_super_admin` | Everything a user can, plus the **Sentiment Dashboard** (`/dashboard`) and analytics/sentiment APIs scoped to their org. |
| `weace_super_admin` | Platform-wide **Admin console** (`/admin`); manage Nexa access & expiry for all users across all orgs. |

**Nexa access rules:**

- Super admins (`weace_super_admin`, `corporate_super_admin`) always have access.
- Regular users are gated by the `nexa_access` flag and `access_last_date` window in `user_settings`.
- The UI shows an expiry banner when `remaining_days <= 7` and blocks input when access is expired.
- A daily job disables users inactive for 30+ days.

---

## AI Coaching Engine

### System prompt (`prompt.py`)

`PROMPT` defines the coach persona: an elite, ICF-aligned leadership coach that is warm, direct, concise, solution-oriented, asks one question at a time, gently redirects off-topic/controversial/taboo/casual digressions back to professional goals, and does not disclose the underlying model vendor.

### Provider abstraction (`app.py`)

The provider is selected by the `AI_PROVIDER` env var (`openai` or `claude`) at startup:

```python
AI_PROVIDER = os.environ.get("AI_PROVIDER", "openai").lower()
DEFAULT_MODELS = {"openai": "gpt-4o", "claude": "claude-sonnet-4-6"}
AI_MODEL = os.environ.get("AI_MODEL", DEFAULT_MODELS.get(AI_PROVIDER, "gpt-4o"))
```

- **Claude**: system prompt passed via the `system=` parameter; message list excludes the system role; `max_tokens=1024`.
- **OpenAI**: full message list (including system) passed to `chat.completions.create`; `temperature=0.7`.

### Prompt personalisation (`_build_personalized_prompt`)

Before the first turn, the base prompt is augmented with:

1. **User profile block** — current role/company, experience level, gender, country, functional background, and industry context (only fields present are included).
2. **Past coaching highlights** — for returning users, a compact summary of up to 5 recent sessions (opening topic + closing takeaway per session) so the coach can build on prior themes without replaying raw transcripts.

The coach is instructed to use this context silently and not read it back to the user unless asked.

### Chat request lifecycle (`POST /chat`)

1. `_get_or_rebuild_history(session_uuid, ...)` returns the cached history or rebuilds it from DB highlights + profile context.
2. The user message is appended and persisted (`save_message`).
3. The LLM is called (provider-appropriate branch).
4. `_extract_suggestions(reply)` strips any suggestion block (see below).
5. The **cleaned** reply is appended to history and persisted (so stored history never contains suggestion markup).
6. Response: `{ "response": <clean text>, "suggestions": [...] }`.
7. On error, the just-appended user turn is rolled back from the in-memory history.

---

## Suggestion Bubbles (Follow-up Nudges)

To help users keep the conversation flowing, the coach can append **2–3 tappable follow-up suggestions** at the end of a reply. These render as pill-shaped chips beneath the assistant message; tapping one sends it as the user's next message.

**How it works end-to-end:**

1. **Prompt** (`prompt.py`) — the coach is instructed to append a block, phrased from the user's point of view, only when genuinely useful:
   ```
   [[SUGGESTIONS]]
   - First suggestion
   - Second suggestion
   - Third suggestion
   [[/SUGGESTIONS]]
   ```
2. **Backend parse** (`_extract_suggestions` in `app.py`) — a tolerant regex (`_SUGGESTIONS_RE`, whitespace/case-insensitive) extracts the block, strips it from the displayed text, and returns up to 3 cleaned bullet strings. The clean text (not the markup) is what gets saved to the DB and conversation history.
3. **API** — `/chat` returns `suggestions: []` (empty when the model omits the block — fully backward compatible).
4. **Frontend** (`static/script.js`) — `renderSuggestions()` draws the chips after the assistant reply; `sendMessage()` submits the chosen chip; `clearSuggestions()` removes them when a new message is sent. Chips are suppressed when input is disabled (expired access).
5. **Styling** (`static/style.css`) — `.suggestion-row` / `.suggestion-chip`, aligned under the assistant bubble, matching the glass-morphism design system.

> Because the suggestions are parsed out of model text, they depend on the model following the prompt format. The parser is lenient, but a structured/tool-use output would give a stronger guarantee if needed.

---

## Sentiment Analysis Pipeline

`sentiment_job.py` performs automated psycholinguistic analysis of user messages to surface organisational wellbeing signals. It scores **8 dimensions** (0–100):

| Dimension | Type | 0 → 100 meaning |
|-----------|------|-----------------|
| `work_life_balance` | Positive | imbalanced → healthy boundaries |
| `job_satisfaction` | Positive | dissatisfied → fulfilled |
| `stress_anxiety` | Negative | calm → extreme stress |
| `self_confidence` | Positive | self-doubting → assertive |
| `empathy` | Positive | none → high empathy |
| `frustration_disengagement` | Negative | none → cynical/disengaged |
| `growth_mindset` | Positive | fixed → growth framing |
| `psychological_safety` | Positive | closed → open about failures |

### Two-tier analysis (`analyze_org_sentiment`)

Runs **incrementally** per organisation using a cursor (`sentiment_analysis_cursor.last_message_id`) so each run only processes messages newer than the last:

1. **Org-level pass** — all new user messages (capped at ~40k chars) are sent to the LLM to generate a `score` + one-sentence `insight` per dimension. Saved to `org_sentiment` (JSON).
2. **Per-user pass** — for each user with new messages (sampled down to `_MAX_USERS = 30` on large orgs), a compact, cheap call scores just the 8 dimensions. Saved as rows in `sentiment_score`.

Score **bands** (`very_high` >75, `moderate` 51–75, `low` 26–50, `negligible` ≤25) are computed as the % distribution across scored users. The cursor is advanced only after a successful run.

### Dashboard aggregation (`get_org_sentiment_data`)

Powers `/api/sentiment`. Combines:
- **Latest per-(user, dimension) scores** → org average + band distribution.
- **Trend** → average score per dimension grouped by run date (last 12 points).
- **Insights** → the LLM sentences from `org_sentiment`.

Supports filtering by date range, gender, seniority level, and cohort. Analysts can also trigger an on-demand recompute via `POST /api/sentiment/refresh`.

---

## Database Schema

MySQL, initialised idempotently by `init_db()` in `database.py` (creates tables and back-fills columns for older deployments).

| Table | Purpose |
|-------|---------|
| `ai_coach_sessions` | One row per chat session (`session_id`, user, org, cohort, `started_at`). |
| `ai_coach_messages` | Every message (`role` = `user`/`assistant`, `content`, `created_at`). |
| `user_settings` | Per-user state: `nexa_access`, profile fields, `first_login`/`last_login`, `access_last_date`. |
| `sentiment` | The 8 sentiment dimensions (seeded on init). |
| `sentiment_score` | Per-user, per-dimension score rows with timestamps. |
| `org_sentiment` | Latest org-level LLM sentiment JSON (scores + insights). |
| `org_sentiment_history` | Snapshots of org dimension scores over time. |
| `sentiment_analysis_cursor` | Incremental-analysis cursor per org (`last_message_id`). |

**Notable relationships & conventions:**
- Messages join to sessions via `session_id`; both carry `user_id` for fast per-user queries.
- `functional_areas` / `industry_types` are stored as JSON text and parsed on read.
- `org_slug` (from `organizationId`) is the primary org grouping key for analytics.
- Every query is routed through `_execute`, which logs SQL + params at INFO level.

---

## HTTP API Reference

Unless noted, protected endpoints require `Authorization: Bearer <we-ace access_token>` and return JSON.

### Auth & session

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/` | — | Serves the chat SPA (`index.html`). |
| `GET` | `/auth` | — | Deep-link entry; exchanges `refresh_token` → token pair, then redirects. |
| `GET` | `/me` | Bearer | Returns current user summary (name, initials, role, access). |
| `POST` | `/session` | Bearer + body | Bootstraps the session; returns profile, history, access info. |
| `POST` | `/new-session` | Bearer | Starts a fresh chat session, re-seeding highlights. |
| `POST` | `/logout` | Bearer (optional) | Clears in-memory + Flask session state. |

### Chat

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/chat` | Bearer | Body `{ message, session_uuid, profile_context, ... }`. Returns `{ response, suggestions }`. |

### Corporate dashboard (`corporate_super_admin` / `weace_super_admin`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/dashboard` | Server-rendered sentiment/analytics page (Flask-session auth). |
| `GET` | `/api/analytics` | Org usage stats + per-user activity. Filters: date, gender, level, cohort, industry. |
| `GET` | `/api/filters` | Distinct filter options (genders, levels, industries) for the org. |
| `GET` | `/api/sentiment` | Aggregated sentiment scores, bands, trends, insights. |
| `POST` | `/api/sentiment/refresh` | Trigger an on-demand sentiment recompute for the org. |
| `GET` | `/api/cohorts` | Proxies the We-Ace cohort list for the org. |

### Platform admin (`weace_super_admin`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin` | Server-rendered admin console (Flask-session auth). |
| `GET` | `/api/admin/users` | All users across all orgs with stats + access. |
| `PUT` | `/api/admin/users/<user_id>/nexa_access` | Enable/disable a user's Nexa access. |
| `PUT` | `/api/admin/users/<user_id>/access_till` | Set a user's access expiry date. |

**Common status codes:** `400` bad input, `401` missing/invalid token or session, `403` insufficient role, `500` server error, `502` upstream (We-Ace API) unreachable.

---

## Background Jobs & Scheduler

APScheduler starts with the app (unless `ENABLE_SCHEDULER=false`) and, in Flask debug mode, only in the reloaded child process (guarded by `WERKZEUG_RUN_MAIN`):

| Job | Schedule (UTC) | Action |
|-----|----------------|--------|
| `sentiment_daily` | 02:00 | `run_sentiment_job()` — analyse all orgs incrementally. |
| `nexa_access_expiry` | 03:00 | `disable_inactive_users(30)` — disable users inactive 30+ days. |

The sentiment job can also be run standalone: `python sentiment_job.py`.

---

## Configuration

All configuration is via environment variables (loaded from `.env` in development through `python-dotenv`).

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_PROVIDER` | `openai` | `openai` or `claude`. |
| `AI_MODEL` | provider default | Override model (`gpt-4o` / `claude-sonnet-4-6`). |
| `OPENAI_API_KEY` | — | Required when `AI_PROVIDER=openai`. |
| `CLAUDE_API_KEY` | — | Required when `AI_PROVIDER=claude`. |
| `WEACE_API_URL` | `https://api.we-ace.com` | Base URL of the We-Ace auth/profile API. |
| `FLASK_SECRET_KEY` | `dev-secret-change-in-prod` | **Set in production** — signs Flask session cookies. |
| `DB_HOST` | `localhost` | MySQL host. |
| `DB_PORT` | `3306` | MySQL port. |
| `DB_USER` | `root` | MySQL user. |
| `DB_PASSWORD` | `` | MySQL password. |
| `DB_NAME` | `ai_coach` | MySQL database name. |
| `ENABLE_SCHEDULER` | `true` | Set `false` to disable background jobs. |

> `.env` is gitignored. Never commit real keys. The frontend receives only `WEACE_API_URL` (injected into the template) — API keys stay server-side.

---

## Local Development

### Prerequisites
- Python 3.12
- A reachable MySQL instance
- An OpenAI or Anthropic API key
- Access to a We-Ace API environment (for login/profile)

### Setup

```bash
# 1. Create a virtualenv and install deps
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Create a .env with at least:
#    AI_PROVIDER, OPENAI_API_KEY (or CLAUDE_API_KEY),
#    DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME,
#    WEACE_API_URL, FLASK_SECRET_KEY

# 3. Run the app (tables auto-create on first boot)
python app.py          # dev server on http://localhost:5000
```

### CLI coach (no web, no DB)

`ai_coach.py` is a standalone terminal chat useful for quickly testing the prompt/provider:

```bash
python ai_coach.py --provider claude          # or: --provider openai
python ai_coach.py --model gpt-4o
```

---

## Deployment

Containerised and shipped to AWS ECR via the `Makefile`. The image runs Gunicorn with 2 workers and a 120s timeout.

```bash
make build     # bump VERSION (patch), build :<version> and :latest
make push      # build + login to ECR + tag + push
make deploy    # push + restart the single-host container (ai-coach-app)
make run       # run :latest locally with --env-file .env
```

Configurable overrides (see `Makefile` header): `AWS_ACCOUNT_ID`, `AWS_REGION`, `ECR_REPO`, `IMAGE_NAME`. `VERSION` is auto-incremented (patch) on each build.

**Production notes:**
- Set a strong `FLASK_SECRET_KEY`.
- Run a single web process or an external cache if you need shared session/history state (current caches are per-process; correctness is preserved via DB rebuild, but affinity avoids re-personalisation churn).
- Ensure only one scheduler instance runs (e.g. set `ENABLE_SCHEDULER=false` on all but one worker/host) to avoid duplicate sentiment runs.

---

## Utility Scripts

These are operational one-offs, run manually (not part of the web request path):

| Script | Purpose |
|--------|---------|
| `migrate_sentiment.py` | One-off migration for the sentiment schema. |
| `reset_password.py` | Reset a user's password (Cognito admin op). |
| `delete_cognito_users.py` | Bulk-delete users from Cognito. |
| `signout_cognito_user.py` | Force a global sign-out for a Cognito user. |
| `custom_email.py` | Email-sending helper (gitignored). |

---

*Internal We-Ace product. Changes ship to a live production coaching app — verify against a staging/API environment before deploying.*
