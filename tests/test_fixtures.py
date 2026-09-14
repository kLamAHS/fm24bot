import json,struct,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from bridge.dates import decode_game_date,decode_game_time
from bridge.fixtures import decode_fixture_values,FixtureReader
from bridge.process import MemoryReadError

RESEARCH=Path(__file__).resolve().parents[1]/'research'
def read(name):return json.loads((RESEARCH/name).read_text(encoding='utf-8'))

class FixtureTests(unittest.TestCase):
    def test_later_save_updates_three_results_and_preserves_schedule(self):
        ui={r[0]:r for r in read('fixtures-ui-later.json')['rows']}
        before=read('observation-checkpoint-earlier.json')
        after=read('observation-checkpoint-later.json')
        self.assertNotEqual(before['routes']['/status']['body']['session_id'],after['routes']['/status']['body']['session_id'])
        fixtures=after['routes']['/fixtures']['body']['data']['fixtures']
        self.assertEqual(len(fixtures),22)
        for row in fixtures:
            expected=ui[row['date']];home=row['home']['team_id']==742
            self.assertEqual(row['time'],expected[1])
            self.assertEqual((row['home_score'],row['away_score']) if home else (row['away_score'],row['home_score']),tuple(expected[4:6]))
        self.assertEqual(sum(r['status']=='played' for r in fixtures),10)
        self.assertEqual(after['routes']['/finances']['body']['data']['balance'],10732008)
        self.assertEqual(after['routes']['/game']['body']['data'],{'date':'2024-02-17','time':'08:00'})

    def test_owned_registry_matches_all_22_current_year_schedule_rows(self):
        captures=read('fixture-registry-earlier.json')['items']
        expected={r[0]:r for r in read('fixtures-ui-earlier.json')['rows'] if r[0]>='2024-01-01'}
        checked=0
        for row in captures:
            raw=bytes.fromhex(row['hex']);result='RESULT' in row['type']['name']
            day,time,home,away=decode_fixture_values(raw,result)
            self.assertEqual(day.timetuple().tm_yday-1,row['day_index'])
            if day.isoformat() not in expected:continue
            ui=expected[day.isoformat()];is_home=row['home']['id']==742
            self.assertEqual(time,ui[1]);self.assertEqual('H' if is_home else 'A',ui[3])
            self.assertEqual((home,away) if is_home else (away,home),(ui[4],ui[5]))
            opponent=row['away' if is_home else 'home']['name']
            self.assertTrue(opponent.startswith(ui[2]),(opponent,ui[2]))
            self.assertTrue(ui[6].startswith(row['competition']['name']))
            checked+=1
        self.assertEqual(checked,22)
        self.assertEqual(len(captures),22)

    def test_time_encoding_matches_all_40_visible_rows_and_two_game_clocks(self):
        # The broad scan is research evidence only; public enumeration uses owned lists.
        candidates=read('fixture-scan-candidates.json')['current_team_candidates']
        by_date={}
        for row in candidates:
            raw=bytes.fromhex(row['hex']);day=decode_game_date(raw[0x4c:0x50]).isoformat()
            by_date.setdefault(day,set()).add(decode_game_time(raw[0x4c:0x50]))
        for ui in read('fixtures-ui-earlier.json')['rows']:
            self.assertIn(ui[1],by_date[ui[0]])
        self.assertEqual(decode_game_time(bytes.fromhex('2600e807')),'00:00')
        self.assertEqual(decode_game_time(bytes.fromhex('3012e807')),'08:00')

    def test_unknown_time_and_invalid_score_are_rejected(self):
        with self.assertRaises(MemoryReadError):decode_game_time(struct.pack('<I',(2024<<16)|(127<<9)|48))
        raw=bytearray(bytes.fromhex(read('fixture-registry-earlier.json')['items'][0]['hex']))
        self.assertEqual(len(raw),0x80)
        raw[0x64]=255
        with self.assertRaises(MemoryReadError):decode_fixture_values(raw,True)
        with self.assertRaises(MemoryReadError):decode_fixture_values(b'',False)

    def test_replaced_manager_is_rejected_before_reading_lists(self):
        db=Mock();f=db.fm;reader=FixtureReader(db)
        reader.global_ptr=0x10000;reader.wrapper=0x20000;reader.manager=0x30000
        f.read_pointer.return_value=0x40000
        with self.assertRaisesRegex(MemoryReadError,'manager changed'):reader.check()

    def test_cached_calendar_from_different_year_is_rejected(self):
        db=Mock();db.dates.read_raw.return_value=bytes.fromhex('2600e807');db.fm.read_uint16.return_value=2023
        reader=FixtureReader(db);reader.manager=0x30000
        with patch.object(reader,'check'):
            with self.assertRaisesRegex(MemoryReadError,'calendar year'):reader.read(Mock())

    def test_empty_scheduled_scores_are_not_zero(self):
        row=next(x for x in read('fixture-registry-earlier.json')['items'] if x['type']['name']=='.?AVFIXTURE@sicomps@@')
        self.assertEqual(decode_fixture_values(bytes.fromhex(row['hex']),False)[2:],(None,None))
