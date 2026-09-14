import ctypes
import struct
import unittest
from types import SimpleNamespace
from bridge.process import FMProcess,MemoryReadError,MBI,READ_ACCESS
from bridge.signatures import scan,compile_pattern,rip_target
from bridge.pointers import vector

class FakeMemory:
    def __init__(self,data,base=0x10000,unreadable=False):
        self.data=data; self.base=base; self.unreadable=unreadable
    def read_bytes(self,a,n):
        data=self.data[a-self.base:a-self.base+n]
        if len(data)!=n: raise MemoryReadError('Short read')
        return data
    def query(self,a):
        return SimpleNamespace(base=self.base,size=len(self.data),readable=not self.unreadable)
    def read_int32(self,a): return struct.unpack('<i',self.read_bytes(a,4))[0]
    def read_pointer(self,a): return struct.unpack('<Q',self.read_bytes(a,8))[0]

class ReaderTests(unittest.TestCase):
    def test_access_has_no_mutation_rights(self):
        self.assertEqual(READ_ACCESS,0x410)
        self.assertEqual(READ_ACCESS & (0x20|0x8|0x2|0x800),0)
    def test_windows_structure(self):
        self.assertEqual(ctypes.sizeof(MBI),48)
    def test_invalid_ranges(self):
        for a,n in [(0,1),(0x10000,-1),(0x10000,2**30),(0x7FFFFFFFFFFF,2)]:
            with self.assertRaises(MemoryReadError): FMProcess.validate_range(a,n)
    def test_cross_chunk_signature_with_wildcard_newline(self):
        fm=FakeMemory(b'\x00'*7+b'\xAA\x0A\xBB'+b'\0'*8)
        self.assertEqual(scan(fm,fm.base,len(fm.data),'AA ?? BB',chunk_size=8),[fm.base+7])
    def test_overlapping_matches(self):
        fm=FakeMemory(b'\xAA'*4)
        self.assertEqual(scan(fm,fm.base,4,'AA AA',chunk_size=2),[fm.base,fm.base+1,fm.base+2])
    def test_unreadable_skipped(self):
        fm=FakeMemory(b'\xAA'*8,unreadable=True)
        self.assertEqual(scan(fm,fm.base,8,'AA AA',chunk_size=4),[])
    def test_signature_validation(self):
        for s in ['', '?? ??','GG', 'A']:
            with self.assertRaises(ValueError): compile_pattern(s)
    def test_signed_rip_displacement(self):
        fm=FakeMemory(b'\x48\x8D\x0D'+struct.pack('<i',-0x107))
        self.assertEqual(rip_target(fm,fm.base,3,7),fm.base-0x100)
    def test_vector_rejects_misalignment_and_overflow(self):
        for b,e in [(0x20000,0x20009),(0x20000,0x10000),(0x20000,0x20000+8*500001)]:
            with self.assertRaises(MemoryReadError): vector(FakeMemory(struct.pack('<QQ',b,e)),0x10000)

if __name__=='__main__': unittest.main()
