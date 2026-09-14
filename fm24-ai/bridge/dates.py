"""Signature-resolved game date and validated Person birth-date decoding."""
from calendar import isleap
from datetime import date, timedelta
import struct
from .process import MemoryReadError
from .signatures import pe_sections, scan, rip_target

DATE_PATTERN='83 F2 01 8B 05 ?? ?? ?? ?? 66 09'

def ordinal_date(year,ordinal):
    if not 1 <= year <= 9999 or not 1 <= ordinal <= 365+isleap(year):
        raise MemoryReadError('Invalid calendar year or ordinal day')
    return date(year,1,1)+timedelta(days=ordinal-1)

def decode_game_date(raw):
    if len(raw)!=4: raise MemoryReadError('Incomplete game date')
    packed=int.from_bytes(raw,'little')
    # Bits 9..15 encode time separately from the ordinal day.
    return ordinal_date(packed>>16,packed&0x1FF)

def decode_game_time(raw):
    decode_game_date(raw)
    slot=(int.from_bytes(raw,'little')>>9)&0x7F
    # Zero is the observed midnight sentinel; ordinary slots count quarter-hours
    # from 06:00, starting at one. Other encodings remain unsupported.
    if slot>72: raise MemoryReadError('Unvalidated game time encoding')
    minutes=0 if slot==0 else 360+(slot-1)*15
    return f'{minutes//60:02}:{minutes%60:02}'

def decode_birth_date(raw):
    if len(raw)!=4: raise MemoryReadError('Incomplete birth date')
    ordinal,year=struct.unpack('<HH',raw)
    return ordinal_date(year,ordinal)

def age_on(born,as_of):
    if born>as_of: raise MemoryReadError('Birth date is after the game date')
    # FM's non-leap-year birthday convention has not been UI validated.
    if (born.month,born.day)==(2,29) and not isleap(as_of.year) and (as_of.month,as_of.day)==(2,28):
        raise MemoryReadError('Leap-day birthday boundary is not validated')
    return as_of.year-born.year-((as_of.month,as_of.day)<(born.month,born.day))

class GameDate:
    def __init__(self,db):
        self.db=db; self.fm=db.fm; self.address=None; self.hits=[]

    def resolve(self):
        # Database resolution already enforces the exact executable hash.
        self.db.person_pointers()
        module=self.db.module; candidates={}
        for section in pe_sections(self.fm,module):
            if not section.characteristics&0x20000000 or section.characteristics&0x80000000: continue
            for hit in scan(self.fm,section.base,section.size,DATE_PATTERN):
                target=rip_target(self.fm,hit,5,9)
                if not module.base<=target<=module.base+module.size-4: continue
                try: decode_game_date(self.fm.read_bytes(target,4))
                except MemoryReadError: continue
                candidates.setdefault(target,[]).append(hit)
        if len(candidates)!=1:
            raise MemoryReadError(f'Expected one game-date target, found {len(candidates)}')
        self.address,self.hits=next(iter(candidates.items()))
        self.read()
        return self

    def read(self):
        return decode_game_date(self.read_raw())

    def read_raw(self):
        if self.address is None: raise RuntimeError('Resolve game date first')
        # The date global can retain a value without a loaded save.
        self.db.person_pointers()
        raw=self.fm.read_bytes(self.address,4)
        decode_game_date(raw)
        if raw!=self.fm.read_bytes(self.address,4):
            raise MemoryReadError('Game date changed during read')
        return raw

    def evidence(self):
        value=self.read()
        return {'date':value.isoformat(),'address':hex(self.address),
                'target_rva':hex(self.address-self.db.module.base),
                'signature_rvas':[hex(hit-self.db.module.base) for hit in self.hits],
                'raw_hex':self.fm.read_bytes(self.address,4).hex()}
