"""Match CPU texgen's object-space position and normal on GPU draws.

Qualified draws retain their original position and normal in the vertex input.
The CPU snapshot transform preserves these as object_pos/object_norm and uses
them for texgen after setting pre_xf. Lighting still uses the transformed values.
Reversible with --restore.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
OLD = 'gxTexgen(" << i << ", gxViewPosition, gxViewNormal, gxInputs)'
NEW = 'gxTexgen(" << i << ", vertexPosition, vertexNormal, gxInputs)'
for tree in ('vendor/NWiiRecomp/nWiiRuntime', 'export/nWiiRuntime'):
    path = ROOT / tree / 'src/hle/gx/tev_shader_gen.cpp'
    raw = path.read_bytes().decode('utf-8', 'surrogateescape')
    a, b = (NEW, OLD) if '--restore' in sys.argv else (OLD, NEW)
    if b in raw:
        continue
    if raw.count(a) != 1:
        raise ValueError((str(path), raw.count(a)))
    path.write_bytes(raw.replace(a, b, 1).encode('utf-8', 'surrogateescape'))
print('GPU texgen object space ' + ('removed' if '--restore' in sys.argv else 'installed'))
