"""Release profiles must relocate safely and reject missing or stale inputs."""
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release_pgo as pgo
from clean_room_build import POSTGEN


def sample_counters():
    return (struct.pack('<4I', 0x67636461, 0x4236312a, 123, 456)
            + struct.pack('<4I', 0xa1000000, 8, 1, 50)
            + struct.pack('<5I', 0x01000000, 12, 77, 88, 99)
            + struct.pack('<Ii', 0x01a10000, -16)
            + struct.pack('<I', 0))


class ReleasePgoTests(unittest.TestCase):
    def test_counter_round_trip_keeps_compressed_zero_runs(self):
        data = sample_counters()
        self.assertEqual(pgo.gcda_bytes(pgo.gcda_records(data)), data)
        # A gcno-style string/path record must never enter the counter bundle.
        with self.assertRaises(ValueError):
            pgo.gcda_records(data[:16] + struct.pack('<3I', 0x03000000, 4, 123))

    def test_release_pipeline_prunes_before_batching_and_memory_passes(self):
        self.assertLess(POSTGEN.index('fix_prune_entry_cases_mom.py'), POSTGEN.index('fix_inst_count_batch_mom.py'))
        self.assertLess(POSTGEN.index('fix_cache_range_loops_mom.py'), POSTGEN.index('fix_inst_count_batch_mom.py'))
        self.assertLess(POSTGEN.index('fix_inst_count_batch_mom.py'), POSTGEN.index('fix_guest_memory_lane_mom.py'))
        self.assertEqual(POSTGEN[-1], 'fix_ctx_restrict_mom.py')

    def test_profiles_relocate_and_source_changes_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix='mom-pgo-test-') as folder:
            root = Path(folder)
            export = root / 'training/export'
            headers = export / 'nWiiRuntime/include/runtime'
            headers.mkdir(parents=True)
            (headers / 'test.h').write_text('#pragma once\ninline int helper() { return 1; }\n')
            (export / 'functions.h').write_text('// generated declarations\n')
            (export / 'CMakeLists.txt').write_text('# generated build\n')
            for name in [*[f'output_{i}.cpp' for i in range(68)], 'output_gap_0.cpp', 'output_gap_1.cpp', 'main_output.cpp']:
                (export / name).write_text('void guest() {}\n')
            pgo.pin_sources(export)
            before = pgo.inputs(export)
            pgo.pin_sources(export)
            self.assertEqual(before, pgo.inputs(export))
            build = root / 'training/build'
            counters = build / 'CMakeFiles/MindOverMutant.dir'
            counters.mkdir(parents=True)
            for unit in pgo.units(export):
                (counters / (unit.name + '.gcda')).write_bytes(sample_counters())
            bundle = root / 'counters.json'
            moved = root / 'another-install/export'
            shutil.copytree(export, moved)
            with patch('release_pgo.compiler_version', return_value='16.1.0'):
                pgo.package(export, build, bundle)
                use_build = root / 'another-install/build'
                pgo.configure(moved, use_build, 'use', bundle)
                self.assertEqual((use_build / 'CMakeFiles/MindOverMutant.dir/main_output.cpp.gcda').read_bytes(), sample_counters())
                self.assertIn('-Werror=coverage-mismatch', (moved / 'CMakeLists.txt').read_text())
                (moved / 'output_0.cpp').write_text('void changed() {}\n')
                with self.assertRaisesRegex(ValueError, 'sources changed'):
                    pgo.configure(moved, use_build, 'use', bundle)
            with patch('release_pgo.compiler_version', return_value='15.2.0'):
                with self.assertRaisesRegex(ValueError, 'compiler version'):
                    pgo.configure(moved, use_build, 'use', bundle)

    def test_unstaged_guest_source_cannot_be_published_as_optimized(self):
        with tempfile.TemporaryDirectory(prefix='mom-pgo-guard-') as folder:
            export = Path(folder)
            (export / 'main_output.cpp').write_text('void guest() {}\n')
            with self.assertRaisesRegex(ValueError, 'performance stages are missing'):
                pgo.verify_guest_performance(export)


if __name__ == '__main__':
    unittest.main()
