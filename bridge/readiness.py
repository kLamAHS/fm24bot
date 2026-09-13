"""Position familiarity and physical readiness, validated against FM detail UI."""
import struct
from .process import MemoryReadError

# Byte index 1 is the legacy sweeper slot; no UI field was available to validate it.
POSITION_OFFSETS = {
    'GK':0, 'DL':2, 'DC':3, 'DR':4, 'DM':5, 'ML':6, 'MC':7, 'MR':8,
    'AML':9, 'AMC':10, 'AMR':11, 'ST':12, 'WBL':13, 'WBR':14,
}
READINESS_OFFSET = 0x1F4
READINESS_SIZE = 0x217 - READINESS_OFFSET

def decode_readiness(raw):
    if len(raw) != READINESS_SIZE:
        raise MemoryReadError('Incomplete readiness block')
    sharpness, fatigue, condition = struct.unpack_from('<hhh', raw)
    if not 0 <= condition <= 10000 or not 0 <= sharpness <= 10000:
        raise MemoryReadError('Readiness value outside validated scale')
    ratings = {name:raw[0x208-READINESS_OFFSET+offset] for name,offset in POSITION_OFFSETS.items()}
    if any(not 1 <= value <= 20 for value in ratings.values()):
        raise MemoryReadError('Position rating outside validated scale')
    positions = [name for name,value in ratings.items() if value >= 15]
    if not positions:
        raise MemoryReadError('No recognized accomplished position')
    # Percentages are a documented normalization of FM's 0..10000 UI scale.
    return {
        'positions':positions, 'position_ratings':ratings,
        'condition':condition/100, 'match_sharpness':sharpness/100,
        'evidence':{'condition_raw':condition, 'match_sharpness_raw':sharpness, 'fatigue_raw':fatigue},
    }
