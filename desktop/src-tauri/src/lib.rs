//! The window's link to `vision serve`. The server takes its key only in an `Authorization` header and
//! answers no cross-origin requests, so the web view can't reach it itself: the socket and every HTTP
//! call live here, and frames go up to the page as `frame` events.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tauri::menu::{Menu, MenuItem, MenuItemBuilder, PredefinedMenuItem, SubmenuBuilder};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, Runtime, State, WindowEvent};
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::Message;

/// A Vision to talk to: the address the pairing code names and its key.
#[derive(Clone, Serialize, Deserialize)]
struct Pairing {
    url: String,
    token: String,
    #[serde(default)]
    name: String,
}

#[derive(Default)]
struct Link {
    pairing: Mutex<Option<Pairing>>,
    outbox: Mutex<Option<mpsc::UnboundedSender<String>>>,
    /// Bumped on every (re)connect or unpair: a socket task from an older one stops.
    generation: AtomicU64,
    http: reqwest::Client,
}

fn normalize(address: &str) -> Option<String> {
    let mut s = address.trim().to_string();
    if s.is_empty() {
        return None;
    }
    if !s.contains("://") {
        s = format!("https://{s}");
    }
    while s.ends_with('/') {
        s.pop();
    }
    Some(s)
}

fn socket_url(base: &str) -> String {
    if let Some(rest) = base.strip_prefix("http://") {
        format!("ws://{rest}/ws")
    } else if let Some(rest) = base.strip_prefix("https://") {
        format!("wss://{rest}/ws")
    } else {
        format!("{base}/ws")
    }
}

fn pairing_file(app: &AppHandle) -> Option<PathBuf> {
    app.path().app_config_dir().ok().map(|d| d.join("pairing.json"))
}

fn load_pairing(app: &AppHandle) -> Option<Pairing> {
    let text = std::fs::read_to_string(pairing_file(app)?).ok()?;
    serde_json::from_str(&text).ok()
}

fn save_pairing(app: &AppHandle, pairing: Option<&Pairing>) -> Result<(), String> {
    let path = pairing_file(app).ok_or("no config folder")?;
    let Some(pairing) = pairing else {
        let _ = std::fs::remove_file(&path);
        return Ok(());
    };
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    std::fs::write(&path, serde_json::to_vec_pretty(pairing).unwrap()).map_err(|e| e.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
    }
    Ok(())
}

fn status(app: &AppHandle, state: &str, detail: &str) {
    let _ = app.emit("connection", json!({"state": state, "detail": detail}));
}

/// Keep a socket open to the paired Vision until the generation moves on: frames in go up as events,
/// frames from the page go out, a ping every 20 s, and a dropped connection is retried with backoff.
async fn run_socket(app: AppHandle, pairing: Pairing, generation: u64, mut outbox: mpsc::UnboundedReceiver<String>) {
    let link = app.state::<Link>();
    let current = || link.generation.load(Ordering::SeqCst) == generation;
    let mut wait = 1u64;
    while current() {
        status(&app, "connecting", "");
        let mut request = match socket_url(&pairing.url).into_client_request() {
            Ok(r) => r,
            Err(e) => {
                status(&app, "error", &format!("bad address: {e}"));
                return;
            }
        };
        request.headers_mut().insert("Authorization", format!("Bearer {}", pairing.token).parse().unwrap());
        let socket = tokio::time::timeout(Duration::from_secs(15), tokio_tungstenite::connect_async(request)).await;
        let (mut sink, mut stream) = match socket {
            Ok(Ok((ws, _))) => ws.split(),
            Ok(Err(e)) => {
                status(&app, "offline", &e.to_string());
                tokio::time::sleep(Duration::from_secs(wait)).await;
                wait = (wait * 2).min(15);
                continue;
            }
            Err(_) => {
                status(&app, "offline", "timed out");
                tokio::time::sleep(Duration::from_secs(wait)).await;
                wait = (wait * 2).min(15);
                continue;
            }
        };
        let mut ping = tokio::time::interval(Duration::from_secs(20));
        let mut check = tokio::time::interval(Duration::from_millis(500));
        let mut refused = false;
        loop {
            tokio::select! {
                incoming = stream.next() => match incoming {
                    Some(Ok(Message::Text(text))) => {
                        if let Ok(frame) = serde_json::from_str::<Value>(&text) {
                            if frame.get("type").and_then(Value::as_str) == Some("hello") {
                                wait = 1;
                                status(&app, "online", "");
                            }
                            let _ = app.emit("frame", frame);
                        }
                    }
                    Some(Ok(Message::Close(close))) => {
                        refused = close.is_some_and(|c| c.code == CloseCode::Library(4401));
                        break;
                    }
                    Some(Ok(_)) => {}
                    Some(Err(_)) | None => break,
                },
                out = outbox.recv() => match out {
                    Some(text) => if sink.send(Message::text(text)).await.is_err() { break },
                    None => return,
                },
                _ = ping.tick() => if sink.send(Message::text(r#"{"type":"ping"}"#)).await.is_err() { break },
                _ = check.tick() => if !current() { let _ = sink.close().await; return },
            }
        }
        if refused {
            status(&app, "refused", "That Vision didn't accept the key.");
            return;
        }
        status(&app, "offline", "");
        tokio::time::sleep(Duration::from_secs(wait)).await;
        wait = (wait * 2).min(15);
    }
}

fn start(app: &AppHandle, pairing: Pairing) {
    let link = app.state::<Link>();
    let generation = link.generation.fetch_add(1, Ordering::SeqCst) + 1;
    let (tx, rx) = mpsc::unbounded_channel();
    *link.outbox.lock().unwrap() = Some(tx);
    *link.pairing.lock().unwrap() = Some(pairing.clone());
    tauri::async_runtime::spawn(run_socket(app.clone(), pairing, generation, rx));
}

/// The saved pairing, without its key: what the window shows before it connects.
#[tauri::command]
fn saved(app: AppHandle) -> Option<Value> {
    load_pairing(&app).map(|p| json!({"url": p.url, "name": p.name}))
}

/// Connect with the saved pairing (on start-up).
#[tauri::command]
fn resume(app: AppHandle) -> bool {
    match load_pairing(&app) {
        Some(p) => {
            start(&app, p);
            true
        }
        None => false,
    }
}

/// A `vision serve` running on this computer, found from the files it leaves: its address and key.
#[tauri::command]
fn local_vision() -> Option<Value> {
    let home = dirs::home_dir()?;
    let env_dir = |var: &str, fallback: &str| {
        std::env::var_os(var).map(PathBuf::from).filter(|p| p.is_absolute()).unwrap_or_else(|| home.join(fallback))
    };
    let serve: Value = serde_json::from_str(
        &std::fs::read_to_string(env_dir("XDG_STATE_HOME", ".local/state").join("vision/serve.json")).ok()?,
    )
    .ok()?;
    let url = serve.get("url")?.as_str()?.to_string();
    let token = std::fs::read_to_string(env_dir("XDG_CONFIG_HOME", ".config").join("vision/remote_token")).ok()?;
    let token = token.trim();
    (!token.is_empty()).then(|| json!({"url": url, "token": token}))
}

/// Pair with a Vision (address and key, or a pasted pairing code) and connect to it.
#[tauri::command]
fn pair(app: AppHandle, url: String, token: String, name: Option<String>) -> Result<(), String> {
    let url = normalize(&url).ok_or("Give it an address.")?;
    let token = token.trim().to_string();
    if token.is_empty() {
        return Err("Give it the key.".into());
    }
    let pairing = Pairing { url, token, name: name.unwrap_or_default() };
    save_pairing(&app, Some(&pairing))?;
    start(&app, pairing);
    Ok(())
}

/// Unpair: close the socket and forget the address and key.
#[tauri::command]
fn forget(app: AppHandle) -> Result<(), String> {
    let link = app.state::<Link>();
    link.generation.fetch_add(1, Ordering::SeqCst);
    *link.outbox.lock().unwrap() = None;
    *link.pairing.lock().unwrap() = None;
    save_pairing(&app, None)
}

/// Send a frame over the socket (`message`, `answer`, `cancel`, `new`, …).
#[tauri::command]
fn send(link: State<'_, Link>, frame: Value) -> Result<(), String> {
    let outbox = link.outbox.lock().unwrap();
    let tx = outbox.as_ref().ok_or("Not connected.")?;
    tx.send(frame.to_string()).map_err(|_| "Not connected.".to_string())
}

/// One HTTP call to the paired Vision; the JSON it answers, or its `detail` as the error.
#[tauri::command]
async fn http(
    link: State<'_, Link>,
    method: String,
    path: String,
    query: Option<HashMap<String, String>>,
    body: Option<Value>,
) -> Result<Value, String> {
    let pairing = link.pairing.lock().unwrap().clone().ok_or("Not paired.")?;
    let method = reqwest::Method::from_bytes(method.to_uppercase().as_bytes()).map_err(|e| e.to_string())?;
    let mut req = link
        .http
        .request(method, format!("{}/{}", pairing.url, path.trim_start_matches('/')))
        .bearer_auth(&pairing.token)
        .timeout(Duration::from_secs(60));
    if let Some(query) = query {
        req = req.query(&query);
    }
    if let Some(body) = body {
        req = req.json(&body);
    }
    let resp = req.send().await.map_err(|e| e.to_string())?;
    let code = resp.status();
    let data: Value = resp.json().await.unwrap_or(Value::Null);
    if !code.is_success() {
        let detail = data.get("detail").and_then(Value::as_str).unwrap_or("");
        return Err(if detail.is_empty() { format!("HTTP {}", code.as_u16()) } else { detail.to_string() });
    }
    Ok(data)
}

/// Upload a file to the paired Vision (POST /upload): the bytes are the call's raw body and its name the
/// `x-name` header (URI-encoded). Answers the path on that computer, for the message's `images`.
#[tauri::command]
async fn upload(link: State<'_, Link>, request: tauri::ipc::Request<'_>) -> Result<String, String> {
    let tauri::ipc::InvokeBody::Raw(bytes) = request.body() else { return Err("No file.".into()) };
    let name = request.headers().get("x-name").and_then(|v| v.to_str().ok()).unwrap_or("file");
    send_upload(&link, percent_decode(name), bytes.clone()).await
}

/// A file dropped on the window (its path, from the native drop: WebKitGTK hands the page no files from a
/// file manager) sent up like the paperclip's.
#[tauri::command]
async fn upload_path(link: State<'_, Link>, path: String) -> Result<Value, String> {
    let file = std::path::PathBuf::from(&path);
    if file.is_dir() {
        return Err(format!("{} is a folder.", file.display()));
    }
    let bytes = tauri::async_runtime::spawn_blocking({ let f = file.clone(); move || std::fs::read(f) })
        .await
        .map_err(|e| e.to_string())?
        .map_err(|e| format!("{}: {e}", file.display()))?;
    let name = file.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_else(|| "file".into());
    Ok(json!({"name": name, "path": send_upload(&link, name.clone(), bytes).await?}))
}

/// What a paste brings that the page can't read itself: a picture on the clipboard (a screenshot; WebKitGTK
/// gives the page none) or files copied in a file manager. Each is uploaded; `[]` when the clipboard has neither.
#[tauri::command]
async fn paste_files(link: State<'_, Link>) -> Result<Value, String> {
    let found = tauri::async_runtime::spawn_blocking(clipboard_files).await.map_err(|e| e.to_string())??;
    let mut out = Vec::new();
    for (name, bytes) in found {
        out.push(json!({"name": name, "path": send_upload(&link, name.clone(), bytes).await?}));
    }
    Ok(Value::Array(out))
}

/// The clipboard's files (copied in a file manager: `file://` URIs) or else its picture, as PNG.
fn clipboard_files() -> Result<Vec<(String, Vec<u8>)>, String> {
    let mut board = arboard::Clipboard::new().map_err(|e| format!("The clipboard can't be read: {e}"))?;
    if let Ok(paths) = board.get().file_list() {
        let files: Vec<(String, Vec<u8>)> = paths
            .into_iter()
            .filter(|p| p.is_file())
            .filter_map(|p| Some((p.file_name()?.to_string_lossy().into_owned(), std::fs::read(&p).ok()?)))
            .collect();
        if !files.is_empty() {
            return Ok(files);
        }
    }
    match board.get_image() {
        Ok(img) => Ok(vec![("pasted image.png".into(), png_of(img.width as u32, img.height as u32, &img.bytes)?)]),
        Err(_) => Ok(Vec::new()),
    }
}

/// RGBA pixels as a PNG.
fn png_of(width: u32, height: u32, rgba: &[u8]) -> Result<Vec<u8>, String> {
    let mut out = Vec::new();
    {
        let mut enc = png::Encoder::new(&mut out, width, height);
        enc.set_color(png::ColorType::Rgba);
        enc.set_depth(png::BitDepth::Eight);
        let mut w = enc.write_header().map_err(|e| e.to_string())?;
        w.write_image_data(rgba).map_err(|e| e.to_string())?;
    }
    Ok(out)
}

async fn send_upload(link: &Link, name: String, bytes: Vec<u8>) -> Result<String, String> {
    let pairing = link.pairing.lock().unwrap().clone().ok_or("Not paired.")?;
    let part = reqwest::multipart::Part::bytes(bytes).file_name(name);
    let resp = link
        .http
        .post(format!("{}/upload", pairing.url))
        .bearer_auth(&pairing.token)
        .multipart(reqwest::multipart::Form::new().part("file", part))
        .timeout(Duration::from_secs(300))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    let code = resp.status();
    let data: Value = resp.json().await.unwrap_or(Value::Null);
    if !code.is_success() {
        return Err(data.get("detail").and_then(Value::as_str).map(str::to_string).unwrap_or(format!("HTTP {}", code.as_u16())));
    }
    data.get("path").and_then(Value::as_str).map(str::to_string).ok_or("The upload came back without a path.".into())
}

fn percent_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let Ok(b) = u8::from_str_radix(&s[i + 1..i + 3], 16) {
                out.push(b);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// A file from the paired Vision as raw bytes (a reply's picture from GET /image, an upload's thumbnail).
#[tauri::command]
async fn fetch_bytes(link: State<'_, Link>, path: String, query: Option<HashMap<String, String>>) -> Result<tauri::ipc::Response, String> {
    let pairing = link.pairing.lock().unwrap().clone().ok_or("Not paired.")?;
    let mut req = link
        .http
        .get(format!("{}/{}", pairing.url, path.trim_start_matches('/')))
        .bearer_auth(&pairing.token)
        .timeout(Duration::from_secs(60));
    if let Some(query) = query {
        req = req.query(&query);
    }
    let resp = req.send().await.map_err(|e| e.to_string())?;
    if !resp.status().is_success() {
        return Err(format!("HTTP {}", resp.status().as_u16()));
    }
    Ok(tauri::ipc::Response::new(resp.bytes().await.map_err(|e| e.to_string())?.to_vec()))
}

/// One piece of a reply in Vision's voice (POST /speak), as WAV bytes for the page to play.
#[tauri::command]
async fn speak(link: State<'_, Link>, text: String) -> Result<tauri::ipc::Response, String> {
    let pairing = link.pairing.lock().unwrap().clone().ok_or("Not paired.")?;
    let resp = link
        .http
        .post(format!("{}/speak", pairing.url))
        .bearer_auth(&pairing.token)
        .json(&json!({"text": text, "spoken": true}))
        .timeout(Duration::from_secs(60))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    if resp.status().as_u16() == 503 {
        return Err("The voice is off on that computer.".into());
    }
    if !resp.status().is_success() {
        return Err(format!("HTTP {}", resp.status().as_u16()));
    }
    Ok(tauri::ipc::Response::new(resp.bytes().await.map_err(|e| e.to_string())?.to_vec()))
}

/// How the system wants windows to look: `os` (the page styles itself after it), `scheme` ("dark",
/// "light" or null for no preference) and `accent` ("#rrggbb" or null). On macOS the web view follows
/// the system by itself; on Linux both come from the desktop portal, and changes arrive as `appearance`.
#[tauri::command]
fn appearance() -> Value {
    #[cfg(target_os = "linux")]
    let (scheme, accent) = portal::read();
    #[cfg(not(target_os = "linux"))]
    let (scheme, accent): (Option<String>, Option<String>) = (None, None);
    json!({"os": std::env::consts::OS, "scheme": scheme, "accent": accent})
}

#[cfg(target_os = "linux")]
mod portal {
    use super::*;
    use zbus::blocking::{Connection, Proxy};
    use zbus::zvariant::{OwnedValue, Value};

    fn proxy(conn: &Connection) -> zbus::Result<Proxy<'_>> {
        Proxy::new(conn, "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop", "org.freedesktop.portal.Settings")
    }

    /// The setting itself: some portals wrap ReadOne's answer in one variant more.
    fn setting(conn: &Connection, key: &str) -> Option<Value<'static>> {
        let reply: OwnedValue = proxy(conn).ok()?.call("ReadOne", &("org.freedesktop.appearance", key)).ok()?;
        let mut value: Value<'static> = reply.try_into().ok()?;
        while let Value::Value(inner) = value {
            value = *inner;
        }
        Some(value)
    }

    pub fn read() -> (Option<String>, Option<String>) {
        let Ok(conn) = Connection::session() else { return (None, None) };
        let scheme = match setting(&conn, "color-scheme") {
            Some(Value::U32(1)) => Some("dark".to_string()),
            Some(Value::U32(2)) => Some("light".to_string()),
            _ => None,
        };
        let accent = match setting(&conn, "accent-color") {
            Some(Value::Structure(rgb)) => {
                let parts: Vec<f64> = rgb.fields().iter().filter_map(|v| if let Value::F64(x) = v { Some(*x) } else { None }).collect();
                // Out of range means the user set none.
                (parts.len() == 3 && parts.iter().all(|x| (0.0..=1.0).contains(x))).then(|| {
                    let c = |x: f64| (x * 255.0).round() as u8;
                    format!("#{:02x}{:02x}{:02x}", c(parts[0]), c(parts[1]), c(parts[2]))
                })
            }
            _ => None,
        };
        (scheme, accent)
    }

    /// Tell the page whenever the appearance settings change (a theme switch while the app is open).
    pub fn watch(app: AppHandle) {
        std::thread::spawn(move || {
            let Ok(conn) = Connection::session() else { return };
            let Ok(proxy) = proxy(&conn) else { return };
            let Ok(changes) = proxy.receive_signal("SettingChanged") else { return };
            for msg in changes {
                let Ok((namespace, _key, _value)) = msg.body().deserialize::<(String, String, OwnedValue)>() else { continue };
                if namespace == "org.freedesktop.appearance" {
                    let _ = app.emit("appearance", appearance());
                }
            }
        });
    }
}

/// The menu bar. Items with an id reach the page as a `menu` event carrying it.
fn menu<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<Menu<R>> {
    let item = |id: &str, label: &str, keys: &str| MenuItemBuilder::with_id(id, label).accelerator(keys).build(app);
    let new_chat = item("new_chat", "New Chat", "CmdOrCtrl+N")?;
    let new_scout = item("new_scout", "New Scout…", "CmdOrCtrl+Shift+N")?;
    let settings = item("settings", "Settings…", "CmdOrCtrl+,")?;
    let connect = MenuItemBuilder::with_id("connect", "Connect to Another Computer…").build(app)?;
    let search = item("search", "Search Chats", "CmdOrCtrl+K")?;
    let sidebar = item("toggle_sidebar", "Toggle Sidebar", "CmdOrCtrl+B")?;
    let board = item("agents", "All Activity", "CmdOrCtrl+Shift+A")?;
    let model = item("model", "Change Model…", "CmdOrCtrl+Shift+M")?;
    let stop = item("stop", "Stop Reply", "CmdOrCtrl+.")?;
    let terminal = item("open_terminal", "Open in Terminal", "CmdOrCtrl+Shift+T")?;

    let mut file = SubmenuBuilder::new(app, "File").item(&new_chat).item(&new_scout).separator().item(&connect);
    let edit = SubmenuBuilder::new(app, "Edit").undo().redo().separator().cut().copy().paste().select_all().build()?;
    let view = SubmenuBuilder::new(app, "View").item(&board).item(&sidebar).item(&search).separator().fullscreen().build()?;
    let chat = SubmenuBuilder::new(app, "Chat").item(&model).item(&stop).separator().item(&terminal).build()?;
    let window = SubmenuBuilder::new(app, "Window").minimize().maximize().separator().close_window().build()?;

    if cfg!(target_os = "macos") {
        let vision = SubmenuBuilder::new(app, "Vision")
            .about(None)
            .separator()
            .item(&settings)
            .separator()
            .services()
            .separator()
            .hide()
            .hide_others()
            .show_all()
            .separator()
            .quit()
            .build()?;
        Menu::with_items(app, &[&vision, &file.build()?, &edit, &view, &chat, &window])
    } else {
        file = file.separator().item(&settings).separator().quit();
        Menu::with_items(app, &[&file.build()?, &edit, &view, &chat])
    }
}

// ---------------------------------------------------------------- setting Vision up on this computer
//
// The first run on a computer without Vision installs it, keeps `vision serve` running and signs in to a
// subscription. The page asks for an action by name; the commands come from here or from Vision itself
// (`vision provider status --json`), never from the page.

const INSTALL: &str = "curl -fsSL https://raw.githubusercontent.com/vision-sAi/vision/master/install.sh | sh";

fn vision_bin() -> Option<PathBuf> {
    let bin = dirs::home_dir()?.join(".local/bin/vision");
    bin.exists().then_some(bin)
}

/// A command through the user's login shell, so it finds what their terminal would (npm, a CLI just installed).
fn shell(command: &str) -> std::process::Command {
    let sh = std::env::var("SHELL").ok().filter(|s| !s.is_empty()).unwrap_or_else(|| "/bin/sh".into());
    let mut cmd = std::process::Command::new(sh);
    cmd.arg("-l").arg("-c").arg(command);
    // From home, not the app's own folder: inside an AppImage that is its mount, gone once it updates.
    if let Some(home) = dirs::home_dir() {
        cmd.current_dir(home);
    }
    for (key, value) in child_env(std::env::vars(), dirs::home_dir().as_deref()) {
        match value {
            Some(v) => cmd.env(key, v),
            None => cmd.env_remove(key),
        };
    }
    cmd
}

/// What a command run from the app gets changed in its environment: `(name, Some(value))` to set,
/// `(name, None)` to remove.
///
/// An AppImage's launcher points PYTHONHOME, PYTHONPATH, LD_LIBRARY_PATH, GTK's and GIO's variables and
/// PATH into the AppImage's own mount (`APPDIR`), for the app. Passed on, they break what the app runs:
/// Python looks for itself in the mount and dies before its first line (`vision upgrade` stopped with
/// "<no Python frame>"). An AppImage that updated itself starts the new one from the old, so the variables
/// can point into an earlier mount too, still mounted while that copy runs: git then loaded its bundled
/// OpenSSL and `vision upgrade` couldn't reach GitHub. So every AppImage mount counts (`<tmp>/.mount_*`, the
/// folder APPDIR is in). A variable pointing only into mounts goes, a list keeps its other entries,
/// and the launcher's PYTHONDONTWRITEBYTECODE goes with them. PATH also loses Vision's shims (they stop
/// `claude` and the like inside a Vision turn, and an app started from one would inherit them) and starts
/// with ~/.local/bin, where Vision installs itself.
fn child_env(vars: impl Iterator<Item = (String, String)>, home: Option<&std::path::Path>) -> Vec<(String, Option<String>)> {
    let vars: Vec<(String, String)> = vars.collect();
    let get = |k: &str| vars.iter().find(|(n, _)| n == k).map(|(_, v)| v.clone());
    let appdir = get("APPDIR").filter(|d| !d.is_empty()).map(|d| d.trim_end_matches('/').to_string());
    // Any AppImage's mount, this one's or an earlier copy's: `/tmp/.mount_VisionAB12` → `/tmp/.mount_`.
    let mounts = appdir.as_deref().and_then(|d| d.rfind("/.mount_").map(|i| d[..i + "/.mount_".len()].to_string()));
    let inside = |entry: &str| {
        appdir.as_deref().is_some_and(|d| entry == d || entry.starts_with(&format!("{d}/")))
            || mounts.as_deref().is_some_and(|m| entry.starts_with(m))
    };
    let mut out = Vec::new();
    if appdir.is_some() {
        for (name, value) in &vars {
            if name == "PATH" || !value.split(':').any(|e| inside(e)) {
                continue;
            }
            let kept: Vec<&str> = value.split(':').filter(|e| !e.is_empty() && !inside(e)).collect();
            let listed = value.contains(':') && !kept.is_empty() && name != "PYTHONHOME";
            out.push((name.clone(), listed.then(|| kept.join(":"))));
        }
        out.push(("APPDIR".into(), None));
        out.push(("PYTHONDONTWRITEBYTECODE".into(), None));
    }
    let path = get("PATH").unwrap_or_default();
    let mut entries: Vec<String> = home.map(|h| h.join(".local/bin").display().to_string()).into_iter().collect();
    for e in path.split(':') {
        if e.is_empty() || inside(e) || e.trim_end_matches('/').ends_with("vision/shims") || entries.iter().any(|x| x == e) {
            continue;
        }
        entries.push(e.to_string());
    }
    out.push(("PATH".into(), Some(entries.join(":"))));
    out
}

fn quote(arg: &str) -> String {
    format!("'{}'", arg.replace('\'', "'\\''"))
}

/// Whether Vision is installed here and whether its server is up.
#[tauri::command]
fn vision_here() -> Value {
    json!({"installed": vision_bin().is_some(), "serving": local_vision().is_some(), "os": std::env::consts::OS})
}

/// `vision provider status --json`: the subscription CLIs, installed and signed in or not.
#[tauri::command]
async fn provider_status() -> Result<Value, String> {
    let bin = vision_bin().ok_or("Vision isn't installed here.")?;
    let out = tauri::async_runtime::spawn_blocking(move || shell(&format!("{} provider status --json", quote(&bin.display().to_string()))).output())
        .await
        .map_err(|e| e.to_string())?
        .map_err(|e| e.to_string())?;
    let text = String::from_utf8_lossy(&out.stdout);
    // The JSON is the last line; anything Vision said before it (a first-run note) is not.
    let line = text.lines().rev().find(|l| l.trim_start().starts_with('[')).ok_or_else(|| {
        let err = String::from_utf8_lossy(&out.stderr);
        if err.trim().is_empty() { "This Vision is too old to report its providers: update it.".to_string() } else { err.trim().to_string() }
    })?;
    serde_json::from_str(line).map_err(|e| e.to_string())
}

/// Run a setup step and stream what it prints as `setup` events ({run, line} then {run, done, code}):
/// `install` Vision, `service` (keep `vision serve` running), `upgrade`, or a provider's `provider_install`
/// / `provider_login` (`name` says which).
#[tauri::command]
async fn setup_run(app: AppHandle, action: String, name: Option<String>) -> Result<u64, String> {
    let bin = || vision_bin().map(|b| quote(&b.display().to_string())).ok_or("Vision isn't installed here.".to_string());
    let command = match action.as_str() {
        "install" => INSTALL.to_string(),
        "service" => format!("{} service install", bin()?),
        "upgrade" => format!("{} upgrade", bin()?),
        "provider_install" | "provider_login" => {
            let rows = provider_status().await?;
            let name = name.ok_or("Which provider?")?;
            let row = rows.as_array().and_then(|r| r.iter().find(|p| p["name"] == name.as_str())).ok_or("No such provider.")?;
            if action == "provider_install" {
                row["install_cmd"].as_str().filter(|c| !c.is_empty()).ok_or("It has no installer to run.")?.to_string()
            } else {
                let parts: Vec<String> = row["login_cmd"].as_array().ok_or("It has no sign-in.")?.iter().filter_map(|v| v.as_str()).map(quote).collect();
                parts.join(" ")
            }
        }
        _ => return Err(format!("unknown step {action}")),
    };
    static RUNS: AtomicU64 = AtomicU64::new(1);
    let run = RUNS.fetch_add(1, Ordering::SeqCst);
    let mut child = shell(&command)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .map_err(|e| e.to_string())?;
    let pipe = |reader: Box<dyn std::io::Read + Send>, app: AppHandle| {
        std::thread::spawn(move || {
            use std::io::BufRead;
            for line in std::io::BufReader::new(reader).lines().map_while(Result::ok) {
                let _ = app.emit("setup", json!({"run": run, "line": line}));
            }
        })
    };
    let out = pipe(Box::new(child.stdout.take().unwrap()), app.clone());
    let err = pipe(Box::new(child.stderr.take().unwrap()), app.clone());
    std::thread::spawn(move || {
        let code = child.wait().map(|s| s.code().unwrap_or(-1)).unwrap_or(-1);
        let _ = out.join();
        let _ = err.join();
        let _ = app.emit("setup", json!({"run": run, "done": true, "code": code}));
    });
    Ok(run)
}

/// How this copy was installed, for updating it: "app" (macOS) and "appimage" replace themselves; a
/// "package" (rpm, deb) is updated by downloading the new one; "dev" is a build from source.
#[tauri::command]
fn install_kind() -> &'static str {
    if cfg!(debug_assertions) {
        "dev"
    } else if cfg!(target_os = "macos") {
        "app"
    } else if std::env::var_os("APPIMAGE").is_some() {
        "appimage"
    } else {
        "package"
    }
}

/// Whether closing the window leaves Vision in the tray (Settings; on unless switched off), and the tray's
/// status line, when there is a tray to keep it in.
struct Tray {
    keep: AtomicBool,
    present: AtomicBool,
    status: Mutex<Option<MenuItem<tauri::Wry>>>,
}

fn show_window(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

/// The icon in the panel: a click brings the window back; its menu opens it, starts a chat, shows the
/// agents, says what they're up to and quits.
fn tray(app: &AppHandle) -> tauri::Result<()> {
    let show = MenuItemBuilder::with_id("tray_show", "Show Vision").build(app)?;
    let new_chat = MenuItemBuilder::with_id("tray_new_chat", "New Chat").build(app)?;
    let board = MenuItemBuilder::with_id("tray_agents", "All Activity").build(app)?;
    let status = MenuItemBuilder::with_id("tray_status", "No agents running").enabled(false).build(app)?;
    let quit = MenuItemBuilder::with_id("tray_quit", "Quit Vision").build(app)?;
    let sep = || PredefinedMenuItem::separator(app);
    let menu = Menu::with_items(app, &[&show, &new_chat, &sep()?, &status, &board, &sep()?, &quit])?;
    let mut builder = TrayIconBuilder::with_id("vision").menu(&menu).tooltip("Vision").show_menu_on_left_click(false);
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder
        .on_menu_event(|app, event| match event.id().as_ref() {
            "tray_show" => show_window(app),
            "tray_new_chat" => { show_window(app); let _ = app.emit("menu", "new_chat"); }
            "tray_agents" => { show_window(app); let _ = app.emit("menu", "agents"); }
            "tray_quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                show_window(tray.app_handle());
            }
        })
        .build(app)?;
    let state = app.state::<Tray>();
    *state.status.lock().unwrap() = Some(status);
    state.present.store(true, Ordering::SeqCst);
    Ok(())
}

/// The page's word on the agents ("1 needs you · 2 working"), for the tray's menu and tooltip.
#[tauri::command]
fn set_tray_status(app: AppHandle, tray: State<'_, Tray>, text: String) {
    let line = if text.is_empty() { "No agents running".to_string() } else { text.clone() };
    if let Some(item) = tray.status.lock().unwrap().as_ref() {
        let _ = item.set_text(&line);
    }
    if let Some(icon) = app.tray_by_id("vision") {
        let _ = icon.set_tooltip(Some(if text.is_empty() { "Vision".to_string() } else { format!("Vision · {text}") }));
    }
}

/// Settings' "Keep running in the tray": whether closing the window only hides it.
#[tauri::command]
fn set_keep_in_tray(tray: State<'_, Tray>, on: bool) -> bool {
    tray.keep.store(on, Ordering::SeqCst);
    tray.present.load(Ordering::SeqCst)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // A second launch brings this one's window back instead (it may be hidden in the tray).
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| show_window(app)))
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .manage(Link::default())
        .manage(Tray { keep: AtomicBool::new(true), present: AtomicBool::new(false), status: Mutex::new(None) })
        .on_window_event(|window, event| {
            // Closed with a tray to go to, the window only hides: replies, agents and their notifications go on.
            if let WindowEvent::CloseRequested { api, .. } = event {
                let tray = window.state::<Tray>();
                if tray.keep.load(Ordering::SeqCst) && tray.present.load(Ordering::SeqCst) {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .menu(menu)
        .on_menu_event(|app, event| {
            let _ = app.emit("menu", event.id().as_ref());
        })
        .setup(|app| {
            #[cfg(target_os = "linux")]
            portal::watch(app.handle().clone());
            // No tray on this desktop (stock GNOME, say): the window closes as usual.
            if let Err(e) = tray(app.handle()) {
                eprintln!("no tray icon: {e}");
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![saved, resume, local_vision, pair, forget, send, http, appearance, upload, upload_path, paste_files, speak, fetch_bytes, set_tray_status, set_keep_in_tray, vision_here, provider_status, setup_run, install_kind])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::*;

    fn env(pairs: &[(&str, &str)]) -> Vec<(String, Option<String>)> {
        child_env(pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())), Some(std::path::Path::new("/home/me")))
    }

    #[test]
    fn commands_from_an_appimage_get_none_of_its_environment() {
        let m = "/tmp/.mount_VisionAB12";
        let got = env(&[
            ("APPDIR", m),
            ("PYTHONHOME", &format!("{m}/usr/")),
            ("PYTHONPATH", &format!("{m}/usr/share/pyshared/:")),
            ("PYTHONDONTWRITEBYTECODE", "1"),
            ("LD_LIBRARY_PATH", &format!("{m}/usr/lib/:{m}/usr/lib64")),
            ("XDG_DATA_DIRS", &format!("{m}/usr/share:/usr/share")),
            ("HOME", "/home/me"),
            ("PATH", &format!("{m}/usr/bin/:/home/me/.local/share/vision/shims:/usr/bin:/home/me/.local/bin")),
        ]);
        let of = |k: &str| got.iter().find(|(n, _)| n == k).map(|(_, v)| v.clone());
        assert_eq!(of("PYTHONHOME"), Some(None), "Python finds its own home again");
        assert_eq!(of("PYTHONPATH"), Some(None));
        assert_eq!(of("PYTHONDONTWRITEBYTECODE"), Some(None));
        assert_eq!(of("LD_LIBRARY_PATH"), Some(None));
        assert_eq!(of("XDG_DATA_DIRS"), Some(Some("/usr/share".into())), "a list keeps what isn't in the mount");
        assert_eq!(of("HOME"), None, "the rest is left alone");
        assert_eq!(of("PATH"), Some(Some("/home/me/.local/bin:/usr/bin".into())), "no mount, no shims, ~/.local/bin first");
    }

    #[test]
    fn an_earlier_copys_mount_goes_too() {
        let (now, old) = ("/tmp/.mount_VisionNOW1", "/tmp/.mount_VisionOLD2");
        let got = env(&[
            ("APPDIR", now),
            ("LD_LIBRARY_PATH", &format!("{old}/usr/lib/:{old}/usr/lib64")),
            ("PYTHONPATH", &format!("{old}/usr/share/pyshared/:{now}/usr/share/pyshared/")),
            ("XDG_DATA_DIRS", &format!("/usr/share:{old}/usr/share:{now}/usr/share")),
            ("OWD", &format!("{old}/usr")),
            ("PATH", &format!("{now}/usr/bin/:{old}/usr/bin/:/usr/bin")),
        ]);
        let of = |k: &str| got.iter().find(|(n, _)| n == k).map(|(_, v)| v.clone());
        assert_eq!(of("LD_LIBRARY_PATH"), Some(None), "git no longer loads the old copy's OpenSSL");
        assert_eq!(of("PYTHONPATH"), Some(None));
        assert_eq!(of("OWD"), Some(None));
        assert_eq!(of("XDG_DATA_DIRS"), Some(Some("/usr/share".into())));
        assert_eq!(of("PATH"), Some(Some("/home/me/.local/bin:/usr/bin".into())));
    }

    #[test]
    fn outside_an_appimage_only_path_changes() {
        let got = env(&[("PYTHONHOME", "/opt/py"), ("PATH", "/usr/bin")]);
        assert_eq!(got, vec![("PATH".to_string(), Some("/home/me/.local/bin:/usr/bin".to_string()))]);
    }

    #[test]
    fn addresses_are_normalized_like_the_phone_does() {
        assert_eq!(normalize(" studio.example.ts.net/ "), Some("https://studio.example.ts.net".into()));
        assert_eq!(normalize("http://127.0.0.1:8765"), Some("http://127.0.0.1:8765".into()));
        assert_eq!(normalize("  "), None);
    }

    #[test]
    fn shell_arguments_are_quoted() {
        assert_eq!(quote("/home/a b/vision"), "'/home/a b/vision'");
        assert_eq!(quote("it's"), "'it'\\''s'");
    }

    #[test]
    fn a_pasted_picture_comes_out_as_a_png() {
        let png = png_of(2, 1, &[255, 0, 0, 255, 0, 0, 255, 128]).unwrap();
        assert_eq!(&png[..8], b"\x89PNG\r\n\x1a\n");
        let mut reader = png::Decoder::new(std::io::Cursor::new(png)).read_info().unwrap();
        let mut px = vec![0; reader.output_buffer_size().unwrap()];
        reader.next_frame(&mut px).unwrap();
        assert_eq!(px, [255, 0, 0, 255, 0, 0, 255, 128]);
    }

    #[test]
    fn upload_names_are_decoded() {
        assert_eq!(percent_decode("my%20photo%C3%A9.png"), "my photoé.png");
        assert_eq!(percent_decode("100%"), "100%");
        assert_eq!(percent_decode("a%2"), "a%2");
    }

    #[test]
    fn the_socket_follows_the_scheme() {
        assert_eq!(socket_url("http://127.0.0.1:8765"), "ws://127.0.0.1:8765/ws");
        assert_eq!(socket_url("https://studio.example.ts.net"), "wss://studio.example.ts.net/ws");
    }
}
