# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Local dev (SQLite, no Docker):**
```bash
pip install -r requirements.txt
python app.py          # runs on http://localhost:5001
```

**Docker (PostgreSQL):**
```bash
docker compose up --build
```

**Tests:**
```bash
pytest                                    # all tests
pytest tests/test_app.py::test_health_endpoint   # single test
```

Tests use an in-memory SQLite DB and disable CSRF. The `DATABASE_URL` and `SECRET_KEY` env vars are set directly in `tests/test_app.py` before imports, so no `.env` is needed.

## Architecture

Single-package Flask app using the **app factory** pattern (`create_app()` in `app.py`). Two blueprints:
- `main` (`routes.py`) — HTML UI: auth, scrape/seed, dashboard (`/select`)
- `api` (`api.py`, prefix `/api`) — JSON REST endpoints for the same data

**Extension singletons** live in `extensions.py` (db, login_manager, cache) to avoid circular imports. Models import from there; routes import from models.

### Data model
`User` ↔ `Team` many-to-many through `user_team_seasons` (includes `season_year` as a composite PK column). This means:
- Data is **scoped per user** — each user only sees their own team-seasons
- The same team/season data is shared if multiple users import it; deleting a team-season only removes underlying data when the last user unlinks it

`Team → Swimmer → Time ← Event` (one-to-many chain). `Time.time_secs` stores the swimmer's **season best** for an event (only updated if a new import finds a faster time).

### Scoring logic (in `routes.py`)
- `INDIV_SCORE` / `RELAY_SCORE`: NCAA dual-meet point tables
- Individual events: ranked by `time_secs` ascending, points assigned by position
- Relay — two modes:
  - **Unscored** (`pick_greedy_squads`): greedy fastest-4 assigned into successive squads
  - **Scored** (`pick_scored_combos` + `rank_scored_combos`): NCAA A/B relay rules — each team's fastest is an A candidate, 2nd is B; A relays fill ranks 1–8 first; B relays cannot outrank unplaced A relays
  - Medley relay assignment is brute-forced over top-10 per stroke (`_best_medley_assignment`)

### Data import
`swimcloud_scraper.py` hits the SwimCloud JSON API (`/api/search/` and `/api/splashes/top_times/`) for all 14 SCY individual events. The `/scrape` route (UI) and `POST /api/import` (REST) share the same import logic, de-duplicating by `(swimmer, event, season_year)`.

The `/seed` route inserts synthetic Pitt/Penn State data for local testing without hitting SwimCloud.

### Config
`config.py` reads from env vars. Defaults: SQLite at `swim.db`, `SimpleCache`. For PostgreSQL set `DATABASE_URL`. Copy `.env.example` to `.env` for local overrides.
