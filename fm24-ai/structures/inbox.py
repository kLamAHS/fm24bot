from dataclasses import dataclass,field

@dataclass
class InboxMessage:
    id:int
    date:str
    time:str|None
    unread:bool
    event_type:str
    sender_id:int|None
    sender_name:str|None
    subject:str|None=None
    body:str|None=None
    text_status:str='not_decoded'
    time_status:str='current'

@dataclass
class Inbox:
    messages:list[InboxMessage]=field(default_factory=list)
    unread_count:int=0
    scope:str='current_human_inbox'
