import json
from pathlib import Path
import unittest
from bridge.readiness import readiness_freshness,decode_readiness

RESEARCH=Path(__file__).resolve().parents[1]/'research'
def capture(label):
    return json.loads((RESEARCH/('morale-gretna-capture-'+label+'.json')).read_text(encoding='utf-8'))['players']

class FitnessFreshnessTests(unittest.TestCase):
    def test_ui_opening_refreshes_all_18_timestamps_and_15_blocks(self):
        before=capture('fitness-before-ui'); idle=capture('fitness-idle'); after=capture('fitness-after-club')
        current=bytes.fromhex('3012e807')
        changed=0
        for a,i,z in zip(before,idle,after):
            self.assertEqual(a['id'],z['id'])
            ar,ir,zr=(bytes.fromhex(x['hex']) for x in (a,i,z))
            self.assertEqual(ar[0x150:0x154],ir[0x150:0x154])
            self.assertEqual(ar[0x1F4:0x1FA],ir[0x1F4:0x1FA])
            self.assertEqual(readiness_freshness(ar[0x150:0x154],current)['status'],'stale')
            self.assertEqual(readiness_freshness(zr[0x150:0x154],current)['status'],'current')
            changed+=ar[0x1F4:0x1FA]!=zr[0x1F4:0x1FA]
        self.assertEqual(changed,15)
        jonny=next(x for x in after if x['id']==61038032)
        values=decode_readiness(bytes.fromhex(jonny['hex'])[0x1F4:0x217])
        # Independently observed in the numeric FM UI after opening the squad.
        self.assertEqual(values['condition'],96.30)
        self.assertEqual(values['match_sharpness'],84.50)

    def test_same_date_different_time_is_stale(self):
        current=bytes.fromhex('3012e807')
        result=readiness_freshness(bytes.fromhex('3000e807'),current)
        self.assertEqual(result,{'status':'stale','updated_on':'2024-02-17'})

    def test_invalid_cache_timestamp_is_unknown(self):
        self.assertEqual(readiness_freshness(b'\0'*4,bytes.fromhex('3012e807')),{'status':'unknown','updated_on':None})

    def test_earlier_save_restart_exhibits_same_refresh(self):
        for before,after in zip(capture('after-restart'),capture('earlier')):
            self.assertEqual(before['id'],after['id'])
            a,z=(bytes.fromhex(x['hex']) for x in (before,after))
            self.assertEqual(readiness_freshness(a[0x150:0x154],bytes.fromhex('2600e807'))['status'],'stale')
            self.assertEqual(readiness_freshness(z[0x150:0x154],bytes.fromhex('2600e807'))['status'],'current')
