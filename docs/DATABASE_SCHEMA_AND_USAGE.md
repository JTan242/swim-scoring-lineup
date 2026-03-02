# Database Schema and How It Is Used

This document describes the full database schema and how each table and relationship is used across the application.

---

## 1. High-Level Picture

The app uses **five tables** plus one **association table**:

- **app_user** — users who log in (coaches/admins).
- **user_team_seasons** — which user can see which (team + season); many-to-many between User and “team-season”.
- **team** — swim teams (e.g. “University of Pittsburgh”).
- **swimmer** — athletes; each belongs to one team.
- **event** — event definitions (e.g. “50 Free”, “200 Back”, course “Y”).
- **time** — a single recorded time: one swimmer, one event, one season; optional meet/date.

Relationship summary: **User ↔ (Team + Season)** via `user_team_seasons`. **Team → Swimmer → Time ← Event.** Times are the central fact table; everything else exists to scope and describe them.

---

## 2. Table Definitions (Schema)

### 2.1 `app_user` (User)

| Column         | Type         | Constraints      | Description                |
|----------------|--------------|------------------|----------------------------|
| id             | Integer      | PK               | Surrogate key              |
| username       | String(80)   | NOT NULL, UNIQUE | Login name                 |
| password_hash  | String(256)  | NOT NULL         | Hashed password (Werkzeug) |

**ORM:** `User(UserMixin, db.Model)`, `__tablename__ = "app_user"`.

**Methods:** `set_password(pwd)`, `check_password(pwd)` (Werkzeug hashing).

**Usage:** Login/register (routes), Flask-Login `current_user`, and as `user_id` in `user_team_seasons` to scope all “my team-seasons” and thus all API and dashboard data.

---

### 2.2 `user_team_seasons` (association table)

| Column       | Type    | Constraints | Description                          |
|-------------|---------|-------------|--------------------------------------|
| user_id     | Integer | PK, FK → app_user.id | User who has access        |
| team_id     | Integer | PK, FK → team.id     | Team                         |
| season_year | Integer | PK          | Season end year (e.g. 2025 = 2024–25) |

**Composite primary key:** `(user_id, team_id, season_year)`. One row = “this user has this team for this season on their dashboard.”

**ORM:** No model class; defined as `db.Table("user_team_seasons", ...)`. Used via `user_team_seasons.c.user_id`, `.c.team_id`, `.c.season_year`, and `.select()`, `.insert()`, `.delete()`.

**Usage:**

- **Scoping data:** Every “list teams” or “list times” query that should be per-user joins or filters by this table and `current_user.id`. Used in:
  - `_team_season_pairs()` — builds (team_id, team_name, season_year) for the current user for the dashboard form.
  - API: `GET /api/teams`, `GET /api/teams/<id>/swimmers` (access check), `GET /api/results`.
- **Linking after import:** When data is imported (scrape or `POST /api/import`), `_link_team_season_to_user(team_id, season_year)` inserts a row so the current user sees that team-season.
- **Removing access:** “Remove Selected Team” deletes rows for the current user; if no other user has that (team_id, season_year), the app then deletes times/swimmers/team for that team-season.

---

### 2.3 `team`

| Column | Type         | Constraints      | Description     |
|--------|--------------|------------------|-----------------|
| id     | Integer      | PK               | Surrogate key   |
| name   | String(100)  | NOT NULL, UNIQUE | Team name      |

**ORM:** `Team(db.Model)`. Relationship: `swimmers` → list of `Swimmer` (backref `team`).

**Usage:**

- **Created** when importing from SwimCloud (by name) or when seeding test data; looked up by `name` to reuse existing team.
- **Read** for: dashboard team-season choices (with `user_team_seasons`), API teams list and swimmers, result queries (join to get team name), Excel export (team name per row).
- **Deleted** only when “Remove” is used and no other user has that team-season and no times remain for that team.

---

### 2.4 `swimmer`

| Column   | Type        | Constraints | Description        |
|----------|-------------|-------------|--------------------|
| id       | Integer     | PK          | Surrogate key      |
| name     | String(100) | NOT NULL    | Display name       |
| gender   | String(1)   |             | "M" or "F"         |
| team_id  | Integer     | FK → team.id| Team they belong to|

**ORM:** `Swimmer(db.Model)`. Relationship: `times` → list of `Time` (backref `swimmer`); `team` backref to `Team`.

**Usage:**

- **Created** during import/seed per (name, team_id); deduplicated by name+team so one swimmer per team.
- **Read** for: roster listing (API `GET /api/teams/<id>/swimmers` with optional gender), relay building (free/back/breast/fly legs by team/gender/season/event), individual results (join Time → Swimmer for name and gender filter), Excel (swimmer name per row).
- **Deleted** when a team-season is removed and that team has no remaining times (cascade-like cleanup in routes).

---

### 2.5 `event`

| Column | Type         | Constraints | Description              |
|--------|--------------|-------------|--------------------------|
| id     | Integer      | PK          | Surrogate key            |
| name   | String(100)  | NOT NULL    | e.g. "50 Free", "200 Back" |
| course | String(10)   | NOT NULL    | e.g. "Y" (SCY)           |

**ORM:** `Event(db.Model)`. Relationship: `times` → list of `Time` (backref `event`).

**Usage:**

- **Created** on demand via `get_or_create_event(name, course='Y')` when storing times (scrape, API import, seed). Events are global (shared across teams).
- **Read** for: event dropdown on dashboard (individual + relay names), API `GET /api/events` (cached), filtering times by event (individual rankings, Excel sheets per event), relay logic (e.g. “100 Free”, “100 Back” for medley legs).

---

### 2.6 `time`

| Column      | Type    | Constraints   | Description                    |
|-------------|---------|---------------|--------------------------------|
| id         | Integer | PK            | Surrogate key                  |
| swimmer_id | Integer | FK → swimmer.id, indexed | Who swam              |
| event_id   | Integer | FK → event.id | Which event                    |
| time_secs   | Numeric | NOT NULL      | Time in seconds                |
| meet       | String(200) |            | Optional meet name             |
| date       | Date    |               | Optional date                  |
| season_year| Integer | indexed       | Season end year (e.g. 2025)     |

**Indexes:** `ix_time_event_time(event_id, time_secs)`, `ix_time_season(season_year)`, `ix_time_swimmer(swimmer_id)` (plus FK index on swimmer_id).

**ORM:** `Time(db.Model)`, `__tablename__ = "time"`. Backrefs: `swimmer`, `event`.

**Usage:**

- **Created/updated** during import and seed: one “best” time per (swimmer, event, season); if a time already exists, it is updated only when the new time is faster.
- **Read** everywhere results are needed:
  - Dashboard: individual rankings (filter by team_ids, seasons, event, gender; exclude by time_id; distinct by (swimmer, team, season); order by time_secs; top_n).
  - Relays: build pools of best times per stroke/distance per team/gender/season, then combine into relay squads.
  - API `GET /api/results`: same filters (team_id, event_id, season, gender, limit), joined with user_team_seasons.
  - Excel: one sheet per individual event (rows = times with team, swimmer, time, points); relay sheets from computed squads.
- **Deleted** when a team-season is removed and no other user has that (team_id, season_year): all times for that team and season are deleted before swimmers/team are cleaned up.

**Business rule:** Application logic treats “one best time per (swimmer, event, season)”; the schema allows multiple rows per (swimmer, event, season) but the code keeps at most one (update-if-faster on import).

---

## 3. Entity-Relationship Summary

```
┌─────────────┐       user_team_seasons       ┌─────────────┐
│  app_user   │◄─────────────────────────────►│    team     │
│ id, username│  (user_id, team_id,           │ id, name    │
│ password_   │   season_year)                └──────┬──────┘
│ hash        │                                      │
└─────────────┘                                      │ 1:N
                                                      ▼
                                               ┌─────────────┐
                                               │  swimmer    │
                                               │ id, name,   │
                                               │ gender,     │
                                               │ team_id     │
                                               └──────┬──────┘
                                                      │ 1:N
                                                      ▼
┌─────────────┐                               ┌─────────────┐
│   event     │◄──────────────────────────────│    time     │
│ id, name,   │         N:1                    │ id,         │
│ course      │                               │ swimmer_id, │
└─────────────┘                               │ event_id,   │
                                              │ time_secs,  │
                                              │ season_year │
                                              └─────────────┘
```

- **User ↔ Team-Season:** Many-to-many via `user_team_seasons` (user can have many team-seasons; a team-season can be linked to many users).
- **Team → Swimmer:** One-to-many (each swimmer has one team).
- **Swimmer → Time:** One-to-many (many times per swimmer, across events/seasons).
- **Event → Time:** One-to-many (many times per event, from different swimmers).

---

## 4. How Each Table Is Used by Feature

| Feature | Tables / relationships used |
|--------|------------------------------|
| **Login / register** | `User`: lookup by username, check_password, set_password. |
| **“My” team-seasons** | `user_team_seasons` + `Team`: _team_season_pairs() for dashboard checkboxes. |
| **Import (scrape or API)** | Resolve/create `Team` by name; get/create `Swimmer` by name+team; get/create `Event` by name; insert/update `Time` (best per swimmer/event/season); insert `user_team_seasons`. |
| **Remove team-season** | Delete from `user_team_seasons` for current user; if no other user has that (team_id, season_year), delete `Time` (team+season), then `Swimmer` (team), then `Team`. |
| **Individual rankings** | `Time` joined to `Swimmer`, `Team`, `Event`; filter by team_ids (from selection), seasons, event_id, gender; join `user_team_seasons` for scope; order by time_secs. |
| **Relay building** | `Time` + `Swimmer` + `Event`: query best times per stroke (e.g. 100 Free, 100 Back) per team/gender/season; build relay combinations and rank. |
| **Excel export** | `Event` for sheet names; `Time` + `Swimmer` + `Team` for rows (team, swimmer, time, points); relay sheets from computed squads. |
| **API: list teams** | `Team` joined `user_team_seasons` on current_user.id; return id, name, swimmer_count. |
| **API: team swimmers** | Check `user_team_seasons` for access; `Swimmer` by team_id (optional gender). |
| **API: events** | `Event` all rows (cached). |
| **API: results** | `Time` joined `Swimmer`, `Team`, `Event`, `user_team_seasons`; filter by user, optional team_id, event_id, season, gender; limit. |
| **API: import** | Same as “Import” above: Team, Swimmer, Event, Time, user_team_seasons. |
| **Seed (test data)** | Create `Team`, `Swimmer`, `Event`, `Time`, and `user_team_seasons` for the current user. |

---

## 5. Important Constraints and Conventions

- **Team name** is unique; used to match SwimCloud and to reuse teams across imports.
- **Season** is represented as a single year (e.g. 2025 = 2024–2025 season); stored in `time.season_year` and `user_team_seasons.season_year`.
- **Best time per (swimmer, event, season)** is enforced in application code on import (upsert, keep faster time).
- **Data isolation:** No row-level security in the DB; all scoping is done in app code by joining or filtering with `user_team_seasons` and `current_user.id`.
- **Cascades:** There are no DB-level ON DELETE CASCADE; cleanup when removing a team-season is done explicitly in routes (delete times → swimmers → team when no other user has that team-season).

This schema supports multi-user dashboards, per–team-season import, individual and relay ranking, and Excel export without redundant storage of team or event names on each time row.
