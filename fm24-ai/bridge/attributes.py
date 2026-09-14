"""The 47 visible player attributes, validated against six FM profiles.

These are database attributes; they do not apply the human manager's scouting
mask. Seven other bytes in this block are intentionally not exposed here.
"""
from .process import MemoryReadError

ATTRIBUTE_OFFSET = 0x217
ATTRIBUTE_SIZE = 54
ATTRIBUTES = {
    'crossing': 0x00, 'dribbling': 0x01, 'finishing': 0x02,
    'heading': 0x03, 'long_shots': 0x04, 'marking': 0x05,
    'off_the_ball': 0x06, 'passing': 0x07, 'penalty_taking': 0x08,
    'tackling': 0x09, 'vision': 0x0A, 'handling': 0x0B,
    'aerial_reach': 0x0C, 'command_of_area': 0x0D, 'communication': 0x0E,
    'kicking': 0x0F, 'throwing': 0x10, 'anticipation': 0x11,
    'decisions': 0x12, 'one_on_ones': 0x13, 'positioning': 0x14,
    'reflexes': 0x15, 'first_touch': 0x16, 'technique': 0x17,
    'flair': 0x1A, 'corners': 0x1B, 'teamwork': 0x1C,
    'work_rate': 0x1D, 'long_throws': 0x1E, 'eccentricity': 0x1F,
    'rushing_out': 0x20, 'punching_tendency': 0x21,
    'acceleration': 0x22, 'free_kick_taking': 0x23, 'strength': 0x24,
    'stamina': 0x25, 'pace': 0x26, 'jumping_reach': 0x27,
    'leadership': 0x28, 'balance': 0x2A, 'bravery': 0x2B,
    'aggression': 0x2D, 'agility': 0x2E, 'natural_fitness': 0x32,
    'determination': 0x33, 'composure': 0x34, 'concentration': 0x35,
}


def decode_attributes(block):
    if len(block) != ATTRIBUTE_SIZE:
        raise MemoryReadError('Invalid attribute block length')
    raw = {name: block[offset] for name, offset in ATTRIBUTES.items()}
    if any(value < 1 or value > 100 for value in raw.values()):
        raise MemoryReadError('Attribute byte outside validated range')
    # FM clamps the displayed scale at 1. Raw 1 and 2 were independently
    # checked on Jamie Mills and Kai Forsyth without editing their values.
    return {name: max(1, (value + 2) // 5) for name, value in raw.items()}
