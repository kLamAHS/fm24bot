"""Current calendar-year fixtures from the competition manager's owned lists.

Temporary UI fixture copies are deliberately excluded. See research/fixtures.md.
"""
import struct
from .process import MemoryReadError
from .pointers import vector
from .rtti import require_type
from .signatures import pe_sections,scan,rip_target
from .dates import decode_game_date,decode_game_time
from .club import direct_string_entry
from structures.fixture import Fixture,FixtureTeam,Fixtures

ROOT_PATTERN='4C 8B 25 ?? ?? ?? ?? 49 8B 44 24 28 49 39 44 24 30 0F 84 ?? ?? ?? ??'
FIXTURE_TYPE='.?AVFIXTURE@sicomps@@'
RESULT_TYPE='.?AVFIXTURE_RESULT@sicomps@@'

def decode_fixture_values(raw,result):
    if len(raw)!=(0x80 if result else 0x58): raise MemoryReadError('Incomplete fixture record')
    day=decode_game_date(raw[0x4C:0x50]);time=decode_game_time(raw[0x4C:0x50])
    home_score,away_score=(raw[0x64],raw[0x68]) if result else (None,None)
    if result and (home_score>99 or away_score>99): raise MemoryReadError('Invalid fixture score')
    return day,time,home_score,away_score

class FixtureReader:
    def __init__(self,db): self.db=db;self.fm=db.fm;self.global_ptr=None;self.wrapper=None;self.manager=None

    def resolve(self):
        db=self.db;f=self.fm;db.person_pointers();candidates={}
        for s in pe_sections(f,db.module):
            if not s.characteristics&0x20000000 or s.characteristics&0x80000000:continue
            for hit in scan(f,s.base,s.size,ROOT_PATTERN):
                target=rip_target(f,hit,3,7)
                if not db.module.base<=target<=db.module.base+db.module.size-8:continue
                try:
                    wrapper=f.read_pointer(target);require_type(db,wrapper,'.?AVGAME_RULE_GROUP_MANAGER@@')
                    manager=f.read_pointer(wrapper+8);require_type(db,manager,'.?AVGAME_COMP_MANAGER@@')
                    begin,end=vector(f,manager+0x18,max_count=512)
                    if begin==end:continue
                    year=f.read_uint16(manager+0x48)
                    if not 1900<=year<=2500:continue
                    candidates.setdefault((wrapper,manager),set()).add(target)
                except MemoryReadError:continue
        if len(candidates)!=1:raise MemoryReadError(f'Expected one fixture manager, found {len(candidates)}')
        (self.wrapper,self.manager),targets=next(iter(candidates.items()))
        self.global_ptr=min(targets)
        return self

    def check(self):
        self.db.person_pointers()
        f=self.fm
        if f.read_pointer(self.global_ptr)!=self.wrapper or f.read_pointer(self.wrapper+8)!=self.manager:
            raise MemoryReadError('Fixture manager changed; reconnect')
        require_type(self.db,self.wrapper,'.?AVGAME_RULE_GROUP_MANAGER@@')
        require_type(self.db,self.manager,'.?AVGAME_COMP_MANAGER@@')

    def read(self,context):
        self.check();context.check();db=self.db;f=self.fm
        db.current_date();clock=db.dates.read_raw();as_of=decode_game_date(clock)
        year=f.read_uint16(self.manager+0x48)
        if year!=as_of.year:raise MemoryReadError('Fixture calendar year differs from the current game year')
        begin,end=vector(f,self.manager+0x18,max_count=512);groups_raw=f.read_bytes(begin,end-begin)
        team_id=f.read_uint32(context.team+12);items=[];snapshots=[];seen=set()
        team_cache={};comp_cache={}
        def observed(address,size):
            raw=f.read_bytes(address,size);snapshots.append((address,raw));return raw
        def team_model(address):
            if address not in team_cache:
                require_type(db,address,'.?AVTEAM@db@@')
                club=int.from_bytes(observed(address+0x30,8),'little');require_type(db,club,'.?AVCLUB@db@@')
                tid=int.from_bytes(observed(address+12,4),'little');cid=int.from_bytes(observed(club+12,4),'little')
                name_ptr=int.from_bytes(observed(club+0xC0,8),'little')
                name=direct_string_entry(f,name_ptr)
                if not 0<tid<0x80000000 or not 0<cid<0x80000000:raise MemoryReadError('Invalid fixture team identity')
                team_cache[address]=FixtureTeam(tid,cid,name)
            return team_cache[address]
        for (group,) in struct.iter_unpack('<Q',groups_raw):
            days=f.read_bytes(group,366*8);snapshots.append((group,days))
            for day_index,(holder,) in enumerate(struct.iter_unpack('<Q',days)):
                if not holder:continue
                start,stop=vector(f,holder,max_count=10000);raw=f.read_bytes(start,stop-start)
                holder_raw=struct.pack('<QQ',start,stop)
                snapshots.extend([(holder,holder_raw),(start,raw)])
                for (address,) in struct.iter_unpack('<Q',raw):
                    header=f.read_bytes(address,24);home,away=struct.unpack_from('<QQ',header,8)
                    if context.team not in (home,away):continue
                    if address in seen:raise MemoryReadError('Duplicate fixture in owned lists')
                    seen.add(address)
                    from .rtti import type_info
                    info=type_info(db,address)
                    if info['offset']!=0 or info['name'] not in (FIXTURE_TYPE,RESULT_TYPE):raise MemoryReadError('Unsupported fixture type')
                    result=info['name']==RESULT_TYPE;record=f.read_bytes(address,0x80 if result else 0x58)
                    if record[:24]!=header:raise MemoryReadError('Fixture changed while reading its header')
                    day,time,hs,aws=decode_fixture_values(record,result)
                    if day.year!=year or day.timetuple().tm_yday-1!=day_index:raise MemoryReadError('Fixture is in the wrong calendar bucket')
                    fn=struct.unpack_from('<Q',record,0x20)[0];require_type(db,fn,'.?AVFIXTURE_NAME@db@@')
                    comp=int.from_bytes(observed(fn+0x18,8),'little')
                    if comp not in comp_cache:
                        require_type(db,comp,'.?AVCOMP@db@@')
                        comp_id=int.from_bytes(observed(comp+12,4),'little')
                        comp_name_ptr=int.from_bytes(observed(comp+0x48,8),'little')
                        if not 0<comp_id<0x80000000:raise MemoryReadError('Invalid competition identity')
                        comp_cache[comp]=(comp_id,direct_string_entry(f,comp_name_ptr))
                    cid,cname=comp_cache[comp]
                    items.append(Fixture(day.isoformat(),time,cid,cname,team_model(home),team_model(away),'played' if result else 'scheduled',hs,aws))
                    snapshots.append((address,record))
        # Re-read owner lists and returned records, not arbitrary heap candidates.
        for address,raw in snapshots:
            if f.read_bytes(address,len(raw))!=raw:raise MemoryReadError('Fixture collection changed during read')
        if vector(f,self.manager+0x18,max_count=512)!=(begin,end) or f.read_bytes(begin,end-begin)!=groups_raw:
            raise MemoryReadError('Fixture groups changed during read')
        self.check();context.check()
        if db.dates.read_raw()!=clock or f.read_uint16(self.manager+0x48)!=year:raise MemoryReadError('Game time changed during fixture read')
        items.sort(key=lambda x:(x.date,x.time,x.home.team_id,x.away.team_id,x.competition_id))
        return Fixtures(context.club_id,team_id,year,as_of.isoformat(),items)
