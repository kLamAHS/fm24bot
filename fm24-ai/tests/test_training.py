import json,struct,unittest
from pathlib import Path
from datetime import date
from types import SimpleNamespace
from bridge.training import TrainingReader
from bridge.database import Database
from bridge.process import MemoryReadError
from tests.test_contracts_nations import RecordedMemory
ROOT=Path(__file__).resolve().parents[1]/'research'

class TrainingTests(unittest.TestCase):
 def setup_reader(self):
  trace=json.loads((ROOT/'training-read-trace-programs.json').read_text(encoding='utf-8'))
  fm=RecordedMemory(trace['reads']);db=SimpleNamespace(fm=fm,module=SimpleNamespace(**trace['module']),person_pointers=lambda:trace['known_persons'],current_date=lambda:date.fromisoformat(trace['date']),dates=SimpleNamespace(read_raw=lambda:bytes.fromhex(trace['date_raw'])))
  db.type_offset=lambda p:Database.type_offset(db,p)
  context=SimpleNamespace(db=db,fm=fm,check=lambda:None,**trace['context'])
  reader=TrainingReader(db);reader.global_ptr=trace['global']
  return reader,context

 def test_february_calendar_ui_transcriptions(self):
  reader,c=self.setup_reader();result=reader.read(c)
  weeks={w.start_date:w for w in result.weeks}
  # Independent manual transcription of five full UI weeks, 105 sessions.
  expected={
   '2024-01-29':[['Quickness','Transition - Press','Routines'],['Endurance','Attacking','Rest'],['Match Tactics','Match Practice','Rest'],['Defending from the Front','Defending Engaged','Rest'],['Transition - Press','Rest','Match Focus'],['Travel','Match','Travel'],['Recovery','Rest','Rest']],
   '2024-02-05':[['Quickness','Transition - Press','Routines'],['Endurance','Attacking','Rest'],['Match Tactics','Match Practice','Rest'],['Defending from the Front','Defending Engaged','Rest'],['Transition - Press','Rest','Match Focus'],['Rest','Match','Rest'],['Recovery','Rest','Rest']],
   '2024-02-12':[['Routines','Outfield','Match Focus'],['Travel','Match','Travel'],['Recovery','Rest','Rest'],['Transition - Press','Rest','Match Focus'],['Rest','Match','Rest'],['Recovery','Rest','Rest'],['Defending from the Front','Defending Engaged','Attacking Direct']],
   '2024-02-19':[['Overall','Defending','Routines'],['Defending Engaged','Ground Defense','Rest'],['Defending Disengaged','Aerial Defense','Rest'],['Defending Wide','Defensive Shadow Play','Rest'],['Defending from the Front','Rest','Match Focus'],['Travel','Match','Travel'],['Recovery','Rest','Rest']],
   '2024-02-26':[['Physical','Possession','Routines'],['Ball Retention','Ball Distribution','Rest'],['Transition - Press','Transition - Restrict','Rest'],['Attacking Shadow Play','Play from the Back','Rest'],['Chance Creation','Rest','Match Focus'],['Rest','Match','Rest'],['Recovery','Rest','Rest']]}
  for monday,days in expected.items():
   with self.subTest(week=monday):
    self.assertEqual([[s.name for s in d.sessions] for d in weeks[monday].days],days)
    self.assertEqual(weeks[monday].days[0].date,monday)
  self.assertEqual(result.current_week_start,'2024-02-05')
  self.assertEqual(len(result.weeks),39)

 def test_committed_schedule_excludes_pending_ui_edit(self):
  old=json.loads((ROOT/'training-team-initial.json').read_text())['0x48']
  pending=json.loads((ROOT/'training-team-pending.json').read_text())['0x48']
  final=json.loads((ROOT/'training-team-confirmed.json').read_text())['0x48']
  self.assertEqual(old,pending)
  changed=[(x['date'],x['sessions'][0][0],y['sessions'][0][0]) for x,y in zip(old,final) if x['sessions']!=y['sessions']]
  self.assertEqual(changed,[('2024-02-19',6,0)])

 def test_two_player_focus_and_intensity_validation(self):
  reader,c=self.setup_reader();programs={x.player_id:x for x in reader.read(c).individual_programs}
  for uid in (28108492,14185872):
   self.assertEqual(programs[uid].additional_focus,'Quickness')
   self.assertEqual(programs[uid].intensity_setting,'Double Intensity')
  # Historical report values disagreed with the individual UI, so no
  # current rating is manufactured from those separate history records.
  for p in programs.values():
   self.assertIsNone(p.rating);self.assertEqual(p.rating_status,'not_decoded')

 def test_unknown_session_and_program_codes_remain_explicit(self):
  reader,c=self.setup_reader();fm=c.fm
  key=next(k for k in fm.values if k[0]=='read_bytes' and k[-1]==0x60)
  raw=bytearray(fm.values[key]);struct.pack_into('<H',raw,4,65000);fm.values[key]=bytes(raw)
  person=next(k[1] for k,v in fm.values.items() if k[0]=='read_bytes' and k[-1]==0x78 and struct.unpack_from('<I',v,12)[0]==14185872)
  focus_ptr=struct.unpack('<Q',fm.values[('read_bytes',person-0x278+0x170,8)])[0]
  focus=('read_bytes',focus_ptr,10)
  raw=bytearray(fm.values[focus]);raw[6]=250;fm.values[focus]=bytes(raw)
  result=reader.read(c)
  self.assertTrue(any(s.name is None and s.status=='not_decoded' for w in result.weeks for d in w.days for s in d.sessions))
  self.assertTrue(any(p.additional_focus_status=='not_decoded' for p in result.individual_programs))

 def test_changed_week_or_owner_is_rejected(self):
  for size in (0x60,16):
   reader,c=self.setup_reader();key=next(k for k in c.fm.values if k[0]=='read_bytes' and k[-1]==size)
   c.fm.change=lambda k,n,v:bytes(len(v)) if k==key and n==2 else v
   with self.assertRaisesRegex(MemoryReadError,'changed during'):reader.read(c)

 def test_cyclic_tree_wrong_human_and_duplicate_week_rejected(self):
  reader,c=self.setup_reader();key=next(k for k in c.fm.values if k[0]=='read_bytes' and k[-1]==0x28)
  raw=bytearray(c.fm.values[key]);struct.pack_into('<Q',raw,0,key[1]);c.fm.values[key]=bytes(raw)
  with self.assertRaisesRegex(MemoryReadError,'Cyclic'):reader.read(c)
  reader,c=self.setup_reader();c.db.person_pointers=lambda:[]
  with self.assertRaisesRegex(MemoryReadError,'human training owner'):reader.read(c)
  reader,c=self.setup_reader();keys=[k for k in c.fm.values if k[0]=='read_bytes' and k[-1]==0x60]
  raw=bytearray(c.fm.values[keys[1]]);raw[0x50:0x54]=c.fm.values[keys[0]][0x50:0x54];c.fm.values[keys[1]]=bytes(raw)
  with self.assertRaisesRegex(MemoryReadError,'duplicate training week'):reader.read(c)

 def test_empty_and_oversized_schedule_vectors(self):
  reader,c=self.setup_reader();key=next(k for k,v in c.fm.values.items() if k[0]=='read_bytes' and k[-1]==16 and isinstance(v,bytes) and len(v)==16 and struct.unpack('<QQ',v)[1]-struct.unpack('<QQ',v)[0]==39*8)
  c.fm.values[key]=bytes(16);self.assertEqual(reader.read(c).weeks,[])
  reader,c=self.setup_reader();start,end=struct.unpack('<QQ',c.fm.values[key]);c.fm.values[key]=struct.pack('<QQ',start,start+257*8)
  with self.assertRaisesRegex(MemoryReadError,'Invalid training vector'):reader.read(c)
