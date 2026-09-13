"""Only decode the executable fingerprint actually investigated on this PC."""
import hashlib
from functools import lru_cache
from pathlib import Path
from .process import MemoryReadError

SUPPORTED_SHA256 = 'e1059eee82fa7832188831521a3fa633ec3260dd98d03da48b16661147e3ab48'

@lru_cache(maxsize=4)
def _hash_exe(path, size, mtime):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def require_supported_build(module):
    path = Path(module.path)
    stat = path.stat()
    if _hash_exe(str(path), stat.st_size, stat.st_mtime_ns) != SUPPORTED_SHA256:
        raise MemoryReadError('Executable is not the validated FM24 build; investigate before decoding')
