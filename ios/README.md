# Vision Remote (iOS)

A SwiftUI app that talks to `vision serve` on the machine: text chat with streamed replies, dictation
through the machine's Whisper, a hands-free live voice mode, replies read aloud by the machine's
voice, AskUserQuestion forms, model switching and resuming earlier conversations.

```
iPhone ──ws/http (same Wi-Fi)──────────────────────────────────▶ machine: vision serve (0.0.0.0:8765)
iPhone ──wss/https──▶ tunnel (Tailscale Funnel / Cloudflare) ──▶ machine: vision serve
                                                                     ├─ brain   (claude -p / codex exec / grok --prompt-file)
                                                                     ├─ ears    (faster-whisper)
                                                                     └─ voice   (Qwen3-TTS → WAV per sentence)
```

## 1. Machine: publish `vision serve` over HTTPS (for use away from home)

At home nothing is needed: `vision serve` listens on the LAN and the QR carries the machine's Wi-Fi
address (plain http, which the app allows for local networking). To use the phone from anywhere, put
a tunnel in front of it once (and set `host = "127.0.0.1"` under `[remote]` if you want the LAN port closed):

**Tailscale Funnel (recommended: free, no domain, certificates handled)**

Install Tailscale on the machine ([tailscale.com/download](https://tailscale.com/download)) and log in, then:

```bash
tailscale funnel --bg 8765         # publishes https://<machine>.<tailnet>.ts.net → 127.0.0.1:8765 (sudo on Linux)
tailscale funnel status
```

The first `funnel` prints a link to enable HTTPS certificates and Funnel for your tailnet if they are
not on yet. `vision serve` detects the Tailscale hostname on its own.

**Cloudflare Tunnel (if you own a domain on Cloudflare)**

```bash
cloudflared tunnel login
cloudflared tunnel create vision
cloudflared tunnel route dns vision vision.example.com
cloudflared tunnel run --url http://127.0.0.1:8765 vision
```

then set `public_url = "https://vision.example.com"` under `[remote]` in `~/.config/vision/config.toml`
(or pass `--public-url`).

Then:

```bash
vision serve            # prints the address, the token and a QR code; Ctrl-C stops
```

`vision serve --new-token` rotates the token (the phone must re-pair). The token lives in
`~/.config/vision/remote_token` (mode 600). Everything the app can do goes through that token, so
treat it like a password.

## 2. Mac: build the app

Requirements: Xcode 15 or newer, an iPhone on iOS 18+, a free or paid Apple developer account (paid for
notifications, below).

```bash
open ios/VisionRemote/VisionRemote.xcodeproj
```

`VisionRemote.xcodeproj` is committed. `project.yml` can regenerate it with
`brew install xcodegen && xcodegen` in `ios/VisionRemote`; commit the result.

To run it on your own phone under your own Apple team (a free Personal Team works), copy
`VisionRemote/Local.xcconfig.example` to `VisionRemote/Local.xcconfig` and set your team ID and a bundle id
prefix there. It is gitignored and overrides `Signing.xcconfig`, so don't change signing in Xcode's
Signing & Capabilities tab. Then plug the phone in and Run. Without XcodeGen: File → New → Project → iOS App
(SwiftUI), delete its `ContentView.swift`, drag the `VisionRemote/` folder in, and add the three
Info.plist keys from `project.yml` (microphone, camera, background audio).

**Notifications** (reminders and scheduled replies while the app is shut) need an APNs key, which only a
paid Apple Developer account can make: Certificates, Identifiers & Profiles → Keys → a new key with Apple
Push Notifications service. Save the .p8 as `~/.config/vision/apns.p8` and fill in `team_id`, `key_id` and
`topic` (your bundle id prefix + `.VisionRemote`) under `[push]` in the config; the comments there say the
rest. Without one the app works, just without notifications.

### Xcode Cloud and TestFlight

To build and distribute your own copy through TestFlight, connect your fork or repository to Xcode Cloud
from your Apple account and create a workflow for the `VisionRemote` scheme. In that workflow, set
`VISION_TEAM_ID` and `VISION_BUNDLE_PREFIX` as secret environment variables. The post-clone script writes
them to the ignored `VisionRemote/Local.xcconfig`, which supplies the team and bundle IDs to all app
targets. Add your signing certificates and profiles through your own Apple developer account in Xcode
Cloud. No Apple account values or credentials belong in a commit. The same setup works for anyone
shipping their own copy; each person uses their own team and bundle ID prefix.

For a one-off TestFlight or App Store build without Xcode Cloud, archive it in Xcode under your own team.

## 3. Pair

The first time the app opens it shows a welcome screen: tap **Scan pairing code** and point it at the
QR in the `vision serve` terminal (or **Enter address manually** and type the address and token). It
checks the connection there and says "Connected to <your computer>" before the chat opens, or why it
could not reach it. `vision pair` shows the code again; `p` does the same in the `vision serve`
window. More Visions are added later under Settings → the connection row.

## Using it

- Type and send; replies stream in. Messages sent during a reply wait their turn in order. Tap ■ to interrupt.
- The folder button beside **Chat** in the drawer starts a new chat in a folder you pick on the machine:
  the recent ones are listed first (the default folder, open chats' and earlier conversations'), and
  below them the machine's folders to walk through. **Start chat here** opens the chat in the folder being
  shown. An empty chat says which folder it works in, and tapping that line opens the same picker:
  the chat you pick there takes the empty one's place. The plain **Chat** button still uses the
  machine's own folder.
- The icon beside the effort in the composer is the chat's mode: a bolt for **auto**, an orange
  clipboard for **plan**. A tap switches between them, as Shift-Tab does in the terminal, and a note
  says which it now is. In plan mode Vision only reads and proposes; its
  plan arrives as a question form, and Yes carries it out (and turns the chat to auto, as there). It
  can't be switched while a reply is running. A chat keeps its mode across `vision restart`.
- A reply says how long its turn has taken, as the terminal does: the time counts up under it while it
  streams and stays there once it is done. Opening the app mid-reply picks the count up from the machine's
  figure. Replies from before the machine's `vision serve` last started have no time to show.
- Settings says when the app and the computer's Vision have drifted apart: the computer's Vision is
  older than the app needs, or the app is older than that Vision (either also said once in a toast,
  not every launch), or they fit and an update is out for the computer, with what to run there. An
  update the computer finds while the app is connected shows up straight away.
- Under a finished reply, beside its time, two small buttons copy the whole reply or hand it to the
  share sheet (as the machine wrote it, markdown included). A reply still being written has neither.
- Tap the mic to dictate: the machine transcribes and the words land in the composer, yours to correct
  and send as an ordinary typed message. Nothing is sent until you send it, and the turn is routed as
  typed text.
- Tap the waveform for **live voice mode**: a call with Vision, on its own screen. The microphone stays
  open, each utterance is cut on silence and sent as it finishes, and the reply is spoken back. Talking
  over her stops her mid-sentence — capture runs through iOS's voice-processing unit, so the speaker's
  own output is cancelled out of the microphone rather than heard as an interruption. Mute holds the
  microphone; ✕ ends the call and the whole conversation is in the transcript behind it.
  Spoken turns go to the chat's conversation model (its own model when that is a Claude, Codex or Local
  one) with tools disabled, which delegates
  structured tasks to the selected CLI agent when needed and then narrates the results.
- "Speak replies" reads typed agent turns aloud too, without changing their route. Spoken replies
  continue after minimising or locking the phone. Both use Vision's own Live Activity (the
  `VisionActivity` widget extension) in the Dynamic Island and on the Lock Screen, never a Now Playing
  player: a spoken reply shows "Speaking" with a stop button; live voice mode keeps its connection and
  microphone active in the background so later turns can be spoken too, and shows Pip, a waveform
  while it hears you, a speaker while Vision talks, a call timer, and mute and hang-up.
  Force-quitting the app ends iOS background audio.
- When Vision asks a question, a form appears; Skip lets it carry on without answers.
- Menu: new conversation, earlier conversations (for the active provider), model + effort, settings.
- The app wears Panes, the desktop app's look: violet on a soft glow, the chat floating beside the drawer as a
  card. It follows the phone's light and dark; there is no other theme to pick.
- The drawer lists the scouts under their row, each with its buddy and what it's up to in that state's colour
  (amber when it needs you); a tap opens its page.
- If the app is backgrounded during a silent text reply, the machine carries on; on return the reply is
  picked up, and the combined typed/voice transcript comes back from the server. Voice history lasts while the server runs;
  resuming an older native session restores its typed history.

## Protocol (for other clients)

All HTTP requests: `Authorization: Bearer <token>`. WebSocket: the same header. The token is never read from the URL.

| | |
|---|---|
| `GET /health` | model, effort, session id, busy |
| `GET /history?limit=60` | `[{role, text}]` for the current typed/voice conversation |
| `GET /models` · `GET /sessions` | pickers |
| `POST /transcribe` (multipart `file`, WAV/FLAC/OGG) | `{text}` |
| `POST /speak` `{text}` | `audio/wav` |
| `WS /ws` | see the docstring at the top of `vision/server.py` |

In a `message` frame, `voice: true` identifies transcribed speech and selects the conversation model;
`voice: false` (the default) sends text straight to the agent. `speak` controls audio playback independently.
`hello` and `done` include `history_id`, an opaque id for the combined transcript; `session_id` remains
the typed agent's native session id and can be absent in a voice-only conversation.
