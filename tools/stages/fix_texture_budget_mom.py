"""Byte-budgeted LRU for the texture cache (MoM).

Marker: NATIVE_MOM_TEXTURE_BUDGET_V1.

MoM's evict_lru counted entries only (>512 evict to 384) and could delete a GL
texture name a pending batch still references.  This stage:

* gives every cache entry its RGBA upload byte footprint and tracks
  ``cache_bytes``; eviction runs while the cache exceeds the
  ``NWII_TEXTURE_CACHE_MB`` budget (default 256, clamped 1..4096) OR the
  4096-entry cap;
* a name the open batch references is never evicted: the renderer
  ``protect()``s every name it binds and ``clear_protection()``s at
  FlushBatch, so no queued draw ever samples a recycled name;
* EFB objects stay protected as before; replaced entries release their byte
  share before the new one is counted; mip_ready bookkeeping is unchanged.

Idempotent and reversible via exact inverse pairs (--restore).
"""
from __future__ import annotations

import argparse
from pathlib import Path

MARKER = 'NATIVE_MOM_TEXTURE_BUDGET_V1'

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / 'vendor').is_dir():
    ROOT = ROOT.parent

RUNTIME = 'vendor/NWiiRecomp/nWiiRuntime'
HDR = RUNTIME + '/include/runtime/texture_cache_gl.h'
CPP = RUNTIME + '/src/hle/texture_cache_gl.cpp'
GL = RUNTIME + '/src/hle/gx/renderer_gl.cpp'

HDR_PAIRS = [
    ('''    // NATIVE_MOM_TEXTURE_EVICT_V1: one program per distinct source key, with a use stamp so
    // the cache can evict the least recently used entry instead of clearing.
    struct Entry {
        GLuint tex = 0;
        uint64_t stamp = 0;
    };
    std::unordered_map<TextureKey, Entry, TextureKeyHash> cache;
    uint64_t tick = 0;
    void evict_lru();''',
     '''    // NATIVE_MOM_TEXTURE_EVICT_V1: one program per distinct source key, with a use stamp so
    // the cache can evict the least recently used entry instead of clearing.
    // ''' + MARKER + ''': byte-budgeted LRU - each entry carries the
    // RGBA byte footprint it uploaded, the cache evicts to a byte budget
    // (NWII_TEXTURE_CACHE_MB, default 256) plus a hard 4096-entry cap, and a
    // GL name a pending batch still references is never evicted: the renderer
    // protect()s every name it binds into the open batch and clear_protection()s
    // at FlushBatch, so no name is deleted out from under a queued draw.
    struct Entry {
        GLuint tex = 0;
        uint64_t stamp = 0;
        size_t bytes = 0;
    };
    std::unordered_map<TextureKey, Entry, TextureKeyHash> cache;
    size_t cache_bytes = 0;
    uint64_t tick = 0;
    void evict_lru();
    std::unordered_set<GLuint> protected_names; // ''' + MARKER),
    ('''    void ensure_mips(GLuint tex);

    void clear();''',
     '''    void ensure_mips(GLuint tex);

    void clear();
    void protect(GLuint tex) { protected_names.insert(tex); }   // ''' + MARKER + '''
    void clear_protection() { protected_names.clear(); }'''),
]

CPP_PAIRS = [
    ('''void TextureCache::evict_lru() {
    static const std::size_t kLowWater = 384;
    if (cache.size() <= kLowWater)
        return;''',
     '''// ''' + MARKER + ''': the budget is bytes, not a fixed count: evict
// until the cache is under NWII_TEXTURE_CACHE_MB (default 256) and the 4096-entry
// cap. A name a pending batch references (protected_names) is skipped just like
// a live EFB object - deleting it would hand the queued draw a recycled name.
static size_t native_texture_budget_bytes() {
    static const size_t v = []() {
        const char* e = std::getenv("NWII_TEXTURE_CACHE_MB");
        const long mb = e ? std::strtol(e, nullptr, 10) : 256;
        return size_t(mb < 1 ? 1 : (mb > 4096 ? 4096 : mb)) << 20;
    }();
    return v;
}
void TextureCache::evict_lru() {
    const size_t budget = native_texture_budget_bytes();
    static const std::size_t kMaxEntries = 4096;
    if (cache_bytes <= budget && cache.size() <= kMaxEntries)
        return;'''),
    ('''    std::unordered_map<GLuint, bool> live;
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
    }''',
     '''    std::unordered_map<GLuint, bool> live;
    for (const auto& pair : efb_textures) live[pair.second] = true;
    std::size_t dropped = 0;
    for (const auto& entry : order) {
        if (cache_bytes <= budget && cache.size() <= kMaxEntries) break;
        auto it = cache.find(entry.second);
        if (it == cache.end()) continue;
        if (live.count(it->second.tex)) continue;
        if (protected_names.count(it->second.tex)) continue; // open batch still needs it
        glDeleteTextures(1, &it->second.tex);
        mip_ready.erase(it->second.tex);
        cache_bytes -= it->second.bytes;
        cache.erase(it);
        ++dropped;
    }'''),
    ('''    if (cache.size() > 512) {
        if (legacy_clear) clear();
        else evict_lru();
    }''',
     '''    if (cache_bytes > native_texture_budget_bytes() || cache.size() > 4096) {
        if (legacy_clear) clear();
        else evict_lru();
    }'''),
    ('''    cache[key] = Entry{tex, ++tick}; // NATIVE_MOM_TEXTURE_EVICT_V1''',
     '''    const size_t entry_bytes = size_t(stage.width) * stage.height * 4;
    auto& prev = cache[key];
    if (prev.tex) cache_bytes -= prev.bytes; // replaced entry: free its share first
    cache[key] = Entry{tex, ++tick, entry_bytes}; // NATIVE_MOM_TEXTURE_EVICT_V1
    cache_bytes += entry_bytes;'''),
]

GL_PAIRS = [
    ('''              glBindTexture(GL_TEXTURE_2D, tex);
              { // NATIVE_MOM_MIPMAP_ORDER_V1:''',
     '''              glBindTexture(GL_TEXTURE_2D, tex);
              nwii::runtime::hle::TextureCache::get().protect(tex); // ''' + MARKER + ''': open batch references this name
              { // NATIVE_MOM_MIPMAP_ORDER_V1:'''),
]


def read(path):
    return Path(path).read_text(encoding='utf-8', errors='surrogateescape').replace('\r\n', '\n')


def write(path, text, orig):
    data = text.replace('\n', '\r\n') if '\r\n' in orig else text
    with open(path, 'w', encoding='utf-8', errors='surrogateescape', newline='') as h:
        h.write(data)


def apply_pairs(text, pairs, tag):
    for old, new in pairs:
        found = text.count(old)
        if found != 1:
            raise SystemExit('%s: %s anchor matched %d times (expected 1): %r'
                             % (MARKER, tag, found, old.splitlines()[0][:60]))
        text = text.replace(old, new, 1)
    return text


def restore_pairs(text, pairs, tag):
    for old, new in reversed(pairs):
        found = text.count(new)
        if found == 0:
            continue
        if found != 1:
            raise SystemExit('%s: %s restore matched %d times (expected 1)'
                             % (MARKER, tag, found))
        text = text.replace(new, old, 1)
    return text


def _flush_protection(text, forward):
    # FlushBatch tail: ensure clear_protection() is present exactly once.
    tag = '\n  nwii::runtime::hle::TextureCache::get().clear_protection(); // ' + MARKER + '\n'
    if forward:
        if tag.strip() in text:
            return text
        i = text.find('static void FlushBatch(')
        assert i > 0
        j = text.find('\n}', i)
        assert j > 0
        return text[:j] + tag + text[j:]
    return text.replace(tag, '')


def apply(root):
    changed = []
    for rel, pairs in ((HDR, HDR_PAIRS), (CPP, CPP_PAIRS), (GL, GL_PAIRS)):
        target = root / rel
        if not target.is_file():
            raise SystemExit('%s: %s is missing' % (MARKER, rel))
        orig = target.read_bytes().decode('utf-8', errors='surrogateescape')
        text = orig.replace('\r\n', '\n')
        before = text
        if rel == GL:
            text = _flush_protection(text, True)
        updated = apply_pairs(restore_pairs(text, pairs, rel), pairs, rel)
        if updated != before:
            write(target, updated, orig)
            changed.append(rel)
    print('%s staged: %s; NWII_TEXTURE_CACHE_MB=<mb> sets the byte budget'
          % (MARKER, ', '.join(changed) or 'already applied'))


def restore(root):
    changed = []
    for rel, pairs in ((GL, GL_PAIRS), (CPP, CPP_PAIRS), (HDR, HDR_PAIRS)):
        target = root / rel
        if not target.is_file():
            continue
        orig = target.read_bytes().decode('utf-8', errors='surrogateescape')
        text = orig.replace('\r\n', '\n')
        updated = restore_pairs(text, pairs, rel)
        if rel == GL:
            updated = _flush_protection(updated, False)
        if updated != orig:
            write(target, updated, orig)
            changed.append(rel)
    print('%s restored: %s' % (MARKER, ', '.join(changed) or 'already pristine'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--restore', action='store_true')
    args = parser.parse_args()
    (restore if args.restore else apply)(args.root)


if __name__ == '__main__':
    main()
