# SwimScore — Swim Scoring & Lineup Optimizer

A full-stack web application that helps collegiate swim coaches and analysts
build optimal relay lineups and dual-meet scoring projections.  It pulls real
athlete data from [SwimCloud](https://www.swimcloud.com), stores it locally,
and then lets users compare teams, rank swimmers, assemble relay squads, and
export everything to Excel.

---

## Problem It Solves

Coaches preparing for a dual meet have to answer questions like:

* *"What is our fastest possible 200 Medley Relay lineup?"*
* *"If we rest our top sprinter in the 200 Free Relay, who should fill in?"*
* *"How do we score against Penn State across all 14 individual events?"*

Doing this by hand with spreadsheets is slow and error-prone.  **SwimScore**
automates the entire workflow:

1. **Import** — scrape a full roster and personal bests in one click.
2. **Rank** — instantly see top-*N* swimmers across any event.
3. **Optimize relays** — brute-force the fastest medley assignment and greedy
   free-relay splits, in both "scoring" (A/B relay) and "non-scoring" modes.
4. **Exclude & re-rank** — toggle individual times on/off and recalculate.
5. **Export** — generate a multi-sheet Excel workbook covering every event.

---

## Architecture Overview

```
┌───────────────────────────────────────────────────────┐
│                   Browser (Jinja2)                     │
│   login · scrape · select · 404/500 error pages       │
└──────────────────────────┬────────────────────────────┘
                           │ HTTP
┌──────────────────────────▼────────────────────────────┐
│              Flask  (routes.py + api.py)               │
│   Auth · Data import · Scoring engine · REST API       │
├──────────┬──────────┬─────────────┬───────────────────┤
│ models   │ forms    │  scraper    │  Flask-Caching     │
│ (ORM)    │ (WTF)    │  (requests) │  (in-memory)       │
└────┬─────┴──────────┴──────┬──────┴───────────────────┘
     │                       │
SQLite / PostgreSQL     swimcloud.com API
```

| Layer | Technology | Purpose |
|-------|-----------|---------|
| **Frontend** | Jinja2 templates, custom CSS | Server-rendered UI with responsive design |
| **Backend** | Flask, Flask-Login, Flask-WTF | Routing, authentication, CSRF protection |
| **REST API** | Flask Blueprint (`/api/*`) | Programmatic access to teams, events, results, and import |
| **ORM / DB** | SQLAlchemy (SQLite default, PostgreSQL supported) | Persistent storage for teams, swimmers, events, times |
| **Scraper** | Custom `swimcloud_scraper.py` using SwimCloud JSON API | Pulls rosters and season-best times event-by-event |
| **Caching** | Flask-Caching (SimpleCache) | In-memory cache for DB queries and API responses |
| **Export** | pandas + openpyxl | Multi-sheet Excel workbook generation |
| **Testing** | pytest (25 tests) | Auth, models, scoring logic, API, and seed data |
| **Deployment** | Docker + Gunicorn | Production-ready containerized deployment |

---

## Key Design Decisions

### 1. Custom scraper over third-party library
The `SwimScraper` PyPI package broke when SwimCloud changed their HTML layout.
Rather than depend on an unmaintained library, I wrote a focused
`swimcloud_scraper.py` module that:
- Uses SwimCloud's **search API** to resolve team names → IDs (no manual
  ID lookup needed).
- Queries the `/api/splashes/top_times/` JSON API event-by-event, returning
  exactly one season-best time per swimmer per event.
- Handles season-year mapping: entering "2025" correctly maps to the
  2024-2025 academic season.

### 2. Relay optimization algorithm
- **Free relays** use a greedy strategy: sort swimmers by split time, pick the
  fastest 4, remove them, repeat for successive squads.
- **Medley relays** build per-stroke candidate pools (Back, Breast, Fly, Free),
  then brute-force all combinations of the top 10 candidates per stroke to find
  the optimal assignment — ensuring no swimmer is used twice in a single relay.
- **Scored mode** picks an A relay, removes those swimmers, then picks a B
  relay — matching NCAA dual-meet rules.

### 3. Exclusion / re-ranking workflow
Coaches often know a swimmer won't race a particular event.  The dashboard
lets users **uncheck** any time and resubmit; excluded time IDs are carried
forward as hidden form fields so the ranking recomputes without them.

### 4. REST API for programmatic access
All data is accessible through a JSON API at `/api/*`:
- `GET /api/teams` — list all teams with swimmer counts
- `GET /api/teams/:id/swimmers` — swimmers on a team (filterable by gender)
- `GET /api/events` — list all events
- `GET /api/results` — query times with filters (team, event, season, gender)
- `POST /api/import` — trigger a SwimCloud import via JSON payload

### 5. Server-side rendering (no SPA framework)
For a data-heavy tool used by a small number of coaches, server-rendered
Jinja2 templates are simpler to deploy, have no build step, and keep the
entire scoring engine in one Python codebase.

---

## Setup & Installation

### Prerequisites
- Python 3.8+
- pip

### Quick start

```bash
# Clone the repository
git clone https://github.com/<your-username>/swim-scoring-lineup.git
cd swim-scoring-lineup

# Create and activate a virtual environment
python -m venv venv
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# (Optional) copy and fill in environment variables
cp .env.example .env

# Run the development server
python app.py
```

The app starts at **http://localhost:5001**.

### Docker

```bash
# Build and run
docker compose up --build

# Or just the app (without postgres)
docker build -t swimscore .
docker run -p 5001:5001 swimscore
```

### Running Tests

```bash
pytest tests/ -v
```

All 25 tests covering auth, models, scoring logic, REST API, seed data, and
the dashboard should pass.

### First-time walkthrough
1. **Register** an account at `/register`.
2. **Import data** at `/scrape` — enter a team name (e.g. "Michigan"), choose
   gender and season year, and click **Import Team**.  The scraper resolves
   the name via SwimCloud's search API and imports roster + best times.
3. Head to the **Dashboard** at `/select`, check the team-seasons you want to
   compare, pick an event, and click **Get Top Swimmers**.
4. Toggle swimmers on/off and **Recalculate**, or click **Export All to Excel**
   for a full workbook.

> **Tip:** If you just want to explore the UI, click **Generate Test Data** on
> the import page to create two fictional teams with realistic times.

---

## REST API

All endpoints require authentication (session cookie from login).

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/teams` | List all teams with swimmer counts |
| GET | `/api/teams/:id/swimmers?gender=M` | List swimmers on a team |
| GET | `/api/events` | List all known events |
| GET | `/api/results?team_id=1&event_id=3&season=2025&limit=50` | Query times with filters |
| POST | `/api/import` | Import from SwimCloud (JSON: `team_name`, `gender`, `year`) |
| GET | `/health` | Liveness probe (checks DB connectivity) |

Example:
```bash
# Import a team via the API
curl -X POST http://localhost:5001/api/import \
  -H "Content-Type: application/json" \
  -d '{"team_name": "Pittsburgh", "gender": "M", "year": 2025}'
```

---

## Project Structure

```
swim-scoring-lineup/
├── app.py                  # Application factory, logging, error handlers
├── config.py               # Environment-based configuration
├── extensions.py           # Shared Flask extensions (db, login, cache)
├── models.py               # SQLAlchemy models (User, Team, Swimmer, Event, Time)
├── forms.py                # Flask-WTF forms with password validation
├── routes.py               # Route handlers + scoring engine
├── api.py                  # REST API blueprint
├── swimcloud_scraper.py    # SwimCloud JSON API scraper
├── requirements.txt        # Python dependencies
├── Dockerfile              # Multi-stage production build
├── docker-compose.yml      # Local dev orchestration
├── .dockerignore           # Docker build exclusions
├── .env.example            # Template for environment variables
├── .gitignore              # Git exclusions
├── tests/
│   ├── __init__.py
│   └── test_app.py         # 25 pytest tests
├── templates/
│   ├── base.html           # Shared layout, nav, CSS design system
│   ├── login.html          # Sign-in page
│   ├── register.html       # Account creation with password strength
│   ├── scrape.html         # Data import / test-data generation
│   ├── select.html         # Dashboard with results + pagination
│   └── errors/
│       ├── 404.html        # Not Found error page
│       └── 500.html        # Server Error page
└── README.md
```

---

## Tradeoffs & Future Improvements

| Area | Current State | Future Enhancement |
|------|--------------|-------------------|
| **Database** | SQLite (file-based, zero-config) | PostgreSQL for multi-user production deployment |
| **Scraping** | Synchronous, event-by-event | Async (aiohttp) or thread-pool for 3-5x faster imports |
| **Auth** | Username + hashed password with strength validation | OAuth / SSO integration |
| **Relay optimization** | Brute-force top-10 candidates per stroke | Optimal assignment via the Hungarian algorithm for very large rosters |
| **Caching** | In-memory SimpleCache | Redis for distributed/persistent caching |
| **Frontend** | Server-rendered Jinja2 with client-side pagination | React/Vue SPA for richer interactivity |
| **CI/CD** | Local pytest | GitHub Actions pipeline with coverage reporting |

---

## License

This project was built as a personal portfolio piece. Feel free to reference
it for educational purposes.
