"""Strip the remaining native_trace calls from the ported stages (A4/A8).

MoM has no native_trace namespace. Every remaining call in the two ported
stages is a pure diagnostic, so it is replaced by a comment.
"""
from pathlib import Path
import re

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
stages = port / 'tools' / 'stages'

for name in ('fix_early_gpu_signal', 'fix_parallel_decode'):
    path = stages / (name + '.py')
    text = path.read_text()
    out = []
    changed = 0
    for line in text.splitlines(keepends=True):
        if 'native_trace' in line and not line.lstrip().startswith('#'):
            indent = line[:len(line) - len(line.lstrip())]
            out.append(indent + '// NATIVE_MOM_ADAPT_A4: native_trace is not available in MoM.\n')
            changed += 1
        else:
            out.append(line)
    if changed:
        path.write_text(''.join(out))
    print('%s: %d native_trace call(s) removed' % (name, changed))
