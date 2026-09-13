"""Experimental, fail-closed FM24 database resolver. See research/sources.md."""
import logging
import struct
from .process import MemoryReadError
from .signatures import pe_sections,scan,rip_target
from .pointers import vector
from .profile import require_supported_build

log=logging.getLogger(__name__)
DB_PATTERN='48 8D 0D ?? ?? ?? ?? 48 8D 15'

class Database:
    def __init__(self,fm):
        self.fm=fm
        self.module=next(m for m in fm.modules() if m.name.casefold() in ('fm.exe','fm24.exe'))
        self.root=None
        self.registry=None
        self.dates=None

    def resolve(self):
        self.dates=None
        require_supported_build(self.module)
        fm=self.fm
        matches=[]
        # The executable's principal code section has a nonstandard name.
        for s in pe_sections(fm,self.module):
            if s.characteristics & 0x20000000 and not s.characteristics & 0x80000000:
                log.info('Scanning executable section %s (%s bytes)',s.name,s.size)
                matches+=scan(fm,s.base,s.size,DB_PATTERN)
        log.info('Database signature: %s candidates',len(matches))
        valid={}
        for hit in matches:
            try:
                root=rip_target(fm,hit,3,7)
                if not self.module.base<=root<self.module.base+self.module.size: continue
                db=fm.read_pointer(root+0x68)
                registry=fm.read_pointer(db+0x80)
                begin,end=vector(fm,registry)
                n=(end-begin)//8
                if n<1000: continue
                pointers=struct.unpack('<16Q',fm.read_bytes(begin,128))
                scores=0
                for p in pointers:
                    vt=fm.read_pointer(p)
                    uid=fm.read_uint32(p+0xC)
                    if self.module.base<=vt<self.module.base+self.module.size and 0<uid<0x80000000 and self.type_offset(p) in (0xF8,0x278,0x138):
                        scores+=1
                if scores<14: continue
                valid[root]=(hit,registry,begin,end)
                log.info('Candidate root=%#x hit=%#x registry=%#x count=%s sample=%s',root,hit,registry,n,[fm.read_uint32(p+0xC) for p in pointers[:4]])
            except MemoryReadError:
                continue
        if len(valid)!=1:
            raise MemoryReadError(f'Expected one validated database candidate, found {len(valid)}. Load a save or investigate build compatibility.')
        self.root,(self.signature,self.registry,begin,end)=next(iter(valid.items()))
        log.info('Registry root=%#x, %s people',self.root,(end-begin)//8)
        return self

    def person_pointers(self):
        if self.registry is None: raise RuntimeError('Resolve database first')
        fm=self.fm
        if fm.read_pointer(fm.read_pointer(self.root+0x68)+0x80)!=self.registry:
            raise MemoryReadError('Save/database changed; resolve again')
        begin,end=vector(fm,self.registry)
        data=fm.read_bytes(begin,end-begin)
        if (begin,end)!=vector(fm,self.registry) or data!=fm.read_bytes(begin,end-begin) or fm.read_pointer(fm.read_pointer(self.root+0x68)+0x80)!=self.registry:
            raise MemoryReadError('Registry changed during read; retry when game is idle')
        return [p[0] for p in struct.iter_unpack('<Q',data) if p[0]]

    def type_offset(self,person):
        fm=self.fm; base=self.module.base; end=base+self.module.size
        vt=fm.read_pointer(person)
        if not base+8<=vt<end: raise MemoryReadError('Person vtable outside FM module')
        locator=fm.read_pointer(vt-8)
        if not base<=locator<end-24: raise MemoryReadError('Type locator outside FM module')
        return fm.read_uint32(locator+4)

    def current_date(self):
        if self.dates is None:
            from .dates import GameDate
            self.dates=GameDate(self).resolve()
        return self.dates.read()

    def info(self):
        b,e=vector(self.fm,self.registry)
        return {'module_base':hex(self.module.base),'signature':hex(self.signature),'signature_rva':hex(self.signature-self.module.base),'root':hex(self.root),'root_rva':hex(self.root-self.module.base),'registry':hex(self.registry),'person_count':(e-b)//8}
