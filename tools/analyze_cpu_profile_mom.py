"""Rank a native_cpu_profile capture into a per-function CPU table.

Consumes the three files written by tools/stages/fix_cpu_profile_mom.py:

    <prefix>                role,module,rva,cpu_us
    <prefix>.modules.csv    index,base,size,name
    <prefix>.meta.json      rate, elapsed, counts

Attribution is offline and exact for this executable: the PE .pdata table gives
each function's [begin,end) range and `nm -C` gives the names, so no PDB, ETW,
WPR or external profiler is involved. Samples whose RIP falls outside the main
executable are grouped by module, which is what shows driver time (opengl32 /
nvoglv64 / atio6axx) that the [PERFPHASE] gl_render number cannot separate
from the renderer's own code.

Weights are the per-interval thread CPU-time deltas recorded by the sampler, so
a function's CPU ms is attributable execution time, not wall-clock samples
during a wait. If a capture has no usable thread times the script falls back to
raw sample counts and says so.

Usage:
    python tools/analyze_cpu_profile_mom.py logs/cpuprofile.csv
    python tools/analyze_cpu_profile_mom.py logs/cpuprofile.csv --top 40
    python tools/analyze_cpu_profile_mom.py logs/cpuprofile.csv \\
        --exe export/build/MindOverMutant.exe --out logs/cpuprofile
"""
from __future__ import annotations

import argparse
import bisect
import collections
import csv
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
from pathlib import Path

IMAGE_SCN_MEM_EXECUTE = 0x20000000
STRING_GROUP = {
    'func_': 'guest function (generated func_*)',
    'interp': 'guest interpreter',
    'dispatch': 'dispatcher',
}


def pe_layout(path: Path):
    """Return (image_base, sections) from the PE headers."""
    with path.open('rb') as handle:
        head = handle.read(0x40)
        if head[:2] != b'MZ':
            raise SystemExit('%s is not a PE file' % path)
        nt_off = struct.unpack_from('<I', head, 0x3c)[0]
        handle.seek(nt_off)
        nt = handle.read(24)
        if nt[:4] != b'PE\0\0':
            raise SystemExit('%s has no PE signature' % path)
        section_count = struct.unpack_from('<H', nt, 6)[0]
        optional_size = struct.unpack_from('<H', nt, 20)[0]
        handle.seek(nt_off + 24)
        optional = handle.read(optional_size)
        magic = struct.unpack_from('<H', optional, 0)[0]
        if magic == 0x20b:
            image_base = struct.unpack_from('<Q', optional, 24)[0]
        elif magic == 0x10b:
            image_base = struct.unpack_from('<I', optional, 28)[0]
        else:
            raise SystemExit('%s: unsupported optional header magic 0x%x'
                             % (path, magic))
        handle.seek(nt_off + 24 + optional_size)
        raw = handle.read(40 * section_count)
    sections = []
    for index in range(section_count):
        offset = 40 * index
        name = raw[offset:offset + 8].split(b'\0')[0].decode('ascii', 'replace')
        virtual_size, virtual_address, raw_size, raw_ptr = struct.unpack_from(
            '<IIII', raw, offset + 8)
        characteristics = struct.unpack_from('<I', raw, offset + 36)[0]
        sections.append(dict(
            name=name,
            va=virtual_address,
            vsize=virtual_size,
            rawptr=raw_ptr,
            rawsize=raw_size,
            execute=bool(characteristics & IMAGE_SCN_MEM_EXECUTE),
        ))
    return image_base, sections


def executable_ranges(sections):
    return [(s['va'], s['va'] + max(s['vsize'], s['rawsize']))
            for s in sections if s['execute']]


def read_pdata(path: Path, sections, image_base):
    """Parse the x64 RUNTIME_FUNCTION table into absolute [begin,end) ranges."""
    section = next((s for s in sections if s['name'] == '.pdata'), None)
    if not section:
        return []
    with path.open('rb') as handle:
        handle.seek(section['rawptr'])
        raw = handle.read(section['rawsize'])
    ranges = {}
    for offset in range(0, len(raw) - 11, 12):
        begin, end, _unwind = struct.unpack_from('<III', raw, offset)
        if not (0 < begin < end):
            continue
        if end - begin > 0x200000:
            continue
        ranges.setdefault(begin, end)
    return sorted((image_base + begin, image_base + end)
                  for begin, end in ranges.items())


def load_symbols(exe: Path, nm: str, image_base: int, exec_ranges):
    """Map function-start RVA to a demangled name; text/weak symbols only."""
    try:
        result = subprocess.run(
            [nm, '-C', '--defined-only', str(exe)],
            capture_output=True, text=True, errors='replace', timeout=900)
    except FileNotFoundError:
        raise SystemExit('nm not found; pass --nm <path to mingw nm.exe>')
    if result.returncode != 0 and not result.stdout:
        raise SystemExit('nm failed: %s' % (result.stderr or '').strip()[-400:])
    by_start = {}
    for line in result.stdout.splitlines():
        match = re.match(r'^([0-9a-fA-F]{8,16})\s+([A-Za-z])\s+(.+)$', line)
        if not match or match.group(2) not in 'TtWw':
            continue
        address = int(match.group(1), 16) - image_base
        name = match.group(3).strip()
        if not name or name.startswith('.'):
            continue
        if not any(lo <= address < hi for lo, hi in exec_ranges):
            continue
        previous = by_start.get(address)
        if previous is None:
            by_start[address] = name
        elif ' [clone' in previous and ' [clone' not in name:
            by_start[address] = name
    return by_start


def load_sdk_names(path: Path):
    names = {}
    if not path or not path.is_file():
        return names
    with path.open(encoding='utf-8', errors='replace', newline='') as handle:
        for row in csv.DictReader(handle):
            try:
                names[int(row['Location'], 16)] = row['Name'].strip()
            except (KeyError, ValueError):
                continue
    return names


class Resolver:
    def __init__(self, exe: Path, nm: str, sdk: Path | None):
        self.exe = exe
        self.image_base, self.sections = pe_layout(exe)
        self.exec_ranges = executable_ranges(self.sections)
        self.ranges = read_pdata(exe, self.sections, self.image_base)
        self.range_starts = [begin for begin, _end in self.ranges]
        self.symbols = load_symbols(exe, nm, self.image_base, self.exec_ranges)
        self.symbol_starts = sorted(self.symbols)
        self.sdk = load_sdk_names(sdk)

    def section_of(self, rva):
        for section in self.sections:
            if not section['execute']:
                continue
            size = max(section['vsize'], section['rawsize'])
            if section['va'] <= rva < section['va'] + size:
                return section['name']
        return None

    def name_at(self, start):
        name = self.symbols.get(start)
        if name is None:
            name = 'sub_%08X' % start
        match = re.match(r'^func_([0-9A-Fa-f]{8})$', name)
        if match:
            sdk = self.sdk.get(int(match.group(1), 16))
            if sdk:
                name = '%s (SDK: %s)' % (name, sdk.split('\t')[0].strip())
        return name

    def resolve(self, rva):
        """Return (function name, how) for an executable RVA, else None."""
        index = bisect.bisect_right(self.range_starts, rva) - 1
        # Walk back a few entries: overlapping /.cold ranges are rare but real.
        for steps in range(3):
            current = index - steps
            if current < 0:
                break
            begin, end = self.ranges[current]
            if begin <= rva < end:
                return self.name_at(begin), 'pdata'
            if rva - begin > 0x40000:
                break
        index = bisect.bisect_right(self.symbol_starts, rva) - 1
        if index >= 0:
            start = self.symbol_starts[index]
            if rva - start < 0x8000:
                return self.name_at(start), 'symbol'
        return None, None


def group_of(name):
    if name.startswith('module:') or name.startswith('unknown module'):
        return name
    if name.startswith('unmapped'):
        return 'unmapped executable code'
    if re.match(r'^(func|FUN)_[0-9A-Fa-f]{8}', name):
        return 'guest function (generated func_*)'
    if 'interpret_one' in name or 'interpret_step' in name:
        return 'guest interpreter'
    if 'EmitPrimitive' in name or 'ProcessGXFifo' in name or 'FifoParser' in name:
        return 'GX submit/parse'
    if 'RendererGL' in name:
        return 'RendererGL'
    if name.startswith('nwii::runtime::gx::'):
        return 'GX (other)'
    if name.startswith('nwii::runtime::'):
        return 'runtime (other)'
    if 'dispatch' in name.lower():
        return 'dispatcher'
    return 'host other'


def read_modules(path: Path):
    if not path.is_file():
        return []
    with path.open(encoding='utf-8', errors='replace', newline='') as handle:
        rows = list(csv.DictReader(handle))
    modules = []
    for row in rows:
        modules.append(dict(
            index=int(row['index']),
            base=int(row['base']),
            size=int(row['size']),
            name=os.path.basename(row['name'].replace('\\', '/')),
        ))
    return modules


def read_samples(path: Path):
    rows = []
    with path.open(encoding='utf-8', errors='replace', newline='') as handle:
        for row in csv.DictReader(handle):
            try:
                rows.append(dict(
                    role=row['role'].strip(),
                    module=int(row['module']),
                    rva=int(row['rva']),
                    cpu_us=int(row['cpu_us']),
                ))
            except (KeyError, ValueError):
                continue
    return rows


def pct(part, total):
    return 100.0 * part / total if total else 0.0


def sha256(path: Path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('samples', type=Path,
                        help='capture CSV from NWII_CPU_PROFILE_FILE')
    parser.add_argument('--exe', type=Path,
                        default=root / 'export/build/MindOverMutant.exe',
                        help='exact executable the capture ran')
    parser.add_argument('--nm', default='nm',
                        help='nm executable (mingw binutils)')
    parser.add_argument('--sdk', type=Path,
                        default=root / 'export/sdk_symbols.csv',
                        help='optional guest PC to SDK name table')
    parser.add_argument('--top', type=int, default=30)
    parser.add_argument('--out', type=Path,
                        help='output prefix (default: the samples path)')
    args = parser.parse_args()

    samples_path = args.samples.resolve()
    if not samples_path.is_file():
        raise SystemExit('no such capture: %s' % samples_path)
    prefix = (args.out or samples_path.with_suffix('')).resolve()
    exe = args.exe.resolve()
    if not exe.is_file():
        raise SystemExit('executable not found: %s' % exe)

    modules = read_modules(Path(str(samples_path) + '.modules.csv'))
    meta = {}
    meta_path = Path(str(samples_path) + '.meta.json')
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding='utf-8-sig'))
    samples = read_samples(samples_path)
    if not samples:
        raise SystemExit('capture has no samples')

    exe_name = exe.name.lower()
    main_index = next((m['index'] for m in modules
                       if m['name'].lower() == exe_name), None)
    module_names = {m['index']: m['name'] for m in modules}
    if main_index is None:
        # Older/partial captures: the exe is normally the first module.
        main_index = modules[0]['index'] if modules else 0
        print('warning: no module named %s; assuming module %d'
              % (exe.name, main_index), file=sys.stderr)

    resolver = Resolver(exe, args.nm, args.sdk)
    exe_digest = sha256(exe)

    by_role = collections.defaultdict(collections.Counter)
    by_count = collections.defaultdict(collections.Counter)
    role_samples = collections.Counter()
    role_weight = collections.Counter()
    unresolved = collections.Counter()
    counts_only = False

    for sample in samples:
        role = sample['role']
        weight = sample['cpu_us']
        if weight <= 0:
            continue
        role_weight[role] += weight
        role_samples[role] += 1
        if sample['module'] == main_index:
            name, how = resolver.resolve(sample['rva'])
            if not name:
                unresolved[role] += 1
                name = 'unmapped executable code'
        elif sample['module'] == 0xFFFF:
            name = 'unknown module'
        else:
            name = 'module:' + module_names.get(sample['module'],
                                                'module%d' % sample['module'])
        by_role[role][name] += weight
        by_count[role][name] += 1

    if not any(role_weight.values()):
        # GetThreadTimes was unavailable: rank by sample count and say so.
        counts_only = True
        by_role.clear()
        by_count.clear()
        for sample in samples:
            role = sample['role']
            role_samples[role] += 1
            role_weight[role] += 1
            if sample['module'] == main_index:
                name, _how = resolver.resolve(sample['rva'])
                name = name or 'unmapped executable code'
            elif sample['module'] == 0xFFFF:
                name = 'unknown module'
            else:
                name = 'module:' + module_names.get(
                    sample['module'], 'module%d' % sample['module'])
            by_role[role][name] += 1
            by_count[role][name] += 1

    elapsed = float(meta.get('elapsed_s', 0.0) or 0.0)
    unit_div = 1.0 if counts_only else 1000.0
    unit_label = 'Samples' if counts_only else 'CPU ms'
    unit_short = 'samples' if counts_only else 'ms'
    order = [role for role in ('guest', 'host') if by_role.get(role)]
    order += [role for role in by_role if role not in order]

    lines = [
        '# Native CPU profile - Mind over Mutant',
        '',
        'Capture: `%s`; executable `%s`; %s.'
        % (os.path.basename(str(samples_path)), exe.name,
           ('%.2f s at %s Hz%s' % (
               elapsed, meta.get('hz', '?'),
               (', after a %.0f s delay' % meta['delay_s'])
               if meta.get('delay_s') else '')
            if elapsed else 'duration unknown')),
        '',
        'Method: RIP sampled with SuspendThread + GetThreadContext, weighted by '
        'the per-interval thread CPU-time delta; functions resolved against the '
        'executable\'s PE `.pdata` ranges and `nm -C` symbols.',
        '',
        'Executable SHA-256: `%s`.' % exe_digest,
        '',
    ]
    if counts_only:
        lines += ['**Warning: no usable thread CPU times; the table ranks raw '
                  'samples, including intervals where the thread was blocked.**',
                  '']
    if meta.get('dropped'):
        lines += ['**Warning: %s samples were dropped at the cap; raise '
                  'NWII_CPU_PROFILE_MAX or shorten the capture.**'
                  % meta['dropped'], '']
    if meta.get('failed'):
        lines += ['Sampler failures (thread open/suspend/context/thread-times): '
                  '%s. They are excluded, not attributed.' % meta['failed'], '']

    if counts_only:
        lines += ['## Thread totals', '',
                  '| Thread | Samples |', '|---|---:|']
        for role in order:
            lines.append('| %s | %d |' % (role, role_samples[role]))
        lines.append('')
    else:
        lines += ['## Thread totals', '',
                  '| Thread | Samples | %s | %s/s |' % (unit_label, unit_label),
                  '|---|---:|---:|---:|']
        for role in order:
            weight = role_weight[role]
            cpu_ms = weight / 1000.0
            lines.append('| %s | %d | %.1f | %.1f |' % (
                role, role_samples[role], cpu_ms,
                cpu_ms / elapsed if elapsed else 0.0))
        lines.append('')
    if meta.get('idle_intervals'):
        lines += ['Intervals where the sampled thread was not running '
                  '(excluded from attribution): %s.'
                  % meta['idle_intervals'], '']

    report = {'capture': str(samples_path), 'exe': str(exe),
              'exe_sha256': exe_digest, 'meta': meta,
              'counts_only': counts_only, 'threads': {}}

    for role in order:
        ranked = by_role[role].most_common()
        total = sum(weight for _name, weight in ranked) or 1
        shown = ranked[:args.top]
        tail = total - sum(weight for _name, weight in shown)
        lines += ['## %s thread' % role.capitalize(), '',
                  ('Total: %d samples; shares are of this thread.'
                   % role_samples[role]) if counts_only else
                  ('Total: %.1f CPU ms over %d samples; shares are of this '
                   'thread\'s attributed CPU time.'
                   % (total / 1000.0, role_samples[role])),
                  '',
                  ('| # | Function | Samples | Share |' if counts_only else
                   '| # | Function | CPU ms | Share | Samples |'),
                  ('|---:|---|---:|---:|' if counts_only else
                   '|---:|---|---:|---:|---:|')]
        for rank, (name, weight) in enumerate(shown, 1):
            if counts_only:
                lines.append('| %d | `%s` | %.1f | %.2f%% |'
                             % (rank, name, weight, pct(weight, total)))
            else:
                lines.append('| %d | `%s` | %.1f | %.2f%% | %d |'
                             % (rank, name, weight / 1000.0,
                                pct(weight, total), by_count[role][name]))
        lines += ['', 'Omitted tail: %.1f %s (%.2f%%).' % (tail / unit_div,
                                                           unit_short,
                                                           pct(tail, total)), '']
        groups = collections.Counter()
        for name, weight in ranked:
            groups[group_of(name)] += weight
        lines += ['| Group | %s | Share |' % unit_label, '|---|---:|---:|']
        for group, weight in groups.most_common():
            lines.append('| `%s` | %.1f | %.2f%% |'
                         % (group, weight / unit_div, pct(weight, total)))
        lines.append('')
        external = [(name, weight) for name, weight in ranked
                    if name.startswith('module:') or name.startswith('unknown')]
        if external:
            lines += ['External modules (driver/OS time; not in the exe):', '']
            for name, weight in external[:12]:
                lines.append('- `%s`: %.1f %s (%.2f%%)'
                             % (name, weight / unit_div, unit_short,
                                pct(weight, total)))
            lines.append('')
        if unresolved[role]:
            lines += ['Unresolved executable samples: %d (%.2f%%).'
                      % (unresolved[role],
                         pct(unresolved[role], role_samples[role])), '']
        report['threads'][role] = dict(
            samples=role_samples[role],
            unit=unit_short,
            cpu_ms=total / 1000.0,
            cpu_ms_per_s=total / 1000.0 / elapsed if elapsed else 0.0,
            functions=[dict(name=name, value=weight / unit_div,
                            cpu_ms=weight / 1000.0,
                            share=pct(weight, total))
                       for name, weight in ranked[:200]],
            groups=[dict(name=name, value=weight / unit_div,
                         cpu_ms=weight / 1000.0,
                         share=pct(weight, total))
                    for name, weight in groups.most_common()],
            unresolved=unresolved[role],
        )

    lines += ['## How to read this', '',
              '- `CPU ms/s` is attributable CPU time per wall second: near 1000 '
              'means the thread saturated a core for the whole capture.',
              '- The host thread is the render thread in this port. Driver time '
              'appears under `module:*`; that is the part `gl_render` cannot '
              'split from renderer code.',
              '- The guest row is host code (`func_*`, dispatcher, interpreter), '
              'not guest PCs. Use the poll census for guest-PC hot sites.',
              '- Sampling is leaf-only: it gives leaf attribution, not inclusive '
              'stacks. ETW is the lane that would add inclusive stacks.',
              '- The names come from the executable on disk; analyse the exact '
              'build the capture ran. A rebuild invalidates the table.', '']

    md_path = Path(str(prefix) + '.cpuprofile.md')
    json_path = Path(str(prefix) + '.cpuprofile.json')
    md_path.write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='\n')
    json_path.write_text(json.dumps(report, indent=2) + '\n',
                         encoding='utf-8', newline='\n')
    print('\n'.join(lines[:90]))
    print('wrote %s and %s' % (md_path, json_path))


if __name__ == '__main__':
    main()
