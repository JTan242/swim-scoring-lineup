"""
REST API blueprint for programmatic access to swim data.

Endpoints:

* ``GET  /api/teams``                -- list all teams
* ``GET  /api/teams/:id/swimmers``   -- swimmers on a team
* ``GET  /api/events``               -- list all events
* ``GET  /api/results``              -- query times with filters
* ``POST /api/import``               -- trigger a SwimCloud import
"""

import logging

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user

from extensions import db, cache
from models import Team, Swimmer, Event, Time
import swimcloud_scraper as sc

log = logging.getLogger(__name__)

api_bp = Blueprint("api", __name__)


# ── Teams ─────────────────────────────────────────────────────────────────────

@api_bp.route("/teams")
@login_required
@cache.cached(timeout=120, query_string=True)
def list_teams():
    """Return all teams with swimmer counts."""
    teams = Team.query.order_by(Team.name).all()
    return jsonify([
        {
            "id": t.id,
            "name": t.name,
            "swimmer_count": len(t.swimmers),
        }
        for t in teams
    ])


@api_bp.route("/teams/<int:team_id>/swimmers")
@login_required
@cache.cached(timeout=120, query_string=True)
def team_swimmers(team_id):
    """Return swimmers for a given team, optionally filtered by gender."""
    team = Team.query.get_or_404(team_id)
    gender = request.args.get("gender")

    q = Swimmer.query.filter_by(team_id=team.id)
    if gender:
        q = q.filter_by(gender=gender)
    swimmers = q.order_by(Swimmer.name).all()

    return jsonify([
        {"id": s.id, "name": s.name, "gender": s.gender}
        for s in swimmers
    ])


# ── Events ────────────────────────────────────────────────────────────────────

@api_bp.route("/events")
@login_required
@cache.cached(timeout=300)
def list_events():
    """Return all known events."""
    events = Event.query.order_by(Event.name).all()
    return jsonify([
        {"id": e.id, "name": e.name, "course": e.course}
        for e in events
    ])


# ── Results ───────────────────────────────────────────────────────────────────

@api_bp.route("/results")
@login_required
def query_results():
    """Query times with optional filters.

    Query params: ``team_id``, ``event_id``, ``season``, ``gender``, ``limit`` (default 50).
    """
    team_id = request.args.get("team_id", type=int)
    event_id = request.args.get("event_id", type=int)
    season = request.args.get("season", type=int)
    gender = request.args.get("gender")
    limit = request.args.get("limit", 50, type=int)
    limit = min(limit, 200)

    q = (
        db.session.query(Time, Swimmer.name, Team.name, Event.name)
        .join(Time.swimmer)
        .join(Swimmer.team)
        .join(Time.event)
    )

    if team_id:
        q = q.filter(Team.id == team_id)
    if event_id:
        q = q.filter(Time.event_id == event_id)
    if season:
        q = q.filter(Time.season_year == season)
    if gender:
        q = q.filter(Swimmer.gender == gender)

    rows = q.order_by(Time.time_secs).limit(limit).all()

    from routes import format_time

    return jsonify([
        {
            "swimmer": swimmer_name,
            "team": team_name,
            "event": event_name,
            "time_secs": float(t.time_secs),
            "time_formatted": format_time(t.time_secs),
            "season": t.season_year,
        }
        for t, swimmer_name, team_name, event_name in rows
    ])


# ── Import ────────────────────────────────────────────────────────────────────

@api_bp.route("/import", methods=["POST"])
@login_required
def api_import():
    """Trigger a SwimCloud import via the API.

    JSON body: ``{"team_name": "Pittsburgh", "gender": "M", "year": 2025}``
    """
    data = request.get_json(silent=True) or {}
    team_name = data.get("team_name", "").strip()
    gender = data.get("gender", "M")
    year = data.get("year")

    if not team_name or not year:
        return jsonify(error="team_name and year are required"), 400

    try:
        year = int(year)
    except (ValueError, TypeError):
        return jsonify(error="year must be an integer"), 400

    try:
        matches = sc.search_teams(team_name)
    except Exception as e:
        log.error("Team search failed: %s", e)
        return jsonify(error=f"Search failed: {e}"), 502

    if not matches:
        return jsonify(error=f'No teams found for "{team_name}"'), 404

    match = matches[0]
    sc_team_id = match["id"]
    sc_team_name = match["name"]

    try:
        events_data, roster_map = sc.get_team_times(
            team_id=sc_team_id, gender=gender, year=year,
        )
    except Exception as e:
        log.error("Import failed: %s", e)
        return jsonify(error=f"Import failed: {e}"), 502

    from routes import get_or_create_event

    team = Team.query.filter_by(name=sc_team_name).first() or Team(name=sc_team_name)
    db.session.add(team)
    db.session.flush()

    swimmer_cache = {}
    times_count = 0

    for event_name, entries in events_data.items():
        evobj = get_or_create_event(event_name)
        for entry in entries:
            sc_id = entry["swimmer_id"]
            name = entry["swimmer_name"]
            secs = entry["time_secs"]

            if sc_id not in swimmer_cache:
                swimmer_cache[sc_id] = (
                    Swimmer.query.filter_by(name=name, team_id=team.id).first()
                    or Swimmer(name=name, gender=gender, team_id=team.id)
                )
                db.session.add(swimmer_cache[sc_id])
                db.session.flush()

            swimmer = swimmer_cache[sc_id]
            existing = Time.query.filter_by(
                swimmer_id=swimmer.id,
                event_id=evobj.id,
                season_year=year,
            ).first()

            if existing:
                if secs < float(existing.time_secs):
                    existing.time_secs = secs
                    times_count += 1
                continue

            db.session.add(Time(
                swimmer_id=swimmer.id, event_id=evobj.id,
                time_secs=secs, season_year=year,
            ))
            times_count += 1

    db.session.commit()
    cache.clear()

    return jsonify(
        team=sc_team_name,
        season=sc.season_label(year),
        swimmers_imported=len(swimmer_cache),
        times_imported=times_count,
        events=len(events_data),
    ), 201
