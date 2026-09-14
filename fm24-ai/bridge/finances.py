"""Native GBP finance fields for the validated full CLUB_FINANCE layout."""
import struct
from .dates import decode_game_date
from .process import MemoryReadError
from .rtti import require_type
from structures.finances import Finances

FINANCE_POINTER=0x150
FIELDS={'balance':0x14,'transfer_budget':0x7CC,'wage_budget_weekly':0x810,'payroll_spending_weekly':0x81C}

def decode_amounts(raw):
    if len(raw)!=0x820: raise MemoryReadError('Incomplete finance block')
    result={name:struct.unpack_from('<i',raw,offset)[0] for name,offset in FIELDS.items()}
    # Negative bank and transfer balances are valid; do not clamp them to zero.
    if result['wage_budget_weekly']<0 or result['payroll_spending_weekly']<0:
        raise MemoryReadError('Unvalidated payroll sentinel or negative amount')
    return result

def read_finances(context):
    context.check(); db=context.db; fm=context.fm
    db.current_date()  # Resolve and validate the clock before reading the block.
    game_raw=db.dates.read_raw(); as_of=decode_game_date(game_raw)
    address=fm.read_pointer(context.club+FINANCE_POINTER)
    info=require_type(db,address,'.?AVCLUB_FINANCE@db@@')
    raw=fm.read_bytes(address,0x820)
    if struct.unpack_from('<Q',raw,8)[0]!=context.club:
        raise MemoryReadError('Finance owner does not match the current club')
    values=decode_amounts(raw)
    # Compare the owner, vtable and relevant fields. Unrelated accounting updates
    # need not invalidate a budget observation if all returned values stay fixed.
    after=fm.read_bytes(address,0x820)
    if raw[:16]!=after[:16] or values!=decode_amounts(after):
        raise MemoryReadError('Finances changed during read')
    if fm.read_pointer(context.club+FINANCE_POINTER)!=address or require_type(db,address,'.?AVCLUB_FINANCE@db@@')!=info:
        raise MemoryReadError('Finance object changed during read')
    context.check()
    if game_raw!=db.dates.read_raw(): raise MemoryReadError('Game time changed during finance read')
    return Finances(context.club_id,'GBP',as_of=as_of.isoformat(),**values)
