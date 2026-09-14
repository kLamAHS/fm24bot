"""Current employment and loan agreements, excluding unvalidated clauses.

The loan pointer on Person addresses a holder, not the loan object itself.
Wages are native weekly GBP and can differ from FM's rounded UI presentation.
"""
import struct
from .dates import decode_game_date
from .process import MemoryReadError
from .rtti import require_type
from .strings import direct_string_entry
from structures.contract import Contract

CONTRACT_TYPES = {'employment': '.?AVFULL_CONTRACT@db@@',
                  'loan': '.?AVLOAN_CONTRACT@db@@'}
CONTRACT_SIZE = 0x44


def decode_contract_terms(raw):
    if len(raw) != CONTRACT_SIZE:
        raise MemoryReadError('Incomplete contract terms')
    wage = struct.unpack_from('<i', raw, 0x18)[0]
    if wage < 0:
        raise MemoryReadError('Unvalidated contract wage sentinel')
    start = decode_game_date(raw[0x3c:0x40])
    end = decode_game_date(raw[0x40:0x44])
    if end < start:
        raise MemoryReadError('Contract ends before it starts')
    return {'start_date':start.isoformat(), 'end_date':end.isoformat(),
            'weekly_wage_gbp':wage}


def read_contracts(db, person):
    fm = db.fm
    links = fm.read_bytes(person + 0xc8, 16)
    uid = fm.read_uint32(person + 0xc)
    agreements = []
    guards = []
    for kind, holder in zip(CONTRACT_TYPES, struct.unpack('<QQ', links)):
        if not holder:
            continue
        contract = fm.read_pointer(holder) if kind == 'loan' else holder
        require_type(db, contract, CONTRACT_TYPES[kind])
        raw = fm.read_bytes(contract, CONTRACT_SIZE)
        owner, team = struct.unpack_from('<QQ', raw, 8)
        if owner != person:
            raise MemoryReadError('Contract belongs to a different person')
        require_type(db, team, '.?AVTEAM@db@@')
        team_id = fm.read_uint32(team + 0xc)
        club = fm.read_pointer(team + 0x30)
        require_type(db, club, '.?AVCLUB@db@@')
        club_id = fm.read_uint32(club + 0xc)
        name_entry = fm.read_pointer(club + 0xc0)
        name = direct_string_entry(fm, name_entry)
        if not 0 < club_id < 0x80000000 or not 0 < team_id < 0x80000000:
            raise MemoryReadError('Invalid contract club or team identity')
        terms = decode_contract_terms(raw)
        agreements.append(Contract(kind, club_id, name, team_id, **terms,
                                   wage_basis='loan_contribution' if kind == 'loan' else 'salary'))
        guards.append((kind, holder, contract, raw, team, team_id, club, club_id, name_entry))
    # Repeat each agreement after reading the other one to prevent mixing two
    # owners, two loans or two versions of a player's contract in one response.
    for kind, holder, contract, raw, team, team_id, club, club_id, name_entry in guards:
        if (fm.read_bytes(contract, CONTRACT_SIZE) != raw or
                fm.read_pointer(team + 0x30) != club or
                fm.read_uint32(team + 0xc) != team_id or
                fm.read_uint32(club + 0xc) != club_id or
                fm.read_pointer(club + 0xc0) != name_entry or
                (kind == 'loan' and fm.read_pointer(holder) != contract)):
            raise MemoryReadError('Contract changed while reading player')
    if links != fm.read_bytes(person + 0xc8, 16) or uid != fm.read_uint32(person + 0xc):
        raise MemoryReadError('Player contract links changed during observation')
    return agreements
