import json
from pathlib import Path
import unittest
from bridge.attributes import ATTRIBUTES, decode_attributes
from bridge.process import MemoryReadError

RESEARCH = Path(__file__).resolve().parents[1] / 'research'

class AttributeTests(unittest.TestCase):
    def test_six_ui_profiles_cover_every_visible_attribute(self):
        report = json.loads((RESEARCH / 'attribute-validation-expanded.json').read_text(encoding='utf-8'))
        self.assertEqual(len(ATTRIBUTES), 47)
        self.assertEqual(set(report['coverage']), set(ATTRIBUTES))
        self.assertGreaterEqual(min(report['coverage'].values()), 2)
        for row in report['samples']:
            decoded = decode_attributes(bytes.fromhex(row['block_hex']))
            for field, expected in row['expected'].items():
                with self.subTest(player=row['name'], field=field):
                    self.assertEqual(decoded[field], expected)

    def test_minimum_inspection_did_not_change_attributes(self):
        before = json.loads((RESEARCH / 'player-details-attributes-minimum-before-second.json').read_text())
        after = json.loads((RESEARCH / 'player-details-attributes-minimum-after.json').read_text())
        by_id = {row['id']:row for row in after['players']}
        for row in before['players']:
            self.assertEqual(row['attributes_hex'], by_id[row['id']]['attributes_hex'])
            self.assertEqual(decode_attributes(bytes.fromhex(row['attributes_hex']))['finishing'], 1)

    def test_bad_bytes_and_truncated_blocks_are_rejected(self):
        for size in [0,53,55]:
            with self.assertRaises(MemoryReadError): decode_attributes(bytes([50])*size)
        for invalid in [0,101,255]:
            block = bytearray([50]*54); block[2] = invalid
            with self.assertRaises(MemoryReadError): decode_attributes(block)

    def test_hidden_bytes_are_not_decoded_as_visible_attributes(self):
        block = bytearray([50]*54)
        for offset in [0x18,0x19,0x29,0x2c,0x2f,0x30,0x31]: block[offset] = 255
        self.assertEqual(set(decode_attributes(block).values()), {10})
