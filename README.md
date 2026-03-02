# SwimScore — Swim Scoring & Lineup Optimizer

A full-stack web application that helps collegiate swim coaches build optimal relay lineups and dual-meet scoring projections. It pulls real athlete data from [SwimCloud](https://www.swimcloud.com), stores it in a PostgreSQL/SQLite database, and exposes both an interactive UI and a JSON REST API for analysis, relay optimization, and Excel export.

---

## The Problem It Solves

Coaches preparing for a dual meet face questions like:

- *"What is our fastest possible 200 Medley Relay lineup?"*
- *"If we rest our top sprinter, who should fill in and how does our score change?"*
- *"How do we project across all 14 events against Penn State?"*

Answering these by hand — pulling times from a PDF, sorting spreadsheet columns, manually checking for double-events — is slow and error-prone. SwimScore automates the entire workflow:

1. **Import** — one form submission scrapes a full roster and personal bests from SwimCloud
2. **Rank** — instantly view top-*N* swimmers across any of the 14 SCY individual events
3. **Optimize relays** — branch-and-bound medley assignment and greedy free-relay splits in both NCAA "scoring" (A/B relay) and "non-scoring" modes
4. **Exclude & re-rank** — toggle individual times off and recompute without refreshing state
5. **Export** — generate a multi-sheet Excel workbook covering every event and relay

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                     Client Layer (HTML Browser)                 │
│           GET/POST via Jinja2 server-rendered templates         │
│           Extensibility Layer → JSON REST API (/api/*)          │
└──────────────────────────────┬──────────────────────────────────┘
                               │ HTTP
┌──────────────────────────────▼──────────────────────────────────┐
│                 Flask App (App Factory Pattern)                 │
│                                                                 │
│  ┌─────────────┐  ┌──────────────────┐  ┌────────────────────┐  │
│  │   Login /   │  │  Import (scrape) │  │  Dashboard         │  │
│  │   Auth      │  │  + Test Data     │  │  (/select)         │  │
│  └─────────────┘  └──────────────────┘  └────────────────────┘  |
│                                                                 |
│  ┌───────────────────────────────────────────────────────────┐  │
│  │             API Blueprint (/api/* — JSON REST)            │  │
│  └───────────────────────────────────────────────────────────┘  │
└──────────────────────┬───────────────────────┬──────────────────┘
                       │                       │
        ┌──────────────▼──────────┐  ┌─────────▼────────────┐
        │  Data Scraper           │  │   Redis Cache        │
        │  (swimcloud_scraper.py) │  │   (Flask-Caching)    │
        │  SwimCloud JSON API     │  │   SimpleCache local  │
        └──────────────┬──────────┘  └──────────────────────┘
                       │ Write / Read
        ┌──────────────▼───────────────────────────────────┐
        │          SQLAlchemy ORM + PostgreSQL             |
        │          (SQLite for local dev)                  |
        └──────────────────────────────────────────────────┘
```

| Layer | Technology | Purpose |
|-------|-----------|---------|
| **Frontend** | Jinja2 templates, custom CSS | Server-rendered UI, responsive design, no build step |
| **Auth** | Flask-Login, Flask-WTF (CSRF), werkzeug.security (scrypt) | Session-based auth with password strength validation |
| **Backend** | Flask (app factory), blueprints | Routing, request handling, scoring engine |
| **REST API** | Flask Blueprint (`/api/*`) | JSON endpoints for teams, swimmers, events, results, import |
| **Scoring engine** | `services/scoring.py` (pure Python) | NCAA point tables, relay pool building, B&B optimizer |
| **Data import** | `services/import_service.py` | SwimCloud scraping + deduplication, shared by UI and API |
| **ORM / DB** | SQLAlchemy (SQLite dev, PostgreSQL prod) | Multi-user data model with per-user team-season scoping |
| **Cache** | Flask-Caching → Redis (Docker) / SimpleCache (local) | Swimmer pool queries cached by team/season/gender |
| **Export** | pandas + openpyxl | Multi-sheet Excel workbook for every event and relay |
| **Logging** | Python `logging` → stderr, per-module levels | Structured request and scraper logs |
| **Containerization** | Docker + Gunicorn + docker-compose | Production-ready: web + PostgreSQL + Redis services |
| **Testing** | pytest (28 tests, in-memory SQLite) | Auth, models, scoring logic, REST API, data isolation |

---

## Key Design Decisions

### 1. Branch-and-bound medley relay assignment

The core algorithmic challenge is assigning exactly one swimmer per stroke (Back → Breast → Fly → Free) to minimize total relay time, with no swimmer used twice.

The naive approach is O(n⁴) brute-force over all candidate combinations. Instead, `_best_medley_assignment()` in `services/scoring.py` uses **branch and bound**:

- Candidates per stroke are sorted ascending by time
- At each depth level, a **relaxed suffix lower bound** is precomputed: the minimum possible sum for remaining strokes ignoring uniqueness constraints (a valid optimistic bound)
- If `partial_time + suffix_lb[idx] >= best_known`, the entire subtree is pruned
- Within a level, once `partial + entry['time'] + suffix_lb[idx+1] >= best_known`, a `break` is safe because candidates are sorted — all subsequent entries are at least as slow
- This is called iteratively to produce A/B relay squads, passing `used_ids` forward each time

In practice this explores a tiny fraction of the n⁴ search space, and runs in milliseconds even for large rosters.

### 2. Caching architecture: raw pools cached, exclusions filtered in memory

Relay pool queries (4 SQL joins per stroke for medley, 1 per free relay distance) are the most expensive repeated operations, and coaches typically work with the same team for an entire session.

The caching strategy is:
- **Cache the full, unfiltered pool** per `(team_id, season_year, gender)` using Redis (Docker) or SimpleCache (local dev)
- Cache keys: `freepool:{tid}:{yr}:{dist}:{gender}` and `medley:{tid}:{yr}:{gender}`
- Apply the coach's `excluded` time-ID set **in memory** after retrieval — this keeps cache keys simple and stable regardless of which times are toggled
- `cache.clear()` is called on every import or team-season deletion, ensuring stale data is never served

This means the first relay page load hits the DB; every subsequent interaction within the session (re-ranking, excluding swimmers, changing events) is served from cache.

### 3. Per-user data scoping with shared storage

The data model separates *ownership* from *storage*:

```
User ↔ Team  (many-to-many through user_team_seasons, composite PK: user_id + team_id + season_year)
Team → Swimmer → Time ← Event
```

If two coaches import the same team, the swimmer and time rows are shared — only one copy is stored. Deleting a team-season only removes the underlying data when the **last** user unlinks it. This avoids data duplication while keeping each coach's dashboard isolated.

### 4. Services layer for shared logic

The scrape UI (`/scrape`) and the REST API (`POST /api/import`) share identical import logic through `services/import_service.py`. Similarly, all scoring and relay logic lives in `services/scoring.py` with no Flask dependencies — it can be imported and unit-tested standalone. This separation prevents duplication and makes each layer independently testable.

### 5. Custom SwimCloud scraper

Rather than depend on an unmaintained third-party library, `swimcloud_scraper.py`:
- Uses SwimCloud's `/api/search/` endpoint to resolve team names to IDs (no manual ID lookup)
- Queries `/api/splashes/top_times/` event-by-event with `dont_group=false` to get exactly one season-best per swimmer
- Maps user-entered year (e.g. 2025) to SwimCloud's internal `season_id` correctly

### 6. Scored vs. unscored relay modes

The dashboard supports two relay modes that match real NCAA dual-meet rules:
- **Unscored** — greedy fastest-4 assignment, no A/B distinction
- **Scored** — each team's B relay cannot outrank any other team's A relay; the B&B optimizer runs twice per team (A squad, then B squad with A swimmers excluded), and `rank_scored_combos()` enforces the A/B ordering before applying the `RELAY_SCORE` point table

---

## Setup & Installation

### Prerequisites
- Python 3.8+

### Local dev (SQLite — no Docker required)

```bash
git clone https://github.com/<your-username>/swim-scoring-lineup.git
cd swim-scoring-lineup

python -m venv venv
# Windows: venv\Scripts\activate
# macOS/Linux: source venv/bin/activate
source venv/bin/activate

pip install -r requirements.txt

cp .env.example .env   # optional: defaults work without a .env

python app.py          # http://localhost:5001
```

### Docker (PostgreSQL + Redis)

```bash
docker compose up --build
# App: http://localhost:5001
# Postgres: localhost:5433  Redis: localhost:6379
```

The Docker environment automatically sets `CACHE_TYPE=RedisCache` and `REDIS_URL=redis://redis:6379/0`. Local dev defaults to `SimpleCache` (no Redis needed).

### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | `sqlite:///swim.db` | DB connection string |
| `SECRET_KEY` | `dev-secret-key-change-me` | Flask session signing |
| `CACHE_TYPE` | `SimpleCache` | `RedisCache` for production |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection (if `CACHE_TYPE=RedisCache`) |
| `CACHE_TIMEOUT` | `300` | Cache TTL in seconds |

### Running tests

```bash
pytest tests/ -v
```

Tests use an in-memory SQLite database and have CSRF disabled. No `.env` or external services needed — all 28 tests run offline.

### First-time walkthrough
1. Register at `/register`
2. Import a team at `/scrape` — enter a team name (e.g. "Michigan"), select gender and season year, click **Import Team** (~15-30 seconds to scrape)
3. Open the **Dashboard** at `/select`, check team-seasons, pick an event, click **Get Top Swimmers**
4. Toggle swimmers on/off and **Recalculate**, or click **Export All to Excel**

> **Tip:** Click **Generate Test Data** on the import page to populate two fictional teams (Pitt Panthers + Penn State Lions) with realistic random times for instant exploration without scraping.

---

## REST API

All endpoints require a valid session (log in first via the browser, or set the session cookie).

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/teams` | All teams the current user has access to |
| `GET` | `/api/teams/:id/swimmers` | Swimmers on a team; optional `?gender=M\|F` |
| `GET` | `/api/events` | All known events (cached) |
| `GET` | `/api/results` | Times with filters: `team_id`, `event_id`, `season`, `gender`, `limit` |
| `POST` | `/api/import` | Trigger SwimCloud import: `{"team_name": "...", "gender": "M", "year": 2025}` |
| `GET` | `/health` | Liveness probe — checks DB connectivity |

```bash
# Example: import a team via the REST API
curl -X POST http://localhost:5001/api/import \
  -H "Content-Type: application/json" \
  -d '{"team_name": "Pittsburgh", "gender": "M", "year": 2025}'
```

---

## Project Structure

```
swim-scoring-lineup/
├── app.py                       # App factory, logging config, error handlers
├── config.py                    # Env-var driven config (DB, cache, Redis)
├── extensions.py                # Flask extension singletons (db, login, cache)
├── models.py                    # SQLAlchemy models + indexes (User, Team, Swimmer, Event, Time)
├── forms.py                     # Flask-WTF forms with password strength validation
├── routes.py                    # Main blueprint: auth, import, dashboard
├── api.py                       # REST API blueprint (/api/*)
├── swimcloud_scraper.py         # SwimCloud JSON API client
├── services/
│   ├── scoring.py               # NCAA tables, relay pools, B&B optimizer (no Flask deps)
│   ├── import_service.py        # SwimCloud import logic shared by UI + API
│   ├── export_service.py        # Excel workbook generation
│   └── test_data_service.py     # Synthetic roster generator for local testing
├── templates/
│   ├── base.html                # Shared layout, navigation, full CSS design system
│   ├── login.html
│   ├── register.html
│   ├── scrape.html              # Import form + test data generation
│   ├── select.html              # Dashboard: event selection, results table, pagination
│   └── errors/
│       ├── 404.html
│       └── 500.html
├── tests/
│   └── test_app.py              # 28 pytest tests (auth, models, scoring, API, isolation)
├── Dockerfile
├── docker-compose.yml           # web + postgres + redis services
├── requirements.txt
└── .env.example
```

---

## Tradeoffs & Future Improvements

| Area | Current approach | Why / Tradeoff | Potential improvement |
|------|-----------------|----------------|----------------------|
| **Frontend** | Server-rendered Jinja2 | No build toolchain; simpler deployment for a small user base | React/HTMX for live re-ranking without full page reloads |
| **Relay optimizer** | Branch-and-bound (depth-first, relaxed LB) | Optimal and fast for typical rosters (~20-50 swimmers) | Hungarian algorithm for guaranteed O(n³) worst-case on very large pools |
| **Scraping** | Synchronous, event-by-event (14 requests) | Simple; 10-30 second import is acceptable | `asyncio` / `aiohttp` for parallel event fetches — estimated 5-8× speedup |
| **Auth** | Username + hashed password | Sufficient for a closed tool | OAuth/SSO for institutional deployment |
| **Caching** | Redis (Docker) / SimpleCache (local) | Redis requires the Docker stack for production | Persistent Redis with AOF so cache survives restarts |
| **CI/CD** | Local pytest | No automation | GitHub Actions: lint + test + Docker build on every PR |
| **Scoring scope** | SCY individual events only | Matches the primary use case (college dual meets) | LCM and relay splits for championship meet projections |
