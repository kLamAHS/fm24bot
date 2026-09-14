import json,struct,unittest
from pathlib import Path
from datetime import date
from types import SimpleNamespace
from bridge.tactics import TacticsReader
from bridge.database import Database
from bridge.process import MemoryReadError
from tests.test_contracts_nations import RecordedMemory

ROOT=Path(__file__).resolve().parents[1]/'research'

class TacticsTests(unittest.TestCase):
 def reader(self,label='resumed'):
  trace=json.loads((ROOT/f'tactics-read-trace-{label}.json').read_text(encoding='utf-8'))
  fm=RecordedMemory(trace['reads']);db=SimpleNamespace(fm=fm,module=SimpleNamespace(**trace['module']),person_pointers=lambda:trace['known_persons'],current_date=lambda:date.fromisoformat(trace['date']),dates=SimpleNamespace(read_raw=lambda:bytes.fromhex(trace['date_raw'])))
  db.type_offset=lambda p:Database.type_offset(db,p)
  reader=TacticsReader(db);reader.global_ptr=trace['global'];reader.player_index={int(k):v for k,v in trace['player_index'].items()}
  ctx=SimpleNamespace(db=db,fm=fm,check=lambda:None,**trace['context'])
  return reader,ctx

 def test_restarted_game_matches_ui_and_preserves_empty_striker(self):
  r,c=self.reader();t=r.read(c)
  # Independent transcription: tactics-resumed-feb7.png.
  expected={'GK':'Arijanet Murić','DR':'Jason McCarthy','DCR':'Chris Forino','DCL':'Ryan Tafazolli','DL':'Joe Jacobson','DMCR':'Jude Bellingham','DMCL':'Luke Leahy','AMR':'Luca Koleosho','AMC':'Darwin Núñez','AML':'Wilson Odobert','STC':None}
  self.assertEqual({p.position:p.player_name for p in t.positions},expected)
  self.assertEqual((t.selected_slot,t.mentality,t.style),(1,'Attacking','Custom Vertical Tiki-Taka'))
  self.assertEqual([p.player_name for p in t.substitutes[:7]],['Maksymilian Stryjek','Kane Vincent-Young','Garath McCleary','Brandon Hanlan','Maxime Estève','Freddie Potts','Gideon Kodua'])
  self.assertTrue(all(p.player_id is None for p in t.substitutes[7:]))

 def test_ui_goalkeeper_swap_changes_current_selection(self):
  r,c=self.reader('goalkeeper-swap');t=r.read(c)
  self.assertEqual((t.positions[0].player_name,t.substitutes[0].player_name),('Maksymilian Stryjek','Arijanet Murić'))

 def test_personalized_role_overrides_the_position_role(self):
  r,c=self.reader('personal-goalkeeper');t=r.read(c);p=t.positions[0]
  self.assertEqual((p.player_name,p.role,p.duty,p.instructions_source),('Maksymilian Stryjek','Goalkeeper','Defend','player'))
  self.assertTrue(all(p.instructions_source=='position' for p in t.positions[1:]))

 def test_disabled_personalized_record_does_not_override_position(self):
  r,c=self.reader('personal-disabled');p=r.read(c).positions[0]
  self.assertEqual((p.player_name,p.role,p.duty,p.instructions_source),('Maksymilian Stryjek','Sweeper Keeper','Defend','position'))

 def test_lineup_changed_during_read_rejected(self):
  r,c=self.reader();key=next(k for k in c.fm.values if k[0]=='read_bytes' and k[-1]==104)
  c.fm.change=lambda k,n,v:bytes(104) if k==key and n==2 else v
  with self.assertRaisesRegex(MemoryReadError,'changed during'):r.read(c)

 def test_duplicate_player_and_changed_index_rejected(self):
  r,c=self.reader();key=next(k for k in c.fm.values if k[0]=='read_bytes' and k[-1]==104)
  raw=bytearray(c.fm.values[key]);raw[4:8]=raw[:4];c.fm.values[key]=bytes(raw)
  with self.assertRaisesRegex(MemoryReadError,'Duplicate selected'):r.read(c)
  r,c=self.reader();person=next(iter(r.player_index.values()));c.fm.values[('read_bytes',person+8,4)]=struct.pack('<I',0xffffffff)
  with self.assertRaisesRegex(MemoryReadError,'outside the current registry'):r.read(c)

 def test_invalid_slot_and_creator_replacement_rejected(self):
  r,c=self.reader();key=next(k for k in c.fm.values if k[0]=='read_bytes' and k[-1]==1);c.fm.values[key]=b'\x03'
  with self.assertRaisesRegex(MemoryReadError,'outside the owned slots'):r.read(c)
  r,c=self.reader();key=next(k for k in c.fm.values if k[0]=='read_bytes' and k[-1]==0x458)
  c.fm.change=lambda k,n,v:bytes(len(v)) if k==key and n==2 else v
  with self.assertRaisesRegex(MemoryReadError,'changed during'):r.read(c)

 def test_unknown_role_and_position_remain_explicitly_unknown(self):
  r,c=self.reader();key=next(k for k in c.fm.values if k[0]=='read_bytes' and k[-1]==0x458)
  raw=bytearray(c.fm.values[key]);struct.pack_into('<QI',raw,0x30,0xffffffffffffffff,0xffffffff);c.fm.values[key]=bytes(raw)
  t=r.read(c);p=t.positions[0]
  self.assertEqual((p.position,p.role,p.duty),(None,None,None));self.assertEqual(p.position_code,0xffffffff)

 def test_tactic_tree_cycles_rejected(self):
  r,c=self.reader();key=next(k for k in c.fm.values if k[0]=='read_bytes' and k[-1]==0x28)
  raw=bytearray(c.fm.values[key]);struct.pack_into('<I',raw,0x20,0xffffffff);struct.pack_into('<Q',raw,0,key[1]);c.fm.values[key]=bytes(raw)
  with self.assertRaisesRegex(MemoryReadError,'Cyclic'):r.read(c)
