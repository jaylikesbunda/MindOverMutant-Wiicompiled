#pragma once
// NATIVE_MOM_VERBOSE_GUEST_LOG_V1: one cached launch-time switch for the guest
// thread's unconditional progress prints.
//
// Every "[HLE Callback] Dispatching", "[HLE PI] Context restore", "[Sched]" and
// "[NativeWPAD]" line is formatted on the guest CPU thread even though it only
// describes what the port already did, and the callback line and the dispatcher
// report also end in std::endl - a flush plus a write syscall per callback.  With
// ~27 % of guest CPU in ntdll and 3.6 % in ucrtbase, none of that is free.
//
// They are off unless NWII_LOG_VERBOSE=1.  Error and warning lines are untouched:
// std::cerr uses, "[NativeWPAD] Cannot open script", "[NativeWPAD] Invalid
// timeline", the run_game zero-PC dump, the heartbeat and every [Sample]/[DIAG]
// counter line keep their old behaviour (the counters are the measurement).
//
// A plain bool read on one thread: a disabled print costs one load and a
// predicted branch, and the flag is resolved once at startup.  It is declared
// `extern` and defined in ios.cpp rather than being an inline variable here: an
// inline variable with a getenv() initializer is dynamically initialised, so
// every use site carries a guard variable and a "is it initialised yet" branch.
namespace nwii::runtime {
extern const bool g_log_verbose;
}
