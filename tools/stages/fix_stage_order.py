"""Move the three performance-tooling stages above the quality lane.

The present-metrics stage (07) anchors on the block that the phase-trace
stage installs around SDL_GL_SwapWindow, so the phase stage has to run first.
The stage runner applies the list in order, so the order in
tools/apply_stages.py is the dependency order.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
path = port / 'tools' / 'apply_stages.py'
text = path.read_text()
perf = ['    "fix_perf_census.py",\n', '    "fix_perf_phase_trace.py",\n', '    "fix_perf_frame_trace.py",\n']
for line in perf:
    if line in text:
        text = text.replace(line, '', 1)
anchor = '    # --- quality lane (CotT work re-derived against this tree) -----------\n'
if anchor in text and 'fix_perf_phase_trace.py' not in text.split(anchor)[0]:
    text = text.replace(anchor, ''.join(perf) + anchor, 1)
    path.write_text(text)
    print('performance stages moved above the quality lane')
else:
    print('already ordered')
