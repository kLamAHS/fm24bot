"""Bounded FM database string entries."""
from .process import MemoryReadError


def direct_string_entry(fm, entry):
    length = fm.read_uint32(entry)
    if not 0 < length <= 512:
        raise MemoryReadError('Invalid string entry length')
    raw = fm.read_bytes(entry + 4, length + 1)
    if raw[-1] != 0:
        raise MemoryReadError('Invalid string terminator')
    value = raw[:-1].decode('utf-8')
    if not value.isprintable():
        raise MemoryReadError('Invalid string contents')
    return value
