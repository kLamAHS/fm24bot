import json,struct,unittest
from pathlib import Path
from datetime import date
from types import SimpleNamespace
from bridge.inbox import read_inbox,message_time
from bridge.process import MemoryReadError
from tests.test_contracts_nations import RecordedMemory
ROOT=Path(__file__).resolve().parents[1]/'research'

class InboxTests(unittest.TestCase):
 def context(self):
  trace=json.loads((ROOT/'inbox-read-trace-third-read.json').read_text(encoding='utf-8'))
  fm=RecordedMemory(trace['reads']);db=SimpleNamespace(fm=fm,module=SimpleNamespace(**trace['module']),person_pointers=lambda:trace['known_persons'],current_date=lambda:date.fromisoformat(trace['date']),dates=SimpleNamespace(read_raw=lambda:bytes.fromhex(trace['date_raw'])))
  return SimpleNamespace(db=db,fm=fm,check=lambda:None,**trace['context'])

 def test_ui_dates_times_and_senders(self):
  rows=read_inbox(self.context()).messages
  # Independent UI transcriptions from inbox-first/third-read, jan28-30,
  # midnight, feb1-details and feb1-heading screenshots.
  expected={0:('01-23','21:51','Sarah McHugh'),1:('01-24','08:00','Sarah McHugh'),2:('01-24','09:00','Robert Dobson'),3:('01-24','11:32','Andrew Howard'),4:('01-24','11:32','Andrew Howard'),5:('01-25','08:47','Sarah McHugh'),6:('01-25','08:58','Scott Mitchell'),7:('01-25','17:00','Lee Harrison'),8:('01-26','07:49','Lee Harrison'),9:('01-26','07:51','Sarah McHugh'),10:('01-26','20:47','Andrew Howard'),11:('01-27','07:55','Sarah McHugh'),18:('01-28','08:58','Ben Cirne'),19:('01-28','16:55','Scott Mitchell'),20:('01-28','21:05','Andrew Howard'),21:('01-29','08:47','Lee Harrison'),22:('01-29','13:34','Sarah McHugh'),23:('01-29','14:33','Jason McCarthy'),24:('01-30','09:48',"Cian O'Doherty"),25:('01-30','09:55','Ben Sayers'),26:('01-30','16:47','Scott Mitchell'),27:('01-30','21:53','Sarah McHugh'),28:('01-30','21:53','Sarah McHugh'),29:('01-31','07:50','Sarah McHugh'),30:('01-31','07:50','Sarah McHugh'),31:('01-31','08:50','Scott Mitchell'),32:('01-31','09:00','Sarah McHugh'),33:('01-31','14:51','Sam Grace'),34:('01-31','23:00','Sarah McHugh'),35:('01-31','23:00','Sarah McHugh'),36:('02-01','00:00','Sarah McHugh'),37:('02-01','00:00','Rob Couhig'),38:('02-01','08:48','Scott Mitchell'),39:('02-01','08:49','Ben Cirne'),40:('02-01','08:49','Ben Cirne'),41:('02-01','08:49','Ben Cirne'),42:('02-01','08:55','Robert Dobson'),43:('02-01','09:57','Rob Couhig'),44:('02-01','14:48','Rob Couhig'),45:('02-01','14:48','Rob Couhig'),46:('02-01','14:49','Jack Wright'),47:('02-01','16:54','Lee Harrison'),48:('02-01','20:52','Jack Wright'),49:('02-02','07:46','Lee Harrison'),50:('02-02','07:52','Sarah McHugh'),51:('02-02','07:58','Sarah McHugh'),52:('02-02','10:00',"Cian O'Doherty")}
  for index,values in expected.items():
   with self.subTest(index=index):self.assertEqual((rows[index].date[5:],rows[index].time,rows[index].sender_name),values)

 def test_ui_unread_count_and_two_read_transitions(self):
  result=read_inbox(self.context());self.assertEqual((len(result.messages),result.unread_count),(75,72))
  self.assertEqual([m.unread for m in result.messages[:4]],[False,False,False,True])
  a=json.loads((ROOT/'inbox-human-initial.json').read_text())['vectors'][0]['items']
  z=json.loads((ROOT/'inbox-human-third-read.json').read_text())['vectors'][0]['items']
  changed=[]
  for i,(old,new) in enumerate(zip(a,z)):
   x,y=bytes.fromhex(old['hex']),bytes.fromhex(new['hex'])
   if x[0xb0]!=y[0xb0]:changed.append((i,x[0xb0],y[0xb0]))
  self.assertEqual(changed,[(1,0x32,0x33),(2,0x32,0x33)])

 def test_support_staff_display_names_are_not_adjacent_generic_names(self):
  rows=read_inbox(self.context()).messages
  self.assertEqual((rows[0].sender_name,rows[2].sender_name,rows[46].sender_name),('Sarah McHugh','Robert Dobson','Jack Wright'))

 def test_undecoded_text_is_not_invented(self):
  for row in read_inbox(self.context()).messages:
   self.assertIsNone(row.subject);self.assertIsNone(row.body);self.assertEqual(row.text_status,'not_decoded')

 def test_changed_message_and_replaced_inbox_rejected(self):
  for size in (0xb8,16):
   c=self.context();key=next(k for k in c.fm.values if k[0]=='read_bytes' and k[-1]==size)
   c.fm.change=lambda k,n,v:bytes(len(v)) if k==key and n==2 else v
   with self.assertRaisesRegex(MemoryReadError,'changed during'):read_inbox(c)

 def test_sender_membership_duplicate_ids_and_future_date_rejected(self):
  c=self.context();c.db.person_pointers=lambda:[]
  with self.assertRaisesRegex(MemoryReadError,'outside the Person registry'):read_inbox(c)
  c=self.context();keys=[k for k in c.fm.values if k[0]=='read_bytes' and k[-1]==0xb8]
  raw=bytearray(c.fm.values[keys[1]]);raw[0xa8:0xac]=c.fm.values[keys[0]][0xa8:0xac];c.fm.values[keys[1]]=bytes(raw)
  with self.assertRaisesRegex(MemoryReadError,'duplicate inbox message ID'):read_inbox(c)
  c=self.context();c.db.current_date=lambda:date(2023,1,1)
  with self.assertRaisesRegex(MemoryReadError,'dated after'):read_inbox(c)

 def test_rtti_base_and_time_bounds_rejected(self):
  c=self.context()
  for key,val in list(c.fm.values.items()):
   if isinstance(val,bytes) and val.startswith(b'.?AVNEWS_ITEM@db@@'):c.fm.values[key]=val.replace(b'NEWS_ITEM',b'FAKE_ITEM')
  with self.assertRaisesRegex(MemoryReadError,'base type is absent'):read_inbox(c)
  with self.assertRaisesRegex(MemoryReadError,'minute adjustment'):message_time(bytes.fromhex('181ae807'),15)
