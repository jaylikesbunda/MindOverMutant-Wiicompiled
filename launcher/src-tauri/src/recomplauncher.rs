//! Opt-in, windowless entry points. The ordinary launcher uses the same commands.
use super::*;
use std::ffi::OsString;
use std::sync::OnceLock;
use tauri::Listener;

static DATA_ROOT: OnceLock<PathBuf> = OnceLock::new();
pub fn data_root() -> Option<&'static PathBuf> { DATA_ROOT.get() }

pub struct Options { prepare: bool, game: PathBuf, data: Option<PathBuf> }

fn parse(args: Vec<OsString>) -> Result<Option<Options>, String> {
    let prepare = args.iter().any(|v| v == "--prepare");
    let play = args.iter().any(|v| v == "--play");
    if !prepare && !play { return Ok(None); }
    if prepare == play { return Err("Choose either --prepare or --play.".into()); }
    let mut game = None;
    let mut data = None;
    let mut args = args.into_iter();
    while let Some(arg) = args.next() {
        if arg == "--prepare" || arg == "--play" { continue; }
        let target = if arg == "--game" { &mut game }
            else if arg == "--data-dir" { &mut data }
            else { return Err(format!("Unknown argument: {}", arg.to_string_lossy())); };
        if target.is_some() { return Err(format!("Repeated argument: {}", arg.to_string_lossy())); }
        let value = args.next().ok_or_else(|| format!("Missing path after {}", arg.to_string_lossy()))?;
        if value.to_string_lossy().starts_with("--") { return Err(format!("Missing path after {}", arg.to_string_lossy())); }
        *target = Some(PathBuf::from(value));
    }
    let game = game.ok_or("Pass --game followed by your PAL Wii disc image.")?;
    Ok(Some(Options { prepare, game, data }))
}

pub fn options_or_exit() -> Option<Options> {
    match parse(std::env::args_os().skip(1).collect()) {
        Ok(options) => {
            if let Some(root) = options.as_ref().and_then(|o| o.data.as_ref()) {
                let absolute = std::path::absolute(root).unwrap_or_else(|_| root.clone());
                let _ = DATA_ROOT.set(absolute);
            }
            options
        }
        Err(error) => { eprintln!("{error}"); std::process::exit(2); }
    }
}

fn progress(value: f64, text: &str) {
    println!("progress {:.3} {}", value.clamp(0.0, 1.0), text.replace(['\r', '\n'], " "));
    let _ = std::io::stdout().flush();
}

fn log_line(log: &Mutex<fs::File>, line: &str) {
    if let Ok(mut file) = log.lock() {
        // Bound each run's log; retain the previous run separately.
        if file.metadata().map(|m| m.len() < 16 * 1024 * 1024).unwrap_or(false) {
            let _ = writeln!(file, "{line}");
        }
    }
}

pub fn start(app: AppHandle, options: Options) {
    tauri::async_runtime::spawn(async move {
        let logs = profile_dir(&app).join("logs");
        let outcome = fs::create_dir_all(&logs).and_then(|_| {
            let current = logs.join("recomplauncher.log");
            if current.exists() {
                let previous = logs.join("recomplauncher.previous.log");
                if previous.exists() { fs::remove_file(&previous)?; }
                fs::rename(&current, previous)?;
            }
            fs::File::create(current)
        });
        let file = match outcome {
            Ok(file) => Arc::new(Mutex::new(file)),
            Err(error) => { eprintln!("Could not open launcher log: {error}"); app.exit(6); return; }
        };
        for event in ["game-log", "runtime-build-log"] {
            let file = file.clone();
            app.listen(event, move |event| {
                if let Ok(serde_json::Value::Array(lines)) = serde_json::from_str(event.payload()) {
                    for item in lines {
                        if let Some(line) = item.get("line").and_then(|v| v.as_str()) {
                            log_line(&file, line);
                            if let Some(rest) = line.strip_prefix("@progress|") {
                                if let Some((fraction, text)) = rest.split_once('|') {
                                    if let Ok(value) = fraction.parse::<f64>() { progress(0.45 + value.clamp(0.0, 100.0) * 0.0054, text); }
                                }
                            }
                        }
                    }
                }
            });
        }
        app.listen("prepare-progress", |event| {
            if let Ok(value) = serde_json::from_str::<serde_json::Value>(event.payload()) {
                let done = value["done"].as_f64().unwrap_or(0.0);
                let total = value["total"].as_f64().unwrap_or(1.0).max(1.0);
                progress(0.05 + 0.39 * done / total, "Extracting your disc");
            }
        });
        log_line(&file, if options.prepare { "Preparing game" } else { "Starting game" });
        let code = match execute(&app, options).await {
            Ok(code) => code,
            Err((code, error)) => { eprintln!("{error}"); log_line(&file, &error); code }
        };
        log_line(&file, &format!("Exit code: {code}"));
        app.exit(code);
    });
}

async fn execute(app: &AppHandle, options: Options) -> Result<i32, (i32, String)> {
    let iso = options.game.canonicalize().map_err(|e| (2, format!("Could not open your disc image: {e}")))?;
    let info = inspect(&iso).map_err(|e| (2, e))?;
    if !info.supported { return Err((2, format!("Choose the supported PAL Wii disc (RC8P7D); this disc is {}.", info.game_id))); }
    let state = app.state::<AppState>();
    let same_source = state.settings.lock().unwrap().iso_path.as_ref()
        .and_then(|s| Path::new(s).canonicalize().ok()).as_ref() == Some(&iso);
    if options.prepare {
        progress(0.0, "Checking your disc");
        if !same_source || !game_ready(app) {
            prepare_game(app.clone(), app.state(), iso.to_str().ok_or((2, "The disc path is not valid UTF-8.".into()))?.to_owned()).await.map_err(|e| (3, e))?;
        }
        if !runtime_ready(app, &state.settings.lock().unwrap()) {
            // Portable releases contain the complete offline pack. Do not download silently.
            if pack_root(app).is_none() { return Err((4, "The offline builder pack is missing or damaged. Reinstall the Windows ZIP.".into())); }
            progress(0.45, "Building the native game; the first build can take several minutes");
            build_runtime(app.clone(), app.state()).await.map_err(|e| (4, e))?;
        }
        let mut settings = state.settings.lock().unwrap();
        settings.iso_path = Some(iso.to_string_lossy().into_owned());
        fs::write(settings_path(app), serde_json::to_vec_pretty(&*settings).map_err(|e| (3, e.to_string()))?).map_err(|e| (3, e.to_string()))?;
        progress(1.0, "Ready to play");
        return Ok(0);
    }
    if !same_source || !game_ready(app) { return Err((3, "Run setup with --prepare --game before playing this disc.".into())); }
    adopt_build(app, active_release(app));
    if !runtime_ready(app, &state.settings.lock().unwrap()) { return Err((4, "Run setup again to build the current native runtime.".into())); }
    if std::env::var("RECOMPLAUNCHER_MODE").as_deref() == Ok("bigpicture") {
        let mut settings = state.settings.lock().unwrap();
        settings.display_mode = "borderless".into();
        settings.input_mode = 2;
    }
    let (sender, receiver) = std::sync::mpsc::sync_channel(1);
    app.once("game-exit", move |event| {
        let code = serde_json::from_str::<i32>(event.payload()).unwrap_or(-1);
        let _ = sender.send(code);
    });
    launch_game(app.clone(), app.state(), None, None, None).await.map_err(|e| (5, e))?;
    // Stay alive until the actual game exits, so the parent backs up saves afterwards.
    tauri::async_runtime::spawn_blocking(move || receiver.recv().unwrap_or(-1)).await.map_err(|e| (5, e.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn args(values: &[&str]) -> Vec<OsString> { values.iter().map(OsString::from).collect() }
    #[test]
    fn ordinary_launch_keeps_the_gui() { assert!(parse(vec![]).unwrap().is_none()); }
    #[test]
    fn headless_requires_an_unambiguous_source() {
        for values in [&["--play"][..], &["--prepare", "--play", "--game", "disc.iso"], &["--prepare", "--game", "--play"], &["--play", "--game", "a", "--game", "b"]] {
            assert!(parse(args(values)).is_err());
        }
        let options = parse(args(&["--prepare", "--game", "My Disc.rvz", "--data-dir", "test profile"])).unwrap().unwrap();
        assert!(options.prepare);
        assert_eq!(options.game, PathBuf::from("My Disc.rvz"));
        assert_eq!(options.data, Some(PathBuf::from("test profile")));
    }
}
