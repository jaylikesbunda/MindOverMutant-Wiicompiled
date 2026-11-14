"""Tell a torn texture read apart from a stale clear.

The green flash is the movie converted from a black texture. Two mechanisms
produce a black texture here, and they need different fixes:

  * a *torn* read - the guest decoder is writing the plane while the host reads
    it, so the sampled bytes change underneath the read. A snapshot taken at
    hand-off fixes this, because the copy happens before the guest reuses the
    buffer.
  * a *stable* clear - the guest zeroed the plane and had not started writing
    it. A snapshot does not help; the hand-off itself is too late, and the host
    would have to keep the previous frame.

This probe answers which one it is. On every texture upload it samples the
guest bytes (`zero` fraction), keeps the previous sample for the same address,
and when a plane goes from non-black to black it samples again 300 us later.
If the second sample is non-zero the read was torn; if it is still zero the
plane really was cleared.

Opt-in: ``NWII_TEX_ZERO_PROBE=1``. Read-only: it samples guest memory, sleeps
on the render thread only inside the rare transition, and changes nothing the
renderer draws.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
source = runtime / 'src/hle/texture_cache_gl.cpp'
MARK = 'NATIVE_TEX_ZERO_PROBE_V1'
text = source.read_text()
if MARK in text:
    print('Texture-zero probe already present.')
    raise SystemExit(0)

anchor = """    if (cache.size() > 512)
        clear();
"""
assert text.count(anchor) == 1, 'cache miss anchor'

probe = """    { // """ + MARK + """: torn read or stable clear?
      static const bool probe = std::getenv("NWII_TEX_ZERO_PROBE") != nullptr;
      if (probe) {
        static std::unordered_map<uint32_t, float> previous;
        auto zero_fraction = [](const uint8_t* p, uint32_t n) -> float {
          if (!p || !n) return -1.f;
          const uint32_t step = n > 4096 ? n / 4096 : 1;
          uint32_t zeros = 0, seen = 0;
          for (uint32_t o = 0; o < n; o += step) { zeros += (p[o] == 0); ++seen; }
          return seen ? float(zeros) / float(seen) : -1.f;
        };
        const uint8_t* live = ctx.mmu.get_ptr(stage.base_addr);
        const float now = zero_fraction(live, size);
        auto seen = previous.find(stage.base_addr);
        const float before = seen == previous.end() ? -1.f : seen->second;
        previous[stage.base_addr] = now;
        if (before >= 0.f && before < 0.05f && now > 0.98f) {
          std::this_thread::sleep_for(std::chrono::microseconds(300));
          const float later = zero_fraction(ctx.mmu.get_ptr(stage.base_addr), size);
          std::cout << "[TexZero] addr=0x" << std::hex << stage.base_addr << std::dec
                    << " size=" << size << " " << stage.width << "x" << stage.height
                    << " fmt=" << (int)stage.format << " hash=0x" << std::hex << hash
                    << std::dec << " before=" << before << " now=" << now
                    << " after300us=" << later
                    << " efb=" << find_efb_texture(stage.base_addr) << std::endl;
        }
      }
    }

"""
text = text.replace(anchor, probe + anchor, 1)
if '#include <thread>' not in text:
    text = text.replace('#include <vector>', '#include <vector>\n#include <thread>\n#include <chrono>', 1)
source.write_text(text)
print('Texture-zero probe installed.')
