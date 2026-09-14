from datetime import date
import json
from pathlib import Path
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from bridge.dates import GameDate,decode_game_date,decode_birth_date,ordinal_date,age_on
from bridge.players import decode_player
from bridge.process import MemoryReadError
from bridge.signatures import Section

RESEARCH=Path(__file__).resolve().parents[1]/'research'
def read(name): return json.loads((RESEARCH/name).read_text(encoding='utf-8'))

class DateTests(unittest.TestCase):
    def test_captured_game_date_matches_ui(self):
        raw=bytes.fromhex(read('date-candidates-first.json')['targets'][0]['raw_hex'])
        self.assertEqual(decode_game_date(raw).isoformat(),read('ui-date-observations.json')['game_date'])
        # Uninterpreted flags must not become part of the ordinal day.
        self.assertEqual(decode_game_date(struct.pack('<I',(2024<<16)|0xFE00|48)),date(2024,2,17))

    def test_captured_birth_dates_and_ages_match_four_ui_profiles(self):
        capture=read('date-candidates-first.json')
        by_id={p['id']:p for p in capture['squad_birth_dates']+capture['birthday_test_candidates']}
        observed=read('ui-date-observations.json')
        for p in observed['players']:
            born=decode_birth_date(bytes.fromhex(by_id[p['id']]['raw_hex']))
            self.assertEqual(born.isoformat(),p['date_of_birth'])
            self.assertEqual(age_on(born,date.fromisoformat(observed['game_date'])),p['age'])

    def test_all_31_captured_squad_ages_match_ui(self):
        observed=read('ui-squad-ages.json')
        expected=observed['ages_by_name']
        rows=read('date-candidates-first.json')['squad_birth_dates']
        self.assertEqual({p['name'] for p in rows},set(expected))
        for p in rows:
            born=decode_birth_date(bytes.fromhex(p['raw_hex']))
            self.assertEqual(age_on(born,date.fromisoformat(observed['game_date'])),expected[p['name']])

    def test_captured_birthday_across_two_saves(self):
        earlier=read('date-candidates-earlier.json')
        observed=read('ui-date-earlier-observations.json')
        as_of=decode_game_date(bytes.fromhex(earlier['targets'][0]['raw_hex']))
        self.assertEqual(as_of.isoformat(),observed['game_date'])
        by_id={p['id']:p for p in earlier['birthday_test_candidates']}
        for p in observed['players']:
            born=decode_birth_date(bytes.fromhex(by_id[p['id']]['raw_hex']))
            self.assertEqual(born.isoformat(),p['date_of_birth'])
            self.assertEqual(age_on(born,as_of),p['age'])
            self.assertEqual(age_on(born,date(2024,2,17)),p['age']+1)

    def test_calendar_and_birthday_boundaries(self):
        self.assertEqual(ordinal_date(2000,60),date(2000,2,29))
        self.assertEqual(ordinal_date(2024,366),date(2024,12,31))
        self.assertEqual(ordinal_date(2100,60),date(2100,3,1))
        born=date(1992,2,14)
        self.assertEqual([age_on(born,date(2024,2,d)) for d in (13,14,15)],[31,32,32])
        # Compare month/day, not ordinals from unlike leap/non-leap years.
        self.assertEqual(age_on(date(2000,3,1),date(2023,3,1)),23)
        self.assertEqual(age_on(date(2001,3,1),date(2024,2,29)),22)

    def test_invalid_dates_and_unvalidated_leap_boundary_rejected(self):
        for year,ordinal in [(0,1),(10000,1),(2024,0),(2024,367),(2023,366),(2100,366)]:
            with self.assertRaises(MemoryReadError): ordinal_date(year,ordinal)
        for raw in (b'',b'\0'*3,b'\0'*5):
            with self.assertRaises(MemoryReadError): decode_game_date(raw)
            with self.assertRaises(MemoryReadError): decode_birth_date(raw)
        with self.assertRaises(MemoryReadError): age_on(date(2025,1,1),date(2024,2,17))
        with self.assertRaises(MemoryReadError): age_on(date(2000,2,29),date(2023,2,28))

    def test_ambiguous_and_outside_module_targets_rejected(self):
        fm=Mock(); fm.read_bytes.return_value=struct.pack('<I',(2024<<16)|48)
        db=Mock(fm=fm,module=SimpleNamespace(base=0x10000,size=0x10000))
        with patch('bridge.dates.pe_sections',return_value=[Section('code',0x10000,0x1000,0x20000000)]),patch('bridge.dates.scan',return_value=[0x10100,0x10200]):
            for targets in [[0x15000,0x16000],[0xFFFF,0x20000]]:
                with patch('bridge.dates.rip_target',side_effect=targets):
                    with self.assertRaises(MemoryReadError): GameDate(db).resolve()

    def test_date_changes_and_unloaded_registry_rejected(self):
        fm=Mock(); fm.read_bytes.side_effect=[struct.pack('<I',(2024<<16)|48),struct.pack('<I',(2024<<16)|49)]
        db=Mock(fm=fm); reader=GameDate(db); reader.address=0x15000
        with self.assertRaises(MemoryReadError): reader.read()
        db.person_pointers.side_effect=MemoryReadError('Save unloaded')
        fm.read_bytes.reset_mock(side_effect=True)
        with self.assertRaises(MemoryReadError): reader.read()
        fm.read_bytes.assert_not_called()

    def test_player_read_rejects_date_rollover_and_changed_dob(self):
        captured=read('validation-readiness-before-reload.json')['players'][0]['memory']['evidence']
        person=int(captured['person'],16); base=int(captured['player_base'],16)
        birth=struct.pack('<HH',captured['birth_day_raw'],captured['birth_year_raw'])
        memory={(person+0x44,4):birth,(base+0x1F4,35):bytes.fromhex(captured['readiness_hex']),
                (base+0x217,54):bytes.fromhex(captured['block_hex']),(base+0x25F,1):bytes([15]),
                (base+0x150,4):bytes.fromhex('3012e807')}
        fm=Mock(); fm.read_bytes.side_effect=lambda a,n:memory[(a,n)]; fm.read_uint32.return_value=29232937
        db=Mock(fm=fm); db.type_offset.return_value=0x278
        db.dates.read_raw.return_value=bytes.fromhex('3012e807')
        db.current_date.side_effect=[date(2024,2,17),date(2024,2,18)]
        with patch('bridge.players.identity',return_value=(29232937,'Jude Bellingham','Jude','Bellingham')):
            with self.assertRaisesRegex(MemoryReadError,'Game date changed'): decode_player(db,person)
            db.current_date.side_effect=None; db.current_date.return_value=date(2024,2,17)
            calls=0
            def changed_birth(a,n):
                nonlocal calls
                if a==person+0x44:
                    calls+=1
                    if calls==2: return struct.pack('<HH',181,2003)
                return memory[(a,n)]
            fm.read_bytes.side_effect=changed_birth
            with self.assertRaisesRegex(MemoryReadError,'Player changed'): decode_player(db,person)

if __name__=='__main__': unittest.main()
