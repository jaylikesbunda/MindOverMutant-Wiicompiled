//! Disc releases the Mind over Mutant launcher can prepare (only the PAL RC8P7D disc is supported).
//! (Forked from the Crash of the Titans launcher.) Disc releases the launcher can prepare and build. The builder pack holds each release's data under `data/` (the PAL
//! reference) or `data/regions/<game id>/`; the values here must match those files (checked by the tests below).

pub struct Release {
    pub game_id: &'static str,
    pub label: &'static str,
    /// SHA-256 of the release's main.dol: the native build is only valid for this exact executable.
    pub dol_sha256: &'static str,
    /// Runtime HLE settings: the SDK's __OSDispatchInterrupt and the scheduler-disable counter's small-data offset.
    pub ext_interrupt_dispatch: u32,
    pub sched_lock_offset: u32,
    /// Size of `package\cdd70a8c.p3d` (the frontend package holding the title logo) in default.rcf.
    pub title_package_len: u32,
}

pub const RELEASES: &[Release] = &[
    Release {
        game_id: "RC8P7D",
        label: "PAL",
        dol_sha256: "9ac7b650b694005d86dd0e0fa66518ccd2eb855c33b815954f05a18c392d451b",
        // MoM: __OSDispatchInterrupt is deliberately NOT given to the runtime (with it, the runtime longjmps into a
        // dispatcher that does not acknowledge the source: a dispatch storm). 0 means "leave unset" in config.toml.
        ext_interrupt_dispatch: 0,
        sched_lock_offset: 0x4218,
        // No title-art patching for MoM (the Titans title package does not exist on this disc).
        title_package_len: 0,
    },
];

/// The release a fresh install (or one prepared before region support) uses.
pub const DEFAULT: &str = "RC8P7D";

pub fn by_game_id(id: &str) -> Option<&'static Release> { RELEASES.iter().find(|r| r.game_id == id) }

/// "PAL (RQJP7D) or USA (RQJE7D)" for messages.
pub fn supported_list() -> String {
    RELEASES.iter().map(|r| format!("{} ({})", r.label, r.game_id)).collect::<Vec<_>>().join(" or ")
}

