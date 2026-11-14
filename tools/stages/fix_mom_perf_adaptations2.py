"""Two more MoM adaptations for the ported CotT performance stages.

A8  fix_early_gpu_signal inserts `native_trace::mark(...)` calls into the new
    helper it adds to gx.cpp. MoM has no native_trace namespace, so the marks
    are replaced by a comment. (The earlier A4 edit used an empty replacement,
    which the idempotence guard treated as "already applied" and therefore did
    nothing.)

A9  fix_parse_work_reuse declares `native_refresh_banks` inside the
    display-list cache-hit branch and then replaces a *different*
    `if (!c.raw->snap_pos_bank.empty()) {` with `if (native_refresh_banks) {`.
    In CotT both are in the same scope; in MoM they are not, so the
    replacement does not compile. The original test is kept.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
stages = port / 'tools' / 'stages'


def edit(name, old, new):
    path = stages / (name + '.py')
    text = path.read_text()
    if new in text:
        print('%s: already adapted' % name)
        return
    assert text.count(old) >= 1, (name, old[:60])
    path.write_text(text.replace(old, new))
    print('%s: adapted (%d)' % (name, text.count(old)))


edit('fix_early_gpu_signal',
     '            native_trace::mark("pe.early_signal", 0x45, resolved);',
     '            // NATIVE_MOM_ADAPT_A8: native_trace is not available in MoM.')
edit('fix_early_gpu_signal',
     '            native_trace::mark("pe.early_signal", reg, resolved & 0xffffu);',
     '            // NATIVE_MOM_ADAPT_A8: native_trace is not available in MoM.')

edit('fix_parse_work_reuse',
     "    replace('                    if (!c.raw->snap_pos_bank.empty()) {',\n"
     "            '                    if (native_refresh_banks) {')",
     "    # NATIVE_MOM_ADAPT_A9: in MoM the two occurrences are in different\n"
     "    # scopes, so the declared flag cannot be used at the second site.\n"
     "    # The original test is kept.")
