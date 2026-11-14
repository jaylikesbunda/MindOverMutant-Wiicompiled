"""Poll the gamepad every few guest polls instead of on every backward branch.

MoM polls at roughly 7 M/s (the census counts them).  The fast path was already
trimmed, but it still calls ``native_wpad_poll`` on every one of those polls, and
the CPU profile puts 10.8 % of guest CPU in ``process_pending_callbacks`` plus
4.8 % in ``native_gx_publish``.  CotT's poll stages (`fix_poll_credit`,
`fix_poll_empty_publish`, `fix_poll_periodic_check`) exist for the same reason.

This stage gives the gamepad poll a credit: it runs once every eight polls.  A
controller sample is 1-2 ms of real time at 7 M polls/s, so eight polls is far
below one frame and no button edge can be missed - the state is published by the
next poll, and the pad state is level-triggered, so even a press held for a
single poll is still read on the following one.

Nothing else changes: the callback count is still checked on every poll, the
GX publish still runs on every poll, and the periodic clock/diagnostic polls keep
their own cadence.

``NWII_WPAD_POLL_DIV=N`` sets the divisor (1 restores a poll on every call).
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_MOM_WPAD_POLL_CREDIT_V1'
path = runtime / 'src/hle/ios.cpp'
text = path.read_text()
if MARK in text:
    print('wpad poll credit already present.')
    raise SystemExit(0)

anchor = """      native_wpad_poll(ctx);"""
assert text.count(anchor) == 1, 'wpad poll call site'
replacement = """      // """ + MARK + """: the pad state is level-triggered and published to guest
      // memory, so polling it every eighth guest poll cannot drop an edge.
      static const unsigned wpad_div = []() {
        const char *e = std::getenv("NWII_WPAD_POLL_DIV");
        const long v = e ? std::strtol(e, nullptr, 10) : 8;
        return unsigned(v < 1 ? 1 : (v > 64 ? 64 : v));
      }();
      static uint32_t wpad_credit = 0;
      if (wpad_div == 1 || (++wpad_credit % wpad_div) == 0)
        native_wpad_poll(ctx);"""
text = text.replace(anchor, replacement, 1)
path.write_text(text)
print('Gamepad polling takes a credit; every eighth guest poll.')
