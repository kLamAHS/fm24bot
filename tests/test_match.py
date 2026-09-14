import json,struct,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from bridge.match import MatchReader,decode_clock,decode_team_stats,decode_match_player,percentage
from bridge.process import MemoryReadError
from api.server import StateService
from structures.match import MatchObservation

RESEARCH=Path(__file__).resolve().parents[1]/'research'
def capture(label):return json.loads((RESEARCH/f'match-details-{label}.json').read_text(encoding='utf-8'))

class MatchTests(unittest.TestCase):
    def test_four_separately_observed_match_stat_panels(self):
        # Values transcribed from the saved UI screenshots, not from the decoder.
        cases=[('half-time-snapshots',(45,0,'half_time'),(3,0),[5,3,.88,0,7,1,87,57],[4,2,.17,1,5,1,83,43]),
               ('minute55',(55,3,'in_play'),(3,1),[5,3,.88,1,10,2,86,54],[6,3,1.11,2,5,1,85,46]),
               ('minute75',(75,31,'in_play'),(5,1),[9,5,2.14,3,12,2,87,55],[7,3,1.15,3,8,1,84,45]),
               ('full-time',(90,0,'full_time'),(6,1),[11,7,2.76,4,13,2,86,53],[11,3,1.60,4,11,2,85,47])]
        for label,clock,score,home,away in cases:
            d=capture(label);self.assertEqual(decode_clock(bytes.fromhex(d['game_hex'])),clock)
            stats=d['stats'][1];rr=bytes.fromhex(stats['result_hex']);self.assertEqual((rr[0x64],rr[0x68]),score)
            h=decode_team_stats(bytes.fromhex(stats['home_hex']));a=decode_team_stats(bytes.fromhex(stats['away_hex']))
            hp=percentage(h['passes_completed'],h['passes_completed']+a['passes_completed'])
            keys=['shots','shots_on_target','xg','corners','fouls','yellow_cards','pass_completion']
            self.assertEqual([h[k] for k in keys]+[hp],home)
            self.assertEqual([a[k] for k in keys]+[100-hp],away)

    def test_controller_cache_is_not_live_statistics(self):
        d=capture('minute55')
        self.assertEqual(bytes.fromhex(d['stats'][0]['result_hex'])[0x68],0)
        self.assertEqual(bytes.fromhex(d['stats'][1]['result_hex'])[0x68],1)

    def test_player_goals_cards_and_full_time_ratings(self):
        d=capture('full-time');names={p['person_index']:p['name'] for p in d['players']};decoded={}
        for p in d['stats'][1]['player_stats']:
            row=decode_match_player(bytes.fromhex(p['hex']))
            if row:decoded[names[row[0]]]=row[1]
        self.assertEqual(len(decoded),36)
        goals={'Joe Jacobson':1,'Jude Bellingham':1,'Wilson Odobert':1,'Garath McCleary':1,'Darwin Núñez':2,'Jonson Clarke-Harris':1}
        cards={'Matt Butcher','Wilson Odobert','Jadel Katongo','Harley Mills'}
        for name,row in decoded.items():
            self.assertEqual(row['goals'],goals.get(name,0));self.assertEqual(row['yellow_cards'],int(name in cards))
        ratings=[7.0,7.1,6.9,7.3,6.8,7.9,6.9,8.4,7.7,8.8,8.9]
        self.assertEqual([decoded[p['name']]['rating'] for p in d['players'][:11]],ratings)
        self.assertIsNone(decoded['Jed Steer']['rating'])
        self.assertTrue(decoded["Charlie O'Connell"]['rating_may_be_provisional'])
        self.assertTrue(decoded['Felipe Araruna']['substituted_in'])
        self.assertTrue(decoded['Jadel Katongo']['substituted_out'])
        self.assertFalse(decoded['Jed Steer']['appeared'])

    def test_invalid_and_empty_statistics_are_rejected_or_null(self):
        self.assertIsNone(percentage(0,0))
        self.assertEqual(percentage(1,8),13)
        row=bytearray(0x280);struct.pack_into('<f',row,0x60,float('nan'))
        with self.assertRaises(MemoryReadError):decode_team_stats(row)
        struct.pack_into('<f',row,0x60,0);row[0x162]=1
        with self.assertRaises(MemoryReadError):decode_team_stats(row)
        g=bytearray(0xE640);g[0xCDE4]=60
        with self.assertRaises(MemoryReadError):decode_clock(g)
        g[0xCDE4]=0
        self.assertEqual(decode_clock(g)[2],'unknown')
        p=bytearray(0x100);struct.pack_into('<I',p,0x10,0xFFFFFFFF)
        self.assertIsNone(decode_match_player(p))

    def test_replaced_manager_and_cyclic_tree_are_rejected(self):
        db=Mock();reader=MatchReader(db);reader.global_ptr=0x10000;reader.manager=0x20000
        db.fm.read_pointer.return_value=0x30000
        with self.assertRaisesRegex(MemoryReadError,'manager changed'):reader.check()
        head=0x40000;node=0x50000
        header=bytearray(0x20);header[0x19]=1;struct.pack_into('<Q',header,8,node)
        raw=bytearray(0x30);struct.pack_into('<Q',raw,0,node);struct.pack_into('<Q',raw,0x10,head)
        memory={(reader.manager+8,16):struct.pack('<QQ',head,1),(head,0x20):bytes(header),(node,0x30):bytes(raw)}
        db.fm.read_bytes.side_effect=lambda p,n:memory[(p,n)]
        with patch.object(reader,'check'),patch('bridge.match.type_info',return_value={'name':'.?AVGAME_LIVE_LATEST_SCORES_CONTROLLER@@','offset':0}):
            with self.assertRaisesRegex(MemoryReadError,'controller tree'):reader.controllers([])

    def test_match_endpoint_returns_no_viewer_without_inventing_zero_score(self):
        bridge=Mock();bridge.fm.alive.return_value=True;bridge.session_id='session-test'
        bridge.match.return_value=MatchObservation(False,'no_active_match_viewer',None)
        service=StateService();service.bridge=bridge
        status,body=service.get('/match')
        self.assertEqual(status,200);self.assertFalse(body['data']['available']);self.assertIsNone(body['data']['match'])
        self.assertEqual(body['session_id'],'session-test')

    def test_match_read_failure_is_not_returned_as_an_empty_match(self):
        bridge=Mock();bridge.fm.alive.return_value=True;bridge.match.side_effect=MemoryReadError('Match viewer changed during observation; retry')
        service=StateService();service.bridge=bridge
        status,body=service.get('/match')
        self.assertEqual(status,503);self.assertNotIn('data',body);bridge.close.assert_called_once()
