"""Key the texture cache on texture identity, evict LRU, and stop validating EFB
copies with a whole-snapshot memcmp.

Two measured problems in ``src/hle/texture_cache_gl.cpp``:

1.  ``TextureKey`` included the texture's *content* hash.  A texture whose
    content changes is therefore always a miss: the movie uploads a new frame
    every frame, so every frame got a new cache entry, a ``glGenTextures`` and a
    fresh ``glTexImage2D``.  The 512-entry ceiling was reached in about ten
    seconds, and ``if (cache.size() > 512) clear();`` then deleted *every* GL
    texture - including the ones the current frame still draws - and every
    following draw re-decoded and re-uploaded from scratch.  That is the
    rhythmic hitch (an overdue ``[TextureCache] clear #N`` roughly every ~14 s at
    513 entries), and it also churned ``mip_ready``, because each new GL name
    needed its own chain.

2.  ``NativeEfbGuestUnchanged`` validated an EFB-derived texture with
    ``std::memcmp`` of the entire guest snapshot - 640x528x4 = 1.35 MB - on every
    ``get_texture`` that lands on an EFB address.  CotT removed exactly this cost
    with OS page-write tracking (``native-port/tools/fix_ram_write_watch.py``:
    "Dirty-page tracking for texture sources: skip the full-texture memcmp when
    the OS says no page of the texture was written").  MoM has no such
    machinery: guest RAM here is ``std::vector<uint8_t>`` (``MMU::mem1/mem2``),
    not ``VirtualAlloc(MEM_WRITE_WATCH)``; the only ``MEM_WRITE_WATCH`` in the
    tree is ``overlays/static_ram_base.cpp`` for the fixed-base build, and
    nothing in the runtime calls ``GetWriteWatch``.  A sound version would need
    a write barrier on *every* path that stores into guest RAM (the emulated
    ``MMU::write*`` plus every HLE ``memcpy`` through ``MMU::get_ptr``), which is
    a much larger change than this stage.  So this stage uses the per-page
    fingerprint fallback and is explicit about it (see "Soundness").

The driver is 25-45 % of host CPU and the goal of this stage is to remove the
hitches (the periodic clear/stall and the per-frame allocate/free), not to move
the mean: hitches are what the eye sees, and the mean cost of a hash hit barely
changes.

What this stage installs
------------------------

``NATIVE_MOM_TEXTURE_KEY_V2``

* ``TextureKey`` is the *identity* of the texture - ``addr, width, height,
  format, tlut_offset, tlut_format`` - and the content hash moved into
  ``TextureCache::Entry::data_hash``.  Consequently the map holds at most one
  entry, and one GL texture, per distinct identity.
* On a hit with the same hash: return as today (one extra ``uint64_t`` compare).
* On a hit with a different hash: re-decode and ``glTexSubImage2D`` level 0 of
  the texture the entry already owns.  The dimensions come from the same key,
  so the storage is the right size and no reallocation, ``glGenTextures`` or
  ``glDeleteTextures`` happens.  The entry's hash is updated, ``GL_TEXTURE_MAX_LEVEL``
  is reset to 0 (the same "level 0 only until ``ensure_mips``" invariant the
  upload path installs) and the GL name is dropped from ``mip_ready`` so the
  chain is rebuilt from the new texels before a mip filter can sample it.
  If the decode returned clamped dimensions (the ``NATIVE_TEXTURE_ALLOC_GUARD_V1``
  invalid-size path) the old object is deleted and the upload path runs instead,
  exactly as before.
* Overflow evicts LRU entries down to 75 % of the ceiling
  (``evict_lru()``: 512 -> 384) instead of ``clear()``.  Recency is a
  monotonically increasing ``tick`` stamped on each use; no list surgery on the
  hot path.  ``evict_lru`` never deletes a GL object owned by the EFB copy path
  (any name in ``efb_textures`` for a registered address), and never deletes an
  entry stamped within the last two ticks (the draws being set up now).  Every
  deleted name is erased from ``mip_ready`` because GL reuses deleted names.

``NATIVE_MOM_EFB_PAGE_CHECK_V1``

* ``register_efb_texture`` additionally stores one 32-bit fingerprint per
  4 KiB page of the snapshot range.  A lookup recomputes those fingerprints and
  only reports "unchanged" when *every* page matches; the first mismatch returns
  "changed" immediately.
* Default fingerprint uses 16 evenly spaced 32-bit words per page (64 of 4096
  bytes, i.e. 1/64 of the snapshot traffic: ~21 KB instead of 1.35 MB).
* ``NWII_EFB_PAGE_CHECK_FULL=1`` fingerprints every word of every page: then
  "all pages match" is byte equality up to a 32-bit collision per page, i.e. as
  strong as the memcmp it replaces, at memcmp-like cost (escape hatch, not the
  default).

Soundness
---------

The *mismatch* direction is sound: a page fingerprint can only differ if a
sampled byte differs, so "changed" is never a false report (up to hash
collisions, which the content hash already relies on).

The *match* direction is **heuristic, not sound**: a write that lands inside
pages of the range without touching any of the 16 sampled words of *every* page
it touches would be reported "unchanged" and a stale texture would be drawn.
The sample is complete over the address range (every page is checked, there is
no unchecked page), which is the important difference from a global sparse
sample.  In this title the invalidation this check exists for is a whole-texture
overwrite of a reused address (a new texture or a new EFB copy at the same
address), which changes essentially every page and is caught.  A sub-word change
in the 1.6 % of each page that is not sampled is the residual risk.  For any
investigation where a stale EFB texture is suspected, run with
``NWII_NO_EFB_PAGE_CHECK=1`` (full memcmp, the previous behaviour) and compare;
``NWII_EFB_PAGE_PROBE=1`` prints which page first disagreed.

Runtime switches (same binary A/B)
----------------------------------

``NWII_NO_TEXTURE_KEY_V2=1``
    Restores the content-keyed cache in effect: a hash mismatch is treated as a
    miss, the previous GL texture is deleted and a new one is created, and the
    cache is flushed wholesale with ``clear()`` above 512 entries - i.e. the
    original behaviour and the original churn.
``NWII_NO_EFB_PAGE_CHECK=1``
    Restores the full-snapshot ``std::memcmp`` for EFB validation.
``NWII_EFB_PAGE_CHECK_FULL=1``
    Full-page fingerprints instead of sampled ones (see above).
``NWII_TEX_KEY_PROBE=1``
    Prints every 300th in-place re-upload (``[TextureCache] refresh ...``).
``NWII_EFB_PAGE_PROBE=1``
    Prints the first 100 EFB fingerprint decisions.

Kept unchanged: ``register_efb_texture`` / ``find_efb_texture``, the
``decode_texture`` call and its invalid-size guard, ``ensure_mips`` and the
``mip_ready`` invalidation, the ``NWII_EFB_CACHE_PROBE`` and
``NWII_TEX_ZERO_PROBE`` probes, the ``NWII_GREEN_CAUSE_PROBE_V1`` clear counter
and ``NWII_LEGACY_EFB_CACHE``.  Note that ``NWII_TEX_ZERO_PROBE`` still runs only
on an identity *miss*; with V2 an in-place refresh is not a miss, so run that
probe with ``NWII_NO_TEXTURE_KEY_V2=1`` to keep its old coverage.

Order: this stage anchors on the text that ``fix_efb_texture_freshness.py``,
``fix_texture_alloc_guard.py``, ``03_fix_texture_mipmaps_mom.py`` and
``fix_mipmap_first_draw.py`` install, so it must run after all four.  Idempotent
(skips when the marker is present).
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_MOM_TEXTURE_KEY_V2'
PAGE_MARK = 'NATIVE_MOM_EFB_PAGE_CHECK_V1'


def sub(text, old, new, what):
    assert text.count(old) == 1, 'anchor %s' % what
    return text.replace(old, new, 1)


def write(path, text):
    with open(path, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(text)


# ---- header: identity key, per-identity entry, LRU state --------------------
header = runtime / 'include/runtime/texture_cache_gl.h'
text = header.read_text()
if MARK not in text:
    old = '''struct TextureKey {
    uint32_t addr;
    uint32_t width;
    uint32_t height;
    uint32_t format;
    uint32_t tlut_offset;
    uint32_t tlut_format;
    uint32_t data_hash;

    bool operator==(const TextureKey& other) const {
        return addr == other.addr && width == other.width &&
               height == other.height && format == other.format &&
               tlut_offset == other.tlut_offset &&
               tlut_format == other.tlut_format &&
               data_hash == other.data_hash;
    }
};

struct TextureKeyHash {
    std::size_t operator()(const TextureKey& k) const {
        std::size_t h = k.addr;
        h = h * 31 + k.width;
        h = h * 31 + k.height;
        h = h * 31 + k.format;
        h = h * 31 + k.tlut_offset;
        h = h * 31 + k.tlut_format;
        h = h * 31 + k.data_hash;
        return h;
    }
};'''
    new = '''// ''' + MARK + ''': keyed by the *identity* of a texture, not by its content.
// The content hash lives in TextureCache::Entry, so a rewritten texture updates
// the GL object it already owns instead of allocating a new one (one entry per
// distinct texture, however often its pixels change).
struct TextureKey {
    uint32_t addr;
    uint32_t width;
    uint32_t height;
    uint32_t format;
    uint32_t tlut_offset;
    uint32_t tlut_format;

    bool operator==(const TextureKey& other) const {
        return addr == other.addr && width == other.width &&
               height == other.height && format == other.format &&
               tlut_offset == other.tlut_offset &&
               tlut_format == other.tlut_format;
    }
};

struct TextureKeyHash {
    std::size_t operator()(const TextureKey& k) const {
        std::size_t h = k.addr;
        h = h * 31 + k.width;
        h = h * 31 + k.height;
        h = h * 31 + k.format;
        h = h * 31 + k.tlut_offset;
        h = h * 31 + k.tlut_format;
        return h;
    }
};'''
    text = sub(text, old, new, 'TextureKey / TextureKeyHash')

    old = '    std::unordered_map<TextureKey, GLuint, TextureKeyHash> cache;\n'
    new = '''    // ''' + MARK + ''': one entry (and one GL texture) per texture identity.
    struct Entry {
        GLuint tex = 0;
        uint32_t data_hash = 0;  // hash of the texels/TLUT currently uploaded
        uint32_t width = 0;      // dimensions of the allocation (from the key)
        uint32_t height = 0;
        uint64_t stamp = 0;      // tick of the last use; the LRU order
    };
    static const std::size_t kCacheHighWater = 512;  // evict above this
    static const std::size_t kCacheLowWater = 384;   // ... down to 75 %
    std::unordered_map<TextureKey, Entry, TextureKeyHash> cache;
    uint64_t tick = 0;  // monotonic use counter, never wrapped in practice
    void evict_lru();
'''
    text = sub(text, old, new, 'cache member')
    write(header, text)

# ---- cpp ------------------------------------------------------------------
source = runtime / 'src/hle/texture_cache_gl.cpp'
text = source.read_text()
if MARK not in text:
    # clear(): the entry value is no longer a bare GLuint.
    old = '''    for (auto& pair : cache) {
        glDeleteTextures(1, &pair.second);
    }
'''
    new = '''    for (auto& pair : cache) {
        glDeleteTextures(1, &pair.second.tex); // ''' + MARK + '''
    }
'''
    text = sub(text, old, new, 'clear() delete loop')

    # clear() left the EFB-owned textures alone on purpose; the same reasoning is
    # what evict_lru() has to respect.
    old = '''    // efb_textures deliberately survive: their GL objects are owned by the
    // renderer's copy path and re-filled every EFB copy.
}
'''
    new = '''    // efb_textures deliberately survive: their GL objects are owned by the
    // renderer's copy path and re-filled every EFB copy.
}

void TextureCache::evict_lru() { // ''' + MARK + '''
    // Drop the least recently used entries down to the low-water mark instead of
    // deleting every texture, which forces the rest of the frame to re-decode and
    // re-upload everything it still draws.
    std::vector<std::pair<uint64_t, TextureKey>> order;
    order.reserve(cache.size());
    for (const auto& pair : cache) order.emplace_back(pair.second.stamp, pair.first);
    std::sort(order.begin(), order.end(),
              [](const std::pair<uint64_t, TextureKey>& a,
                 const std::pair<uint64_t, TextureKey>& b) { return a.first < b.first; });
    // The EFB copy path owns its GL objects: never delete one, even if a decoded
    // entry somehow shares the name. Collected once per eviction pass (rare).
    std::unordered_set<GLuint> efb_owned;
    efb_owned.reserve(efb_textures.size());
    for (const auto& pair : efb_textures)
        if (pair.second) efb_owned.insert(pair.second);
    unsigned removed = 0;
    for (const auto& item : order) {
        if (cache.size() <= kCacheLowWater) break;
        auto it = cache.find(item.second);
        if (it == cache.end()) continue;
        if (efb_owned.count(it->second.tex)) continue;   // the EFB texture of a live address
        if (it->second.stamp + 2 >= tick) continue;      // used by the draws being set up now
        glDeleteTextures(1, &it->second.tex);
        mip_ready.erase(it->second.tex);                 // GL reuses deleted names
        cache.erase(it);
        ++removed;
    }
    if (removed)
        std::cout << "[TextureCache] evict " << removed << " entries, kept "
                  << cache.size() << std::endl;
}
'''
    text = sub(text, old, new, 'evict_lru definition')

    # EFB validation: per-page fingerprints next to the snapshot.
    old = 'struct NativeEfbGuest { std::vector<uint8_t> bytes; };\n'
    new = '''// ''' + PAGE_MARK + ''': one fingerprint per 4 KiB page of the snapshot, so a
// validation reads ~1/64 of the snapshot instead of all of it. Heuristic - see
// the stage docstring ("Soundness").
struct NativeEfbGuest { std::vector<uint8_t> bytes; std::vector<uint32_t> pages; };
static const size_t kEfbPageBytes = 4096;
static uint32_t NativeEfbPageHash(const uint8_t* page, size_t bytes) {
    // 16 evenly spaced words per page; NWII_EFB_PAGE_CHECK_FULL=1 uses every word.
    static const bool full = std::getenv("NWII_EFB_PAGE_CHECK_FULL") != nullptr;
    const size_t step = full ? 4 : 256;
    uint32_t h = 2166136261u;
    for (size_t off = 0; off < bytes; off += step) {
        uint32_t w = 0;
        const size_t n = bytes - off < 4 ? bytes - off : 4;
        std::memcpy(&w, page + off, n);
        h = (h ^ w) * 16777619u;
    }
    return h;
}
static size_t NativeEfbPageCount(size_t bytes) {
    return (bytes + kEfbPageBytes - 1) / kEfbPageBytes;
}
'''
    text = sub(text, old, new, 'NativeEfbGuest')

    old = '''static bool NativeEfbGuestUnchanged(CPUContext& ctx, uint32_t address) {
    static const bool legacy = std::getenv("NWII_LEGACY_EFB_CACHE") != nullptr;
    if (legacy) return true;
    auto it = s_efb_guest.find(address & 0x3fffffffu);
    return it != s_efb_guest.end() && !it->second.bytes.empty() &&
           NativeEfbSpan(ctx, address, it->second.bytes.size()) &&
           std::memcmp(ctx.mmu.get_ptr(address), it->second.bytes.data(), it->second.bytes.size()) == 0;
}
'''
    new = '''static bool NativeEfbGuestUnchanged(CPUContext& ctx, uint32_t address) {
    static const bool legacy = std::getenv("NWII_LEGACY_EFB_CACHE") != nullptr;
    if (legacy) return true;
    auto it = s_efb_guest.find(address & 0x3fffffffu);
    if (it == s_efb_guest.end() || it->second.bytes.empty()) return false;
    const size_t bytes = it->second.bytes.size();
    if (!NativeEfbSpan(ctx, address, bytes)) return false;
    const uint8_t* live = ctx.mmu.get_ptr(address);
    if (!live) return false;
    // NWII_NO_EFB_PAGE_CHECK=1: the full 1.35 MB compare, exactly as before.
    static const bool page_check = std::getenv("NWII_NO_EFB_PAGE_CHECK") == nullptr;
    if (!page_check || it->second.pages.size() != NativeEfbPageCount(bytes))
        return std::memcmp(live, it->second.bytes.data(), bytes) == 0;
    static const bool probe = std::getenv("NWII_EFB_PAGE_PROBE") != nullptr;
    static unsigned shown = 0;
    size_t page = 0;
    for (size_t off = 0; off < bytes; off += kEfbPageBytes, ++page) {
        const size_t len = bytes - off < kEfbPageBytes ? bytes - off : kEfbPageBytes;
        if (NativeEfbPageHash(live + off, len) == it->second.pages[page]) continue;
        // A fingerprint can only differ if a sampled byte differs, so this
        // direction cannot report a stale texture: "changed" is sound.
        if (probe && shown < 100) {
            ++shown;
            std::cout << "[EFB page] address=0x" << std::hex << address << std::dec
                      << " bytes=" << bytes << " changed_page=" << page
                      << " unchanged=0" << std::endl;
        }
        return false;
    }
    if (probe && shown < 100) {
        ++shown;
        std::cout << "[EFB page] address=0x" << std::hex << address << std::dec
                  << " bytes=" << bytes << " pages=" << page
                  << " unchanged=1 fingerprints" << std::endl;
    }
    return true;
}
'''
    text = sub(text, old, new, 'NativeEfbGuestUnchanged')

    # register_efb_texture: build the page fingerprints with the snapshot.
    old = '''    if (g_ctx_ptr && size && size <= (4u << 20) && NativeEfbSpan(*g_ctx_ptr, addr, size)) {
        const auto* bytes = g_ctx_ptr->mmu.get_ptr(addr);
        snapshot.assign(bytes, bytes + size);
    }
'''
    new = '''    auto& pages = s_efb_guest[addr & 0x3fffffff].pages; // ''' + PAGE_MARK + '''
    pages.clear();
    if (g_ctx_ptr && size && size <= (4u << 20) && NativeEfbSpan(*g_ctx_ptr, addr, size)) {
        const auto* bytes = g_ctx_ptr->mmu.get_ptr(addr);
        snapshot.assign(bytes, bytes + size);
        for (size_t off = 0; off < size; off += kEfbPageBytes) {
            const size_t len = size - off < kEfbPageBytes ? size - off : kEfbPageBytes;
            pages.push_back(NativeEfbPageHash(bytes + off, len));
        }
    }
'''
    text = sub(text, old, new, 'register_efb_texture snapshot')

    # get_texture: tick first, then the identity-keyed lookup.
    old = '''GLuint TextureCache::get_texture(CPUContext& ctx, const gx::TexStage& stage) {
'''
    new = '''GLuint TextureCache::get_texture(CPUContext& ctx, const gx::TexStage& stage) {
    ++tick; // ''' + MARK + ''': cheap recency stamp for eviction
'''
    text = sub(text, old, new, 'get_texture entry')

    old = '''    TextureKey key = {stage.base_addr, stage.width, stage.height,
                      stage.format, stage.tlut_offset, stage.tlut_format, hash};
    auto it = cache.find(key);
    if (it != cache.end()) {
        return it->second;
    }
'''
    new = '''    // ''' + MARK + ''': the key is the texture's identity; the content hash only
    // decides whether the GL texture the entry already owns is still right.
    static const bool key_v2 = std::getenv("NWII_NO_TEXTURE_KEY_V2") == nullptr;
    TextureKey key = {stage.base_addr, stage.width, stage.height,
                      stage.format, stage.tlut_offset, stage.tlut_format};
    auto it = cache.find(key);
    if (it != cache.end()) {
        if (it->second.data_hash == hash) {
            it->second.stamp = tick;
            return it->second.tex;
        }
        if (key_v2) {
            // Same identity, new content: replace level 0 in place. The
            // dimensions come from the key, so this never reallocates.
            Image img = decode_texture(ctx, stage);
            if ((uint32_t)img.width == it->second.width &&
                (uint32_t)img.height == it->second.height) {
                const GLuint tex = it->second.tex;
                glBindTexture(GL_TEXTURE_2D, tex);
                glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, img.width, img.height,
                                GL_RGBA, GL_UNSIGNED_BYTE, img.data);
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAX_LEVEL, 0); // NATIVE_MOM_MIPMAPS_V1
                UnloadImage(img);
                // NATIVE_MOM_MIPMAP_ORDER_V1: the existing chain is stale. Drop
                // the name so ensure_mips() rebuilds it from the new texels
                // before a mip filter can sample it.
                mip_ready.erase(tex);
                it->second.data_hash = hash;
                it->second.stamp = tick;
                { // NWII_TEX_KEY_PROBE=1: count in-place re-uploads (the movie).
                  static const bool probe = std::getenv("NWII_TEX_KEY_PROBE") != nullptr;
                  static unsigned refreshes = 0;
                  if (probe && (++refreshes % 300) == 1)
                      std::cout << "[TextureCache] refresh #" << refreshes
                                << " addr=0x" << std::hex << stage.base_addr
                                << std::dec << std::endl;
                }
                return tex;
            }
            UnloadImage(img); // degenerate size: fall through and recreate
        }
        // Legacy content-keyed cache (or a size the decoder clamped): the old
        // object cannot be reused.
        glDeleteTextures(1, &it->second.tex);
        mip_ready.erase(it->second.tex);
        cache.erase(it);
    }
'''
    text = sub(text, old, new, 'cache lookup')

    # Overflow: evict LRU instead of dropping every texture.
    old = '''    if (cache.size() > 512)
        clear();
'''
    new = '''    // ''' + MARK + ''': bound the cache by evicting the least recently used
    // entries (down to 75 %), never by deleting GL objects the current frame
    // still samples.
    if (key_v2) {
        if (cache.size() > kCacheHighWater) evict_lru();
    } else if (cache.size() > 512) {
        clear(); // NWII_NO_TEXTURE_KEY_V2=1: the old wholesale flush
    }
'''
    text = sub(text, old, new, 'overflow')

    # Insert: remember the content hash and the dimensions with the GL name.
    old = '''    cache[key] = tex;
    return tex;
'''
    new = '''    {
        Entry& entry = cache[key];
        entry.tex = tex;
        entry.data_hash = hash;
        entry.width = stage.width;
        entry.height = stage.height;
        entry.stamp = tick;
    }
    return tex;
'''
    text = sub(text, old, new, 'cache insert')

    write(source, text)

print('Texture cache keyed by identity with LRU eviction; EFB snapshots validated'
      ' by per-page fingerprints.')
