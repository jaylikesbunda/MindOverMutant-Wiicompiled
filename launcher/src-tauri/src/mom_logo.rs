//! The Mind over Mutant launcher logo, read from the player's own disc (no game art ships with the launcher).
//!
//! The title screen draws two texture groups from the frontend package (`package\cdd70a8c.p3d` in default.rcf):
//! `Crash_Title_ENG.tga` (the CRASH lettering on gears, twelve tiles) and `Crash_Subtitle_ENG.tga` ("Mind over Mutant",
//! six tiles). Unlike Crash of the Titans these are native GX textures, not PNG: each image chunk (0x19002) holds a 4-byte
//! length, an 18-byte header (width and height at +16 as u16 LE), a 256-entry BGRA8 palette and linear 8-bit indices.
//! Tiles are laid out row by row; a row is as wide as the group's first row. The logo is the title with the subtitle
//! across the middle of the gears and the WIICOMP wordmark in the dip of its arc, as an RGBA PNG. The game files are not modified.

use crate::{soundtrack::rcf_members, title_art::{self, Rgba}};
use std::{fs, io::{Read, Seek, SeekFrom}, path::Path};

const FRONTEND: &str = "package\\cdd70a8c.p3d";
const GROUP: u32 = 0x0001_9005;
const TEXTURE: u32 = 0x0001_9001;
const IMAGE: u32 = 0x0001_9002;

fn le32(b: &[u8], at: usize) -> Result<u32, String> {
    b.get(at..at + 4).map(|x| u32::from_le_bytes(x.try_into().unwrap())).ok_or_else(|| "Truncated frontend package.".to_string())
}

fn le16(b: &[u8], at: usize) -> Result<u32, String> {
    b.get(at..at + 2).map(|x| u16::from_le_bytes(x.try_into().unwrap()) as u32).ok_or_else(|| "Truncated frontend package.".to_string())
}

/// Top-level texture group by name: (children start, end).
fn group(p3d: &[u8], name: &str) -> Result<(usize, usize), String> {
    let mut pos = le32(p3d, 4)? as usize;
    while pos + 12 <= p3d.len() {
        let (id, header, total) = (le32(p3d, pos)?, le32(p3d, pos + 4)? as usize, le32(p3d, pos + 8)? as usize);
        if total < 12 || header > total || pos + total > p3d.len() { return Err("Invalid chunk in the frontend package.".into()); }
        let len = *p3d.get(pos + 12).unwrap_or(&0) as usize;
        if id == GROUP && len > 0 && p3d.get(pos + 13..pos + 13 + len).is_some_and(|n| n.starts_with(&[name.as_bytes(), b"\0"].concat())) {
            return Ok((pos + header, pos + total));
        }
        pos += total;
    }
    Err(format!("The frontend package has no {name} texture group."))
}

/// Decodes one native C8 texture (BGRA8 palette, linear indices).
fn decode_tile(p3d: &[u8], texture: usize) -> Result<Rgba, String> {
    let image = texture + le32(p3d, texture + 4)? as usize;
    if le32(p3d, image)? != IMAGE { return Err("A title texture has no image chunk.".into()); }
    let data = image + 12;
    let (w, h) = (le16(p3d, data + 16)?, le16(p3d, data + 18)?);
    let (pal, idx) = (data + 22, data + 22 + 1024);
    let end = image + le32(p3d, image + 8)? as usize;
    if w == 0 || h == 0 || w > 1024 || h > 1024 || idx + (w * h) as usize > end || end > p3d.len() {
        return Err("Unexpected title texture layout.".into());
    }
    let px = p3d[idx..idx + (w * h) as usize].iter().flat_map(|&i| { let c = pal + i as usize * 4; [p3d[c + 2], p3d[c + 1], p3d[c], p3d[c + 3]] }).collect();
    Ok(Rgba { w, h, px })
}

/// All tiles of a texture group assembled row by row, cropped to the visible pixels.
fn assemble(p3d: &[u8], name: &str) -> Result<Rgba, String> {
    let (mut pos, end) = group(p3d, name)?;
    let mut tiles = Vec::new();
    while pos < end {
        let (id, total) = (le32(p3d, pos)?, le32(p3d, pos + 8)? as usize);
        if total < 12 || pos + total > end { return Err("Invalid child in a title texture group.".into()); }
        if id == TEXTURE { tiles.push(decode_tile(p3d, pos)?); }
        pos += total;
    }
    let first_h = tiles.first().ok_or("A title texture group is empty.")?.h;
    let width: u32 = tiles.iter().take_while(|t| t.h == first_h).map(|t| t.w).sum();
    let mut placed = Vec::new();
    let (mut x, mut y, mut row_h) = (0u32, 0u32, 0u32);
    for t in &tiles {
        if x + t.w > width { x = 0; y += row_h; }
        placed.push((t, x as i32, y as i32));
        x += t.w; row_h = t.h;
    }
    title_art::flatten(width, y + row_h, &placed)
}

/// The frontend package from a game's default.rcf.
fn frontend(rcf: &Path) -> Result<Vec<u8>, String> {
    let member = rcf_members(rcf)?.into_iter().find(|m| m.name.eq_ignore_ascii_case(FRONTEND)).ok_or("The disc is missing its frontend asset package.")?;
    let mut f = fs::File::open(rcf).map_err(|e| e.to_string())?;
    f.seek(SeekFrom::Start(member.offset)).map_err(|e| e.to_string())?;
    let mut p3d = vec![0u8; member.len as usize];
    f.read_exact(&mut p3d).map_err(|e| e.to_string())?;
    Ok(p3d)
}

/// Composes the launcher logo (PNG bytes) from a prepared game's default.rcf.
pub fn compose(rcf: &Path) -> Result<Vec<u8>, String> {
    let p3d = frontend(rcf)?;
    let title = assemble(&p3d, "Crash_Title_ENG.tga")?;
    let subtitle = assemble(&p3d, "Crash_Subtitle_ENG.tga")?;
    let wordmark = title_art::wordmark()?;
    let w = title.w.max(subtitle.w).max(wordmark.w);
    // Subtitle across the middle of the gears, wordmark in the dip of its arc: both inside the title's outline.
    let sub_y = title.h * 50 / 100;
    let mark_y = sub_y + subtitle.h * 58 / 100;
    let center = |img: &Rgba| ((w - img.w) / 2) as i32;
    let logo = title_art::flatten(w, title.h.max(mark_y + wordmark.h), &[
        (&title, center(&title), 0),
        (&subtitle, center(&subtitle), sub_y as i32),
        (&wordmark, center(&wordmark), mark_y as i32),
    ])?;
    title_art::encode_png(&logo)
}

#[cfg(test)]
mod tests {
    #[test]
    fn composes_from_the_real_disc() {
        let Ok(dir) = std::env::var("MOM_GAME_DIR") else { return };
        let png = super::compose(&std::path::Path::new(&dir).join("files/default.rcf")).unwrap();
        let img = crate::title_art::decode_png(&png).unwrap();
        assert!(img.w > 500 && img.h > 280, "{}x{}", img.w, img.h);
        if let Ok(out) = std::env::var("MOM_LOGO_OUT") { std::fs::write(out, &png).unwrap(); }
    }
}
