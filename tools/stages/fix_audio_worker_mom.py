r"""AX command lists on a worker thread (port of Crash of the Titans tools/fix_audio_worker.py, NATIVE_AX_WORKER_V1).

MoM's AX HLE (include/runtime/native_ax, src/hw/hw_dsp.cpp) is the same code as CotT's. The edits are CotT's, verbatim,
except that MoM's native_dsp_send has no native_trace::Scope line, so that line is dropped from both sides. In MoM's
guest profile (prof5) AX mixing shows on the guest thread (GetInputSamples / native_ax lambdas). CotT's notes:

AX command lists run on a worker thread instead of inside the guest's DSP-mailbox write.

Capture frame-cost-20261006-221021: the guest thread spent 78 of its 2,913 samples (about 0.9 ms per frame) inside the DSP mailbox write handler,
NativeAX::Protocol::Send -> AXWiiUCode::HandleCommandList -> ProcessPBList (PCM mixing).

The DSP is a separate processor on real hardware: the CPU writes the command-list address, the DSP mixes, then answers with a YIELD mail and an
interrupt. This emulation already delivers those replies through the guest's virtual-time event scheduler, 1 ms of guest time after the write.
So the mixing can run concurrently with the guest for that millisecond and be joined by the delivery event; the guest observes the same mails at
the same guest times:
  * Protocol::Send posts `Execute` + the YIELD mail as a job (state machine and everything else stay on the guest thread);
  * the job is joined (and its mails queued, in the order a synchronous run produced them) by the delivery event 1 ms later, by the next
    mailbox write, and by a DSP reset, whichever comes first;
  * the worker only touches guest RAM the same way the synchronous code did (parameter blocks, sample and output buffers); the guest cannot
    legitimately read those before the YIELD mail, exactly as on hardware.
NWII_NO_AUDIO_THREAD=1 restores the synchronous behaviour. A failed job is reported at its join and exits like the synchronous failure did.
Idempotent.

Edits vendor/NWiiRecomp/nWiiRuntime and export/nWiiRuntime. Idempotent; --restore puts every file back exactly
(backups under logs/stage-backups/fix_audio_worker_mom).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

MARK = 'NATIVE_AX_WORKER_V1'
ROOT = Path(__file__).resolve().parents[2]
TREES = {'vendor': ROOT / 'vendor/NWiiRecomp/nWiiRuntime', 'export': ROOT / 'export/nWiiRuntime'}
BACKUP = ROOT / 'logs/stage-backups/fix_audio_worker_mom'
EDITS = [('include/runtime/native_ax/protocol.h', [('  u32 crc=0;\n  unsigned command_frames=0;\n', '  u32 crc=0;\n  unsigned command_frames=0;\n  std::function<void(std::function<void()>)> async_exec; // NATIVE_AX_WORKER_V1: runs a command list off the guest thread\n  bool job_posted=false;\n'), ('      ax->Execute(mail,command_size); // Produces SYNC only after actual PCM output.\n      ++command_frames;\n      dsp.mail.PushMail(0xdcd10002,true); // YIELD after sample/history writeback.\n      state=State::Next;return;\n', '      {\n        const u32 list=mail;const u16 size=command_size;\n        auto work=[this,list,size]{\n          ax->Execute(list,size); // Produces SYNC only after actual PCM output.\n          ++command_frames;\n          dsp.mail.PushMail(0xdcd10002,true); // YIELD after sample/history writeback.\n        };\n        if(async_exec){async_exec(std::move(work));job_posted=true;}else work();\n      }\n      state=State::Next;return;\n')]), ('src/hw/hw_dsp.cpp', [('#include "runtime/event_scheduler.h"\n', '#include "runtime/event_scheduler.h"\n#include <thread>\n#include <condition_variable>\n#include <functional>\n#include <string>\n'), ('static std::vector<uint32_t> native_dsp_mails;\nstatic bool native_mail_scheduled=false;\nstatic void native_dsp_schedule_mail() {\n    if(native_mail_scheduled||native_dsp_mails.empty()||(dsp_mbox_dsp_hi&0x8000))return;\n    native_mail_scheduled=true;\n    EventScheduler::get().schedule_after(std::max<uint64_t>(1,g_ctx_ptr->tb_freq/1000),[](CPUContext&,uint64_t){\n        native_mail_scheduled=false;\n        if(native_dsp_mails.empty()||(dsp_mbox_dsp_hi&0x8000))return;\n        auto mail=native_dsp_mails.front();native_dsp_mails.erase(native_dsp_mails.begin());\n        dsp_mbox_dsp_hi=uint16_t(mail>>16);dsp_mbox_dsp_lo=uint16_t(mail);\n        dsp_control|=0x0080;dsp_update_pi();\n    });\n}\nstatic void native_dsp_send(uint32_t mail) {\n    try {\n        auto& processor=native_ax();processor.Send(mail);\n        auto& replies=processor.dsp.mail.pending;\n        native_dsp_mails.insert(native_dsp_mails.end(),replies.begin(),replies.end());replies.clear();\n        if(std::getenv("NWII_AX_PROBE")&&processor.command_frames && (processor.command_frames<=5||processor.command_frames%1000==0))\n            std::cout<<"[AX] frames="<<processor.command_frames<<" last_mail="<<std::hex<<mail<<std::dec<<"\\n";\n        native_dsp_schedule_mail();\n    }catch(const std::exception& error){\n        std::cerr<<"[AX] DSP processing failed: mail="<<std::hex<<mail<<std::dec<<" "<<error.what()<<std::endl;\n        // Unsupported work is never acknowledged as successful.\n        std::exit(1);\n    }\n}\nstatic void native_dsp_reset() {\n    native_ax().Reset();native_dsp_mails.clear();\n', 'static std::vector<uint32_t> native_dsp_mails;\nstatic bool native_mail_scheduled=false;\n// NATIVE_AX_WORKER_V1: see tools/fix_audio_worker.py\nstruct NativeAudioWorker {\n    std::mutex mutex;\n    std::condition_variable wake, idle;\n    std::function<void()> job;\n    bool has_job=false, busy=false, started=false;\n    std::string error;\n    void post(std::function<void()> work) {\n        std::unique_lock<std::mutex> lock(mutex);\n        idle.wait(lock,[&]{return !busy;});\n        if(!started){started=true;std::thread([this]{loop();}).detach();}\n        job=std::move(work);has_job=true;busy=true;\n        wake.notify_one();\n    }\n    void wait() {\n        std::unique_lock<std::mutex> lock(mutex);\n        idle.wait(lock,[&]{return !busy;});\n    }\n    std::string failure() {std::lock_guard<std::mutex> lock(mutex);return error;}\n    void loop() {\n        for(;;){\n            std::function<void()> work;\n            {std::unique_lock<std::mutex> lock(mutex);wake.wait(lock,[&]{return has_job;});work=std::move(job);has_job=false;}\n            std::string failed;\n            try{work();}catch(const std::exception& e){failed=e.what();}catch(...){failed="unknown exception";}\n            {std::lock_guard<std::mutex> lock(mutex);if(!failed.empty())error=failed;busy=false;}\n            idle.notify_all();\n        }\n    }\n};\nstatic NativeAudioWorker& native_audio_worker() {static NativeAudioWorker* worker=new NativeAudioWorker;return *worker;}\nstatic bool native_audio_inflight=false;\nstatic void native_dsp_deliver_one() {\n    if(native_dsp_mails.empty()||(dsp_mbox_dsp_hi&0x8000))return;\n    auto mail=native_dsp_mails.front();native_dsp_mails.erase(native_dsp_mails.begin());\n    dsp_mbox_dsp_hi=uint16_t(mail>>16);dsp_mbox_dsp_lo=uint16_t(mail);\n    dsp_control|=0x0080;dsp_update_pi();\n}\nstatic void native_dsp_schedule_mail() {\n    if(native_mail_scheduled||native_dsp_mails.empty()||(dsp_mbox_dsp_hi&0x8000))return;\n    native_mail_scheduled=true;\n    EventScheduler::get().schedule_after(std::max<uint64_t>(1,g_ctx_ptr->tb_freq/1000),[](CPUContext&,uint64_t){\n        native_mail_scheduled=false;\n        native_dsp_deliver_one();\n    });\n}\n// Finish the posted command list and queue the mails it produced, in the order a synchronous run produced them.\nstatic void native_dsp_join_audio() {\n    if(!native_audio_inflight)return;\n    auto& worker=native_audio_worker();\n    worker.wait();\n    native_audio_inflight=false;\n    const std::string failed=worker.failure();\n    if(!failed.empty()){\n        std::cerr<<"[AX] DSP processing failed: "<<failed<<std::endl;\n        std::exit(1);\n    }\n    auto& replies=native_ax().dsp.mail.pending;\n    native_dsp_mails.insert(native_dsp_mails.end(),replies.begin(),replies.end());replies.clear();\n}\nstatic void native_dsp_prepare_send() {\n    auto& processor=native_ax();\n    static const bool threaded=std::getenv("NWII_NO_AUDIO_THREAD")==nullptr;\n    if(threaded&&!processor.async_exec)processor.async_exec=[](std::function<void()> work){native_audio_worker().post(std::move(work));};\n    native_dsp_join_audio();   // the previous command list must be finished (and its mails queued) before the protocol runs again\n    processor.job_posted=false;\n}\nstatic void native_dsp_send(uint32_t mail) {\n    try {\n        native_dsp_prepare_send();\n        auto& processor=native_ax();processor.Send(mail);\n        if(processor.job_posted){\n            native_audio_inflight=true;\n            // The guest sees the replies where the synchronous version delivered them: one millisecond of guest time after the write.\n            EventScheduler::get().schedule_after(std::max<uint64_t>(1,g_ctx_ptr->tb_freq/1000),[](CPUContext&,uint64_t){\n                native_dsp_join_audio();\n                if(!native_mail_scheduled)native_dsp_deliver_one();\n            });\n            return;\n        }\n        auto& replies=processor.dsp.mail.pending;\n        native_dsp_mails.insert(native_dsp_mails.end(),replies.begin(),replies.end());replies.clear();\n        static const bool ax_probe=std::getenv("NWII_AX_PROBE")!=nullptr;\n        if(ax_probe&&processor.command_frames && (processor.command_frames<=5||processor.command_frames%1000==0))\n            std::cout<<"[AX] frames="<<processor.command_frames<<" last_mail="<<std::hex<<mail<<std::dec<<"\\n";\n        native_dsp_schedule_mail();\n    }catch(const std::exception& error){\n        std::cerr<<"[AX] DSP processing failed: mail="<<std::hex<<mail<<std::dec<<" "<<error.what()<<std::endl;\n        // Unsupported work is never acknowledged as successful.\n        std::exit(1);\n    }\n}\nstatic void native_dsp_reset() {\n    native_dsp_join_audio();\n    native_ax().Reset();native_dsp_mails.clear();\n')])]


def main():
    manifest_path = BACKUP / 'manifest.json'
    if '--restore' in sys.argv:
        if not manifest_path.exists():
            print('audio worker: nothing to restore.'); return
        manifest = json.loads(manifest_path.read_text())
        for key, digest in manifest.items():
            if hashlib.sha256((ROOT / key).read_bytes()).hexdigest() != digest:
                print('fix_audio_worker_mom: %s changed since this stage wrote it' % key); sys.exit(1)
        for key in manifest:
            shutil.copyfile(BACKUP / key, ROOT / key)
        shutil.rmtree(BACKUP)
        print('audio worker removed: %d file(s) restored.' % len(manifest)); return
    plan = []
    for base in TREES.values():
        for rel, pairs in EDITS:
            path = base / rel
            raw = path.read_bytes().decode('utf-8', errors='surrogateescape')
            if MARK in raw:
                continue
            crlf = '\r\n' in raw
            t = raw.replace('\r\n', '\n')
            for old, new in pairs:
                if t.count(old) != 1:
                    print('fix_audio_worker_mom: %s: anchor found %d times' % (path, t.count(old))); sys.exit(1)
                t = t.replace(old, new, 1)
            plan.append((path, raw, t.replace('\n', '\r\n') if crlf else t))
    if not plan:
        print('audio worker already installed.'); return
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    for path, raw, new in plan:
        key = str(path.relative_to(ROOT)).replace(chr(92), '/')
        dst = BACKUP / key
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(raw.encode('utf-8', errors='surrogateescape'))
        path.write_bytes(new.encode('utf-8', errors='surrogateescape'))
        manifest[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    BACKUP.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=1))
    print('audio worker installed: %d file(s) changed.' % len(plan))


if __name__ == '__main__':
    main()
