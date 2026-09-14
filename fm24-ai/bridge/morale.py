"""Empirical morale labels for the exact supported FM build.

All 20 labels were observed in FM's English UI across Wycombe and Gretna players.
Values outside that empirically validated domain remain unavailable.
"""
from .process import MemoryReadError

MORALE_OFFSET = 0x25F
MORALE_LABELS = {
    1: 'Abysmal', 2: 'Extremely Poor', 3: 'Very Poor', 4: 'Poor',
    5: 'Quite Poor', 6: 'Fairly Poor', 7: 'Slightly Poor', 8: 'Fair',
    9: 'Fairly Okay', 10: 'Okay',
    11: 'Fairly Good', 12: 'Quite Good', 13: 'Good', 14: 'Really Good',
    15: 'Very Good', 16: 'Extremely Good', 17: 'Excellent', 18: 'Superb',
    19: 'Exceptional', 20: 'Perfect',
}

def decode_morale(raw):
    if len(raw) != 1:
        raise MemoryReadError('Incomplete morale byte')
    value = raw[0]
    if value not in MORALE_LABELS:
        raise MemoryReadError(f'Morale value {value} has no validated UI label')
    return MORALE_LABELS[value]
