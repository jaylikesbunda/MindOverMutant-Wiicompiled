"""Package the windowless launcher and offline builder for Windows x64."""
import argparse
import json
from pathlib import Path
import sys
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def package(executable, pack, output):
    if not executable.is_file():
        raise SystemExit(f'Launcher not found: {executable}')
    subprocess.run([sys.executable, str(ROOT / 'tools/check_builder_pack.py'), str(pack)], check=True)
    if output.exists():
        raise SystemExit(f'Refusing to overwrite {output}')
    output.parent.mkdir(parents=True, exist_ok=True)
    files = [(executable, 'mom-launcher.exe'), (ROOT / 'recomplauncher.json', 'recomplauncher.json'),
             (ROOT / 'LICENSE', 'LICENSE'), (ROOT / 'THIRD_PARTY_NOTICES.md', 'THIRD_PARTY_NOTICES.md')]
    files.extend((path, 'builder-pack/' + path.relative_to(pack).as_posix())
                 for path in sorted(pack.rglob('*')) if path.is_file())
    # Only explicit release inputs; never copy a profile, local build or disc files.
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path, name in files:
            archive.write(path, name)
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None, 'corrupt portable archive'
        for name in ['mom-launcher.exe', 'recomplauncher.json', 'builder-pack/toolchain/python/python.exe']:
            assert name in archive.namelist(), f'missing {name}'
    print(output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path, default=ROOT / 'launcher/src-tauri/target/release/mom-launcher.exe')
    parser.add_argument('--pack', type=Path, default=ROOT / 'launcher/src-tauri/builder-pack')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    version = json.loads((ROOT / 'launcher/src-tauri/tauri.conf.json').read_text(encoding='utf-8'))['version']
    package(args.exe, args.pack, args.out or ROOT / f'dist/MoMLauncher-{version}-windows-x64.zip')
