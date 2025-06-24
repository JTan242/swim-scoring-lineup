import itertools
import datetime
import importlib
import requests
from bs4 import BeautifulSoup

from flask import (
    Blueprint, render_template, request,
    flash, redirect, url_for, current_app
)
from flask_login import login_user, logout_user, login_required
from extensions import db, login_manager
from models import User, Team, Swimmer, Event, Time
from forms import LoginForm, RegistrationForm, ScrapeForm, SelectionForm

# DISABLE BROKEN POWER-INDEX 
ss = importlib.import_module("SwimScraper.SwimScraper")
ss.getPowerIndex = lambda swimmer_ID: None

# SCORING MAPS & EVENT LISTS 
INDIV_SCORE = [20,17,16,15,14,13,12,11,9,7,6,5,4,3,2,1]
RELAY_SCORE = [40,34,32,30,28,26,24,22,20,16,12,10,8,6,4,2]

INDIVIDUAL_EVENTS = {
    '50 Free','100 Free','200 Free','500 Free','1000 Free','1650 Free',
    '100 Back','200 Back','100 Breast','200 Breast',
    '100 Fly','200 Fly','200 IM','400 IM'
}
RELAYS = {
    'relay_200_free': 50,
    'relay_400_free':100,
    'relay_800_free':200,
    'relay_medley':    None
}

main = Blueprint('main', __name__)

@login_manager.user_loader
def load_user(uid):
    from models import User
    return User.query.get(int(uid))


# HELPERS 

def parse_time_to_seconds(ts: str) -> float:
    parts = ts.split(':')
    if len(parts) == 1:
        return float(parts[0])
    m, s = parts
    return int(m)*60 + float(s)

def get_swimcloud_best_times(swimmer_id: str):
    """Scrape the Personal Bests table from SwimCloud for a swimmer."""
    url = f'https://www.swimcloud.com/swimmer/{swimmer_id}/'
    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/114.0.0.0 Safari/537.36'
        ),
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Referer': 'https://www.swimcloud.com/',
    }
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')

    best_table = None
    for table in soup.find_all('table'):
        ths = [th.get_text(strip=True) for th in table.find_all('th')]
        if {'Event', 'Time', 'Meet', 'Date'}.issubset(set(ths)):
            best_table = table
            break

    if best_table is None:
        current_app.logger.warning(f"No Personal Bests table found for swimmer {swimmer_id}")
        return []

    results = []
    for tr in best_table.select('tbody tr'):
        tds = tr.find_all('td')
        if len(tds) < 4:
            continue

        raw_event = tds[0].get_text(strip=True) 
        parts = raw_event.split()
        if len(parts) < 3:
            continue

        distance, course, stroke = parts[0], parts[1], ' '.join(parts[2:])
        # only yards
        if course != 'Y':
            continue

        event_name = f"{distance} {stroke}"
        time_str   = tds[1].get_text(strip=True)
        meet       = tds[2].get_text(strip=True)
        date_str   = tds[3].get_text(strip=True)

        try:
            date = datetime.datetime.strptime(date_str, '%b %d, %Y').date()
        except ValueError:
            date = None

        results.append({
            'event':    event_name,
            'time_str': time_str,
            'meet':     meet,
            'date':     date
        })

    return results

@main.route('/register', methods=['GET','POST'])
def register():
    form = RegistrationForm()
    if form.validate_on_submit():
        u = User(username=form.username.data)
        u.set_password(form.password.data)
        db.session.add(u); db.session.commit()
        flash('Registered! Log in now.', 'success')
        return redirect(url_for('main.login'))
    return render_template('register.html', form=form)

@main.route('/login', methods=['GET','POST'])
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


@main.route('/scrape', methods=['GET','POST'])
@login_required
def scrape():
    form = ScrapeForm()
    if form.validate_on_submit():
        team_name, team_id = form.team_name.data, form.team_id.data
        gender, year, pro  = form.gender.data, form.year.data, form.pro.data

        roster = ss.getRoster(
            team=team_name, team_ID=team_id,
            gender=gender, year=year, pro=pro
        )

        team = Team.query.filter_by(name=team_name).first() or Team(name=team_name)
        db.session.add(team); db.session.flush()

        swimmers_count = times_count = 0

        for info in roster:
            sid, name = info['swimmer_ID'], info['swimmer_name']
            swimmer = (Swimmer.query
                          .filter_by(name=name, team_id=team.id)
                          .first()
                       ) or Swimmer(name=name, gender=gender, team_id=team.id)
            db.session.add(swimmer); db.session.flush()

            # get personal bests
            try:
                best_times = get_swimcloud_best_times(sid)
            except Exception as e:
                current_app.logger.warning(f"Error scraping {sid}: {e}")
                best_times = []

            for bt in best_times:
                ev = bt['event']
                # only yard events
                if ev not in INDIVIDUAL_EVENTS:
                    continue

                secs = parse_time_to_seconds(bt['time_str'])

                # avoid duplicates 
                exists = Time.query.filter_by(
                    swimmer_id=swimmer.id,
                    event_id=Event.query
                                  .filter_by(name=ev, course='Y')
                                  .with_entities(Event.id)
                                  .scalar_subquery(),
                    time_secs=secs,
                    date=bt['date'],
                    season_year=year
                ).first()
                if exists:
                    continue

                evobj = (Event.query
                             .filter_by(name=ev, course='Y')
                             .first()) or Event(name=ev, course='Y')
                db.session.add(evobj); db.session.flush()

                t = Time(
                    swimmer_id=swimmer.id,
                    event_id=evobj.id,
                    time_secs=secs,
                    meet=bt['meet'],
                    date=bt['date'],
                    season_year=year
                )
                db.session.add(t)
                times_count += 1

            swimmers_count += 1

        db.session.commit()
        flash(f"Imported {swimmers_count} swimmers and {times_count} times for {year}.", 'success')
        return render_template('scrape.html',
                               form=form,
                               swimmers_count=swimmers_count,
                               times_count=times_count)

    return render_template('scrape.html', form=form)

# DASHBOARD & SCORING 

@main.route('/select', methods=['GET','POST'])
@login_required
def select():
    form = SelectionForm()

    # Build Team–Season checkboxes 
    pairs = (
        db.session.query(Team.id, Team.name, Time.season_year)
                  .join(Team.swimmers).join(Swimmer.times)
                  .distinct()
                  .order_by(Time.season_year.desc(), Team.name)
                  .all()
    )
    form.teams.choices = [
        (f"{tid}:{yr}", f"{yr} {tname}") for tid, tname, yr in pairs
    ]

    # Build Event dropdown
    relay_opts = [
        (key, f"{' '.join(key.split('_')[1:]).title()} Relay")
        for key in RELAYS
    ]
    indiv_opts = [
        (str(e.id), e.name)
        for e in Event.query.filter(Event.name.in_(INDIVIDUAL_EVENTS))\
                              .order_by(Event.name).all()
    ]
    form.event.choices = relay_opts + indiv_opts

    swimmers = []
    excluded = set()

    if request.method == 'POST':
        existing_excl = {
            int(x) for x in request.form.getlist('excluded')
            if x.isdigit()
        }
        raw_ids = [
            int(x) for x in request.form.getlist('time_id')
            if x.isdigit()
        ]
        if request.form.get('event') in RELAYS:
            kept = {
                int(x) for x in request.form.getlist('include_time_id')
                if x.isdigit()
            }
            newly_excl = set(raw_ids) - kept
        else:
            kept = set()
            for idx, tid in enumerate(raw_ids):
                if request.form.get(f'include_{idx}'):
                    kept.add(tid)
            newly_excl = set(raw_ids) - kept

        excluded = existing_excl | newly_excl

        # Validate team-seasons
        selected = request.form.getlist('teams')
        if not selected:
            flash("Select at least one Team–Season.", "danger")
            return render_template('select.html',
                                   form=form,
                                   swimmers=[],
                                   excluded=excluded,
                                   RELAYS=RELAYS)

        # Restore form fields
        form.teams.data        = selected
        form.event.data        = ev       = request.form['event']
        form.top_n.data        = top_n    = int(request.form['top_n'])
        scoring = request.form.get('scoring_mode', 'unscored')
        form.scoring_mode.data = scoring

        team_ids = [int(x.split(':')[0]) for x in selected]
        seasons  = [int(x.split(':')[1]) for x in selected]

        # Individual‐Event Branch 
        if ev not in RELAYS:
            raw_rows = (
                db.session.query(
                    Time,
                    Swimmer.name.label('swimmer_name'),
                    Team.name.label('team_name'),
                    Time.season_year.label('season')
                )
                .join(Time.swimmer)
                .join(Swimmer.team)
                .filter(
                    Team.id.in_(team_ids),
                    Time.event_id == int(ev),
                    Time.season_year.in_(seasons),
                    ~Time.id.in_(excluded)
                )
                .order_by(Time.time_secs)
                .limit(top_n * 2)
                .all()
            )

            seen_names = set()
            distinct    = []
            # keep first occurrence of each name
            for row in raw_rows:
                name = row.swimmer_name
                if name in seen_names:
                    continue
                seen_names.add(name)
                distinct.append(row)
                if len(distinct) >= top_n:
                    break

            # build  result list
            for idx, row in enumerate(distinct, start=1):
                secs = float(row.Time.time_secs)
                mins, sec = divmod(secs, 60)
                fmt = f"{int(mins)}:{sec:05.2f}"
                swimmers.append({
                    'time_id':        row.Time.id,
                    'combo_rank':     idx,
                    'stroke':         '',
                    'swimmer_id':     row.Time.swimmer_id,
                    'name':           row.swimmer_name,
                    'team':           row.team_name,
                    'season':         row.season,
                    'time':           secs,
                    'time_fmt':       fmt,
                    'combo_time':     secs,
                    'combo_time_fmt': fmt,
                    'points':         INDIV_SCORE[idx-1] if idx <= len(INDIV_SCORE) else 0
                })

            return render_template(
                'select.html',
                form=form,
                swimmers=swimmers,
                excluded=excluded,
                RELAYS=RELAYS
            )

        #  Relay Branch
        pools = {}
        for tid, tname, yr in pairs:
            key = f"{tid}:{yr}"
            if key not in selected:
                continue

            if ev != 'relay_medley':
                # freestyle relays
                dist = RELAYS[ev]
                rows = (
                    db.session.query(Time, Swimmer.id, Swimmer.name)
                              .join(Time.swimmer)
                              .filter(
                                  Swimmer.team_id==tid,
                                  Time.season_year==yr,
                                  Time.event.has(name=f"{dist} Free",course='Y')
                              )
                              .order_by(Time.time_secs)
                              .all()
                )
                pools[key] = [
                    {
                      'time_id':    tr.id,
                      'swimmer_id': sid,
                      'name':       nm,
                      'time':       float(tr.time_secs),
                      'stroke':     'Free'
                    }
                    for tr, sid, nm in rows
                    if tr.id not in excluded
                ]

            else:
                # medley relays 
                strokes = ['Back','Breast','Fly','Free']
                by_sw = {}
                for stroke in strokes:
                    rows = (
                        db.session.query(Time, Swimmer.id, Swimmer.name)
                                  .join(Time.swimmer)
                                  .filter(
                                      Swimmer.team_id==tid,
                                      Time.season_year==yr,
                                      Time.event.has(name=f"100 {stroke}",course='Y')
                                  )
                                  .order_by(Time.time_secs)
                                  .all()
                    )
                    for tr, sid, nm in rows:
                        if tr.id in excluded:
                            continue
                        entry = by_sw.setdefault(sid, {
                            'swimmer_id': sid,
                            'name':       nm,
                            'times':      {},
                            'time_ids':   {}
                        })
                        entry['times'][stroke]    = float(tr.time_secs)
                        entry['time_ids'][stroke] = tr.id

                pools[key] = [v for v in by_sw.values() if len(v['times'])==4]

        # Non-Scoring Relay Mode 
        if scoring == 'unscored':
            all_squads = []
            for key, pool in pools.items():
                tid, yr = key.split(':')
                team_label = dict(form.teams.choices)[key]

                if ev != 'relay_medley':
                    temp = pool[:]
                    while len(temp) >= 4:
                        best4 = sorted(temp, key=lambda x: x['time'])[:4]
                        total = sum(x['time'] for x in best4)
                        all_squads.append({
                            'leg':    best4,
                            'time':   total,
                            'team':   team_label,
                            'season': int(yr)
                        })
                        used = {s['swimmer_id'] for s in best4}
                        temp = [s for s in temp if s['swimmer_id'] not in used]
                else:
                    temp = pool[:]
                    strokes = ['Back','Breast','Fly','Free']
                    while len(temp) >= 4:
                        combo = []
                        for stroke in strokes:
                            swimmer = min(temp, key=lambda x: x['times'][stroke])
                            combo.append({
                                'time_id':    swimmer['time_ids'][stroke],
                                'swimmer_id': swimmer['swimmer_id'],
                                'name':       swimmer['name'],
                                'stroke':     stroke,
                                'time':       swimmer['times'][stroke]
                            })
                            temp = [s for s in temp if s['swimmer_id'] != swimmer['swimmer_id']]
                        total = sum(x['time'] for x in combo)
                        all_squads.append({
                            'leg':    combo,
                            'time':   total,
                            'team':   team_label,
                            'season': int(yr)
                        })

            all_squads.sort(key=lambda x: x['time'])
            all_squads = all_squads[:top_n]
            for idx, combo in enumerate(all_squads, start=1):
                mins_c, sec_c = divmod(combo['time'], 60)
                combo_fmt = f"{int(mins_c)}:{sec_c:05.2f}"
                for leg in combo['leg']:
                    mins, sec = divmod(leg['time'], 60)
                    split_fmt = f"{int(mins)}:{sec:05.2f}"
                    swimmers.append({
                        'time_id':        leg['time_id'],
                        'combo_rank':     idx,
                        'team':           combo['team'],
                        'season':         combo['season'],
                        'stroke':         leg['stroke'],
                        'swimmer_id':     leg['swimmer_id'],
                        'name':           leg['name'],
                        'time':           leg['time'],
                        'time_fmt':       split_fmt,
                        'combo_time':     combo['time'],
                        'combo_time_fmt': combo_fmt,
                        'points':         None
                    })
            return render_template('select.html',
                                   form=form,
                                   swimmers=swimmers,
                                   excluded=excluded,
                                   RELAYS=RELAYS)


        # Scoring Relay Mode 
        combos = []
        for key, pool in pools.items():
            tid, yr = key.split(':')
            team_label = dict(form.teams.choices)[key]

            if ev != 'relay_medley':
                sorted4 = sorted(pool, key=lambda x: x['time'])
                if len(sorted4) >= 4:
                    combos.append({
                        'leg':    sorted4[:4],
                        'time':   sum(x['time'] for x in sorted4[:4]),
                        'type':   'A',
                        'team':   team_label,
                        'season': int(yr)
                    })
                if len(sorted4) >= 8:
                    combos.append({
                        'leg':    sorted4[4:8],
                        'time':   sum(x['time'] for x in sorted4[4:8]),
                        'type':   'B',
                        'team':   team_label,
                        'season': int(yr)
                    })
            else:
                strokes = ['Back','Breast','Fly','Free']
                bests = []
                for quad in itertools.permutations(pool, 4):
                    total = sum(quad[i]['times'][strokes[i]] for i in range(4))
                    bests.append((total, quad))
                bests.sort(key=lambda x: x[0])

                if bests:
                    t1, q1 = bests[0]
                    combos.append({
                        'leg':    [
                            {
                                'time_id':    q1[i]['time_ids'][strokes[i]],
                                'swimmer_id': q1[i]['swimmer_id'],
                                'name':       q1[i]['name'],
                                'stroke':     strokes[i],
                                'time':       q1[i]['times'][strokes[i]]
                            } for i in range(4)
                        ],
                        'time':   t1,
                        'type':   'A',
                        'team':   team_label,
                        'season': int(yr)
                    })
                    used = {q1[i]['swimmer_id'] for i in range(4)}
                    rem = [s for s in pool if s['swimmer_id'] not in used]
                    if len(rem) >= 4:
                        bests2 = []
                        for quad in itertools.permutations(rem, 4):
                            total = sum(quad[i]['times'][strokes[i]] for i in range(4))
                            bests2.append((total, quad))
                        bests2.sort(key=lambda x: x[0])
                        t2, q2 = bests2[0]
                        combos.append({
                            'leg':    [
                                {
                                    'time_id':    q2[i]['time_ids'][strokes[i]],
                                    'swimmer_id': q2[i]['swimmer_id'],
                                    'name':       q2[i]['name'],
                                    'stroke':     strokes[i],
                                    'time':       q2[i]['times'][strokes[i]]
                                } for i in range(4)
                            ],
                            'time':   t2,
                            'type':   'B',
                            'team':   team_label,
                            'season': int(yr)
                        })

        # rank & score A/B
        A = sorted([c for c in combos if c['type']=='A'], key=lambda x: x['time'])
        B = sorted([c for c in combos if c['type']=='B'], key=lambda x: x['time'])
        scored = A[:8] + B[:8]
        scored = (A[:8] + B[:8])[:top_n]
        for idx, c in enumerate(scored, start=1):
            pts = RELAY_SCORE[idx-1] if idx <= len(RELAY_SCORE) else 0
            mins_c, sec_c = divmod(c['time'], 60)
            combo_fmt = f"{int(mins_c)}:{sec_c:05.2f}"
            for leg in c['leg']:
                mins, sec = divmod(leg['time'], 60)
                split_fmt = f"{int(mins)}:{sec:05.2f}"
                swimmers.append({
                    'time_id':        leg['time_id'],
                    'combo_rank':     idx,
                    'team':           c['team'],
                    'season':         c['season'],
                    'stroke':         leg['stroke'],
                    'swimmer_id':     leg['swimmer_id'],
                    'name':           leg['name'],
                    'time':           leg['time'],
                    'time_fmt':       split_fmt,
                    'combo_time':     c['time'],
                    'combo_time_fmt': combo_fmt,
                    'points':         pts
                })

        return render_template('select.html',
                               form=form,
                               swimmers=swimmers,
                               excluded=excluded,
                               RELAYS=RELAYS)
    return render_template(
        'select.html',
        form=form,
        swimmers=swimmers,
        excluded=excluded,
        RELAYS=RELAYS
    )