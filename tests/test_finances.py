from datetime import date
import json
from pathlib import Path
import struct
import unittest
from unittest.mock import Mock,patch
from bridge.finances import decode_amounts,read_finances
from bridge.process import MemoryReadError

RESEARCH=Path(__file__).resolve().parents[1]/'research'
def captured(label): return json.loads((RESEARCH/('finance-probe-'+label+'.json')).read_text(encoding='utf-8'))

class FinanceTests(unittest.TestCase):
    def test_bank_and_weekly_budget_match_three_clubs(self):
        for label,balance,budget in [('earlier',10909005,78979),('bristol-earlier',-1879153,85928),('arsenal-earlier',56033809,3870924)]:
            amounts=decode_amounts(bytes.fromhex(captured(label)['hex'])[:0x820])
            self.assertEqual(amounts['balance'],balance)
            self.assertEqual(amounts['wage_budget_weekly'],budget)
        self.assertEqual(decode_amounts(bytes.fromhex(captured('earlier')['hex'])[:0x820])['payroll_spending_weekly'],431417)

    def test_transfer_budget_preserves_native_amount_and_records_ui_difference(self):
        # Independent UI transcriptions: no invented low-bit mask or rounding.
        for label,ui,difference in [('earlier',1720250,1),('bristol-earlier',17330,1),('arsenal-earlier',63785379,0)]:
            native=decode_amounts(bytes.fromhex(captured(label)['hex'])[:0x820])['transfer_budget']
            self.assertEqual(native-ui,difference)

    def test_save_change_and_usd_display_conversion(self):
        earlier=captured('earlier'); later=captured('later')
        self.assertNotEqual(earlier['finance_address'],later['finance_address'])
        for row,usd_balance in [(earlier,13628970),(later,13407842)]:
            raw=bytes.fromhex(row['hex']); money=decode_amounts(raw[:0x820])
            usd=next(x for x in row['currencies'] if x['strings'].get('0x18')=='United States Dollar')
            rate=struct.unpack_from('<f',bytes.fromhex(usd['hex']),0x38)[0]
            self.assertEqual(round(money['balance']*rate),usd_balance)
            self.assertEqual(round(money['wage_budget_weekly']*52*rate),5130892)

    def test_incomplete_and_invalid_payroll_rejected(self):
        with self.assertRaises(MemoryReadError):decode_amounts(b'')
        raw=bytearray(bytes.fromhex(captured('earlier')['hex'])[:0x820])
        struct.pack_into('<i',raw,0x810,-1)
        with self.assertRaises(MemoryReadError):decode_amounts(raw)

    def test_owner_and_replaced_finance_objects_rejected(self):
        row=captured('earlier'); raw=bytes.fromhex(row['hex'])[:0x820]
        club=struct.unpack_from('<Q',raw,8)[0]; address=int(row['finance_address'],16)
        fm=Mock(); fm.read_pointer.return_value=address; fm.read_bytes.return_value=raw
        db=Mock(fm=fm); db.current_date.return_value=date(2024,2,7);db.dates.read_raw.return_value=bytes.fromhex('2600e807')
        ctx=Mock(fm=fm,db=db,club=club,club_id=742)
        with patch('bridge.finances.require_type',return_value={'name':'CLUB_FINANCE'}):
            self.assertEqual(read_finances(ctx).balance,10909005)
            ctx.club=club+8
            with self.assertRaisesRegex(MemoryReadError,'owner'):read_finances(ctx)
            ctx.club=club; fm.read_pointer.side_effect=[address,address+8]
            with self.assertRaisesRegex(MemoryReadError,'object changed'):read_finances(ctx)

    def test_budget_change_during_read_rejected(self):
        row=captured('earlier'); raw=bytes.fromhex(row['hex'])[:0x820]
        changed=bytearray(raw);struct.pack_into('<i',changed,0x7CC,0)
        fm=Mock();fm.read_pointer.return_value=int(row['finance_address'],16);fm.read_bytes.side_effect=[raw,bytes(changed)]
        db=Mock(fm=fm); db.current_date.return_value=date(2024,2,7)
        db.dates.read_raw.return_value=bytes.fromhex('2600e807')
        ctx=Mock(fm=fm,db=db,club=struct.unpack_from('<Q',raw,8)[0],club_id=742)
        with patch('bridge.finances.require_type',return_value={}):
            with self.assertRaisesRegex(MemoryReadError,'Finances changed'):read_finances(ctx)
