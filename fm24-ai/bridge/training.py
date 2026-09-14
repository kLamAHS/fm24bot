"""Owned, committed team training schedule and individual training settings.

No UI edit-session state is read. The stored week name does not include UI
category prefixes or its Modified suffix. Unknown session labels stay null.
"""
import struct
from datetime import timedelta
from .dates import decode_game_date
from .process import MemoryReadError
from .rtti import require_type
from .signatures import pe_sections, scan, rip_target
from .strings import direct_string_entry
from .players import identity
from structures.training import Training, TrainingWeek, TrainingDay, TrainingSession, TrainingProgram

ROOT_PATTERN = ('48 8B 0D ?? ?? ?? ?? 4C 89 FA E8 ?? ?? ?? ?? 48 85 C0 74 20 '
                '48 8B 48 08 48 63 51 04 48 8D 4C 10 08')
# UI transcriptions from the February calendar. Wider label coverage is tracked
# in research/training.md; unobserved codes never become inferred labels.
SESSIONS = {0:'Overall',1:'Outfield',2:'Attacking',3:'Possession',4:'Defending',
            6:'Physical',7:'Endurance',9:'Quickness',10:'Recovery',11:'Rest',
            12:'Travel',14:'Match',100:'Match Tactics',101:'Match Practice',
            103:'Match Focus',202:'Attacking Direct',203:'Defending Engaged',
            204:'Defending Disengaged',205:'Defending Wide',206:'Attacking Shadow Play',
            207:'Defensive Shadow Play',209:'Chance Creation',210:'Ball Retention',
            211:'Ball Distribution',212:'Transition - Press',213:'Transition - Restrict',
            214:'Ground Defense',215:'Aerial Defense',221:'Defending from the Front',
            222:'Play from the Back',301:'Routines'}


class TrainingReader:
    def __init__(self, db):
        self.db, self.fm = db, db.fm
        self.global_ptr = None

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
                    require_type(self.db,self.fm.read_pointer(target),'.?AVTRAINING_MANAGER@@')
                    candidates.add(target)
                except MemoryReadError:
                    continue
        if len(candidates) != 1:
            raise MemoryReadError('Expected one supported training manager')
        self.global_ptr = candidates.pop()
        return self

    def read(self, context):
        context.check()
        db,fm = self.db,self.fm
        known = set(db.person_pointers())
        today = db.current_date()
        clock = db.dates.read_raw()
        guards = []

        def observed(address,size):
            raw = fm.read_bytes(address,size)
            guards.append((address,raw))
            return raw

        def pointer(address):
            return struct.unpack('<Q',observed(address,8))[0]

        def pointers(address,maximum):
            start,end = struct.unpack('<QQ',observed(address,16))
            if start == end == 0:
                return []
            if not 0x10000 <= start <= end < 0x7fffffff0000 or start%8 or (end-start)%8 or end-start > maximum*8:
                raise MemoryReadError('Invalid training vector')
            data = observed(start,end-start) if end>start else b''
            values = [x[0] for x in struct.iter_unpack('<Q',data)]
            if len(values) != len(set(values)) or any(x < 0x10000 or x%8 for x in values):
                raise MemoryReadError('Invalid or duplicate training object')
            return values

        def nodes(address,maximum,size):
            head,count = struct.unpack('<QQ',observed(address,16))
            if count > maximum or head < 0x10000 or head%8:
                raise MemoryReadError('Invalid training tree')
            stack = [pointer(head+8)]
            seen = set()
            rows = []
            while stack:
                node = stack.pop()
                if node == head:
                    continue
                if node in seen or len(seen) >= count or node < 0x10000 or node%8:
                    raise MemoryReadError('Cyclic or oversized training tree')
                seen.add(node)
                raw = observed(node,size)
                if raw[0x19]:
                    raise MemoryReadError('Unexpected training tree sentinel')
                stack.extend(struct.unpack_from('<Q',raw,off)[0] for off in (0,0x10))
                rows.append((node,raw))
            if len(rows) != count:
                raise MemoryReadError('Training tree count changed')
            return rows

        def finish(result):
            for address,raw in guards:
                if fm.read_bytes(address,len(raw)) != raw:
                    raise MemoryReadError('Training changed during observation')
            if db.dates.read_raw() != clock or set(db.person_pointers()) != known:
                raise MemoryReadError('Training registry or game time changed')
            context.check()
            return result

        root = pointer(self.global_ptr)
        require_type(db,root,'.?AVTRAINING_MANAGER@@')
        owner = context.staff + 0x450
        # HUMAN Person is the virtual base at +450 on this supported build.
        if owner not in known or db.type_offset(owner) != 0x450:
            raise MemoryReadError('Unsupported human training owner')
        matches = []
        for record in pointers(root+0x20,32):
            person,club = struct.unpack('<QQ',observed(record+0x68,16))
            if person == owner and club == context.club:
                matches.append(record)
        if len(matches) != 1:
            raise MemoryReadError('Expected one training record for the current human and club')
        record = matches[0]
        teams = [(node,raw) for node,raw in nodes(record+0x40,512,0x28)
                 if struct.unpack_from('<Q',raw,0x20)[0] == context.team]
        if len(teams) != 1:
            raise MemoryReadError('Expected one training record for the current team')
        weeks = []
        starts = set()
        for week in pointers(teams[0][0]+0x48,256):
            raw = observed(week,0x60)
            start = decode_game_date(raw[0x50:0x54])
            if start.weekday() != 0 or start in starts or abs((start-today).days)>1098:
                raise MemoryReadError('Invalid or duplicate training week')
            starts.add(start)
            name_ptr = struct.unpack_from('<Q',raw,0x48)[0]
            name = direct_string_entry(fm,name_ptr) if name_ptr else None
            days = []
            for day in range(7):
                sessions = []
                for slot,code in enumerate(struct.unpack_from('<3H',raw,day*10+4),1):
                    label = SESSIONS.get(code)
                    kind = {11:'rest',12:'travel',14:'match'}.get(code,'training' if label else 'unknown')
                    sessions.append(TrainingSession(slot,label,kind,'decoded' if label else 'not_decoded'))
                days.append(TrainingDay((start+timedelta(days=day)).isoformat(),sessions))
            weeks.append(TrainingWeek(start.isoformat(),name,days))
        weeks.sort(key=lambda week:week.start_date)

        squad = pointers(context.team+0x38,512)
        persons = [player+0x278 for player in squad]
        selected = {}
        for person in persons:
            if person not in known or db.type_offset(person) != 0x278:
                raise MemoryReadError('Unregistered training player')
            index = struct.unpack('<I',observed(person+8,4))[0]
            if index in selected:
                raise MemoryReadError('Duplicate training player index')
            selected[index] = person
        programs = []
        for index,person in selected.items():
            uid,name,*_ = identity(fm,person)
            player = person-0x278
            focus_ptr = pointer(player+0x170)
            if focus_ptr:
                code = observed(focus_ptr,10)[6]
                focus = {9:'Quickness',23:'GK Technique'}.get(code)
                focus_status = 'decoded' if focus else 'not_decoded'
            else:
                focus,focus_status = None,'none'
            setting = observed(player+0x1c2,1)[0]
            intensity = {255:'Automatic',2:'Double Intensity'}.get(setting)
            programs.append(TrainingProgram(uid,name,focus,focus_status,intensity,
                                            'decoded' if intensity else 'not_decoded'))
        programs.sort(key=lambda program:program.player_id)
        monday = today-timedelta(days=today.weekday())
        return finish(Training(True,weeks=weeks,individual_programs=programs,
                               current_week_start=monday.isoformat() if monday in starts else None))
