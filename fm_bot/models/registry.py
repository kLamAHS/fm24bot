"""Immutable model registry with release gates and baseline fallbacks (spec 13.5, 14 MOD 01).

A :class:`~fm_bot.state.records.ModelVersion` is registered once; its
artifact hash never changes. Only the release state moves, and only
through :meth:`ModelRegistry.promote`, which applies three gates:

1. calibration passed on held-out data (a :class:`CalibrationReport`);
2. the feature schema is allowed by the current visibility mask for the
   information mode the model will serve;
3. the model's build scope includes the current game build.

A version that fails gate 1 but clears 2 and 3 may be released as
``EXPERIMENTAL_ADVISORY`` when the caller asks, with its scope explicit;
it never becomes the default. :meth:`ModelRegistry.resolve` returns the
released version for a request or an explicit fallback to a named
baseline with the reason, so planners always know which model spoke.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..state.records import ModelVersion, ReleaseState
from ..state.store import Store
from ..state.visibility import FeatureLineageError, InformationMode, VisibilityMask, assert_features_allowed, mode_of_model
from .calibration import CalibrationReport

REGISTRY_VERSION = "registry-1.0"


class ModelRegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseContext:
    """The environment a model must be shown to support before release or use."""

    build: str
    information_mode: InformationMode
    mask: VisibilityMask | None = None
    available_features: frozenset[str] | None = None   # None means "not checked"


@dataclass
class GateResult:
    model_id: str
    version: str
    calibration_passed: bool
    schema_supported: bool
    build_supported: bool
    release_state: ReleaseState
    reasons: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"model_id": self.model_id, "version": self.version, "calibration_passed": self.calibration_passed, "schema_supported": self.schema_supported, "build_supported": self.build_supported, "release_state": self.release_state.value, "reasons": list(self.reasons)}


@dataclass(frozen=True)
class Resolution:
    """Which model answers a request. ``fallback`` is true when a baseline stands in."""

    model_id: str
    version: str | None
    baseline_id: str | None
    fallback: bool
    reason: str
    advisory: bool = False

    @property
    def resolved_id(self) -> str:
        return self.baseline_id if self.fallback else f"{self.model_id}:{self.version}"


def feature_names(model: ModelVersion) -> list[str]:
    """Feature schema entries are ``entity.field`` names, either a list under ``features`` or the dict keys."""
    schema = model.feature_schema
    if isinstance(schema.get("features"), list):
        return [str(n) for n in schema["features"]]
    return [str(k) for k in schema if k != "features"]


def schema_supported(model: ModelVersion, context: ReleaseContext) -> tuple[bool, str | None]:
    names = feature_names(model)
    if not names:
        return False, "feature schema declares no features"
    if not mode_of_model(model.information_mode, context.information_mode):
        return False, f"model trained in {model.information_mode} cannot serve {context.information_mode.value} requests"
    try:
        assert_features_allowed({n: None for n in names}, context.information_mode, context.mask)
    except FeatureLineageError as exc:
        return False, str(exc)
    if context.available_features is not None:
        absent = sorted(set(names) - context.available_features)
        if absent:
            return False, f"features not available in this context: {absent}"
    return True, None


class ModelRegistry:
    """Store-backed registry. Journals every gate decision under ``model.gate``."""

    def __init__(self, store: Store):
        self.store = store
        self._default_baselines: dict[str, str] = {}

    # ----- registration -----
    def register(self, model: ModelVersion) -> ModelVersion:
        """Register a version. Re-registering the identical artifact is a no-op; a different artifact under the same version raises."""
        existing = self.get(model.model_id, model.version)
        if existing is not None:
            if existing.artifact_hash != model.artifact_hash:
                raise ModelRegistryError(f"{model.model_id}:{model.version} already registered with artifact {existing.artifact_hash}; versions are immutable")
            return existing
        self.store.insert_model_version(model)
        return model

    def get(self, model_id: str, version: str) -> ModelVersion | None:
        for item in self.store.list_model_versions(model_id):
            if item.version == version:
                return item
        return None

    def versions(self, model_id: str) -> list[ModelVersion]:
        return self.store.list_model_versions(model_id)

    def set_default_baseline(self, model_id: str, baseline_id: str) -> None:
        """Name the transparent baseline that stands in when no version of ``model_id`` resolves."""
        self._default_baselines[model_id] = baseline_id
        self.store.put_setting(f"model.baseline.{model_id}", {"baseline_id": baseline_id})

    def default_baseline(self, model_id: str) -> str | None:
        if model_id in self._default_baselines:
            return self._default_baselines[model_id]
        setting = self.store.get_setting(f"model.baseline.{model_id}")
        return setting[0]["baseline_id"] if setting else None

    # ----- release gates -----
    def promote(self, model_id: str, version: str, gate_report: CalibrationReport, context: ReleaseContext, *, allow_advisory: bool = False) -> GateResult:
        """Apply the release gates. Returns the resulting state; RELEASED only when all gates pass."""
        model = self.get(model_id, version)
        if model is None:
            raise ModelRegistryError(f"{model_id}:{version} is not registered")
        if model.release_state in (ReleaseState.REJECTED, ReleaseState.RETIRED):
            raise ModelRegistryError(f"{model_id}:{version} is {model.release_state.value} and cannot be promoted")
        if (gate_report.model_id, gate_report.version) != (model_id, version):
            raise ModelRegistryError("calibration report belongs to a different model version")
        reasons: list[str] = []
        calibration_ok = gate_report.passed
        if not calibration_ok:
            reasons.append(f"calibration {gate_report.status}: {'; '.join(gate_report.failures) or 'no detail'}")
        schema_ok, schema_reason = schema_supported(model, context)
        if not schema_ok:
            reasons.append(f"feature schema unsupported: {schema_reason}")
        build_ok = context.build in model.build_scope
        if not build_ok:
            reasons.append(f"build {context.build!r} not in scope {model.build_scope}")
        if calibration_ok and schema_ok and build_ok:
            state = ReleaseState.RELEASED
        elif schema_ok and build_ok and allow_advisory:
            state = ReleaseState.EXPERIMENTAL_ADVISORY
            reasons.append("released as experimental advisory only; not the autonomous default")
        else:
            state = ReleaseState.CANDIDATE
        model.release_state = state
        model.calibration = gate_report.to_json()
        self.store.update_model_version(model)
        result = GateResult(model_id, version, calibration_ok, schema_ok, build_ok, state, reasons)
        self.store.journal("model.gate", result.to_json(), f"{model_id}:{version}")
        return result

    def retire(self, model_id: str, version: str, reason: str) -> None:
        model = self.get(model_id, version)
        if model is None:
            raise ModelRegistryError(f"{model_id}:{version} is not registered")
        model.release_state = ReleaseState.RETIRED
        model.notes = f"{model.notes + '; ' if model.notes else ''}retired: {reason}"
        self.store.update_model_version(model)

    # ----- resolution -----
    def resolve(self, model_id: str, context: ReleaseContext, *, allow_advisory: bool = False) -> Resolution:
        """Pick the released version usable in ``context`` or fall back to the named baseline with the reason."""
        candidates = [m for m in self.versions(model_id) if m.release_state is ReleaseState.RELEASED or (allow_advisory and m.release_state is ReleaseState.EXPERIMENTAL_ADVISORY)]
        reasons: list[str] = []
        for model in reversed(candidates):           # newest registered first
            ok, reason = self._usable(model, context)
            if ok:
                return Resolution(model_id, model.version, model.baseline_id, False, "released version supports this context", model.release_state is ReleaseState.EXPERIMENTAL_ADVISORY)
            reasons.append(f"{model.version}: {reason}")
        baseline = self._baseline_for(model_id, candidates)
        detail = "; ".join(reasons) if reasons else "no released version"
        return Resolution(model_id, None, baseline, True, f"fallback to baseline {baseline!r}: {detail}")

    def _usable(self, model: ModelVersion, context: ReleaseContext) -> tuple[bool, str | None]:
        if context.build not in model.build_scope:
            return False, f"build {context.build!r} not in scope"
        return schema_supported(model, context)

    def _baseline_for(self, model_id: str, candidates: Iterable[ModelVersion]) -> str | None:
        for model in candidates:
            if model.baseline_id:
                return model.baseline_id
        for model in self.versions(model_id):
            if model.baseline_id:
                return model.baseline_id
        return self.default_baseline(model_id)
