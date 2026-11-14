#pragma once
// NATIVE_MOM_SHADER_CACHE_IO_V1: bounded filesystem writer for the program
// binary cache (MoM port of CotT's native_shader_cache_io.h; the include of
// CotT's native_frame_trace.h becomes this tree's runtime/frame_trace.h).
// GL binary queries stay on the context owner. This worker only saves owned
// bytes; it never receives a GL object, game RAM pointer or renderer state.
#include <array>
#include <atomic>
#include <condition_variable>
#include <cstring>
#include <deque>
#include <filesystem>
#include <fstream>
#include <mutex>
#include <thread>
#include <vector>
#include <string>
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <cerrno>
#include <fcntl.h>
#include <unistd.h>
#endif
#include "runtime/frame_trace.h"
namespace native_shader_cache {
class Writer {
 struct Job {std::filesystem::path path;std::array<char,24> header;std::vector<char> data;std::atomic<uint64_t>* successes;};
 std::mutex mutex;std::condition_variable wake,idle;std::deque<Job> jobs;
 std::thread thread;size_t bytes=0;bool stopping=false,busy=false;
 static bool save(const Job& job) {
  static std::atomic<uint64_t> sequence{0};
  for (unsigned attempt = 0; attempt < 32; ++attempt) {
   auto temporary = job.path;
#ifdef _WIN32
   temporary += "." + std::to_string(GetCurrentProcessId()) + "." +
                std::to_string(sequence.fetch_add(1)) + ".tmp";
   HANDLE file = CreateFileW(temporary.c_str(), GENERIC_WRITE, 0, nullptr,
                             CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
   if (file == INVALID_HANDLE_VALUE) {
    if (GetLastError() == ERROR_FILE_EXISTS || GetLastError() == ERROR_ALREADY_EXISTS) continue;
    return false;
   }
   DWORD written = 0;
   bool ok = WriteFile(file, job.header.data(), DWORD(job.header.size()), &written, nullptr)
             && written == job.header.size();
   if (ok) ok = WriteFile(file, job.data.data(), DWORD(job.data.size()), &written, nullptr)
                && written == job.data.size();
   if (ok) ok = FlushFileBuffers(file) != 0;
   if (!CloseHandle(file)) ok = false;
   if (ok) ok = MoveFileExW(temporary.c_str(), job.path.c_str(),
                            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH) != 0;
   if (!ok) DeleteFileW(temporary.c_str()); // This invocation exclusively created it.
   return ok;
#else
   temporary += "." + std::to_string(getpid()) + "." +
                std::to_string(sequence.fetch_add(1)) + ".tmp";
   const int file = ::open(temporary.c_str(), O_WRONLY | O_CREAT | O_EXCL, 0600);
   if (file < 0) { if (errno == EEXIST) continue; return false; }
   auto write_all = [file](const char* data, size_t bytes) {
    while (bytes) {
     const auto n = ::write(file, data, bytes);
     if (n < 0 && errno == EINTR) continue;
     if (n <= 0) return false;
     data += n; bytes -= size_t(n);
    }
    return true;
   };
   bool ok = write_all(job.header.data(), job.header.size()) &&
             write_all(job.data.data(), job.data.size());
   if (ok) ok = ::fsync(file) == 0;
   if (::close(file) != 0) ok = false;
   std::error_code error;
   if (ok) { std::filesystem::rename(temporary, job.path, error); ok = !error; }
   if (!ok) std::filesystem::remove(temporary, error);
   return ok;
#endif
  }
  return false;
 }
 void run(){for(;;){Job job;{std::unique_lock<std::mutex> lock(mutex);wake.wait(lock,[&]{return stopping || !jobs.empty();});if(jobs.empty() && stopping)return;
   job=std::move(jobs.front());jobs.pop_front();busy=true;}
   if(!save(job))++failed;
   else job.successes->fetch_add(1,std::memory_order_relaxed);
   {std::lock_guard<std::mutex> lock(mutex);bytes-=job.data.capacity();busy=false;if(jobs.empty())idle.notify_all();}
  }}
public:
 static bool write_blob(const std::filesystem::path& path, const std::vector<char>& blob) {
  if (blob.size() < 24) return false;
  Job job{path, {}, std::vector<char>(blob.begin() + 24, blob.end()), nullptr};
  std::memcpy(job.header.data(), blob.data(), 24);
  return save(job);
 }
 std::atomic<uint64_t> failed{0},skipped{0};
 // Bounds: 128 jobs, 8 MiB queued, 8 MiB per record.  A full queue reports
 // false; the caller skips the cache save rather than blocking a frame.
 bool can_accept(size_t size){std::lock_guard<std::mutex> lock(mutex);return !stopping && jobs.size()<128 && size<=(8u<<20) && bytes<=(8u<<20)-size;}
 bool enqueue(std::filesystem::path path,const void* header,std::vector<char> data,std::atomic<uint64_t>& success){
  std::lock_guard<std::mutex> lock(mutex);if(stopping || jobs.size()>=128 || data.capacity()>(8u<<20) || bytes>(8u<<20)-data.capacity()){++skipped;return false;}
  if(!thread.joinable()){try{thread=std::thread([this]{run();});}catch(const std::system_error&){++failed;return false;}} // worker startup failure skips the save, never the game
  Job job{std::move(path),{},std::move(data),&success};std::memcpy(job.header.data(),header,24);bytes+=job.data.capacity();jobs.push_back(std::move(job));wake.notify_one();return true;
 }
 void flush(){std::unique_lock<std::mutex> lock(mutex);idle.wait(lock,[&]{return jobs.empty() && !busy;});}
 ~Writer(){ {std::lock_guard<std::mutex> lock(mutex);stopping=true;}wake.notify_one();if(thread.joinable())thread.join();}
};
}
