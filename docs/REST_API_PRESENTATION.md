# REST APIs: In-Depth Explanation for Presentation

This document explains every aspect of the project’s REST API: purpose, implementation, data flow, and how it fits into the application.

---

## 1. Overview: Why the API Exists

The app is **server-rendered** (Jinja2 templates, form POSTs). The REST API is a **separate surface** that:

- **Enables programmatic access**: scripts, external tools, or a future SPA can use JSON instead of scraping HTML.
- **Mirrors core workflows**: teams, swimmers, events, and results are exposed so clients can build their own UIs or integrations.
- **Supports the same import flow**: `POST /api/import` does the same SwimCloud import as the scrape page, so import can be triggered from automation or API clients.

All API routes live in **one blueprint** (`api.py`) and are mounted under the `/api` prefix.

---

## 2. How the API Is Wired Into the App

### Registration

In `app.py`, the API blueprint is registered with a URL prefix:

```python
from api import api_bp
# ...
app.register_blueprint(main_bp)           # web routes: /login, /scrape, /select, etc.
app.register_blueprint(api_bp, url_prefix="/api")   # all API routes under /api
```

So every route in `api.py` is exposed as `https://your-host/api/...`.

### Dependencies

The API module uses:

- **Flask**: `Blueprint`, `jsonify`, `request`
- **Flask-Login**: `login_required`, `current_user` for auth
- **Extensions**: `db` (SQLAlchemy), `cache` (Flask-Caching)
- **Models**: `Team`, `Swimmer`, `Event`, `Time`, and the association table `user_team_seasons`
- **SwimCloud**: `swimcloud_scraper` as `sc` for search and import
- **Routes**: helpers from `routes.py` are imported **inside** the view functions to avoid circular imports: `format_time`, `get_or_create_event`, `_link_team_season_to_user`

---

## 3. Authentication and Authorization

### Authentication

- **Every API endpoint** (except the app-level `/health`) is protected with `@login_required`.
- If the client is not logged in, Flask-Login redirects to the login page (HTTP 302). For API clients that expect JSON, you would typically add a custom handler to return 401 Unauthorized with a JSON body; the current implementation uses the default redirect.
- The “current user” is provided by Flask-Login’s session (cookie-based); there is no API-key or Bearer-token auth in this project.

### Authorization (data scoping)

Data is **scoped per user** via the `user_team_seasons` table:

- A row `(user_id, team_id, season_year)` means “this user has access to this team for this season.”
- **Teams list**: only teams that appear in `user_team_seasons` for `current_user.id` are returned.
- **Team swimmers**: the client can only request swimmers for a `team_id` if that user has a `user_team_seasons` row for that team (otherwise 404).
- **Results**: the query joins through `user_team_seasons` and filters by `user_id == current_user.id`, so a user only sees times for team-seasons they have linked.

So the API does not expose “all teams in the database”; it exposes “teams (and their swimmers and times) that this user has imported or been given access to.”

---

## 4. Endpoint-by-Endpoint

### 4.1 `GET /api/teams`

**Purpose:** Return all teams the current user has access to, with a swimmer count for each. Used to populate team pickers or dashboards.

**Implementation:**

1. Query `Team` joined with `user_team_seasons` on `Team.id == user_team_seasons.c.team_id`.
2. Filter by `user_team_seasons.c.user_id == current_user.id`.
3. `distinct()` so each team appears once even if the user has multiple seasons for that team.
4. Order by `Team.name`.
5. For each team, return `id`, `name`, and `swimmer_count` (length of `t.swimmers`).

**Response:** JSON array of objects:

```json
[
  { "id": 1, "name": "University of Pittsburgh", "swimmer_count": 28 },
  { "id": 2, "name": "Penn State", "swimmer_count": 32 }
]
```

**Status:** 200 on success. 302 if not authenticated.

---

### 4.2 `GET /api/teams/<team_id>/swimmers`

**Purpose:** List swimmers for a specific team, optionally filtered by gender. Allows building roster views or dropdowns for one team.

**Implementation:**

1. **Authorization:** Check that the current user has at least one `user_team_seasons` row for this `team_id`. If not, return 404 with `{"error": "Team not found"}`.
2. Load the team with `Team.query.get_or_404(team_id)`.
3. Optional query parameter: `gender` (e.g. `?gender=M` or `?gender=F`). If present, filter swimmers by `Swimmer.gender`.
4. Query `Swimmer` by `team_id`, apply gender filter, order by name.
5. Return list of `{ "id", "name", "gender" }`.

**Request example:** `GET /api/teams/1/swimmers?gender=M`

**Response:** JSON array:

```json
[
  { "id": 101, "name": "John Smith", "gender": "M" },
  { "id": 102, "name": "Alex Jones", "gender": "M" }
]
```

**Status:** 200 on success. 404 if team doesn’t exist or user has no access. 302 if not authenticated.

---

### 4.3 `GET /api/events`

**Purpose:** Return the full list of events (e.g. “50 Free”, “200 Back”) used in the system. Event definitions are global (not per-user). Used to populate event selectors.

**Implementation:**

1. Query all `Event` rows, ordered by name.
2. Return `id`, `name`, `course` for each.
3. **Caching:** The view is decorated with `@cache.cached(timeout=300)`. The first request after a cold start (or after cache clear) runs the query; subsequent requests within 5 minutes get the cached JSON. Cache is cleared whenever data is written (scrape, seed, remove team-season, or API import).

**Response:** JSON array:

```json
[
  { "id": 1, "name": "50 Free", "course": "Y" },
  { "id": 2, "name": "100 Free", "course": "Y" }
]
```

**Status:** 200. 302 if not authenticated.

---

### 4.4 `GET /api/results`

**Purpose:** Query stored times (results) with optional filters. Returns who swam what, for which team and season, with time in seconds and a human-readable formatted time. Powers “top times” or result tables via the API.

**Implementation:**

1. **Query parameters (all optional):**
   - `team_id` (int): restrict to one team.
   - `event_id` (int): restrict to one event.
   - `season` (int): restrict to one season year (e.g. 2025).
   - `gender`: restrict to one gender (e.g. `M`, `F`).
   - `limit` (int): max number of rows (default 50, capped at 200).

2. **Base query:**  
   Select from `Time`, joining `Swimmer`, `Team`, `Event`, and `user_team_seasons`. The join to `user_team_seasons` is on `Team.id` and `Time.season_year == user_team_seasons.c.season_year`. Filter by `user_team_seasons.c.user_id == current_user.id`. So only times that belong to team-seasons linked to the current user are visible.

3. Apply filters if provided: `team_id`, `event_id`, `season`, `gender`.

4. Order by `Time.time_secs` (fastest first), then `limit`.

5. **Response shape:** For each row, return `swimmer` (name), `team` (name), `event` (name), `time_secs` (float), `time_formatted` (from `format_time()` in routes: `M:SS.xx`), and `season` (year).

**Request example:**  
`GET /api/results?team_id=2&event_id=15&season=2025&gender=M&limit=10`

**Response:** JSON array:

```json
[
  {
    "swimmer": "Jane Doe",
    "team": "Penn State",
    "event": "200 Free",
    "time_secs": 112.45,
    "time_formatted": "1:52.45",
    "season": 2025
  }
]
```

**Status:** 200. 302 if not authenticated.

---

### 4.5 `POST /api/import`

**Purpose:** Trigger a SwimCloud import for a team by name, for a given gender and season year. Same business logic as the web “Import Team” on the scrape page: resolve team name → fetch times from SwimCloud → persist teams, swimmers, times, and link the team-season to the current user. Allows automation (e.g. cron or scripts) to import without using the UI.

**Request body (JSON):**

- `team_name` (string, required): e.g. `"Pittsburgh"` or `"Michigan"`. Passed to SwimCloud search.
- `gender` (string, optional): `"M"` or `"F"`. Default `"M"`.
- `year` (integer, required): season end year, e.g. `2025` for the 2024–2025 season.

**Implementation (step by step):**

1. **Parse and validate:**  
   Read JSON from `request.get_json(silent=True)`. If `team_name` is missing or blank, or `year` is missing, return 400 with `{"error": "team_name and year are required"}`. If `year` is not an integer, return 400 with `{"error": "year must be an integer"}`.

2. **Resolve team:**  
   Call `sc.search_teams(team_name)`. If the request to SwimCloud fails, return 502 with a message. If no matches, return 404 with a message like `No teams found for "..."`. Use the first match: `sc_team_id`, `sc_team_name`.

3. **Reuse existing data (optional):**  
   Look up `Team` by `sc_team_name`. If such a team exists, check whether there are any `Time` rows for that team, the requested gender, and the requested `year`. If yes:
   - Call `_link_team_season_to_user(existing_team.id, year)` so the current user gets access.
   - Commit, clear cache, and return **200** with a JSON body indicating “data already exists, linked to your account” and zeros for swimmers/times/events imported.

4. **Fetch from SwimCloud:**  
   Call `sc.get_team_times(team_id=sc_team_id, gender=gender, year=year)`. This hits SwimCloud’s API for all configured events and returns `(events_data, roster_map)`. On failure, return 502.

5. **Persist:**  
   - Create the team if it didn’t exist: `Team(name=sc_team_name)`, add, flush.  
   - For each event name and its list of entries (swimmer_id, swimmer_name, time_secs):
     - Resolve or create `Event` via `get_or_create_event(event_name)`.
     - For each entry: get or create `Swimmer` by name and team (using a in-memory cache keyed by SwimCloud swimmer id to avoid duplicate inserts), then get or create/update `Time`: one best time per (swimmer, event, season); if a time already exists, update only if the new time is faster.
   - Call `_link_team_season_to_user(team.id, year)`.
   - Commit and clear cache.

6. **Response:** Return **201** with JSON: `team`, `season` (e.g. from `sc.season_label(year)`), `swimmers_imported`, `times_imported`, `events`.

**Response examples:**

- **201 (new import):**
```json
{
  "team": "University of Pittsburgh",
  "season": "2024-2025",
  "swimmers_imported": 28,
  "times_imported": 312,
  "events": 14
}
```

- **200 (already in DB, just linked):**
```json
{
  "team": "University of Pittsburgh",
  "season": "2024-2025",
  "message": "Data already exists — linked to your account.",
  "swimmers_imported": 0,
  "times_imported": 0,
  "events": 0
}
```

- **400:** `{"error": "team_name and year are required"}` or `{"error": "year must be an integer"}`
- **404:** `{"error": "No teams found for \"...\""}`
- **502:** `{"error": "Search failed: ..."}` or `{"error": "Import failed: ..."}`

**Status:** 200 (linked existing), 201 (created), 400 (validation), 404 (no team), 502 (external/import error). 302 if not authenticated.

---

## 5. Caching

- **Cached endpoint:** Only `GET /api/events` is cached, with `@cache.cached(timeout=300)` (5 minutes). Events are rarely changed; they’re created when new event names appear during import.
- **Cache backend:** From `config.py`, `CACHE_TYPE` (e.g. `SimpleCache`) and `CACHE_DEFAULT_TIMEOUT` (default 300). Other backends (e.g. Redis) can be used via env.
- **Invalidation:** After any write that affects the app’s data (scrape, seed, remove team-season, or `POST /api/import`), the code calls `cache.clear()`. So the next `GET /api/events` (and any other cached view if added) sees up-to-date data.

---

## 6. Shared Logic with the Web App

The API reuses behavior from the main app without duplicating code:

- **`format_time(secs)`** (routes): Converts seconds to `M:SS.xx` for `GET /api/results`.
- **`get_or_create_event(name, course)`** (routes): Ensures an `Event` row exists; used during import in both scrape and API.
- **`_link_team_season_to_user(team_id, season_year)`** (routes): Inserts into `user_team_seasons` for the current user (idempotent). Used after import in both scrape and API.

These are imported **inside** the API view functions (e.g. `from routes import format_time`) to avoid circular imports between `api` and `routes`.

---

## 7. Error Handling and Status Codes Summary

| Situation              | Status | Body / behavior                          |
|------------------------|--------|------------------------------------------|
| Not logged in          | 302    | Redirect to login                        |
| Validation (missing/invalid body) | 400 | `{"error": "..."}`                       |
| Team not found / no access | 404  | `{"error": "Team not found"}` or similar |
| SwimCloud search/import failure | 502 | `{"error": "..."}`                       |
| Success (read)         | 200    | JSON array or object                     |
| Success (import, new)  | 201    | JSON with team, season, counts           |
| Success (import, linked) | 200  | JSON with message and zeros              |

The app also defines global error handlers in `app.py` for 404 and 500 (HTML error pages); they apply to the whole app, including API routes, unless overridden for the API to return JSON.

---

## 8. Data Model Context (Quick Reference)

- **User:** id, username, password_hash.
- **Team:** id, name.
- **user_team_seasons:** (user_id, team_id, season_year) — which user can see which team-season.
- **Swimmer:** id, name, gender, team_id.
- **Event:** id, name, course.
- **Time:** id, swimmer_id, event_id, time_secs, season_year, optional meet/date.

The API never exposes raw IDs beyond what’s needed (e.g. team id, swimmer id, event id); it often returns names for readability (swimmer name, team name, event name).

---

## 9. Summary Table for Slides

| Method | Endpoint | Purpose | Auth | Cache |
|--------|----------|---------|------|--------|
| GET | `/api/teams` | List user’s teams + swimmer count | login | no |
| GET | `/api/teams/<id>/swimmers` | Roster for one team (optional ?gender=) | login | no |
| GET | `/api/events` | List all events | login | 5 min |
| GET | `/api/results` | Query times (team, event, season, gender, limit) | login | no |
| POST | `/api/import` | SwimCloud import (team_name, gender, year) | login | no (clears cache after) |

You can use this document as the basis for slides or talking points, and refer to `api.py` and `app.py` for the exact code paths.
