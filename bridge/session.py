"""Address-free public bridge and guarded per-session ID index."""
from .process import FMProcess,MemoryReadError
from .database import Database
from .players import decode_player
from .club import CurrentClub
from uuid import uuid4

class FMBridge:
    def __init__(self,pid=None):
        self.fm=FMProcess(pid); self.db=None; self.context=None; self.index={}; self.session_id=None

    def attach(self):
        self.fm.attach()
        try:
            self.db=Database(self.fm)
            self.db.resolve()
            self.refresh_index()
            self.session_id=str(uuid4())
        except Exception:
            self.close(); raise
        return self

    def refresh_index(self):
        index={}
        for p in self.db.person_pointers():
            if self.db.type_offset(p)==0x278:
                uid=self.fm.read_uint32(p+12)
                if uid in index: raise MemoryReadError('Duplicate player UID in registry')
                index[uid]=p
        self.index=index

    def player(self,unique_id):
        # Registry membership is rechecked; this is a bounded vector read,
        # never an address-space scan, and avoids stale pointers after reload.
        known=set(self.db.person_pointers())
        p=self.index.get(unique_id)
        if p not in known or (p and self.fm.read_uint32(p+12)!=unique_id):
            self.refresh_index(); p=self.index.get(unique_id)
        if p is None: raise KeyError(unique_id)
        result=decode_player(self.db,p)
        if result.id!=unique_id: raise MemoryReadError('Player ID changed during read')
        return result

    def players(self):
        """Strict bulk decode; raises if any indexed player has unvalidated data."""
        self.refresh_index()
        return [decode_player(self.db,p) for p in self.index.values()]

    def player_ids(self):
        """Enumerate supported player-type identities without decoding attributes."""
        self.refresh_index()
        return sorted(self.index)

    def _context(self):
        self.db.person_pointers()
        if self.context is None: self.context=CurrentClub(self.db).resolve()
        self.context.check()
        return self.context

    def current_club(self): return self._context().model()
    def manager(self): return self._context().manager
    def squad(self): return self._context().squad()

    def close(self):
        self.fm.close(); self.db=None; self.context=None; self.index={}; self.session_id=None

    def __enter__(self): return self.attach()
    def __exit__(self,*args): self.close()
