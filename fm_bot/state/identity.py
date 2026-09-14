"""Career, branch and checkpoint identity plus continuity checks.

A bridge ``session_id`` is a connection identifier and never a save
identifier. A saved-file checksum identifies a checkpoint, not an enduring
career, because the file changes when saved again. Careers are registered
explicitly with an orchestrator-generated identifier; branches record their
parent and checkpoint so laboratory forks never merge into production history.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

from .units import game_time_key


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class SaveManifest:
    """What we know about a save file at one moment. Identifies a checkpoint."""

    build: str
    manager_id: int
    club_id: int
    game_date: str
    game_time: str | None
    path: str | None = None
    checksum_sha256: str | None = None
    recorded_at: str = field(default_factory=utc_now)
    notes: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SaveManifest":
        return cls(**data)


@dataclass(frozen=True)
class CareerIdentity:
    career_id: str
    label: str
    build: str
    manager_id: int
    club_id: int
    registered_at: str = field(default_factory=utc_now)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class BranchKind(str, Enum):
    PRODUCTION = "production"
    LABORATORY = "laboratory"


@dataclass(frozen=True)
class BranchIdentity:
    branch_id: str
    career_id: str
    kind: BranchKind
    parent_branch_id: str | None = None
    checkpoint_id: str | None = None
    created_at: str = field(default_factory=utc_now)
    label: str | None = None

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        return data


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    branch_id: str
    manifest: SaveManifest
    created_at: str = field(default_factory=utc_now)
    label: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"checkpoint_id": self.checkpoint_id, "branch_id": self.branch_id, "manifest": self.manifest.to_json(), "created_at": self.created_at, "label": self.label}


@dataclass(frozen=True)
class Anchor:
    """The identity and time context observed at one collection."""

    career_id: str | None
    branch_id: str | None
    session_id: str | None
    build: str | None
    manager_id: int | None
    club_id: int | None
    game_date: str | None
    game_time: str | None
    sequence: int = 0            # monotonic local sequence
    observed_at: str = field(default_factory=utc_now)

    def game_key(self) -> tuple[int, int] | None:
        if self.game_date is None:
            return None
        return game_time_key(self.game_date, self.game_time)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class ContinuityStatus(str, Enum):
    CONTINUOUS = "continuous"
    UNKNOWN_LINEAGE = "unknown_lineage"        # no confirmed registration for this observation
    SESSION_CHANGED = "session_changed"        # reconnect; lineage must be confirmed
    DATE_REVERSED = "date_reversed"            # a load happened
    FORWARD_JUMP = "forward_jump"              # time moved without the bot progressing
    IDENTITY_CHANGED = "identity_changed"      # manager or club differs
    BUILD_CHANGED = "build_changed"
    DISCONNECTED = "disconnected"


@dataclass
class ContinuityResult:
    status: ContinuityStatus
    reasons: list[str] = field(default_factory=list)

    @property
    def invalidates_snapshot(self) -> bool:
        return self.status is not ContinuityStatus.CONTINUOUS

    @property
    def requires_lineage_confirmation(self) -> bool:
        return self.status in (ContinuityStatus.UNKNOWN_LINEAGE, ContinuityStatus.SESSION_CHANGED, ContinuityStatus.FORWARD_JUMP)

    @property
    def stops_for_identity_resolution(self) -> bool:
        return self.status in (ContinuityStatus.IDENTITY_CHANGED, ContinuityStatus.BUILD_CHANGED, ContinuityStatus.DATE_REVERSED, ContinuityStatus.DISCONNECTED)

    def to_json(self) -> dict[str, Any]:
        return {"status": self.status.value, "reasons": list(self.reasons), "invalidates_snapshot": self.invalidates_snapshot, "requires_lineage_confirmation": self.requires_lineage_confirmation}


def check_continuity(previous: Anchor | None, current: Anchor, *, lineage_confirmed: bool = False, bot_progressed: bool = False) -> ContinuityResult:
    """Decide whether ``current`` continues the history ending at ``previous``.

    ``lineage_confirmed`` means the operator (or a matching save manifest) has
    confirmed that the currently loaded save is the registered career branch.
    ``bot_progressed`` means the bot itself pressed Continue since ``previous``
    and therefore expects in-game time to have advanced.

    Date monotonicity alone is never sufficient: a forward-dated load also
    changes the underlying career state.
    """
    reasons: list[str] = []
    if current.session_id is None or current.manager_id is None or current.club_id is None or current.game_date is None:
        return ContinuityResult(ContinuityStatus.DISCONNECTED, ["current observation lacks session, identity or game time"])
    if previous is None:
        if lineage_confirmed:
            return ContinuityResult(ContinuityStatus.CONTINUOUS, ["first observation on a confirmed registration"])
        return ContinuityResult(ContinuityStatus.UNKNOWN_LINEAGE, ["no previous anchor and lineage not confirmed"])
    if previous.build and current.build and previous.build != current.build:
        return ContinuityResult(ContinuityStatus.BUILD_CHANGED, [f"build {previous.build} -> {current.build}"])
    if previous.manager_id != current.manager_id or previous.club_id != current.club_id:
        reasons.append(f"manager/club {previous.manager_id}/{previous.club_id} -> {current.manager_id}/{current.club_id}")
        return ContinuityResult(ContinuityStatus.IDENTITY_CHANGED, reasons)
    if previous.career_id and current.career_id and previous.career_id != current.career_id:
        return ContinuityResult(ContinuityStatus.IDENTITY_CHANGED, [f"career {previous.career_id} -> {current.career_id}"])
    if previous.branch_id and current.branch_id and previous.branch_id != current.branch_id:
        return ContinuityResult(ContinuityStatus.IDENTITY_CHANGED, [f"branch {previous.branch_id} -> {current.branch_id}"])
    prev_key, cur_key = previous.game_key(), current.game_key()
    if prev_key is not None and cur_key is not None and cur_key < prev_key:
        return ContinuityResult(ContinuityStatus.DATE_REVERSED, [f"game time {previous.game_date} {previous.game_time} -> {current.game_date} {current.game_time}"])
    if previous.session_id != current.session_id:
        reasons.append(f"bridge session {previous.session_id} -> {current.session_id}; reconnecting to the same process or club does not prove continuity")
        if lineage_confirmed:
            reasons.append("lineage confirmed by operator or manifest")
            return ContinuityResult(ContinuityStatus.CONTINUOUS, reasons)
        return ContinuityResult(ContinuityStatus.SESSION_CHANGED, reasons)
    if prev_key is not None and cur_key is not None and cur_key > prev_key and not bot_progressed and not lineage_confirmed:
        reasons.append(f"game time advanced {previous.game_date} {previous.game_time} -> {current.game_date} {current.game_time} without the bot progressing")
        return ContinuityResult(ContinuityStatus.FORWARD_JUMP, reasons)
    if current.sequence < previous.sequence:
        return ContinuityResult(ContinuityStatus.DATE_REVERSED, ["local sequence moved backwards; contradictory history"])
    return ContinuityResult(ContinuityStatus.CONTINUOUS, reasons or ["same session, identity and non-reversed time"])


class CareerRegistry:
    """Registry of careers, branches and checkpoints backed by a store.

    The registry never merges histories across branches. Laboratory forks get
    a new branch with an explicit parent and checkpoint.
    """

    def __init__(self, store):
        self.store = store

    def register_career(self, label: str, manifest: SaveManifest, *, career_id: str | None = None) -> tuple[CareerIdentity, BranchIdentity, Checkpoint]:
        career = CareerIdentity(career_id or new_id("career"), label, manifest.build, manifest.manager_id, manifest.club_id)
        branch = BranchIdentity(new_id("branch"), career.career_id, BranchKind.PRODUCTION, None, None, label="main")
        checkpoint = Checkpoint(new_id("ckpt"), branch.branch_id, manifest, label="registration")
        branch = BranchIdentity(branch.branch_id, branch.career_id, branch.kind, None, checkpoint.checkpoint_id, branch.created_at, branch.label)
        with self.store.transaction():
            self.store.insert_career(career)
            self.store.insert_branch(branch)
            self.store.insert_checkpoint(checkpoint)
        return career, branch, checkpoint

    def fork_branch(self, parent: BranchIdentity, manifest: SaveManifest, *, kind: BranchKind = BranchKind.LABORATORY, label: str | None = None) -> tuple[BranchIdentity, Checkpoint]:
        if parent.kind is BranchKind.PRODUCTION and kind is BranchKind.PRODUCTION:
            raise ValueError("a production branch cannot be forked into another production branch; production history is linear")
        checkpoint = Checkpoint(new_id("ckpt"), parent.branch_id, manifest, label=label)
        branch = BranchIdentity(new_id("branch"), parent.career_id, kind, parent.branch_id, checkpoint.checkpoint_id, label=label)
        with self.store.transaction():
            self.store.insert_checkpoint(checkpoint)
            self.store.insert_branch(branch)
        return branch, checkpoint

    def record_checkpoint(self, branch: BranchIdentity, manifest: SaveManifest, label: str | None = None) -> Checkpoint:
        checkpoint = Checkpoint(new_id("ckpt"), branch.branch_id, manifest, label=label)
        with self.store.transaction():
            self.store.insert_checkpoint(checkpoint)
        return checkpoint

    def matches_registration(self, career: CareerIdentity, status_payload: dict[str, Any], manager_id: int, club_id: int) -> tuple[bool, list[str]]:
        problems = []
        if status_payload.get("build") != career.build:
            problems.append(f"build {status_payload.get('build')} differs from registered {career.build}")
        if manager_id != career.manager_id:
            problems.append(f"manager {manager_id} differs from registered {career.manager_id}")
        if club_id != career.club_id:
            problems.append(f"club {club_id} differs from registered {career.club_id}")
        return (not problems), problems
