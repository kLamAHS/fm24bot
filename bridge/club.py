"""Resolve human manager and the directly referenced team roster."""
import struct
from .process import MemoryReadError
from .signatures import pe_sections,scan,rip_target
from .pointers import vector
from .players import identity,decode_player
from structures.club import Club
from structures.manager import Manager

MANAGER_PATTERN='48 8B 35 ?? ?? ?? ?? 48 8B 56 18 4C 8B 76 20 49 29 D6 B0 01 49 83 FE 10'

def direct_string_entry(fm,entry):
    length=fm.read_uint32(entry)
    if not 0<length<=512: raise MemoryReadError('Invalid string entry length')
    raw=fm.read_bytes(entry+4,length+1)
    if raw[-1]!=0: raise MemoryReadError('Invalid string terminator')
    value=raw[:-1].decode('utf-8')
    if not value.isprintable(): raise MemoryReadError('Invalid string contents')
    return value

class CurrentClub:
    def __init__(self,db):
        self.db=db; self.fm=db.fm

    def resolve(self):
        fm=self.fm; db=self.db
        persons=set(db.person_pointers())
        candidates={}
        for section in pe_sections(fm,db.module):
            if not section.characteristics & 0x20000000 or section.characteristics & 0x80000000: continue
            for hit in scan(fm,section.base,section.size,MANAGER_PATTERN):
                global_ptr=rip_target(fm,hit,3,7)
                root=fm.read_pointer(global_ptr)
                begin,end=vector(fm,root+0x18,max_count=32)
                for entry in range(begin,end,8):
                    staff=fm.read_pointer(entry)
                    # Find the actual embedded Person by registry membership
                    # and matching RTTI back-offset, never by a guessed human size.
                    for off in range(0x80,0x901,8):
                        person=staff+off
                        if person not in persons: continue
                        if db.type_offset(person)!=off: continue
                        uid,name,_,_=identity(fm,person)
                        contract=fm.read_pointer(person+0xC8)
                        team=fm.read_pointer(contract+0x10)
                        club=fm.read_pointer(team+0x30)
                        club_id=fm.read_uint32(club+0xC)
                        club_name=direct_string_entry(fm,fm.read_pointer(club+0xC0))
                        vector(fm,team+0x38,max_count=512)
                        candidates[person]=(uid,name,team,club,club_id,club_name,global_ptr,root,begin,end,staff)
        if len(candidates)!=1:
            raise MemoryReadError(f'Expected one human manager with a club, found {len(candidates)}. Multiple-manager and unemployed saves are not supported yet.')
        self.person,values=next(iter(candidates.items()))
        uid,name,self.team,self.club,self.club_id,self.club_name,self.global_ptr,self.root,self.manager_begin,self.manager_end,self.staff=values
        self.manager=Manager(uid,name)
        return self

    def check(self):
        fm=self.fm
        if not fm.alive() or fm.read_pointer(self.global_ptr)!=self.root or vector(fm,self.root+0x18,max_count=32)!=(self.manager_begin,self.manager_end):
            raise MemoryReadError('Human manager context changed; reconnect')
        manager_bases=struct.unpack('<'+'Q'*((self.manager_end-self.manager_begin)//8),fm.read_bytes(self.manager_begin,self.manager_end-self.manager_begin))
        if self.staff not in manager_bases or fm.read_uint32(self.person+0xC)!=self.manager.id:
            raise MemoryReadError('Human manager identity changed; reconnect')
        team=fm.read_pointer(fm.read_pointer(self.person+0xC8)+0x10)
        if team!=self.team or fm.read_pointer(team+0x30)!=self.club or fm.read_uint32(self.club+0xC)!=self.club_id:
            raise MemoryReadError('Current club changed; reconnect')

    def squad(self):
        self.check(); fm=self.fm
        as_of=self.db.current_date()
        begin,end=vector(fm,self.team+0x38,max_count=512)
        raw=fm.read_bytes(begin,end-begin)
        members=[]
        # Empirical team vector contains complete player pointers, validated
        # by their Person vtables and the global person registry.
        known=set(self.db.person_pointers())
        for (pointer,) in struct.iter_unpack('<Q',raw):
            if pointer in known:
                person=pointer
            elif pointer+0x278 in known:
                person=pointer+0x278
            else:
                raise MemoryReadError('Unrecognized roster member; refusing partial squad')
            members.append(decode_player(self.db,person,as_of=as_of))
        if len({p.id for p in members})!=len(members): raise MemoryReadError('Duplicate squad IDs')
        if vector(fm,self.team+0x38,max_count=512)!=(begin,end) or fm.read_bytes(begin,end-begin)!=raw:
            raise MemoryReadError('Squad changed during read')
        self.check()
        if self.db.current_date()!=as_of:
            raise MemoryReadError('Game date changed while reading squad')
        return members

    def model(self):
        return Club(self.club_id,self.club_name,self.squad())

    def evidence(self):
        b,e=vector(self.fm,self.team+0x38,max_count=512)
        return {'manager_person':hex(self.person),'manager_base':hex(self.staff),'team':hex(self.team),'club':hex(self.club),'roster_begin':hex(b),'roster_end':hex(e),'roster_count':(e-b)//8,'manager_global_rva':hex(self.global_ptr-self.db.module.base)}
