#!/usr/bin/env python3
"""apply_stages.py - port the Crash of the Titans runtime stage fixes onto this tree.

The CotT port applies ~300 per-function stage scripts after its patch. Four of
them target the failure classes MoM hits first, and all four are game-independent
(no guest addresses, except one gate that is re-keyed here):

  fix_virtual_disc_region_index.py   prefix-max index for overlapping disc extents
                                     (fixes the "[VDisc] MISS ... no file region" reads)
  fix_guest_idle_wait.py             park the verified SDK idle loop on wall-clock
                                     deadlines instead of spinning
  fix_single_vi_source.py            one VI interrupt source, opt-in
                                     (NWII_SINGLE_VI=1 with NWII_WALLTB=1)
  fix_event_mask.py                  VI/event mask handling

Order matters: the idle-wait stage guards against the two later stages rewriting
the same host-loop line, exactly as its own comment documents.

The scripts are copied verbatim into ``tools/stages/`` and adapted here rather
than edited in place, so the diff against the CotT originals stays visible:

  * their staged-tree paths (``build/nwii-full-source/nWiiRuntime``) become this
    repo's vendored runtime (``vendor/NWiiRecomp/nWiiRuntime``)
  * their ``runtime-overlays`` directory becomes ``overlays/``
  * ``Path(__file__).resolve().parents[1]`` becomes this repo root, so the copies
    can be run from anywhere
  * the idle-wait gate is re-keyed from CotT to MoM (see REKEY below)

Usage:
    python tools/apply_stages.py                 # apply to vendor/NWiiRecomp
    python tools/apply_stages.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAGES_DIR = os.path.join(ROOT, "tools", "stages")
RUNTIME = os.path.join("vendor", "NWiiRecomp", "nWiiRuntime")

STAGES = [
    # The idle-wait stage installs the native_idle::notify() wake-ups that
    # fix_single_vi_source.py and fix_event_mask.py rewrite, so it has to run
    # first. (It used to be absent, and the runner deleted the notify() lines
    # it had inserted; that is no longer the case now the stage is ported.)
    "fix_guest_idle_wait_mom.py",
    "fix_virtual_disc_region_index.py",
    "fix_single_vi_source.py",
    "fix_event_mask.py",
    "fix_watch_chain.py",
    "prepare_ax_backend.py",
    "fix_dsp_ax.py",
    # Local stages (not ported from CotT):
    "fix_fp_available.py",
    "fix_fp_context.py",
    "fix_paired_single_alias.py",
    "fix_fp_ownership.py",
    "fix_hle_false_positive.py",
    "fix_xfb_copy_color_mask.py",
    "fix_gx_lines_points.py",
    "fix_gpu_completion_noblock.py",
    "fix_efb_texture_freshness.py",
    "fix_gx_sdk_state.py",
    "fix_dec_repeat_drop.py",
    "fix_callback_return_owner.py",
    "fix_tev_compare_mode.py",
    "fix_gx_fidelity.py",
    "fix_guest_compile_flags.py",
    "fix_raw_gamepad.py",
    "fix_sdl_joystick_init.py",
    "fix_texture_alloc_guard.py",
    "fix_poll_fast_path.py",
    # A cache-sync `sc` must not be decoded as an IOS syscall: a leftover r0
    # dispatched a garbage ioctlv that allocated until std::bad_alloc.
    "fix_fast_syscall_guard.py",
    # Opt-in: capture the frame and the TEV/TLUT state when the whole picture
    # is tinted green (a wash, not a green level).
    "fix_green_wash_probe.py",
    # Correlate a green (black-texture) frame with texture-cache clears.
    "fix_green_cause_probe.py",
    # Decide whether a black texture is a torn read or a stale clear.
    "fix_texture_zero_probe.py",
    # Record the last draws and dump them on a captured green frame.
    "fix_draw_ring_probe.py",
    # Log any copy clear that is not black.
    "fix_copy_clear_probe.py",
    # Report the texture the last draw sampled when a frame is captured.
    "fix_texture_capture_probe.py",
    # Record the quad's texture coordinates in the draw ring.
    "fix_draw_ring_uv.py",
    # --- the CotT runtime performance bundle (ported 2026-10-20) -------
    # Order matters: the FIFO batching has to be in place before the
    # quantised gather and the gather redirect, the three display-list
    # stages belong together, and the clock cache invalidates next to the
    # idle park that fix_guest_idle_wait_mom installs.
    "fix_inline_ram_access.py",
    "fix_mmio_lookup_cache.py",
    "fix_gx_fifo_batch.py",
    "fix_perf_quant_gather.py",
    "fix_gather_redirect_span.py",
    # --- second performance batch (ported 2026-10-20) --------------------
    # The ordinary-FIFO fast path must precede the redirect cache: it is what
    # makes PI FIFO base/end/g_cp_fifo_base externally visible, and the cache
    # reads them directly. Both keep the byte path for probes and watches.
    "fix_gather_ordinary_fast.py",
    "fix_gather_redirect_cache.py",
    # CotT's final form of those two: hoist the launch-fixed probe/diagnostic
    # flags out of the per-write path (anchors are the text the two above install).
    "fix_gather_guard_outline.py",
    "fix_fifo_mutex_srwlock.py",
    "fix_ios_bulk_transfer.py",
    "fix_clock_cache.py",
    "fix_event_scheduler_fastpath.py",
    "fix_timebase_scaling.py",
    # After fix_timebase_scaling: it rewrites the same read_timebase().
    "fix_timebase_cache.py",
    "fix_mmu_slow_scalar.py",
    "fix_dl_hash_interleave.py",
    "fix_shared_draw_bytes.py",
    "fix_parse_work_reuse.py",
    "fix_display_list_gpu_ownership.py",
    "fix_cp_derived_skip.py",
    "fix_direct_vertex_decode.py",
    "fix_sampler_mode_cache.py",
    # fix_used_texture_binding binds only the texture units the mask models.
    # The mask does not model the induction map, so the video's chroma planes
    # were left unbound and the film played greyscale. It stays out until the
    # mask covers every sampler the generated TEV shader reads.
    "fix_vertex_reuse.py",
    # In-place vertex writer: depends on fix_vertex_reuse's ConvertVertex.
    "fix_vertex_inplace.py",
    "fix_indexed_batches.py",
    "fix_gl_raster_shadow.py",
    "fix_program_binary_cache.py",
    "fix_parallel_decode.py",
    # fix_static_ram_base needs a new translation unit registered in the
    # build's CMakeLists; this tree builds the runtime from export/nWiiRuntime
    # with its own list, so that stage is deferred (medium value).
    #
    # fix_early_gpu_signal moves the PE token/finish signal ahead of the GL
    # submission. It is worth 8.7 ms/frame of guest idle in CotT, but here it
    # made the presented picture flicker between a finished frame and a
    # half-rendered one (every second frame came out nearly black), so it
    # stays out until the present path can be re-ordered with it.
    #
    # The cache-range loops are collapsed in the *generated* guest code, so
    # this stage has to run after the recompiler regenerates it.
    "fix_cache_range_loops_mom.py",
    # --- generated guest-code performance bundle (CotT codegen lane) -----
    # These four all rewrite code the recompiler generated (the 70 chunks plus
    # main_output.cpp), so they lead the tail of the list: everything that
    # regenerates or rewrites the generated code has already run.  Order matters
    # between them:
    #   * pruning the never-taken entry cases first, because it is what makes
    #     straight-line runs exist at all (with one case per instruction every
    #     label is a jump target, so the count batching can merge nothing);
    #   * the count batching second, because it merges the per-instruction
    #     ``++ctx.inst_count`` that fix_cache_range_loops_mom anchors on.
    # Both only remove host work: the entry switches keep ``default:
    # interpret_step`` and the counter stays exact wherever anything reads it.
    "fix_prune_entry_cases_mom.py",
    "fix_inst_count_batch_mom.py",
    # The dispatcher's own per-call path (main_output.cpp): it was the only
    # generated translation unit compiled at -O0, and its target resolution is a
    # binary search over 128,850 immutable override records on every guest
    # bl/blr.  Both are host-side only: -O2 changes code generation alone, and
    # the memo is keyed on the full guest pc.
    "fix_dispatcher_o2_mom.py",
    "fix_dispatch_hot_cache_mom.py",
    # --- guest register promotion (CotT's promote_regs, re-derived) ------
    # Keeps the hottest guest registers in host locals inside the functions that
    # hold 90% of this port's guest instructions (tools/hot_functions_mom.txt,
    # profiled from this tree's own poll-site census).  It has to run after the
    # two chunk stages above, whose statement shapes it parses (pruned entry
    # switches, batched inst_count lines), and after the dispatcher stages, and
    # nothing later may rewrite export/.
    #
    # It is registered but OFF by default: measured neutral on this port (see
    # the stage docstring and logs/cott-promote-regs.md).  NWII_BUILD_PROMOTE_REGS=1
    # turns it on; deleting this line is a complete rollback.
    "promote_regs_mom.py",
    # Host-backed ISFS so the game can save (CotT's virtual Wii storage).
    "fix_nand_filesystem.py",
    "fix_perf_census.py",
    "fix_perf_phase_trace.py",
    "fix_perf_frame_trace.py",
    # In-process RIP sampler for both runtime threads plus an offline resolver,
    # so a phase can be split by function and by module (our code vs the driver).
    "fix_cpu_profile_mom.py",
    # --- quality lane (CotT work re-derived against this tree) -----------
    # Integer normals were decoded with shift 0, so a sphere/environment map
    # driven by them tiled 64x-16384x too often (speckle and shimmer).
    "01_fix_normal_scale_mom.py",
    # The EFB is RGBA8 here, so draw alpha leaked into EFB-to-texture copies
    # and into the window (a translucent surface).
    "02_fix_alpha_isolation_mom.py",
    # Mipmapped sampling: a minified texture was point-sampled from level 0.
    "03_fix_texture_mipmaps_mom.py",
    # A level-0-only texture is incomplete under a mip filter and samples as
    # black; built late, that made the first draw of a new texture a green
    # full-screen flash.
    "fix_mipmap_first_draw.py",
    # The right stick drives the Wii pointer; the mouse no longer fights it.
    "04_fix_gamepad_pointer_mom.py",
    # A real audio jitter buffer instead of a 1 s ADMA ring that starved.
    "05_fix_audio_jitter_buffer_mom.py",
    # The device period was never the problem (626 callbacks x 512 frames =
    # 32051 frames/s in every window).  The ring sat at its 30 ms clamp
    # saturation point, the 60 ms re-prime turned each underrun into 60+ ms of
    # silence, and the DMA event period was latched at enable time while the
    # callback read the live block count.  Anchors are the text 05 installs.
    "fix_audio_stutter_mom.py",
    # Borderless fullscreen, F11, anisotropic filtering.
    "06_fix_user_options_mom.py",
    # Frame-rate counters in the window title.
    "07_fix_present_metrics_mom.py",
    # NWII_FPS_LIMIT: host pacing and the guest VI agree on the rate.
    "08_fix_frame_limit_mom.py",
    # --- measurement tooling (ported from the CotT performance tooling) ---
    # Both stages are opt-in at run time and change no guest behaviour: they
    # add a new header, new env-var-gated counters and a new report line, and
    # they touch none of the existing diagnostics' output format.
    # --- renderer submission batching (re-derived from the CotT renderer) ---
    # Keeps one GL batch open across adjacent draws and canonicalises the
    # topology key (CotT's fix_renderer_submission + fix_flush_reason_census).
    # Last, because it rewrites the Render loop and EmitPrimitive that the GX
    # stages above install, and it anchors on their final text.
    "fix_renderer_batching_mom.py",
    # F3 developer overlay and F4 export (CotT's fix_dev_hud + fix_stutter_watch
    # re-derived).  Opt-in at run time, free when hidden, and it reads
    # native_perf_phase's per-frame ring rather than timing anything itself.
    # Last, because nothing after it may rewrite the host event loop.
    "fix_dev_hud_mom.py",
    # The shader cache key hashed BP 0x40..0x43 (ZMode and the blend /
    # destination-alpha registers).  Those are applied as GL state by
    # ApplyZMode/ApplyBlend and are never read by the shader generator, so
    # every distinct depth/blend setting compiled an identical program again.
    # Last, because it rewrites GetShaderHash and the compile site in
    # renderer_gl.cpp, which the renderer stage above already finalised.
    "fix_shader_key_mom.py",
    # 1 ms of speculative idle park instead of 250 us, and no cancel syscall:
    # the guest was re-entering the park ~35 times per frame (ntdll = 25% of
    # guest CPU) and audio production rides on guest progress.
    "fix_idle_park_churn_mom.py",
    # NativeTexGen evaluated all 8 generators per vertex regardless of
    # numTexGens (the movie quad declares 0); 9-12% of host CPU sat here.
    # Supersedes the first texgen bound: the old one decoded bp[0x28+i] as a map
    # index (it is a two-stage register, so it read texcoord bits as an enable
    # bit) and under-counted lanes, which made stages sample vTex7 -> NaN ->
    # invisible character textures.  Also drops the 160-byte zero-fill.
    "fix_vertex_pass_mom.py",
    # 10-19 identical glUniform* uploads per draw run, including constant
    # sampler units; the driver is 25-45% of host CPU.  Exact-value cache.
    "fix_uniform_cache_mom.py",
    # glBufferData reallocated the vertex and index buffers on every submission
    # (~170/frame); an 8 MiB ring appends instead and orphans only on wrap.
    "fix_upload_ring_mom.py",
    # ~800 redundant driver binds per frame at 270 draws/frame: one VAO and a
    # handful of programs, mostly re-bound to the value already in force.
    "fix_bind_shadow_mom.py",
    # A PE token with no new submission since the last flush has nothing to
    # flush; ~240 flushes/s force a driver submit and a kernel transition each.
    "fix_pe_flush_elide_mom.py",
    # native_wpad_poll ran on every one of ~7M guest polls/s; the pad state is
    # level-triggered and republished, so every eighth poll is enough.
    "fix_poll_credit_mom.py",
    # The texture cache stays content-keyed: keying it on identity let two
    # different textures at the same address share one GL texture, and because
    # draws are batched the earlier draw sampled the later upload (bald
    # characters, missing props).  The cache-wipe hitch is addressed separately.
    # "fix_texture_cache_mom.py",
    # Present only when a new XFB generation exists (CotT's rate gate, not its
    # ordering, which this tree already has).  The loop was swapping ~4x per
    # new image: present 35.2/s against 8.8 new frames/s.
    "fix_present_pacing_mom.py",
    # The cache wiped every GL texture at 512 entries, and deferred draws then
    # sampled deleted names: a character lost its albedo and effect quads drew
    # black.  Outdoors (many distinct textures) reached the limit, indoors did
    # not - which is why it looked like a build regression.  Evict LRU instead.
    "fix_texture_evict_mom.py",
    # store() in the program-binary cache bailed out whenever an earlier GL call
    # had left an error, so 2,924 shader keys produced only 864 cached binaries
    # and every run recompiled ~13 programs per second inside the draw call.
    "fix_program_store_mom.py",
    # 2,270 state keys produced only ~885 distinct shaders; the rest were the
    # same GLSL under a different key, each paying a link+JIT inside the draw
    # call (the 20-120 ms gl_render spikes).  Cache programs by source.
    "fix_shader_src_dedup_mom.py",
    # The dedupe above returned from SetupDrawState before glUseProgram, so every
    # reused program drew with the previous bound program and the previous state
    # (vanishing character, black effect quads).  Moves the dedupe into
    # CompileShader and removes the early return.
    "fix_shader_dedupe_flow_mom.py",
    # Key memoisation by memcmp of the words the generator reads, shader binary
    # writes moved out of the draw, and cache-key pollution pruned (cp[0x50/0x60],
    # xf[0x103F], texStage formats, bp[0x0F/0x20-0x24/0xE9-0xED] are never read by
    # the generator).  Must follow the three shader stages above.
    "fix_shader_lane_mom.py",
    # Makes the ring/uniform-cache/bind-shadow trio safe: the uniform cache now
    # tracks the actually-bound program and invalidates on untracked uploads, the
    # bind shadows resync per drain, and a ring fallback resets its cursor.
    "fix_render_state_safe_mom.py",
    # Fog/alpha *data* becomes three uniforms instead of shader text
    # (NWII_LITERAL_SHADER_DATA=1 restores literals); also removes the stale
    # s_shader_by_src early return that skipped NativeUseProgram/state setup
    # and adds exact-string collision checks to both source-dedup maps.
    "fix_shader_data_uniforms_mom.py",
    # Program-binary file writes move to a bounded worker (128 jobs/8 MiB,
    # atomic temp+rename, counters); NWII_NO_SHADER_ASYNC_STORE=1 restores the
    # synchronous write.  load() also rejects trailing payloads now.
    "fix_shader_cache_io_mom.py",
    # Cached binaries are read+validated on an I/O worker and linked in a 2 ms
    # render-thread pump (NWII_NO_SHADER_PRELOAD=1 disables).  Must run after
    # the cache-io stage: it anchors on its include line.
    "fix_shader_preload_mom.py",
    # Guest memory lane: lmw/stmw spans, one index-domain proof per draw, MEM2
    # inline paths, fill blocks, psq pairs.  Last: it rewrites export/ chunks.
    "fix_guest_memory_lane_mom.py",
    # Qualified draws keep object-space position/normal (and regular texgen) and
    # the vertex shader applies the parse-time XF banks - CotT's
    # fix_gpu_vertex_pipeline re-derived.  NWII_NO_GPU_VERTEX=1 is today's
    # per-draw path; NWII_NO_GPU_TEXGEN=1 keeps texgen on the CPU.  Last: it
    # anchors on the writer, the batch boundary and the compile site that the
    # stages above finalise, and it rewrites the shader generator.
    "fix_gpu_vertex_mom.py",
    # Per-command EFB-copy descriptor snapshots (BP 0x52 freeze + display-list
    # resnapshot), a byte-threshold early drain with hysteresis and the exact
    # generation CV wake - reanchored to MoM's GXCommand and the FIFO handoff.
    # NWII_NO_GX_EARLY_DRAIN / NWII_NO_GX_WAKE_EXACT keep reference behaviour;
    # early PE stays off (texture-RAM lifetime hazard).
    "fix_gx_overlap_mom.py",
    # Per-command EFB-copy snapshot from parse BP registers (lead-authored).
    "fix_copy_snapshot_mom.py",
    # Byte-budgeted texture LRU (NWII_TEXTURE_CACHE_MB, default 256) + the
    # hard 4096-entry cap; names an open batch references are protected until
    # FlushBatch.
    "fix_texture_budget_mom.py",
    # Byte-budget LRU safety: protect() on every successful get_texture,
    # deferred clear, mip byte accounting.
    "fix_texture_budget_safety_mom.py",
    # Compact post-decode upload: 136-byte packed rows in FlushBatch only,
    # NWII_PACKED_VERTICES=1 opt-in (NWII_NO_PACKED_VERTICES wins), float path
    # unchanged.
    "fix_packed_upload_mom.py",
    # Persistent coherently-mapped segmented upload rings (opt-in).
    "fix_persistent_upload_mom.py",
    # Generated backward-branch polls gated behind native_poll_due(): a plain
    # credit (NWII_POLL_CREDIT, default 64, clamped 1..1024) plus the
    # native_idle::attention flag producers already raise.  The idle-loop PC
    # 0x80492408 and the pc-0/0xFFFFFFFC sentinels keep every-site polls;
    # IPC/DI poll-counted delays force the reload to 1.  The "Spinning at PC"
    # modulo folds into the taken branch.  Must run after every stage that
    # rewrites generated chunks (the memory lane above), and re-run after any
    # regeneration.
    "fix_poll_due_mom.py",
    # CPU->shared FIFO publish swaps the whole buffer when the destination is
    # empty; the host drain swaps g_hw_fifo out under the SRW lock and appends
    # into the (render-thread-owned) parse buffer after unlocking.  Byte
    # order, partial-command carries and the pending-byte count are unchanged.
    "fix_fifo_handoff_mom.py",
    # Guest-thread per-poll and per-print overhead: plain fast-poll counter
    # instead of a locked RMW (13.7 M/s), the empty test inlined before
    # native_gx_publish, the sampler flag test inlined, one verbose switch for
    # the guest progress prints, run_game's per-dispatch modulos replaced by
    # counters plus the WPAD range test, wgp_push probes hoisted, and the 5 s
    # DIAG report's clock read throttled to every 256th due poll.
    # NWII_MOM_TRIM=fcounter,gxpub,sample,verbose,dispatch,wgp,diagclock
    # (unset = all, none = reference); must run last, it edits
    # export/main_output.cpp.
    "fix_guest_runtime_trim_mom.py",
    # reference/mom-frame-gate.json (lead-verified against the DOL): wrap the
    # frame-gate's 1/60 threshold load at 0x8029E23C in output_35.cpp with
    # native_frame_limit::MinimumUpdateSeconds (NWII_FPS_LIMIT /
    # NWII_NO_GUEST_FRAME_LIMIT).  Validates the DOL hash and the generated
    # context; refuses rather than patching the wrong site.  The debug-step
    # load of the same constant at 0x8029E1F0 is never touched.
    "fix_guest_frame_gate_mom.py",
    "fix_mipmap_state_trace_mom.py",
    "fix_used_texture_binding_mom.py",
    "fix_live_prompts_mom.py",
    # Overlap host rendering with guest execution: per-command EFB copy
    # snapshots (correctness prerequisite), a byte-threshold drain request from
    # the publisher, and an exact condition-variable wake (no 2 ms cap).  Must
    # run last: it anchors on the host loop, native_gx_publish, the renderer's
    # BP 0x52 branch and the parser as the stages above leave them.
    # DISABLED pending a trustworthy baseline: "fix_gx_overlap_mom.py",
]

# Not ported: fix_guest_idle_wait.py. It parks the verified SDK idle loop, but it
# anchors on 'return native_process_callbacks_slow(ctx,due,false);' in ios.cpp,
# a shape this tree does not have - that call is introduced by intermediate CotT
# stages (the dispatch/idle-park family) that have not been ported. It is an
# efficiency fix (spin instead of park), not a correctness one, so it is left out
# until those predecessors are ported.

# Adaptations applied to every stage.
COMMON_SUBSTITUTIONS = [
    ("build/nwii-full-source/nWiiRuntime", RUNTIME.replace("\\", "/")),
    ("runtime-overlays", "overlays"),
    ("Path(__file__).resolve().parents[1]", 'Path(r"%s")' % ROOT.replace("\\", "\\\\")),
]

# The idle-wait gate names CotT's idle loop PC and an r13-relative OS-global.
# MoM's equivalents are derived, not guessed:
#   PC    0x803BBFA0 is FUN_803BBE4C+0x154; the cross-match finds MoM's twin as
#         FUN_804A0B34 (shape-identical), so the gate becomes 0x804A0B34+0x154.
#   r13   CotT reads r13-0x2948, the word next to the scheduler counter at
#         r13-0x2950. MoM's counter is at r13-0x4218 (`sched_lock_offset` in
#         config.toml), so the adjacent word is r13-0x4210 = r13-16896.
# If either re-key is wrong the gate simply never fires - it is a conjunction of
# positive conditions, so it fails safe rather than mis-parking the guest.
REKEY = {
    "fix_guest_idle_wait.py": [
        ("0x803BBFA0u", "0x804A0C88u"),
        ("10568u", "16896u"),
    ],
}

NOTE = "adapted by tools/apply_stages.py: paths re-rooted at this repo"

TEXT_SUFFIXES = {".h", ".hpp", ".cpp", ".inc", ".txt", ".toml", ".md", ".cmake"}


def normalize_newlines():
    """Restore LF in the vendored tree.

    The stage scripts write with Python's default newline translation, which turns
    pristine LF files into CRLF on Windows. Left alone that bloats the patch and
    makes ``git apply`` fail on a clean checkout, so every stage is followed by a
    normalisation pass over the vendor tree.
    """
    changed = 0
    for base, _dirs, files in os.walk(os.path.join(ROOT, "vendor")):
        for name in files:
            if os.path.splitext(name)[1].lower() not in TEXT_SUFFIXES:
                continue
            path = os.path.join(base, name)
            with open(path, "rb") as handle:
                data = handle.read()
            if b"\r\n" not in data:
                continue
            with open(path, "wb") as handle:
                handle.write(data.replace(b"\r\n", b"\n"))
            changed += 1
    return changed


def build_script(name):
    with open(os.path.join(STAGES_DIR, name), encoding="utf-8") as handle:
        text = handle.read()
    applied = []
    for needle, replacement in COMMON_SUBSTITUTIONS + REKEY.get(name, []):
        if needle in text:
            text = text.replace(needle, replacement)
            applied.append(needle if needle not in REKEY.get(name, []) else "%s -> %s" % (needle, replacement))
    if NOTE not in text:
        text = text.replace("\n", "\n# " + NOTE + "\n", 1)
    return text, applied


def strip_idle_wait_dependencies():
    """Kept for reference; the idle-wait stage is ported, so nothing is stripped.

    This used to delete every line that mentioned ``native_idle`` because the
    idle-wait stage was missing and the references were dangling. With
    ``fix_guest_idle_wait_mom.py`` in the stage list the references are real
    code, and this cleanup removed whole statements from main.cpp and ios.cpp.
    It is no longer called.
    """
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not os.path.isdir(os.path.join(ROOT, RUNTIME)):
        parser.error("%s not found - vendored NWiiRecomp missing" % RUNTIME)

    for name in STAGES:
        source, applied = build_script(name)
        print("\n=== %s ===" % name)
        for change in applied:
            print("   adapted: %s" % change)
        if args.dry_run:
            continue

        # Pre-flight on a copy: a stage that copies headers before hitting a
        # failed assert would otherwise leave a half-applied tree behind, which
        # is exactly how an orphaned include got installed once already.
        # The directory carries the PID so two runs (a hand-run and an agent's)
        # cannot delete each other's copy mid-flight.
        preflight_root = os.path.join(ROOT, "logs", "_preflight_%d" % os.getpid())
        preflight_runtime = os.path.join(preflight_root, RUNTIME)
        if os.path.isdir(preflight_runtime):
            shutil.rmtree(preflight_runtime, ignore_errors=True)
        os.makedirs(os.path.dirname(preflight_runtime), exist_ok=True)
        shutil.copytree(os.path.join(ROOT, RUNTIME), preflight_runtime)
        preflight_overlays = os.path.join(preflight_root, "overlays")
        if os.path.isdir(preflight_overlays):
            shutil.rmtree(preflight_overlays, ignore_errors=True)
        shutil.copytree(os.path.join(ROOT, "overlays"), preflight_overlays)
        # Stages may read the retained upstream sources (reference/dolphin-ax for
        # the AX backend), so the sandbox mirrors those too.
        preflight_reference = os.path.join(preflight_root, "reference")
        if os.path.isdir(os.path.join(ROOT, "reference")):
            if os.path.isdir(preflight_reference):
                shutil.rmtree(preflight_reference, ignore_errors=True)
            shutil.copytree(os.path.join(ROOT, "reference"), preflight_reference)
        preflight_source = source.replace(
            os.path.join(ROOT, RUNTIME).replace("\\", "/").replace("/", os.sep),
            preflight_runtime.replace("/", os.sep))
        preflight_source = preflight_source.replace(ROOT.replace("\\", "\\\\"),
                                                    preflight_root.replace("\\", "\\\\"))
        path = os.path.join(ROOT, "logs", "_stage_%s" % name)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(preflight_source)
        result = subprocess.run([sys.executable, path], cwd=ROOT, capture_output=True, text=True)
        if result.returncode != 0:
            print("   FAILED pre-flight (exit %d) - tree untouched" % result.returncode)
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            for line in detail[-3:]:
                print("   %s" % line[:180])
            sys.exit(result.returncode)

        # Pre-flight passed: run for real.
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(source)
        result = subprocess.run([sys.executable, path], cwd=ROOT, capture_output=True, text=True)
        output = (result.stdout or "").strip() or (result.stderr or "").strip()
        if output:
            print("   %s" % output.splitlines()[-1][:160])
        if result.returncode != 0:
            print("   FAILED (exit %d)" % result.returncode)
            sys.exit(result.returncode)

    print("\nstages applied" if not args.dry_run else "\ndry run only")
    if not args.dry_run:
        print("normalised %d file(s) to LF under vendor/" % normalize_newlines())
        # The idle-wait stage is ported, so the native_idle::notify() wake-ups
        # it installs are real code now. The old cleanup that deleted them
        # (it existed only because the stage was missing) has been removed;
        # running it here silently removed whole statements from main.cpp and
        # ios.cpp.


if __name__ == "__main__":
    main()
