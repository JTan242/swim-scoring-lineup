# Auth, scrape/seed, and dashboard routes for the main blueprint.

from flask import (
    Blueprint, render_template, request,
    flash, redirect, url_for, Response,
)
from flask_login import login_user, logout_user, login_required, current_user

from extensions import db, login_manager, cache
from models import User, Team, Swimmer, Event, Time, user_team_seasons
from forms import LoginForm, RegistrationForm, ScrapeForm, SelectionForm
import swimcloud_scraper as sc

from services.scoring import (
    INDIVIDUAL_EVENTS_ORDER, INDIVIDUAL_EVENTS, RELAYS,
    build_all_pools, query_individual_event, build_relay_view,
)
from services.export_service import build_excel
from services.import_service import import_team
from services.seed_service import seed_teams

main = Blueprint('main', __name__)


@login_manager.user_loader
def load_user(uid):
    return db.session.get(User, int(uid))


def _render_select(form, swimmers=None, excluded=None, pagination=None):
    return render_template(
        'select.html', form=form,
        swimmers=swimmers or [],
        excluded=excluded or set(),
        RELAYS=RELAYS,
        pagination=pagination,
    )


def _team_season_pairs():
    return (
        db.session.query(Team.id, Team.name, user_team_seasons.c.season_year)
        .join(user_team_seasons, Team.id == user_team_seasons.c.team_id)
        .filter(user_team_seasons.c.user_id == current_user.id)
        .order_by(user_team_seasons.c.season_year.desc(), Team.name)
        .all()
    )


def _parse_selected(selected):
    team_ids = [int(x.split(':')[0]) for x in selected]
    seasons  = [int(x.split(':')[1]) for x in selected]
    return team_ids, seasons


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


@main.route('/seed', methods=['POST'])
@login_required
def seed_test_data():
    swimmers, times, teams = seed_teams(current_user.id)
    flash(f"Generated test data: {swimmers} swimmers, {times} times across {teams} teams.", 'success')
    return redirect(url_for('main.scrape'))


@main.route('/scrape', methods=['GET', 'POST'])
@login_required
def scrape():
    form = ScrapeForm()
    if form.validate_on_submit():
        query  = form.team_name.data.strip()
        gender = form.gender.data
        year   = form.year.data

        try:
            matches = sc.search_teams(query)
        except Exception as e:
            flash(f"Error searching for team: {e}", "danger")
            return render_template('scrape.html', form=form)

        if not matches:
            flash(f'No teams found for "{query}". Try a different name.', "warning")
            return render_template('scrape.html', form=form)

        match = matches[0]
        flash(
            f'Matched "{query}" to {match["name"]}. '
            f'Importing {sc.season_label(year)} season data\u2026',
            'info',
        )

        try:
            result = import_team(match['id'], match['name'], gender, year, current_user.id)
        except LookupError as e:
            flash(str(e), "warning")
            return render_template('scrape.html', form=form)
        except Exception as e:
            flash(f"Error fetching team times: {e}", "danger")
            return render_template('scrape.html', form=form)

        if result.already_existed:
            flash(
                f"{result.team_name} ({result.season_label}) data already in the database "
                f"\u2014 added to your dashboard "
                f"({result.swimmer_count} swimmers, {result.times_count} times).",
                "success",
            )
        else:
            flash(
                f"Imported {result.swimmer_count} swimmers and {result.times_count} best times "
                f"across {result.event_count} events for the {result.season_label} season.",
                'success',
            )
        return render_template('scrape.html', form=form,
                               swimmers_count=result.swimmer_count,
                               times_count=result.times_count)
    return render_template('scrape.html', form=form)


@main.route('/select', methods=['GET', 'POST'])
@login_required
def select():
    form = SelectionForm()
    pairs = _team_season_pairs()
    form.teams.choices = [
        (f"{tid}:{yr}", f"{yr} {tname}") for tid, tname, yr in pairs
    ]

    if request.method == 'POST' and 'remove_ts' in request.form:
        selected = request.form.getlist('teams')
        if not selected:
            flash("Please check at least one Team-Season to remove.", "warning")
            return redirect(url_for('main.select'))
        removed = []
        for ts in selected:
            tid, yr = int(ts.split(':')[0]), int(ts.split(':')[1])
            team = db.session.get(Team, tid)
            removed.append(f"{team.name} ({yr})")
            db.session.execute(
                user_team_seasons.delete().where(
                    (user_team_seasons.c.user_id == current_user.id) &
                    (user_team_seasons.c.team_id == tid) &
                    (user_team_seasons.c.season_year == yr)
                )
            )
            db.session.flush()
            other = db.session.execute(
                user_team_seasons.select().where(
                    (user_team_seasons.c.team_id == tid) &
                    (user_team_seasons.c.season_year == yr)
                )
            ).first()
            if not other:
                swimmer_ids = [s.id for s in Swimmer.query.filter_by(team_id=tid).all()]
                if swimmer_ids:
                    Time.query.filter(
                        Time.swimmer_id.in_(swimmer_ids),
                        Time.season_year == yr,
                    ).delete(synchronize_session=False)
                remaining = (
                    db.session.query(Time.id)
                    .join(Time.swimmer)
                    .filter(Swimmer.team_id == tid)
                    .first()
                )
                if not remaining:
                    Swimmer.query.filter_by(team_id=tid).delete(synchronize_session=False)
                    db.session.delete(team)
        db.session.commit()
        cache.clear()
        flash(f"Removed: {', '.join(removed)}.", "success")
        return redirect(url_for('main.select'))

    event_map = {
        e.name: str(e.id)
        for e in Event.query.filter(Event.name.in_(INDIVIDUAL_EVENTS)).all()
    }
    form.event.choices = (
        [(event_map[n], n) for n in INDIVIDUAL_EVENTS_ORDER if n in event_map]
        + [(key, f"{' '.join(key.split('_')[1:]).title()} Relay") for key in RELAYS]
    )
    if not form.event.choices:
        form.event.choices = [(None, '— No events —')]

    if request.method != 'POST':
        if form.event.data is None and form.event.choices and form.event.choices[0][0] is not None:
            form.event.data = form.event.choices[0][0]
        if getattr(form, 'gender', None) is not None and form.gender.data is None:
            form.gender.data = 'M'
        return _render_select(form)

    existing_excl = {int(x) for x in request.form.getlist('excluded') if x.isdigit()}
    raw_ids       = [int(x) for x in request.form.getlist('time_id') if x.isdigit()]
    ev            = request.form['event']
    gender        = request.form.get('gender', 'M')
    if gender not in ('M', 'F'):
        gender = 'M'
    form.gender.data = gender

    kept     = {int(x) for x in request.form.getlist('include_time_id') if x.isdigit()}
    excluded = existing_excl | (set(raw_ids) - kept)

    selected = request.form.getlist('teams')
    if not selected:
        flash("Select at least one Team-Season.", "danger")
        return _render_select(form, excluded=excluded)

    form.teams.data               = selected
    form.event.data               = ev
    form.top_n.data               = top_n = int(request.form['top_n'])
    form.scoring_mode.data        = scoring = request.form.get('scoring_mode', 'unscored')
    raw_rpt                       = request.form.get('max_relays_per_team', '0')
    form.max_relays_per_team.data = raw_rpt
    max_rpt                       = int(raw_rpt) if raw_rpt != '0' else 999
    form.relay_sort.data          = relay_sort = request.form.get('relay_sort', 'speed')
    team_ids, seasons             = _parse_selected(selected)
    choices_map                   = dict(form.teams.choices)

    if 'export_excel' in request.form:
        data = build_excel(pairs, selected, team_ids, seasons, excluded, top_n, form.teams.choices, gender)
        return Response(
            data,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition': 'attachment;filename=swim_results.xlsx'},
        )

    if ev not in RELAYS:
        swimmers = query_individual_event(team_ids, seasons, excluded, int(ev), gender, top_n)
        return _render_select(form, swimmers, excluded)

    pools = build_all_pools(pairs, selected, ev, excluded, gender)
    try:
        page = int(request.form.get('page', 1))
    except Exception:
        page = 1

    swimmers, pagination, max_possible = build_relay_view(
        pools, ev, scoring, max_rpt, relay_sort, choices_map, top_n, page, page_size=16,
    )
    form.max_relays_per_team.choices = [('0', 'No limit')] + [
        (str(i), str(i)) for i in range(1, max_possible + 1)
    ]
    return _render_select(form, swimmers, excluded, pagination=pagination)
