"""Read bounded MSVC x64 type metadata; never call game functions."""
from .process import MemoryReadError

def type_info(db,address):
    fm=db.fm; base=db.module.base; end=base+db.module.size
    vtable=fm.read_pointer(address)
    if not base+8<=vtable<end:
        raise MemoryReadError('Object vtable outside the supported FM module')
    locator=fm.read_pointer(vtable-8)
    if not base<=locator<=end-24:
        raise MemoryReadError('Object type locator outside FM module')
    raw=fm.read_bytes(locator,24)
    if int.from_bytes(raw[:4],'little')!=1:
        raise MemoryReadError('Unsupported RTTI locator format')
    descriptor=base+int.from_bytes(raw[12:16],'little')
    if not base<=descriptor<=end-144:
        raise MemoryReadError('Object type descriptor outside FM module')
    name_raw=fm.read_bytes(descriptor+16,128)
    if b'\0' not in name_raw: raise MemoryReadError('Unterminated RTTI type name')
    try: name=name_raw.split(b'\0',1)[0].decode('ascii')
    except UnicodeDecodeError as exc: raise MemoryReadError('Invalid RTTI type name') from exc
    return {'name':name,'offset':int.from_bytes(raw[4:8],'little'),'vtable_rva':vtable-base}

def require_type(db,address,name):
    info=type_info(db,address)
    if info['name']!=name or info['offset']!=0:
        raise MemoryReadError('Unsupported object type for this decoder')
    return info

def require_base_type(db,address,name):
    """Validate a complete object and an offset-zero MSVC base descriptor."""
    import struct
    info=type_info(db,address)
    if info['offset']!=0: raise MemoryReadError('Expected a complete object')
    fm=db.fm;base=db.module.base;end=base+db.module.size
    def relative(rva,size):
        target=base+rva
        if not base<=target<=end-size: raise MemoryReadError('RTTI hierarchy outside FM module')
        return target
    locator=fm.read_pointer(fm.read_pointer(address)-8)
    hierarchy=relative(fm.read_uint32(locator+16),16)
    header=fm.read_bytes(hierarchy,16);count,array_rva=struct.unpack_from('<II',header,8)
    if not 1<=count<=64: raise MemoryReadError('Invalid RTTI base count')
    array=fm.read_bytes(relative(array_rva,count*4),count*4)
    for (rva,) in struct.iter_unpack('<I',array):
        desc=fm.read_bytes(relative(rva,28),28)
        td=relative(struct.unpack_from('<I',desc)[0],144)
        raw=fm.read_bytes(td+16,128)
        if b'\0' not in raw: raise MemoryReadError('Unterminated base type name')
        if raw.split(b'\0',1)[0].decode('ascii')==name:
            member,virtual,inside=struct.unpack_from('<iii',desc,8)
            if member!=0 or virtual!=-1: raise MemoryReadError('Unsupported virtual or shifted base')
            return info
    raise MemoryReadError('Required object base type is absent')
