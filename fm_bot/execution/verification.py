"""Independent postcondition verification (spec 12.2, BOT 009).

A click that "worked" and a screen that changed are not evidence that the
club's state changed the way the intent wanted. Every plan here reads the
committed state back through the adapter and, when the bridge exposes the
same fact, corroborates it against a fresh snapshot:

* ``selected_tactic_matches_catalog`` - the selected tactic id and name equal the catalog entry;
* ``lineup_matches_selection``        - the selected player ids (in slot order) and roles are exactly the intended ones;
* ``training_settings_reread``        - the committed training settings reread equal the intended ones;
* ``contract_accepted_with_obligations`` - the agreement exists and its obligations equal the intended commitments;
* ``game_advanced_past_boundary``     - in-game time has moved on from the moment the intent recorded (Continue);
* ``inbox_message_answered``          - the message the answer was sent to is no longer pending in the bridge's inbox metadata;
* ``navigation_only``                 - the identified screen is the intended one.

A screen that changed and a click that returned are explicitly not enough, so
Continue and inbox answers are judged by their effect on the game rather than
by where the UI ended up: the in-game clock (the only clock for game state)
and the ``/inbox`` metadata of a *fresh* snapshot decide. Where the fresh
reading is not available the verdict is UNCERTAIN - never a guess, and never a
reason to send the input again.

Exact machine values are compared. The only tolerances are for values the UI
displays rounded (:data:`DISPLAY_TOLERANCES`); money is never rounded.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from ..state.records import ActionIntent, DecisionSnapshot
from ..state.status import Observed, ValueStatus
from ..state.units import Money, game_time_key
from .adapter import ScreenObservation, UIAdapter

VERIFICATION_VERSION = "execution.verification/1"

# Intent parameters a ``progress.continue`` carries so its effect can be established later:
# the in-game moment the calendar was to move on from, and the boundary it was expected to reach.
CONTINUE_FROM_DATE = "from_game_date"
CONTINUE_FROM_TIME = "from_game_time"
CONTINUE_BOUNDARY = "expected_boundary"
INBOX_ROUTE = "/inbox"

# Tolerances apply only to fields the UI shows rounded; the key is the field
# name suffix, the value the maximum absolute difference accepted. Money and
# identifiers never appear here.
DISPLAY_TOLERANCES: dict[str, int] = {
    "_percent": 1,          # condition / sharpness shown as whole percentages
}

# Minimum recognition confidence for a screen to count as identified in navigation checks.
NAVIGATION_MIN_CONFIDENCE = 0.9


class VerdictKind(str, Enum):
    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


@dataclass
class Evidence:
    """Observation ids plus the objects behind them, taken before or after execution."""

    observation_ids: list[str] = field(default_factory=list)
    snapshot: DecisionSnapshot | None = None
    screen: ScreenObservation | None = None
    step_results: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"observation_ids": list(self.observation_ids), "snapshot_id": self.snapshot.snapshot_id if self.snapshot else None, "screen": self.screen.to_json() if self.screen else None, "step_results": list(self.step_results)}


@dataclass
class Verdict:
    kind: VerdictKind
    plan: str
    reasons: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    readbacks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def confirmed(self) -> bool:
        return self.kind is VerdictKind.CONFIRMED

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "plan": self.plan, "reasons": list(self.reasons), "details": dict(self.details), "readbacks": list(self.readbacks), "version": VERIFICATION_VERSION}


def _confirmed(plan: str, effect: dict[str, Any], readbacks: list[Observed], reasons: list[str] | None = None) -> Verdict:
    return Verdict(VerdictKind.CONFIRMED, plan, reasons or ["readback equals the intended state"], {"effect": effect}, [r.to_json() for r in readbacks])


def _failed(plan: str, reasons: list[str], readbacks: list[Observed], details: dict[str, Any] | None = None) -> Verdict:
    return Verdict(VerdictKind.FAILED, plan, reasons, details or {}, [r.to_json() for r in readbacks])


def _uncertain(plan: str, reasons: list[str], readbacks: list[Observed], details: dict[str, Any] | None = None) -> Verdict:
    return Verdict(VerdictKind.UNCERTAIN, plan, reasons, details or {}, [r.to_json() for r in readbacks])


def values_match(observed: Any, expected: Any, field_name: str = "") -> bool:
    """Exact equality, except a documented display tolerance for rounded percentage fields."""
    if isinstance(expected, dict) and isinstance(observed, dict):
        return set(expected) == set(observed) and all(values_match(observed[k], expected[k], k) for k in expected)
    if isinstance(expected, (list, tuple)) and isinstance(observed, (list, tuple)):
        return len(expected) == len(observed) and all(values_match(o, e, field_name) for o, e in zip(observed, expected))
    if isinstance(expected, bool) or isinstance(observed, bool):
        return observed == expected
    if isinstance(expected, (int, float)) and isinstance(observed, (int, float)):
        for suffix, tolerance in DISPLAY_TOLERANCES.items():
            if field_name.endswith(suffix):
                return abs(observed - expected) <= tolerance
        return observed == expected
    return observed == expected


# ----- plans -----


def verify_selected_tactic(intent: ActionIntent, before: Evidence, after: Evidence, adapter: UIAdapter) -> Verdict:
    plan = "selected_tactic_matches_catalog"
    readback = adapter.readback("selected_tactic", {})
    if not readback.available:
        return _uncertain(plan, [f"tactic readback {readback.status.value}: {readback.reason}"], [readback])
    expected_id = intent.parameters.get("tactic_catalog_id")
    expected_name = intent.parameters.get("catalog_name")
    observed = readback.value or {}
    reasons: list[str] = []
    if observed.get("tactic_id") != expected_id:
        reasons.append(f"selected tactic id {observed.get('tactic_id')!r} != catalog entry {expected_id!r}")
    if expected_name is not None and observed.get("name") != expected_name:
        reasons.append(f"selected tactic name {observed.get('name')!r} != catalog name {expected_name!r}")
    if reasons:
        return _failed(plan, reasons, [readback], {"observed": observed})
    bridge_name = _bridge_tactic_name(after.snapshot)
    if expected_name is not None and bridge_name is not None and bridge_name != expected_name:
        return _uncertain(plan, [f"UI readback says {expected_name!r} but the bridge reports stored tactic {bridge_name!r}; sources contradict"], [readback], {"observed": observed, "bridge_stored_name": bridge_name})
    return _confirmed(plan, {"tactic_id": expected_id, "name": observed.get("name"), "bridge_corroborated": bridge_name == expected_name if expected_name is not None else None}, [readback])


def _bridge_tactic_name(snapshot: DecisionSnapshot | None) -> str | None:
    if snapshot is None:
        return None
    tactics = snapshot.routes.get("/tactics")
    if isinstance(tactics, dict) and tactics.get("available"):
        return tactics.get("stored_name")
    return None


def _bridge_lineup_ids(snapshot: DecisionSnapshot | None) -> list[int] | None:
    if snapshot is None:
        return None
    tactics = snapshot.routes.get("/tactics")
    if isinstance(tactics, dict) and tactics.get("available") and isinstance(tactics.get("positions"), list):
        return [slot.get("player_id") for slot in tactics["positions"]]
    return None


def verify_lineup(intent: ActionIntent, before: Evidence, after: Evidence, adapter: UIAdapter) -> Verdict:
    plan = "lineup_matches_selection"
    readback = adapter.readback("lineup", {})
    if not readback.available:
        return _uncertain(plan, [f"lineup readback {readback.status.value}: {readback.reason}"], [readback])
    expected_ids = list(intent.parameters.get("player_ids") or [])
    expected_roles = dict(intent.parameters.get("roles") or {})
    observed = readback.value or {}
    reasons: list[str] = []
    if list(observed.get("player_ids") or []) != expected_ids:
        reasons.append(f"selected player ids {observed.get('player_ids')} != intended {expected_ids} (slot order matters)")
    if dict(observed.get("roles") or {}) != expected_roles:
        reasons.append(f"selected roles {observed.get('roles')} != intended {expected_roles}")
    if reasons:
        return _failed(plan, reasons, [readback], {"observed": observed})
    bridge_ids = _bridge_lineup_ids(after.snapshot)
    if bridge_ids is not None and bridge_ids != expected_ids:
        return _uncertain(plan, [f"UI readback matches but the bridge reports lineup {bridge_ids}; sources contradict"], [readback], {"observed": observed, "bridge_lineup": bridge_ids})
    return _confirmed(plan, {"player_ids": expected_ids, "roles": expected_roles, "bridge_corroborated": bridge_ids == expected_ids if bridge_ids is not None else None}, [readback])


def verify_training(intent: ActionIntent, before: Evidence, after: Evidence, adapter: UIAdapter) -> Verdict:
    plan = "training_settings_reread"
    readback = adapter.readback("training_settings", {})
    if not readback.available:
        return _uncertain(plan, [f"training readback {readback.status.value}: {readback.reason}"], [readback])
    expected = dict(intent.parameters.get("settings") or {})
    observed = readback.value or {}
    if not values_match(observed, expected):
        return _failed(plan, [f"committed training settings {observed} != intended {expected}"], [readback], {"observed": observed})
    return _confirmed(plan, {"settings": observed}, [readback])


def _commitment_key(item: dict[str, Any]) -> tuple:
    amount = item.get("amount")
    money = Money.from_json(amount) if isinstance(amount, dict) else amount
    return (item.get("counterparty"), item.get("category"), str(money), item.get("due_date"), item.get("end_date"), item.get("trigger"))


def verify_contract(intent: ActionIntent, before: Evidence, after: Evidence, adapter: UIAdapter) -> Verdict:
    plan = "contract_accepted_with_obligations"
    offer_id = intent.parameters.get("offer_id")
    readback = adapter.readback("agreement", {"offer_id": offer_id})
    if readback.status is ValueStatus.NULL:
        return _failed(plan, [f"no agreement exists for offer {offer_id!r}: {readback.reason}"], [readback])
    if not readback.available:
        return _uncertain(plan, [f"agreement readback {readback.status.value}: {readback.reason}"], [readback])
    observed = readback.value or {}
    reasons: list[str] = []
    if not observed.get("agreement_id"):
        reasons.append("agreement readback carries no agreement id")
    expected = intent.parameters.get("expected_commitments")
    if expected is None:
        return _uncertain(plan, ["intent names no expected commitments; obligations cannot be verified"], [readback], {"observed": observed})
    observed_keys = sorted(_commitment_key(c) for c in observed.get("commitments", []))
    expected_keys = sorted(_commitment_key(c) for c in expected)
    if observed_keys != expected_keys:
        reasons.append(f"resulting obligations {observed_keys} != intended {expected_keys}")
    if reasons:
        return _failed(plan, reasons, [readback], {"observed": observed})
    return _confirmed(plan, {"agreement_id": observed.get("agreement_id"), "offer_id": offer_id, "commitments": list(observed.get("commitments", []))}, [readback])


def _unusable_snapshot(after: Evidence) -> str | None:
    """Why the fresh snapshot cannot be read as evidence, or ``None`` when it can.

    Both effect-based plans read the game itself rather than the UI, so they
    need a snapshot the collector accepted: an inconsistent one is not a
    weaker reading, it is no reading at all (spec 5.2, 12.2).
    """
    if after.snapshot is None:
        return "no fresh snapshot was collected after the input"
    if not after.snapshot.valid:
        return f"the fresh snapshot is not consistent ({after.snapshot.consistency.value}): " + "; ".join(after.snapshot.consistency_reasons)
    return None


def _game_moment(snapshot: DecisionSnapshot | None) -> tuple[str, str | None] | None:
    """The in-game moment a snapshot read, or ``None`` when it read none."""
    if snapshot is None or snapshot.game_date is None:
        return None
    return snapshot.game_date, snapshot.game_time


def _moment_text(moment: tuple[str, str | None] | None) -> str:
    return "unknown" if moment is None else f"{moment[0]} {moment[1] or '(no time)'}"


def verify_game_advanced(intent: ActionIntent, before: Evidence, after: Evidence, adapter: UIAdapter) -> Verdict:
    """Continue's effect: the in-game clock has moved on from the moment the intent recorded (spec 12.2, 12.4, CAL 01).

    The screen the UI ended up on proves nothing about the calendar, so the
    verdict is read from a fresh bridge snapshot's own in-game time - the only
    clock for game state. Moved on is CONFIRMED (whether the expected
    boundary was reached is recorded, not required: the game may stop earlier
    at an event nobody foresaw); still the same moment is FAILED (the
    calendar did not move); no fresh reading, or no recorded starting moment,
    is UNCERTAIN.
    """
    plan = "game_advanced_past_boundary"
    boundary = intent.parameters.get(CONTINUE_BOUNDARY)
    started = (intent.parameters.get(CONTINUE_FROM_DATE), intent.parameters.get(CONTINUE_FROM_TIME))
    if started[0] is None:
        started = _game_moment(before.snapshot) or (None, None)
    unusable = _unusable_snapshot(after)
    reached = None if unusable else _game_moment(after.snapshot)
    source = f"bridge:/game@{after.snapshot.snapshot_id}" if after.snapshot is not None else "bridge:/game"
    if reached is None:
        detail = unusable or "the fresh snapshot read no in-game clock"
        readback = Observed.unavailable(ValueStatus.MISSING, "game_time", detail, source=source)
        return _uncertain(plan, [f"no usable in-game clock reading after the input ({detail}); whether the calendar moved is unknown"], [readback], {"expected_boundary": boundary, "from": {"game_date": started[0], "game_time": started[1]}})
    readback = Observed.available_value({"game_date": reached[0], "game_time": reached[1]}, source, after.snapshot.collected_at, game_time=_moment_text(reached), what="game_time")
    if started[0] is None:
        return _uncertain(plan, ["the intent records no in-game moment to compare against; whether the calendar moved cannot be established"], [readback], {"observed": {"game_date": reached[0], "game_time": reached[1]}, "expected_boundary": boundary})
    if reached[0] == started[0] and (reached[1] is None or started[1] is None):
        # Same date and no time of day on one side: FM reports an uninitialised time as none, and a missing
        # reading is not "midnight". Whether the calendar moved inside that day is simply not observable.
        return _uncertain(plan, [f"in-game date is still {reached[0]} and the time of day is not reported for both readings ({_moment_text(started)} -> {_moment_text(reached)}); whether the calendar moved within the day cannot be established"], [readback], {"observed": {"game_date": reached[0], "game_time": reached[1]}, "expected_boundary": boundary})
    reached_key, started_key = game_time_key(*reached), game_time_key(*started)
    if reached_key == started_key:
        return _failed(plan, [f"in-game time is still {_moment_text(started)}: the calendar did not move on"], [readback], {"observed": {"game_date": reached[0], "game_time": reached[1]}, "expected_boundary": boundary})
    if reached_key < started_key:
        return _failed(plan, [f"in-game time went backwards {_moment_text(started)} -> {_moment_text(reached)}; the calendar did not move on (another save may be loaded)"], [readback], {"observed": {"game_date": reached[0], "game_time": reached[1]}, "expected_boundary": boundary})
    expected_date = (boundary or {}).get("date") if isinstance(boundary, dict) else None
    at_boundary = None if not expected_date else reached_key >= game_time_key(expected_date, (boundary or {}).get("time"))
    reasons = [f"in-game time moved {_moment_text(started)} -> {_moment_text(reached)}"]
    if at_boundary is False:
        reasons.append(f"the game stopped before the expected boundary ({_moment_text((expected_date, (boundary or {}).get('time')))}): {(boundary or {}).get('description')}")
    return _confirmed(plan, {"from": {"game_date": started[0], "game_time": started[1]}, "game_date": reached[0], "game_time": reached[1], "expected_boundary": boundary, "reached_expected_boundary": at_boundary}, [readback], reasons)


def _inbox_message(snapshot: DecisionSnapshot | None, message_id: Any) -> tuple[str, dict[str, Any] | None]:
    """How the fresh inbox metadata describes ``message_id``: ``unavailable``, ``absent`` or ``listed`` with the record."""
    if snapshot is None or not snapshot.valid:
        return "unavailable", None
    payload = snapshot.routes.get(INBOX_ROUTE)
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        return "unavailable", None
    for message in payload["messages"]:
        if isinstance(message, dict) and message.get("id") == message_id:
            return "listed", message
    return "absent", None


def verify_inbox_answered(intent: ActionIntent, before: Evidence, after: Evidence, adapter: UIAdapter) -> Verdict:
    """An inbox answer's effect: the message it answered is no longer pending (spec 12.2, 11.3, AUD 01).

    The bridge decodes no message text, so the observable effect is the
    metadata one: the message is gone from the inbox, or it is no longer
    unread. Which option the game recorded is *not* observable, so the chosen
    option id is carried in the effect as what was sent, never as something
    read back. Still unread is FAILED; no fresh inbox metadata is UNCERTAIN.
    """
    plan = "inbox_message_answered"
    message_id = intent.parameters.get("message_id")
    option_id = intent.parameters.get("option_id")
    state, record = _inbox_message(after.snapshot, message_id)
    source = f"bridge:{INBOX_ROUTE}@{after.snapshot.snapshot_id}" if after.snapshot is not None else f"bridge:{INBOX_ROUTE}"
    observed_at = after.snapshot.collected_at if after.snapshot is not None else None
    effect = {"message_id": message_id, "option_id_sent": option_id, "option_readback": "unsupported: the bridge decodes no inbox text, so which option the game recorded cannot be read back"}
    if state == "unavailable":
        detail = _unusable_snapshot(after) or f"the fresh snapshot carries no {INBOX_ROUTE} metadata"
        readback = Observed.unavailable(ValueStatus.MISSING, "inbox_metadata", detail, source=source)
        return _uncertain(plan, [f"no usable {INBOX_ROUTE} metadata ({detail}); whether message {message_id!r} is still pending is unknown"], [readback], {"message_id": message_id})
    if state == "absent":
        readback = Observed.available_value({"message_id": message_id, "listed": False}, source, observed_at, what="inbox_metadata")
        return _confirmed(plan, {**effect, "pending": False, "listed": False}, [readback], [f"message {message_id!r} is no longer listed in the inbox: it is no longer pending"])
    unread = (record or {}).get("unread")
    readback = Observed.available_value({"message_id": message_id, "listed": True, "unread": unread}, source, observed_at, what="inbox_metadata")
    if unread is None:
        return _uncertain(plan, [f"the inbox record for message {message_id!r} carries no unread flag; whether it is still pending is unknown"], [readback], {"observed": dict(record or {})})
    if unread:
        return _failed(plan, [f"message {message_id!r} is still unread in the inbox: the answer did not land"], [readback], {"observed": dict(record or {})})
    return _confirmed(plan, {**effect, "pending": False, "listed": True, "unread": False}, [readback], [f"message {message_id!r} is no longer unread in the inbox: it is no longer pending"])


def verify_navigation(intent: ActionIntent, before: Evidence, after: Evidence, adapter: UIAdapter) -> Verdict:
    plan = "navigation_only"
    expected = intent.parameters.get("target") or intent.parameters.get("expected_screen")
    observation = adapter.identify_screen()
    readback = Observed.available_value(observation.to_json(), f"ui:{getattr(adapter, 'name', 'adapter')}:screen", observation.observed_at, what="screen")
    if not observation.identified or observation.confidence < NAVIGATION_MIN_CONFIDENCE:
        return _uncertain(plan, [f"screen unidentified or low confidence ({observation.confidence:.2f}): {observation.reason}"], [readback])
    if expected is None:
        return _uncertain(plan, ["intent names no target screen"], [readback])
    if observation.screen_id != expected:
        return _failed(plan, [f"screen {observation.screen_id!r} shown, expected {expected!r}"], [readback])
    return _confirmed(plan, {"screen_id": observation.screen_id}, [readback])


PLANS: dict[str, Callable[[ActionIntent, Evidence, Evidence, UIAdapter], Verdict]] = {
    "selected_tactic_matches_catalog": verify_selected_tactic,
    "lineup_matches_selection": verify_lineup,
    "training_settings_reread": verify_training,
    "contract_accepted_with_obligations": verify_contract,
    "game_advanced_past_boundary": verify_game_advanced,
    "inbox_message_answered": verify_inbox_answered,
    "navigation_only": verify_navigation,
}


def verify(intent: ActionIntent, before: Evidence, after: Evidence, adapter: UIAdapter) -> Verdict:
    """Establish the intended effect by independent readback; never by step success alone.

    ``before``/``after`` evidence is attached for the audit trail. Step results
    in ``after`` are recorded but never decide the verdict.
    """
    plan = PLANS.get(intent.verification)
    if plan is None:
        return Verdict(VerdictKind.UNCERTAIN, intent.verification, [f"no verification plan named {intent.verification!r}; effect cannot be established"], {"before": before.to_json(), "after": after.to_json()})
    verdict = plan(intent, before, after, adapter)
    verdict.details["before"] = before.to_json()
    verdict.details["after"] = after.to_json()
    return verdict
