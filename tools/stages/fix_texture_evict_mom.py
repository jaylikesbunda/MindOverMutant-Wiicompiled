"""Evict textures least-recently-used instead of deleting the whole cache.

`TextureCache::get_texture` keys on `(address, size, format, tlut, content
hash)`, so a texture whose content changes - every movie frame - is a new entry.
The cache therefore reaches its 512-entry limit and does:

    if (cache.size() > 512)
        clear();          // deletes every GL texture in the cache

Draws are deferred: the renderer batches vertices and only binds the texture
when the batch opens, and the commands of one drain are parsed before any of them
execute.  A clear therefore deletes textures that queued draws still reference,
and those draws sample whatever GL returns for a deleted name - black.  That is
exactly the pattern reported from the outdoor area (many distinct textures, so
the limit is reached): a character with no albedo disappears, and effect quads
around the mojo/xp orbs draw as opaque black rectangles.  Indoors the cache never
reaches the limit, which is why it looked like a regression from a build rather
than a capacity bug.

This stage keeps the content hash in the key (deferred draws must keep their own
texels - keying on identity alone collapses two textures that reuse an address)
and replaces the wholesale clear with LRU eviction to a low-water mark:

    cache: key -> { GLuint tex; uint64_t stamp; }
    hit:   stamp = ++tick
    insert: stamp = ++tick
    over 512: drop the 128 least-recently-used entries

Eviction never touches a texture registered as a live EFB copy
(`find_efb_texture`), and clears its `mip_ready` entry, because GL reuses deleted
names.

`NWII_TEXTURE_CACHE_CLEAR=1` restores the previous wholesale clear.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_MOM_TEXTURE_EVICT_V1'
header = runtime / 'include/runtime/texture_cache_gl.h'
source = runtime / 'src/hle/texture_cache_gl.cpp'

ht = header.read_text()
st = source.read_text()
if MARK in ht and MARK in st:
    print('texture LRU eviction already present.')
    raise SystemExit(0)


def sub(text, old, new, what):
    assert text.count(old) == 1, 'anchor %s (%d)' % (what, text.count(old))
    return text.replace(old, new, 1)


# ---- 1. the cache entry carries a use stamp --------------------------------
old_map = '    std::unordered_map<TextureKey, GLuint, TextureKeyHash> cache;'
new_map = '''    // ''' + MARK + ''': one program per distinct source key, with a use stamp so
    // the cache can evict the least recently used entry instead of clearing.
    struct Entry {
        GLuint tex = 0;
        uint64_t stamp = 0;
    };
    std::unordered_map<TextureKey, Entry, TextureKeyHash> cache;
    uint64_t tick = 0;
    void evict_lru();'''
ht = sub(ht, old_map, new_map, 'cache map')

# ---- 2. clear(): the timer/name bookkeeping the clear used to do -----------
old_clear = '''    for (auto& pair : cache) {
        glDeleteTextures(1, &pair.second);
    }'''
new_clear = '''    for (auto& pair : cache) {
        glDeleteTextures(1, &pair.second.tex);
    }'''
st = sub(st, old_clear, new_clear, 'clear loop')

# ---- 3. hit and insert use the entry ---------------------------------------
old_hit = '''    auto it = cache.find(key);
    if (it != cache.end()) {
        return it->second;
    }'''
new_hit = '''    auto it = cache.find(key);
    if (it != cache.end()) {
        it->second.stamp = ++tick; // ''' + MARK + ''': LRU
        return it->second.tex;
    }'''
st = sub(st, old_hit, new_hit, 'cache hit')

old_over = '''    if (cache.size() > 512)
        clear();'''
new_over = '''    static const bool legacy_clear =
        std::getenv("NWII_TEXTURE_CACHE_CLEAR") != nullptr;
    if (cache.size() > 512) {
        if (legacy_clear) clear();
        else evict_lru();
    }'''
st = sub(st, old_over, new_over, 'overflow')

old_store = '''    cache[key] = tex;
    return tex;'''
new_store = '''    cache[key] = Entry{tex, ++tick}; // ''' + MARK + '''
    return tex;'''
st = sub(st, old_store, new_store, 'cache insert')

# ---- 4. the eviction itself ------------------------------------------------
old_tail = '''void TextureCache::register_efb_texture(uint32_t addr, GLuint tex) {'''
evict = '''// ''' + MARK + ''': drop the least recently used entries down to a low-water mark.
// A live EFB copy is never evicted (the renderer owns its GL object and refills
// it every copy), and a deleted name is dropped from mip_ready because GL reuses
// deleted texture names.
void TextureCache::evict_lru() {
    static const std::size_t kLowWater = 384;
    if (cache.size() <= kLowWater)
        return;
    std::vector<std::pair<uint64_t, TextureKey>> order;
    order.reserve(cache.size());
    for (const auto& pair : cache)
        order.push_back({pair.second.stamp, pair.first});
    std::sort(order.begin(), order.end(),
              [](const std::pair<uint64_t, TextureKey>& a,
                 const std::pair<uint64_t, TextureKey>& b) { return a.first < b.first; });
    std::unordered_map<GLuint, bool> live;
    for (const auto& pair : efb_textures) live[pair.second] = true;
    std::size_t dropped = 0;
    for (const auto& entry : order) {
        if (cache.size() <= kLowWater) break;
        auto it = cache.find(entry.second);
        if (it == cache.end()) continue;
        if (live.count(it->second.tex)) continue;
        glDeleteTextures(1, &it->second.tex);
        mip_ready.erase(it->second.tex);
        cache.erase(it);
        ++dropped;
    }
    static const bool trace = std::getenv("NWII_TEXTURE_EVICT_TRACE") != nullptr;
    if (trace)
        std::cout << "[TextureCache] evicted " << dropped << " entries, kept "
                  << cache.size() << std::endl;
}

''' + old_tail
st = sub(st, old_tail, evict, 'evict_lru')

header.write_text(ht)
source.write_text(st)
print('Texture cache evicts LRU; it no longer deletes every texture at 512 entries.')
