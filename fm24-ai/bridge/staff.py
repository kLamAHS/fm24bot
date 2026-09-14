"""Club-owned staff lists with registry, type, contract and repeat-read guards."""
import struct
from .process import MemoryReadError
from .rtti import require_type, type_info
from .players import identity
from .nations import read_primary_nationality
from .contracts import decode_contract_terms, CONTRACT_SIZE
from structures.contract import Contract
from structures.staff import Staff

STAFF_TYPES = {'.?AVACTUAL_NON_PLAYER@db@@': 0xf8,
               '.?AVHUMAN_NON_PLAYER@db@@': 0x450}
# Labels promoted after comparison with multiple staff. Unknown codes remain
# explicitly null; a lone similar-looking value is not sufficient evidence.
JOBS = {2:'Coach', 6:'Director', 12:'Physio', 14:'Scout', 16:'Head Coach',20:'Assistant Coach',
        26:'Fitness Coach', 34:'Goalkeeping Coach', 38:'Chief Doctor',
        40:'Head of Sports Science', 44:'Chief Scout', 48:'Sports Scientist',
        50:'Head Physio', 58:'Recruitment Analyst',
        60:'Performance Analyst',62:'Head Performance Analyst', 88:'Technical Director'}


def read_staff(context):
    context.check()
    db, fm, club = context.db, context.fm, context.club
    db.current_date()
    game_raw = db.dates.read_raw()
    known = set(db.person_pointers())
    require_type(db, club, '.?AVCLUB@db@@')
    guards = []

    def remember(address, size):
        raw = fm.read_bytes(address, size)
        guards.append((address, raw))
        return raw

    def pointer(address):
        return struct.unpack('<Q', remember(address, 8))[0]

    def members(address):
        raw = remember(address, 16)
        begin, end = struct.unpack('<QQ', raw)
        if begin == end == 0:
            return []
        if not 0x10000 <= begin <= end < 0x7fffffff0000 or begin % 8 or (end-begin) % 8 or end-begin > 512*8:
            raise MemoryReadError('Invalid staff vector bounds')
        data = remember(begin, end-begin) if end != begin else b''
        result = [x[0] for x in struct.iter_unpack('<Q', data)]
        if any(not x for x in result) or len(set(result)) != len(result):
            raise MemoryReadError('Null or duplicate staff vector entry')
        return result

    groups = {name:members(club+off) for name,off in
              [('medical',0x60),('coaching',0x78),('recruitment',0x90)]}
    board = pointer(club+0x110)
    groups['board'] = members(board) if board else []
    # Team managers may also occur in the coaching vector. Deduplicate the
    # same person across lists, while rejecting duplicates within each list.
    teams = members(club+0x18)
    for team in teams:
        require_type(db, team, '.?AVTEAM@db@@')
        if pointer(team+0x30) != club:
            raise MemoryReadError('Staff team belongs to a different club')
        head = pointer(team+0x80)
        if head and head not in groups['coaching']:
            groups['coaching'].append(head)
    by_pointer = {}
    for department, entries in groups.items():
        for address in entries:
            by_pointer.setdefault(address, []).append(department)
    result = []
    for address, departments in by_pointer.items():
        info = type_info(db,address)
        offset = STAFF_TYPES.get(info['name'])
        if info['offset'] != 0 or offset is None:
            raise MemoryReadError('Unsupported staff object type')
        person = address+offset
        if person not in known or db.type_offset(person) != offset:
            raise MemoryReadError('Staff person is outside the current registry')
        remember(person,0x78)
        uid, name, _, _ = identity(fm,person)
        contract = pointer(person+0xc8)
        ci = type_info(db,contract)
        if ci['offset'] != 0 or ci['name'] not in ('.?AVBASIC_CONTRACT@db@@','.?AVFULL_CONTRACT@db@@'):
            raise MemoryReadError('Unsupported staff contract class')
        raw = remember(contract, CONTRACT_SIZE if ci['name']=='.?AVFULL_CONTRACT@db@@' else 0x20)
        owner, team = struct.unpack_from('<QQ',raw,8)
        if owner != person or team not in teams:
            raise MemoryReadError('Staff contract ownership mismatch')
        team_id = struct.unpack('<I',remember(team+0xc,4))[0]
        if not 0 < team_id < 0x80000000:
            raise MemoryReadError('Invalid staff team identity')
        job_code = struct.unpack_from('<H',raw,0x1c)[0]
        employment = None
        if ci['name']=='.?AVFULL_CONTRACT@db@@':
            employment = Contract('employment',context.club_id,context.club_name,team_id,
                                  **decode_contract_terms(raw),wage_basis='salary')
        nation = read_primary_nationality(db,person)
        result.append(Staff(uid,name,nation,team_id,team==context.team,
                            sorted(departments),JOBS.get(job_code),job_code,employment))
    if len({s.id for s in result}) != len(result):
        raise MemoryReadError('Duplicate staff identity')
    for address,raw in guards:
        if fm.read_bytes(address,len(raw)) != raw:
            raise MemoryReadError('Staff changed during observation')
    if game_raw != db.dates.read_raw() or set(db.person_pointers()) != known:
        raise MemoryReadError('Staff registry or game time changed')
    context.check()
    return sorted(result,key=lambda s:s.id)
