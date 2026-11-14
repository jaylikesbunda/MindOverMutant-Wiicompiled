"""Read broadcast operands before either paired-single destination is written.

ps_muls0/ps_madds0 with destination == scalar register otherwise use the new
lane-zero result as lane one's multiplier, corrupting matrix calculations.
tools/tests/paired_single.cpp covers all source/destination aliases.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]
path = port / "build/nwii-full-source/nWiiRuntime/include/runtime/cpu_context.h"
text = path.read_text()
if "NATIVE_PS_BROADCAST_ALIAS_V1" not in text:
    for name, args, expression in (
        ("ps_muls0", "uint32_t D, uint32_t A, uint32_t C", "fpr[A] * fpr[C]"),
        ("ps_madds0", "uint32_t D, uint32_t A, uint32_t C, uint32_t B",
         "std::fma(fpr[A], fpr[C], fpr[B])"),
    ):
        second = expression.replace("fpr[A]", "ps1[A]").replace("fpr[B]", "ps1[B]")
        old = f"  inline void {name}({args}) {{\n    fpr[D] = {expression};\n    ps1[D] = {second};\n  }}"
        new = f"""  inline void {name}({args}) {{
    // NATIVE_PS_BROADCAST_ALIAS_V1: D may alias the broadcast source C.
    const double lane0 = {expression};
    const double lane1 = {second};
    fpr[D] = lane0;
    ps1[D] = lane1;
  }}"""
        assert text.count(old) == 1, name
        text = text.replace(old, new, 1)
    path.write_text(text)
print("Paired-single scalar broadcasts read operands before writing results.")
