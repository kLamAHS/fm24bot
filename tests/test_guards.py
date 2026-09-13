import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from bridge.database import Database
from bridge.process import MemoryReadError
from bridge.profile import require_supported_build

class GuardTests(unittest.TestCase):
    def test_unsupported_executable_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fm.exe'
            path.write_bytes(b'Unvalidated executable')
            with self.assertRaises(MemoryReadError):
                require_supported_build(SimpleNamespace(path=str(path)))

    def registry(self):
        fm = Mock()
        pointers = {0x10068: 0x20000, 0x20080: 0x30000,
                    0x30000: 0x40000, 0x30008: 0x40008}
        fm.read_pointer.side_effect = pointers.__getitem__
        db = Database.__new__(Database)
        db.fm, db.root, db.registry = fm, 0x10000, 0x30000
        return db, fm, pointers

    def test_same_bounds_changed_members_rejected(self):
        db, fm, _ = self.registry()
        fm.read_bytes.side_effect = [struct.pack('<Q', 0x50000), struct.pack('<Q', 0x60000)]
        with self.assertRaises(MemoryReadError): db.person_pointers()

    def test_stale_registry_rejected_before_member_read(self):
        db, fm, pointers = self.registry()
        pointers[0x20080] = 0x70000
        with self.assertRaises(MemoryReadError): db.person_pointers()
        fm.read_bytes.assert_not_called()

    def test_consistent_registry_accepted(self):
        db, fm, _ = self.registry()
        fm.read_bytes.return_value = struct.pack('<Q', 0x50000)
        self.assertEqual(db.person_pointers(), [0x50000])
