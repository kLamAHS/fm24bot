"""Empirical morale labels for the exact supported FM build.

Only UI-confirmed byte values are decoded. Other plausible 1..20 values remain
unavailable until their label is observed; range alone does not validate them.
"""
from .process import MemoryReadError

MORALE_OFFSET = 0x25F
MORALE_LABELS = {
    2: 'Extremely Poor', 6: 'Fairly Poor', 8: 'Fair', 10: 'Okay',
    11: 'Fairly Good', 12: 'Quite Good', 13: 'Good', 14: 'Really Good',
    15: 'Very Good', 16: 'Extremely Good', 17: 'Excellent', 18: 'Superb', 20: 'Perfect',
}

def decode_morale(raw):
    if len(raw) != 1:
        raise MemoryReadError('Incomplete morale byte')
    value = raw[0]
    if value not in MORALE_LABELS:
        raise MemoryReadError(f'Morale value {value} has no validated UI label')
    return MORALE_LABELS[value]
