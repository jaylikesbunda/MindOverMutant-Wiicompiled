"""Validate the release manifest against the official SDK schema and MoM settings."""
import argparse
import json
from pathlib import Path
import re

import jsonschema

ROOT = Path(__file__).resolve().parents[2]


def check(schema_path=None):
    manifest = json.loads((ROOT / 'recomplauncher.json').read_text(encoding='utf-8'))
    # Official format-1 schema, vendored so CI does not depend on the website.
    schema = json.loads(Path(schema_path or ROOT / 'launcher/schemas/recomplauncher.schema.json').read_text(encoding='utf-8'))
    jsonschema.Draft7Validator(schema).validate(manifest)
    source = (ROOT / 'launcher/src-tauri/src/lib.rs').read_text(encoding='utf-8')
    settings = source.split('pub struct Settings {', 1)[1].split('\n}', 1)[0]
    keys = set(re.findall(r'pub (\w+):', settings))
    ids = [setting['id'] for setting in manifest['settings']]
    assert len(ids) == len(set(ids)), 'duplicate setting IDs'
    for setting in manifest['settings']:
        for values in [setting.get('defaults', {}), *[choice.get('set', {}) for choice in setting.get('choices', [])]]:
            for key, value in values.items():
                assert key in keys, f'unknown Settings field: {key}'
                json.loads(value)
        if 'slider' in setting:
            assert setting['slider']['key'] in keys
    assert manifest['game']['check']['ids'] == ['RC8P7D']
    assert manifest['start']['args'] == ['--play', '--game', '{game}']
    assert manifest['prepare']['args'] == ['--prepare', '--game', '{game}']
    assert manifest['start']['program'] == manifest['prepare']['program'] == ['mom-launcher.exe']
    print(f'RecompLauncher schema and {len(ids)} settings checked.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--schema', help='Use a downloaded official schema for an offline check')
    check(parser.parse_args().schema)
