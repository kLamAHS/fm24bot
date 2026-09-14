from dataclasses import dataclass, field


@dataclass
class TrainingSession:
    slot: int
    name: str | None
    kind: str
    status: str = 'decoded'


@dataclass
class TrainingDay:
    date: str
    sessions: list[TrainingSession]


@dataclass
class TrainingWeek:
    start_date: str
    stored_name: str | None
    days: list[TrainingDay]


@dataclass
class TrainingProgram:
    player_id: int
    player_name: str
    additional_focus: str | None
    additional_focus_status: str
    intensity_setting: str | None
    intensity_setting_status: str
    rating: float | None = None
    rating_status: str = 'not_decoded'
    position_role_duty_status: str = 'not_decoded'


@dataclass
class Training:
    available: bool
    reason: str | None = None
    scope: str = 'current_team_committed_schedule'
    weeks: list[TrainingWeek] = field(default_factory=list)
    current_week_start: str | None = None
    individual_programs: list[TrainingProgram] = field(default_factory=list)
