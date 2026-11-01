//! Dictation: the page records the microphone itself (getUserMedia, encoded to a 16 kHz mono WAV in mic.ts)
//! and the words come from the paired Vision's Whisper (POST /transcribe), through here like every call.

use std::time::Duration;

use serde_json::Value;
use tauri::State;
use tauri::webview::{PermissionKind, PermissionResponse};

use crate::Link;

/// The web view asking for a device. Only the microphone is granted, and only because the page asks for it
/// when the mic button is held: WebKitGTK denies every request nobody answers, and WKWebView would prompt
/// on each launch. The system's own prompt (macOS: NSMicrophoneUsageDescription) still comes first.
pub fn permission(kind: PermissionKind) -> PermissionResponse {
    match kind {
        PermissionKind::Microphone => PermissionResponse::Allow,
        _ => PermissionResponse::Default,
    }
}

/// A recording (the call's raw body, a WAV) as words, from the paired Vision's Whisper.
#[tauri::command]
pub async fn transcribe(link: State<'_, Link>, request: tauri::ipc::Request<'_>) -> Result<String, String> {
    let tauri::ipc::InvokeBody::Raw(wav) = request.body() else { return Err("No recording.".into()) };
    let pairing = link.pairing.lock().unwrap().clone().ok_or("Not paired.")?;
    let part = reqwest::multipart::Part::bytes(wav.clone()).file_name("speech.wav").mime_str("audio/wav").map_err(|e| e.to_string())?;
    let resp = link
        .http
        .post(format!("{}/transcribe", pairing.url))
        .bearer_auth(&pairing.token)
        .multipart(reqwest::multipart::Form::new().part("file", part))
        // The first one after a restart loads Whisper.
        .timeout(Duration::from_secs(120))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    let code = resp.status().as_u16();
    words(code, resp.json().await.unwrap_or(Value::Null))
}

/// The words in /transcribe's answer, or what to say instead: a 503 says whether the voice is off or no
/// Whisper is installed there.
fn words(code: u16, data: Value) -> Result<String, String> {
    if (200..300).contains(&code) {
        return Ok(data.get("text").and_then(Value::as_str).unwrap_or("").trim().to_string());
    }
    match data.get("detail").and_then(Value::as_str) {
        Some(detail) if !detail.is_empty() => Err(detail.to_string()),
        _ => Err(format!("HTTP {code}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn the_words_come_back_trimmed() {
        assert_eq!(words(200, json!({"text": " Hello there. "})), Ok("Hello there.".into()));
        assert_eq!(words(200, json!({})), Ok(String::new()));
    }

    #[test]
    fn a_refusal_says_why() {
        assert_eq!(words(503, json!({"detail": "The voice is off on the computer."})), Err("The voice is off on the computer.".into()));
        assert_eq!(words(500, Value::Null), Err("HTTP 500".into()));
    }

    #[test]
    fn only_the_microphone_is_granted() {
        assert!(matches!(permission(PermissionKind::Microphone), PermissionResponse::Allow));
        assert!(matches!(permission(PermissionKind::Camera), PermissionResponse::Default));
    }
}
