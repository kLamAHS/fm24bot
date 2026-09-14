import json,struct,unittest
from pathlib import Path
from datetime import date
from types import SimpleNamespace
from bridge.staff import read_staff,JOBS
from bridge.database import Database
from bridge.process import MemoryReadError
from tests.test_contracts_nations import RecordedMemory

ROOT=Path(__file__).resolve().parents[1]/'research'
# Names and jobs transcribed from staff-all-feb11.png, independently of memory.
UI={'Ian Gallagher':'Physio','Lee Harrison':'Goalkeeping Coach','Scott Mitchell':'Chief Scout',
    'Bob Rickwood':'Scout','Andrew Howard':'Technical Director','Michael Amoah':'Fitness Coach',
    "Cian O'Doherty":'Head Physio','Ben Cirne':'Head Performance Analyst','Trevor Stroud':'Director',
    'Bob Sangar':'Chief Doctor','Isaac Leckie':'Physio','Rob Couhig':'Chairperson','Craig Smith':'Sports Scientist',
    'Sam Grace':'Head Coach','Pete Couhig':'Director','David Cook':'Director','Missy Couhig':'Director',
    'Ben Sayers':'Head of Sports Science','Stephen Clarke':'Recruitment Analyst','Liam Boyd':'Head Coach'}

class StaffTests(unittest.TestCase):
 def context(self):
  trace=json.loads((ROOT/'staff-read-trace.json').read_text(encoding='utf-8'))
  fm=RecordedMemory(trace['reads']);db=SimpleNamespace(fm=fm,module=SimpleNamespace(**trace['module']),
    person_pointers=lambda:trace['known_persons'],current_date=lambda:date.fromisoformat(trace['date']),
    dates=SimpleNamespace(read_raw=lambda:bytes.fromhex(trace['date_raw'])))
  db.type_offset=lambda p:Database.type_offset(db,p)
  return SimpleNamespace(db=db,fm=fm,check=lambda:None,**trace['context'])

 def test_all_twenty_ui_names_and_validated_jobs(self):
  result=read_staff(self.context());self.assertEqual({s.name for s in result},set(UI))
  for s in result:
   if s.job is not None:self.assertEqual(s.job,UI[s.name])
  self.assertEqual(sum(not s.current_team for s in result),3)
  self.assertEqual({s.name for s in result if not s.current_team},{'Ian Gallagher','Sam Grace','Craig Smith'})

 def test_basic_contracts_have_no_fabricated_salary_or_expiry(self):
  result=read_staff(self.context())
  self.assertEqual({s.name for s in result if s.employment is None},
    {'Rob Couhig','Trevor Stroud','Pete Couhig','David Cook','Missy Couhig','Bob Sangar'})

 def test_full_contracts_match_unrounded_ui_examples(self):
  result={s.name:s for s in read_staff(self.context())}
  for name,wage,end in [('Lee Harrison',800,'2024-06-30'),('Michael Amoah',650,'2024-06-30'),
                        ('Scott Mitchell',1200,'2025-06-30'),("Cian O'Doherty",500,'2025-06-30')]:
   c=result[name].employment;self.assertEqual((c.weekly_wage_gbp,c.end_date),(wage,end))

 def test_unknown_job_is_null_while_member_is_preserved(self):
  ctx=self.context();original=read_staff(ctx)
  self.assertIsNone(next(s.job for s in original if s.name=='Rob Couhig'))
  self.assertNotIn(65535,JOBS)

 def test_changed_roster_and_missing_registry_member_rejected(self):
  ctx=self.context();key=('read_bytes',ctx.club+0x60,16)
  ctx.fm.change=lambda k,n,v:bytes(16) if k==key and n==2 else v
  with self.assertRaisesRegex(MemoryReadError,'Staff changed'):read_staff(ctx)
  ctx=self.context();ctx.db.person_pointers=lambda:[]
  with self.assertRaisesRegex(MemoryReadError,'outside the current registry'):read_staff(ctx)

 def test_contract_owner_and_short_class_boundaries(self):
  ctx=self.context()
  person=next(k[1] for k,v in ctx.fm.values.items() if k[0]=='read_bytes' and k[-1]==0x78
              and int.from_bytes(v[12:16],'little')==31042699)
  contract=int.from_bytes(ctx.fm.values[('read_bytes',person+0xc8,8)],'little')
  key=('read_bytes',contract,0x20)
  raw=bytearray(ctx.fm.values[key]);struct.pack_into('<Q',raw,8,0);ctx.fm.values[key]=bytes(raw)
  with self.assertRaisesRegex(MemoryReadError,'ownership mismatch'):read_staff(ctx)

 def test_duplicate_vector_and_foreign_team_rejected(self):
  ctx=self.context();bounds=ctx.fm.values[('read_bytes',ctx.club+0x60,16)];start,end=struct.unpack('<QQ',bounds)
  key=('read_bytes',start,end-start);raw=bytearray(ctx.fm.values[key]);raw[8:16]=raw[:8];ctx.fm.values[key]=bytes(raw)
  with self.assertRaisesRegex(MemoryReadError,'duplicate'):read_staff(ctx)
  ctx=self.context();ctx.fm.values[('read_bytes',ctx.team+0x30,8)]=struct.pack('<Q',ctx.club+8)
  with self.assertRaisesRegex(MemoryReadError,'different club'):read_staff(ctx)
