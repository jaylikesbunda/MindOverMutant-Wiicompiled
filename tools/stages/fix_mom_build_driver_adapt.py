"""Remove CotT's build-driver integration from the two ported stages.

CotT's `tools/build_native.py` is its build orchestrator; every stage that
needs a verification run registers itself there. MoM has no such driver -
`tools/apply_stages.py` is the stage list - so the registration is dropped.
"""
from pathlib import Path
import re

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
stages = port / 'tools' / 'stages'

NOTE = ("# NATIVE_MOM_ADAPT_A5: CotT's build driver (tools/build_native.py) does\n"
        "# not exist here; tools/apply_stages.py is MoM's stage list.\n")

path = stages / 'fix_vertex_reuse.py'
text = path.read_text()
old = """build = port/'tools/build_native.py'
s = build.read_text()
if "run('native-vertex-reuse-stage'" not in s:
    anchor = "run('native-game-build',"
    assert s.count(anchor)==1
    s = s.replace(anchor,"run('native-vertex-reuse-stage',[sys.executable,PORT/'tools/fix_vertex_reuse.py'])\\n"+anchor,1)
    build.write_text(s)
"""
if old in text:
    path.write_text(text.replace(old, NOTE))
    print('fix_vertex_reuse: build-driver registration dropped.')

path = stages / 'fix_indexed_batches.py'
text = path.read_text()
pattern = re.compile(r"path=p/'tools/build_native\.py';s=path\.read_text\(\)\n.*?path\.write_text\(s\.replace\(anchor,.*?\n", re.S)
match = pattern.search(text)
if match:
    path.write_text(text[:match.start()] + NOTE + text[match.end():])
    print('fix_indexed_batches: build-driver registration dropped.')
