from datetime import date
import json
from pathlib import Path
import unittest
from unittest.mock import Mock,patch
from bridge.morale import decode_morale,MORALE_OFFSET,MORALE_LABELS
from bridge.players import decode_player
from bridge.process import MemoryReadError

RESEARCH=Path(__file__).resolve().parents[1]/'research'
def read(name): return json.loads((RESEARCH/name).read_text(encoding='utf-8'))

class MoraleTests(unittest.TestCase):
    def test_captured_31_players_match_independent_ui_labels(self):
        rows=read('morale-candidates-first.json')['players']
        expected=read('ui-morale-observations.json')['morale_by_name']
        self.assertEqual(len(rows),31)
        self.assertEqual({p['name'] for p in rows},set(expected))
        for p in rows:
            raw=bytes.fromhex(p['hex'])
            self.assertTrue(p['stable'])
            self.assertEqual(decode_morale(raw[MORALE_OFFSET:MORALE_OFFSET+1]),expected[p['name']])

    def test_gretna_capture_confirms_remaining_seven_labels(self):
        rows=read('morale-gretna-capture-earlier.json')['players']
        expected=read('ui-morale-gretna-observations.json')['morale_by_name']
        self.assertEqual(len(rows),18)
        self.assertEqual(set(expected)-{p['name'] for p in rows},
                         {'Bryan Gilfillan','Carter Jenkins','Paddy Meechan'})
        values=set()
        for p in rows:
            raw=bytes.fromhex(p['hex'])[MORALE_OFFSET:MORALE_OFFSET+1]
            self.assertTrue(p['morale_byte_stable'])
            self.assertEqual(decode_morale(raw),expected[p['name']])
            values.add(raw[0])
        self.assertTrue({1,3,4,5,7,9,19}<=values)
        self.assertEqual(set(MORALE_LABELS),set(range(1,21)))

    def test_incomplete_and_outside_values_rejected(self):
        for raw in (b'',b'\x0f\x00'):
            with self.assertRaises(MemoryReadError): decode_morale(raw)
        for value in [0,*range(21,256)]:
            with self.assertRaises(MemoryReadError): decode_morale(bytes([value]))

    def test_earlier_save_labels_and_changes_match_ui(self):
        first={p['id']:p for p in read('morale-candidates-first.json')['players']}
        expected=read('ui-morale-earlier-observations.json')['morale_by_name']
        rows=read('morale-candidates-earlier.json')['players']
        self.assertEqual({p['name'] for p in rows},set(expected))
        changed=0
        for p in rows:
            value=bytes.fromhex(p['hex'])[MORALE_OFFSET:MORALE_OFFSET+1]
            self.assertEqual(decode_morale(value),expected[p['name']])
            changed+=value!=bytes.fromhex(first[p['id']]['hex'])[MORALE_OFFSET:MORALE_OFFSET+1]
        self.assertEqual(changed,22)

    def test_repeated_byte_detects_change_and_model_matches_capture(self):
        row=next(p for p in read('morale-candidates-first.json')['players'] if p['id']==29232937)
        person=int(row['person'],16); base=int(row['player_base'],16)
        raw=bytes.fromhex(row['hex']); fm=Mock()
        fm.read_bytes.side_effect=lambda a,n:raw[a-base:a-base+n]
        fm.read_uint32.return_value=row['id']
        db=Mock(fm=fm); db.type_offset.return_value=0x278
        db.current_date.return_value=date(2024,2,17)
        db.dates.read_raw.return_value=bytes.fromhex('3012e807')
        with patch('bridge.players.identity',return_value=(row['id'],row['name'],'Jude','Bellingham')), patch('bridge.players.read_primary_nationality',return_value=None), patch('bridge.players.read_contracts',return_value=[]):
            model=decode_player(db,person)
            self.assertEqual(model.morale,'Very Good')
            self.assertEqual(model.morale_rating,15)
            calls=0
            def changed(a,n):
                nonlocal calls
                if a==base+MORALE_OFFSET:
                    calls+=1
                    if calls==2: return bytes([16])
                return raw[a-base:a-base+n]
            fm.read_bytes.side_effect=changed
            with self.assertRaisesRegex(MemoryReadError,'Player changed'):
                decode_player(db,person)

if __name__=='__main__': unittest.main()
