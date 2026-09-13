"""Candidate decoding for 24.4.2. Unknown fields are not fabricated."""
import struct
from dataclasses import asdict
from .process import MemoryReadError
from structures.player import Player
from .readiness import decode_readiness, READINESS_OFFSET, READINESS_SIZE

# Facts from attributed research; confidence is recorded per field in research/offsets.md.
ATTRIBUTES={
    'acceleration':0x22,'pace':0x26,'passing':0x07,'finishing':0x02,
    'technique':0x17,'decisions':0x12,'vision':0x0A,'work_rate':0x1D,'strength':0x24,
}

def name_entry(fm,pointer):
    if not pointer: return ''
    # This Windows build uses a wrapper whose first pointer addresses a
    # uint32 byte-length followed by UTF-8 bytes and a NUL terminator.
    entry=fm.read_pointer(pointer)
    length=fm.read_uint32(entry)
    if length>255: raise MemoryReadError('Name entry length exceeds limit')
    raw=fm.read_bytes(entry+4,length+1)
    if raw[-1]!=0: raise MemoryReadError('Name entry lacks expected terminator')
    value=raw[:-1].decode('utf-8',errors='strict')
    if value and (not value.isprintable() or len(value)>100):
        raise MemoryReadError('Invalid name entry')
    return value

def identity(fm,person):
    data=fm.read_bytes(person,0x78)
    uid=struct.unpack_from('<I',data,0xC)[0]
    first,last,common=struct.unpack_from('<QQQ',data,0x58)
    first=name_entry(fm,first); last=name_entry(fm,last); common=name_entry(fm,common)
    name=common or (first+' '+last).strip()
    if not name or not 0<uid<0x80000000: raise MemoryReadError('Invalid player identity')
    return uid,name,first,last

def decode_player(db,person,with_evidence=False):
    fm=db.fm
    offset=db.type_offset(person)
    if offset!=0x278: raise MemoryReadError(f'Unvalidated player type offset {offset:#x}')
    uid,name,first,last=identity(fm,person)
    base=person-offset
    readiness_raw=fm.read_bytes(base+READINESS_OFFSET,READINESS_SIZE)
    readiness=decode_readiness(readiness_raw)
    block=fm.read_bytes(base+0x217,54)
    raw={k:block[v] for k,v in ATTRIBUTES.items()}
    if any(v<1 or v>100 for v in raw.values()): raise MemoryReadError('Attribute byte outside candidate range')
    attributes={k:(v+2)//5 for k,v in raw.items()}
    if any(v<1 or v>20 for v in attributes.values()): raise MemoryReadError('Invalid display attribute')
    if uid!=fm.read_uint32(person+0xC) or block!=fm.read_bytes(base+0x217,54) or readiness_raw!=fm.read_bytes(base+READINESS_OFFSET,READINESS_SIZE):
        raise MemoryReadError('Player changed during read')
    player=Player(uid,name,first,last,attributes,readiness['positions'],readiness['position_ratings'],readiness['condition'],readiness['match_sharpness'])
    if not with_evidence: return player
    return {'player':asdict(player),'evidence':{'person':hex(person),'player_base':hex(base),'uid_address':hex(person+0xC),'attribute_block':hex(base+0x217),'raw_attributes':raw,'block_hex':block.hex(),'birth_day_raw':fm.read_uint16(person+0x44),'birth_year_raw':fm.read_uint16(person+0x46),'readiness_address':hex(base+READINESS_OFFSET),'readiness_hex':readiness_raw.hex(),**readiness['evidence']}}

def find_players(db,query):
    found=[]; errors=0
    for person in db.person_pointers():
        try:
            if db.type_offset(person)!=0x278: continue
            uid,name,first,last=identity(db.fm,person)
            if str(uid)==query or query.casefold() in name.casefold():
                found.append(decode_player(db,person,True))
        except (MemoryReadError,UnicodeDecodeError):
            errors+=1
    return {'query':query,'matches':found,'undecodable_records':errors}
