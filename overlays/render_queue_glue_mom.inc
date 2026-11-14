// NATIVE_MOM_RENDER_QUEUE_V1: GL thread and batch queue for Mind over Mutant. Included by gx.cpp ahead of ProcessGXFifo;
// tools/stages/fix_render_queue_mom.py. Ported from the Crash of the Titans port's runtime-overlays/render_queue_glue.inc.
//
// Ownership. The front end (the main thread, which calls ProcessGXFifo) parses, decodes and raises the early PE signal.
// It hands each drain to the GL thread as a batch, and each frame's GL work (XFB composite, probes, HUD, swap) as a task.
// The GL thread is the only thread that calls GL, renders, or touches RenderState() and the texture cache. Everything a
// batch needs from the front end travels with it: the command vector and the parse snapshot. Items run in publish order,
// so a frame task sees every batch published before it.
#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <vector>
#include "runtime/native_render_queue.h"
#include "runtime/gx/render_state.h"

namespace nwii::runtime::render_queue {
struct Item {
    std::vector<gx::GXCommand> stream;   // batch: moved in by SubmitBatch
    std::function<void()> task;          // task: runs on the GL thread
    gx::ParseSnapshot snapshot{};        // batch: parse-owned view for RenderStateBegin
};
using Queue = native_render_queue::OrderedQueue<Item>;

static Queue* s_queue = nullptr;            // created by Start on the main thread, deleted by Stop
static std::thread s_thread;
static std::function<void()> s_bind_frame;  // GL thread: binds the EFB and its viewport before a batch
static gx::ParseSnapshot s_front_snapshot{};  // front end: the snapshot of the drain being prepared
static std::mutex s_title_mutex;
static std::string s_title;
static bool s_title_pending = false;

bool Active() { return s_queue != nullptr; }
gx::ParseSnapshot& FrontSnapshot() { return s_front_snapshot; }

// GL thread.
static void ExecuteItem(Item& item) {
    if (item.task) {
        std::function<void()> task = std::move(item.task);
        item.task = nullptr;
        task();
        return;
    }
    s_bind_frame();
    if (!g_renderer) {
        g_renderer = gx::IRenderer::Create();
        if (g_renderer) g_renderer->Initialize(nullptr);
    }
    gx::RenderBatchSnapshot() = item.snapshot;
    if (g_renderer) {
        native_perf_phase::Scope native_perf_phase_render(native_perf_phase::Phase_GlRender);
        const auto r0 = std::chrono::steady_clock::now();
        g_renderer->Render(item.stream);
        native_perf_phase_render.stop();
        g_stat_render_us += uint64_t(std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now() - r0).count());
    }
    std::vector<gx::GXCommand>().swap(item.stream);   // release the drain's commands on the thread that used them
}

// Main thread. The caller has released the context; make_current runs on the new thread, release when it stops.
void Start(std::function<void()> make_current, std::function<void()> release, std::function<void()> bind_frame) {
    if (s_queue) return;
    static const size_t depth = [] {
        const char* e = std::getenv("NWII_RENDER_QUEUE_DEPTH");
        return size_t(std::clamp(e ? std::atoi(e) : 8, 1, 16));
    }();
    s_bind_frame = std::move(bind_frame);
    s_queue = new Queue(depth);
    s_thread = std::thread([make_current, release] {
        make_current();
        s_queue->Run(ExecuteItem);
        release();
    });
}

// Front end: waits until the GL thread has completed everything published so far.
void Flush() {
    if (s_queue && s_queue->InFlight()) s_queue->Flush();
}
void FlushForSavestate() { Flush(); }

// Main thread, at shutdown: completes queued work, stops the GL thread and waits for it.
void Stop() {
    if (!s_queue) return;
    s_queue->Flush();
    s_queue->Stop();
    s_thread.join();
    delete s_queue;
    s_queue = nullptr;
}

// Front end: moves a parsed drain into the next free slot.
void SubmitBatch(std::vector<gx::GXCommand>& frame) {
    Item& slot = s_queue->Acquire();
    slot.stream = std::move(frame);
    slot.task = nullptr;
    slot.snapshot = s_front_snapshot;
    s_queue->Publish();
}

// Main thread: frame GL work (composite, present, HUD, thumbnail, screenshots), run after the batches published before it.
// It must never call ProcessGXFifo or anything else that submits to this queue: the GL thread would wait on itself.
void Post(std::function<void()> task) {
    Item& slot = s_queue->Acquire();
    std::vector<gx::GXCommand>().swap(slot.stream);
    slot.task = std::move(task);
    s_queue->Publish();
}

// GL thread: the window title is applied by the main thread (SDL window calls stay on the window's thread).
void SetTitle(const std::string& title) {
    std::lock_guard<std::mutex> lock(s_title_mutex);
    s_title = title;
    s_title_pending = true;
}
bool TakeTitle(std::string& out) {
    std::lock_guard<std::mutex> lock(s_title_mutex);
    if (!s_title_pending) return false;
    out = s_title;
    s_title_pending = false;
    return true;
}
} // namespace nwii::runtime::render_queue
