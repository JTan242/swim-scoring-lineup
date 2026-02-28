"""
Route handlers and scoring logic for the swim lineup optimizer.

This module contains:
* **Authentication** -- register, login, logout.
* **Data import** -- scrape real data from SwimCloud or generate test data.
* **Dashboard / scoring** -- select team-seasons and events, compute optimal
  relay lineups (greedy and scored modes), and export results to Excel.

Scoring tables follow NCAA Division I dual-meet rules.
"""

import datetime
import random
from io import BytesIO
from itertools import permutations

import pandas as pd
from openpyxl.utils import get_column_letter
from flask import (
    Blueprint, render_template, request,
    flash, redirect, url_for, current_app, Response,
)
from flask_login import login_user, logout_user, login_required

from extensions import db, login_manager, cache
from models import User, Team, Swimmer, Event, Time
from forms import LoginForm, RegistrationForm, ScrapeForm, SelectionForm
import swimcloud_scraper as sc

log = __import__('logging').getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

INDIV_SCORE = [20, 17, 16, 15, 14, 13, 12, 11, 9, 7, 6, 5, 4, 3, 2, 1]
RELAY_SCORE = [40, 34, 32, 30, 28, 26, 24, 22, 20, 16, 12, 10, 8, 6, 4, 2]

INDIVIDUAL_EVENTS_ORDER = [
    '50 Free', '100 Free', '200 Free', '500 Free', '1000 Free', '1650 Free',
    '100 Back', '200 Back',
    '100 Breast', '200 Breast',
    '100 Fly', '200 Fly',
    '200 IM', '400 IM',
]
INDIVIDUAL_EVENTS = set(INDIVIDUAL_EVENTS_ORDER)
RELAYS = {
    'relay_200_free': 50,
    'relay_400_free': 100,
    'relay_800_free': 200,
    'relay_medley':   None,
}
MEDLEY_STROKES = ['Back', 'Breast', 'Fly', 'Free']

main = Blueprint('main', __name__)


# ─── Generic helpers ──────────────────────────────────────────────────────────

@login_manager.user_loader
def load_user(uid):
    return User.query.get(int(uid))


def format_time(secs):
    m, s = divmod(float(secs), 60)
    return f"{int(m)}:{s:05.2f}"


def score_for(table, rank):
    return table[rank - 1] if rank <= len(table) else 0


def parse_time_to_seconds(ts: str) -> float:
    parts = ts.split(':')
    if len(parts) == 1:
        return float(parts[0])
    return int(parts[0]) * 60 + float(parts[1])


def get_or_create_event(name, course='Y'):
    ev = Event.query.filter_by(name=name, course=course).first()
    if not ev:
        ev = Event(name=name, course=course)
        db.session.add(ev)
        db.session.flush()
    return ev


def _render_select(form, swimmers=None, excluded=None):
    return render_template(
        'select.html', form=form,
        swimmers=swimmers or [],
        excluded=excluded or set(),
        RELAYS=RELAYS,
    )


def _team_season_pairs():
    return (
        db.session.query(Team.id, Team.name, Time.season_year)
        .join(Team.swimmers).join(Swimmer.times)
        .distinct()
        .order_by(Time.season_year.desc(), Team.name)
        .all()
    )


def _parse_selected(selected):
    team_ids = [int(x.split(':')[0]) for x in selected]
    seasons  = [int(x.split(':')[1]) for x in selected]
    return team_ids, seasons


# ─── Relay pool builders ─────────────────────────────────────────────────────

def _build_free_pool(tid, yr, dist, excluded):
    rows = (
        db.session.query(Time, Swimmer.id, Swimmer.name)
        .join(Time.swimmer)
        .filter(
            Swimmer.team_id == tid,
            Time.season_year == yr,
            Time.event.has(name=f"{dist} Free", course='Y'),
        )
        .order_by(Time.time_secs)
        .all()
    )
    return [
        {'time_id': tr.id, 'swimmer_id': sid, 'name': nm,
         'time': float(tr.time_secs), 'stroke': 'Free'}
        for tr, sid, nm in rows if tr.id not in excluded
    ]


def _build_medley_pools(tid, yr, excluded):
    """Return per-stroke pools: ``{stroke: [{swimmer_id, name, time, time_id}, ...]}``."""
    stroke_pools = {}
    for stroke in MEDLEY_STROKES:
        rows = (
            db.session.query(Time, Swimmer.id, Swimmer.name)
            .join(Time.swimmer)
            .filter(
                Swimmer.team_id == tid,
                Time.season_year == yr,
                Time.event.has(name=f"100 {stroke}", course='Y'),
            )
            .order_by(Time.time_secs)
            .all()
        )
        stroke_pools[stroke] = [
            {'swimmer_id': sid, 'name': nm,
             'time': float(tr.time_secs), 'time_id': tr.id}
            for tr, sid, nm in rows if tr.id not in excluded
        ]
    return stroke_pools


def build_all_pools(pairs, selected, relay_key, excluded):
    pools = {}
    for tid, _tname, yr in pairs:
        key = f"{tid}:{yr}"
        if key not in selected:
            continue
        if relay_key == 'relay_medley':
            pools[key] = _build_medley_pools(tid, yr, excluded)
        else:
            pools[key] = _build_free_pool(tid, yr, RELAYS[relay_key], excluded)
    return pools


# ─── Relay squad assembly ────────────────────────────────────────────────────

def _best_medley_assignment(stroke_pools, used_ids=None):
    """Brute-force the fastest 4-swimmer medley assignment.

    *stroke_pools* is ``{stroke: [entry, ...]}``.  Each entry needs only a
    time in that one stroke.  Returns ``(total_secs, legs_list)`` or None.
    """
    if used_ids is None:
        used_ids = set()

    filtered = {}
    for stroke in MEDLEY_STROKES:
        candidates = [e for e in stroke_pools.get(stroke, [])
                       if e['swimmer_id'] not in used_ids]
        if not candidates:
            return None
        filtered[stroke] = candidates[:10]

    best = None
    for b in filtered['Back']:
        for br in filtered['Breast']:
            if br['swimmer_id'] == b['swimmer_id']:
                continue
            for fl in filtered['Fly']:
                if fl['swimmer_id'] in (b['swimmer_id'], br['swimmer_id']):
                    continue
                for fr in filtered['Free']:
                    if fr['swimmer_id'] in (b['swimmer_id'], br['swimmer_id'],
                                             fl['swimmer_id']):
                        continue
                    tot = b['time'] + br['time'] + fl['time'] + fr['time']
                    if best is None or tot < best[0]:
                        best = (tot, [
                            {**b,  'stroke': 'Back'},
                            {**br, 'stroke': 'Breast'},
                            {**fl, 'stroke': 'Fly'},
                            {**fr, 'stroke': 'Free'},
                        ])
    return best


def pick_greedy_squads(pool, relay_key, top_n):
    """Repeatedly pick the fastest 4 swimmers, remove them, repeat."""
    if relay_key == 'relay_medley':
        stroke_pools = pool
        squads = []
        used = set()
        for _ in range(top_n):
            result = _best_medley_assignment(stroke_pools, used)
            if result is None:
                break
            total, legs = result
            squads.append({'leg': legs, 'time': total})
            used.update(leg['swimmer_id'] for leg in legs)
        return squads

    squads, temp = [], pool[:]
    while len(temp) >= 4 and len(squads) < top_n:
        best4 = sorted(temp, key=lambda x: x['time'])[:4]
        squads.append({'leg': best4, 'time': sum(x['time'] for x in best4)})
        used = {s['swimmer_id'] for s in best4}
        temp = [s for s in temp if s['swimmer_id'] not in used]
    return squads


def pick_scored_combos(pool, relay_key):
    """Pick A (and optionally B) relay from one team's pool for scored mode."""
    combos = []
    if relay_key != 'relay_medley':
        sp = sorted(pool, key=lambda x: x['time'])
        if len(sp) >= 4:
            combos.append({
                'leg': sp[:4], 'type': 'A',
                'time': sum(x['time'] for x in sp[:4]),
            })
        if len(sp) >= 8:
            combos.append({
                'leg': sp[4:8], 'type': 'B',
                'time': sum(x['time'] for x in sp[4:8]),
            })
    else:
        stroke_pools = pool
        best = _best_medley_assignment(stroke_pools)
        if best:
            t1, legs1 = best
            combos.append({'leg': legs1, 'time': t1, 'type': 'A'})
            used = {leg['swimmer_id'] for leg in legs1}
            best2 = _best_medley_assignment(stroke_pools, used)
            if best2:
                t2, legs2 = best2
                combos.append({'leg': legs2, 'time': t2, 'type': 'B'})
    return combos


def rank_scored_combos(all_combos, top_n):
    a = sorted([c for c in all_combos if c['type'] == 'A'], key=lambda x: x['time'])[:8]
    b = sorted([c for c in all_combos if c['type'] == 'B'], key=lambda x: x['time'])[:8]
    return (a + b)[:top_n]


def _attach_team_info(squads, key, choices_map):
    label, season = choices_map[key], int(key.split(':')[1])
    for sq in squads:
        sq['team'], sq['season'] = label, season


# ─── Result formatters ───────────────────────────────────────────────────────

def squads_to_display_rows(squads, score_table=None):
    """Flatten ranked squads into the dicts expected by select.html."""
    rows = []
    for rank, squad in enumerate(squads, start=1):
        pts = score_for(score_table, rank) if score_table else None
        combo_fmt = format_time(squad['time'])
        for leg in squad['leg']:
            rows.append({
                'time_id':        leg['time_id'],
                'combo_rank':     rank,
                'team':           squad['team'],
                'season':         squad['season'],
                'stroke':         leg.get('stroke', 'Free'),
                'swimmer_id':     leg['swimmer_id'],
                'name':           leg['name'],
                'time':           leg['time'],
                'time_fmt':       format_time(leg['time']),
                'combo_time':     squad['time'],
                'combo_time_fmt': combo_fmt,
                'points':         pts,
            })
    return rows


def _squads_to_excel_rows(squads, score_table=None):
    rows = []
    for rank, squad in enumerate(squads, start=1):
        pts = score_for(score_table, rank) if score_table else 0
        for leg in squad['leg']:
            row = {
                'Relay #':     rank,
                'Team/Season': squad['team'],
                'Swimmer':     leg['name'],
                'Stroke':      leg.get('stroke', 'Free'),
                'Split':       format_time(leg['time']),
                'Relay Time':  format_time(squad['time']),
            }
            if score_table is not None:
                row['Points'] = pts
            rows.append(row)
    return rows


# ─── Excel export ─────────────────────────────────────────────────────────────

def _write_sheet(writer, name, df):
    name = name[:31]
    df.to_excel(writer, sheet_name=name, index=False)
    if df.empty:
        return
    ws = writer.sheets[name]
    for ci, col in enumerate(df.columns):
        w = max(df[col].astype(str).map(len).max(), len(col)) + 2
        ws.column_dimensions[get_column_letter(ci + 1)].width = w


def build_excel(pairs, selected, team_ids, seasons, excluded, top_n, team_choices):
    choices_map = dict(team_choices)
    output = BytesIO()

    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        for ev_name in INDIVIDUAL_EVENTS_ORDER:
            ev_obj = Event.query.filter_by(name=ev_name, course='Y').first()
            if not ev_obj:
                continue
            rows = (
                db.session.query(
                    Time, Swimmer.name.label('swimmer'),
                    Team.name.label('team'), Time.season_year.label('season'),
                )
                .join(Time.swimmer).join(Swimmer.team)
                .filter(
                    Team.id.in_(team_ids), Time.event_id == ev_obj.id,
                    Time.season_year.in_(seasons), ~Time.id.in_(excluded),
                )
                .order_by(Time.time_secs)
                .all()
            )
            data = [
                {
                    'Team/Season': f"{team} ({season})",
                    'Swimmer':     swimmer,
                    'Time':        format_time(t.time_secs),
                    'Points':      score_for(INDIV_SCORE, i),
                }
                for i, (t, swimmer, team, season) in enumerate(rows, start=1)
            ]
            _write_sheet(writer, ev_name, pd.DataFrame(data))

        for relay_key in RELAYS:
            pools = build_all_pools(pairs, selected, relay_key, excluded)
            label = relay_key.split('_', 1)[1].title()

            all_squads = []
            for key, pool in pools.items():
                squads = pick_greedy_squads(pool, relay_key, top_n)
                _attach_team_info(squads, key, choices_map)
                all_squads.extend(squads)
            all_squads.sort(key=lambda x: x['time'])
            _write_sheet(writer, f"{label} Unscored",
                         pd.DataFrame(_squads_to_excel_rows(all_squads)))

            all_combos = []
            for key, pool in pools.items():
                combos = pick_scored_combos(pool, relay_key)
                _attach_team_info(combos, key, choices_map)
                all_combos.extend(combos)
            ranked = rank_scored_combos(all_combos, top_n)
            _write_sheet(writer, f"{label} Scored",
                         pd.DataFrame(_squads_to_excel_rows(ranked, RELAY_SCORE)))

    output.seek(0)
    return output.getvalue()


# ─── Auth routes ──────────────────────────────────────────────────────────────

@main.route('/')
def index():
    return redirect(url_for('main.login'))


@main.route('/register', methods=['GET', 'POST'])
def register():
    form = RegistrationForm()
    if form.validate_on_submit():
        u = User(username=form.username.data)
        u.set_password(form.password.data)
        db.session.add(u)
        db.session.commit()
        flash('Registered! Log in now.', 'success')
        return redirect(url_for('main.login'))
    return render_template('register.html', form=form)


@main.route('/login', methods=['GET', 'POST'])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        u = User.query.filter_by(username=form.username.data).first()
        if u and u.check_password(form.password.data):
            login_user(u)
            return redirect(url_for('main.scrape'))
        flash('Invalid credentials', 'danger')
    return render_template('login.html', form=form)


@main.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('main.login'))


# ─── Data import / seed ───────────────────────────────────────────────────────

@main.route('/seed', methods=['POST'])
@login_required
def seed_test_data():
    """Populate the DB with two realistic fake teams for testing."""
    MALE_FIRST = [
        'James','Michael','Ryan','Andrew','David','Tyler','Matt','Nick',
        'Chris','Jake','Ethan','Ben','Luke','Sam','Connor','Dylan',
        'Jack','Noah','Owen','Daniel','Liam','Will','Alex','Caleb',
        'Kyle','Cole','Ian','Josh','Nathan','Sean','Brody','Garrett',
    ]
    FEMALE_FIRST = [
        'Emma','Olivia','Sophia','Ava','Isabella','Mia','Abigail','Emily',
        'Harper','Ella','Grace','Madison','Chloe','Riley','Lily','Natalie',
        'Hannah','Claire','Zoe','Leah','Sydney','Kate','Anna','Morgan',
        'Lauren','Julia','Taylor','Brooke','Paige','Rachel','Megan','Sarah',
    ]
    LAST = [
        'Smith','Johnson','Williams','Brown','Jones','Garcia','Miller','Davis',
        'Rodriguez','Martinez','Anderson','Taylor','Thomas','Jackson','White',
        'Harris','Clark','Lewis','Robinson','Walker','Young','Allen','King',
        'Wright','Scott','Green','Baker','Nelson','Carter','Mitchell','Turner',
    ]
    BASE_TIMES = {
        '50 Free': (19.5, 22.8),    '100 Free': (43.0, 50.5),
        '200 Free': (98.0, 112.0),   '500 Free': (265.0, 305.0),
        '1000 Free': (560.0, 630.0), '1650 Free': (930.0, 1050.0),
        '100 Back': (47.0, 55.0),    '200 Back': (105.0, 120.0),
        '100 Breast': (52.0, 62.0),  '200 Breast': (117.0, 135.0),
        '100 Fly': (47.0, 54.0),     '200 Fly': (108.0, 125.0),
        '200 IM': (110.0, 125.0),    '400 IM': (240.0, 275.0),
    }
    MEETS = [
        'Fall Invitational', 'Dual Meet', 'Conference Champs',
        'Mid-Season Classic', 'Sprint Invitational',
    ]
    teams_cfg = [('Pitt Panthers', 2025), ('Penn State Lions', 2025)]

    swimmers_created = times_created = 0
    for team_name, year in teams_cfg:
        team = Team.query.filter_by(name=team_name).first() or Team(name=team_name)
        db.session.add(team)
        db.session.flush()

        for gender in ('M', 'F'):
            firsts = MALE_FIRST if gender == 'M' else FEMALE_FIRST
            used_names = set()
            for _ in range(random.randint(18, 24)):
                while True:
                    name = f"{random.choice(firsts)} {random.choice(LAST)}"
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

                for ev_name in random.sample(list(BASE_TIMES), random.randint(3, 8)):
                    lo, hi = BASE_TIMES[ev_name]
                    if gender == 'F':
                        lo, hi = lo * 1.07, hi * 1.07
                    evobj = get_or_create_event(ev_name)
                    t = Time(
                        swimmer_id=swimmer.id, event_id=evobj.id,
                        time_secs=round(random.uniform(lo, hi), 2),
                        meet=random.choice(MEETS),
                        date=datetime.date(year, random.randint(10, 12),
                                           random.randint(1, 28)),
                        season_year=year,
                    )
                    db.session.add(t)
                    times_created += 1

    db.session.commit()
    cache.clear()
    flash(f"Generated test data: {swimmers_created} swimmers, "
          f"{times_created} times across {len(teams_cfg)} teams.", 'success')
    return redirect(url_for('main.scrape'))


@main.route('/scrape', methods=['GET', 'POST'])
@login_required
def scrape():
    form = ScrapeForm()
    if form.validate_on_submit():
        query  = form.team_name.data.strip()
        gender = form.gender.data
        year   = form.year.data
        season = sc.season_label(year)

        # Look up team ID from name via SwimCloud search
        try:
            matches = sc.search_teams(query)
        except Exception as e:
            flash(f"Error searching for team: {e}", "danger")
            return render_template('scrape.html', form=form)

        if not matches:
            flash(f'No teams found for "{query}". Try a different name.', "warning")
            return render_template('scrape.html', form=form)

        match = matches[0]
        team_id   = match['id']
        team_name = match['name']
        flash(
            f'Matched "{query}" to {team_name}. '
            f'Importing {season} season data\u2026',
            'info',
        )

        try:
            events_data, roster_map = sc.get_team_times(
                team_id=team_id, gender=gender, year=year,
            )
        except Exception as e:
            flash(f"Error fetching team times: {e}", "danger")
            return render_template('scrape.html', form=form)

        if not roster_map:
            flash(f"No swimmers found for the {season} season.", "warning")
            return render_template('scrape.html', form=form)

        team = Team.query.filter_by(name=team_name).first() or Team(name=team_name)
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

        db.session.commit()
        cache.clear()
        log.info(
            "Imported %d swimmers, %d times for %s (%s)",
            len(swimmer_cache), times_count, team_name, season,
        )
        flash(
            f"Imported {len(swimmer_cache)} swimmers and {times_count} best times "
            f"across {len(events_data)} events for the {season} season.",
            'success',
        )
        return render_template('scrape.html', form=form,
                               swimmers_count=len(swimmer_cache),
                               times_count=times_count)
    return render_template('scrape.html', form=form)


# ─── Dashboard & scoring ─────────────────────────────────────────────────────

@main.route('/select', methods=['GET', 'POST'])
@login_required
def select():
    form = SelectionForm()
    pairs = _team_season_pairs()
    form.teams.choices = [
        (f"{tid}:{yr}", f"{yr} {tname}") for tid, tname, yr in pairs
    ]

    # Handle remove action
    if request.method == 'POST' and 'remove_ts' in request.form:
        selected = request.form.getlist('teams')
        if not selected:
            flash("Please check at least one Team-Season to remove.", "warning")
            return redirect(url_for('main.select'))
        removed = []
        for ts in selected:
            tid, yr = int(ts.split(':')[0]), int(ts.split(':')[1])
            team = Team.query.get(tid)
            removed.append(f"{team.name} ({yr})")
            Time.query.filter(
                Time.season_year == yr,
                Time.swimmer.has(team_id=tid),
            ).delete(synchronize_session=False)
        db.session.commit()
        cache.clear()
        flash(f"Removed data for: {', '.join(removed)}.", "success")
        return redirect(url_for('main.select'))

    # Build event dropdown
    relay_opts = [
        (key, f"{' '.join(key.split('_')[1:]).title()} Relay")
        for key in RELAYS
    ]
    event_map = {
        e.name: str(e.id)
        for e in Event.query.filter(Event.name.in_(INDIVIDUAL_EVENTS)).all()
    }
    indiv_opts = [
        (event_map[name], name)
        for name in INDIVIDUAL_EVENTS_ORDER
        if name in event_map
    ]
    form.event.choices = indiv_opts + relay_opts

    if request.method != 'POST':
        return _render_select(form)

    # ─── Parse POST data ─────────────────────────────────────────────────
    existing_excl = {int(x) for x in request.form.getlist('excluded') if x.isdigit()}
    raw_ids = [int(x) for x in request.form.getlist('time_id') if x.isdigit()]
    ev = request.form['event']

    if ev in RELAYS:
        kept = {int(x) for x in request.form.getlist('include_time_id')
                if x.isdigit()}
    else:
        kept = {raw_ids[i] for i in range(len(raw_ids))
                if request.form.get(f'include_{i}')}
    excluded = existing_excl | (set(raw_ids) - kept)

    selected = request.form.getlist('teams')
    if not selected:
        flash("Select at least one Team-Season.", "danger")
        return _render_select(form, excluded=excluded)

    form.teams.data        = selected
    form.event.data        = ev
    form.top_n.data        = top_n = int(request.form['top_n'])
    form.scoring_mode.data = scoring = request.form.get('scoring_mode', 'unscored')
    team_ids, seasons = _parse_selected(selected)
    choices_map = dict(form.teams.choices)

    # ─── Excel export (all events, regardless of selection) ───────────
    if 'export_excel' in request.form:
        data = build_excel(
            pairs, selected, team_ids, seasons,
            excluded, top_n, form.teams.choices,
        )
        return Response(
            data,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition': 'attachment;filename=swim_results.xlsx'},
        )

    # ─── Individual event ─────────────────────────────────────────────
    if ev not in RELAYS:
        q = (
            db.session.query(
                Time, Swimmer.name.label('swimmer_name'),
                Team.name.label('team_name'),
                Time.season_year.label('season'),
            )
            .join(Time.swimmer).join(Swimmer.team)
            .filter(
                Team.id.in_(team_ids), Time.event_id == int(ev),
                Time.season_year.in_(seasons), ~Time.id.in_(excluded),
            )
            .order_by(Time.time_secs)
            .limit(top_n * 2)
            .all()
        )
        seen, distinct = set(), []
        for row in q:
            if row.swimmer_name not in seen:
                seen.add(row.swimmer_name)
                distinct.append(row)
                if len(distinct) >= top_n:
                    break
        swimmers = []
        for idx, row in enumerate(distinct, start=1):
            secs = float(row.Time.time_secs)
            t_fmt = format_time(secs)
            swimmers.append({
                'time_id':        row.Time.id,
                'combo_rank':     idx,
                'stroke':         '',
                'swimmer_id':     row.Time.swimmer_id,
                'name':           row.swimmer_name,
                'team':           row.team_name,
                'season':         row.season,
                'time':           secs,
                'time_fmt':       t_fmt,
                'combo_time':     secs,
                'combo_time_fmt': t_fmt,
                'points':         score_for(INDIV_SCORE, idx),
            })
        return _render_select(form, swimmers, excluded)

    # ─── Relay display ────────────────────────────────────────────────
    pools = build_all_pools(pairs, selected, ev, excluded)

    if scoring == 'unscored':
        all_squads = []
        for key, pool in pools.items():
            squads = pick_greedy_squads(pool, ev, top_n)
            _attach_team_info(squads, key, choices_map)
            all_squads.extend(squads)
        all_squads.sort(key=lambda x: x['time'])
        swimmers = squads_to_display_rows(all_squads)
    else:
        all_combos = []
        for key, pool in pools.items():
            combos = pick_scored_combos(pool, ev)
            _attach_team_info(combos, key, choices_map)
            all_combos.extend(combos)
        ranked = rank_scored_combos(all_combos, top_n)
        swimmers = squads_to_display_rows(ranked, score_table=RELAY_SCORE)

    return _render_select(form, swimmers, excluded)
