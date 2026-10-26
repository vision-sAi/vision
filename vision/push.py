"""Push notifications to the Vision iPhone app: a reminder, or a scheduled task's reply, reaches the
phone while the app is shut.

Apple only takes pushes for an app from whoever holds the APNs key of the account it is built under.
Someone who builds the app under their own Apple Developer account gives Vision that key ([push] in the
config, the .p8 beside it): Vision signs an ES256 provider token with it and posts straight to Apple.
APNs speaks HTTP/2 only, which the standard library does not, so the request goes through `curl`
(headers on stdin, never in the process list). Without a key of one's own, pushes go through a relay
([push] relay), a server that holds the key of the account the app was built under.

What Apple carries is sealed: the app makes a random key when it registers (POST /push/register, over
the paired connection) and this module encrypts each notification with it (AES-256-GCM). The relay and
Apple carry ciphertext; the app's notification extension unseals it on the phone.

Devices live in ~/.local/share/vision/push_devices.json (0600): [{token, key, sandbox, name, seen}].
A token Apple calls dead is dropped; one registered against the other environment (a development
build uses Apple's sandbox, TestFlight and the App Store the production host) is retried there and
remembered. Signing and sealing need the `cryptography` package (the `weather` extra brings it).
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from vision.config import CONFIG_DIR, DATA_DIR, PushConfig

DEVICES = DATA_DIR / "push_devices.json"
HOSTS = {False: "api.push.apple.com", True: "api.sandbox.push.apple.com"}
TOKEN_LIFETIME_S = 45 * 60  # Apple wants one under an hour old, and no more than one new token per 20 minutes
LIFETIME_S = 60 * 60  # a notification Apple cannot deliver within the hour is dropped, not sprung days late
GONE = ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic")
BODY_MAX = 600  # characters of a reply that travel; the phone shows about four lines of it

_lock = threading.Lock()
_jwt: tuple[float, str, str] | None = None  # (minted, key id, token)


class PushError(Exception):
    pass


def direct(cfg: PushConfig) -> bool:
    """A key of one's own is set up (an app built under one's own account): push straight to Apple."""
    return bool(cfg.team_id.strip() and cfg.key_id.strip() and cfg.topic.strip() and key_path(cfg).is_file())


def ready(cfg: PushConfig) -> bool:
    """Push is on, there is a way to Apple (one's own key or the relay) and the crypto library is here."""
    if not (cfg.enabled and (direct(cfg) or cfg.relay.strip())):
        return False
    try:
        import cryptography  # noqa: F401
    except ImportError:
        return False
    return True


def key_path(cfg: PushConfig) -> Path:
    p = Path(cfg.key_file or "apns.p8").expanduser()
    return p if p.is_absolute() else CONFIG_DIR / p


def make_token(cfg: PushConfig, now: float | None = None) -> str:
    """A signed APNs provider token (ES256), reused until TOKEN_LIFETIME_S old."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    from vision.weather import _b64url

    global _jwt
    now = int(now if now is not None else time.time())
    with _lock:
        if _jwt and _jwt[1] == cfg.key_id and now - _jwt[0] < TOKEN_LIFETIME_S:
            return _jwt[2]
        try:
            key = serialization.load_pem_private_key(key_path(cfg).read_bytes(), password=None)
        except (OSError, ValueError, TypeError) as e:
            raise PushError(f"APNs key at {key_path(cfg)} is not a readable .p8 private key: {e}") from e
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise PushError("APNs keys must be EC (P-256) keys from developer.apple.com.")
        signing_input = _b64url(json.dumps({"alg": "ES256", "kid": cfg.key_id.strip()}, separators=(",", ":")).encode()) + "." + \
            _b64url(json.dumps({"iss": cfg.team_id.strip(), "iat": now}, separators=(",", ":")).encode())
        r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
        token = signing_input + "." + _b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        _jwt = (now, cfg.key_id, token)
        return token


# -- the phones
def devices(path: Path | None = None) -> list[dict]:
    try:
        data = json.loads((path or DEVICES).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict) and d.get("token") and d.get("key")] if isinstance(data, list) else []


def _save(items: list[dict], path: Path | None = None) -> None:
    path = path or DEVICES
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=1), encoding="utf-8")
    tmp.chmod(0o600)  # it holds the keys the notifications are sealed with
    os.replace(tmp, path)


def register(body: dict, path: Path | None = None) -> dict:
    """Add a phone, or refresh the one with this token: {token (hex), key (base64, 32 bytes), sandbox?,
    name?}. Raises ValueError."""
    token = str(body.get("token") or "").strip().lower()
    if not token or len(token) > 200 or any(c not in "0123456789abcdef" for c in token):
        raise ValueError("token must be the device token in hex")
    try:
        key = base64.b64decode(str(body.get("key") or ""), validate=True)
    except ValueError:
        key = b""
    if len(key) != 32:
        raise ValueError("key must be 32 random bytes in base64")
    dev = {"token": token, "key": base64.b64encode(key).decode(), "sandbox": bool(body.get("sandbox")),
           "name": str(body.get("name") or "")[:80], "seen": time.time()}
    with _lock:
        _save([d for d in devices(path) if d["token"] != token] + [dev], path)
    return dev


def unregister(token: str, path: Path | None = None) -> bool:
    token = token.strip().lower()
    with _lock:
        before = devices(path)
        after = [d for d in before if d["token"] != token]
        if len(after) != len(before):
            _save(after, path)
        return len(after) != len(before)


# -- sealing
def message(title: str, body: str, chat: str = "", project: str = "", thread: str = "", category: str = "",
            approval: str = "") -> dict:
    """What the phone shows, before it is sealed: {title, body, chat, project?, thread?, category?, approval?}
    (the app opens `chat` on a tap; with `project` and `thread`, that project thread's view). `category` puts
    buttons on it, answered without opening the app: "approval" (Approve / Deny `approval`, a scout's request)
    or "land" (Land `thread`, finished and ready for review)."""
    body = " ".join(body.split())
    if len(body) > BODY_MAX:
        body = body[:BODY_MAX - 1].rstrip() + "…"
    msg = {"title": title, "body": body, "chat": chat}
    if project:
        msg["project"] = project
        if thread:
            msg["thread"] = thread
        if category:
            msg["category"] = category
            if approval:
                msg["approval"] = approval
    return msg


def seal(key_b64: str, msg: dict) -> str:
    """`msg` encrypted for one phone: base64 of nonce (12 bytes) + ciphertext + tag, which is the
    layout CryptoKit's AES.GCM.SealedBox(combined:) reads."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    sealed = AESGCM(base64.b64decode(key_b64)).encrypt(nonce, json.dumps(msg, ensure_ascii=False).encode(), None)
    return base64.b64encode(nonce + sealed).decode()


def unseal(key_b64: str, enc: str) -> dict:
    """The other way (what the app does); here for the tests."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    raw = base64.b64decode(enc)
    return json.loads(AESGCM(base64.b64decode(key_b64)).decrypt(raw[:12], raw[12:], None))


# -- sending
def payload(enc: str) -> dict:
    """What Apple carries: fixed words, shown only if the app cannot unseal `enc` (a phone that has
    since re-paired, say), and the sealed notification."""
    return {"aps": {"alert": {"title": "Vision", "body": "Open Vision to read this."}, "sound": "default",
                    "mutable-content": 1}, "enc": enc}


def _post(cfg: PushConfig, token: str, sandbox: bool, data: str, timeout: float, kind: str = "alert") -> tuple[int, str]:
    """One APNs request; (HTTP status, Apple's reason or ""). `kind` "liveactivity" is a change to a Lock
    Screen card (liveactivity.py), which Apple takes on the app's topic with its own suffix."""
    if not direct(cfg):
        return _post_relay(cfg.relay, token, sandbox, data, timeout, kind)
    topic = cfg.topic.strip() + (".push-type.liveactivity" if kind == "liveactivity" else "")
    conf = "\n".join([
        f'url = "https://{HOSTS[sandbox]}/3/device/{token}"',
        f'header = "authorization: bearer {make_token(cfg)}"',
        f'header = "apns-topic: {topic}"',
        f'header = "apns-push-type: {kind}"',
        'header = "apns-priority: 10"',
        f'header = "apns-expiration: {int(time.time()) + LIFETIME_S}"',
        'header = "content-type: application/json"',
    ]) + "\n"
    try:
        r = subprocess.run(
            ["curl", "--silent", "--http2", "--max-time", str(int(timeout)), "--write-out", "\n%{http_code}",
             "--data-binary", data, "--config", "-"],
            input=conf, capture_output=True, text=True, encoding="utf-8", timeout=timeout + 5)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PushError(f"could not reach {HOSTS[sandbox]}: {e}") from e
    out, _, code = r.stdout.rpartition("\n")
    if r.returncode != 0 or not code.strip().isdigit():
        raise PushError(f"could not reach {HOSTS[sandbox]}: {r.stderr.strip() or f'curl exit {r.returncode}'}")
    try:
        reason = str(json.loads(out).get("reason") or "") if out.strip() else ""
    except ValueError:
        reason = out.strip()[:120]
    if int(code) == 403:
        global _jwt
        _jwt = None  # an expired or refused provider token: mint afresh next time
    return int(code), reason


def _post_relay(relay: str, token: str, sandbox: bool, data: str, timeout: float, kind: str) -> tuple[int, str]:
    """The same request through the relay, which signs it with the app's key; for a
    notification only the sealed `enc` is read there."""
    body = {"token": token, "sandbox": sandbox, "kind": kind, "payload": json.loads(data)}
    req = urllib.request.Request(relay.rstrip("/") + "/push", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "Vision/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            answer = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            why = json.loads(e.read().decode()).get("error")
        except (ValueError, AttributeError):
            why = None
        raise PushError(f"the relay said {e.code}: {why or e.reason}") from e
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise PushError(f"could not reach the relay: {getattr(e, 'reason', e)}") from e
    return int(answer.get("status") or 0), str(answer.get("reason") or "")


def send(cfg: PushConfig, title: str, body: str = "", chat: str = "", path: Path | None = None,
         post=_post, timeout: float = 10, project: str = "", thread: str = "", category: str = "",
         approval: str = "") -> list[str]:
    """Notify every registered phone; returns one line per phone that did not get it (empty = all did,
    or there are none). Raises PushError when push is not set up."""
    if not ready(cfg):
        raise PushError("push is off: it needs [push] relay, or your own APNs key in [push], and the "
                        "`cryptography` package")
    if post is _post and direct(cfg) and not shutil.which("curl"):
        raise PushError("push needs curl (APNs is HTTP/2 only)")
    targets = devices(path)
    if not targets:
        return []
    msg = message(title, body, chat, project, thread, category, approval)
    failed, gone, moved = [], [], {}
    for dev in targets:
        label = dev.get("name") or dev["token"][:8]
        data = json.dumps(payload(seal(dev["key"], msg)), separators=(",", ":"))
        try:
            status, reason = post(cfg, dev["token"], dev["sandbox"], data, timeout)
            if status == 400 and reason == "BadDeviceToken":  # a token from the other environment
                status, reason = post(cfg, dev["token"], not dev["sandbox"], data, timeout)
                if status == 200:
                    moved[dev["token"]] = not dev["sandbox"]
        except PushError as e:
            failed.append(f"{label}: {e}")
            continue
        if status == 200:
            continue
        if status == 410 or reason in GONE:
            gone.append(dev["token"])
        failed.append(f"{label}: {status} {reason}".rstrip())
    if gone or moved:
        with _lock:
            _save([{**d, "sandbox": moved.get(d["token"], d["sandbox"])} for d in devices(path) if d["token"] not in gone], path)
    return failed
