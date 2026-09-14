"""Windows process reader. Opens handles with QUERY_INFORMATION | VM_READ only.

No privilege changes, process suspension, remote allocation or write APIs.
"""
import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass, asdict
import logging
import os
import struct

log = logging.getLogger(__name__)
READ_ACCESS = 0x0400 | 0x0010
MAX_ADDRESS = 0x7FFFFFFFFFFF

class MemoryReadError(RuntimeError):
    pass

class PROCESSENTRY32W(C.Structure):
    _fields_ = [("dwSize",W.DWORD),("cntUsage",W.DWORD),("th32ProcessID",W.DWORD),
                ("th32DefaultHeapID",C.c_size_t),("th32ModuleID",W.DWORD),
                ("cntThreads",W.DWORD),("th32ParentProcessID",W.DWORD),
                ("pcPriClassBase",W.LONG),("dwFlags",W.DWORD),("szExeFile",W.WCHAR*260)]

class MODULEENTRY32W(C.Structure):
    _fields_ = [("dwSize",W.DWORD),("th32ModuleID",W.DWORD),("th32ProcessID",W.DWORD),
                ("GlblcntUsage",W.DWORD),("ProccntUsage",W.DWORD),("modBaseAddr",C.c_void_p),
                ("modBaseSize",W.DWORD),("hModule",W.HMODULE),
                ("szModule",W.WCHAR*256),("szExePath",W.WCHAR*260)]

class MBI(C.Structure):
    _fields_ = [("BaseAddress",C.c_void_p),("AllocationBase",C.c_void_p),
                ("AllocationProtect",W.DWORD),("PartitionId",W.WORD),
                ("RegionSize",C.c_size_t),("State",W.DWORD),
                ("Protect",W.DWORD),("Type",W.DWORD)]

class MODULEINFO(C.Structure):
    _fields_ = [("lpBaseOfDll",C.c_void_p),("SizeOfImage",W.DWORD),("EntryPoint",C.c_void_p)]

@dataclass(frozen=True)
class Module:
    name: str
    path: str
    base: int
    size: int

@dataclass(frozen=True)
class Region:
    base: int
    size: int
    state: int
    protection: int
    kind: int

    @property
    def readable(self):
        return self.state == 0x1000 and not (self.protection & 0x100) and (self.protection & 0xFF) in (2,4,8,0x20,0x40,0x80)

class FMProcess:
    def __init__(self, pid=None):
        if os.name != "nt" or C.sizeof(C.c_void_p) != 8:
            raise RuntimeError("Requires 64-bit Python on Windows")
        self.pid = pid
        self.handle = None
        self.pointer_size = 8
        self.k = C.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "OpenProcess": ([W.DWORD,W.BOOL,W.DWORD],W.HANDLE),
            "CloseHandle": ([W.HANDLE],W.BOOL),
            "ReadProcessMemory": ([W.HANDLE,C.c_void_p,C.c_void_p,C.c_size_t,C.POINTER(C.c_size_t)],W.BOOL),
            "VirtualQueryEx": ([W.HANDLE,C.c_void_p,C.POINTER(MBI),C.c_size_t],C.c_size_t),
            "CreateToolhelp32Snapshot": ([W.DWORD,W.DWORD],W.HANDLE),
            "Process32FirstW": ([W.HANDLE,C.POINTER(PROCESSENTRY32W)],W.BOOL),
            "Process32NextW": ([W.HANDLE,C.POINTER(PROCESSENTRY32W)],W.BOOL),
            "Module32FirstW": ([W.HANDLE,C.POINTER(MODULEENTRY32W)],W.BOOL),
            "Module32NextW": ([W.HANDLE,C.POINTER(MODULEENTRY32W)],W.BOOL),
            "IsWow64Process2": ([W.HANDLE,C.POINTER(W.WORD),C.POINTER(W.WORD)],W.BOOL),
            "GetExitCodeProcess": ([W.HANDLE,C.POINTER(W.DWORD)],W.BOOL),
        }
        for name,(args,result) in signatures.items():
            fn = getattr(self.k,name)
            fn.argtypes,fn.restype = args,result
        self.psapi=C.WinDLL("psapi",use_last_error=True)
        self.psapi.EnumProcessModulesEx.argtypes=[W.HANDLE,C.POINTER(W.HMODULE),W.DWORD,C.POINTER(W.DWORD),W.DWORD]
        self.psapi.EnumProcessModulesEx.restype=W.BOOL
        self.psapi.GetModuleInformation.argtypes=[W.HANDLE,W.HMODULE,C.POINTER(MODULEINFO),W.DWORD]
        self.psapi.GetModuleInformation.restype=W.BOOL
        self.psapi.GetModuleFileNameExW.argtypes=[W.HANDLE,W.HMODULE,W.LPWSTR,W.DWORD]
        self.psapi.GetModuleFileNameExW.restype=W.DWORD

    def _snapshot(self, flags, pid):
        h = self.k.CreateToolhelp32Snapshot(flags,pid)
        if h == C.c_void_p(-1).value:
            raise C.WinError(C.get_last_error())
        return h

    def processes(self):
        h = self._snapshot(2,0)
        try:
            entry = PROCESSENTRY32W(); entry.dwSize=C.sizeof(entry)
            ok = self.k.Process32FirstW(h,C.byref(entry))
            rows=[]
            while ok:
                rows.append((entry.th32ProcessID,entry.szExeFile))
                ok=self.k.Process32NextW(h,C.byref(entry))
            return rows
        finally:
            self.k.CloseHandle(h)

    def attach(self):
        if self.handle:
            raise RuntimeError("Already attached")
        matches=[pid for pid,name in self.processes() if name.casefold() in ("fm.exe","fm24.exe")]
        if self.pid is None:
            if len(matches)!=1:
                raise RuntimeError(f"Expected one FM process; found {matches}. Specify PID if needed.")
            self.pid=matches[0]
        elif self.pid not in matches:
            raise RuntimeError("Selected PID is not an FM process")
        self.handle=self.k.OpenProcess(READ_ACCESS,False,self.pid)
        if not self.handle:
            raise C.WinError(C.get_last_error())
        try:
            proc,native=W.WORD(),W.WORD()
            if not self.k.IsWow64Process2(self.handle,C.byref(proc),C.byref(native)):
                raise C.WinError(C.get_last_error())
            if proc.value or native.value != 0x8664:
                raise RuntimeError("This build supports native x64 FM only")
            log.info("Attached PID %s with access mask %#x",self.pid,READ_ACCESS)
        except Exception:
            self.close(); raise
        return self

    def close(self):
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle=None

    def __enter__(self):
        return self.attach()

    def __exit__(self,*args):
        self.close()

    def alive(self):
        code=W.DWORD()
        return bool(self.handle and self.k.GetExitCodeProcess(self.handle,C.byref(code)) and code.value==259)

    def modules(self):
        if not self.handle:
            raise RuntimeError("Not attached")
        count=256
        while True:
            handles=(W.HMODULE*count)(); needed=W.DWORD()
            if not self.psapi.EnumProcessModulesEx(self.handle,handles,C.sizeof(handles),C.byref(needed),3):
                raise C.WinError(C.get_last_error())
            if needed.value<=C.sizeof(handles): break
            count=needed.value//C.sizeof(W.HMODULE)+16
            if count>16384: raise MemoryReadError("Invalid module count")
        rows=[]
        for h in handles[:needed.value//C.sizeof(W.HMODULE)]:
            info=MODULEINFO(); path=C.create_unicode_buffer(32768)
            if not self.psapi.GetModuleInformation(self.handle,h,C.byref(info),C.sizeof(info)):
                raise C.WinError(C.get_last_error())
            if not self.psapi.GetModuleFileNameExW(self.handle,h,path,len(path)):
                raise C.WinError(C.get_last_error())
            rows.append(Module(os.path.basename(path.value),path.value,info.lpBaseOfDll,info.SizeOfImage))
        return rows

    @staticmethod
    def validate_range(address,size):
        if not isinstance(address,int) or not isinstance(size,int) or size<0 or size>64*1024*1024 or address<0x10000 or address+size>MAX_ADDRESS+1:
            raise MemoryReadError(f"Invalid read range: {address!r}, {size!r}")

    def query(self,address):
        if not self.handle:
            raise RuntimeError("Not attached")
        self.validate_range(address,1)
        mbi=MBI()
        if not self.k.VirtualQueryEx(self.handle,address,C.byref(mbi),C.sizeof(mbi)):
            raise MemoryReadError(f"VirtualQueryEx {address:#x}: {C.WinError(C.get_last_error())}")
        return Region(mbi.BaseAddress,mbi.RegionSize,mbi.State,mbi.Protect,mbi.Type)

    def regions(self,start=0x10000,end=MAX_ADDRESS):
        addr=start
        while addr<end:
            region=self.query(addr)
            yield region
            nxt=region.base+region.size
            if nxt<=addr: raise MemoryReadError("Invalid region size")
            addr=nxt

    def read_bytes(self,address,size):
        if not self.handle:
            raise RuntimeError("Not attached")
        self.validate_range(address,size)
        if not size: return b""
        buf=C.create_string_buffer(size); actual=C.c_size_t()
        ok=self.k.ReadProcessMemory(self.handle,address,buf,size,C.byref(actual))
        if not ok or actual.value!=size:
            raise MemoryReadError(f"Read {address:#x}+{size:#x}: got {actual.value}; {C.WinError(C.get_last_error())}")
        return buf.raw

    def _scalar(self,address,fmt):
        return struct.unpack(fmt,self.read_bytes(address,struct.calcsize(fmt)))[0]

    def read_int32(self,a): return self._scalar(a,"<i")
    def read_uint32(self,a): return self._scalar(a,"<I")
    def read_int64(self,a): return self._scalar(a,"<q")
    def read_uint64(self,a): return self._scalar(a,"<Q")
    def read_float(self,a): return self._scalar(a,"<f")
    def read_double(self,a): return self._scalar(a,"<d")
    def read_pointer(self,a): return self.read_uint64(a)
    def read_uint16(self,a): return self._scalar(a,"<H")

    def read_string(self,address,max_bytes=512,encoding="utf-8"):
        if encoding not in ("utf-8","utf-16-le") or not 0<max_bytes<=65536:
            raise ValueError("Bounded UTF-8 or UTF-16-LE string required")
        unit=2 if encoding=="utf-16-le" else 1
        output=bytearray()
        while len(output)<max_bytes:
            a=address+len(output)
            r=self.query(a)
            if not r.readable: raise MemoryReadError(f"Unreadable string at {a:#x}")
            n=min(64,max_bytes-len(output),r.base+r.size-a)
            data=self.read_bytes(a,n)
            output.extend(data)
            for i in range(0,len(output)-unit+1,unit):
                if output[i:i+unit]==b"\0"*unit:
                    return output[:i].decode(encoding,errors="strict")
        raise MemoryReadError("String has no terminator within limit")
