"""Builder failures, launcher progress and packaged DLL finalization."""
import contextlib,io,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from clean_room_build import run,copy_runtime_dlls

class ExecutionTests(unittest.TestCase):
    def test_streams_ninja_progress_and_child_output(self):
        out=io.StringIO()
        with contextlib.redirect_stdout(out):
            run([sys.executable,'-c',"print('[1/4] one');print('[4/4] done')"],Path.cwd(),os.environ.copy(),'Build',(10,30))
        lines=out.getvalue().splitlines()
        self.assertIn('@progress|15|Build',lines)
        self.assertEqual(lines[-1],'@progress|30|Build')

    def test_failure_is_not_reported_as_completed(self):
        out=io.StringIO()
        with contextlib.redirect_stdout(out),self.assertRaises(subprocess.CalledProcessError):
            run([sys.executable,'-c',"import sys;print('failed',file=sys.stderr);sys.exit(7)"],Path.cwd(),os.environ.copy(),'Build',(10,30))
        self.assertIn('failed',out.getvalue())
        self.assertNotIn('@progress|30|Build',out.getvalue())

    def test_dlls_are_found_beside_compiler_without_gcc_lookup(self):
        with tempfile.TemporaryDirectory(prefix='mom-builder-test-') as folder:
            root=Path(folder);compiler=root/'bin/g++.exe';compiler.parent.mkdir();compiler.touch()
            dest=root/'game';dest.mkdir()
            names=('libgcc_s_seh-1.dll','libstdc++-6.dll','libwinpthread-1.dll')
            for name in names:(compiler.parent/name).write_bytes(name.encode())
            with patch('clean_room_build.shutil.which',return_value=str(compiler)),patch('clean_room_build.subprocess.check_output',side_effect=AssertionError('unnecessary GCC lookup')):
                copy_runtime_dlls(dest,os.environ.copy())
            for name in names:self.assertEqual((dest/name).read_bytes(),name.encode())

if __name__=='__main__':unittest.main()
