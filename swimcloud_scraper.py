"""
swimcloud_scraper  --  Import swim data from SwimCloud.

Public functions:

* :func:`search_teams`       -- fuzzy team search by name (returns ID + metadata).
* :func:`get_team_times`     -- bulk import: best time per swimmer per event via API.
* :func:`season_label`       -- human-readable label for a season year.
"""

import logging

import requests

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/json, text/html, */*",
    "Referer": "https://www.swimcloud.com/",
}

_API_BASE = "https://www.swimcloud.com"

# SwimCloud API event codes use the format "stroke|distance|course"
# where stroke: 1=Free, 2=Back, 3=Breast, 4=Fly, 5=IM  and  course: 1=SCY
INDIVIDUAL_EVENT_CODES = {
    "1|50|1":    "50 Free",
    "1|100|1":   "100 Free",
    "1|200|1":   "200 Free",
    "1|500|1":   "500 Free",
    "1|1000|1":  "1000 Free",
    "1|1650|1":  "1650 Free",
    "2|100|1":   "100 Back",
    "2|200|1":   "200 Back",
    "3|100|1":   "100 Breast",
    "3|200|1":   "200 Breast",
    "4|100|1":   "100 Fly",
    "4|200|1":   "200 Fly",
    "5|200|1":   "200 IM",
    "5|400|1":   "400 IM",
}


# ── Internal helpers ─────────────────────────────────────────────────────────

def _fetch_json(url, params=None, timeout=15):
    """GET *url* and return the decoded JSON body."""
    resp = requests.get(url, headers=_HEADERS, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _year_to_season_id(year):
    """Convert a user-facing year to SwimCloud's internal season_id.

    The user enters the *ending* year of the academic season:
    ``2025`` means the 2024-2025 season.  SwimCloud's formula is
    ``season_id = start_year - 1996``, so ``2024 - 1996 = 28``.
    """
    return (year - 1) - 1996


def season_label(year):
    """Return a human-readable season string, e.g. ``'2024-2025'``."""
    return f"{year - 1}-{year}"


# ── Team search ──────────────────────────────────────────────────────────────

def search_teams(query):
    """Search SwimCloud for teams whose name matches *query*.

    Returns a list of dicts sorted by relevance::

        [{"name": "University of Pittsburgh", "abbr": "Pittsburgh", "id": 405}, ...]
    """
    data = _fetch_json(f"{_API_BASE}/api/search/?q={query}&types=team")
    teams = []
    for item in data:
        team_url = item.get("url", "")
        try:
            tid = int(team_url.strip("/").split("/")[-1])
        except (ValueError, IndexError):
            continue
        teams.append({
            "name": item.get("name", ""),
            "abbr": item.get("abbr", ""),
            "id": tid,
        })
    return teams


# ── Bulk team times (event-by-event via API) ─────────────────────────────────

def _fetch_event_times(team_id, gender, season_id, event_code):
    """Fetch one page of best times for a single event from the API.

    Uses ``dont_group=false`` so the API returns **one row per swimmer**
    (their season-best time for that event).
    """
    params = {
        "team_id": team_id,
        "event": event_code,
        "event_course": "Y",
        "gender": gender,
        "season_id": season_id,
        "page": 1,
        "dont_group": "false",
    }
    data = _fetch_json(
        f"{_API_BASE}/api/splashes/top_times/",
        params=params,
    )
    results = []
    for entry in data.get("results", []):
        swimmer_id = entry.get("swimmer_id")
        display_name = entry.get("display_name", "")
        event_time = entry.get("eventtime")
        if not swimmer_id or not display_name or event_time is None:
            continue
        results.append({
            "swimmer_name": display_name,
            "swimmer_id": swimmer_id,
            "time_secs": float(event_time),
        })
    return results


def get_team_times(team_id, gender, year):
    """Import best times for every individual SCY event for a team + season.

    Iterates over all 14 individual events and calls SwimCloud's
    ``/api/splashes/top_times/`` endpoint for each.  The API is called with
    ``dont_group=false`` which returns exactly **one row per swimmer** (their
    season-best time), already sorted fastest-first.

    Returns::

        {
            "50 Free":  [{"swimmer_name": "...", "swimmer_id": 123, "time_secs": 20.83}, ...],
            "100 Free": [...],
            ...
        }

    Also collects a de-duplicated roster dict (swimmer_id -> swimmer_name)
    from all events combined, available via the second return value.

    Returns a tuple ``(events_dict, roster_dict)``.
    """
    season_id = _year_to_season_id(year)
    all_events = {}
    roster = {}

    for event_code, event_name in INDIVIDUAL_EVENT_CODES.items():
        try:
            times = _fetch_event_times(team_id, gender, season_id, event_code)
        except Exception as e:
            log.warning("Failed to fetch %s for team %s: %s", event_name, team_id, e)
            continue

        all_events[event_name] = times
        for t in times:
            roster[t["swimmer_id"]] = t["swimmer_name"]

        log.info(
            "  %s: %d swimmers",
            event_name, len(times),
        )

    log.info(
        "Team %s (%s %s): %d events, %d unique swimmers",
        team_id, gender, year, len(all_events), len(roster),
    )
    return all_events, roster
