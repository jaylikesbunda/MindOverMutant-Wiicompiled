r"""Idle wake-cause report for the guest idle park (compact MoM port of CotT fix_wait_cause_trace.py, NATIVE_IDLE_RESUME_CAUSE_V1).

Marker: NATIVE_MOM_IDLE_CAUSE_V1. Diagnostic only, off unless NWII_IDLE_CAUSE=1; it never changes delivery.

After each park in the guest idle loop (NATIVE_OS_IDLE_WAIT_MOM_V1) it records how long the park lasted and what was
pending when it returned: the masked PI interrupt bits (PI_INTSR & PI_INTMR: DI=bit2, AI=5, DSP=6, VI=8, PE token=9,
PE finish=10, CP=11, ...), a vblank, queued callbacks, or nothing (timeout / timer). Every 5 s of wall time it prints the
totals per cause ([IDLE_CAUSE] lines on stdout) and resets them.

Edits src/hle/ios.cpp in vendor and export. Idempotent; --restore is exact.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARK = 'NATIVE_MOM_IDLE_CAUSE_V1'
ROOT = Path(__file__).resolve().parents[2]
FILES = [ROOT / 'vendor/NWiiRecomp/nWiiRuntime/src/hle/ios.cpp', ROOT / 'export/nWiiRuntime/src/hle/ios.cpp']
OLD = ('        native_idle::wait(ticks, ctx.tb_freq, pending);\n'
       '        native_perf_phase_idle.stop(); // NATIVE_PERF_PHASE_V1_IDLE\n')
NEW = ('        static const bool idle_cause_on = std::getenv("NWII_IDLE_CAUSE") != nullptr; // ' + MARK + '\n'
       '        const auto idle_cause_t0 = idle_cause_on ? std::chrono::steady_clock::now() : std::chrono::steady_clock::time_point{};\n'
       '        native_idle::wait(ticks, ctx.tb_freq, pending);\n'
       '        if (idle_cause_on) { // ' + MARK + ': what ended this park, and how long it took\n'
       '          static std::map<std::string, std::pair<uint64_t, double>> causes;\n'
       '          static auto report_t0 = std::chrono::steady_clock::now();\n'
       '          const auto t1 = std::chrono::steady_clock::now();\n'
       '          const uint32_t irq = hw::pi_intsr.load(std::memory_order_acquire) & hw::pi_intmr.load(std::memory_order_relaxed);\n'
       '          std::string key;\n'
       '          if (irq) { char b[32]; std::snprintf(b, sizeof b, "irq=0x%x", irq); key = b; }\n'
       '          if (ctx.vblank_pending.load(std::memory_order_acquire)) key += key.empty() ? "vblank" : "+vblank";\n'
       '          if (ctx.pending_cb_count.load(std::memory_order_acquire)) key += key.empty() ? "callback" : "+callback";\n'
       '          if (key.empty()) { // which bound ended the park\n'
       '            const uint64_t ev = EventScheduler::get().next_due(), now0 = ctx.read_timebase();\n'
       '            const bool dec_due = ctx.dec_irq_pending && now0 - ctx.dec_written_tb >= ctx.dec_value;\n'
       '            key = dec_due ? "timer:decrementer" : (ev <= now0 ? "timer:scheduler_event" : "timer:park_cap");\n'
       '            if (ctx.dec_irq_pending && !dec_due) key += "(dec_armed)";\n'
       '          }\n'
       '          auto& c = causes[key]; ++c.first; c.second += std::chrono::duration<double, std::milli>(t1 - idle_cause_t0).count();\n'
       '          if (t1 - report_t0 >= std::chrono::seconds(5)) {\n'
       '            const double window = std::chrono::duration<double, std::milli>(t1 - report_t0).count();\n'
       '            for (auto& kv : causes)\n'
       '              std::cout << "[IDLE_CAUSE] " << kv.first << " parks=" << kv.second.first << " ms=" << kv.second.second\n'
       '                        << " (" << 100.0 * kv.second.second / window << "% of wall)" << std::endl;\n'
       '            causes.clear(); report_t0 = t1;\n'
       '          }\n'
       '        }\n'
       '        native_perf_phase_idle.stop(); // NATIVE_PERF_PHASE_V1_IDLE\n')
INC_OLD = '#include <iostream>\n'
INC_NEW = '#include <iostream>\n#include <map> // ' + MARK + '\n#include <string> // ' + MARK + '\n'


def main():
    restore = '--restore' in sys.argv
    for p in FILES:
        raw = p.read_bytes().decode('utf-8', errors='surrogateescape')
        crlf = '\r\n' in raw
        t = raw.replace('\r\n', '\n')
        if restore:
            n = t.replace(NEW, OLD, 1).replace(INC_NEW, INC_OLD, 1)
        elif MARK in t:
            continue
        else:
            if t.count(OLD) != 1 or INC_OLD not in t:
                print('fix_idle_cause_mom: anchor not found in %s' % p); sys.exit(1)
            n = t.replace(OLD, NEW, 1).replace(INC_OLD, INC_NEW, 1)
        if n != t:
            p.write_bytes((n.replace('\n', '\r\n') if crlf else n).encode('utf-8', errors='surrogateescape'))
    print('idle cause %s.' % ('removed' if restore else 'installed'))


if __name__ == '__main__':
    main()
