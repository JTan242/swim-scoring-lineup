# Domain logic: NCAA scoring tables, relay pool building, and squad selection.

import logging
from collections import defaultdict

from extensions import db, cache
from models import Team, Swimmer, Time

log = logging.getLogger(__name__)

# NCAA dual-meet points: 1st–16th individual, then relay table
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


# ── Cached pool builders ────────────────────────────────────────
# The full, unfiltered pool is cached per (team, season, gender).
# Exclusions are applied in memory after retrieval so the cache
# stays stable regardless of which times a coach toggles.

def _query_free_pool(tid, yr, dist, gender):
    """Query DB for full free-relay pool (uncached helper)."""
    cache_key = f"freepool:{tid}:{yr}:{dist}:{gender}"
    cached = cache.get(cache_key)
    if cached is not None:
        log.debug("cache HIT  %s", cache_key)
        return cached

    log.debug("cache MISS %s — querying DB", cache_key)
    rows = (
        db.session.query(Time, Swimmer.id, Swimmer.name)
        .join(Time.swimmer)
        .filter(
            Swimmer.team_id == tid,
            Swimmer.gender == gender,
            Time.season_year == yr,
            Time.event.has(name=f"{dist} Free", course='Y'),
        )
        .order_by(Time.time_secs)
        .all()
    )
    pool = [
        {'time_id': tr.id, 'swimmer_id': sid, 'name': nm,
         'time': float(tr.time_secs), 'stroke': 'Free'}
        for tr, sid, nm in rows
    ]
    cache.set(cache_key, pool)
    return pool


def _query_medley_pools(tid, yr, gender):
    """Query DB for full medley stroke pools (uncached helper)."""
    cache_key = f"medley:{tid}:{yr}:{gender}"
    cached = cache.get(cache_key)
    if cached is not None:
        log.debug("cache HIT  %s", cache_key)
        return cached

    log.debug("cache MISS %s — querying DB", cache_key)
    stroke_pools = {}
    for stroke in MEDLEY_STROKES:
        rows = (
            db.session.query(Time, Swimmer.id, Swimmer.name)
            .join(Time.swimmer)
            .filter(
                Swimmer.team_id == tid,
                Swimmer.gender == gender,
                Time.season_year == yr,
                Time.event.has(name=f"100 {stroke}", course='Y'),
            )
            .order_by(Time.time_secs)
            .all()
        )
        stroke_pools[stroke] = [
            {'swimmer_id': sid, 'name': nm,
             'time': float(tr.time_secs), 'time_id': tr.id}
            for tr, sid, nm in rows
        ]
    cache.set(cache_key, stroke_pools)
    return stroke_pools


def _filter_excluded(pool, excluded):
    """Remove excluded time IDs from a flat pool list."""
    return [e for e in pool if e['time_id'] not in excluded]


def _filter_medley_excluded(stroke_pools, excluded):
    """Remove excluded time IDs from every stroke in a medley pool dict."""
    return {stroke: [e for e in entries if e['time_id'] not in excluded]
            for stroke, entries in stroke_pools.items()}


def build_all_pools(pairs, selected, relay_key, excluded, gender):
    pools = {}
    for tid, _tname, yr in pairs:
        key = f"{tid}:{yr}"
        if key not in selected:
            continue
        if relay_key == 'relay_medley':
            raw = _query_medley_pools(tid, yr, gender)
            pools[key] = _filter_medley_excluded(raw, excluded)
        else:
            raw = _query_free_pool(tid, yr, RELAYS[relay_key], gender)
            pools[key] = _filter_excluded(raw, excluded)
    return pools


def _best_medley_assignment(stroke_pools, used_ids=None):
    """Brute-force best 4 (one per stroke). stroke_pools = {stroke: [entries]}. Returns (total_secs, legs) or None."""
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
    """Take fastest 4, remove, repeat for up to top_n squads."""
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


def pick_scored_combos(pool, relay_key, max_per_team=2):
    """Up to max_per_team relays per team. For medley, brute-force each successive relay."""
    combos = []
    if relay_key != 'relay_medley':
        sp = sorted(pool, key=lambda x: x['time'])
        for i in range(max_per_team):
            start = i * 4
            if len(sp) >= start + 4:
                combos.append({
                    'leg': sp[start:start+4],
                    'time': sum(x['time'] for x in sp[start:start+4]),
                })
    else:
        stroke_pools = pool
        used = set()
        for _ in range(max_per_team):
            best = _best_medley_assignment(stroke_pools, used)
            if not best:
                break
            t, legs = best
            combos.append({'leg': legs, 'time': t})
            used.update(leg['swimmer_id'] for leg in legs)
    return combos


def rank_scored_combos(all_combos):
    """NCAA scored relay ranking.

    Rules implemented:
    - 16 total scored slots: 8 A (ranks 1-8) and 8 B (ranks 9-16).
    - Each team's fastest relay is its A candidate.
    - Sort all A candidates by time; the 8 fastest become the A relays (ranks 1-8).
    - If a team has >8 A candidates (impossible with 8 teams but handled),
      the 9th+ fastest A candidates overflow into B.
    - For B slots: each team's 2nd (or overflow) relay is a B candidate.
    - A B relay CANNOT place ahead of any team's A relay that isn't its own,
      even if the B is faster.  We enforce this by ranking B relays after all A relays.
    - Points come from RELAY_SCORE[rank-1].
    """
    team_counter = defaultdict(int)
    all_combos_sorted = sorted(all_combos, key=lambda c: c['time'])
    for c in all_combos_sorted:
        key = (c.get('team', ''), c.get('season', ''))
        team_counter[key] += 1
        c['_team_order'] = team_counter[key]

    a_candidates = sorted(
        [c for c in all_combos if c['_team_order'] == 1],
        key=lambda c: c['time'],
    )
    b_candidates = sorted(
        [c for c in all_combos if c['_team_order'] == 2],
        key=lambda c: c['time'],
    )

    a_relays = a_candidates[:8]
    overflow = a_candidates[8:]
    b_candidates = sorted(overflow + b_candidates, key=lambda c: c['time'])
    b_relays = b_candidates[:8]

    ranked = []
    for i, c in enumerate(a_relays):
        c['type'] = 'A'
        c['rank'] = i + 1
        ranked.append(c)
    for i, c in enumerate(b_relays):
        c['type'] = 'B'
        c['rank'] = len(a_relays) + i + 1
        ranked.append(c)
    return ranked


def _attach_team_info(squads, key, choices_map):
    label, season = choices_map[key], int(key.split(':')[1])
    for sq in squads:
        sq['team'], sq['season'] = label, season


def squads_to_display_rows(squads, score_table=None):
    """Turn squads into the row dicts select.html expects.

    If a squad has a pre-assigned 'rank' (from rank_scored_combos),
    that rank is used for point lookup so relay_sort doesn't affect scoring.
    """
    rows = []
    for display_pos, squad in enumerate(squads, start=1):
        scoring_rank = squad.get('rank', display_pos)
        pts = score_for(score_table, scoring_rank) if score_table else None
        combo_fmt = format_time(squad['time'])
        for leg in squad['leg']:
            rows.append({
                'time_id':        leg['time_id'],
                'combo_rank':     display_pos,
                'relay_type':     squad.get('type', ''),
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


def query_individual_event(team_ids, seasons, excluded, ev_id, gender, top_n):
    """Query and rank swimmers for one individual event. Returns display row dicts."""
    rows = (
        db.session.query(
            Time, Swimmer.name.label('swimmer_name'),
            Team.name.label('team_name'),
            Time.season_year.label('season'),
        )
        .join(Time.swimmer).join(Swimmer.team)
        .filter(
            Team.id.in_(team_ids), Swimmer.gender == gender,
            Time.event_id == ev_id,
            Time.season_year.in_(seasons), ~Time.id.in_(excluded),
        )
        .order_by(Time.time_secs)
        .limit(top_n * 50)
        .all()
    )
    seen, distinct = set(), []
    for row in rows:
        key = (row.swimmer_name, row.team_name, row.season)
        if key not in seen:
            seen.add(key)
            distinct.append(row)
            if len(distinct) >= top_n:
                break
    swimmers = []
    for idx, row in enumerate(distinct, start=1):
        secs = float(row.Time.time_secs)
        swimmers.append({
            'time_id':        row.Time.id,
            'combo_rank':     idx,
            'stroke':         '',
            'swimmer_id':     row.Time.swimmer_id,
            'name':           row.swimmer_name,
            'team':           row.team_name,
            'season':         row.season,
            'time':           secs,
            'time_fmt':       format_time(secs),
            'combo_time':     secs,
            'combo_time_fmt': format_time(secs),
            'points':         score_for(INDIV_SCORE, idx),
        })
    return swimmers


def build_relay_view(pools, ev, scoring_mode, max_rpt, relay_sort, choices_map, top_n, page, page_size):
    """Orchestrate relay pool → squads → display rows.

    Returns:
        swimmers: list of display row dicts
        pagination: dict for unscored paging, or None for scored
        max_possible: largest number of relays any single team can field
    """
    if ev == 'relay_medley':
        pool_sizes = [
            min(len(v) for v in pool.values()) if pool else 0
            for pool in pools.values()
            if isinstance(pool, dict)
        ]
    else:
        pool_sizes = [len(p) // 4 for p in pools.values()]
    max_possible = max(pool_sizes, default=1)
    max_possible = max(max_possible, 1)

    if scoring_mode == 'unscored':
        all_squads = []
        for key, pool in pools.items():
            squads = pick_greedy_squads(pool, ev, max_rpt)
            _attach_team_info(squads, key, choices_map)
            all_squads.extend(squads)
        all_squads.sort(key=lambda x: x['time'])
        all_squads = all_squads[:top_n]
        start = (page - 1) * page_size
        swimmers = squads_to_display_rows(all_squads[start:start + page_size])
        pagination = {'page': page, 'total': len(all_squads), 'page_size': page_size}
    else:
        all_combos = []
        for key, pool in pools.items():
            combos = pick_scored_combos(pool, ev, max_rpt)
            _attach_team_info(combos, key, choices_map)
            all_combos.extend(combos)
        ranked = rank_scored_combos(all_combos)
        if relay_sort != 'points':
            ranked.sort(key=lambda x: x['time'])
        swimmers = squads_to_display_rows(ranked, score_table=RELAY_SCORE)
        pagination = None

    return swimmers, pagination, max_possible
