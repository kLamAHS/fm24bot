"""Operator controls: persisted, versioned settings (spec 15.1, 15.3).

The operator configures the club's information mode, the authority the bot
holds over the game, the club's objective profile, spending limits, the cash
reserve, development emphasis, the permitted action families, optional staff
delegation, language-model usage limits and polling intervals. Every setting
is stored through :meth:`fm_bot.state.store.Store.put_setting`, which bumps
its version and journals the change, so a decision can name the exact
settings it was made under and a restart finds them unchanged.

Baseline only: the defaults here are policy starting points for a league
club, not fitted values. Poll intervals are engineering defaults, not
demonstrated safe sampling rates (spec 4.2).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..planning.objective import ClubObjectiveProfile, default_profile
from ..rules.authority import ACTION_FAMILIES, AuthorityLimits, AuthorityMode, AuthorityProfile
from ..state.records import Decision
from ..state.units import Money, Period, UnitError
from ..state.visibility import InformationMode

CONTROLS_VERSION = "interface.controls/1"

# Setting keys (one store row each, individually versioned).
KEY_INFORMATION_MODE = "settings:information_mode"
KEY_AUTHORITY_MODE = "settings:authority_mode"
KEY_ACTION_FAMILIES = "settings:permitted_action_families"
KEY_SPENDING_LIMITS = "settings:spending_limits"
KEY_CASH_RESERVE = "settings:cash_reserve"
KEY_DEVELOPMENT_EMPHASIS = "settings:development_emphasis"
KEY_CLUB_OBJECTIVE = "settings:club_objective"
KEY_DELEGATION = "settings:staff_delegation"
KEY_LM_LIMITS = "settings:language_model_limits"
KEY_POLL_INTERVALS = "settings:poll_intervals"
# The composed authority profile is re-stored whenever one of its inputs changes,
# so its version is the single number decisions and executors cite.
KEY_AUTHORITY_PROFILE = "settings:authority_profile"

# Short names the CLI and operator use -> store keys.
SETTING_NAMES: dict[str, str] = {
    "information_mode": KEY_INFORMATION_MODE,
    "authority_mode": KEY_AUTHORITY_MODE,
    "action_families": KEY_ACTION_FAMILIES,
    "spending_limits": KEY_SPENDING_LIMITS,
    "cash_reserve": KEY_CASH_RESERVE,
    "development_emphasis": KEY_DEVELOPMENT_EMPHASIS,
    "club_objective": KEY_CLUB_OBJECTIVE,
    "delegation": KEY_DELEGATION,
    "lm_limits": KEY_LM_LIMITS,
    "poll_intervals": KEY_POLL_INTERVALS,
}
AUTHORITY_INPUT_NAMES = ("authority_mode", "action_families", "spending_limits", "cash_reserve", "delegation")

# Engineering defaults (spec 4.2): 5 s idle, 2 s in a supported paused match viewer,
# exponential backoff on errors. Budgets to benchmark, not measured safe rates.
DEFAULT_POLL_INTERVALS: dict[str, float] = {
    "idle_seconds": 5.0,
    "paused_match_seconds": 2.0,
    "settle_interval_seconds": 1.0,
    "settle_reads": 2,
    "settle_max_reads": 10,
    "backoff_factor": 2.0,
    "max_backoff_seconds": 60.0,
}
DEFAULT_SPENDING_LIMITS: dict[str, Any] = {
    "max_weekly_wage_commitment": None,
    "max_total_fee_commitment": None,
    "max_total_conditional_commitment": None,
    "max_contract_years": None,
    "allow_player_release": False,
    "allow_player_sale": False,
    "allow_continue": False,
}
DEFAULT_LM_LIMITS: dict[str, Any] = {"max_calls": None, "max_input_tokens": None, "max_output_tokens": None, "max_cost": None}
MONEY_LIMIT_PERIODS: dict[str, Period] = {
    "max_weekly_wage_commitment": Period.WEEKLY,
    "max_total_fee_commitment": Period.ONCE,
    "max_total_conditional_commitment": Period.ONCE,
}
AUTHORITY_MODE_ALIASES: dict[str, str] = {"scoped": AuthorityMode.SCOPED_EXECUTION.value, "autonomy": AuthorityMode.CLUB_AUTONOMY.value}


class SettingsError(ValueError):
    """A setting value is not acceptable; nothing was changed."""


@dataclass
class Setting:
    """One stored value with the version the store assigned it (0 = default, never saved)."""

    name: str
    key: str
    value: Any
    version: int = 0

    @property
    def saved(self) -> bool:
        return self.version > 0

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "key": self.key, "value": self.value, "version": self.version}


def _defaults() -> dict[str, Any]:
    return {
        "information_mode": InformationMode.BRIDGE_OBSERVED.value,
        "authority_mode": AuthorityMode.ADVISE.value,
        "action_families": [],
        "spending_limits": dict(DEFAULT_SPENDING_LIMITS),
        "cash_reserve": None,
        "development_emphasis": 0.3,
        "club_objective": default_profile().to_json(),
        "delegation": {},
        "lm_limits": dict(DEFAULT_LM_LIMITS),
        "poll_intervals": dict(DEFAULT_POLL_INTERVALS),
    }


# ----- validators: each returns the normalised value or raises SettingsError -----

def _validate_information_mode(value: Any) -> str:
    try:
        return InformationMode(value).value
    except ValueError as exc:
        raise SettingsError(f"information mode must be one of {[m.value for m in InformationMode]}") from exc


def _validate_authority_mode(value: Any) -> str:
    value = AUTHORITY_MODE_ALIASES.get(value, value)
    try:
        return AuthorityMode(value).value
    except ValueError as exc:
        raise SettingsError(f"authority mode must be one of {[m.value for m in AuthorityMode]} (aliases: {sorted(AUTHORITY_MODE_ALIASES)})") from exc


def _validate_families(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",") if v.strip()]
    families = list(value or [])
    unknown = [f for f in families if f not in ACTION_FAMILIES]
    if unknown:
        raise SettingsError(f"unknown action families {unknown}; known: {ACTION_FAMILIES}")
    return sorted(set(families))


def _money_or_none(name: str, value: Any, period: Period) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        money = Money.from_json(value) if isinstance(value, dict) else Money.native_gbp(int(value), period)
    except (UnitError, TypeError, ValueError, KeyError) as exc:
        raise SettingsError(f"{name} must be Money JSON or whole pounds: {exc}") from exc
    if money.period is not period:
        raise SettingsError(f"{name} must carry a {period.value} period, got {money.period.value}")
    if money.is_negative:
        raise SettingsError(f"{name} cannot be negative")
    return money.to_json()


def _validate_spending_limits(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SettingsError("spending limits must be an object")
    unknown = set(value) - set(DEFAULT_SPENDING_LIMITS)
    if unknown:
        raise SettingsError(f"unknown spending limit fields {sorted(unknown)}")
    result = dict(DEFAULT_SPENDING_LIMITS)
    result.update(value)
    for name, period in MONEY_LIMIT_PERIODS.items():
        result[name] = _money_or_none(name, result[name], period)
    years = result["max_contract_years"]
    if years is not None and (isinstance(years, bool) or not isinstance(years, int) or years <= 0):
        raise SettingsError("max_contract_years must be a positive integer or null")
    for flag in ("allow_player_release", "allow_player_sale", "allow_continue"):
        if not isinstance(result[flag], bool):
            raise SettingsError(f"{flag} must be true or false")
    return result


def _validate_cash_reserve(value: Any) -> dict[str, Any] | None:
    return _money_or_none("cash_reserve", value, Period.ONCE)


def _validate_emphasis(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SettingsError("development emphasis must be a number in 0..1") from exc
    if not 0.0 <= number <= 1.0:
        raise SettingsError("development emphasis must be within 0..1")
    return number


def _validate_objective(value: Any) -> dict[str, Any]:
    try:
        return ClubObjectiveProfile.from_json(value).to_json()
    except (KeyError, TypeError, ValueError) as exc:
        raise SettingsError(f"club objective profile is invalid: {exc}") from exc


def _validate_delegation(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise SettingsError("delegation must map action family -> delegate (e.g. {'media': 'assistant'})")
    unknown = [f for f in value if f not in ACTION_FAMILIES]
    if unknown:
        raise SettingsError(f"delegation names unknown families {unknown}")
    return {k: str(v) for k, v in value.items()}


def _validate_lm_limits(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SettingsError("language model limits must be an object")
    unknown = set(value) - set(DEFAULT_LM_LIMITS)
    if unknown:
        raise SettingsError(f"unknown language model limit fields {sorted(unknown)}")
    result = dict(DEFAULT_LM_LIMITS)
    result.update(value)
    for name in ("max_calls", "max_input_tokens", "max_output_tokens"):
        item = result[name]
        if item is not None and (isinstance(item, bool) or not isinstance(item, int) or item < 0):
            raise SettingsError(f"{name} must be a non-negative integer or null")
    result["max_cost"] = _money_or_none("max_cost", result["max_cost"], Period.ONCE)
    return result


def _validate_poll_intervals(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        raise SettingsError("poll intervals must be an object")
    unknown = set(value) - set(DEFAULT_POLL_INTERVALS)
    if unknown:
        raise SettingsError(f"unknown poll interval fields {sorted(unknown)}")
    result = dict(DEFAULT_POLL_INTERVALS)
    result.update(value)
    for name, item in result.items():
        if isinstance(item, bool) or not isinstance(item, (int, float)) or item <= 0:
            raise SettingsError(f"{name} must be a positive number")
    if result["backoff_factor"] < 1.0:
        raise SettingsError("backoff_factor must be at least 1")
    result["settle_reads"] = int(result["settle_reads"])
    result["settle_max_reads"] = int(result["settle_max_reads"])
    return result


VALIDATORS: dict[str, Callable[[Any], Any]] = {
    "information_mode": _validate_information_mode,
    "authority_mode": _validate_authority_mode,
    "action_families": _validate_families,
    "spending_limits": _validate_spending_limits,
    "cash_reserve": _validate_cash_reserve,
    "development_emphasis": _validate_emphasis,
    "club_objective": _validate_objective,
    "delegation": _validate_delegation,
    "lm_limits": _validate_lm_limits,
    "poll_intervals": _validate_poll_intervals,
}


@dataclass
class Settings:
    """The operator's controls as loaded from (and saved to) the store.

    ``items`` maps the short setting name to its :class:`Setting`. Use
    :meth:`change` to alter a value: it validates, persists with a new
    version, re-stores the composed authority profile when one of its inputs
    changed, and journals who changed what and why.
    """

    store: Any
    items: dict[str, Setting] = field(default_factory=dict)
    authority_profile_version: int = 0

    # ----- persistence -----
    @classmethod
    def load(cls, store) -> "Settings":
        settings = cls(store)
        for name, default in _defaults().items():
            key = SETTING_NAMES[name]
            stored = store.get_setting(key)
            if stored is None:
                settings.items[name] = Setting(name, key, default, 0)
            else:
                body, version = stored
                settings.items[name] = Setting(name, key, body, version)
        profile = store.get_setting(KEY_AUTHORITY_PROFILE)
        settings.authority_profile_version = profile[1] if profile else 0
        return settings

    def save(self) -> dict[str, int]:
        """Persist every setting that has never been saved (defaults), so restarts see explicit values."""
        saved: dict[str, int] = {}
        with self.store.transaction():
            for name, item in self.items.items():
                if not item.saved:
                    item.version = self.store.put_setting(item.key, item.value)
                    saved[name] = item.version
            if self.authority_profile_version == 0 or saved.keys() & set(AUTHORITY_INPUT_NAMES):
                self._store_authority_profile()
        return saved

    def change(self, name: str, value: Any, *, reason: str = "", by: str = "operator") -> Setting:
        """Validate and persist one setting; the new version is journaled with the old value."""
        if name not in self.items:
            raise SettingsError(f"unknown setting {name!r}; known: {sorted(self.items)}")
        normalised = VALIDATORS[name](value)
        item = self.items[name]
        previous = item.value
        with self.store.transaction():
            item.version = self.store.put_setting(item.key, normalised)
            item.value = normalised
            if name in AUTHORITY_INPUT_NAMES:
                self._store_authority_profile()
            self.store.journal("settings.changed", {"name": name, "key": item.key, "version": item.version, "previous": previous, "value": normalised, "reason": reason, "by": by, "authority_profile_version": self.authority_profile_version, "controls_version": CONTROLS_VERSION}, item.key)
        return item

    def _store_authority_profile(self) -> None:
        stored = self.store.get_setting(KEY_AUTHORITY_PROFILE)
        expected = (stored[1] + 1) if stored else 1
        profile = self.authority_profile()
        profile.version = expected
        self.authority_profile_version = self.store.put_setting(KEY_AUTHORITY_PROFILE, profile.to_json())
        if self.authority_profile_version != expected:
            raise SettingsError(f"authority profile version drifted: stored {self.authority_profile_version}, expected {expected}")

    # ----- typed accessors -----
    def get(self, name: str) -> Any:
        return self.items[name].value

    def version(self, name: str) -> int:
        return self.items[name].version

    def information_mode(self) -> InformationMode:
        return InformationMode(self.get("information_mode"))

    def authority_mode(self) -> AuthorityMode:
        return AuthorityMode(self.get("authority_mode"))

    def authority_profile(self) -> AuthorityProfile:
        """The authority profile composed from mode, families, spending limits, reserve and delegation."""
        limits_json = dict(self.get("spending_limits"))
        limits_json["min_cash_reserve"] = self.get("cash_reserve")
        limits = AuthorityLimits.from_json(limits_json)
        return AuthorityProfile(self.authority_mode(), set(self.get("action_families")), limits, self.authority_profile_version, dict(self.get("delegation")), label="operator settings")

    def club_objective(self) -> ClubObjectiveProfile:
        return ClubObjectiveProfile.from_json(self.get("club_objective"))

    def cash_reserve(self) -> Money | None:
        value = self.get("cash_reserve")
        return Money.from_json(value) if value else None

    def poll_intervals(self) -> dict[str, float]:
        return dict(self.get("poll_intervals"))

    def lm_limits(self) -> dict[str, Any]:
        return dict(self.get("lm_limits"))

    # ----- provenance -----
    def stamp(self) -> dict[str, str]:
        """Setting versions as ``key -> version`` strings, for ``Decision.model_versions`` and explanations."""
        result = {item.key: str(item.version) for item in self.items.values()}
        result[KEY_AUTHORITY_PROFILE] = str(self.authority_profile_version)
        return result

    def stamp_decision(self, decision: Decision) -> Decision:
        """Record the setting versions this decision was made under (spec 15.1)."""
        decision.model_versions = {**decision.model_versions, **self.stamp()}
        return decision

    def to_json(self) -> dict[str, Any]:
        return {"settings": {name: item.to_json() for name, item in self.items.items()}, "authority_profile_version": self.authority_profile_version, "controls_version": CONTROLS_VERSION}


def parse_setting_value(name: str, text: str) -> Any:
    """Turn a CLI string into a value for ``Settings.change``: JSON where it parses, else the raw text."""
    import json
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
