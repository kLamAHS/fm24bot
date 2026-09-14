import json
from pathlib import Path
import struct
from types import SimpleNamespace
import unittest
from bridge.contracts import read_contracts, decode_contract_terms, CONTRACT_SIZE
from bridge.nations import read_primary_nationality
from bridge.process import MemoryReadError

RESEARCH = Path(__file__).resolve().parents[1] / 'research'


class RecordedMemory:
    def __init__(self, reads):
        self.values = {(r['method'], *r['args']): bytes.fromhex(r['value']['hex'])
                       if isinstance(r['value'], dict) else r['value'] for r in reads}
        self.counts = {}
        self.change = lambda key, count, value: value

    def __getattr__(self, method):
        def read(*args):
            key = (method, *args)
            if key not in self.values:
                raise MemoryReadError('Read outside captured evidence')
            self.counts[key] = self.counts.get(key, 0) + 1
            return self.change(key, self.counts[key], self.values[key])
        return read


class ContractNationTests(unittest.TestCase):
    def capture(self, uid):
        trace = json.loads((RESEARCH / 'player-contract-nation-trace.json').read_text(encoding='utf-8'))
        row = next(p for p in trace['players'] if p['id'] == uid)
        fm = RecordedMemory(row['reads'])
        return SimpleNamespace(fm=fm, module=SimpleNamespace(**trace['module'])), row['person']

    def test_six_profiles_primary_nationality(self):
        # Manually observed national labels, not expected values from the decoder.
        expected = {211388:(793,'Scotland'),29234746:(765,'England'),
                    29232937:(765,'England'),28028684:(114,'Iran'),
                    14185872:(1649,'Argentina'),96053555:(787,'Poland')}
        for uid, pair in expected.items():
            with self.subTest(id=uid):
                db, person = self.capture(uid)
                nation = read_primary_nationality(db, person)
                self.assertEqual((nation.id,nation.name), pair)

    def test_employment_and_loan_dates_match_contract_screens(self):
        # UI: contract-stryjek-post-match, contract-jude-post-match,
        # contract-potts-post-match. Loan and parent terms must stay separate.
        expected = {
            96053555:[('employment','2022-08-18','2024-06-30',1500)],
            29232937:[('employment','2023-07-01','2029-06-30',0),
                      ('loan','2023-08-12','2028-08-11',342046)],
            2000020024:[('employment','2023-07-03','2026-06-30',2500),
                        ('loan','2023-07-03','2024-05-31',1250)],
        }
        for uid, values in expected.items():
            db, person = self.capture(uid)
            result = read_contracts(db, person)
            self.assertEqual([(c.kind,c.start_date,c.end_date,c.weekly_wage_gbp) for c in result],values)
            for c in result:
                self.assertEqual(c.wage_basis, 'loan_contribution' if c.kind == 'loan' else 'salary')

    def test_ui_rounding_does_not_replace_native_amount(self):
        for uid, native, shown in [(29232937,342046,350000),(2000020024,1250,1300)]:
            db, person = self.capture(uid)
            loan = next(c for c in read_contracts(db,person) if c.kind == 'loan')
            self.assertEqual(loan.weekly_wage_gbp,native)
            self.assertNotEqual(loan.weekly_wage_gbp,shown)

    def test_free_agent_has_no_agreement_not_a_zero_salary_contract(self):
        db, person = self.capture(29234746)
        self.assertEqual(read_contracts(db, person), [])

    def test_salary_and_club_for_direct_contracts(self):
        for uid, wage, club in [(211388,100,'Drumchapel United'),
                                (28028684,3000,'Wycombe Wanderers'),
                                (14185872,800,'Wycombe Wanderers')]:
            db, person = self.capture(uid)
            agreement, = read_contracts(db,person)
            self.assertEqual((agreement.weekly_wage_gbp,agreement.club_name),(wage,club))

    def test_wrong_owner_and_changed_loan_holder_are_rejected(self):
        db, person = self.capture(29232937)
        links = db.fm.values[('read_bytes',person+0xc8,16)]
        parent, holder = struct.unpack('<QQ',links)
        raw = bytearray(db.fm.values[('read_bytes',parent,CONTRACT_SIZE)])
        struct.pack_into('<Q',raw,8,person+8)
        db.fm.values[('read_bytes',parent,CONTRACT_SIZE)] = bytes(raw)
        with self.assertRaisesRegex(MemoryReadError,'different person'): read_contracts(db,person)
        db, person = self.capture(29232937)
        db.fm.change = lambda key,count,value: value+8 if key==('read_pointer',holder) and count==2 else value
        with self.assertRaisesRegex(MemoryReadError,'Contract changed'): read_contracts(db,person)

    def test_nation_changes_and_unknown_types_are_rejected(self):
        db, person = self.capture(29232937)
        db.fm.change = lambda key,count,value: value+8 if key==('read_pointer',person+0x70) and count==2 else value
        with self.assertRaisesRegex(MemoryReadError,'Nationality changed'): read_primary_nationality(db,person)
        db, person = self.capture(29232937)
        for key,value in list(db.fm.values.items()):
            if isinstance(value,bytes) and value.startswith(b'.?AVNATION@db@@'):
                db.fm.values[key] = value.replace(b'NATION',b'FAKEXX')
        with self.assertRaisesRegex(MemoryReadError,'Unsupported object type'): read_primary_nationality(db,person)

    def test_truncated_invalid_dates_and_wages_are_rejected(self):
        with self.assertRaises(MemoryReadError): decode_contract_terms(bytes(CONTRACT_SIZE-1))
        db, person = self.capture(96053555)
        parent = struct.unpack('<QQ',db.fm.values[('read_bytes',person+0xc8,16)])[0]
        original = db.fm.values[('read_bytes',parent,CONTRACT_SIZE)]
        for offset, replacement in [(0x18,b'\xff'*4),(0x3c,b'\0'*4),(0x40,bytes.fromhex('0100e507'))]:
            raw = bytearray(original); raw[offset:offset+4] = replacement
            with self.assertRaises(MemoryReadError): decode_contract_terms(raw)
