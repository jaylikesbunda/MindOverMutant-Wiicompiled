"""Keep queued texture names alive and account cache resets/mipmap storage."""
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = Path('vendor/NWiiRecomp/nWiiRuntime')
MARK = 'NATIVE_MOM_TEXTURE_BUDGET_SAFETY_V1'

PAIRS = {
    'include/runtime/texture_cache_gl.h': [
        ('    void protect(GLuint tex) { protected_names.insert(tex); }',
         '    GLuint protect(GLuint tex) { if (tex) protected_names.insert(tex); return tex; }'),
        ('    void clear_protection() { protected_names.clear(); }',
         '''    void clear_protection() {
        protected_names.clear();
        if (clear_pending) { clear_pending = false; clear(); }
        else evict_lru();
    }'''),
        ('    size_t cache_bytes = 0;',
         '    size_t cache_bytes = 0;\n    bool clear_pending = false; // ' + MARK),
    ],
    'src/hle/texture_cache_gl.cpp': [
        ('void TextureCache::clear() {\n',
         '''void TextureCache::clear() {
    // ''' + MARK + ''': finish the queued batch before deleting its images.
    if (!protected_names.empty()) { clear_pending = true; return; }
    clear_pending = false;
'''),
        ('    cache.clear();\n', '    cache.clear();\n    cache_bytes = 0;\n'),
        ('        if (unchanged) return efb;',
         '        if (unchanged) return protect(efb);'),
        ('        return it->second.tex;', '        return protect(it->second.tex);'),
        ('    cache_bytes += entry_bytes;\n    return tex;',
         '''    cache_bytes += entry_bytes;
    protect(tex);
    evict_lru(); // Active images may temporarily exceed the budget until flush.
    return tex;'''),
        ('    mip_ready.insert(tex);\n',
         '''    mip_ready.insert(tex);
    // Account every allocated level, including non-power-of-two dimensions.
    for (auto& item : cache) {
        if (item.second.tex != tex) continue;
        size_t extra = 0;
        uint32_t w = item.first.width, h = item.first.height;
        while (w > 1 || h > 1) {
            w = std::max(1u, w / 2); h = std::max(1u, h / 2);
            extra += size_t(w) * h * 4;
        }
        item.second.bytes += extra;
        cache_bytes += extra;
        break;
    }
'''),
    ],
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--restore', action='store_true')
    args = parser.parse_args()
    if args.restore and not any(
            MARK in (args.root / RUNTIME / relative).read_text(encoding='utf-8')
            for relative in PAIRS):
        print(MARK + ' already pristine')
        return
    prepared = []
    for relative, pairs in PAIRS.items():
        path = args.root / RUNTIME / relative
        original = path.read_text(encoding='utf-8')
        text = original
        for old, new in (reversed(pairs) if args.restore else pairs):
            source, target = (new, old) if args.restore else (old, new)
            if not args.restore and new in text:
                continue
            if args.restore and new not in text and old in text:
                continue
            if text.count(source) != 1:
                raise SystemExit('%s: ambiguous anchor in %s: %r' %
                                 (MARK, relative, source.splitlines()[0]))
            text = text.replace(source, target, 1)
        prepared.append((path, original, text))
    for path, original, text in prepared:
        if original != text:
            path.write_text(text, encoding='utf-8', newline='\n')
    print(MARK + (' restored' if args.restore else ' staged'))


if __name__ == '__main__':
    main()
