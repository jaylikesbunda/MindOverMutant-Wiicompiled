"""Stop flushing the GL pipeline when nothing new has been submitted.

``NativeGpuCompletion`` calls ``glFlush`` before every PE token and finish
interrupt - about 240 times a second (the batch census counted 712 ``BP 0x48``
boundaries per 3 s window).  A flush forces the driver to submit its command
buffer to the GPU; it is a kernel transition and it serialises the driver's own
batching, which is why the CPU profile shows a large share in ``ntdll``.

Some of those flushes are pure waste: the guest can signal a token with no draw
submitted since the previous flush (state-only drains, repeated tokens).  This
stage keeps a submission counter in ``FlushBatch`` and skips the flush when the
count has not moved, so the guest still observes the same ordering and every
draw is still flushed before the token that completes it.

This is the *sound* half of the idea: it cannot change ordering, only elide a
flush with nothing to flush.  Rate-limiting flushes further (e.g. one per field)
would make the token fire with draws still in the driver queue, which risks the
guest reusing a buffer the GPU has not read; that is not done here.

``NWII_NO_PE_FLUSH_ELIDE=1`` restores the unconditional flush.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_MOM_PE_FLUSH_ELIDE_V1'
path = runtime / 'src/hle/gx/renderer_gl.cpp'
text = path.read_text()
if MARK in text:
    print('PE flush elision already present.')
    raise SystemExit(0)


def sub(old, new, what):
    global text
    assert text.count(old) == 1, 'anchor %s' % what
    text = text.replace(old, new, 1)


# The counter has to be visible to FlushBatch, which is defined earlier in the
# file than NativeGpuCompletion, so it goes above the flush itself.
sub("""static void FlushBatch(native_batch_census::Reason reason =""",
    """// """ + MARK + """: how many batch submissions have reached the driver.  A token
// with no new submission since the last flush has nothing to flush.
static uint64_t s_native_submissions = 0;
static uint64_t s_native_flushed_submissions = 0;
static bool NativeNeedSubmit() {
  static const bool disabled = std::getenv("NWII_NO_PE_FLUSH_ELIDE") != nullptr;
  if (disabled) return true;
  if (s_native_submissions == s_native_flushed_submissions) return false;
  s_native_flushed_submissions = s_native_submissions;
  return true;
}

static void FlushBatch(native_batch_census::Reason reason =""",
    'completion helper')

text = text.replace(
    """    if(have_gl){ static const bool block = std::getenv("NWII_GPU_FINISH") != nullptr; if(block) glFinish(); else glFlush(); } // NATIVE_GPU_COMPLETION_NOBLOCK_V1
    nwii::runtime::hw::pe_signal_finish();""",
    """    if(have_gl && NativeNeedSubmit()){ static const bool block = std::getenv("NWII_GPU_FINISH") != nullptr; if(block) glFinish(); else glFlush(); } // NATIVE_GPU_COMPLETION_NOBLOCK_V1
    nwii::runtime::hw::pe_signal_finish();""", 1)
text = text.replace(
    """    if(have_gl){ static const bool block = std::getenv("NWII_GPU_FINISH") != nullptr; if(block) glFinish(); else glFlush(); } // NATIVE_GPU_COMPLETION_NOBLOCK_V1
    nwii::runtime::hw::pe_signal_token(value&0xffffu,reg==0x48);""",
    """    if(have_gl && NativeNeedSubmit()){ static const bool block = std::getenv("NWII_GPU_FINISH") != nullptr; if(block) glFinish(); else glFlush(); } // NATIVE_GPU_COMPLETION_NOBLOCK_V1
    nwii::runtime::hw::pe_signal_token(value&0xffffu,reg==0x48);""", 1)
assert text.count('NativeNeedSubmit()') == 3, 'flush sites'

# Count submissions at the one place a batch reaches the driver.
sub("""  native_batch_census::Submit(census, reason, s_batch.size());""",
    """  native_batch_census::Submit(census, reason, s_batch.size());
  ++s_native_submissions; // """ + MARK,
    'submission counter')

path.write_text(text)
print('PE token flushes are skipped when no new batch was submitted.')
