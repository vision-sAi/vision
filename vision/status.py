"""What `vision serve` is running for speech, in words the phone can show as they are (/health "status").

Read from the config, the speech models on disk and whatever the hub has already loaded: nothing is loaded
or timed here, so /health stays quick. The server owns the wording, so a new check needs no app update.
"""
from __future__ import annotations

from vision.config import ListenConfig, VoiceConfig

# Row states the app colours: ready (green), loading (spinner), failed (orange), off (grey).
READY, LOADING, FAILED, OFF = "ready", "loading", "failed", "off"


def _row(label: str, value: str, state: str) -> dict:
    return {"label": label, "value": value, "state": state}


def _model_name(repo: str) -> str:
    """mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit -> 1.7B 8-bit."""
    tail = repo.rsplit("/", 1)[-1]
    size = next((p for p in tail.split("-") if p.endswith("B") and p[:-1].replace(".", "").isdigit()), "")
    bits = tail.rsplit("-", 1)[-1]
    bits = bits.replace("bit", "-bit") if bits.endswith("bit") else ""
    return " ".join(p for p in (size, bits) if p) or tail


def voice_status(cfg: VoiceConfig, speaker=None) -> tuple[dict, list[str]]:
    """The voice's row and any problems. `speaker`: the hub's Speaker, if one was made."""
    from vision import tts
    from vision.config import saved_voices

    problems: list[str] = []
    name = cfg.voice
    if cfg.engine == "qwen3" and name not in saved_voices():
        problems.append(f"There is no voice named “{name}” on the server. Make it with: vision voice design {name}")
    if not tts.models_present(cfg):
        problems.append("The voice model is not downloaded. Run: vision setup")

    mlx = tts.uses_mlx(cfg)
    parts = [name]
    if mlx:
        parts.append(_model_name(tts.mlx_model(cfg)))
        choice = tts.mlx_choice()
        if not cfg.mlx_model and choice:
            chip = tts.mac_chip()
            if choice.get("chip") != chip:
                problems.append(f"The voice model was timed on {choice.get('chip') or 'another Mac'}, not this {chip}. "
                                "Run: vision setup --retime")
            else:
                rtf = float(choice.get("rtf") or 0)
                parts.append(f"{rtf:.2f} s per second of speech")
                if rtf > 1:
                    problems.append("The voice is made slower than it plays, so speech will break up.")

    state = OFF
    if speaker is not None:
        error = getattr(speaker, "_load_error", None)
        if getattr(speaker, "_loaded", False):
            state = READY
            if speaker.device == "cpu":
                problems.append("The voice runs on the CPU, slower than it plays, so speech will break up.")
        elif error:
            state = FAILED
            problems.append(f"The voice did not load: {error[0]}")
        else:
            state = LOADING
    return _row("Voice", " · ".join(p for p in parts if p), state), problems


def ears_status(cfg: ListenConfig, stt=None, error: str = "") -> tuple[dict, list[str]]:
    """Speech recognition's row and any problems. `stt`: the hub's Transcriber; `error`: why it failed to load."""
    from vision.stt import mlx_whisper_repo

    problems: list[str] = []
    device = getattr(stt, "device", "?") if stt is not None else "?"
    if device != "?":  # loaded: "mlx/large-v3-turbo", "cuda/large-v3-turbo", "cpu/small.en"
        value, state = device.partition("/")[2], READY
    else:
        planned = "large-v3-turbo" if mlx_whisper_repo(cfg) else cfg.whisper_model
        value, state = planned, (FAILED if error else LOADING if stt is not None else OFF)
        if error:
            problems.append(f"Speech recognition did not load: {error}")
    return _row("Speech recognition", value, state), problems


def server_status(voice_cfg: VoiceConfig, listen_cfg: ListenConfig, speaker=None, stt=None, stt_error: str = "") -> dict:
    """{"rows": [...], "problems": [...]} for /health. Never raises: a broken check becomes a problem line."""
    rows, problems = [], []
    for check in (lambda: voice_status(voice_cfg, speaker), lambda: ears_status(listen_cfg, stt, stt_error)):
        try:
            row, found = check()
        except Exception as e:  # noqa: BLE001 - a missing voice build, say; /health must still answer
            row, found = None, [f"Could not check: {e}"]
        if row:
            rows.append(row)
        problems += found
    return {"rows": rows, "problems": problems}
