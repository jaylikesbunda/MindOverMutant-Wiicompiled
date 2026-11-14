"""Correlate a green (black-texture) frame with texture-cache churn.

The captured wash is a flat (0,150,4): that is what the movie's YUV conversion
produces from a *black* texture (Y = U = V = 0 leaves only the green term), so
the movie quad sampled an empty texture for one draw. The two candidates are

  * the texture cache dropping its contents (``TextureCache::clear()`` runs
    when the cache passes 512 entries, and the movie creates a new entry per
    new frame), and
  * a torn read of the guest's frame buffer while the decoder is still writing
    it.

This stage adds the numbers needed to tell them apart: the probe reports the
frame it captured on, and the cache reports every clear with its entry count.
Read-only observation; it changes no behaviour and is always on (two lines per
clear, at most).
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_GREEN_CAUSE_PROBE_V1'

# ---- 1. the cache reports each clear --------------------------------------
source = runtime / 'src/hle/texture_cache_gl.cpp'
text = source.read_text()
if MARK not in text:
    old = """    cache.clear();
"""
    assert text.count(old) == 1, 'cache clear anchor'
    text = text.replace(old, """    { // """ + MARK + """: a clear invalidates every cached upload.
      static unsigned clears = 0;
      std::cout << "[TextureCache] clear #" << ++clears << " (" << cache.size()
                << " entries)" << std::endl;
    }
    cache.clear();
""", 1)
    source.write_text(text)

# ---- 2. the probe reports its frame number --------------------------------
main = runtime / 'src/core/main.cpp'
text = main.read_text()
if MARK not in text:
    old = '              std::cout << "[GreenWash] #" << written;'
    assert text.count(old) == 1, 'probe log anchor'
    text = text.replace(old,
                        '              std::cout << "[GreenWash] #" << written'
                        ' << " frame=" << tick; // ' + MARK, 1)
    main.write_text(text)
print('Green-frame cause probe installed (cache clears are numbered).')
