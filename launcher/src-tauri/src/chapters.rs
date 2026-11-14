//! MoM area selection through the game's own new-game and debug level-start actions.
//! The frontend embeds Lua 5.1 bytecode: edits preserve every length and instruction.
//! Normal Play restores the original resource bytes.
use crate::soundtrack::rcf_members;
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
};
const SCRIPT: &str = "fighttrees\\Frontend.bfig";
const ORIGINAL_SHA: &str = "b4286647d13b146bd1f1c53d0d8816a96f5312e2b428950e3ae6089642c2c951";
const MAGIC: &[u8; 8] = b"MOMAREA1";
const STATE: &[u8] = &[0x83, 0xa9, 0x36, 0x33];
const TRANSITION: &[u8] = &[0xbc, 0x27, 0x35, 0x9d];
const ACTIONS: &[u8] = &[0x0a, 0x87, 0x0f, 0x46];
pub const LEVELS: &[&str] = &["L0", "L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8", "L9"];
#[derive(Serialize)]
pub struct ChapterInfo {
    pub id: &'static str,
    pub number: usize,
}
pub fn list(rcf: &Path) -> Result<Vec<ChapterInfo>, String> {
    let members = rcf_members(rcf)?;
    Ok(LEVELS
        .iter()
        .enumerate()
        .filter(|(_, id)| {
            let name = format!("levels\\{id}\\LevelPackages.p3d");
            members.iter().any(|m| m.name.eq_ignore_ascii_case(&name))
        })
        .map(|(number, &id)| ChapterInfo { id, number })
        .collect())
}
fn find(d: &[u8], n: &[u8], from: usize) -> Option<usize> {
    d.get(from..)?
        .windows(n.len())
        .position(|w| w == n)
        .map(|i| i + from)
}
fn be32(d: &[u8], at: usize) -> Option<u32> {
    Some(u32::from_be_bytes(d.get(at..at + 4)?.try_into().ok()?))
}
struct Record {
    start: usize,
    fields: usize,
    name: Vec<u8>,
}
fn records(d: &[u8], tag: &[u8]) -> Vec<Record> {
    let mut out = Vec::new();
    let mut at = 0;
    while let Some(p) = find(d, tag, at) {
        at = p + 4;
        let Some(n) = be32(d, p + 4).map(|v| v as usize) else {
            continue;
        };
        if !(1..=64).contains(&n) {
            continue;
        }
        let Some(name) = d.get(p + 8..p + 8 + n) else {
            continue;
        };
        let fields = p + 8 + n.div_ceil(4) * 4;
        if name.iter().all(|c| c.is_ascii_alphanumeric() || *c == b'_') && fields + 32 <= d.len() {
            out.push(Record {
                start: p,
                fields,
                name: name.to_vec(),
            });
        }
    }
    out
}
fn action_end(d: &[u8], r: &Record) -> Option<usize> {
    let at = find(d, ACTIONS, r.fields)?;
    let size = be32(d, at + 4)? as usize;
    let end = at + if size == 0 { 20 } else { 12 + size };
    (end <= d.len()).then_some(end)
}
fn build(script: &[u8], level: &str, direct: bool) -> Result<Vec<u8>, String> {
    if !LEVELS.contains(&level) {
        return Err("Unknown MoM area.".into());
    }
    let bad = || "The MoM frontend has an unexpected layout.".to_string();
    if script.get(4..12) != Some(&b"\x1bLua\x51\x01\x04\x04"[..]) {
        return Err(bad());
    }
    // A Lua string constant: type=4, little-endian size=3, two characters + NUL.
    let pattern = b"StartLevelByName\0\x04\x03\0\0\0L0\0";
    let mut sites = Vec::new();
    let mut at = 0;
    while let Some(p) = find(script, pattern, at) {
        sites.push(p + pattern.len() - 3);
        at = p + pattern.len();
    }
    if sites.len() != 5 {
        return Err(bad());
    }
    let mut out = script.to_vec();
    for &p in sites.iter().skip(if direct { 0 } else { 1 }) {
        out[p..p + 2].copy_from_slice(level.as_bytes());
    }
    if !direct {
        return Ok(out);
    }
    let states = records(script, STATE);
    let transitions = records(script, TRANSITION);
    let boot = states
        .iter()
        .find(|s| s.name == b"LoadPersistentPostBootUp")
        .ok_or_else(bad)?;
    let title = states
        .iter()
        .find(|s| s.name == b"TitleScreen")
        .ok_or_else(bad)?;
    let next_title = states
        .iter()
        .find(|s| s.start > title.start)
        .ok_or_else(bad)?
        .start;
    let debug = transitions
        .iter()
        .find(|r| r.name == b"ExitToLevel" && r.start > title.start && r.start < next_title)
        .ok_or_else(bad)?;
    let debug_end = action_end(script, debug).ok_or_else(bad)?;
    let exits: Vec<_> = transitions
        .iter()
        .filter(|r| r.name == b"ExitTitleScreen" && r.start > boot.start && r.start < title.start)
        .collect();
    if exits.len() != 2 {
        return Err(bad());
    }
    // Both the persistent-load and opening-movie routes can enter the title.
    // Keep each source state; reuse the shipped level-loader action in both.
    for exit in exits {
        let end = action_end(script, exit).ok_or_else(bad)?;
        if end - exit.fields != debug_end - debug.fields {
            return Err(bad());
        }
        out[exit.fields + 4..end].copy_from_slice(&script[debug.fields + 4..debug_end]);
    }
    let license = states
        .iter()
        .find(|s| s.name == b"LicenseScreen")
        .ok_or_else(bad)?;
    let license_id = transitions
        .iter()
        .find(|r| r.start > license.start)
        .and_then(|r| be32(script, r.fields))
        .ok_or_else(bad)?;
    let demo = [&[0xe8, 0xcb, 0x72, 0x1b, 0, 0, 0, 4][..], b"Demo"].concat();
    let load = find(script, &demo, boot.start)
        .and_then(|p| be32(script, p + 12))
        .ok_or_else(bad)?;
    let mut redirects = 0;
    for r in &transitions {
        if be32(script, r.fields + 24) == Some(license_id) {
            out[r.fields + 24..r.fields + 28].copy_from_slice(&load.to_be_bytes());
            redirects += 1;
        }
    }
    if redirects == 0 {
        return Err(bad());
    }
    Ok(out)
}
fn backup_path(rcf: &Path) -> PathBuf {
    let mut p = rcf.as_os_str().to_owned();
    p.push(".episodes");
    PathBuf::from(p)
}
fn read_script(rcf: &Path) -> Result<(u64, Vec<u8>), String> {
    let m = rcf_members(rcf)?
        .into_iter()
        .find(|m| m.name.eq_ignore_ascii_case(SCRIPT))
        .ok_or("The MoM frontend was not found.")?;
    if m.len > 8 * 1024 * 1024 {
        return Err("Unexpected frontend size.".into());
    }
    let mut f = fs::File::open(rcf).map_err(|e| e.to_string())?;
    f.seek(SeekFrom::Start(m.offset))
        .map_err(|e| e.to_string())?;
    let mut b = vec![0; m.len as usize];
    f.read_exact(&mut b).map_err(|e| e.to_string())?;
    Ok((m.offset, b))
}
fn original(d: &[u8]) -> bool {
    format!("{:x}", Sha256::digest(d)) == ORIGINAL_SHA
}
fn write_script(rcf: &Path, offset: u64, data: &[u8]) -> Result<(), String> {
    let mut f = fs::OpenOptions::new()
        .write(true)
        .open(rcf)
        .map_err(|e| e.to_string())?;
    f.seek(SeekFrom::Start(offset)).map_err(|e| e.to_string())?;
    f.write_all(data).map_err(|e| e.to_string())?;
    f.sync_all().map_err(|e| e.to_string())
}
pub fn revert(rcf: &Path) -> Result<(), String> {
    let path = backup_path(rcf);
    if !path.exists() {
        return Ok(());
    }
    let data = fs::read(&path).map_err(|e| e.to_string())?;
    if data.get(..8) != Some(&MAGIC[..]) || data.len() < 16 {
        return Err("Invalid episode restore record.".into());
    }
    let offset = u64::from_le_bytes(data[8..16].try_into().unwrap());
    let bytes = &data[16..];
    let (actual, current) = read_script(rcf)?;
    if actual != offset || current.len() != bytes.len() || !original(bytes) {
        return Err("Episode restore record does not match this disc.".into());
    }
    write_script(rcf, offset, bytes)?;
    fs::remove_file(path).map_err(|e| e.to_string())
}
pub fn apply(rcf: &Path, level: &str, direct: bool) -> Result<(), String> {
    if !list(rcf)?.iter().any(|c| c.id == level) {
        return Err("This area is not present on the disc.".into());
    }
    revert(rcf)?;
    let (offset, script) = read_script(rcf)?;
    if !original(&script) {
        return Err("Episode selection requires the original PAL frontend script.".into());
    }
    let out = build(&script, level, direct)?;
    let mut record = MAGIC.to_vec();
    record.extend_from_slice(&offset.to_le_bytes());
    record.extend_from_slice(&script);
    let path = backup_path(rcf);
    let mut backup = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|e| e.to_string())?;
    backup
        .write_all(&record)
        .and_then(|_| backup.sync_all())
        .map_err(|e| e.to_string())?;
    write_script(rcf, offset, &out)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn malformed_and_unknown_inputs_are_rejected() {
        assert!(build(b"bad", "L1", true).is_err());
        assert!(build(b"bad", "L99", false).is_err());
    }
    #[test]
    fn real_disc_level_edits_keep_size_and_recover_exact_bytes() {
        let Some(game) = std::env::var_os("MOM_GAME_DIR") else {
            return;
        };
        let rcf = Path::new(&game).join("files/default.rcf");
        let (_, script) = read_script(&rcf).unwrap();
        assert!(original(&script));
        assert_eq!(list(&rcf).unwrap().len(), 10);
        for level in LEVELS {
            for direct in [false, true] {
                let out = build(&script, level, direct).unwrap();
                assert_eq!(out.len(), script.len());
                assert_eq!(&out[4..16], &script[4..16]);
            }
        }
        if let Some(path) = std::env::var_os("MOM_EPISODE_SCRIPT_OUT") {
            fs::write(path, build(&script, "L2", true).unwrap()).unwrap();
        }
    }
    #[test]
    fn scratch_archive_applies_switches_and_restores() {
        let Some(game) = std::env::var_os("MOM_EPISODE_SCRATCH") else {
            return;
        };
        let rcf = Path::new(&game).join("files/default.rcf");
        let (_, before) = read_script(&rcf).unwrap();
        apply(&rcf, "L2", true).unwrap();
        assert_ne!(read_script(&rcf).unwrap().1, before);
        apply(&rcf, "L3", false).unwrap();
        revert(&rcf).unwrap();
        assert_eq!(read_script(&rcf).unwrap().1, before);
        assert!(!backup_path(&rcf).exists());
    }
}
