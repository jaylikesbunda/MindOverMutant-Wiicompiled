"""Refuse the HLE binding where the guest implementation must run.

Dolphin's OSReport signature is the *generic* MWCC varargs prologue
(``stwu r1,-0x80 / mflr r0 / stw r0,0x84(r1) / beq cr1,+0x24 / stfd f1..f8``), so
it matches every variadic function in the binary. This game statically links a
whole printf family whose prologues are instruction-for-instruction identical
(four addresses match), so the scanner named all of them ``OSReport`` and the
recompiler replaced the guest code with the console-print HLE stub.

That stub is not a formatter and it never writes r3, so callers that use the
returned length receive their own argument back. The level loader does

    addi r3,r1,0x10      ; &buf
    li   r4,0x40         ; size
    addi r5,r13,-0x7460  ; format
    bl   0x802D4FAC      ; formatter (HLE'd as OSReport)
    mr   r5,r3           ; len = returned length
    bl   0x802D7CAC      ; build a log record from (buf, len)

so ``len`` became the *destination buffer* (0x90158978, ~2.3 GB). The record
builder then copied that many bytes into address 0x8, wiped guest RAM, left
lr = 0 and sent the guest to pc 0.

Even where the return value is unused, replacing the formatter with a printer
leaves the destination buffer unwritten, so every name/string the game builds
stays empty and later lookups fail (observed: an object expected to hold the
front-end scene graph is NULL, and the named-child search then walks garbage
for ~100 M iterations).

Fix: drop the HLE binding at the addresses where the matched function is really
guest code that must run. Nothing else about the matcher is changed.
"""
import re
from pathlib import Path

port = Path(__file__).resolve().parents[1]
path = port / "vendor" / "NWiiRecomp" / "nWiiAnalyzer" / "src" / "analyzer.cpp"
if not path.exists():
    # apply_stages pre-flights stages against a sandbox that mirrors the runtime
    # only; the analyzer lives outside it, so there is nothing to do there.
    print("nWiiAnalyzer source not present here; nothing to do.")
    raise SystemExit(0)

ADDRESSES = [
    ("0x802D4FACu", "printf family"),
    ("0x80307AC0u", "printf family"),
    ("0x803EB98Cu", "printf family"),
    ("0x803FF650u", "printf family"),
]
SET_BLOCK = "  const std::set<uint32_t> hle_excluded = {\n" + "".join(
    "      %s,  // %s\n" % (a, c) for a, c in ADDRESSES) + "  };\n"

text = path.read_text()
marker = "NATIVE_HLE_ADDRESS_EXCLUSION_V1"

if marker in text:
    # Upgrade the address list in place (the block is ours, so rewriting it is
    # idempotent and keeps the stage the single source of truth).
    new_text = re.sub(r"  const std::set<uint32_t> hle_excluded = \{\n(?:.*\n)*?  \};\n",
                      SET_BLOCK, text, count=1)
    if new_text == text:
        print("HLE address exclusion already up to date (%d addresses)." % len(ADDRESSES))
    else:
        path.write_text(new_text)
        print("HLE address exclusion updated to %d addresses." % len(ADDRESSES))
    raise SystemExit(0)

old = """  SignatureScanner scanner;
  for (auto &pair : functions_) {
    Function &func = pair.second;
    std::vector<uint32_t> opcodes;
    for (const auto &inst : func.instructions) {
      opcodes.push_back(inst.opcode);
    }
    func.hle_hook_name = scanner.match(opcodes);
"""

new = """  SignatureScanner scanner;
  // NATIVE_HLE_ADDRESS_EXCLUSION_V1: the HLE signatures are generic enough to
  // match unrelated MWCC helpers. The OSReport signature is the varargs
  // prologue, so it also names this game's own printf family, whose prologues
  // are byte-identical. HLE'ing a formatter replaces formatting with a console
  // print and leaves r3 untouched, so a caller that uses the returned length
  // gets the destination pointer (a ~2.3 GB memmove over guest RAM); and even
  // where the length is unused, the destination buffer is never filled, so the
  // names the game builds stay empty and later lookups fail. Keep the guest
  // implementation at these addresses.
""" + SET_BLOCK + """  for (auto &pair : functions_) {
    Function &func = pair.second;
    std::vector<uint32_t> opcodes;
    for (const auto &inst : func.instructions) {
      opcodes.push_back(inst.opcode);
    }
    func.hle_hook_name = scanner.match(opcodes);
    if (hle_excluded.count(func.start_address)) {
      if (!func.hle_hook_name.empty())
        std::cout << "[Analyzer] HLE binding refused at 0x" << std::hex
                  << func.start_address << " (" << func.hle_hook_name
                  << "): guest implementation kept" << std::dec << "\\n";
      func.hle_hook_name.clear();
    }
"""

assert text.count(old) == 1, text.count(old)
path.write_text(text.replace(old, new, 1))
print("HLE address exclusion installed (%d addresses)." % len(ADDRESSES))
