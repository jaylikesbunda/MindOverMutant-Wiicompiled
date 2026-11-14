"""Do not treat the SDK's cache-sync `sc` as an IOS syscall.

The Wii SDK's cache-range routines end with a bare `sc` instruction:

    DCFlushRange_8048B068:
        ...
        dcbf 0,r3 ; addi r3,r3,0x20 ; bdnz ...
        sc                     <-- 0x8048B090
        blr

On hardware that is the "sync" the flush needs; the runtime, however, decodes
every `sc` as an IOS fast syscall and takes the command id from r0.  r0 at
that point is whatever the caller left there, so when it happens to be 0x67,
0x69 or 0x6C the runtime dispatches IOS_Ioctlv / IOS_CloseAsync /
IOS_IoctlvAsync with the flush buffer address in r3 (not an IOS handle).

The async ioctlv handler then walks `ioctlv_vecs` for `arg_in + arg_out`
entries built from the leftover registers.  In the load-zone stall r6 was
0xff384d5e, so the handler pushed four billion vectors and threw
std::bad_alloc - after spending minutes in the loop, which is what looked
like a freeze:

    [FastSyscall] id=0x6c ... DEAD HANDLE 0x90f417e0 r4=0x2b r5=0x0
                  r6=0xff384d5e r7=0x0 r8=0x4d ...
    [Thread] host exception while running the guest: std::bad_alloc
             pc=0x8048b094 lr=0x80365284

Two fixes:

1.  handle_syscall only dispatches the handle-taking IOS commands (0x62-0x67
    and 0x69-0x6C) when r3 really is a live IOS handle.  Otherwise the `sc`
    is a cache sync and is ignored, exactly like hardware would treat an
    unknown command.  IOS_Open (0x61/0x68) is left alone: its r3 is a path
    pointer, not a handle.
2.  The async ioctlv handler refuses an impossible vector count and an
    unreadable vector array instead of allocating for it, so a bogus call can
    never take the process down.

The first dead handle at each (call site, command) pair is still reported, so
a genuine guest bug remains visible in the log.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_FAST_SYSCALL_GUARD_V1'

# ---- 1. a public "is this a live handle" query -----------------------------
header = runtime / 'include/runtime/ios_kernel.h'
text = header.read_text()
if MARK not in text:
    old = "    int32_t ioctlv(CPUContext& ctx, const IpcRequest& req);\n"
    assert text.count(old) == 1, 'ios_kernel.h anchor'
    text = text.replace(old, old +
        "    // " + MARK + ": the fast syscall path can carry a leftover value in r3\n"
        "    // that is not a handle; the caller must be able to tell.\n"
        "    bool handle_is_live(uint32_t fd) { return get_entry(fd) != nullptr; }\n")
    header.write_text(text)

# ---- 2. the guard in the syscall dispatcher -------------------------------
ios = runtime / 'src/hle/ios.cpp'
text = ios.read_text()
if MARK not in text:
    old = """bool handle_syscall(CPUContext &ctx) {
  uint32_t syscall_id = ctx.gpr[0];
  uint32_t old_pc = ctx.pc; 

  switch (syscall_id) {"""
    assert text.count(old) == 1, 'handle_syscall anchor'
    new = """bool handle_syscall(CPUContext &ctx) {
  uint32_t syscall_id = ctx.gpr[0];
  uint32_t old_pc = ctx.pc; 

  { // """ + MARK + """
    // The SDK's cache-range routines end with a bare `sc` whose r0 is
    // whatever the caller left there. Those are cache syncs, not IOS
    // requests: a real IOS call always carries a live handle in r3, so a
    // dead handle means this `sc` is not an IOS command and must be ignored.
    switch (syscall_id) {
      case 0x62: case 0x63: case 0x64: case 0x65: case 0x66: case 0x67:
      case 0x69: case 0x6A: case 0x6B: case 0x6C: {
        const uint32_t fd = ctx.gpr[3];
        if (!IOSKernel::get().handle_is_live(fd)) {
          static uint32_t seen_site[16] = {0};
          static uint32_t seen_id[16] = {0};
          static unsigned seen = 0;
          bool reported = false;
          for (unsigned i = 0; i < seen && i < 16; ++i)
            if (seen_site[i] == ctx.lr && seen_id[i] == syscall_id) reported = true;
          if (!reported) {
            if (seen < 16) { seen_site[seen] = ctx.lr; seen_id[seen] = syscall_id; ++seen; }
            std::cout << "[FastSyscall] ignoring id=0x" << std::hex << syscall_id
                      << " lr=0x" << ctx.lr << std::dec << ": r3=0x" << std::hex
                      << fd << " is not an IOS handle (cache sync)" << std::dec
                      << std::endl;
          }
          ctx.pc = old_pc;
          return false;
        }
        break;
      }
      default: break;
    }
  }

  switch (syscall_id) {"""
    text = text.replace(old, new, 1)
    ios.write_text(text)

# ---- 3. the async ioctlv vector count must be possible --------------------
plat = runtime / 'src/platform/wii_platform.cpp'
text = plat.read_text()
if MARK not in text:
    old = """  uint32_t arg_in = ctx.gpr[5];
  uint32_t arg_out = ctx.gpr[6];
  uint32_t arg_array = ctx.gpr[7];

  req.arg_cnt_in = arg_in;
  req.arg_cnt_out = arg_out;

  for (uint32_t i = 0; i < arg_in + arg_out; i++) {
      IoctlvVector vec;
      vec.addr = ctx.mmu.read32(arg_array + i * 8);
      vec.len = ctx.mmu.read32(arg_array + i * 8 + 4);
      req.ioctlv_vecs.push_back(vec);
  }

  result = IOSKernel::get().ioctlv(ctx, req);"""
    assert text.count(old) == 1, 'async ioctlv vector anchor'
    new = """  uint32_t arg_in = ctx.gpr[5];
  uint32_t arg_out = ctx.gpr[6];
  uint32_t arg_array = ctx.gpr[7];

  // """ + MARK + """: a caller that is not an IOS request can leave anything
  // in these registers. A real GX/IOS ioctlv uses at most a handful of
  // vectors, so an impossible count or an unreadable array is refused
  // instead of being allowed to allocate for it.
  if (arg_in + arg_out > 64 || (arg_array != 0 &&
      (arg_array < 0x80000000u || arg_array >= 0x94000000u))) {
      std::cout << "[HLE IOS] IOS_IoctlvAsync: refusing impossible vector list"
                << " in=" << arg_in << " out=" << arg_out << " array=0x"
                << std::hex << arg_array << std::dec << std::endl;
      ctx.gpr[3] = IPC_EINVAL;
      ctx.pc = ctx.lr;
      return;
  }

  req.arg_cnt_in = arg_in;
  req.arg_cnt_out = arg_out;

  for (uint32_t i = 0; i < arg_in + arg_out; i++) {
      IoctlvVector vec;
      vec.addr = ctx.mmu.read32(arg_array + i * 8);
      vec.len = ctx.mmu.read32(arg_array + i * 8 + 4);
      req.ioctlv_vecs.push_back(vec);
  }

  result = IOSKernel::get().ioctlv(ctx, req);"""
    text = text.replace(old, new, 1)
    plat.write_text(text)
print('Fast syscall guard installed; impossible ioctlv vector lists refused.')
