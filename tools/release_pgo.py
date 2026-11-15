"""Portable GCC execution-count profiles for locally generated MoM guest code.

The distributed JSON contains only numeric GCC metadata and execution counters,
not instructions, generated C++, executable code, disc assets or save states.
"""
from pathlib import Path
import argparse
import hashlib
import json
import struct
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / 'reference/pgo/mom-pal.json'
MARK = '# NATIVE_MOM_RELEASE_PGO_V1'
FILTER = '-fprofile-filter-files=mom-pgo/.*'


def units(export):
    return sorted(export.glob('output_*.cpp')) + [export / 'main_output.cpp']


def source_hash(path):
    return hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def pin_sources(export):
    paths = units(export) + [export / 'functions.h']
    paths += sorted((export / 'nWiiRuntime/include').rglob('*.h'))
    for path in paths:
        text = path.read_text(encoding='utf-8')
        prefix = f'#line 1 "mom-pgo/{path.relative_to(export).as_posix()}"\n'
        if not text.startswith(prefix):
            path.write_text(prefix + text, encoding='utf-8', newline='\n')


def inputs(export):
    paths = units(export) + [export / 'functions.h']
    # Headers affect the guest CFG too. Require their exact normalized content.
    paths += sorted((export / 'nWiiRuntime/include').rglob('*.h'))
    paths += sorted((export / 'nWiiRuntime/include').rglob('*.inc'))
    return {p.relative_to(export).as_posix(): source_hash(p) for p in paths}


def verify_guest_performance(export):
    cases = updates = 0
    markers = set()
    required = {'NATIVE_MOM_INST_COUNT_BATCH_V1', 'NATIVE_MOM_GUEST_MEMORY_LANE_V1',
                'NATIVE_CACHE_RANGE_COLLAPSE_MOM_V1', 'NATIVE_MOM_CTX_RESTRICT_V1'}
    for path in units(export):
        text = path.read_text(encoding='utf-8')
        cases += text.count('case 0x')
        updates += text.count('++ctx.inst_count;')
        markers.update(mark for mark in required if mark in text)
    if markers != required or cases > 400000 or updates > 700000:
        raise ValueError(f'guest performance stages are missing: {sorted(required - markers)}; {cases} entry cases, {updates} instruction updates')
    print(f'Guest performance stages checked: {cases} entry cases, {updates} instruction updates')


def validate_bundle(bundle=BUNDLE):
    value = json.loads(bundle.read_text(encoding='utf-8'))
    if set(value) != {'format', 'compiler', 'game_id', 'dol_sha256', 'training', 'inputs', 'profiles'}:
        raise ValueError('unexpected release profile fields')
    if value['format'] != 1 or value['compiler'] != '16.1.0' or value['game_id'] != 'RC8P7D':
        raise ValueError('unexpected release profile metadata')
    names = {'main_output.cpp.gcda', 'output_gap_0.cpp.gcda', 'output_gap_1.cpp.gcda'}
    names.update(f'output_{index}.cpp.gcda' for index in range(68))
    if set(value['profiles']) != names:
        raise ValueError('release profile must cover all 71 guest translation units')
    if value['dol_sha256'] != (ROOT / 'data/main.dol.sha256').read_text().strip():
        raise ValueError('profile was trained for a different disc')
    for name, digest in value['inputs'].items():
        if Path(name).is_absolute() or '..' in Path(name).parts or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('invalid training source hash')
    for profile in value['profiles'].values():
        gcda_bytes(profile)
    return value


def gcda_records(data):
    if len(data) < 16 or len(data) % 4:
        raise ValueError('invalid GCC counter file length')
    header = list(struct.unpack_from('<4I', data))
    if header[:2] != [0x67636461, 0x4236312a]:
        raise ValueError('expected GCC 16.1 gcda counters')
    records = []
    pos = 16
    while pos < len(data):
        tag, = struct.unpack_from('<I', data, pos)
        pos += 4
        if tag == 0:
            if pos != len(data):
                raise ValueError('trailing profile data')
            return {'header': header, 'records': records, 'end': True}
        length, = struct.unpack_from('<i', data, pos)
        pos += 4
        if tag == 0xa1000000:
            valid = length == 8
        elif tag == 0x01000000:
            valid = length == 12
        else:
            valid = tag in range(0x01a10000, 0x01b00000, 0x20000) and length % 8 == 0
        if not valid or abs(length) > 16 * 1024 * 1024 or pos + max(0, length) > len(data):
            raise ValueError('unexpected GCC counter record')
        words = list(struct.unpack_from(f'<{max(0, length) // 4}I', data, pos))
        pos += max(0, length)
        records.append([tag, length, words])
    return {'header': header, 'records': records, 'end': False}


def gcda_bytes(profile):
    if set(profile) != {'header', 'records', 'end'} or not isinstance(profile['end'], bool):
        raise ValueError('unexpected counter profile fields')
    data = bytearray(struct.pack('<4I', *profile['header']))
    for tag, length, words in profile['records']:
        if len(words) * 4 != max(0, length):
            raise ValueError('counter record size differs')
        data += struct.pack('<Ii', tag, length)
        data += struct.pack(f'<{len(words)}I', *words)
    if profile['end']:
        data += struct.pack('<I', 0)
    # Reject unknown records, opaque payloads and malformed lengths on import too.
    gcda_records(data)
    return bytes(data)


def compiler_version():
    return subprocess.check_output(['g++', '-dumpfullversion'], text=True).strip()


def package(export, build, bundle):
    pin_sources(export)
    version = compiler_version()
    if version != '16.1.0':
        raise ValueError('release profiles require the pinned GCC 16.1.0 compiler')
    profiles = {}
    for unit in units(export):
        name = unit.name + '.gcda'
        data = (build / 'CMakeFiles/MindOverMutant.dir' / name).read_bytes()
        record = gcda_records(data)
        if gcda_bytes(record) != data:
            raise ValueError('profile round-trip differs: ' + name)
        profiles[name] = record
    value = {'format': 1, 'compiler': version, 'game_id': 'RC8P7D',
             'dol_sha256': (ROOT / 'data/main.dol.sha256').read_text().strip(),
             'training': 'Seven save states, with two-second alternating up/down strokes.',
             'inputs': inputs(export), 'profiles': profiles}
    bundle.parent.mkdir(parents=True, exist_ok=True)
    # One unit per line keeps profile updates out of unrelated source diffs.
    with bundle.open('w', encoding='utf-8', newline='\n') as out:
        out.write('{\n')
        for key, item in value.items():
            if key != 'profiles':
                out.write(json.dumps(key) + ':' + json.dumps(item, separators=(',', ':')) + ',\n')
        out.write('"profiles":{\n')
        for index, (name, profile) in enumerate(profiles.items()):
            out.write(json.dumps(name) + ':' + json.dumps(profile, separators=(',', ':')) + (',\n' if index + 1 < len(profiles) else '\n'))
        out.write('}\n}\n')
    validate_bundle(bundle)
    print(f'Packaged {len(profiles)} numeric execution profiles: {bundle}')


def configure(export, build, mode, bundle=BUNDLE):
    pin_sources(export)
    if mode == 'use':
        value = validate_bundle(bundle)
        if value['format'] != 1 or value['compiler'] != compiler_version():
            raise ValueError('PGO compiler version differs from the trained release')
        if value['inputs'] != inputs(export):
            expected = value['inputs']
            actual = inputs(export)
            changed = sorted(k for k in expected.keys() | actual.keys() if expected.get(k) != actual.get(k))
            raise ValueError('PGO sources changed; retrain before publishing: ' + ', '.join(changed[:12]))
        names = {p.name + '.gcda' for p in units(export)}
        if set(value['profiles']) != names:
            raise ValueError('PGO bundle does not cover every generated translation unit')
        dest = build / 'CMakeFiles/MindOverMutant.dir'
        dest.mkdir(parents=True, exist_ok=True)
        for name in sorted(names):
            (dest / name).write_bytes(gcda_bytes(value['profiles'][name]))
        flags = ['-fprofile-use', '-fprofile-correction', '-fprofile-partial-training',
                 '-Werror=coverage-mismatch', '-Wno-missing-profile', FILTER]
    else:
        flags = ['-fprofile-generate', '-fprofile-update=single', FILTER]
    path = export / 'CMakeLists.txt'
    text = path.read_text(encoding='utf-8')
    if MARK in text:
        text = text[:text.index(MARK)].rstrip() + '\n'
    block = MARK + '\nset_property(SOURCE\n'
    block += ''.join(f'    "{p.name}"\n' for p in units(export))
    block += '    APPEND PROPERTY COMPILE_OPTIONS "' + ';'.join(flags) + '")\n'
    if mode == 'gen':
        block += 'target_link_options(MindOverMutant PRIVATE -fprofile-generate)\n'
        block += 'target_compile_definitions(MindOverMutant PRIVATE NWII_PGO_HOOK=1)\n'
    path.write_text(text + '\n' + block, encoding='utf-8', newline='\n')
    print(f'Release PGO: {mode}; {len(units(export))} translation units')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['gen', 'use', 'package'])
    parser.add_argument('--export', type=Path, required=True)
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('--bundle', type=Path, default=BUNDLE)
    args = parser.parse_args()
    if args.mode == 'package':
        package(args.export.resolve(), args.build.resolve(), args.bundle.resolve())
    else:
        configure(args.export.resolve(), args.build.resolve(), args.mode, args.bundle.resolve())
