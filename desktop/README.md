# Vision for the desktop

A window onto `vision serve`, for Linux and macOS (Windows may follow; the code keeps clear of anything that would rule it out): the same chats the phone sees, with replies
streaming in, Vision's questions to answer and the model to switch. Like the iPhone app it is only a client;
the conversations run on the computer with `vision serve`.

## Pairing

- On the computer running `vision serve`, **Use Vision on this computer** finds it by itself.
- Anywhere else, give it the address `vision pair` shows and the key from that computer's
  `~/.config/vision/remote_token`, or paste the pairing code (`{"url": …, "token": …}`) into the address box.

The address and key are kept in the app's config folder (`~/.config/com.vision.desktop/pairing.json` on Linux).

## Keys

- Return sends; Shift-Return starts a new line. While Vision replies, a message waits its turn.
- `!command` runs a command on the computer, in the chat's folder.
- Esc stops the reply (or closes whatever is open).
- Ctrl/Cmd-N new chat, Ctrl/Cmd-K search, Ctrl/Cmd-B sidebar, Ctrl/Cmd-Shift-M model, Ctrl/Cmd-, settings.

## Building

Needs Node 22 and Rust (`rustup`); on a Mac, also Xcode's command line tools. On Linux, the WebKitGTK
headers too:

    sudo dnf install webkit2gtk4.1-devel gtk3-devel libsoup3-devel librsvg2-devel openssl-devel   # Fedora
    sudo apt install libwebkit2gtk-4.1-dev libgtk-3-dev libsoup-3.0-dev librsvg2-dev libssl-dev    # Ubuntu, Debian

Then, in `desktop/`:

    npm install
    npm run tauri dev      # a window that reloads as you edit
    npm run tauri build    # installers in src-tauri/target/release/bundle/

`./dev.sh` at the repo root runs the whole dev stack beside an installed Vision: a `vision serve` from the
checkout (its `.venv`, from `uv sync --extra serve`) on port 8766 with its own data, config and state under
`~/.vision-dev`, then `npm run tauri dev`, which finds that serve by itself. Its memory and settings start
empty. `PORT=` picks another port; Ctrl-C stops both.

`python ios/fake_server.py --port 8799` stands in for `vision serve` with no model behind it: pair with
`http://127.0.0.1:8799` and any key, then `POST /_scene` and `POST /_push` frames to it (see its docstring).

`npm run shots` screenshots the window without opening one: the built page in headless Chromium, with
`harness/fake-tauri.js` standing in for the native side and `harness/scenes.js` saying what happens (frames
arriving, clicks, typing) and what to check. Pictures land in `harness/out/`; `node harness/shots.mjs plan:light`
runs one scene in one theme (`:macos` adds the Mac title bar).

CI (`.github/workflows/desktop.yml`) builds Linux and macOS on every change under `desktop/` and keeps
the installers as artifacts (unsigned; releases are below).

## Releasing

    node scripts/version.mjs 0.2.0      # tauri.conf.json, package.json and Cargo.toml together
    git commit -am "Desktop app 0.2.0" && git tag desktop-v0.2.0 && git push origin master desktop-v0.2.0

The tag runs `.github/workflows/desktop-release.yml`: a universal macOS app (Apple Silicon and Intel) and
Linux deb, rpm and AppImage, in a **draft** GitHub Release with the updater's `latest.json`. Look it over and
publish it; installed apps find it on their next check (at launch and every six hours). The Mac app and an
AppImage update themselves ("Restart to update"); an rpm or deb install is offered the download.

The updater reads `releases/latest/download/latest.json`, so the newest published release of the repo has to
be a desktop one (it is the only kind there is today).

Updates are signed with the updater key, from the repository's Actions secrets (Settings › Secrets and
variables › Actions, which needs admin on the repo). Without them the run warns and makes no `latest.json`:

| Secret | What it is |
| --- | --- |
| `TAURI_SIGNING_PRIVATE_KEY` | The updater's private key (`npx tauri signer generate`). Its public half is `plugins.updater.pubkey` in `tauri.conf.json`; lose the private key and installed apps can never update again, so keep a copy somewhere safe. |
| `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` | Its password. |

The Mac app is signed with a Developer ID and notarized when these secrets are there too; without them it
is built unsigned and macOS makes people allow it once, in System Settings › Privacy & Security:

| Secret | What it is |
| --- | --- |
| `APPLE_CERTIFICATE` | The Developer ID Application certificate and its private key, exported from Keychain Access as a `.p12`, then `base64 -i cert.p12`. |
| `APPLE_CERTIFICATE_PASSWORD` | The password the `.p12` was exported with. |
| `APPLE_SIGNING_IDENTITY` | The certificate's name, as `security find-identity -v -p codesigning` prints it. |
| `APPLE_API_ISSUER` | Issuer ID of an App Store Connect API key (Users and Access › Integrations), for notarization. |
| `APPLE_API_KEY` | That key's ID. |
| `APPLE_API_KEY_P8` | The contents of its `AuthKey_<id>.p8`. |

## How it is put together

The server takes its key only in an `Authorization` header and answers no cross-origin requests, so the web
view can't reach it directly. `src-tauri/src/lib.rs` holds the WebSocket (reconnecting, with a ping every
20 s) and makes the HTTP calls; frames reach the page as `frame` events. `src/main.ts` keeps the chats and
draws them (`src/render.ts` for messages and cards), following what the iPhone app's `VisionClient.swift` does
with the same frames.
