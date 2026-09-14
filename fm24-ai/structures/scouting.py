"""Public scouting observations; no process addresses or internal scores."""
from dataclasses import dataclass,field

@dataclass
class ShortlistPlayer:
    id:int
    name:str
    added_on:str
    expires_on:str|None=None
    expiry_status:str='not_decoded'

@dataclass
class Shortlist:
    name:str
    is_default:bool
    players:list[ShortlistPlayer]=field(default_factory=list)

@dataclass
class Shortlists:
    lists:list[Shortlist]=field(default_factory=list)
    scope:str='current_human_player_shortlists'

@dataclass
class ScoutReport:
    player_id:int
    player_name:str
    scout_id:int
    scout_name:str
    completed_on:str
    knowledge:str|None
    recommendation:str|None=None
    recommendation_status:str='not_decoded'
    text_status:str='not_decoded'

@dataclass
class Scouting:
    reports:list[ScoutReport]=field(default_factory=list)
    scope:str='current_human_stored_player_reports'
    knowledge_scope:str='stored_report; not a live worldwide knowledge estimate'

@dataclass
class TransferTarget:
    player_id:int
    player_name:str
    added_on:str
    type:str|None
    status:str|None
    priority:str|None
    group_name:str|None
    terms_status:str='not_decoded'

@dataclass
class TransferTargets:
    targets:list[TransferTarget]=field(default_factory=list)
    scope:str='current_human_transfer_targets'
