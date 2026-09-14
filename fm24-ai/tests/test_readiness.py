import json
from pathlib import Path
import struct
import unittest
from bridge.readiness import decode_readiness, READINESS_SIZE
from bridge.process import MemoryReadError

RESEARCH=Path(__file__).resolve().parents[1]/'research'

def captured_blocks():
    rows=json.loads((RESEARCH/'position-candidates.json').read_text(encoding='utf-8'))
    # Captured before opening any detail panels or implementing this decoder.
    return {r['id']:bytes.fromhex(r['nearby_180_208_hex'])[0x74:]+bytes(r['positions_candidate'].values()) for r in rows}

class ReadinessTests(unittest.TestCase):
    def test_captured_bytes_match_independent_ui_values(self):
        captured=captured_blocks()
        observed=json.loads((RESEARCH/'ui-readiness-observations.json').read_text())['players']
        for row in observed:
            decoded=decode_readiness(captured[row['id']])
            self.assertEqual(decoded['position_ratings'],row['position_ratings'])
            self.assertEqual(decoded['condition'],row['condition_raw']/100)
            self.assertEqual(decoded['match_sharpness'],row['match_sharpness_raw']/100)
            self.assertEqual(decoded['evidence']['fatigue_raw'],row['fatigue_raw'])

    def test_squad_roles_match_ui(self):
        captured=captured_blocks()
        expected=json.loads((RESEARCH/'ui-squad-positions.json').read_text())['positions_by_id']
        for uid,positions in expected.items():
            self.assertEqual(set(decode_readiness(captured[int(uid)])['positions']),set(positions))

    def test_invalid_values_are_not_clamped(self):
        original=captured_blocks()[29232937]
        for offset,value in ((0,-1),(0,10001),(4,-1),(4,10001)):
            raw=bytearray(original); struct.pack_into('<h',raw,offset,value)
            with self.assertRaises(MemoryReadError): decode_readiness(raw)
        for value in (0,21,255):
            raw=bytearray(original); raw[20]=value
            with self.assertRaises(MemoryReadError): decode_readiness(raw)

    def test_incomplete_block_rejected(self):
        with self.assertRaises(MemoryReadError): decode_readiness(bytes(READINESS_SIZE-1))
