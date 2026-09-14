"""Primary nationality; secondary nationalities are not inferred."""
import struct
from .process import MemoryReadError
from .rtti import require_type
from .strings import direct_string_entry
from structures.nation import Nation


def read_primary_nationality(db, person):
    fm = db.fm
    nation = fm.read_pointer(person + 0x70)
    require_type(db, nation, '.?AVNATION@db@@')
    raw = fm.read_bytes(nation, 0x20)
    uid = struct.unpack_from('<I', raw, 0x0C)[0]
    name_entry = struct.unpack_from('<Q', raw, 0x18)[0]
    if not 0 < uid < 0x80000000:
        raise MemoryReadError('Invalid nation identity')
    name = direct_string_entry(fm, name_entry)
    if fm.read_pointer(person + 0x70) != nation or fm.read_bytes(nation, 0x20) != raw:
        raise MemoryReadError('Nationality changed while reading player')
    return Nation(uid, name)
