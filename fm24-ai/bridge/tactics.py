"""Current human team's selected tactic and lineup, never a saved lineup copy.

All object pointers are resolved on every read: a UI mentality change replaces
the creator object. See research/tactics.md for the code/UI validation.
"""
import struct
from .process import MemoryReadError
from .rtti import require_type
from .signatures import pe_sections, scan, rip_target
from .strings import direct_string_entry
from .players import identity
from structures.tactics import Tactics, TacticPosition, TacticSubstitute

ROOT_PATTERN = ('48 8B 0D ?? ?? ?? ?? 48 8B 95 C8 21 00 00 E8 ?? ?? ?? ?? '
                '48 85 C0 74 0A 0F B6 40 19 88 85 58 22 00 00')
MENTALITIES = {1:'Very Defensive',2:'Defensive',3:'Cautious',4:'Balanced',
               5:'Positive',6:'Attacking',7:'Very Attacking'}
# Explicitly observed position codes, not a general guess about bit fields.
POSITIONS = {1:'GK',4:'DR',8:'DL',0x10:'DC',0x200010:'DCR',0x100010:'DCL',
             0x20:'WBR',0x40:'WBL',0x80:'DM',0x200080:'DMCR',0x100080:'DMCL',
             0x100:'MR',0x200:'ML',0x200400:'MCR',0x100400:'MCL',
             0x800:'AMR',0x1000:'AML',0x2000:'AMC',
             0x4000:'STC',0x204000:'STCR',0x104000:'STCL'}
# Only combinations seen on multiple UI position records are labelled.
ROLE_DUTY = {0x200001:('Goalkeeper','Defend'),0x200002:('Central Defender','Defend'),
             0x400004:('Full-Back','Support'),0x400008:('Wing-Back','Support'),
             0x200010:('Defensive Midfielder','Defend'),
             0x200020:('Central Midfielder','Defend'),0x400020:('Central Midfielder','Support'),
             0x400040:('Wide Midfielder','Support'),0x400080:('Winger','Support'),
             0x400400:('Deep-Lying Forward','Support'),0x800800:('Advanced Forward','Attack'),
             0x410000:('Box To Box Midfielder','Support'),0x4000002:('Central Defender','Cover')}
# Independently checked with different selected players across the two sessions.
ROLE_DUTY.update({0x201000:('Sweeper Keeper','Defend'),
                  0x408000:('Deep-Lying Playmaker','Support'),
                  0x1200000:('Ball Playing Defender','Defend'),
                  0x400200:('Attacking Midfielder','Support')})


class TacticsReader:
    def __init__(self, db):
        self.db, self.fm = db, db.fm
        self.global_ptr = None
        self.player_index = {}

    def resolve(self):
        candidates = set()
        for section in pe_sections(self.fm,self.db.module):
            if not section.characteristics & 0x20000000 or section.characteristics & 0x80000000:
                continue
            for hit in scan(self.fm,section.base,section.size,ROOT_PATTERN):
                target = rip_target(self.fm,hit,3,7)
                if not self.db.module.base <= target <= self.db.module.base+self.db.module.size-8:
                    continue
                try:
                    require_type(self.db,self.fm.read_pointer(target),'.?AVTACTICS_MANAGER@@')
                    candidates.add(target)
                except MemoryReadError:
                    continue
        if len(candidates) != 1:
            raise MemoryReadError('Expected one supported tactics manager')
        self.global_ptr = candidates.pop()
        return self

    def read(self, context):
        context.check()
        db, fm = self.db, self.fm
        known = set(db.person_pointers())
        db.current_date()
        clock = db.dates.read_raw()
        guards = []

        def observed(address,size):
            raw = fm.read_bytes(address,size)
            guards.append((address,raw))
            return raw

        def pointer(address):
            return struct.unpack('<Q',observed(address,8))[0]

        def vector_data(address,stride,maximum):
            begin,end = struct.unpack('<QQ',observed(address,16))
            if begin == end == 0:
                return b''
            if not 0x10000 <= begin <= end < 0x7fffffff0000 or begin%8 or (end-begin)%stride or end-begin>stride*maximum:
                raise MemoryReadError('Invalid tactics vector')
            return observed(begin,end-begin) if end != begin else b''

        def lookup(tree_address,key,maximum):
            head,count = struct.unpack('<QQ',observed(tree_address,16))
            if count > maximum or not head:
                raise MemoryReadError('Invalid tactics tree')
            node = pointer(head+8)
            seen = set()
            while node != head:
                if node in seen or len(seen)>=min(count,64):
                    raise MemoryReadError('Cyclic or oversized tactics tree')
                seen.add(node)
                raw = observed(node,0x28)
                if raw[0x19] != 0:
                    raise MemoryReadError('Unexpected tactics tree sentinel')
                current = struct.unpack_from('<I',raw,0x20)[0]
                if current == key:
                    return node+0x28
                node = struct.unpack_from('<Q',raw,0 if key<current else 0x10)[0]
            return None

        def finish(result):
            for address,raw in guards:
                if fm.read_bytes(address,len(raw)) != raw:
                    raise MemoryReadError('Tactics or lineup changed during observation')
            if db.dates.read_raw() != clock or set(db.person_pointers()) != known:
                raise MemoryReadError('Tactics registry or game time changed')
            context.check()
            return result

        root = pointer(self.global_ptr)
        require_type(db,root,'.?AVTACTICS_MANAGER@@')
        human_records = vector_data(root+0x18,8,32)
        matches = []
        for (record,) in struct.iter_unpack('<Q',human_records):
            if pointer(record+0x68) == context.staff:
                matches.append(record)
        if len(matches) != 1:
            raise MemoryReadError('Expected one tactics record for the current human')
        require_type(db,context.team,'.?AVTEAM@db@@')
        team_index = struct.unpack('<I',observed(context.team+8,4))[0]
        value = lookup(matches[0],team_index,512)
        if value is None:
            return finish(Tactics(False,'no_tactic_for_current_team'))
        slots = vector_data(value+0x1010,8,3)
        selected = observed(value+0x1028,1)[0]
        if not slots:
            return finish(Tactics(False,'no_tactic_selected'))
        if selected > 2 or selected*8 >= len(slots):
            raise MemoryReadError('Selected tactic is outside the owned slots')
        creator = struct.unpack_from('<Q',slots,selected*8)[0]
        if not creator:
            return finish(Tactics(False,'empty_tactic_slot'))
        raw = observed(creator,0x458)
        mentality = MENTALITIES.get(raw[0x19])
        if mentality is None:
            raise MemoryReadError('Unknown tactic mentality')
        style_ptr = struct.unpack_from('<Q',raw,0x20)[0]
        name_ptr = struct.unpack_from('<Q',raw,0x450)[0]
        style = direct_string_entry(fm,style_ptr) if style_ptr else 'Custom'
        name = direct_string_entry(fm,name_ptr) if name_ptr else None
        lineup = struct.unpack('<26I',observed(value+0x1030,26*4))
        selected_ids = [i for i in lineup if i != 0xffffffff]
        if len(set(selected_ids)) != len(selected_ids):
            raise MemoryReadError('Duplicate selected player')
        # The internal index is not an FM UID or a permanent registry position.
        if any(i not in self.player_index or self.player_index[i] not in known for i in selected_ids):
            self.player_index = {}
            for person in known:
                if db.type_offset(person) != 0x278:
                    continue
                idx = fm.read_uint32(person+8)
                if idx in self.player_index:
                    raise MemoryReadError('Duplicate internal player index')
                self.player_index[idx] = person
        players = []
        for idx in lineup:
            if idx == 0xffffffff:
                players.append((None,None))
                continue
            person = self.player_index.get(idx)
            if person not in known or struct.unpack('<I',observed(person+8,4))[0] != idx or db.type_offset(person)!=0x278:
                raise MemoryReadError('Selected player is outside the current registry')
            observed(person+8,0x68)
            uid,player_name,_,_ = identity(fm,person)
            players.append((uid,player_name))
        positions = []
        for i in range(11):
            record = raw[0x30+i*0x48:0x78+i*0x48]
            code = struct.unpack_from('<I',record,8)[0]
            source = 'position'
            # FM permits player-specific instructions for the same position.
            # Follow the game's getter, including its enable byte, before
            # interpreting the effective role/duty combination.
            override = lookup(creator+0x428,lineup[i],2048) if lineup[i]!=0xffffffff else None
            if override is not None:
                data = vector_data(override,0x48,64)
                active = [data[j:j+0x48] for j in range(0,len(data),0x48)
                          if struct.unpack_from('<I',data,j+8)[0]==code and data[j+0x28]]
                if len(active)>1:
                    raise MemoryReadError('Multiple active player instructions for one position')
                if active:
                    record = active[0]
                    source = 'player'
            role,duty = ROLE_DUTY.get(struct.unpack_from('<Q',record)[0],(None,None))
            positions.append(TacticPosition(i+1,POSITIONS.get(code),code,*players[i],role,duty,source))
        if len({p.position_code for p in positions}) != 11:
            raise MemoryReadError('Duplicate tactic position')
        bench = [TacticSubstitute(i-10,*players[i]) for i in range(11,26)]
        return finish(Tactics(True,selected_slot=selected+1,stored_name=name,style=style,
                              mentality=mentality,positions=positions,substitutes=bench))
