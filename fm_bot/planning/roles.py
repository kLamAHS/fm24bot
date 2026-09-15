"""Role valuation baseline (design specification 7.1, 7.3; ticket BOT 008).

A role score says how well a player's *observed* attributes fit a tactical
role in a given slot, adjusted for how familiar he is with the slot's
position and, when the readiness cache is current, for his condition and
match sharpness. Everything here is an explicit, reviewable baseline:

* :data:`ROLE_WEIGHTS` (``roles-v1``) are hand-written integer weights over
  the 47 attribute names the bridge decodes. They make **no claim** to match
  the match engine; they exist so a reviewer can read, dispute and version
  them. Context effects and role interactions are an experiment for
  ``fm_bot.models`` once enough varied outcome data exists (spec 7.1).
* :data:`FAMILIARITY_TABLE` maps a position rating of 15..20 to a multiplier.
  Below :data:`UNFAMILIAR_THRESHOLD` the assignment is *unfamiliar*: heavily
  penalised but still allowed, so an emergency selection is possible and
  visibly labelled.
* Readiness is applied only when the bridge reports the cache as ``current``.
  A stale cache gives **no adjustment** and the explanation carries the
  ``readiness_unavailable`` flag. Condition is not fitness proof and never
  stands in for eligibility (spec 10.1).

The score is unavailable (never zero) when the attributes a role needs are
masked or missing, when the role is not one the bridge decodes, or when the
slot position code is unknown.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..bridge_client.schemas import ATTRIBUTE_NAMES, POSITION_NAMES
from ..state.records import PlayerState
from ..state.status import Observed, ValueStatus

ROLE_WEIGHTS_VERSION = "roles-v1"

# Role name (as decoded by the bridge) -> attribute -> integer weight.
# Weights are relative importances chosen by hand. Attribute values are the
# bridge's validated 1..20 integers; the score is the weighted mean divided
# by 20 so that a player with 20 in every weighted attribute scores 1.0.
ROLE_WEIGHTS: dict[str, dict[str, int]] = {
    "Goalkeeper": {"handling": 4, "reflexes": 4, "aerial_reach": 3, "command_of_area": 3, "one_on_ones": 3, "positioning": 3, "communication": 2, "kicking": 2, "concentration": 2, "decisions": 2, "agility": 2, "anticipation": 2, "throwing": 1, "composure": 1},
    "Sweeper Keeper": {"handling": 3, "reflexes": 4, "aerial_reach": 2, "command_of_area": 3, "one_on_ones": 3, "positioning": 3, "rushing_out": 3, "kicking": 3, "first_touch": 2, "passing": 2, "communication": 2, "concentration": 2, "decisions": 2, "agility": 2, "anticipation": 2, "acceleration": 1, "pace": 1, "composure": 1},
    "Central Defender": {"marking": 4, "tackling": 4, "heading": 4, "positioning": 4, "strength": 3, "jumping_reach": 3, "anticipation": 3, "concentration": 3, "decisions": 2, "bravery": 2, "composure": 2, "pace": 2, "aggression": 1},
    "Ball Playing Defender": {"marking": 3, "tackling": 3, "heading": 3, "positioning": 4, "passing": 3, "first_touch": 2, "technique": 2, "vision": 2, "composure": 3, "strength": 2, "jumping_reach": 2, "anticipation": 3, "concentration": 3, "decisions": 2, "bravery": 1, "pace": 2},
    "Full-Back": {"tackling": 3, "marking": 3, "positioning": 3, "crossing": 2, "passing": 2, "pace": 3, "acceleration": 2, "stamina": 3, "work_rate": 3, "teamwork": 2, "anticipation": 2, "concentration": 2, "decisions": 2, "dribbling": 1},
    "Wing-Back": {"crossing": 3, "dribbling": 3, "pace": 3, "acceleration": 3, "stamina": 4, "work_rate": 3, "tackling": 2, "marking": 2, "positioning": 2, "off_the_ball": 2, "technique": 2, "teamwork": 2, "passing": 2, "decisions": 1},
    "Defensive Midfielder": {"tackling": 4, "marking": 3, "positioning": 4, "anticipation": 3, "concentration": 3, "decisions": 3, "teamwork": 3, "work_rate": 3, "strength": 2, "stamina": 2, "passing": 2, "composure": 2, "aggression": 1, "bravery": 1},
    "Central Midfielder": {"passing": 4, "first_touch": 3, "decisions": 3, "teamwork": 3, "work_rate": 3, "stamina": 3, "tackling": 2, "vision": 2, "technique": 2, "positioning": 2, "anticipation": 2, "off_the_ball": 2, "composure": 2},
    "Box To Box Midfielder": {"stamina": 4, "work_rate": 4, "passing": 3, "tackling": 3, "off_the_ball": 3, "teamwork": 3, "decisions": 2, "first_touch": 2, "finishing": 2, "long_shots": 2, "strength": 2, "pace": 2, "positioning": 2, "determination": 2},
    "Deep-Lying Playmaker": {"passing": 4, "vision": 4, "first_touch": 4, "technique": 3, "composure": 3, "decisions": 3, "teamwork": 3, "anticipation": 2, "positioning": 2, "concentration": 2, "tackling": 1, "work_rate": 1, "balance": 1},
    "Wide Midfielder": {"crossing": 3, "passing": 3, "work_rate": 3, "teamwork": 3, "stamina": 3, "dribbling": 2, "technique": 2, "decisions": 2, "off_the_ball": 2, "positioning": 2, "tackling": 2, "pace": 2, "acceleration": 2, "first_touch": 2},
    "Winger": {"dribbling": 4, "crossing": 4, "pace": 4, "acceleration": 4, "agility": 3, "technique": 3, "off_the_ball": 3, "first_touch": 2, "flair": 2, "balance": 2, "stamina": 2, "work_rate": 1, "passing": 1},
    "Attacking Midfielder": {"passing": 3, "first_touch": 3, "technique": 3, "vision": 3, "decisions": 3, "off_the_ball": 3, "flair": 2, "long_shots": 2, "finishing": 2, "dribbling": 2, "composure": 2, "anticipation": 2, "agility": 1, "acceleration": 1},
    "Deep-Lying Forward": {"first_touch": 4, "passing": 3, "technique": 3, "vision": 3, "off_the_ball": 3, "finishing": 3, "composure": 3, "decisions": 2, "teamwork": 2, "strength": 2, "anticipation": 2, "balance": 1, "heading": 1},
    "Advanced Forward": {"finishing": 4, "off_the_ball": 4, "composure": 3, "first_touch": 3, "acceleration": 3, "pace": 3, "dribbling": 2, "technique": 2, "anticipation": 2, "decisions": 2, "heading": 2, "strength": 1, "balance": 1, "work_rate": 1},
}

KNOWN_ROLES: tuple[str, ...] = tuple(ROLE_WEIGHTS)

# Fallback role per base position when a tactic slot's role is not decoded.
# Using it is a labelled heuristic (flag ``role_fallback``), never a claim
# about the stored tactic.
DEFAULT_ROLE_FOR_POSITION: dict[str, str] = {
    "GK": "Goalkeeper", "DL": "Full-Back", "DR": "Full-Back", "DC": "Central Defender", "WBL": "Wing-Back", "WBR": "Wing-Back",
    "DM": "Defensive Midfielder", "MC": "Central Midfielder", "ML": "Wide Midfielder", "MR": "Wide Midfielder",
    "AML": "Winger", "AMR": "Winger", "AMC": "Attacking Midfielder", "ST": "Advanced Forward",
}

# Tactic slot position codes (bridge /tactics) -> base position used for the
# familiarity lookup in ``position_ratings``.
SLOT_POSITION_MAP: dict[str, str] = {
    "GK": "GK", "DL": "DL", "DR": "DR", "DC": "DC", "DCL": "DC", "DCR": "DC", "WBL": "WBL", "WBR": "WBR",
    "DM": "DM", "DMC": "DM", "DML": "DM", "DMR": "DM", "DMCL": "DM", "DMCR": "DM",
    "ML": "ML", "MR": "MR", "MC": "MC", "MCL": "MC", "MCR": "MC",
    "AML": "AML", "AMR": "AMR", "AMC": "AMC", "AMCL": "AMC", "AMCR": "AMC",
    "ST": "ST", "STC": "ST", "STCL": "ST", "STCR": "ST", "STL": "ST", "STR": "ST",
}

# Position rating -> multiplier. Ratings below UNFAMILIAR_THRESHOLD are
# "unfamiliar": allowed, heavily penalised, and labelled.
FAMILIARITY_TABLE: dict[int, float] = {20: 1.0, 19: 0.97, 18: 0.94, 17: 0.90, 16: 0.86, 15: 0.82}
UNFAMILIAR_THRESHOLD = 15
UNFAMILIAR_MULTIPLIER = 0.30
FAMILIARITY_LABELS: dict[int, str] = {20: "natural", 19: "accomplished", 18: "accomplished", 17: "competent", 16: "competent", 15: "competent"}
UNFAMILIAR_LABEL = "unfamiliar"
# When ratings are masked but the positions list is visible, a listed position
# is treated as the threshold rating (flag ``familiarity_from_positions_list``).
POSITIONS_LIST_ASSUMED_RATING = 15

# Readiness adjustment, applied only when readiness_status == "current".
# factor = (1 - w) + w * value/100 for each of condition and sharpness.
READINESS_CONDITION_WEIGHT = 0.30
READINESS_SHARPNESS_WEIGHT = 0.15

FLAG_READINESS_UNAVAILABLE = "readiness_unavailable"
FLAG_UNFAMILIAR = "unfamiliar_position"
FLAG_ROLE_FALLBACK = "role_fallback"
FLAG_FAMILIARITY_FROM_LIST = "familiarity_from_positions_list"


def _validate_weights() -> None:
    known = set(ATTRIBUTE_NAMES)
    for role, weights in ROLE_WEIGHTS.items():
        unknown = sorted(set(weights) - known)
        if unknown:
            raise ValueError(f"{ROLE_WEIGHTS_VERSION}: role {role!r} references unknown attributes {unknown}")
        if any(w <= 0 for w in weights.values()):
            raise ValueError(f"{ROLE_WEIGHTS_VERSION}: role {role!r} has a non-positive weight")
    for pos, role in DEFAULT_ROLE_FOR_POSITION.items():
        if pos not in POSITION_NAMES or role not in ROLE_WEIGHTS:
            raise ValueError(f"{ROLE_WEIGHTS_VERSION}: bad fallback {pos!r} -> {role!r}")


_validate_weights()


def base_position(slot_code: str | None) -> str | None:
    """Map a tactic slot code such as ``DCR`` or ``STCL`` to its base position (``DC``, ``ST``)."""
    if not slot_code:
        return None
    return SLOT_POSITION_MAP.get(slot_code.upper())


def weights_for(role: str | None) -> dict[str, int] | None:
    return ROLE_WEIGHTS.get(role) if role else None


@dataclass
class RoleScore:
    """The explanation behind one player/role/slot score.

    ``score`` is the combined value in 0..1 (unavailable when it cannot be
    computed honestly). ``attribute_fit`` is the weighted attribute mean
    before familiarity and readiness; ``familiarity`` is the label from
    :data:`FAMILIARITY_LABELS`; ``flags`` lists every caveat the operator
    should see.
    """

    player_id: int
    role: str | None
    position: str | None
    base_position: str | None
    score: Observed
    attribute_fit: float | None = None
    familiarity: str = "unknown"
    familiarity_multiplier: float | None = None
    position_rating: int | None = None
    readiness_factor: float | None = None
    flags: list[str] = field(default_factory=list)
    weights_version: str = ROLE_WEIGHTS_VERSION

    def to_json(self) -> dict[str, Any]:
        return {"player_id": self.player_id, "role": self.role, "position": self.position, "base_position": self.base_position, "score": self.score.to_json(), "attribute_fit": self.attribute_fit, "familiarity": self.familiarity, "familiarity_multiplier": self.familiarity_multiplier, "position_rating": self.position_rating, "readiness_factor": self.readiness_factor, "flags": list(self.flags), "weights_version": self.weights_version}


def attribute_fit(state: PlayerState, role: str) -> Observed:
    """Weighted attribute mean for a role, in 0..1; unavailable when any weighted attribute is masked."""
    weights = weights_for(role)
    source = f"{ROLE_WEIGHTS_VERSION}:{state.player_id}"
    if weights is None:
        return Observed.unavailable(ValueStatus.UNSUPPORTED, "attribute_fit", f"role {role!r} has no weights in {ROLE_WEIGHTS_VERSION}", source)
    attrs = state.attributes or {}
    missing = [name for name in weights if not isinstance(attrs.get(name), int) or isinstance(attrs.get(name), bool)]
    if missing:
        return Observed.unavailable(ValueStatus.MISSING, "attribute_fit", f"attributes masked or missing for role {role}: {missing[:5]}{'...' if len(missing) > 5 else ''}", source)
    total = sum(weights.values())
    value = sum(w * attrs[name] for name, w in weights.items()) / (20.0 * total)
    return Observed.available_value(value, source, what="attribute_fit")


def familiarity(state: PlayerState, base: str) -> tuple[Observed, str, int | None, list[str]]:
    """Familiarity multiplier for a base position: ``(multiplier, label, rating, flags)``."""
    flags: list[str] = []
    ratings = state.position_ratings or {}
    rating = ratings.get(base)
    if isinstance(rating, bool) or not isinstance(rating, int):
        if state.positions:
            if base in state.positions:
                rating = POSITIONS_LIST_ASSUMED_RATING
                flags.append(FLAG_FAMILIARITY_FROM_LIST)
            else:
                rating = UNFAMILIAR_THRESHOLD - 1
                flags.append(FLAG_FAMILIARITY_FROM_LIST)
        else:
            return Observed.unavailable(ValueStatus.MISSING, "familiarity", f"no position rating or positions list for {base}", "roles"), "unknown", None, flags
    if rating >= UNFAMILIAR_THRESHOLD:
        capped = min(20, rating)
        return Observed.available_value(FAMILIARITY_TABLE[capped], "roles", what="familiarity"), FAMILIARITY_LABELS[capped], rating, flags
    flags.append(FLAG_UNFAMILIAR)
    return Observed.available_value(UNFAMILIAR_MULTIPLIER, "roles", what="familiarity"), UNFAMILIAR_LABEL, rating, flags


def readiness_factor(state: PlayerState) -> tuple[float | None, list[str]]:
    """Readiness multiplier from condition/sharpness, only when the cache is current; otherwise ``None`` and a flag."""
    if state.readiness_status != "current" or state.condition is None or state.match_sharpness is None:
        return None, [FLAG_READINESS_UNAVAILABLE]
    condition = max(0.0, min(100.0, float(state.condition)))
    sharpness = max(0.0, min(100.0, float(state.match_sharpness)))
    factor = ((1 - READINESS_CONDITION_WEIGHT) + READINESS_CONDITION_WEIGHT * condition / 100.0) * ((1 - READINESS_SHARPNESS_WEIGHT) + READINESS_SHARPNESS_WEIGHT * sharpness / 100.0)
    return factor, []


def explain_role_score(state: PlayerState, role: str | None, position: str | None) -> RoleScore:
    """Full explanation of a player's fit for ``role`` in tactic slot ``position``.

    A ``None`` or undecoded role falls back to :data:`DEFAULT_ROLE_FOR_POSITION`
    with the ``role_fallback`` flag. The score is unavailable when the slot
    code is unknown, the role has no weights, or attributes are masked.
    """
    base = base_position(position)
    flags: list[str] = []
    if base is None:
        return RoleScore(state.player_id, role, position, None, Observed.unavailable(ValueStatus.UNSUPPORTED, "role_score", f"unknown slot position code {position!r}", "roles"), flags=flags)
    effective_role = role
    if effective_role not in ROLE_WEIGHTS:
        if role is None or role == "not_decoded":
            effective_role = DEFAULT_ROLE_FOR_POSITION[base]
            flags.append(FLAG_ROLE_FALLBACK)
        else:
            return RoleScore(state.player_id, role, position, base, Observed.unavailable(ValueStatus.UNSUPPORTED, "role_score", f"role {role!r} has no weights in {ROLE_WEIGHTS_VERSION}", "roles"), flags=flags)
    fit = attribute_fit(state, effective_role)
    if not fit.available:
        return RoleScore(state.player_id, effective_role, position, base, Observed.unavailable(fit.status, "role_score", fit.reason, fit.source), flags=flags)
    multiplier, label, rating, fam_flags = familiarity(state, base)
    flags.extend(fam_flags)
    if not multiplier.available:
        return RoleScore(state.player_id, effective_role, position, base, Observed.unavailable(multiplier.status, "role_score", multiplier.reason, "roles"), fit.value, label, None, rating, None, flags)
    factor, ready_flags = readiness_factor(state)
    flags.extend(ready_flags)
    value = fit.value * multiplier.value * (factor if factor is not None else 1.0)
    score = Observed.available_value(max(0.0, min(1.0, value)), f"{ROLE_WEIGHTS_VERSION}:{state.player_id}", what="role_score")
    return RoleScore(state.player_id, effective_role, position, base, score, fit.value, label, multiplier.value, rating, factor, flags)


def role_score(state: PlayerState, role: str | None, position: str | None) -> Observed:
    """Role fit in 0..1 as an :class:`Observed`; see :func:`explain_role_score` for the breakdown."""
    return explain_role_score(state, role, position).score
