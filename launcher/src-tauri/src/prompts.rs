//! MoM's action prompts use eight native C8/TGA textures in its shared frontend font.
//! Decode originals from the player's archive and substitute only on the host GPU.
//! The resource archive and guest RAM remain unchanged, including across save states.
use crate::{
    controls::Controls,
    prompt_glyphs::GLYPHS,
    soundtrack::rcf_members,
    title_art::{self, Rgba},
};
use std::{
    fs,
    io::{Read, Seek, SeekFrom},
    path::Path,
};
const PACKAGE: &str = "package\\b4c85fe7.p3d";
const BUTTONS: [&str; 8] = ["a", "two", "b", "z", "minus", "plus", "c", "one"];
/// Which buttons the prompts show.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Family {
    Xbox,
    PlayStation,
    Keyboard,
}

impl Family {
    fn dir(self) -> &'static str {
        match self {
            Family::Xbox => "xbox",
            Family::PlayStation => "playstation",
            Family::Keyboard => "kbm",
        }
    }

    /// The family a controller's name suggests (the same rule as the Controls page uses for its key caps).
    pub fn of_controller(name: &str) -> Family {
        let n = name.to_ascii_lowercase();
        let ps = [
            "dualsense",
            "dualshock",
            "playstation",
            "ps3",
            "ps4",
            "ps5",
            "wireless controller",
        ];
        if ps.iter().any(|k| n.contains(k)) {
            Family::PlayStation
        } else {
            Family::Xbox
        }
    }
}

/// The glyph file name for a keyboard key, as the glyph tool names it.
fn key_file(name: &str) -> String {
    let safe = match name {
        "Keypad /" => "Keypad_Divide",
        "Keypad *" => "Keypad_Multiply",
        "Keypad -" => "Keypad_Minus",
        "Keypad +" => "Keypad_Plus",
        "Keypad ." => "Keypad_Period",
        "Keypad =" => "Keypad_Equals",
        "\\" => "Backslash",
        "-" => "Minus",
        "=" => "Equals",
        "[" => "LeftBracket",
        "]" => "RightBracket",
        ";" => "Semicolon",
        "'" => "Quote",
        "," => "Comma",
        "." => "Period",
        "/" => "Slash",
        "`" => "Backquote",
        other => other,
    };
    format!("Key_{}", safe.replace(' ', "_"))
}

fn glyph(family: Family, name: &str) -> Option<&'static [u8]> {
    GLYPHS
        .iter()
        .find(|(f, n, _)| *f == family.dir() && n.eq_ignore_ascii_case(name))
        .map(|(_, _, b)| *b)
}

/// The glyph for one Wii button: the first of its bindings that belongs to the family's device and has a drawing.
fn glyph_for(family: Family, bindings: &[String]) -> Option<&'static [u8]> {
    bindings.iter().find_map(|t| {
        if family == Family::Keyboard {
            if let Some(k) = t.strip_prefix("Key:") {
                glyph(family, &key_file(k))
            } else if let Some(b) = t.strip_prefix("Mouse:") {
                glyph(family, &format!("Mouse_{b}"))
            } else {
                None
            }
        } else if t.contains(':') {
            None
        } else {
            glyph(family, t)
        }
    })
}

fn le32(b: &[u8], at: usize) -> Result<usize, String> {
    b.get(at..at + 4)
        .map(|v| u32::from_le_bytes(v.try_into().unwrap()) as usize)
        .ok_or_else(|| "Truncated prompt texture.".into())
}

/// Locate validated Pure3D chunks, never search arbitrary image payload for a name.
fn originals(package: &[u8]) -> Result<Vec<Rgba>, String> {
    fn visit(
        b: &[u8],
        start: usize,
        end: usize,
        depth: usize,
        found: &mut [Option<Rgba>],
    ) -> Result<(), String> {
        if depth > 16 {
            return Err("Prompt package nesting is invalid.".into());
        }
        let mut pos = start;
        while pos < end {
            if end - pos < 12 {
                return Err("Truncated prompt chunk.".into());
            }
            let tag = le32(b, pos)?;
            let header = le32(b, pos + 4)?;
            let total = le32(b, pos + 8)?;
            if header < 12 || total < header || total > end - pos {
                return Err("Invalid prompt chunk.".into());
            }
            if tag == 0x19001 && header > 13 {
                let n = b[pos + 12] as usize;
                let name = b
                    .get(pos + 13..pos + 13 + n)
                    .filter(|_| 13 + n <= header)
                    .ok_or("Invalid prompt name.")?;
                for (i, slot) in found.iter_mut().enumerate() {
                    let expected = format!("Titans_final_48_texture_{}", 10 + i);
                    if name.split(|&v| v == 0).next() == Some(expected.as_bytes()) {
                        if slot.is_some() {
                            return Err("Duplicate action prompt texture.".into());
                        }
                        let image = pos + header;
                        if total - header < 12
                            || le32(b, image)? != 0x19002
                            || le32(b, image + 8)? != total - header
                        {
                            return Err("Missing native prompt image.".into());
                        }
                        let data = image + 12;
                        // A length-prefixed, uncompressed color-mapped TGA, with 256 BGRA entries.
                        let size = le32(b, data)?;
                        if size != 18 + 1024 + 1024
                            || le32(b, image + 4)? != total - header
                            || size + 16 != total - header
                        {
                            return Err("Unexpected native prompt image size.".into());
                        }
                        let tga = &b[data + 4..pos + total];
                        if tga[0] != 0
                            || tga[1] != 1
                            || tga[2] != 1
                            || tga[3..7] != [0, 0, 0, 1]
                            || tga[7] != 32
                            || tga[12..16] != [32, 0, 32, 0]
                            || tga[16] != 8
                            || tga[17] != 32
                        {
                            return Err("Unsupported native prompt image format.".into());
                        }
                        let palette = &tga[18..1042];
                        let px = tga[1042..]
                            .iter()
                            .flat_map(|&index| {
                                let c = index as usize * 4;
                                [palette[c + 2], palette[c + 1], palette[c], palette[c + 3]]
                            })
                            .collect();
                        *slot = Some(Rgba { w: 32, h: 32, px });
                    }
                }
            }
            if tag != 0x19002 && header < total {
                visit(b, pos + header, pos + total, depth + 1, found)?;
            }
            pos += total;
        }
        Ok(())
    }
    if package.len() < 12 || le32(package, 0)? != 0xff443350 || le32(package, 8)? != package.len() {
        return Err("Unsupported frontend prompt package.".into());
    }
    let header = le32(package, 4)?;
    if header < 12 || header > package.len() {
        return Err("Invalid prompt package header.".into());
    }
    let mut found: Vec<Option<Rgba>> = (0..8).map(|_| None).collect();
    visit(package, header, package.len(), 0, &mut found)?;
    found
        .into_iter()
        .collect::<Option<Vec<_>>>()
        .ok_or_else(|| "The MoM action font is missing one or more button textures.".into())
}

fn pack(package: &[u8], layout: &Controls) -> Result<Vec<u8>, String> {
    let mut data = b"MMPRMT01".to_vec();
    for (original, button) in originals(package)?.iter().zip(BUTTONS) {
        data.extend_from_slice(&original.px);
        for family in [Family::Xbox, Family::PlayStation, Family::Keyboard] {
            let image = glyph_for(family, &layout.effective(button))
                .map(title_art::decode_png)
                .transpose()?;
            let pixels = image
                .as_ref()
                .map_or(original.px.as_slice(), |im| im.px.as_slice());
            if pixels.len() != 4096 {
                return Err("Invalid replacement prompt dimensions.".into());
            }
            data.extend_from_slice(pixels);
        }
    }
    Ok(data)
}

pub fn live_pack(rcf: &Path, layout: &Controls, destination: &Path) -> Result<(), String> {
    let member = rcf_members(rcf)?
        .into_iter()
        .find(|m| m.name.eq_ignore_ascii_case(PACKAGE))
        .ok_or("The MoM action font package was not found.")?;
    if member.len > 16 * 1024 * 1024 {
        return Err("Unexpected action font package size.".into());
    }
    let mut file = fs::File::open(rcf).map_err(|e| e.to_string())?;
    file.seek(SeekFrom::Start(member.offset))
        .map_err(|e| e.to_string())?;
    let mut package = vec![0; member.len as usize];
    file.read_exact(&mut package).map_err(|e| e.to_string())?;
    let data = pack(&package, layout)?;
    let pending = destination.with_extension("pending");
    fs::write(&pending, data).map_err(|e| format!("Could not write button prompts: {e}"))?;
    fs::rename(&pending, destination).map_err(|e| format!("Could not publish button prompts: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn real_mom_pack_preserves_archive_and_maps_all_eight_buttons() {
        let Some(game) = std::env::var_os("MOM_GAME_DIR") else {
            return;
        };
        let rcf = Path::new(&game).join("files/default.rcf");
        let member = rcf_members(&rcf)
            .unwrap()
            .into_iter()
            .find(|m| m.name.eq_ignore_ascii_case(PACKAGE))
            .unwrap();
        let read = || {
            let mut f = fs::File::open(&rcf).unwrap();
            f.seek(SeekFrom::Start(member.offset)).unwrap();
            let mut b = vec![0; member.len as usize];
            f.read_exact(&mut b).unwrap();
            b
        };
        let before = read();
        let c = layout(&[("a", "North,Key:J")]);
        let data = pack(&before, &c).unwrap();
        assert_eq!(&data[..8], b"MMPRMT01");
        assert_eq!(data.len(), 8 + 8 * 4096 * 4);
        let expected = title_art::decode_png(glyph(Family::Keyboard, "Key_J").unwrap()).unwrap();
        assert_eq!(&data[8 + 4096 * 3..8 + 4096 * 4], &expected.px);
        let out = std::env::var_os("MOM_PROMPT_PACK_OUT")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|| {
                std::env::temp_dir().join(format!("mom-prompts-{}.bin", std::process::id()))
            });
        live_pack(&rcf, &c, &out).unwrap();
        assert_eq!(fs::read(&out).unwrap(), data);
        // Remapping while the game runs atomically replaces an existing pack.
        live_pack(&rcf, &Controls::default(), &out).unwrap();
        assert_ne!(fs::read(&out).unwrap(), data);
        assert_eq!(read(), before);
        if std::env::var_os("MOM_PROMPT_PACK_OUT").is_none() {
            fs::remove_file(out).unwrap();
        }
        let mut damaged = before.clone();
        damaged[8..12].copy_from_slice(&12u32.to_le_bytes());
        assert!(pack(&damaged, &c).is_err());
    }
    fn layout(pairs: &[(&str, &str)]) -> Controls {
        let mut c = Controls::default();
        for (k, v) in pairs {
            c.bindings.insert(k.to_string(), v.to_string());
        }
        c
    }

    #[test]
    fn every_glyph_is_a_32px_rgba_image() {
        for (family, name, png) in GLYPHS {
            let img = title_art::decode_png(png).unwrap_or_else(|e| panic!("{family}/{name}: {e}"));
            assert_eq!((img.w, img.h), (32, 32), "{family}/{name}");
            assert_eq!(img.px.len(), 4096, "{family}/{name}");
        }
    }

    #[test]
    fn every_bindable_controller_and_extended_keyboard_input_has_art() {
        for family in [Family::Xbox, Family::PlayStation] {
            for (input, _) in crate::controls::PAD_INPUTS {
                assert!(
                    glyph_for(family, &[input.to_string()]).is_some(),
                    "{family:?}/{input}"
                );
            }
        }
        for key in [
            "\\",
            "Keypad /",
            "Keypad *",
            "Keypad -",
            "Keypad +",
            "Keypad .",
            "Keypad =",
            "Keypad Enter",
            "Left GUI",
            "Right GUI",
            "Application",
            "PrintScreen",
            "ScrollLock",
            "Pause",
            "Numlock",
        ] {
            assert!(
                glyph_for(Family::Keyboard, &[format!("Key:{key}")]).is_some(),
                "{key}"
            );
        }
        for n in 1..=24 {
            assert!(glyph_for(Family::Keyboard, &[format!("Key:F{n}")]).is_some());
        }
        for button in crate::controls::MOUSE_BUTTONS {
            assert!(glyph_for(Family::Keyboard, &[format!("Mouse:{button}")]).is_some());
        }
    }

    #[test]
    fn glyphs_follow_the_bindings_of_the_chosen_device() {
        let c = layout(&[("a", "North,Key:J"), ("b", "Mouse:X1"), ("z", "Paddle1")]);
        assert_eq!(
            glyph_for(Family::Xbox, &c.effective("a")),
            glyph(Family::Xbox, "North")
        );
        assert_eq!(
            glyph_for(Family::Keyboard, &c.effective("a")),
            glyph(Family::Keyboard, "Key_J")
        );
        assert_eq!(
            glyph_for(Family::Keyboard, &c.effective("b")),
            glyph(Family::Keyboard, "Mouse_X1")
        );
        assert!(
            glyph_for(Family::Xbox, &c.effective("b")).is_none(),
            "no controller binding keeps the Wii glyph"
        );
        assert_eq!(
            glyph_for(Family::PlayStation, &c.effective("z")),
            glyph(Family::PlayStation, "Paddle1")
        );
        // Unset buttons use the runtime's defaults: A is South / Space, Z is the right shoulder / Left Shift.
        let d = Controls::default();
        assert_eq!(
            glyph_for(Family::PlayStation, &d.effective("a")),
            glyph(Family::PlayStation, "South")
        );
        assert_eq!(
            glyph_for(Family::Keyboard, &d.effective("z")),
            glyph(Family::Keyboard, "Key_Left_Shift")
        );
        assert_eq!(
            Family::of_controller("DualSense Wireless Controller"),
            Family::PlayStation
        );
        assert_eq!(
            Family::of_controller("Xbox Series X Controller"),
            Family::Xbox
        );
    }
}
