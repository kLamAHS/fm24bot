"""Route contracts of the observation bridge (README, section 3.1 of the spec).

Validation is structural: required keys and basic types. Unknown extra keys
are tolerated but recorded so a decoder change is visible. A mismatch never
fabricates data; the payload is stored with ``schema_mismatch`` quality.
"""
from __future__ import annotations

from typing import Any

SCHEMA_VERSION = "bridge-24.4.2-r1"

# route -> {"kind": "object"|"list", "required": {key: type_names}}
_T = {"int": (int,), "str": (str,), "bool": (bool,), "list": (list,), "dict": (dict,), "num": (int, float), "opt_int": (int, type(None)), "opt_str": (str, type(None)), "opt_num": (int, float, type(None)), "opt_dict": (dict, type(None)), "opt_list": (list, type(None))}

ROUTES: dict[str, dict[str, Any]] = {
    "/status": {"kind": "object", "required": {"connected": "bool", "read_only": "bool"}},
    "/game": {"kind": "object", "required": {"date": "str", "time": "str"}},
    "/manager": {"kind": "object", "required": {"id": "int", "name": "str"}},
    "/club": {"kind": "object", "required": {"id": "int", "name": "str", "squad": "list"}},
    "/squad": {"kind": "list", "item": "player"},
    "/finances": {"kind": "object", "required": {"club_id": "int", "currency": "str", "balance": "int", "transfer_budget": "int", "wage_budget_weekly": "int", "payroll_spending_weekly": "int", "as_of": "str"}},
    "/fixtures": {"kind": "object", "required": {"club_id": "int", "team_id": "int", "calendar_year": "int", "as_of": "str", "fixtures": "list"}},
    "/staff": {"kind": "list", "item": "staff"},
    "/tactics": {"kind": "object", "required": {"available": "bool"}},
    "/inbox": {"kind": "object", "required": {"messages": "list", "unread_count": "int"}},
    "/training": {"kind": "object", "required": {"available": "bool"}},
    "/scouting": {"kind": "object", "required": {"reports": "list"}},
    "/shortlists": {"kind": "object", "required": {"lists": "list"}},
    "/transfer-targets": {"kind": "object", "required": {"targets": "list"}},
    "/match": {"kind": "object", "required": {"available": "bool"}},
    "player": {"kind": "object", "required": {"id": "int", "name": "str", "attributes": "dict", "positions": "list", "position_ratings": "dict", "condition": "opt_num", "match_sharpness": "opt_num", "date_of_birth": "str", "age": "int", "morale": "str", "morale_rating": "int", "readiness": "dict", "contracts": "list"}},
    "staff": {"kind": "object", "required": {"id": "int", "name": "str", "team_id": "int", "departments": "list", "job_code": "int"}},
    "fixture": {"kind": "object", "required": {"date": "str", "competition_id": "int", "home": "dict", "away": "dict", "status": "str"}},
    "inbox_message": {"kind": "object", "required": {"id": "int", "date": "str", "unread": "bool", "event_type": "str", "text_status": "str"}},
}

ATTRIBUTE_NAMES = ["crossing", "dribbling", "finishing", "heading", "long_shots", "marking", "off_the_ball", "passing", "penalty_taking", "tackling", "vision", "handling", "aerial_reach", "command_of_area", "communication", "kicking", "throwing", "anticipation", "decisions", "one_on_ones", "positioning", "reflexes", "first_touch", "technique", "flair", "corners", "teamwork", "work_rate", "long_throws", "eccentricity", "rushing_out", "punching_tendency", "acceleration", "free_kick_taking", "strength", "stamina", "pace", "jumping_reach", "leadership", "balance", "bravery", "aggression", "agility", "natural_fitness", "determination", "composure", "concentration"]

POSITION_NAMES = ["GK", "DL", "DC", "DR", "DM", "ML", "MC", "MR", "AML", "AMC", "AMR", "ST", "WBL", "WBR"]


def route_for(path: str) -> str | None:
    if path.startswith("/players/"):
        return "player"
    return path if path in ROUTES else None


def _check_object(spec: dict[str, Any], payload: Any, where: str, problems: list[str]) -> None:
    if not isinstance(payload, dict):
        problems.append(f"{where}: expected object, got {type(payload).__name__}")
        return
    for key, type_name in spec.get("required", {}).items():
        if key not in payload:
            problems.append(f"{where}: missing key {key!r}")
            continue
        allowed = _T[type_name]
        value = payload[key]
        if isinstance(value, bool) and bool not in allowed:
            problems.append(f"{where}: key {key!r} is a bool, expected {type_name}")
        elif not isinstance(value, allowed):
            problems.append(f"{where}: key {key!r} has type {type(value).__name__}, expected {type_name}")


def validate_payload(route: str, payload: Any) -> list[str]:
    """Return a list of problems (empty means the payload matches the contract)."""
    problems: list[str] = []
    name = route_for(route) or route
    spec = ROUTES.get(name)
    if spec is None:
        return [f"unknown route {route}"]
    if spec["kind"] == "list":
        if not isinstance(payload, list):
            return [f"{route}: expected list, got {type(payload).__name__}"]
        for index, item in enumerate(payload):
            _check_object(ROUTES[spec["item"]], item, f"{route}[{index}]", problems)
        return problems
    _check_object(spec, payload, route, problems)
    if name == "player" and isinstance(payload, dict) and isinstance(payload.get("attributes"), dict):
        attrs = payload["attributes"]
        missing = [a for a in ATTRIBUTE_NAMES if a not in attrs]
        if missing:
            problems.append(f"{route}: attributes missing {missing[:5]}{'...' if len(missing) > 5 else ''}")
        bad = [k for k, v in attrs.items() if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 20]
        if bad:
            problems.append(f"{route}: attributes outside 1..20: {bad[:5]}")
    if name == "/fixtures" and isinstance(payload, dict):
        for index, item in enumerate(payload.get("fixtures", [])):
            _check_object(ROUTES["fixture"], item, f"{route}.fixtures[{index}]", problems)
    if name == "/inbox" and isinstance(payload, dict):
        for index, item in enumerate(payload.get("messages", [])):
            _check_object(ROUTES["inbox_message"], item, f"{route}.messages[{index}]", problems)
    return problems
