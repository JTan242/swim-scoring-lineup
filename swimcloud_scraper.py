# Pull roster + best times from SwimCloud. search_teams(name), get_team_times(team_id, gender, year), season_label(year).

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

# API codes: stroke|distance|course — stroke 1=Free,2=Back,3=Breast,4=Fly,5=IM; course 1=SCY
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


def _fetch_json(url, params=None, timeout=15):
    """GET url, return JSON."""
    resp = requests.get(url, headers=_HEADERS, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _year_to_season_id(year):
    """User types 2025 (season 2024-25). SwimCloud wants season_id = (year-1) - 1996."""
    return (year - 1) - 1996


def season_label(year):
    """e.g. 2025 -> '2024-2025'."""
    return f"{year - 1}-{year}"


def search_teams(query):
    """Fuzzy search by team name. Returns list of {name, abbr, id}."""
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


def _fetch_event_times(team_id, gender, season_id, event_code):
    """One event's top times; dont_group=false gives one row per swimmer (season best)."""
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
    """Hit top_times for all 14 SCY events. Returns (events_dict, roster_dict). events_dict is name -> list of {swimmer_name, swimmer_id, time_secs}."""
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
