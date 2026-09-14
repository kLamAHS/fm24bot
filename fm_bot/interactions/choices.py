"""Ranking the answers the game offers (design specification 11.3, 4.3, AUD 01).

When a player knocks on the door, the press ask a question, or a message
demands a reply, the game shows a fixed set of answers. The bot only ever
picks one of those *legal option ids*. It never types a free-text reply when
the game offers buttons, and when it does not know what an option means it
says so rather than guessing.

Ranking is two-layered:

* Baseline: a versioned club-policy score. Each option is tagged from its
  visible label and consequences text with a keyword table (a HEURISTIC),
  and the club policy assigns weights to tags. Open promises that an option
  would break subtract a penalty. Options the policy forbids remain legal
  but are never chosen automatically.
* Optional: a language model (through :mod:`fm_bot.interactions.language_model`)
  ranks the same legal ids from mode-filtered evidence. An accepted,
  validated answer earns a bonus; a rejected answer changes nothing.

Numeric limits are enforced *outside* both layers: :func:`choice_intent`
builds an :class:`ActionIntent` for an option that carries money or contract
terms and :func:`authorize_options` runs :func:`fm_bot.rules.authority.authorize`
over it. An option outside the authority profile is reported with its exact
terms and is not chosen.

Every :class:`DialogueDecision` records the chosen id, the legal ids, the
cited evidence, the policy version, the information mode and the language
model request id, so an executed answer resolves back to its inputs (AUD 01).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..rules.authority import Allowed, AuthorityProfile, OutsideScope, authorize
from ..state.identity import new_id, utc_now
from ..state.records import ActionIntent, Decision, Promise, payload_hash
from ..state.status import MissingCapabilityReport
from ..state.units import Money
from ..state.visibility import InformationMode, VisibilityMask
from .inbox import DialogueOption
from .language_model import CHOICE_SCHEMA, Evidence, LanguageModel, LMRequest, ValidatedChoice, ask, build_request
from .promises import OUTGOING_CONFLICT_KINDS, PromiseTerms, parse_terms

CHOICE_POLICY_VERSION = "choice-policy/1.0"
OPTION_TAG_PATTERNS_VERSION = "option-tag-patterns/1.0"

# Keyword table that turns an option's visible words into policy tags.
# HEURISTIC: matching is substring, case-insensitive, over label and
# consequences text. Review when new dialogue screens are observed.
OPTION_TAG_PATTERNS: dict[str, tuple[str, ...]] = {
    "makes_promise": ("promise", "guarantee", "assure", "make sure", "i will ensure"),
    "accept": ("accept", "agree", "approve"),
    "reject": ("reject", "decline", "refuse", "turn down", "not interested"),
    "negotiate": ("negotiate", "counter", "discuss terms", "talk terms"),
    "delegate": ("let my assistant", "assistant handle", "delegate", "leave it to"),
    "back_player_publicly": ("back him", "back the player", "support", "confidence in", "praise", "defend"),
    "criticise_publicly": ("criticis", "criticiz", "blame", "disappoint", "fine him", "warn", "unacceptable"),
    "sell_player": ("sell", "transfer list", "let him leave", "allow to leave", "release", "listen to offers"),
    "commit_money": ("spend", "invest", "raise the budget", "pay", "increase wage"),
    "no_comment": ("no comment", "won't discuss", "will not discuss", "decline to comment"),
}

# Scores are integers. Bonuses and penalties are module-level and versioned
# with CHOICE_POLICY_VERSION so a decision can be replayed.
LM_PREFERENCE_BONUS = 3          # an accepted, validated language-model choice
PROMISE_CONFLICT_PENALTY = 5     # per open promise the option would break
UNTAGGED_OPTION_SCORE = 0        # an option matching no tag: neutral, not favoured

AUTHORITY_ALLOWED = "allowed"
AUTHORITY_OUTSIDE_SCOPE = "outside_scope"
AUTHORITY_NOT_CHECKED = "not_checked"

STATUS_CHOSEN = "chosen"
STATUS_NO_OPTIONS = "no_options"
STATUS_UNAVAILABLE = "unavailable"
STATUS_OUTSIDE_SCOPE = "outside_scope"


@dataclass
class ClubPolicy:
    """Club stance on dialogue, in tag weights. Positive favours, negative disfavours.

    ``forbidden_tags`` mark options that may never be chosen automatically
    (for example anything that makes a new promise). Version the policy
    whenever weights change; the version is stamped on every decision.
    """

    version: str = CHOICE_POLICY_VERSION
    weights: dict[str, int] = field(default_factory=dict)
    forbidden_tags: frozenset[str] = frozenset()
    label: str = "default"

    def to_json(self) -> dict[str, Any]:
        return {"version": self.version, "weights": dict(self.weights), "forbidden_tags": sorted(self.forbidden_tags), "label": self.label}


DEFAULT_CLUB_POLICY = ClubPolicy(
    CHOICE_POLICY_VERSION,
    {"makes_promise": -3, "delegate": -1, "back_player_publicly": 2, "criticise_publicly": -2, "sell_player": -1, "commit_money": -1, "no_comment": 1, "negotiate": 1},
    frozenset({"makes_promise"}),
    "default-conservative",
)


def tag_option(option: DialogueOption) -> list[str]:
    """Policy tags an option's visible words match (HEURISTIC keyword table)."""
    words = f"{option.label} {option.consequences_text or ''}".lower()
    return [tag for tag, keywords in OPTION_TAG_PATTERNS.items() if any(k in words for k in keywords)]


def option_promise_conflicts(option: DialogueOption, promises: Iterable[Promise], terms_lookup: Callable[[Promise], PromiseTerms] | None = None) -> list[str]:
    """Open promises the option would break: outgoing-player options aimed at a promised player."""
    tags = tag_option(option)
    if "sell_player" not in tags or not option.target_ids:
        return []
    lookup = terms_lookup or (lambda p: parse_terms(p.commitment))
    reasons = []
    for promise in promises:
        if promise.status != "open" or promise.party_id not in option.target_ids:
            continue
        if lookup(promise).kind in OUTGOING_CONFLICT_KINDS:
            reasons.append(f"breaks promise {promise.promise_id} to {promise.party_id}: {promise.commitment!r}")
    return reasons


@dataclass
class RankedChoice:
    option_id: str
    label: str
    score: int
    rank: int
    tags: list[str]
    reasons: list[str]
    policy_conflicts: list[str] = field(default_factory=list)
    promise_conflicts: list[str] = field(default_factory=list)
    authority: str = AUTHORITY_NOT_CHECKED
    exact_terms: dict[str, Any] = field(default_factory=dict)
    lm_selected: bool = False

    @property
    def eligible(self) -> bool:
        """May be chosen automatically: legal, not policy-forbidden, not outside authority."""
        return not self.policy_conflicts and self.authority != AUTHORITY_OUTSIDE_SCOPE

    def to_json(self) -> dict[str, Any]:
        return {
            "option_id": self.option_id, "label": self.label, "score": self.score, "rank": self.rank, "tags": list(self.tags), "reasons": list(self.reasons),
            "policy_conflicts": list(self.policy_conflicts), "promise_conflicts": list(self.promise_conflicts), "authority": self.authority,
            "exact_terms": dict(self.exact_terms), "lm_selected": self.lm_selected, "eligible": self.eligible,
        }


@dataclass
class ChoiceRanking:
    ranked: list[RankedChoice]
    policy_version: str
    evidence_ids: list[str]
    lm_status: str                              # unavailable | accepted | rejected | not_asked
    lm_reason: str | None = None
    lm_choice: ValidatedChoice | None = None
    request: LMRequest | None = None
    excluded: list[tuple[str, str]] = field(default_factory=list)   # (option_id, reason) e.g. free text
    tag_patterns_version: str = OPTION_TAG_PATTERNS_VERSION

    def best(self) -> RankedChoice | None:
        for item in self.ranked:
            if item.eligible:
                return item
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "ranked": [r.to_json() for r in self.ranked], "policy_version": self.policy_version, "evidence_ids": list(self.evidence_ids), "lm_status": self.lm_status,
            "lm_reason": self.lm_reason, "lm_request_id": self.request.request_id if self.request else None, "excluded": [list(e) for e in self.excluded], "tag_patterns_version": self.tag_patterns_version,
        }


def _score_option(option: DialogueOption, policy: ClubPolicy, promises: Iterable[Promise], terms_lookup) -> RankedChoice:
    tags = tag_option(option)
    reasons: list[str] = []
    score = UNTAGGED_OPTION_SCORE
    for tag in tags:
        weight = policy.weights.get(tag, 0)
        score += weight
        reasons.append(f"{tag}: {weight:+d} (policy {policy.version})")
    if not tags:
        reasons.append("no policy tag matched the visible words")
    policy_conflicts = [f"policy forbids {tag}" for tag in tags if tag in policy.forbidden_tags]
    promise_conflicts = option_promise_conflicts(option, promises, terms_lookup)
    score -= PROMISE_CONFLICT_PENALTY * len(promise_conflicts)
    reasons.extend(f"-{PROMISE_CONFLICT_PENALTY}: {c}" for c in promise_conflicts)
    return RankedChoice(option.option_id, option.label, score, 0, tags, reasons, policy_conflicts, promise_conflicts)


def _apply_authority(item: RankedChoice, verdict: Allowed | OutsideScope | None) -> None:
    if verdict is None:
        return
    if verdict.allowed:
        item.authority = AUTHORITY_ALLOWED
        return
    item.authority = AUTHORITY_OUTSIDE_SCOPE
    item.exact_terms = dict(verdict.exact_terms)
    item.reasons.extend(f"outside authority: {r}" for r in verdict.reasons)


def _consult_lm(lm: LanguageModel | None, evidence: list[Evidence], legal: list[str], mode: InformationMode, mask: VisibilityMask | None, task: str) -> tuple[str, str | None, ValidatedChoice | None, LMRequest | None]:
    if lm is None or not legal:
        return "not_asked", None, None, None
    request = build_request(evidence, legal, CHOICE_SCHEMA, mode, mask, task=task)
    answer = ask(lm, request)
    if answer.available:
        return "accepted", None, answer.require(), request
    status = "rejected" if answer.reason and answer.reason.startswith("rejected") else "unavailable"
    return status, answer.reason, None, request


def rank_options(options: Iterable[DialogueOption], policy: ClubPolicy, evidence: Iterable[Evidence], promises: Iterable[Promise], lm: LanguageModel | None = None, *, information_mode: InformationMode | str = InformationMode.BRIDGE_OBSERVED, mask: VisibilityMask | None = None, authority: dict[str, Allowed | OutsideScope] | None = None, terms_lookup: Callable[[Promise], PromiseTerms] | None = None, task: str = "Choose the answer that best serves the club's policy and existing commitments.") -> ChoiceRanking:
    """Rank the game's fixed options. Free-text options are excluded; the bot never composes prose."""
    mode = InformationMode(information_mode)
    evidence = list(evidence)
    promises = list(promises)
    fixed: list[DialogueOption] = []
    excluded: list[tuple[str, str]] = []
    for option in options:
        if option.kind == "fixed":
            fixed.append(option)
        else:
            excluded.append((option.option_id, f"option kind {option.kind!r}: the bot does not invent free text"))
    ranked = [_score_option(option, policy, promises, terms_lookup) for option in fixed]
    for item in ranked:
        _apply_authority(item, (authority or {}).get(item.option_id))
    legal = [item.option_id for item in ranked]
    lm_status, lm_reason, lm_choice, request = _consult_lm(lm, evidence, legal, mode, mask, task)
    if lm_choice is not None:
        for item in ranked:
            if item.option_id == lm_choice.option_id:
                item.lm_selected = True
                item.score += LM_PREFERENCE_BONUS
                item.reasons.append(f"+{LM_PREFERENCE_BONUS}: language model preferred it citing {list(lm_choice.cited_observation_ids)}")
    ranked.sort(key=lambda item: (-item.score, item.option_id))
    for index, item in enumerate(ranked, start=1):
        item.rank = index
    return ChoiceRanking(ranked, policy.version, [e.observation_id for e in evidence], lm_status, lm_reason, lm_choice, request, excluded)


# ---------------------------------------------------------------------------
# Authority on options that carry money
# ---------------------------------------------------------------------------


def choice_intent(option: DialogueOption, *, kind: str, authority_scope: str, career_id: str, branch_id: str, snapshot_id: str, context_id: str, terms: dict[str, Any] | None = None, required_capabilities: Iterable[str] = ("ui_action_adapter", "inbox_text"), entity_versions: dict[str, str] | None = None) -> ActionIntent:
    """An :class:`ActionIntent` for picking ``option`` in dialogue ``context_id``.

    ``terms`` may carry ``weekly_wage``, ``total_fee``, ``conditional_total``
    (:class:`Money`) and ``contract_years``; :func:`authorize` compares them
    with the profile limits. The idempotency key ties the intent to the
    branch, dialogue and option so the same answer cannot be sent twice.
    """
    params: dict[str, Any] = {"option_id": option.option_id, "label": option.label, "context_id": context_id}
    for name, value in (terms or {}).items():
        params[name] = value.to_json() if isinstance(value, Money) else value
    key = payload_hash({"branch": branch_id, "context": context_id, "option": option.option_id, "terms": {k: v for k, v in params.items() if k not in ("label",)}})
    return ActionIntent(new_id("act"), kind, authority_scope, career_id, branch_id, snapshot_id, {"context_id": context_id, "option_id": option.option_id, "target_ids": list(option.target_ids)}, params, ["dialogue still open", "option ids unchanged"], list(required_capabilities), "relevant_state_change", "dialogue_closed_and_inbox_reread", key, entity_versions=dict(entity_versions or {}), risk_class="consequential")


def authorize_options(options: Iterable[DialogueOption], profile: AuthorityProfile, *, kind: str, authority_scope: str, career_id: str, branch_id: str, snapshot_id: str, context_id: str, terms_by_option: dict[str, dict[str, Any]] | None = None, capability_report: MissingCapabilityReport | None = None) -> dict[str, Allowed | OutsideScope]:
    """Run every option through the authority profile; the verdicts feed :func:`rank_options`."""
    verdicts: dict[str, Allowed | OutsideScope] = {}
    for option in options:
        intent = choice_intent(option, kind=kind, authority_scope=authority_scope, career_id=career_id, branch_id=branch_id, snapshot_id=snapshot_id, context_id=context_id, terms=(terms_by_option or {}).get(option.option_id))
        verdicts[option.option_id] = authorize(intent, profile, capability_report)
    return verdicts


# ---------------------------------------------------------------------------
# Typed decision records
# ---------------------------------------------------------------------------


@dataclass
class DialogueDecision:
    """A conversation, media or inbox decision, with everything needed to audit it."""

    decision_id: str
    kind: str                            # conversation | media | inbox | contract_dialogue
    context_id: str                      # e.g. "inbox:501" or "press:2024-02-17"
    status: str                          # chosen | no_options | unavailable | outside_scope
    chosen_option_id: str | None
    legal_option_ids: list[str]
    cited_observation_ids: list[str]
    policy_version: str
    information_mode: str
    snapshot_id: str | None
    reasons: list[str]
    ranking: dict[str, Any] = field(default_factory=dict)
    lm_request_id: str | None = None
    lm_status: str = "not_asked"
    exact_terms: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    @property
    def available(self) -> bool:
        return self.status == STATUS_CHOSEN and self.chosen_option_id is not None

    def to_json(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id, "kind": self.kind, "context_id": self.context_id, "status": self.status, "chosen_option_id": self.chosen_option_id,
            "legal_option_ids": list(self.legal_option_ids), "cited_observation_ids": list(self.cited_observation_ids), "policy_version": self.policy_version,
            "information_mode": self.information_mode, "snapshot_id": self.snapshot_id, "reasons": list(self.reasons), "ranking": dict(self.ranking),
            "lm_request_id": self.lm_request_id, "lm_status": self.lm_status, "exact_terms": dict(self.exact_terms), "created_at": self.created_at,
        }

    def to_decision(self) -> Decision:
        """The generic :class:`Decision` record for the store (candidates are the ranked options)."""
        candidates = list(self.ranking.get("ranked", []))
        selected = next((c for c in candidates if c.get("option_id") == self.chosen_option_id), None)
        return Decision(self.decision_id, self.policy_version, self.snapshot_id or "", candidates, [{"legal_option_ids": list(self.legal_option_ids)}], [], selected, list(self.reasons), {"status": self.status, "cited_observation_ids": list(self.cited_observation_ids), "lm_request_id": self.lm_request_id}, self.information_mode, {}, self.created_at, f"dialogue.{self.kind}")


def decide(options: Iterable[DialogueOption], policy: ClubPolicy, evidence: Iterable[Evidence], promises: Iterable[Promise], lm: LanguageModel | None = None, *, kind: str, context_id: str, snapshot_id: str | None = None, information_mode: InformationMode | str = InformationMode.BRIDGE_OBSERVED, mask: VisibilityMask | None = None, authority: dict[str, Allowed | OutsideScope] | None = None, terms_lookup: Callable[[Promise], PromiseTerms] | None = None) -> DialogueDecision:
    """Rank and pick, or explain why no pick is available. Never invents an option."""
    options = list(options)
    evidence = list(evidence)
    mode = InformationMode(information_mode)
    ranking = rank_options(options, policy, evidence, promises, lm, information_mode=mode, mask=mask, authority=authority, terms_lookup=terms_lookup)
    ranked_ids = {item.option_id for item in ranking.ranked}
    legal = [option.option_id for option in options if option.option_id in ranked_ids]  # the game's offered order, not the ranking order
    base = dict(kind=kind, context_id=context_id, legal_option_ids=legal, policy_version=policy.version, information_mode=mode.value, snapshot_id=snapshot_id, ranking=ranking.to_json(), lm_request_id=ranking.request.request_id if ranking.request else None, lm_status=ranking.lm_status)
    if not legal:
        reasons = ["the game offered no fixed options"] + [f"excluded {oid}: {why}" for oid, why in ranking.excluded]
        return DialogueDecision(new_id("dec"), status=STATUS_NO_OPTIONS, chosen_option_id=None, cited_observation_ids=[], reasons=reasons, **base)
    best = ranking.best()
    if best is None:
        outside = [item for item in ranking.ranked if item.authority == AUTHORITY_OUTSIDE_SCOPE]
        status = STATUS_OUTSIDE_SCOPE if outside and all(not item.policy_conflicts for item in ranking.ranked if item.authority != AUTHORITY_OUTSIDE_SCOPE) else STATUS_UNAVAILABLE
        reasons = [f"{item.option_id}: " + "; ".join(item.policy_conflicts + [r for r in item.reasons if r.startswith("outside authority")]) for item in ranking.ranked]
        terms = {item.option_id: item.exact_terms for item in outside}
        return DialogueDecision(new_id("dec"), status=status, chosen_option_id=None, cited_observation_ids=[], reasons=["no eligible option"] + reasons, exact_terms=terms, **base)
    cited = list(ranking.lm_choice.cited_observation_ids) if (ranking.lm_choice and best.lm_selected) else list(ranking.evidence_ids)
    return DialogueDecision(new_id("dec"), status=STATUS_CHOSEN, chosen_option_id=best.option_id, cited_observation_ids=cited, reasons=list(best.reasons), exact_terms=dict(best.exact_terms), **base)
