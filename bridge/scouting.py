"""Bounded manager-owned shortlists, reports and transfer targets.

These are independent of the current screen. UI widgets contain temporary
copies of transfer targets and must never be used as the ownership root.
"""
import struct
from .process import MemoryReadError
from .players import identity
from .strings import direct_string_entry
from .dates import decode_game_date
from .rtti import require_type,type_info
from structures.scouting import (ShortlistPlayer,Shortlist,Shortlists,
    ScoutReport,Scouting,TransferTarget,TransferTargets)

class Observation:
    def __init__(self,context):
        context.check()
        self.context=context;self.db=context.db;self.fm=self.db.fm
        self.known=set(self.db.person_pointers());self.today=self.db.current_date()
        self.clock=self.db.dates.read_raw();self.guards={}

    def read(self,address,size):
        raw=self.fm.read_bytes(address,size)
        key=(address,size)
        if key in self.guards and raw!=self.guards[key]:
            raise MemoryReadError('Scouting changed during observation')
        self.guards[key]=raw
        return raw

    def pointer(self,address):return struct.unpack('<Q',self.read(address,8))[0]

    def vector(self,address,limit):
        begin,end=struct.unpack('<QQ',self.read(address,16))
        if begin==end==0:return []
        if not 0x10000<=begin<=end<0x7fffffff0000 or begin%8 or end%8 or end-begin>8*limit:
            raise MemoryReadError('Invalid scouting vector')
        raw=self.read(begin,end-begin) if end>begin else b''
        rows=[p for (p,) in struct.iter_unpack('<Q',raw)]
        if len(set(rows))!=len(rows) or any(p<0x10000 or p>=0x7fffffff0000 for p in rows):
            raise MemoryReadError('Invalid or duplicate scouting pointer')
        return rows

    def person(self,pointer,player=False):
        if pointer not in self.known:raise MemoryReadError('Scouting Person outside registry')
        ti=type_info(self.db,pointer)
        permitted={('.?AVACTUAL_PLAYER@db@@',0x278)}
        if not player:permitted|={('.?AVACTUAL_NON_PLAYER@db@@',0xf8),('.?AVHUMAN_NON_PLAYER@db@@',0x450)}
        if (ti['name'],ti['offset']) not in permitted:raise MemoryReadError('Unsupported scouting Person type')
        self.read(pointer,0x78)
        return identity(self.fm,pointer)[:2]

    def date(self,raw):
        value=decode_game_date(raw)
        if value>self.today:raise MemoryReadError('Scouting record is dated in the future')
        return value.isoformat()

    def finish(self,result):
        for (address,size),raw in self.guards.items():
            if self.fm.read_bytes(address,size)!=raw:raise MemoryReadError('Scouting changed during observation')
        if self.db.dates.read_raw()!=self.clock or set(self.db.person_pointers())!=self.known:
            raise MemoryReadError('Scouting registry or game time changed')
        self.context.check()
        return result

def read_shortlists(context):
    o=Observation(context);lists=[]
    for pointer in o.vector(context.staff+0x210,256):
        if o.pointer(pointer)!=context.staff:raise MemoryReadError('Shortlist owner mismatch')
        # Kind 0 includes both the default and user-named player lists.
        # Kinds 6/8 are not user player shortlists; do not mislabel them.
        if o.read(pointer+0x24,1)[0]!=0:continue
        name_pointer=o.pointer(pointer+0x30)
        name=direct_string_entry(o.fm,name_pointer) if name_pointer else 'Default'
        players=[];ids=set()
        for entry in o.vector(pointer+0xc0,10000):
            raw=o.read(entry,0x30);uid,display=o.person(struct.unpack_from('<Q',raw)[0],True)
            if uid in ids:raise MemoryReadError('Duplicate player in shortlist')
            ids.add(uid);players.append(ShortlistPlayer(uid,display,o.date(raw[0x20:0x24])))
        lists.append(Shortlist(name,not bool(name_pointer),players))
    if sum(row.is_default for row in lists)>1:raise MemoryReadError('Multiple default player shortlists')
    return o.finish(Shortlists(lists))

def read_scouting(context):
    o=Observation(context);reports=[]
    for pointer in o.vector(context.staff+0x108,50000):
        raw=o.read(pointer,0x44)
        player,scout,team=struct.unpack_from('<QQQ',raw)
        # This collection is manager-specific, but reports can be for other
        # teams managed previously. Validate the team link before decoding.
        require_type(o.db,team,'.?AVTEAM@db@@')
        pid,pname=o.person(player,True);sid,sname=o.person(scout)
        reports.append(ScoutReport(pid,pname,sid,sname,o.date(raw[0x18:0x1c]),
            {4:'Reasonable',5:'Extensive'}.get(raw[0x40])))
    return o.finish(Scouting(reports))

def read_transfer_targets(context):
    o=Observation(context);targets=[];seen=set()
    holder=o.pointer(context.staff+0x370)
    for group in o.vector(holder+0xb48,1000):
        name_pointer=o.pointer(group)
        name=direct_string_entry(o.fm,name_pointer) if name_pointer else None
        for pointer in o.vector(group+8,10000):
            if pointer in seen:raise MemoryReadError('Transfer target belongs to multiple groups')
            seen.add(pointer)
            require_type(o.db,pointer,'.?AVTRANSFER_TARGET@db@@')
            raw=o.read(pointer,0x66);pid,pname=o.person(struct.unpack_from('<Q',raw,8)[0],True)
            targets.append(TransferTarget(pid,pname,o.date(raw[0x10:0x14]),
                {4:'Transfer'}.get(raw[0x60]),{0:'Not Started',4:'On Hold'}.get(raw[0x62]),
                {1:'Urgent',2:'Normal'}.get(raw[0x65]),name))
    return o.finish(TransferTargets(targets))
