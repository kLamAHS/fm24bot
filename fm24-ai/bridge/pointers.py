from .process import MemoryReadError

def follow(fm,address,offsets):
    """For each offset: dereference the current address, then add offset."""
    for offset in offsets:
        address=fm.read_pointer(address)
        if not address: raise MemoryReadError('Null pointer in chain')
        address+=offset
        fm.validate_range(address,1)
    return address

def vector(fm,address,max_count=500000):
    begin=fm.read_pointer(address); end=fm.read_pointer(address+8)
    if begin<0x10000 or end<begin or (end-begin)%8 or (end-begin)//8>max_count:
        raise MemoryReadError('Invalid pointer vector bounds')
    return begin,end
