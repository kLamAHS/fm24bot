"""Address-free public bridge and guarded per-session ID index."""
from .process import FMProcess,MemoryReadError
from .database import Database
from .players import decode_player
from .club import CurrentClub
from uuid import uuid4
from structures.game import Game

class FMBridge:
    def __init__(self,pid=None):
        self.fm=FMProcess(pid); self.db=None; self.context=None; self.index={}; self.session_id=None; self.fixture_reader=None; self.match_reader=None; self.tactics_reader=None; self.training_reader=None

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
        as_of=self.db.current_date()
        result=[decode_player(self.db,p,as_of=as_of) for p in self.index.values()]
        if self.db.current_date()!=as_of: raise MemoryReadError('Game date changed while reading players')
        return result

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
    def game(self):
        from .dates import decode_game_date,decode_game_time
        self.db.current_date();raw=self.db.dates.read_raw()
        return Game(date=decode_game_date(raw).isoformat(),time=decode_game_time(raw))
    def manager(self): return self._context().manager
    def squad(self): return self._context().squad()
    def finances(self):
        from .finances import read_finances
        return read_finances(self._context())

    def staff(self):
        from .staff import read_staff
        return read_staff(self._context())

    def inbox(self):
        from .inbox import read_inbox
        return read_inbox(self._context())

    def scouting(self):
        from .scouting import read_scouting
        return read_scouting(self._context())

    def shortlists(self):
        from .scouting import read_shortlists
        return read_shortlists(self._context())

    def transfer_targets(self):
        from .scouting import read_transfer_targets
        return read_transfer_targets(self._context())

    def tactics(self):
        from .tactics import TacticsReader
        context=self._context()
        if self.tactics_reader is None:self.tactics_reader=TacticsReader(self.db).resolve()
        return self.tactics_reader.read(context)

    def training(self):
        from .training import TrainingReader
        context=self._context()
        if self.training_reader is None:self.training_reader=TrainingReader(self.db).resolve()
        return self.training_reader.read(context)

    def fixtures(self):
        from .fixtures import FixtureReader
        context=self._context()
        if self.fixture_reader is None:self.fixture_reader=FixtureReader(self.db).resolve()
        return self.fixture_reader.read(context)

    def close(self):
        self.fm.close(); self.db=None; self.context=None; self.index={}; self.session_id=None; self.fixture_reader=None; self.match_reader=None; self.tactics_reader=None; self.training_reader=None

    def match(self):
        from .match import MatchReader
        context=self._context()
        if self.match_reader is None:self.match_reader=MatchReader(self.db).resolve()
        return self.match_reader.read(context)

    def __enter__(self): return self.attach()
    def __exit__(self,*args): self.close()
