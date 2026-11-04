"""Injured list stints are built from transaction text. These are small made-up histories."""

import datetime as dt

import pandas as pd

from data_pipeline.fetch_mlb_api import build_il_stints, classify


def history(*events):
    rows = []
    for i, (date, description) in enumerate(events):
        day = dt.date.fromisoformat(date)
        rows.append({"transaction_id": i, "date": day, "effective_date": day, "description": description,
                     "mlbam_id": 1, "name": "A Pitcher", "to_team": "Team", "from_team": None})
    return pd.DataFrame(rows)


def stints(*events):
    return build_il_stints(history(*events)).to_dict("records")


PLACED = "Team placed LHP A Pitcher on the 15-day injured list. Right knee subluxation."
ACTIVATED = "Team activated LHP A Pitcher from the 15-day injured list."


def test_an_activation_ends_a_stint():
    [s] = stints(("2025-04-01", PLACED), ("2025-04-20", ACTIVATED))
    assert s["days_on_il"] == 19 and s["end_reason"] == "activated"


def test_a_roster_move_ends_a_stint_without_an_activation():
    [s] = stints(("2025-03-24", PLACED), ("2025-05-11", "Team optioned LHP A Pitcher to Minor Team."))
    assert s["end_date"] == dt.date(2025, 5, 11) and s["end_reason"] == "back_on_roster"


def test_a_rehab_assignment_doesnt_end_a_stint():
    [s] = stints(("2025-04-01", PLACED), ("2025-04-10", "Team sent LHP A Pitcher on a rehab assignment to Minor Team."),
                 ("2025-04-20", ACTIVATED))
    assert s["days_on_il"] == 19


def test_a_new_injury_in_a_later_season_is_a_new_stint():
    first, second = stints(("2024-06-17", PLACED),
                           ("2026-04-05", "Team placed LHP A Pitcher on the 10-day injured list. Right oblique strain."),
                           ("2026-05-11", "Team activated LHP A Pitcher from the 10-day injured list."))
    assert first["end_reason"] == "unknown" and pd.isna(first["days_on_il"])  # blank beats a made-up length
    assert second["days_on_il"] == 36 and second["injury_note"] == "Right oblique strain."


def test_a_long_absence_for_the_same_injury_stays_one_stint():
    [s] = stints(("2024-08-01", "Team placed LHP A Pitcher on the 15-day injured list. Right elbow surgery."),
                 ("2025-02-15", "Team placed LHP A Pitcher on the 60-day injured list. Right elbow surgery."),
                 ("2025-08-01", ACTIVATED))
    assert s["days_on_il"] == 365 and s["il_type"] == "60-day"


def test_a_placement_in_the_same_season_extends_the_stint():
    [s] = stints(("2025-04-01", PLACED),
                 ("2025-05-01", "Team placed LHP A Pitcher on the 60-day injured list. Right knee surgery."),
                 ("2025-08-01", ACTIVATED))
    assert s["il_type"] == "60-day" and "surgery" in s["injury_note"]


def test_moves_with_no_open_stint_are_ignored():
    assert classify("Team recalled LHP A Pitcher from Minor Team.") == "back"
    assert len(stints(("2025-04-01", "Team recalled LHP A Pitcher from Minor Team."), ("2025-05-01", PLACED))) == 1


def test_a_recall_onto_the_injured_list_is_a_placement():
    assert classify("Team recalled LHP A Pitcher from Minor Team and placed him on the 15-day injured list.") == "placement"
