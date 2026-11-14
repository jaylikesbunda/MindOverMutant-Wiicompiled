"""Make the ported gl_raster_shadow stage importable from this repo.

CotT's stage lives in tools/ and does `from gl_state_shadow import ...`, which
works because the stage is executed from that directory. MoM's runner copies
each stage to logs/_stage_<name>.py and runs it with the repository root as the
working directory, so the helper module is not on sys.path. The stage now
looks for the helper next to itself and in tools/.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
path = port / 'tools' / 'stages' / 'fix_gl_raster_shadow.py'
text = path.read_text()
old = "from gl_state_shadow import p,specs,header"
new = ("import sys, os\n"
       "# NATIVE_MOM_ADAPT_A6: MoM's runner executes the stage from the repository\n"
       "# root, so the helper module in tools/ has to be put on sys.path.\n"
       "_here = os.path.dirname(os.path.abspath(__file__))\n"
       "for _cand in (_here, os.path.join(_here, '..'), os.path.join(_here, '..', 'tools')):\n"
       "    if os.path.isfile(os.path.join(_cand, 'gl_state_shadow.py')):\n"
       "        sys.path.insert(0, _cand)\n"
       "        break\n"
       "from gl_state_shadow import p,specs,header")
if new in text:
    print('already adapted')
elif text.count(old) == 1:
    path.write_text(text.replace(old, new))
    print('fix_gl_raster_shadow: import path fixed.')
else:
    raise SystemExit('anchor not found')
