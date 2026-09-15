"""``python -m fm_bot``: the operator's command line (spec 15.1).

Subcommands, all read-only towards the game unless ``run`` is given an
executing authority mode and a UI adapter that reports validated workflows:

* ``status``     the operator view (career, information mode, authority, bridge, game day, next action, prerequisites, Stop)
* ``register``   register the loaded save as a career (manifest from the bridge; ``--confirm-lineage`` when the operator vouches for it)
* ``confirm-lineage``  vouch that the loaded save is the *registered* career, clearing an identity-resolution stop without forking the history (ID 01)
* ``snapshot``   collect one consistent snapshot and report its consistency
* ``plan``       plan once and print the football-language report (``--json`` for the full record)
* ``explain``    a decision or an executed action resolved to its inputs, limits, versions and evidence (AUD 01)
* ``config``     get or set operator settings (versioned, journaled)
* ``run``        the orchestrator: ``--once`` or ``--iterations N``; ``--authority`` changes the persisted authority mode first
* ``reconcile``  reconcile intents left EXECUTING/VERIFYING by an earlier process (REC 01)

The bridge is reached through ``--bridge-url`` (loopback only). The UI adapter
is the in-memory fake unless ``--adapter windows`` is passed, and the Windows
adapter fails closed: it reports no validated workflow, so nothing needing
``ui_action_adapter`` can run. ``TRANSPORT_FACTORY`` and ``ADAPTER_FACTORY``
are module-level hooks tests use to substitute the scripted fixture world.

Exit codes: 0 done; 1 the game or bridge state prevented the command
(disconnected, inconsistent snapshot); 2 a setup or usage problem (no
career registered, unknown id, invalid setting); 3 another bot instance
holds the manager lock.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable, TextIO

from . import BRIDGE_DEFAULT_URL, __version__
from .bridge_client.client import BridgeClient
from .bridge_client.transport import HttpTransport, Transport
from .execution.adapter import FakeAdapter, WindowsAdapter
from .execution.reconciliation import reconcile_on_restart
from .interface.controls import AUTHORITY_MODE_ALIASES, Settings, SettingsError, parse_setting_value
from .interface.explain import ExplanationError, explain_action, explain_decision, render_action, render_decision, render_decision_evidence
from .interface.notify import LogSink, Notifier
from .interface.status import active_career, build_view, render_evidence, render_text, set_active_career
from .orchestrator import DEFAULT_ROUTES, JOURNAL_LINEAGE_CONFIRMED, OPTIONAL_ROUTES, ManagerLockHeld, Orchestrator, PassResult, PassStatus, run_planner
from .rules.capabilities import CapabilityRegistry
from .state.identity import CareerRegistry, SaveManifest, file_sha256
from .state.records import DecisionSnapshot
from .state.snapshot import CollectionContext, SnapshotCollector, SnapshotRequirements
from .state.store import Store

CLI_VERSION = "fm_bot.cli/1"
DEFAULT_DB = "fm_bot.sqlite3"
ADAPTER_NAMES = ("fake", "windows")
AUTHORITY_CHOICES = ("observe", "advise", "scoped", "autonomy", "scoped_execution", "club_autonomy")

EXIT_OK, EXIT_GAME_STATE, EXIT_SETUP, EXIT_LOCK = 0, 1, 2, 3
# Pass outcomes in which the game or bridge state prevented the command, so ``run`` exits EXIT_GAME_STATE
# (an inconsistent snapshot or an unsettled clock is such a state, and so is a stop for identity resolution).
PREVENTED_BY_GAME_STATE: tuple[PassStatus, ...] = (PassStatus.DISCONNECTED, PassStatus.BUILD_UNSUPPORTED, PassStatus.IDENTITY_MISMATCH, PassStatus.IDENTITY_RESOLUTION_REQUIRED, PassStatus.INCONSISTENT, PassStatus.LOCK_LOST)

# Test hooks: replace the loopback HTTP transport / the UI adapter without touching the command line.
TRANSPORT_FACTORY: Callable[[str], Transport] | None = None
ADAPTER_FACTORY: Callable[[str], Any] | None = None
# Clock and sleep handed to the orchestrator by ``run``; tests inject a fake clock so nothing waits.
CLOCK: Any = None
SLEEP: Callable[[float], None] | None = None


class CliError(Exception):
    def __init__(self, message: str, code: int = EXIT_SETUP):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------


def make_transport(url: str) -> Transport:
    if TRANSPORT_FACTORY is not None:
        return TRANSPORT_FACTORY(url)
    try:
        return HttpTransport(url)
    except ValueError as exc:
        raise CliError(str(exc)) from exc


def make_adapter(name: str) -> Any:
    if ADAPTER_FACTORY is not None:
        return ADAPTER_FACTORY(name)
    if name == "fake":
        return FakeAdapter()
    if name == "windows":
        return WindowsAdapter()
    raise CliError(f"unknown adapter {name!r}; choose one of {ADAPTER_NAMES}")


def adapter_line(adapter: Any) -> str:
    """One line saying what the UI adapter can do; the Windows adapter states why it cannot act."""
    capabilities = list(adapter.capabilities())
    if capabilities:
        return f"UI adapter {adapter.name}: workflows reported for {', '.join(capabilities)}"
    reason = getattr(adapter, "status_reason", None) or "no validated workflow"
    return f"UI adapter {adapter.name}: cannot act on the game ({reason})"


def _require_career(store: Store):
    active = active_career(store)
    if active is None:
        raise CliError("no career registered in this database; run `register --label ... --save-path ...` first")
    return active


def _client(args: argparse.Namespace, store: Store, career=None, branch=None) -> BridgeClient:
    context = {"career_id": career.career_id, "branch_id": branch.branch_id} if career is not None and branch is not None else None
    return BridgeClient(make_transport(args.bridge_url), store, context=context)


def _collect(client: BridgeClient, store: Store, settings: Settings, career, branch, lineage_confirmed: bool, label: str) -> DecisionSnapshot:
    requirements = SnapshotRequirements(routes=[*DEFAULT_ROUTES, *OPTIONAL_ROUTES], optional=list(OPTIONAL_ROUTES), label=label)
    context = CollectionContext(career.career_id, branch.branch_id, settings.information_mode(), None, lineage_confirmed, False, None, 0)
    return SnapshotCollector(client, store).collect(requirements, context)


def snapshot_lines(snapshot: DecisionSnapshot) -> list[str]:
    """Consistency in plain words: usable or not, and why."""
    lines = [f"Snapshot {snapshot.snapshot_id}: " + ("consistent - usable for decisions" if snapshot.valid else f"NOT usable ({snapshot.consistency.value})")]
    lines.append(f"Game day: {snapshot.game_date or 'unknown'} {snapshot.game_time or ''}".rstrip())
    lines.append(f"Looked at: {', '.join(sorted(snapshot.routes)) or 'nothing'} (attempt {snapshot.attempts})")
    for reason in snapshot.consistency_reasons:
        lines.append(f"  - {reason}")
    if snapshot.continuity:
        lines.append(f"Continuity: {snapshot.continuity.get('status')}" + (f" - {'; '.join(snapshot.continuity.get('reasons', []))}" if snapshot.continuity.get("reasons") else ""))
    return lines


def render_pass(index: int, result: PassResult) -> str:
    """One orchestrator pass in football language."""
    day = f"{result.game_date} {result.game_time or ''}".strip() if result.game_date else "game day unknown"
    head = f"Pass {index} ({result.level.value.replace('_', ' ')}): {result.status.value.replace('_', ' ')} - {day}"
    lines = [head]
    if result.triggers:
        lines.append("  Changed since last time: " + "; ".join(t.description for t in result.triggers))
    if result.plan is not None:
        for line in result.plan.summary_lines:
            lines.append(f"  {line}")
    if result.next_action:
        lines.append(f"  Next: {result.next_action.get('description') or result.next_action.get('kind')}")
    if result.continue_gate is not None:
        gate = result.continue_gate
        lines.append("  Calendar: clear to move on" if gate.allowed else "  Calendar: waiting - " + "; ".join(gate.blockers or [f"missing {', '.join(gate.missing_capabilities.missing)}"]))
    for report in result.blocked:
        lines.append(f"  Cannot {report.blocked_action} yet: needs " + ", ".join(report.missing))
    if result.executed is not None:
        lines.append(f"  Acted: {result.executed.action_id} {result.executed.state.value} - {result.executed.reason}")
    for note in result.notes:
        lines.append(f"  Note: {note}")
    lines.append(f"  Notifications: {result.notifications}; next look in {result.next_poll_seconds:g} s")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace, store: Store, out: TextIO) -> int:
    settings = Settings.load(store)
    view = build_view(store, settings=settings)
    if args.json:
        print(json.dumps(view.to_json(), indent=2, sort_keys=True, default=str), file=out)
        return EXIT_OK
    print(render_text(view), file=out)
    if args.evidence:
        print(render_evidence(view), file=out)
    return EXIT_OK


def cmd_register(args: argparse.Namespace, store: Store, out: TextIO) -> int:
    client = _client(args, store)
    state = client.connection_state()
    if not state["connected"]:
        raise CliError(f"cannot register: the bridge is not connected to a save ({state.get('reason')})", EXIT_GAME_STATE)
    if not state["build_supported"]:
        raise CliError(f"cannot register: {state.get('reason')}", EXIT_GAME_STATE)
    manager, club, game = client.manager(), client.club(), client.game()
    for name, response in (("/manager", manager), ("/club", club), ("/game", game)):
        if not response.ok:
            raise CliError(f"cannot register: {name} unavailable ({response.error})", EXIT_GAME_STATE)
    checksum = file_sha256(args.save_path) if args.save_path and os.path.isfile(args.save_path) else None
    manifest = SaveManifest(state["build"], int(manager.data["id"]), int(club.data["id"]), game.data.get("date"), game.data.get("time"), path=args.save_path, checksum_sha256=checksum, notes="registered from the command line")
    career, branch, checkpoint = CareerRegistry(store).register_career(args.label, manifest)
    set_active_career(store, career.career_id, branch.branch_id, lineage_confirmed=bool(args.confirm_lineage))
    saved = Settings.load(store).save()
    print(f"Registered career {career.career_id} ({args.label}): club {club.data.get('name', club.data['id'])}, manager {manager.data.get('name', manager.data['id'])}, build {state['build']}", file=out)
    print(f"Branch {branch.branch_id} (production), checkpoint {checkpoint.checkpoint_id} at {manifest.game_date} {manifest.game_time or ''}".rstrip(), file=out)
    print("Lineage: confirmed by the operator" if args.confirm_lineage else "Lineage: not confirmed - run `confirm-lineage` (not `register` again, which would start a second career) before the bot trusts this save", file=out)
    if checksum is None and args.save_path:
        print(f"Save file not found at {args.save_path}; checksum not recorded", file=out)
    if saved:
        print(f"Settings saved with defaults: {', '.join(sorted(saved))}", file=out)
    return EXIT_OK


def cmd_confirm_lineage(args: argparse.Namespace, store: Store, out: TextIO) -> int:
    """Vouch that the loaded save is the registered career (spec 5.1, ID 01).

    This is the only way to clear an identity-resolution stop: it confirms the
    lineage of the career and branch already registered, and refuses when the
    loaded save's manager, club or build is not the registered one (that is a
    different career, which must be registered as its own). Nothing new is
    created, so the production history stays linear.
    """
    career, branch, confirmed = _require_career(store)
    client = _client(args, store, career, branch)
    state = client.connection_state()
    if not state["connected"]:
        raise CliError(f"cannot confirm lineage: the bridge is not connected to a save ({state.get('reason')})", EXIT_GAME_STATE)
    if not state["build_supported"]:
        raise CliError(f"cannot confirm lineage: {state.get('reason')}", EXIT_GAME_STATE)
    manager, club, game = client.manager(), client.club(), client.game()
    for name, response in (("/manager", manager), ("/club", club), ("/game", game)):
        if not response.ok:
            raise CliError(f"cannot confirm lineage: {name} unavailable ({response.error})", EXIT_GAME_STATE)
    matched, problems = CareerRegistry(store).matches_registration(career, state["status"], int(manager.data["id"]), int(club.data["id"]))
    if not matched:
        raise CliError("cannot confirm lineage: the loaded save is not the registered career (" + "; ".join(problems) + "); register it as its own career instead of confirming this one", EXIT_GAME_STATE)
    set_active_career(store, career.career_id, branch.branch_id, lineage_confirmed=True)
    reason = args.reason or "operator confirmed the loaded save is the registered career"
    store.journal(JOURNAL_LINEAGE_CONFIRMED, {"reason": reason, "career_id": career.career_id, "branch_id": branch.branch_id, "game_date": game.data.get("date"), "game_time": game.data.get("time"), "build": state["build"], "by": "cli", "was_confirmed": confirmed}, branch.branch_id)
    print(f"Lineage confirmed for career {career.career_id} ({career.label}), branch {branch.branch_id}: {reason}", file=out)
    print(f"Loaded save: club {club.data.get('name', club.data['id'])}, manager {manager.data.get('name', manager.data['id'])}, build {state['build']}, game day {game.data.get('date')} {game.data.get('time') or ''}".rstrip(), file=out)
    print("No new career, branch or checkpoint was created; the next run reconnects, settles and reconciles from this save.", file=out)
    return EXIT_OK


def cmd_snapshot(args: argparse.Namespace, store: Store, out: TextIO) -> int:
    career, branch, confirmed = _require_career(store)
    settings = Settings.load(store)
    client = _client(args, store, career, branch)
    snapshot = _collect(client, store, settings, career, branch, confirmed, "cli.snapshot")
    if args.json:
        print(json.dumps({k: v for k, v in snapshot.to_json().items() if k != "routes"}, indent=2, sort_keys=True, default=str), file=out)
    else:
        print("\n".join(snapshot_lines(snapshot)), file=out)
    return EXIT_OK if snapshot.valid else EXIT_GAME_STATE


def _capabilities(client: BridgeClient, adapter: Any) -> CapabilityRegistry:
    state = client.connection_state()
    registry = CapabilityRegistry.from_status(state["status"], supported_builds=client.supported_builds)
    for name in adapter.capabilities():
        registry.provide(name, f"ui_adapter:{adapter.name}", "reported by the adapter; verified per workflow")
    return registry


def cmd_plan(args: argparse.Namespace, store: Store, out: TextIO) -> int:
    career, branch, confirmed = _require_career(store)
    settings = Settings.load(store)
    client = _client(args, store, career, branch)
    adapter = make_adapter(args.adapter)
    registry = _capabilities(client, adapter)
    snapshot = _collect(client, store, settings, career, branch, confirmed, "cli.plan")
    if not snapshot.valid:
        print("\n".join(snapshot_lines(snapshot)), file=out)
        print("No plan: the game state could not be read consistently.", file=out)
        return EXIT_GAME_STATE
    outcome = run_planner(snapshot, store=store, settings=settings, capabilities=registry, build=(client.last_status or {}).get("build"))
    if args.json:
        body = outcome.to_json()
        body["decisions"] = [d.to_json() for d in outcome.decisions]
        body["settings"] = settings.stamp()
        print(json.dumps(body, indent=2, sort_keys=True, default=str), file=out)
        return EXIT_OK
    print(f"Plan for {snapshot.game_date} {snapshot.game_time or ''} ({outcome.status}) - {adapter_line(adapter)}".replace("  ", " "), file=out)
    for line in outcome.summary_lines:
        print(f"  {line}", file=out)
    if outcome.status == "unavailable":
        print(f"  Plan unavailable: {outcome.reason}", file=out)
    lineup = outcome.lineup_status
    if lineup is not None:
        print(f"  Team sheet: {lineup.status}" + (" - " + "; ".join(lineup.reasons) if lineup.reasons else ""), file=out)
    print("  Next: " + (outcome.next_action.get("description") or outcome.next_action["kind"]) if outcome.next_action else "  Next: nothing proposed for execution (advice recorded)", file=out)
    for report in outcome.missing:
        print(f"  Cannot {report.blocked_action} yet: needs " + ", ".join(report.missing), file=out)
    print("  Decisions recorded: " + ", ".join(f"{d.kind} {d.decision_id}" for d in outcome.decisions) if outcome.decisions else "  Decisions recorded: none", file=out)
    return EXIT_OK


def cmd_explain(args: argparse.Namespace, store: Store, out: TextIO) -> int:
    identifier = args.identifier
    try:
        if store.get_intent(identifier) is not None:
            explanation = explain_action(identifier, store)
            if args.json:
                print(json.dumps(explanation.to_json(), indent=2, sort_keys=True, default=str), file=out)
            else:
                print(render_action(explanation), file=out)
                if args.evidence:
                    print(render_decision_evidence(explanation.decision), file=out)
            return EXIT_OK
        if store.get_decision(identifier) is not None:
            explanation = explain_decision(identifier, store)
            if args.json:
                print(json.dumps(explanation.to_json(), indent=2, sort_keys=True, default=str), file=out)
            else:
                print(render_decision(explanation), file=out)
                if args.evidence:
                    print(render_decision_evidence(explanation), file=out)
            return EXIT_OK
    except ExplanationError as exc:
        raise CliError(f"cannot explain {identifier}: {exc}") from exc
    raise CliError(f"{identifier} is neither a decision nor an action in this database")


def cmd_config(args: argparse.Namespace, store: Store, out: TextIO) -> int:
    settings = Settings.load(store)
    if args.config_command == "get":
        names = [args.name] if args.name else sorted(settings.items)
        unknown = [n for n in names if n not in settings.items]
        if unknown:
            raise CliError(f"unknown setting {unknown[0]!r}; known: {sorted(settings.items)}")
        if args.json:
            print(json.dumps({n: settings.items[n].to_json() for n in names}, indent=2, sort_keys=True), file=out)
            return EXIT_OK
        for name in names:
            item = settings.items[name]
            print(f"{name} (v{item.version}{'' if item.saved else ', default not yet saved'}): {json.dumps(item.value, sort_keys=True)}", file=out)
        print(f"authority profile v{settings.authority_profile_version}", file=out)
        return EXIT_OK
    try:
        item = settings.change(args.name, parse_setting_value(args.name, args.value), reason=args.reason or "", by="cli")
    except SettingsError as exc:
        raise CliError(f"setting not changed: {exc}") from exc
    print(f"{item.name} is now v{item.version}: {json.dumps(item.value, sort_keys=True)}", file=out)
    print(f"authority profile v{settings.authority_profile_version}", file=out)
    return EXIT_OK


def cmd_run(args: argparse.Namespace, store: Store, out: TextIO) -> int:
    career, branch, confirmed = _require_career(store)
    settings = Settings.load(store)
    if args.authority:
        item = settings.change("authority_mode", AUTHORITY_MODE_ALIASES.get(args.authority, args.authority), reason="run --authority", by="cli")
        print(f"Authority mode set to {item.value} (v{item.version}; profile v{settings.authority_profile_version})", file=out)
    client = _client(args, store, career, branch)
    adapter = make_adapter(args.adapter)
    print(adapter_line(adapter), file=out)
    iterations = 1 if args.once or not args.iterations else int(args.iterations)
    try:
        orchestrator = Orchestrator(store, client, adapter, settings, career=career, branch=branch, lineage_confirmed=confirmed, notifier=Notifier([LogSink()], store=store), clock=CLOCK, sleep=SLEEP)
    except ManagerLockHeld as exc:
        raise CliError(str(exc), EXIT_LOCK) from exc
    try:
        results = orchestrator.run(max_iterations=iterations)
        for index, result in enumerate(results, 1):
            print(render_pass(index, result), file=out)
        print(render_text(orchestrator.status_view()), file=out)
    finally:
        orchestrator.close()
    last = results[-1] if results else None
    return EXIT_GAME_STATE if last is not None and last.status in PREVENTED_BY_GAME_STATE else EXIT_OK


def cmd_reconcile(args: argparse.Namespace, store: Store, out: TextIO) -> int:
    career, branch, confirmed = _require_career(store)
    settings = Settings.load(store)
    client = _client(args, store, career, branch)
    adapter = make_adapter(args.adapter)
    print(adapter_line(adapter), file=out)
    snapshot = _collect(client, store, settings, career, branch, confirmed, "cli.reconcile")
    if not snapshot.valid:
        print("\n".join(snapshot_lines(snapshot)), file=out)
        print("Reconciliation needs a consistent snapshot; nothing was changed.", file=out)
        return EXIT_GAME_STATE
    decisions = reconcile_on_restart(store, adapter, snapshot, branch_id=branch.branch_id)
    if not decisions:
        print("No action was left in flight; nothing to reconcile.", file=out)
        return EXIT_OK
    for decision in decisions:
        print(f"{decision.action_id} ({decision.kind}): {decision.previous_state.value} -> {decision.new_state.value} - {decision.reason}", file=out)
    return EXIT_OK


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m fm_bot", description="FM24 club management bot - operator command line")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite database path (default {DEFAULT_DB}; ':memory:' for a throwaway)")
    parser.add_argument("--bridge-url", default=BRIDGE_DEFAULT_URL, help="loopback URL of the observation bridge")
    parser.add_argument("--adapter", choices=ADAPTER_NAMES, default="fake", help="UI adapter: the in-memory fake (default) or the fail-closed Windows adapter")
    parser.add_argument("--version", action="version", version=f"fm_bot {__version__} ({CLI_VERSION})")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="operator view")
    status.add_argument("--evidence", action="store_true", help="also print identifiers, versions and raw statuses")
    status.add_argument("--json", action="store_true")

    register = sub.add_parser("register", help="register the loaded save as a career")
    register.add_argument("--label", required=True)
    register.add_argument("--save-path", default=None, help="path of the save file (checksum recorded when the file exists)")
    register.add_argument("--confirm-lineage", action="store_true", help="the operator vouches that this save is the registered career")

    confirm = sub.add_parser("confirm-lineage", help="vouch that the loaded save is the registered career (clears an identity-resolution stop)")
    confirm.add_argument("--reason", default="", help="why the operator vouches for this save (journaled)")

    snapshot = sub.add_parser("snapshot", help="collect one snapshot and report its consistency")
    snapshot.add_argument("--json", action="store_true")

    plan = sub.add_parser("plan", help="plan once and print the report")
    plan.add_argument("--json", action="store_true")

    explain = sub.add_parser("explain", help="explain a decision or an action by id")
    explain.add_argument("identifier")
    explain.add_argument("--evidence", action="store_true")
    explain.add_argument("--json", action="store_true")

    config = sub.add_parser("config", help="operator settings")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    get = config_sub.add_parser("get")
    get.add_argument("name", nargs="?")
    get.add_argument("--json", action="store_true")
    set_ = config_sub.add_parser("set")
    set_.add_argument("name")
    set_.add_argument("value", help="JSON where it parses, otherwise the raw text")
    set_.add_argument("--reason", default="")

    run = sub.add_parser("run", help="run the orchestrator")
    run.add_argument("--once", action="store_true")
    run.add_argument("--iterations", type=int, default=None)
    run.add_argument("--authority", choices=AUTHORITY_CHOICES, default=None, help="persist this authority mode before running")

    sub.add_parser("reconcile", help="reconcile actions left in flight by an earlier process")
    return parser


HANDLERS: dict[str, Callable[[argparse.Namespace, Store, TextIO], int]] = {
    "status": cmd_status, "register": cmd_register, "confirm-lineage": cmd_confirm_lineage, "snapshot": cmd_snapshot,
    "plan": cmd_plan, "explain": cmd_explain, "config": cmd_config, "run": cmd_run, "reconcile": cmd_reconcile,
}


def main(argv: list[str] | None = None, *, out: TextIO | None = None, err: TextIO | None = None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    args = build_parser().parse_args(argv)
    if args.command == "run" and args.iterations is not None and args.iterations < 1:
        print("--iterations must be at least 1", file=err)
        return EXIT_SETUP
    store = Store(args.db)
    try:
        return HANDLERS[args.command](args, store, out)
    except CliError as exc:
        print(f"error: {exc}", file=err)
        return exc.code
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
