# Shared team import logic used by both the UI (/scrape) and REST (/api/import).
# Accepts user_id explicitly so it has no Flask request-context dependency.

import logging
from dataclasses import dataclass

from extensions import db, cache
from models import Team, Swimmer, Event, Time, user_team_seasons
import swimcloud_scraper as sc

log = logging.getLogger(__name__)


@dataclass
class ImportResult:
    team_name: str
    season_label: str
    swimmer_count: int
    times_count: int
    event_count: int
    already_existed: bool


def get_or_create_event(name, course='Y'):
    ev = Event.query.filter_by(name=name, course=course).first()
    if not ev:
        ev = Event(name=name, course=course)
        db.session.add(ev)
        db.session.flush()
    return ev


def link_team_season_to_user(team_id, season_year, user_id):
    """Associate a team-season with a user (idempotent)."""
    exists = db.session.execute(
        user_team_seasons.select().where(
            (user_team_seasons.c.user_id == user_id) &
            (user_team_seasons.c.team_id == team_id) &
            (user_team_seasons.c.season_year == season_year)
        )
    ).first()
    if not exists:
        db.session.execute(
            user_team_seasons.insert().values(
                user_id=user_id,
                team_id=team_id,
                season_year=season_year,
            )
        )


def import_team(sc_team_id: int, sc_team_name: str, gender: str, year: int, user_id: int) -> ImportResult:
    """Import all SCY times for a team/gender/year from SwimCloud into the DB.

    If data already exists, links it to the user and returns early.

    Raises:
        LookupError: if no swimmers found for the season
        Exception: on SwimCloud API errors (propagated to caller)
    """
    season = sc.season_label(year)

    existing_team = Team.query.filter_by(name=sc_team_name).first()
    if existing_team:
        has_data = (
            db.session.query(Time.id)
            .join(Time.swimmer)
            .filter(
                Swimmer.team_id == existing_team.id,
                Swimmer.gender == gender,
                Time.season_year == year,
            )
            .first()
        )
        if has_data:
            link_team_season_to_user(existing_team.id, year, user_id)
            db.session.commit()
            cache.clear()
            sw_count = Swimmer.query.filter_by(
                team_id=existing_team.id, gender=gender,
            ).count()
            t_count = (
                db.session.query(Time)
                .join(Time.swimmer)
                .filter(
                    Swimmer.team_id == existing_team.id,
                    Swimmer.gender == gender,
                    Time.season_year == year,
                )
                .count()
            )
            return ImportResult(sc_team_name, season, sw_count, t_count, 0, already_existed=True)

    events_data, roster_map = sc.get_team_times(
        team_id=sc_team_id, gender=gender, year=year,
    )

    if not roster_map:
        raise LookupError(f"No swimmers found for the {season} season.")

    team = existing_team or Team(name=sc_team_name)
    db.session.add(team)
    db.session.flush()

    swimmer_cache = {}
    times_count = 0

    for event_name, entries in events_data.items():
        evobj = get_or_create_event(event_name)
        for entry in entries:
            sc_id = entry['swimmer_id']
            name  = entry['swimmer_name']
            secs  = entry['time_secs']

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

    link_team_season_to_user(team.id, year, user_id)
    db.session.commit()
    cache.clear()
    log.info(
        "Imported %d swimmers, %d times for %s (%s)",
        len(swimmer_cache), times_count, sc_team_name, season,
    )
    return ImportResult(sc_team_name, season, len(swimmer_cache), times_count, len(events_data), already_existed=False)
