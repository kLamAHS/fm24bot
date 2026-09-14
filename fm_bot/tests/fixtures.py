"""A realistic fake bridge world for offline tests.

Shapes follow the bridge README and ``structures/``. Player IDs 1001..1024
form the squad; 2001..2003 are non-squad players (other clubs). Money is
native whole-pound GBP; wages are weekly.
"""
from __future__ import annotations

import copy
import random
from typing import Any

from ..bridge_client.schemas import ATTRIBUTE_NAMES
from ..bridge_client.transport import FakeTransport

BUILD = "24.4.2+2081827"
SESSION = "11111111-2222-3333-4444-555555555555"
GAME_DATE = "2024-02-17"
GAME_TIME = "10:00"
MANAGER = {"id": 90001, "name": "Test Manager"}
CLUB = {"id": 742, "name": "Wycombe"}

STATUS_CAPABILITIES = ["player_identity", "player_attributes_47", "primary_nationality", "employment_contracts", "loan_contracts", "positions", "condition", "match_sharpness", "readiness_freshness", "morale", "date_of_birth", "age", "game_date", "current_manager", "current_club", "team_roster", "club_finances", "transfer_budget", "wage_budget", "fixtures", "results", "time_of_day", "match_viewer", "match_score", "match_clock", "match_team_statistics", "match_player_identity", "match_player_ratings", "match_player_goals", "match_player_yellow_cards", "match_retained_condition", "match_player_positions", "opposition_starting_formation", "club_staff", "selected_tactic", "selected_lineup", "inbox_metadata", "training_schedule", "training_program_settings", "player_shortlists", "scout_report_metadata", "scout_report_knowledge", "transfer_targets"]
STATUS_UNRESOLVED = ["match_replay_classification", "match_current_simulation_condition", "match_red_cards", "match_injuries", "opposition_current_formation", "virtual_player_names", "contract_clauses", "secondary_nationalities", "staff_attributes", "team_instructions", "all_tactic_role_combinations", "inbox_text", "inbox_attachments", "training_current_ratings", "training_positions", "all_training_focus_labels", "effective_training_intensity", "scouting_recommendations", "scouting_report_text", "world_scouting_knowledge", "shortlist_expiry", "transfer_target_terms", "all_transfer_target_labels", "finance_breakdowns", "scouting_budget", "debts"]

# (id, name, primary positions, quality tier 1..3, weekly wage, morale)
SQUAD_SPEC = [
    (1001, "Max Stryjek", ["GK"], 2, 3500, "Good"),
    (1002, "Franco Ravizzoli", ["GK"], 1, 1200, "Okay"),
    (1003, "Jack Grimmer", ["DR", "DC"], 2, 3800, "Good"),
    (1004, "Ryan Tafazolli", ["DC"], 3, 5200, "Very Good"),
    (1005, "Chris Forino", ["DC", "DL"], 2, 2900, "Fairly Good"),
    (1006, "Joe Low", ["DC"], 2, 2400, "Okay"),
    (1007, "Jasper Pattenden", ["DL", "WBL"], 1, 1400, "Fair"),
    (1008, "Joe Jacobson", ["DL", "WBL"], 2, 3600, "Good"),
    (1009, "Josh Scowen", ["DM", "MC"], 2, 4100, "Good"),
    (1010, "Matt Butcher", ["MC", "DM"], 2, 3300, "Fairly Good"),
    (1011, "Luke Leahy", ["MC", "AMC"], 3, 4800, "Very Good"),
    (1012, "Kieran Sadlier", ["ML", "AML", "AMC"], 2, 4200, "Good"),
    (1013, "Garath McCleary", ["MR", "AMR"], 2, 4500, "Good"),
    (1014, "Daniel Udoh", ["ST"], 2, 3900, "Okay"),
    (1015, "Sam Vokes", ["ST"], 3, 6100, "Very Good"),
    (1016, "Brandon Hanlan", ["ST", "AML"], 2, 3700, "Fairly Good"),
    (1017, "Beryly Lubala", ["AMR", "ST"], 2, 3000, "Good"),
    (1018, "Tjay De Barr", ["ST"], 1, 1500, "Fair"),
    (1019, "Jamie Mills", ["MC"], 1, 500, "Okay"),
    (1020, "Kai Forsyth", ["DC"], 1, 450, "Okay"),
    (1021, "Jason McCarthy", ["DR", "DC"], 2, 3400, "Good"),
    (1022, "Freddie Potts", ["MC", "DM"], 2, 2600, "Really Good"),
    (1023, "David Wheeler", ["MR", "AMR", "ST"], 2, 3200, "Good"),
    (1024, "Jonny Jamieson", ["DL"], 1, 1000, "Fair"),
]

ALL_POSITIONS = ["GK", "DL", "DC", "DR", "DM", "ML", "MC", "MR", "AML", "AMC", "AMR", "ST", "WBL", "WBR"]


def _attributes(seed: int, tier: int, positions: list[str]) -> dict[str, int]:
    rng = random.Random(seed)
    base = {1: 7, 2: 10, 3: 13}[tier]
    attrs = {name: max(1, min(20, base + rng.randint(-3, 3))) for name in ATTRIBUTE_NAMES}
    if "GK" in positions:
        for name in ("handling", "reflexes", "aerial_reach", "command_of_area", "one_on_ones", "kicking"):
            attrs[name] = min(20, base + 4 + rng.randint(0, 2))
        for name in ("finishing", "dribbling", "long_shots"):
            attrs[name] = max(1, rng.randint(1, 4))
    else:
        for name in ("handling", "reflexes", "aerial_reach", "command_of_area", "one_on_ones", "rushing_out", "punching_tendency", "kicking", "throwing", "eccentricity"):
            attrs[name] = max(1, rng.randint(1, 5))
    return attrs


def player_payload(pid: int, name: str, positions: list[str], tier: int, wage: int, morale: str, *, readiness_current: bool = True, condition: float = 93.0, sharpness: float = 88.0, club_id: int = CLUB["id"], club_name: str = CLUB["name"]) -> dict[str, Any]:
    rng = random.Random(pid)
    ratings = {pos: (rng.randint(15, 20) if pos in positions else rng.randint(1, 10)) for pos in ALL_POSITIONS}
    for pos in positions:
        ratings[pos] = max(ratings[pos], 15)
    morale_rating = {"Abysmal": 1, "Fair": 8, "Okay": 10, "Fairly Good": 11, "Good": 13, "Really Good": 14, "Very Good": 15}.get(morale, 10)
    return {
        "id": pid, "name": name, "first_name": name.split()[0], "surname": name.split()[-1],
        "attributes": _attributes(pid, tier, positions), "positions": [p for p in ALL_POSITIONS if ratings[p] >= 15],
        "position_ratings": ratings,
        "condition": condition if readiness_current else None, "match_sharpness": sharpness if readiness_current else None,
        "date_of_birth": f"{2024 - 20 - (pid % 12)}-03-{(pid % 27) + 1:02d}", "age": 20 + (pid % 12), "age_as_of": GAME_DATE,
        "morale": morale, "morale_rating": morale_rating,
        "readiness": {"status": "current" if readiness_current else "stale", "updated_on": GAME_DATE if readiness_current else "2024-02-10"},
        "primary_nationality": {"id": 1, "name": "England"},
        "contracts": [{"kind": "employment", "club_id": club_id, "club_name": club_name, "team_id": 7420, "start_date": "2023-07-01", "end_date": "2025-06-30", "weekly_wage_gbp": wage, "wage_basis": "salary"}],
    }


def squad_payload() -> list[dict[str, Any]]:
    return [player_payload(*spec) for spec in SQUAD_SPEC]


def finances_payload() -> dict[str, Any]:
    return {"club_id": CLUB["id"], "currency": "GBP", "balance": 10909005, "transfer_budget": 1720250, "wage_budget_weekly": 78979, "payroll_spending_weekly": sum(s[4] for s in SQUAD_SPEC), "as_of": GAME_DATE}


def _team(team_id: int, club_id: int, name: str) -> dict[str, Any]:
    return {"team_id": team_id, "club_id": club_id, "club_name": name}


HOME = _team(7420, CLUB["id"], CLUB["name"])
LEAGUE = (14, "Sky Bet League One")
CUP = (33, "Bristol Street Motors Trophy")


def fixtures_payload() -> dict[str, Any]:
    opponents = [(8001, 801, "Stevenage"), (8002, 802, "Peterborough"), (8003, 803, "Oxford"), (8004, 804, "Bolton"), (8005, 805, "Derby"), (8006, 806, "Barnsley"), (8007, 807, "Leyton Orient"), (8008, 808, "Reading")]
    items = []
    played = [("2024-01-27", "15:00", LEAGUE, True, 1, 1), ("2024-02-03", "15:00", LEAGUE, False, 2, 0), ("2024-02-10", "15:00", LEAGUE, True, 0, 1)]
    for (day, time, comp, home, hs, aws), opp in zip(played, opponents):
        home_team, away_team = (HOME, _team(*opp)) if home else (_team(*opp), HOME)
        items.append({"date": day, "time": time, "competition_id": comp[0], "competition_name": comp[1], "home": home_team, "away": away_team, "status": "played", "home_score": hs, "away_score": aws})
    scheduled = [("2024-02-20", "19:45", CUP, True), ("2024-02-24", "15:00", LEAGUE, False), ("2024-03-02", "15:00", LEAGUE, True), ("2024-03-09", "15:00", LEAGUE, False), ("2024-03-16", "15:00", LEAGUE, True)]
    for (day, time, comp, home), opp in zip(scheduled, opponents[3:]):
        home_team, away_team = (HOME, _team(*opp)) if home else (_team(*opp), HOME)
        items.append({"date": day, "time": time, "competition_id": comp[0], "competition_name": comp[1], "home": home_team, "away": away_team, "status": "scheduled", "home_score": None, "away_score": None})
    return {"club_id": CLUB["id"], "team_id": 7420, "calendar_year": 2024, "as_of": GAME_DATE, "fixtures": items}


def tactics_payload() -> dict[str, Any]:
    slots = [("GK", 1001, "Goalkeeper", "Defend"), ("DR", 1003, "Full-Back", "Support"), ("DCR", 1004, "Central Defender", "Defend"), ("DCL", 1005, "Central Defender", "Defend"), ("DL", 1008, "Full-Back", "Support"), ("MR", 1013, "Wide Midfielder", "Support"), ("MCR", 1009, "Central Midfielder", "Defend"), ("MCL", 1011, "Box To Box Midfielder", "Support"), ("ML", 1012, "Wide Midfielder", "Support"), ("STCR", 1015, "Advanced Forward", "Attack"), ("STCL", 1014, "Deep-Lying Forward", "Support")]
    names = {spec[0]: spec[1] for spec in SQUAD_SPEC}
    positions = [{"slot": i, "position": pos, "position_code": 0, "player_id": pid, "player_name": names[pid], "role": role, "duty": duty, "instructions_source": "validated"} for i, (pos, pid, role, duty) in enumerate(slots)]
    subs = [{"slot": i, "player_id": pid, "player_name": names[pid]} for i, pid in enumerate([1002, 1021, 1010, 1016, 1017, 1023, 1006])]
    return {"available": True, "reason": None, "selected_slot": 0, "stored_name": "4-4-2 Balanced", "style": None, "mentality": "Balanced", "positions": positions, "substitutes": subs}


def inbox_payload() -> dict[str, Any]:
    return {"messages": [
        {"id": 501, "date": "2024-02-16", "time": "09:00", "unread": True, "event_type": "news_item_transfer_offer", "sender_id": 95, "sender_name": "Director of Football", "subject": None, "body": None, "text_status": "not_decoded", "time_status": "current"},
        {"id": 502, "date": "2024-02-15", "time": "17:30", "unread": False, "event_type": "news_item_training", "sender_id": 96, "sender_name": "Assistant Manager", "subject": None, "body": None, "text_status": "not_decoded", "time_status": "current"},
        {"id": 503, "date": "2024-02-17", "time": None, "unread": True, "event_type": "news_item_board_meeting_request", "sender_id": None, "sender_name": None, "subject": None, "body": None, "text_status": "not_decoded", "time_status": "not_initialized"},
    ], "unread_count": 2, "scope": "current_human_inbox"}


def training_payload() -> dict[str, Any]:
    days = [{"date": f"2024-02-{12 + i:02d}", "sessions": [{"slot": 0, "name": "Recovery" if i in (0, 4) else "Match Practice", "kind": "training", "status": "decoded"}]} for i in range(7)]
    programs = [{"player_id": s[0], "player_name": s[1], "additional_focus": None, "additional_focus_status": "none", "intensity_setting": "Normal", "intensity_setting_status": "decoded", "rating": None, "rating_status": "not_decoded", "position_role_duty_status": "not_decoded"} for s in SQUAD_SPEC[:6]]
    return {"available": True, "reason": None, "scope": "current_team_committed_schedule", "weeks": [{"start_date": "2024-02-12", "stored_name": "Match Week", "days": days}], "current_week_start": "2024-02-12", "individual_programs": programs}


def scouting_payload() -> dict[str, Any]:
    return {"reports": [{"player_id": 2001, "player_name": "Brad Target", "scout_id": 300, "scout_name": "Chief Scout", "completed_on": "2024-02-11", "knowledge": "Good", "recommendation": None, "recommendation_status": "not_decoded", "text_status": "not_decoded"}], "scope": "current_human_stored_player_reports", "knowledge_scope": "stored_report; not a live worldwide knowledge estimate"}


def shortlists_payload() -> dict[str, Any]:
    return {"lists": [{"name": "Default", "is_default": True, "players": [{"id": 2001, "name": "Brad Target", "added_on": "2024-02-09", "expires_on": None, "expiry_status": "not_decoded"}, {"id": 2002, "name": "Sam Winger", "added_on": "2024-02-10", "expires_on": None, "expiry_status": "not_decoded"}]}], "scope": "current_human_player_shortlists"}


def transfer_targets_payload() -> dict[str, Any]:
    return {"targets": [{"player_id": 2001, "player_name": "Brad Target", "added_on": "2024-02-09", "type": "Transfer", "status": "Normal", "priority": None, "group_name": None, "terms_status": "not_decoded"}], "scope": "current_human_transfer_targets"}


def staff_payload() -> list[dict[str, Any]]:
    return [
        {"id": 96, "name": "Assistant Manager", "primary_nationality": {"id": 1, "name": "England"}, "team_id": 7420, "current_team": True, "departments": ["coaching"], "job": "Assistant Manager", "job_code": 2, "employment": {"kind": "employment", "club_id": CLUB["id"], "club_name": CLUB["name"], "team_id": 7420, "start_date": "2023-07-01", "end_date": "2025-06-30", "weekly_wage_gbp": 1500, "wage_basis": "salary"}},
        {"id": 300, "name": "Chief Scout", "primary_nationality": {"id": 1, "name": "England"}, "team_id": 7420, "current_team": True, "departments": ["scouting"], "job": "Chief Scout", "job_code": 9, "employment": {"kind": "employment", "club_id": CLUB["id"], "club_name": CLUB["name"], "team_id": 7420, "start_date": "2023-07-01", "end_date": "2025-06-30", "weekly_wage_gbp": 900, "wage_basis": "salary"}},
    ]


def match_unavailable_payload() -> dict[str, Any]:
    return {"available": False, "reason": "no supported match viewer active", "match": None}


def status_payload(*, connected: bool = True, build: str = BUILD, session: str = SESSION, reason: str | None = None) -> dict[str, Any]:
    if not connected:
        return {"connected": False, "read_only": True, "reason": reason or "no supported save available"}
    return {"connected": True, "pid": 5464, "session_id": session, "read_only": True, "build": build, "game_date": GAME_DATE, "capabilities": list(STATUS_CAPABILITIES), "unresolved": list(STATUS_UNRESOLVED), "validation_scope": "test fixture"}


def other_club_player(pid: int = 2001, name: str = "Brad Target", positions: list[str] | None = None, tier: int = 3, wage: int = 4000) -> dict[str, Any]:
    return player_payload(pid, name, positions or ["DL", "WBL"], tier, wage, "Good", club_id=803, club_name="Oxford")


def world(*, session: str = SESSION, game_date: str = GAME_DATE, game_time: str = GAME_TIME, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Complete route map. Values are ``(status, body)`` tuples ready for FakeTransport."""
    env = lambda data: FakeTransport.envelope(data, session_id=session)  # noqa: E731
    squad = squad_payload()
    routes: dict[str, Any] = {
        "/status": (200, status_payload(session=session)),
        "/game": env({"date": game_date, "time": game_time}),
        "/manager": env(dict(MANAGER)),
        "/club": env({"id": CLUB["id"], "name": CLUB["name"], "squad": squad}),
        "/squad": env(squad),
        "/finances": env(finances_payload()),
        "/fixtures": env(fixtures_payload()),
        "/staff": env(staff_payload()),
        "/tactics": env(tactics_payload()),
        "/inbox": env(inbox_payload()),
        "/training": env(training_payload()),
        "/scouting": env(scouting_payload()),
        "/shortlists": env(shortlists_payload()),
        "/transfer-targets": env(transfer_targets_payload()),
        "/match": env(match_unavailable_payload()),
    }
    for player in squad:
        routes[f"/players/{player['id']}"] = env(player)
    for pid, name in ((2001, "Brad Target"), (2002, "Sam Winger"), (2003, "Henry Unscouted")):
        routes[f"/players/{pid}"] = env(other_club_player(pid, name))
    for path, value in (overrides or {}).items():
        routes[path] = value
    return routes


def transport(**kw) -> FakeTransport:
    return FakeTransport(world(**kw))


def client(store=None, **kw):
    from ..bridge_client.client import BridgeClient
    return BridgeClient(transport(**kw), store, context={"career_id": kw.pop("career_id", None), "branch_id": kw.pop("branch_id", None)} if False else None)


def deep(value: Any) -> Any:
    return copy.deepcopy(value)
