"""Bounded, read-only observations from the active match viewer.

The simulation and controller's cached statistics are separate objects. Only
the viewer's decoded frame owns the statistics returned here. The source is
explicit: replay classification is validated separately from the field layout.
"""
import math
import struct
from .process import MemoryReadError
from .rtti import require_type,type_info
from .signatures import pe_sections,scan,rip_target
from .fixtures import decode_fixture_values,RESULT_TYPE
from .club import direct_string_entry
from .players import identity
from .pointers import vector
from structures.fixture import FixtureTeam
from structures.match import Match,MatchObservation,TeamMatchStats,MatchPlayer

ROOT_PATTERN='48 8B 0D ?? ?? ?? ?? 48 8B 56 18 E8 ?? ?? ?? ?? 84 C0 74 15 48 8B 0D'
MANAGER_TYPE='.?AVMATCH_CONTROLLER_MANAGER@fmmatchviewer@@'
LIVE_TYPE='.?AVGAME_LIVE_MATCH_CONTROLLER@@'

def percentage(numerator,denominator):
    return (200*numerator+denominator)//(2*denominator) if denominator else None

def decode_team_stats(raw):
    if len(raw)!=0x280:raise MemoryReadError('Incomplete match team statistics')
    xg=struct.unpack_from('<f',raw,0x60)[0]
    attempted,completed=struct.unpack_from('<HH',raw,0xDC)
    if not math.isfinite(xg) or not 0<=xg<=100 or completed>attempted or attempted>10000:
        raise MemoryReadError('Invalid match statistics')
    if raw[0x162]>raw[0x161]:raise MemoryReadError('Shots on target exceed total shots')
    return dict(shots=raw[0x161],shots_on_target=raw[0x162],xg=round(xg,2),corners=raw[0x218],
                fouls=raw[0x21B],yellow_cards=raw[0x21D],passes_attempted=attempted,
                passes_completed=completed,pass_completion=percentage(completed,attempted))

def decode_clock(raw):
    if len(raw)!=0xE640:raise MemoryReadError('Incomplete match frame')
    second,minute=raw[0xCDE4:0xCDE6];flags=struct.unpack_from('<I',raw,0xAA4)[0]
    if minute>150 or second>59:raise MemoryReadError('Invalid match clock')
    # These three states have UI observations and corresponding clock code.
    # Extra-time and other pre/post-period combinations stay unknown.
    if flags&4 and not flags&0x18 and minute==90 and second==0:phase='full_time'
    elif flags&8 and flags&1 and not flags&0x20 and minute==45 and second==0:phase='half_time'
    elif flags&8 and not flags&1:phase='in_play'
    else:phase='unknown'
    return minute,second,phase

def decode_match_player(raw):
    if len(raw)!=0x100:raise MemoryReadError('Incomplete match player statistics')
    person_index=struct.unpack_from('<I',raw,0x10)[0]
    if person_index==0xFFFFFFFF:return None
    start=struct.unpack_from('<I',raw,0x64)[0]!=0
    sub_in,sub_out=struct.unpack_from('<ii',raw,0x50)
    rating,previous=struct.unpack_from('<HH',raw,0x76)
    if raw[0x7B] not in (0,1) or not 0<rating<=1000 or sub_in< -1 or sub_out< -1:
        raise MemoryReadError('Invalid match player statistics')
    if raw[0x85]>raw[0x84] or raw[0x7F]>99 or raw[0x9A]>2:raise MemoryReadError('Invalid match player counters')
    appeared=start or sub_in>=0
    return person_index,dict(side='home' if raw[0x7B]==0 else 'away',shirt_number=raw[0x7A],started=start,
        appeared=appeared,substituted_in=sub_in>=0,substituted_out=sub_out>=0,
        rating=((rating+5)//10)/10 if appeared else None,
        rating_may_be_provisional=appeared and previous==0,
        goals=raw[0x7F],shots=raw[0x84],shots_on_target=raw[0x85],yellow_cards=raw[0x9A])

class MatchReader:
    def __init__(self,db):
        self.db=db;self.fm=db.fm;self.global_ptr=None;self.manager=None;self.player_index=None

    def index_players(self):
        # This internal index is scoped to the concrete Player registry type;
        # it is never returned to API consumers as an FM unique ID.
        result={}
        for person in self.db.person_pointers():
            if self.db.type_offset(person)!=0x278:continue
            idx=self.fm.read_uint32(person+8)
            if idx in result:raise MemoryReadError('Duplicate player index')
            result[idx]=person
        self.player_index=result

    def resolve(self):
        db=self.db;f=self.fm;db.person_pointers();candidates={}
        for s in pe_sections(f,db.module):
            if not s.characteristics&0x20000000 or s.characteristics&0x80000000:continue
            for hit in scan(f,s.base,s.size,ROOT_PATTERN):
                target=rip_target(f,hit,3,7)
                if not db.module.base<=target<=db.module.base+db.module.size-8:continue
                try:
                    manager=f.read_pointer(target);require_type(db,manager,MANAGER_TYPE)
                    if f.read_uint64(manager+0x10)>64:continue
                    candidates[(target,manager)]=True
                except MemoryReadError:continue
        if len(candidates)!=1:raise MemoryReadError('Could not resolve one match viewer manager')
        self.global_ptr,self.manager=next(iter(candidates));return self

    def check(self):
        if self.fm.read_pointer(self.global_ptr)!=self.manager:raise MemoryReadError('Match manager changed; reconnect')
        require_type(self.db,self.manager,MANAGER_TYPE)

    def controllers(self,snapshots):
        self.check();f=self.fm
        def observed(p,n):
            r=f.read_bytes(p,n);snapshots.append((p,r));return r
        owner=observed(self.manager+8,16);head,count=struct.unpack('<QQ',owner)
        if count>64:raise MemoryReadError('Match controller count exceeds supported bound')
        header=observed(head,0x20)
        if not header[0x19]:raise MemoryReadError('Invalid match controller sentinel')
        pending=[struct.unpack_from('<Q',header,8)[0]];seen=set();live=[];other=[]
        while pending:
            node=pending.pop()
            if node==head:continue
            if node in seen or len(seen)>=count:raise MemoryReadError('Invalid match controller tree')
            seen.add(node);r=observed(node,0x30)
            if r[0x19]:raise MemoryReadError('Unexpected sentinel in match controller tree')
            pending.extend(struct.unpack_from('<Q',r,o)[0] for o in (0,0x10))
            key,controller=struct.unpack_from('<QQ',r,0x20);info=type_info(self.db,controller)
            if info['offset']!=0:raise MemoryReadError('Unsupported match controller subobject')
            if info['name']==LIVE_TYPE:live.append((key,controller))
            elif info['name']!='.?AVGAME_LIVE_LATEST_SCORES_CONTROLLER@@':other.append(info['name'])
        if len(seen)!=count:raise MemoryReadError('Incomplete match controller tree')
        return live,other

    def read(self,context):
        context.check();known=set(self.db.person_pointers());db=self.db;f=self.fm;snapshots=[]
        if self.player_index is None or any(p not in known for p in self.player_index.values()):self.index_players()
        def observed(p,n):
            raw=f.read_bytes(p,n);snapshots.append((p,raw));return raw
        def pointer(p):return int.from_bytes(observed(p,8),'little')
        def finish():
            for address,raw in snapshots:
                if f.read_bytes(address,len(raw))!=raw:raise MemoryReadError('Match viewer changed during observation; retry')
            self.check();context.check();db.person_pointers()
        live,other=self.controllers(snapshots)
        if not live:
            finish()
            return MatchObservation(False,'unsupported_viewer' if other else 'no_active_match_viewer',None)
        if len(live)!=1:raise MemoryReadError('Multiple live match viewers are not supported')
        key,c=live[0]
        if int.from_bytes(observed(c+0x3D0,8),'little')!=key:raise MemoryReadError('Match controller identity mismatch')
        wrapper=pointer(c+0x20)
        if not wrapper:
            finish();return MatchObservation(False,'match_viewer_not_ready',None)
        impl=pointer(wrapper);game=pointer(impl+0x1C0)
        if not game:
            finish();return MatchObservation(False,'match_frame_not_ready',None)
        require_type(db,game,'.?AVGAME_MATCH@@')
        # Capture once and compare only the meaningful identity/clock/owner fields
        # at completion. Animating 3D coordinates may change independently.
        frame=f.read_bytes(game,0xE640)
        for start,end in ((0,8),(0x28,0x30),(0x680,0x688),(0x930,0x938),(0x9E8,0x9EC),(0xA84,0xA88),(0xAA4,0xAA8),(0xCDE4,0xCDE6)):
            snapshots.append((game+start,frame[start:end]))
        if struct.unpack_from('<Q',frame,0x28)[0]!=key:raise MemoryReadError('Viewer frame belongs to a different match')
        stats=struct.unpack_from('<Q',frame,0x930)[0]
        result=pointer(stats+0x10);require_type(db,result,RESULT_TYPE);rr=observed(result,0x80)
        day,time,hs,aws=decode_fixture_values(rr,True)
        home,away=struct.unpack_from('<QQ',rr,8)
        if context.team not in (home,away):raise MemoryReadError('Active viewer is not the current team match')
        def team(p):
            require_type(db,p,'.?AVTEAM@db@@');club=pointer(p+0x30);require_type(db,club,'.?AVCLUB@db@@')
            tid=int.from_bytes(observed(p+12,4),'little');cid=int.from_bytes(observed(club+12,4),'little')
            if not 0<tid<0x80000000 or not 0<cid<0x80000000:raise MemoryReadError('Invalid match team identity')
            return FixtureTeam(tid,cid,direct_string_entry(f,pointer(club+0xC0)))
        ht,at=team(home),team(away)
        fn=struct.unpack_from('<Q',rr,0x20)[0];require_type(db,fn,'.?AVFIXTURE_NAME@db@@');comp=pointer(fn+0x18)
        require_type(db,comp,'.?AVCOMP@db@@')
        if comp!=struct.unpack_from('<Q',frame,0x680)[0]:raise MemoryReadError('Match competition mismatch')
        cid=int.from_bytes(observed(comp+12,4),'little');cname=direct_string_entry(f,pointer(comp+0x48))
        hpointer=pointer(stats+0x50);apointer=pointer(stats+0x58)
        hraw=observed(hpointer,0x280);araw=observed(apointer,0x280)
        h=decode_team_stats(hraw);a=decode_team_stats(araw)
        players=[];seen=set()
        for side,team_pointer,team_raw in [('home',hpointer,hraw),('away',apointer,araw)]:
            begin,end=vector(f,team_pointer+0x240,max_count=64)
            if (begin,end)!=struct.unpack_from('<QQ',team_raw,0x240):raise MemoryReadError('Player statistics list changed')
            addresses=observed(begin,end-begin);count=0
            for (pp,) in struct.iter_unpack('<Q',addresses):
                require_type(db,pp,'.?AVGAME_MATCH_PLAYER_STATS@@');pr=observed(pp,0x100)
                decoded=decode_match_player(pr)
                if decoded is None:continue
                index,fields=decoded;person=self.player_index.get(index)
                if person not in known or int.from_bytes(observed(person+8,4),'little')!=index:
                    raise MemoryReadError('Match player is not in the supported database registry')
                if db.type_offset(person)!=0x278:raise MemoryReadError('Match player type changed')
                ident=observed(person,0x78);uid,name,_,_=identity(f,person)
                if uid!=struct.unpack_from('<I',ident,12)[0] or uid in seen or fields['side']!=side:
                    raise MemoryReadError('Match player identity or side mismatch')
                seen.add(uid);players.append(MatchPlayer(uid,name,**fields));count+=1
            if count!=team_raw[0] or not 11<=count<=64:raise MemoryReadError('Incomplete match player roster')
        total=h['passes_completed']+a['passes_completed'];hp=percentage(h['passes_completed'],total)
        minute,second,phase=decode_clock(frame)
        model=Match(day.isoformat(),time,cid,cname,ht,at,hs,aws,minute,second,phase,
                    TeamMatchStats(**h,possession=hp),TeamMatchStats(**a,possession=100-hp if hp is not None else None),players=players)
        finish();return MatchObservation(True,None,model)
