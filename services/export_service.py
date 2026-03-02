# Excel export: per-event and relay sheets for the swim scoring dashboard.

from io import BytesIO

import pandas as pd
from openpyxl.utils import get_column_letter

from extensions import db
from models import Team, Swimmer, Event, Time
from services.scoring import (
    INDIV_SCORE, RELAY_SCORE, INDIVIDUAL_EVENTS_ORDER, RELAYS,
    format_time, score_for,
    build_all_pools, pick_greedy_squads, pick_scored_combos,
    rank_scored_combos, _attach_team_info,
)


def _squads_to_excel_rows(squads, score_table=None):
    rows = []
    for display_pos, squad in enumerate(squads, start=1):
        scoring_rank = squad.get('rank', display_pos)
        pts = score_for(score_table, scoring_rank) if score_table else 0
        for leg in squad['leg']:
            row = {
                'Relay #':     display_pos,
                'Type':        squad.get('type', ''),
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


def _write_sheet(writer, name, df):
    name = name[:31]
    df.to_excel(writer, sheet_name=name, index=False)
    if df.empty:
        return
    ws = writer.sheets[name]
    for ci, col in enumerate(df.columns):
        w = max(df[col].astype(str).map(len).max(), len(col)) + 2
        ws.column_dimensions[get_column_letter(ci + 1)].width = w


def build_excel(pairs, selected, team_ids, seasons, excluded, top_n, team_choices, gender):
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
                    Team.id.in_(team_ids), Swimmer.gender == gender,
                    Time.event_id == ev_obj.id,
                    Time.season_year.in_(seasons), ~Time.id.in_(excluded),
                )
                .order_by(Time.time_secs)
                .all()
            )
            seen_ts = set()
            distinct_rows = []
            for t, swimmer, team, season in rows:
                key = (swimmer, team, season)
                if key not in seen_ts:
                    seen_ts.add(key)
                    distinct_rows.append((t, swimmer, team, season))
            data = [
                {
                    'Team/Season': f"{team} ({season})",
                    'Swimmer':     swimmer,
                    'Time':        format_time(t.time_secs),
                    'Points':      score_for(INDIV_SCORE, i),
                }
                for i, (t, swimmer, team, season) in enumerate(distinct_rows, start=1)
            ]
            _write_sheet(writer, ev_name, pd.DataFrame(data))

        for relay_key in RELAYS:
            pools = build_all_pools(pairs, selected, relay_key, excluded, gender)
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
                combos = pick_scored_combos(pool, relay_key, 2)
                _attach_team_info(combos, key, choices_map)
                all_combos.extend(combos)
            ranked = rank_scored_combos(all_combos)
            _write_sheet(writer, f"{label} Scored",
                         pd.DataFrame(_squads_to_excel_rows(ranked, RELAY_SCORE)))

    output.seek(0)
    return output.getvalue()
