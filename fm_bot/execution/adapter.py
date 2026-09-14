"""UI execution adapter contract, versioned screen models and a fake FM UI (spec 12.1, BOT 009).

The adapter is the only component that sends input to Football Manager. It is
deliberately separate from the bridge (which only reads memory) and from the
planner (which never touches the UI). Three implementations live here:

* :class:`UIAdapter` - the protocol every adapter honours;
* :class:`FakeAdapter` - an in-memory fake FM front end with fault injection so
  the executor, verifier and reconciliation can be tested offline (ACT 01-03,
  REC 01);
* :class:`WindowsAdapter` - a fail-closed stub. FM accessibility support is
  untested. Until a workflow has been validated against the real client on a
  supported configuration, the stub provides no capabilities and identifies no
  screen, so every action that needs the UI is blocked by the capability gate.

Everything in a :class:`ScreenModel` is data: screen ids, legal actions per
screen, expected transitions, timeouts and recovery states, tagged with a
version string. A change in display scaling, window geometry, language, skin
or focus invalidates a workflow until :func:`validate_environment` passes.
"""
from __future__ import annotations

import platform
import threading
from dataclasses import dataclass, field, asdict
from typing import Any, Protocol, runtime_checkable

from ..state.identity import utc_now
from ..state.status import Observed, ValueStatus

ADAPTER_CONTRACT_VERSION = "execution.adapter/1"

RISK_NAVIGATION = "navigation"
RISK_CONSEQUENTIAL = "consequential"

# Step outcomes. ``done`` is the only success. Everything else describes why no
# effect can be assumed; the executor decides the intent state from these.
STEP_DONE = "done"
STEP_TIMEOUT = "timeout"
STEP_STOPPED = "stopped"
STEP_NO_FOCUS = "no_focus"
STEP_UNEXPECTED_SCREEN = "unexpected_screen"
STEP_UNKNOWN_SCREEN = "unknown_screen"
STEP_ILLEGAL = "illegal_action"
STEP_ERROR = "error"
STEP_UNSUPPORTED = "unsupported"

ANY_SCREEN = "*"


class AdapterCrash(RuntimeError):
    """The adapter lost contact with the UI mid-step; the effect is unknown."""


# ---------------------------------------------------------------------------
# Observations and steps
# ---------------------------------------------------------------------------


@dataclass
class ScreenObservation:
    """What the adapter believes the game is showing right now.

    ``screen_id`` is ``None`` when the screen is unidentified; the adapter must
    then refuse to send input. ``method`` records how the screen was
    recognised so that screenshot-based recognition is never mistaken for an
    accessibility read.
    """

    screen_id: str | None
    confidence: float
    geometry: dict[str, int]
    scaling: float | None
    language: str | None
    skin: str | None
    focused: bool
    observed_at: str
    method: str                              # "accessibility" | "screen_recognition" | "fake"
    reason: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def identified(self) -> bool:
        return self.screen_id is not None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UIStep:
    """One input the adapter may send: an action on an expected screen."""

    step_id: str
    action: str                              # e.g. "navigate", "select_tactic", "accept_offer"
    screen: str                              # screen the step expects before input, or ANY_SCREEN
    params: dict[str, Any] = field(default_factory=dict)
    risk_class: str = RISK_CONSEQUENTIAL
    expect_screen: str | None = None         # screen expected after the input
    timeout_ms: int = 5000

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StepResult:
    ok: bool
    status: str                              # STEP_* constant
    screen_after: ScreenObservation | None
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "status": self.status, "screen_after": self.screen_after.to_json() if self.screen_after else None, "evidence": dict(self.evidence), "error": self.error}


class StopFlag:
    """Visible Stop control. Once set, no adapter sends another input until cleared by the operator."""

    def __init__(self):
        self._event = threading.Event()
        self.reason: str | None = None

    def set(self, reason: str = "operator stop") -> None:
        self.reason = reason
        self._event.set()

    def clear(self) -> None:
        self.reason = None
        self._event.clear()

    def is_set(self) -> bool:
        return self._event.is_set()


@runtime_checkable
class UIAdapter(Protocol):
    """The contract the executor relies on. Every method must be safe to call at any time."""

    name: str

    def capabilities(self) -> list[str]:
        """Capability names this adapter verifiably provides right now (e.g. ``ui_action_adapter``)."""

    def identify_screen(self) -> ScreenObservation: ...

    def has_focus(self) -> bool: ...

    def perform(self, step: UIStep) -> StepResult: ...

    def readback(self, kind: str, params: dict[str, Any] | None = None) -> Observed:
        """Independent readback of committed UI state (selected tactic, lineup, training, agreement)."""

    def stop(self, reason: str = "operator stop") -> None: ...


# ---------------------------------------------------------------------------
# Screen model and workflows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnvironmentSpec:
    """The validated set of display settings a screen model was tested against."""

    scalings: frozenset[float]
    min_width: int
    min_height: int
    languages: frozenset[str]
    skins: frozenset[str]
    min_confidence: float = 0.9


@dataclass(frozen=True)
class ScreenSpec:
    screen_id: str
    legal_actions: frozenset[str]
    transitions: dict[str, str]              # action -> expected screen after
    timeout_ms: int = 5000
    recovery: tuple[str, ...] = ()           # ordered recovery actions, e.g. ("navigate:home",)

    def legal(self, action: str) -> bool:
        return action in self.legal_actions


@dataclass(frozen=True)
class ScreenModel:
    version: str
    screens: dict[str, ScreenSpec]
    environment: EnvironmentSpec
    home_screen: str

    def legal(self, screen_id: str | None, action: str) -> bool:
        if screen_id is None:
            return False
        spec = self.screens.get(screen_id)
        return bool(spec and spec.legal(action))

    def expected_after(self, screen_id: str, action: str, params: dict[str, Any]) -> str | None:
        spec = self.screens.get(screen_id)
        if spec is None:
            return None
        if action == "navigate":
            return params.get("target")
        return spec.transitions.get(action)


@dataclass(frozen=True)
class Workflow:
    """A tested sequence of steps for one action kind against one screen model version."""

    workflow_id: str
    version: str
    kind: str
    screen_model: ScreenModel
    steps: tuple[UIStep, ...]
    readback_kind: str | None
    required_capabilities: tuple[str, ...] = ("ui_action_adapter",)

    def instantiate(self, parameters: dict[str, Any]) -> list[UIStep]:
        """Fill step parameters from an intent's parameters. Unknown keys are ignored."""
        steps = []
        for template in self.steps:
            params = {key: parameters.get(key, value) if value is None else value for key, value in template.params.items()}
            steps.append(UIStep(template.step_id, template.action, template.screen, params, template.risk_class, template.expect_screen, template.timeout_ms))
        return steps

    @property
    def entry_screen(self) -> str:
        return self.steps[0].screen if self.steps else ANY_SCREEN


def validate_environment(observation: ScreenObservation, model: ScreenModel, *, require_focus: bool = True) -> list[str]:
    """Problems that invalidate every workflow on ``model`` until rechecked (spec 12.1).

    ``require_focus=False`` lets the executor report focus separately: a
    focus loss pauses work at the next safe boundary rather than invalidating
    the screen model.
    """
    env = model.environment
    problems: list[str] = []
    if observation.scaling is None:
        problems.append("display scaling unknown")
    elif observation.scaling not in env.scalings:
        problems.append(f"display scaling {observation.scaling} not in validated set {sorted(env.scalings)}")
    width, height = observation.geometry.get("width"), observation.geometry.get("height")
    if width is None or height is None:
        problems.append("window geometry unknown")
    elif width < env.min_width or height < env.min_height:
        problems.append(f"window {width}x{height} smaller than validated minimum {env.min_width}x{env.min_height}")
    if observation.language not in env.languages:
        problems.append(f"language {observation.language!r} not validated (expected one of {sorted(env.languages)})")
    if observation.skin not in env.skins:
        problems.append(f"skin {observation.skin!r} not validated (expected one of {sorted(env.skins)})")
    if require_focus and not observation.focused:
        problems.append("game window does not have input focus")
    if not observation.identified:
        problems.append(f"screen unidentified: {observation.reason or 'no recognition result'}")
    elif observation.confidence < env.min_confidence:
        problems.append(f"screen recognition confidence {observation.confidence:.2f} below {env.min_confidence:.2f}")
    return problems


# ---------------------------------------------------------------------------
# The fake FM front end used by offline tests
# ---------------------------------------------------------------------------

FAKE_SCREEN_MODEL_VERSION = "fake-screen-model/1"

FAKE_ENVIRONMENT = EnvironmentSpec(frozenset({1.0}), 1920, 1080, frozenset({"en"}), frozenset({"default"}))


def _screen(screen_id: str, actions: tuple[str, ...], transitions: dict[str, str] | None = None, recovery: tuple[str, ...] = ("navigate:home",)) -> ScreenSpec:
    return ScreenSpec(screen_id, frozenset(("navigate", *actions)), dict(transitions or {}), 5000, recovery)


FAKE_SCREEN_MODEL = ScreenModel(
    FAKE_SCREEN_MODEL_VERSION,
    {
        "home": _screen("home", ()),
        "tactics": _screen("tactics", ("select_tactic",), {"select_tactic": "tactics"}),
        "squad.selection": _screen("squad.selection", ("set_lineup",), {"set_lineup": "squad.selection"}),
        "training": _screen("training", ("set_training",), {"set_training": "training"}),
        "inbox": _screen("inbox", ("open_message",), {"open_message": "inbox.offer"}),
        "inbox.offer": _screen("inbox.offer", ("accept_offer", "reject_offer"), {"accept_offer": "inbox", "reject_offer": "inbox"}),
        # A recognised-but-unwanted screen: only navigation (recovery) is legal on it.
        "unexpected": ScreenSpec("unexpected", frozenset({"navigate"}), {}, 5000, ("navigate:home",)),
    },
    FAKE_ENVIRONMENT,
    "home",
)


def _nav(step_id: str, target: str) -> UIStep:
    return UIStep(step_id, "navigate", ANY_SCREEN, {"target": target}, RISK_NAVIGATION, target)


# Workflows against the fake screen model. ``None`` parameter values are filled from the intent.
FAKE_WORKFLOWS: dict[str, Workflow] = {
    "select_validated_tactic": Workflow("fake.select_tactic", "1", "select_validated_tactic", FAKE_SCREEN_MODEL, (
        _nav("go_tactics", "tactics"),
        UIStep("select", "select_tactic", "tactics", {"tactic_catalog_id": None, "catalog_version": None}, RISK_CONSEQUENTIAL, "tactics"),
    ), "selected_tactic", ("ui_action_adapter", "tactic_selection_ui", "tactic_catalog_readback")),
    "submit.lineup": Workflow("fake.submit_lineup", "1", "submit.lineup", FAKE_SCREEN_MODEL, (
        _nav("go_selection", "squad.selection"),
        UIStep("set", "set_lineup", "squad.selection", {"player_ids": None, "roles": None}, RISK_CONSEQUENTIAL, "squad.selection"),
    ), "lineup"),
    "set.training": Workflow("fake.set_training", "1", "set.training", FAKE_SCREEN_MODEL, (
        _nav("go_training", "training"),
        UIStep("set", "set_training", "training", {"settings": None}, RISK_CONSEQUENTIAL, "training"),
    ), "training_settings"),
    "commit.contract": Workflow("fake.accept_offer", "1", "commit.contract", FAKE_SCREEN_MODEL, (
        _nav("go_inbox", "inbox"),
        UIStep("open", "open_message", "inbox", {"offer_id": None}, RISK_NAVIGATION, "inbox.offer"),
        UIStep("accept", "accept_offer", "inbox.offer", {"offer_id": None}, RISK_CONSEQUENTIAL, "inbox"),
    ), "agreement"),
    "navigate": Workflow("fake.navigate", "1", "navigate", FAKE_SCREEN_MODEL, (
        UIStep("go", "navigate", ANY_SCREEN, {"target": None}, RISK_NAVIGATION, None),
    ), None),
}

FAULT_KINDS = ("timeout_after_success", "timeout_before_effect", "focus_loss", "unexpected_screen", "crash")


@dataclass
class InjectedFault:
    kind: str
    on_step: int                              # 1-based index of the perform() call the fault hits
    fired: bool = False


class FakeAdapter:
    """An in-memory Football Manager front end.

    State is small on purpose: the selected tactic, the lineup, training
    intensity, which offers have been accepted, the current screen and the
    focus flag. Faults are injected per ``perform`` call so a test can make a
    step time out *after* its effect landed (ACT 02) or before it, lose focus,
    land on an unexpected screen, or crash mid-input.
    """

    name = "fake"
    method = "fake"

    def __init__(self, *, tactic_catalog: dict[str, str] | None = None, selected_tactic_id: str | None = None, lineup_ids: list[int] | None = None, lineup_roles: dict[str, str] | None = None, training_settings: dict[str, Any] | None = None, offers: dict[str, dict[str, Any]] | None = None, screen: str = "home", focused: bool = True, scaling: float = 1.0, geometry: dict[str, int] | None = None, language: str = "en", skin: str = "default", capabilities: tuple[str, ...] = ("ui_action_adapter", "tactic_selection_ui", "tactic_catalog_readback")):
        self.tactic_catalog = dict(tactic_catalog or {"balanced-01": "4-4-2 Balanced", "counter-02": "4-2-3-1 Counter"})
        self.selected_tactic_id = selected_tactic_id or "balanced-01"
        self.lineup_ids = list(lineup_ids or [])
        self.lineup_roles = dict(lineup_roles or {})
        self.training_settings = dict(training_settings or {"intensity": "Normal"})
        self.offers = {k: dict(v) for k, v in (offers or {}).items()}   # offer_id -> {"agreement_id", "commitments": [...]}
        self.accepted: dict[str, dict[str, Any]] = {}
        self.screen = screen
        self.focused = focused
        self.scaling, self.geometry, self.language, self.skin = scaling, dict(geometry or {"x": 0, "y": 0, "width": 1920, "height": 1080}), language, skin
        self._capabilities = tuple(capabilities)
        self.stop_flag = StopFlag()
        self.faults: list[InjectedFault] = []
        self.readback_faults: dict[str, tuple[ValueStatus, str]] = {}
        self.inputs: list[dict[str, Any]] = []       # every input actually sent, in order
        self.calls = 0                               # perform() invocations, including refused ones

    # ----- fault injection -----
    def inject(self, kind: str, on_step: int = 1) -> None:
        if kind not in FAULT_KINDS:
            raise ValueError(f"unknown fault {kind!r}; known: {FAULT_KINDS}")
        self.faults.append(InjectedFault(kind, on_step))

    def inject_readback(self, kind: str, status: ValueStatus, reason: str) -> None:
        """Make ``readback(kind)`` report a non-available status (e.g. the readback screen is unreadable)."""
        self.readback_faults[kind] = (status, reason)

    def _fault_for(self, call_index: int) -> InjectedFault | None:
        for fault in self.faults:
            if not fault.fired and fault.on_step == call_index:
                fault.fired = True
                return fault
        return None

    # ----- protocol -----
    def capabilities(self) -> list[str]:
        return list(self._capabilities)

    def identify_screen(self) -> ScreenObservation:
        known = self.screen in FAKE_SCREEN_MODEL.screens
        return ScreenObservation(self.screen if known else None, 1.0 if known else 0.0, dict(self.geometry), self.scaling, self.language, self.skin, self.focused, utc_now(), self.method, None if known else f"screen {self.screen!r} is not in the model")

    def has_focus(self) -> bool:
        return self.focused

    def stop(self, reason: str = "operator stop") -> None:
        self.stop_flag.set(reason)

    def perform(self, step: UIStep) -> StepResult:
        self.calls += 1
        refusal = self._refuse(step)
        if refusal is not None:
            return refusal
        fault = self._fault_for(self.calls)
        if fault and fault.kind == "focus_loss":
            self.focused = False
            return StepResult(False, STEP_NO_FOCUS, self.identify_screen(), {"fault": fault.kind}, "focus lost before input; nothing sent")
        if fault and fault.kind == "crash":
            raise AdapterCrash(f"fake UI crashed during step {step.step_id}")
        if fault and fault.kind == "timeout_before_effect":
            return StepResult(False, STEP_TIMEOUT, self.identify_screen(), {"fault": fault.kind}, f"step {step.step_id} timed out after {step.timeout_ms} ms")
        self.inputs.append({"step_id": step.step_id, "action": step.action, "params": dict(step.params), "screen_before": self.screen})
        error = self._apply(step)
        if error:
            return StepResult(False, STEP_ERROR, self.identify_screen(), {}, error)
        if fault and fault.kind == "timeout_after_success":
            return StepResult(False, STEP_TIMEOUT, self.identify_screen(), {"fault": fault.kind}, f"no confirmation of step {step.step_id} within {step.timeout_ms} ms")
        if fault and fault.kind == "unexpected_screen":
            self.screen = "unexpected"
            return StepResult(False, STEP_UNEXPECTED_SCREEN, self.identify_screen(), {"fault": fault.kind}, "screen changed unexpectedly after input")
        return StepResult(True, STEP_DONE, self.identify_screen(), {"input_index": len(self.inputs)})

    def _refuse(self, step: UIStep) -> StepResult | None:
        """Refusals that never send input: Stop, no focus, unidentified screen, illegal action."""
        if self.stop_flag.is_set():
            return StepResult(False, STEP_STOPPED, self.identify_screen(), {}, f"stop flag set: {self.stop_flag.reason}")
        if not self.focused:
            return StepResult(False, STEP_NO_FOCUS, self.identify_screen(), {}, "game window does not have focus; nothing sent")
        observation = self.identify_screen()
        if not observation.identified:
            return StepResult(False, STEP_UNKNOWN_SCREEN, observation, {}, observation.reason)
        if step.screen != ANY_SCREEN and step.screen != observation.screen_id:
            return StepResult(False, STEP_UNEXPECTED_SCREEN, observation, {}, f"step expects screen {step.screen!r} but {observation.screen_id!r} is shown")
        if not FAKE_SCREEN_MODEL.legal(observation.screen_id, step.action):
            return StepResult(False, STEP_ILLEGAL, observation, {}, f"action {step.action!r} is not legal on screen {observation.screen_id!r}")
        return None

    def _apply(self, step: UIStep) -> str | None:
        """Apply the effect of a sent input. Returns an error message when the UI rejects it."""
        params = step.params
        if step.action == "navigate":
            self.screen = params["target"]
            return None
        if step.action == "select_tactic":
            tactic_id = params.get("tactic_catalog_id")
            if tactic_id not in self.tactic_catalog:
                return f"tactic {tactic_id!r} is not in the catalog"
            self.selected_tactic_id = tactic_id
            return None
        if step.action == "set_lineup":
            self.lineup_ids = list(params.get("player_ids") or [])
            self.lineup_roles = dict(params.get("roles") or {})
            return None
        if step.action == "set_training":
            self.training_settings = dict(params.get("settings") or {})
            return None
        if step.action == "open_message":
            if params.get("offer_id") not in self.offers:
                return f"offer {params.get('offer_id')!r} is not in the inbox"
            self.screen = "inbox.offer"
            return None
        if step.action == "accept_offer":
            offer_id = params.get("offer_id")
            if offer_id not in self.offers:
                return f"offer {offer_id!r} is not in the inbox"
            if offer_id in self.accepted:
                return f"offer {offer_id!r} was already accepted"
            self.accepted[offer_id] = {"agreement_id": self.offers[offer_id]["agreement_id"], "offer_id": offer_id, "commitments": list(self.offers[offer_id].get("commitments", []))}
            self.screen = "inbox"
            return None
        if step.action == "reject_offer":
            self.screen = "inbox"
            return None
        return f"unsupported action {step.action!r}"

    def readback(self, kind: str, params: dict[str, Any] | None = None) -> Observed:
        params = params or {}
        if kind in self.readback_faults:
            status, reason = self.readback_faults[kind]
            return Observed.unavailable(status, kind, reason, source=f"ui:{self.name}")
        source = f"ui:{self.name}:readback"
        if kind == "selected_tactic":
            return Observed.available_value({"tactic_id": self.selected_tactic_id, "name": self.tactic_catalog.get(self.selected_tactic_id)}, source, utc_now(), what=kind)
        if kind == "lineup":
            return Observed.available_value({"player_ids": list(self.lineup_ids), "roles": dict(self.lineup_roles)}, source, utc_now(), what=kind)
        if kind == "training_settings":
            return Observed.available_value(dict(self.training_settings), source, utc_now(), what=kind)
        if kind == "agreement":
            offer_id = params.get("offer_id")
            if offer_id not in self.offers:
                return Observed.unavailable(ValueStatus.MISSING, kind, f"offer {offer_id!r} is not visible in the inbox", source=source)
            if offer_id not in self.accepted:
                return Observed.unavailable(ValueStatus.NULL, kind, f"offer {offer_id!r} has no agreement", source=source)
            return Observed.available_value(dict(self.accepted[offer_id]), source, utc_now(), what=kind)
        if kind == "screen":
            return Observed.available_value(self.identify_screen().to_json(), source, utc_now(), what=kind)
        return Observed.unavailable(ValueStatus.UNSUPPORTED, kind, f"fake adapter has no readback for {kind!r}", source=source)


# ---------------------------------------------------------------------------
# Windows: fail-closed stub
# ---------------------------------------------------------------------------

WINDOWS_ADAPTER_STATUS_UNVALIDATED = "windows adapter is a fail-closed stub: FM accessibility support is untested and must be validated per workflow"


class WindowsAdapter:
    """Fail-closed placeholder for the real Windows adapter.

    FM's accessibility tree has not been validated, and no screen-recognition
    workflow has been tested. Until then this adapter provides no
    capabilities, identifies no screen, sends no input and answers every
    readback with ``unsupported``. Importing it never raises on any platform;
    an optional accessibility library is only looked up lazily and its absence
    is reported as a status, never as an exception.
    """

    name = "windows"
    method = "accessibility"

    def __init__(self, library_name: str = "uiautomation"):
        self.library_name = library_name
        self.stop_flag = StopFlag()
        self.status_reason = self._probe()

    def _probe(self) -> str:
        if platform.system() != "Windows":
            return f"not running on Windows ({platform.system()}); {WINDOWS_ADAPTER_STATUS_UNVALIDATED}"
        try:
            __import__(self.library_name)
        except Exception as exc:  # noqa: BLE001 - any import problem means fail closed
            return f"accessibility library {self.library_name!r} unavailable ({exc.__class__.__name__}); {WINDOWS_ADAPTER_STATUS_UNVALIDATED}"
        return WINDOWS_ADAPTER_STATUS_UNVALIDATED

    def capabilities(self) -> list[str]:
        return []

    def identify_screen(self) -> ScreenObservation:
        return ScreenObservation(None, 0.0, {}, None, None, None, False, utc_now(), self.method, self.status_reason)

    def has_focus(self) -> bool:
        return False

    def perform(self, step: UIStep) -> StepResult:
        return StepResult(False, STEP_UNSUPPORTED, self.identify_screen(), {}, self.status_reason)

    def readback(self, kind: str, params: dict[str, Any] | None = None) -> Observed:
        return Observed.unavailable(ValueStatus.UNSUPPORTED, kind, self.status_reason, source=f"ui:{self.name}")

    def stop(self, reason: str = "operator stop") -> None:
        self.stop_flag.set(reason)
