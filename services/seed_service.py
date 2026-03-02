# Generates synthetic Pitt / Penn State data for local dev and testing.

import datetime
import random

from extensions import db, cache
from models import Team, Swimmer, Time
from services.import_service import get_or_create_event, link_team_season_to_user

_MALE_FIRST = [
    'James', 'Michael', 'Ryan', 'Andrew', 'David', 'Tyler', 'Matt', 'Nick',
    'Chris', 'Jake', 'Ethan', 'Ben', 'Luke', 'Sam', 'Connor', 'Dylan',
    'Jack', 'Noah', 'Owen', 'Daniel', 'Liam', 'Will', 'Alex', 'Caleb',
    'Kyle', 'Cole', 'Ian', 'Josh', 'Nathan', 'Sean', 'Brody', 'Garrett',
]
_FEMALE_FIRST = [
    'Emma', 'Olivia', 'Sophia', 'Ava', 'Isabella', 'Mia', 'Abigail', 'Emily',
    'Harper', 'Ella', 'Grace', 'Madison', 'Chloe', 'Riley', 'Lily', 'Natalie',
    'Hannah', 'Claire', 'Zoe', 'Leah', 'Sydney', 'Kate', 'Anna', 'Morgan',
    'Lauren', 'Julia', 'Taylor', 'Brooke', 'Paige', 'Rachel', 'Megan', 'Sarah',
]
_LAST = [
    'Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia', 'Miller', 'Davis',
    'Rodriguez', 'Martinez', 'Anderson', 'Taylor', 'Thomas', 'Jackson', 'White',
    'Harris', 'Clark', 'Lewis', 'Robinson', 'Walker', 'Young', 'Allen', 'King',
    'Wright', 'Scott', 'Green', 'Baker', 'Nelson', 'Carter', 'Mitchell', 'Turner',
]
_BASE_TIMES = {
    '50 Free':    (19.5, 22.8),  '100 Free':   (43.0,  50.5),
    '200 Free':   (98.0, 112.0), '500 Free':   (265.0, 305.0),
    '1000 Free':  (560.0, 630.0),'1650 Free':  (930.0, 1050.0),
    '100 Back':   (47.0,  55.0), '200 Back':   (105.0, 120.0),
    '100 Breast': (52.0,  62.0), '200 Breast': (117.0, 135.0),
    '100 Fly':    (47.0,  54.0), '200 Fly':    (108.0, 125.0),
    '200 IM':     (110.0, 125.0),'400 IM':     (240.0, 275.0),
}
_MEETS = [
    'Fall Invitational', 'Dual Meet', 'Conference Champs',
    'Mid-Season Classic', 'Sprint Invitational',
]
_TEAMS = [('Pitt Panthers', 2025), ('Penn State Lions', 2025)]


def seed_teams(user_id: int) -> tuple:
    """Create synthetic Pitt and Penn State rosters.

    Returns:
        (swimmers_created, times_created, teams_count)
    """
    swimmers_created = times_created = 0

    for team_name, year in _TEAMS:
        team = Team.query.filter_by(name=team_name).first() or Team(name=team_name)
        db.session.add(team)
        db.session.flush()
        link_team_season_to_user(team.id, year, user_id)

        for gender in ('M', 'F'):
            firsts = _MALE_FIRST if gender == 'M' else _FEMALE_FIRST
            used_names: set = set()
            for _ in range(random.randint(18, 24)):
                while True:
                    name = f"{random.choice(firsts)} {random.choice(_LAST)}"
                    if name not in used_names:
                        used_names.add(name)
                        break
                swimmer = (
                    Swimmer.query.filter_by(name=name, team_id=team.id).first()
                    or Swimmer(name=name, gender=gender, team_id=team.id)
                )
                db.session.add(swimmer)
                db.session.flush()
                swimmers_created += 1

                for ev_name in random.sample(list(_BASE_TIMES), random.randint(3, 8)):
                    lo, hi = _BASE_TIMES[ev_name]
                    if gender == 'F':
                        lo, hi = lo * 1.07, hi * 1.07
                    evobj = get_or_create_event(ev_name)
                    db.session.add(Time(
                        swimmer_id=swimmer.id,
                        event_id=evobj.id,
                        time_secs=round(random.uniform(lo, hi), 2),
                        meet=random.choice(_MEETS),
                        date=datetime.date(year, random.randint(10, 12), random.randint(1, 28)),
                        season_year=year,
                    ))
                    times_created += 1

    db.session.commit()
    cache.clear()
    return swimmers_created, times_created, len(_TEAMS)
