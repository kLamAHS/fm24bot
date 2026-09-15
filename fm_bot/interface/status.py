"""The operator's status view (spec 15.1).

One screen answers: which career and branch am I managing, in which
information mode and with how much authority, is the bridge connected, what
day is it in the game, what does the bot want to do next, what is stopping
it, is the Stop control engaged, what happened last time it acted, and
which models it is using. The main text is football language; identifiers,
hashes and versions live in :func:`render_evidence`.

Everything is read from the store, the settings and (optionally) the latest
snapshot the orchestrator holds; nothing is guessed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..state.identity import BranchIdentity, CareerIdentity
from ..state.records import DecisionSnapshot
from ..state.status import MissingCapabilityReport
from .controls import Settings

STATUS_VIEW_VERSION = "interface.status/1"

# The one operator command that clears an identity-resolution stop for the registered career (spec 5.1, ID 01).
# Defined here (not imported from the orchestrator) because the view is built without it.
CONFIRM_LINEAGE_COMMAND = "python -m fm_bot confirm-lineage"

# Store key naming the career/branch the bot manages (a registration pointer, not an operator preference).
ACTIVE_CAREER_KEY = "registry:active_career"
JOURNAL_STOP = "orchestrator.stop"
JOURNAL_STOP_CLEARED = "orchestrator.stop_cleared"
JOURNAL_EXECUTION = "executor.result"
JOURNAL_CONNECTION = "orchestrator.connection"


def set_active_career(store, career_id: str, branch_id: str, *, lineage_confirmed: bool = False) -> int:
    return store.put_setting(ACTIVE_CAREER_KEY, {"career_id": career_id, "branch_id": branch_id, "lineage_confirmed": bool(lineage_confirmed)})


def active_career(store) -> tuple[CareerIdentity, BranchIdentity, bool] | None:
    """The registered career and branch the bot manages, plus whether lineage was confirmed by the operator."""
    stored = store.get_setting(ACTIVE_CAREER_KEY)
    if stored is None:
        return None
    body, _ = stored
    career = store.get_career(body["career_id"])
    branch = store.get_branch(body["branch_id"])
    if career is None or branch is None:
        return None
    return career, branch, bool(body.get("lineage_confirmed"))


def latest_snapshot(store, branch_id: str | None) -> DecisionSnapshot | None:
    if branch_id is None:
        return None
    row = store.connection.execute("SELECT snapshot_id FROM snapshots WHERE branch_id = ? ORDER BY collected_at DESC, rowid DESC LIMIT 1", (branch_id,)).fetchone()
    return store.get_snapshot(row["snapshot_id"]) if row else None


def stop_state(store) -> dict[str, Any]:
    """The Stop control as last journaled: engaged or cleared, by whom and when.

    Read as the newest entry of each kind, so the view keeps following the
    journal however long the bot has been running (see :meth:`Store.latest_journal_entry`).
    """
    entries = [entry for entry in (store.latest_journal_entry(kind=JOURNAL_STOP), store.latest_journal_entry(kind=JOURNAL_STOP_CLEARED)) if entry is not None]
    if not entries:
        return {"engaged": False, "reason": None, "at": None, "status": "never_engaged"}
    last = max(entries, key=lambda e: e["seq"])
    engaged = last["kind"] == JOURNAL_STOP
    return {"engaged": engaged, "reason": last["body"].get("reason") if engaged else None, "at": last["at_utc"], "status": "engaged" if engaged else "cleared"}


def last_execution(store) -> dict[str, Any] | None:
    """The last action result, read as the newest journal entry of its kind (spec 15.1).

    Never the end of a page of the *oldest* entries: once a kind has more
    entries than the page returns, such a read freezes on a stale row and the
    operator's line silently stops following what the bot is doing.
    """
    last = store.latest_journal_entry(kind=JOURNAL_EXECUTION)
    if last is None:
        return None
    return {"action_id": last["body"].get("action_id"), "state": last["body"].get("state"), "reason": last["body"].get("reason"), "at": last["at_utc"]}


def last_connection(store) -> dict[str, Any] | None:
    """The bridge's last reported connection state, read as the newest journal entry of its kind (spec 15.1)."""
    last = store.latest_journal_entry(kind=JOURNAL_CONNECTION)
    if last is None:
        return None
    return {**last["body"], "at": last["at_utc"]}


@dataclass
class OperatorView:
    career: dict[str, Any] | None
    branch: dict[str, Any] | None
    information_mode: str
    authority: dict[str, Any]
    connection: dict[str, Any]
    game_date: str | None
    game_time: str | None
    snapshot: dict[str, Any] | None
    next_action: dict[str, Any] | None
    prerequisites: list[dict[str, Any]]
    continue_gate: dict[str, Any] | None
    stop: dict[str, Any]
    last_execution: dict[str, Any] | None
    model_versions: list[dict[str, Any]]
    settings_versions: dict[str, str]
    pending: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    version: str = STATUS_VIEW_VERSION

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


def build_view(store, *, settings: Settings | None = None, snapshot: DecisionSnapshot | None = None, career: CareerIdentity | None = None, branch: BranchIdentity | None = None, lineage_confirmed: bool | None = None, connection: dict[str, Any] | None = None, next_action: dict[str, Any] | None = None, prerequisites: list[MissingCapabilityReport] | None = None, continue_gate: dict[str, Any] | None = None, stop: dict[str, Any] | None = None, pending: list[dict[str, Any]] | None = None, notes: list[str] | None = None) -> OperatorView:
    """Assemble the view. Anything not passed is read from the store; anything unknown stays ``None``."""
    settings = settings or Settings.load(store)
    if career is None or branch is None:
        active = active_career(store)
        if active:
            career, branch, confirmed = active
            lineage_confirmed = confirmed if lineage_confirmed is None else lineage_confirmed
    career_view = branch_view = None
    if career is not None and branch is not None:
        career_view = {**career.to_json(), "lineage_confirmed": bool(lineage_confirmed)}
        branch_view = branch.to_json()
        snapshot = snapshot or latest_snapshot(store, branch.branch_id)
    career, branch = career_view, branch_view
    profile = settings.authority_profile()
    authority = {"mode": profile.mode.value, "enabled_families": sorted(profile.enabled_families), "version": profile.version, "allow_continue": profile.limits.allow_continue}
    connection = connection or last_connection(store) or {"connected": None, "build_supported": None, "reason": "not checked yet", "session_id": None}
    snapshot_view = None
    if snapshot is not None:
        snapshot_view = {"snapshot_id": snapshot.snapshot_id, "valid": snapshot.valid, "consistency": snapshot.consistency.value, "reasons": list(snapshot.consistency_reasons), "collected_at": snapshot.collected_at, "routes": sorted(snapshot.routes), "unresolved": list(snapshot.unresolved)}
    models = [{"model_id": m.model_id, "version": m.version, "release_state": m.release_state.value, "information_mode": m.information_mode} for m in store.list_model_versions()]
    return OperatorView(career, branch, settings.information_mode().value, authority, connection, snapshot.game_date if snapshot else None, snapshot.game_time if snapshot else None, snapshot_view, next_action, [r.to_json() for r in (prerequisites or [])], continue_gate, stop or stop_state(store), last_execution(store), models, settings.stamp(), list(pending or []), list(notes or []))


# ----- rendering -----

_MODE_TEXT = {"observe": "watching only", "advise": "advising only - no game actions", "scoped_execution": "acting within the enabled families and limits", "club_autonomy": "running the club within the saved profile"}


def _connection_text(connection: dict[str, Any]) -> str:
    if connection.get("connected") is None:
        return f"not checked ({connection.get('reason')})"
    if not connection.get("connected"):
        return f"disconnected ({connection.get('reason') or 'bridge reports no save'})"
    if connection.get("build_supported") is False:
        return f"connected but the game build is not supported ({connection.get('reason')})"
    return "connected"


def render_text(view: OperatorView) -> str:
    """The operator's main screen in football language."""
    lines: list[str] = []
    if view.career:
        lines.append(f"Managing: {view.career['label']} (club {view.career['club_id']}, manager {view.career['manager_id']}); branch {view.branch['label'] or view.branch['kind']} ({view.branch['kind']})" + ("" if view.career.get("lineage_confirmed") else f"; lineage not yet confirmed - run `{CONFIRM_LINEAGE_COMMAND}` (registering again would start a second career)"))
    else:
        lines.append("Managing: no career registered - run `register` first")
    lines.append(f"Information: {view.information_mode.replace('_', ' ')}")
    lines.append(f"Authority: {_MODE_TEXT.get(view.authority['mode'], view.authority['mode'])} (profile v{view.authority['version']}" + (f"; families {', '.join(view.authority['enabled_families'])}" if view.authority["enabled_families"] else "") + ")")
    lines.append(f"Bridge: {_connection_text(view.connection)}")
    if view.game_date:
        lines.append(f"Game day: {view.game_date} {view.game_time or ''}".rstrip())
    else:
        lines.append("Game day: unknown (no consistent snapshot yet)")
    if view.snapshot and not view.snapshot["valid"]:
        lines.append(f"Latest look at the game was not usable: {view.snapshot['consistency']} - " + "; ".join(view.snapshot["reasons"]))
    stop = view.stop
    lines.append("Stop: ENGAGED - no game input will be sent" + (f" ({stop['reason']})" if stop.get("reason") else "") if stop.get("engaged") else "Stop: not engaged")
    if view.next_action:
        action = view.next_action
        lines.append(f"Next: {action.get('kind')} ({action.get('authority_scope', 'scope unknown')})" + (f" - {action['description']}" if action.get("description") else ""))
    else:
        lines.append("Next: nothing proposed")
    if view.continue_gate:
        gate = view.continue_gate
        if gate.get("allowed"):
            lines.append("Calendar: clear to move on")
        else:
            lines.append("Calendar: waiting - " + "; ".join(gate.get("blockers") or ["blocked"]))
    for report in view.prerequisites:
        lines.append(f"Cannot {report['blocked_action']} yet: needs " + ", ".join(f"{name} ({report['reasons'].get(name, 'not available')})" for name in report["missing"]))
    for item in view.pending:
        lines.append(f"Pending: {item.get('description', item.get('action_id'))}")
    if view.last_execution:
        last = view.last_execution
        lines.append(f"Last action: {last['state']} - {last['reason']} ({last['at'][:16]})")
    else:
        lines.append("Last action: none yet")
    for note in view.notes:
        lines.append(f"Note: {note}")
    return "\n".join(lines)


def render_evidence(view: OperatorView) -> str:
    """Expandable detail: identifiers, versions and raw statuses."""
    lines = [f"status view {view.version}"]
    if view.career:
        lines.append(f"career {view.career['career_id']} build {view.career['build']}; branch {view.branch['branch_id']} parent {view.branch.get('parent_branch_id')} checkpoint {view.branch.get('checkpoint_id')}")
    lines.append(f"connection {view.connection}")
    if view.snapshot:
        lines.append(f"snapshot {view.snapshot['snapshot_id']} {view.snapshot['consistency']} collected {view.snapshot['collected_at']} routes {view.snapshot['routes']}")
        if view.snapshot["unresolved"]:
            lines.append("bridge unresolved: " + ", ".join(view.snapshot["unresolved"]))
    lines.append("settings: " + ", ".join(f"{k}={v}" for k, v in sorted(view.settings_versions.items())))
    for model in view.model_versions:
        lines.append(f"model {model['model_id']} {model['version']} {model['release_state']} ({model['information_mode']})")
    if not view.model_versions:
        lines.append("models: none registered")
    if view.next_action:
        lines.append(f"next action {view.next_action}")
    if view.continue_gate:
        lines.append(f"continue gate {view.continue_gate}")
    return "\n".join(lines)
