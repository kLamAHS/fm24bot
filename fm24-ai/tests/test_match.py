import json,struct,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from bridge.match import MatchReader,decode_clock,decode_team_stats,decode_match_player,percentage,match_identity,PLAYER_STATS_SIZE,starting_formation
from bridge.process import MemoryReadError
from api.server import StateService
from structures.match import MatchObservation,TeamMatchStats,MatchPlayer

RESEARCH=Path(__file__).resolve().parents[1]/'research'
def capture(label):return json.loads((RESEARCH/f'match-details-{label}.json').read_text(encoding='utf-8'))

class MatchTests(unittest.TestCase):
    def test_virtual_player_preserves_stats_without_guessing_identity(self):
        db=Mock();db.type_offset.return_value=0x30;observed=Mock(return_value=bytes(0x20))
        with patch('bridge.match.type_info',return_value={'name':'.?AVVIRTUAL_PLAYER@db@@','offset':0x30}),patch('bridge.match.identity') as actual_identity:
            self.assertEqual(match_identity(db,0x10000,observed),(None,None,'virtual_player_identity_not_decoded'))
            actual_identity.assert_not_called()
        d=capture('stevenage-after-virtual-fix')['public']['match']
        unresolved=[p for p in d['players'] if p['id'] is None]
        self.assertEqual(len(d['players']),36)
        self.assertEqual(len(unresolved),1)
        # Official team sheet and the independently viewed stats table: Ady
        # Cornick is Stevenage's unused substitute goalkeeper, number 27.
        self.assertEqual((unresolved[0]['side'],unresolved[0]['shirt_number'],unresolved[0]['appeared']),('home',27,False))
        self.assertIsNone(unresolved[0]['name']);self.assertIsNone(unresolved[0]['rating'])

    def test_virtual_identity_requires_exact_type(self):
        db=Mock();db.type_offset.return_value=0x30
        with patch('bridge.match.type_info',return_value={'name':'.?AVUNKNOWN@@','offset':0x30}):
            with self.assertRaises(MemoryReadError):match_identity(db,0x10000,Mock())

    def test_six_separately_observed_match_stat_panels(self):
        # Values transcribed from the saved UI screenshots, not from the decoder.
        cases=[('half-time-snapshots',(45,0,'half_time'),(3,0),[5,3,.88,0,7,1,87,57],[4,2,.17,1,5,1,83,43]),
               ('minute55',(55,3,'in_play'),(3,1),[5,3,.88,1,10,2,86,54],[6,3,1.11,2,5,1,85,46]),
               ('minute75',(75,31,'in_play'),(5,1),[9,5,2.14,3,12,2,87,55],[7,3,1.15,3,8,1,84,45]),
               ('full-time',(90,0,'full_time'),(6,1),[11,7,2.76,4,13,2,86,53],[11,3,1.60,4,11,2,85,47]),
               ('stevenage-half-time',(45,0,'half_time'),(0,2),[3,1,.29,1,11,1,82,55],[5,4,1.13,5,13,3,78,45]),
               ('stevenage-full-time',(90,0,'full_time'),(1,3),[9,2,.59,7,14,2,78,45],[10,7,1.80,7,16,3,82,55])]
        for label,clock,score,home,away in cases:
            d=capture(label);self.assertEqual(decode_clock(bytes.fromhex(d['game_hex'])),clock)
            stats=d['stats'][1];rr=bytes.fromhex(stats['result_hex']);self.assertEqual((rr[0x64],rr[0x68]),score)
            h=decode_team_stats(bytes.fromhex(stats['home_hex']));a=decode_team_stats(bytes.fromhex(stats['away_hex']))
            hp=percentage(h['passes_completed'],h['passes_completed']+a['passes_completed'])
            keys=['shots','shots_on_target','xg','corners','fouls','yellow_cards','pass_completion']
            self.assertEqual([h[k] for k in keys]+[hp],home)
            self.assertEqual([a[k] for k in keys]+[100-hp],away)
            self.assertEqual(TeamMatchStats(**h,possession=hp).possession_basis,
                             'inferred_from_completed_pass_share')

    def test_controller_cache_is_not_live_statistics(self):
        d=capture('minute55')
        self.assertEqual(bytes.fromhex(d['stats'][0]['result_hex'])[0x68],0)
        self.assertEqual(bytes.fromhex(d['stats'][1]['result_hex'])[0x68],1)

    def test_player_goals_cards_and_full_time_ratings(self):
        d=capture('full-time');names={p['person_index']:p['name'] for p in d['players']};decoded={}
        for p in d['stats'][1]['player_stats']:
            row=decode_match_player(bytes.fromhex(p['hex'])[:PLAYER_STATS_SIZE])
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
        p=bytearray(PLAYER_STATS_SIZE);struct.pack_into('<I',p,0x10,0xFFFFFFFF)
        self.assertIsNone(decode_match_player(p))
        with self.assertRaises(MemoryReadError):decode_match_player(p+bytes(8))

    def test_stevenage_positions_against_opposition_panel(self):
        d=capture('stevenage-minute28')
        names={p['person_index']:p['name'] for p in d['players'] if 'name' in p}
        players=[]
        for p in next(s for s in d['stats'] if s['kind']=='game_stats')['player_stats']:
            decoded=decode_match_player(bytes.fromhex(p['hex'])[:PLAYER_STATS_SIZE])
            if decoded:players.append(MatchPlayer(None,names.get(decoded[0]),**decoded[1]))
        formation=starting_formation(players,'home',123)
        # All eleven labels independently transcribed from Opposition at 28:33.
        self.assertEqual({s.player_name:s.position for s in formation.slots},{
            'Taye Ashby-Hammond':'GK','Finley Burns':'DCR','Patrick Gamble':'DC',
            'Carl Piergianni':'DCL','Luther James-Wildin':'WBR','Louis Thompson':'DM',
            'Nesta Guinness-Walker':'WBL','Ben Thompson':'MCR','Harvey White':'MCL',
            'Joshua Duffus':'STCR','Elliott List':'STCL'})
        self.assertEqual(formation.basis,'starting_lineup');self.assertIsNone(formation.name)
        with self.assertRaises(MemoryReadError):starting_formation(players[:2],'home',123)

    def test_substituted_players_retain_last_positions(self):
        d=capture('full-time');names={p['person_index']:p['name'] for p in d['players']};rows={}
        for p in d['stats'][1]['player_stats']:
            decoded=decode_match_player(bytes.fromhex(p['hex'])[:PLAYER_STATS_SIZE])
            if decoded:rows[names[decoded[0]]]=decoded[1]
        self.assertTrue(rows['Jadel Katongo']['substituted_out'])
        self.assertEqual(rows['Jadel Katongo']['last_position'],'DR')
        self.assertEqual(rows['Ronnie Edwards']['starting_position'],'DCR')
        self.assertEqual(rows['Ronnie Edwards']['last_position'],'DCL')

    def test_retained_condition_matches_three_postmatch_numeric_panels(self):
        d=capture('stevenage-full-time')
        names={p['person_index']:p['name'] for p in d['players'] if 'name' in p};rows={}
        for p in next(s for s in d['stats'] if s['kind']=='game_stats')['player_stats']:
            decoded=decode_match_player(bytes.fromhex(p['hex'])[:PLAYER_STATS_SIZE])
            if decoded:rows[names.get(decoded[0])]=decoded[1]
        # Independent Fitness detail panels after match processing: 9500,
        # 5900 and 7100 on FM's 0..10000 scale (screenshots in research/ui).
        for name,expected in [('Arijanet Murić',95),('Jason McCarthy',59),('Garath McCleary',71)]:
            player=MatchPlayer(None,name,**rows[name])
            self.assertEqual(player.condition,expected)
            self.assertEqual(player.condition_basis,'retained_match_statistics')
            self.assertTrue(player.condition_may_lag)
            self.assertIsNone(player.red_cards);self.assertIsNone(player.injury)

    def test_retained_condition_can_lag_simulation_and_rejects_invalid_values(self):
        d=capture('stevenage-minute28')
        actor=next(p for p in d['players'] if p.get('name')=='Arijanet Murić')
        raw=next(bytes.fromhex(p['hex'])[:PLAYER_STATS_SIZE] for p in next(s for s in d['stats'] if s['kind']=='game_stats')['player_stats']
                 if struct.unpack_from('<I',bytes.fromhex(p['hex']),0x10)[0]==actor['person_index'])
        decoded=decode_match_player(raw)[1]
        precise=struct.unpack_from('<i',bytes.fromhex(actor['hex']),0x240)[0]/10000
        self.assertGreater(decoded['condition'],precise)
        invalid=bytearray(raw);invalid[0x7D]=101
        with self.assertRaisesRegex(MemoryReadError,'condition'):decode_match_player(invalid)

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
