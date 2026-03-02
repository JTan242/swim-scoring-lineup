# REST API: teams, swimmers, events, results, and POST /api/import for SwimCloud.

import logging

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user

from extensions import db, cache
from models import Team, Swimmer, Event, Time, user_team_seasons
import swimcloud_scraper as sc

from services.scoring import format_time
from services.import_service import import_team

log = logging.getLogger(__name__)

api_bp = Blueprint("api", __name__)


@api_bp.route("/teams")
@login_required
def list_teams():
    """Teams the current user has access to, plus swimmer count."""
    teams = (
        db.session.query(Team)
        .join(user_team_seasons, Team.id == user_team_seasons.c.team_id)
        .filter(user_team_seasons.c.user_id == current_user.id)
        .distinct()
        .order_by(Team.name)
        .all()
    )
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
def team_swimmers(team_id):
    """Swimmers for one team; optional ?gender=M or F. Scoped to current user."""
    has_access = db.session.execute(
        user_team_seasons.select().where(
            (user_team_seasons.c.user_id == current_user.id) &
            (user_team_seasons.c.team_id == team_id)
        )
    ).first()
    if not has_access:
        return jsonify(error="Team not found"), 404

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


@api_bp.route("/events")
@login_required
@cache.cached(timeout=300)
def list_events():
    """All events (name + course)."""
    events = Event.query.order_by(Event.name).all()
    return jsonify([
        {"id": e.id, "name": e.name, "course": e.course}
        for e in events
    ])


@api_bp.route("/results")
@login_required
def query_results():
    """Times with optional filters: team_id, event_id, season, gender, limit (max 200)."""
    team_id  = request.args.get("team_id", type=int)
    event_id = request.args.get("event_id", type=int)
    season   = request.args.get("season", type=int)
    gender   = request.args.get("gender")
    limit    = min(request.args.get("limit", 50, type=int), 200)

    q = (
        db.session.query(Time, Swimmer.name, Team.name, Event.name)
        .join(Time.swimmer)
        .join(Swimmer.team)
        .join(Time.event)
        .join(
            user_team_seasons,
            (Team.id == user_team_seasons.c.team_id) &
            (Time.season_year == user_team_seasons.c.season_year),
        )
        .filter(user_team_seasons.c.user_id == current_user.id)
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


@api_bp.route("/import", methods=["POST"])
@login_required
def api_import():
    """Import from SwimCloud. Body: {"team_name": "...", "gender": "M"|"F", "year": 2025}."""
    data      = request.get_json(silent=True) or {}
    team_name = data.get("team_name", "").strip()
    gender    = data.get("gender", "M")
    year      = data.get("year")

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

    try:
        result = import_team(match["id"], match["name"], gender, year, current_user.id)
    except LookupError as e:
        return jsonify(error=str(e)), 404
    except Exception as e:
        log.error("Import failed: %s", e)
        return jsonify(error=f"Import failed: {e}"), 502

    status = 200 if result.already_existed else 201
    return jsonify(
        team=result.team_name,
        season=result.season_label,
        message="Data already exists \u2014 linked to your account." if result.already_existed else None,
        swimmers_imported=result.swimmer_count,
        times_imported=result.times_count,
        events=result.event_count,
    ), status
