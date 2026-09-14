import json,struct,unittest
from pathlib import Path
from datetime import date
from types import SimpleNamespace
from bridge.scouting import read_scouting,read_shortlists,read_transfer_targets
from bridge.process import MemoryReadError
from tests.test_contracts_nations import RecordedMemory
ROOT=Path(__file__).resolve().parents[1]/'research'

class ScoutingTests(unittest.TestCase):
 def context(self):
  trace=json.loads((ROOT/'scouting-read-trace-checked.json').read_text(encoding='utf-8'))
  fm=RecordedMemory(trace['reads']);db=SimpleNamespace(fm=fm,module=SimpleNamespace(**trace['module']),person_pointers=lambda:trace['known_persons'],current_date=lambda:date.fromisoformat(trace['date']),dates=SimpleNamespace(read_raw=lambda:bytes.fromhex(trace['date_raw'])))
  return SimpleNamespace(db=db,fm=fm,check=lambda:None,**trace['context'])

 def test_default_named_and_nonplayer_lists(self):
  rows=read_shortlists(self.context()).lists
  self.assertEqual({r.name for r in rows},{'Default','Bridge Watch'})
  default=next(r for r in rows if r.is_default)
  self.assertEqual({(p.id,p.name,p.added_on) for p in default.players},{(29226265,'Brad Young','2024-02-07'),(29001407,'Sam Vokes','2024-02-07')})
  self.assertEqual(next(r for r in rows if not r.is_default).players,[])
  # A null expiry must never be interpreted as an indefinite shortlist term.
  for p in default.players:self.assertIsNone(p.expires_on);self.assertEqual(p.expiry_status,'not_decoded')

 def test_three_ui_scout_report_comparisons(self):
  rows={r.player_id:r for r in read_scouting(self.context()).reports}
  for uid,expected in {29226265:('Brad Young','Scott Mitchell','2024-01-31','Extensive'),29001407:('Sam Vokes','Lee Harrison','2024-01-24','Extensive'),29110538:('Farrend Rawson','Bob Rickwood','2023-07-11','Reasonable')}.items():
   r=rows[uid];self.assertEqual((r.player_name,r.scout_name,r.completed_on,r.knowledge),expected)
  self.assertEqual(len(rows),57)
  # The combined overview and individual report can have different grades.
  self.assertTrue(all(r.recommendation is None for r in rows.values()))

 def test_two_ui_targets_and_status_transition(self):
  rows=read_transfer_targets(self.context()).targets
  self.assertEqual([(r.player_id,r.type,r.status,r.priority,r.added_on) for r in rows],[(29226265,'Transfer','Not Started','Urgent','2024-02-07'),(29001407,'Transfer','On Hold','Normal','2024-02-07')])
  a=json.loads((ROOT/'scouting-transfer-two-normal.json').read_text())['roots'][0]['groups'][1]['targets'][0]
  z=json.loads((ROOT/'scouting-transfer-two-held.json').read_text())['roots'][0]['groups'][1]['targets'][0]
  before,after=bytes.fromhex(a['hex']),bytes.fromhex(z['hex'])
  self.assertEqual([(i,x,y) for i,(x,y) in enumerate(zip(before,after)) if x!=y],[(0x62,0,4)])

 def test_wrong_owner_and_unknown_person_rejected(self):
  c=self.context();key=next(k for k,v in c.fm.values.items() if k[0]=='read_bytes' and k[-1]==8 and v==struct.pack('<Q',c.staff));c.fm.values[key]=bytes(8)
  with self.assertRaisesRegex(MemoryReadError,'owner mismatch'):read_shortlists(c)
  for fn in (read_scouting,read_shortlists,read_transfer_targets):
   c=self.context();c.db.person_pointers=lambda:[]
   with self.assertRaisesRegex(MemoryReadError,'outside registry'):fn(c)

 def test_unknown_labels_are_not_guessed(self):
  c=self.context()
  for k,v in list(c.fm.values.items()):
   if k[0]=='read_bytes' and k[-1]==0x66:
    raw=bytearray(v);raw[0x60]=raw[0x62]=raw[0x65]=254;c.fm.values[k]=bytes(raw)
  self.assertTrue(all(r.type is r.status is r.priority is None for r in read_transfer_targets(c).targets))
  c=self.context()
  for k,v in list(c.fm.values.items()):
   if k[0]=='read_bytes' and k[-1]==0x44:
    raw=bytearray(v);raw[0x40]=254;c.fm.values[k]=bytes(raw)
  self.assertTrue(all(r.knowledge is None for r in read_scouting(c).reports))

 def test_changed_or_replaced_records_rejected(self):
  for fn,size in ((read_scouting,0x44),(read_shortlists,0x30),(read_transfer_targets,0x66)):
   c=self.context();key=next(k for k in c.fm.values if k[0]=='read_bytes' and k[-1]==size)
   c.fm.change=lambda k,n,v:bytes(len(v)) if k==key and n==2 else v
   with self.assertRaisesRegex(MemoryReadError,'changed during'):fn(c)

 def test_vectors_bounded_and_empty_supported(self):
  for fn,offset in ((read_scouting,0x108),(read_shortlists,0x210)):
   c=self.context();key=('read_bytes',c.staff+offset,16);c.fm.values[key]=struct.pack('<QQ',0x10000,0x7fffffff0000)
   with self.assertRaisesRegex(MemoryReadError,'Invalid scouting vector'):fn(c)
   c=self.context();c.fm.values[key]=bytes(16)
   self.assertEqual(len(getattr(fn(c),'reports' if offset==0x108 else 'lists')),0)
