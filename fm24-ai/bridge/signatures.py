"""Bounded, overlapping AOB scanning of readable module sections."""
import re
import struct
from dataclasses import dataclass
from .process import MemoryReadError

@dataclass(frozen=True)
class Section:
    name: str
    base: int
    size: int
    characteristics: int

def pe_sections(fm,module):
    if fm.read_bytes(module.base,2)!=b'MZ': raise MemoryReadError('Missing MZ header')
    pe=module.base+fm.read_uint32(module.base+0x3C)
    header=fm.read_bytes(pe,24)
    if header[:4]!=b'PE\0\0': raise MemoryReadError('Missing PE header')
    machine,count=struct.unpack_from('<HH',header,4)
    opt=struct.unpack_from('<H',header,20)[0]
    if machine!=0x8664 or not 0<count<96: raise MemoryReadError('Unsupported PE header')
    raw=fm.read_bytes(pe+24+opt,count*40)
    result=[]
    for i in range(count):
        row=raw[i*40:(i+1)*40]
        name=row[:8].rstrip(b'\0').decode('ascii')
        size,rva=struct.unpack_from('<II',row,8)
        flags=struct.unpack_from('<I',row,36)[0]
        if rva+size>module.size: raise MemoryReadError('Section exceeds image')
        result.append(Section(name,module.base+rva,size,flags))
    return result

def compile_pattern(pattern):
    parts=pattern.split()
    if not parts or all(p in ('?','??') for p in parts):
        raise ValueError('Pattern requires at least one fixed byte')
    out=b''
    for p in parts:
        if p in ('?','??'): out+=b'.'
        elif re.fullmatch('[0-9a-fA-F]{2}',p): out+=re.escape(bytes([int(p,16)]))
        else: raise ValueError(f'Invalid pattern token {p}')
    return re.compile(b'(?=('+out+b'))',re.DOTALL),len(parts)

def scan(fm,start,size,pattern,chunk_size=2*1024*1024,max_hits=20000):
    regex,width=compile_pattern(pattern)
    if chunk_size<width: raise ValueError('Chunk smaller than signature')
    end=start+size; cursor=start; tail=b''; hits=[]
    while cursor<end:
        region=fm.query(cursor)
        count=min(chunk_size,end-cursor,region.base+region.size-cursor)
        if not region.readable:
            tail=b''; cursor+=count; continue
        # A failed read aborts resolution rather than silently returning a partial scan.
        data=fm.read_bytes(cursor,count)
        merged=tail+data; origin=cursor-len(tail)
        hits.extend(origin+m.start() for m in regex.finditer(merged))
        if len(hits)>max_hits: raise MemoryReadError('Signature is too broad')
        tail=merged[-(width-1):] if width>1 else b''
        cursor+=count
    return sorted(set(hits))

def rip_target(fm,instruction,displacement_offset,instruction_length):
    return instruction+instruction_length+fm.read_int32(instruction+displacement_offset)
