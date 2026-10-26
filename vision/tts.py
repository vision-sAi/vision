"""Text-to-speech. Two engines behind one Speaker: Qwen3-TTS 1.7B (clones a saved or designed voice; the
default) and Orpheus 3B (llama.cpp + SNAC, eight built-in voices). Both stream audio as it is generated."""
from __future__ import annotations

import contextlib
import hashlib
import os
import queue
import random
import re
import signal
import sys
import threading
import time
from pathlib import Path

import numpy as np

from vision.cuda import GpuBusy, GpuClaim
from vision.mlxrun import MlxWorker as _MlxWorker, mlx_available, mlx_fetch
from vision.config import (
    LLAMA_DIR,
    MLX_TTS_DESIGN,
    MLX_TTS_TIERS,
    MODELS_DIR,
    ORPHEUS_MODEL_FILE,
    QWEN_TTS_BASE,
    QWEN_TTS_DESIGN,
    SNAC_MODEL_FILE,
    STATE_DIR,
    VOICE_PRESETS,
    VOICES,
    AudioDevice,
    VoiceConfig,
    resolve_device,
    saved_voices,
    voice_dir,
)

SAMPLE_RATE = 24000  # both engines speak 24 kHz mono
_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"')\]]?\s+|\n+")
_ABBREV = re.compile(r"\b(e\.g|i\.e|etc|vs|Mr|Mrs|Ms|Dr|St|No|approx)\.$", re.I)
_TAIL_END = re.compile(r"[.!?…][\"')\]]?\s*$")  # a buffer that ends on a finished sentence


class TTSError(RuntimeError):
    pass


def _held(busy: GpuBusy) -> str:
    """Why the voice can't load here: another Vision has it. When that is the background service,
    which has no window to close, say so and name the command that connects to it instead."""
    from vision import service

    try:
        ours = busy.pid == service.pid()
    except Exception:  # noqa: BLE001 - no service manager to ask
        ours = False
    if not ours:
        return f"voice is {busy}"
    return (f"voice is already loaded in another Vision (pid {busy.pid} {busy.name}): the background service, and only one "
            "Vision can have the voice at a time. Don't start another `vision serve`; connect to the one that is "
            "running: `vision pair` shows its pairing code.")


def model_files() -> tuple[Path, Path]:
    return MODELS_DIR / ORPHEUS_MODEL_FILE, MODELS_DIR / SNAC_MODEL_FILE


def llama_server_bin() -> Path:
    return LLAMA_DIR / "llama-server"


def orpheus_present() -> bool:
    return all(p.exists() for p in (*model_files(), llama_server_bin()))


def qwen_present(repo: str = QWEN_TTS_BASE) -> bool:
    """True when the Hugging Face cache already holds the model (no network involved)."""
    from huggingface_hub import try_to_load_from_cache

    return isinstance(try_to_load_from_cache(repo, "model.safetensors"), str)


def models_present(cfg: VoiceConfig | None = None) -> bool:
    cfg = cfg or VoiceConfig()
    if cfg.engine == "orpheus":
        return orpheus_present()
    return mlx_present(mlx_model(cfg)) if uses_mlx(cfg) else qwen_present()


def uses_mlx(cfg: VoiceConfig) -> bool:
    """Whether qwen3 runs through MLX here: Apple Silicon with the voice-mac build, unless the config pins torch."""
    return cfg.engine == "qwen3" and cfg.device in ("auto", "mlx") and mlx_available()


def _engine(cfg: VoiceConfig, stop: threading.Event):
    if cfg.engine == "orpheus":
        return OrpheusEngine(cfg, stop)
    return Qwen3MlxEngine(cfg, stop) if uses_mlx(cfg) else Qwen3Engine(cfg, stop)


# ---------------------------------------------------------------- text cleanup
_FENCE = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]+\)")  # a picture shown on screen: nothing to say aloud
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL = re.compile(r"https?://\S+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.M)
_EMPH = re.compile(r"(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.M)
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF⬀-⯿️]+"
)
_SOUND_TAG = re.compile(r"\s*<(?:chuckle|laugh|sigh|gasp|groan|yawn|cough|sniffle)>\s*", re.I)
# A dash used as a spoken pause ("word - word", "word -- word", "word—word"): the voice reads straight
# through dashes, so it becomes the comma the voice does honour. Hyphens inside words are left alone.
_DASH = re.compile(r"\s*—+\s*|\s+[–-]{1,2}\s+")
# A styled voice is a set of cloned references named <name>-conversational, <name>-expressive and
# <name>-reassuring (plus any extra ones, such as <name>-london, that can be the configured default);
# each reply is spoken in whichever style suits its text.
_STYLED_VOICE = re.compile(r"^(.+)-(conversational|expressive|reassuring|london)$", re.I)
_REASSURING = re.compile(
    r"\b(?:all good|don't worry|no worries|no stress|not to worry|nothing to worry about|i(?:'m| am) here|"
    r"i(?:'ve| have) got you|we(?:'ve| have) got this|take your time|you(?:'re| are) okay|you(?:'re| are) fine|"
    r"you(?:'re| are) alright|we(?:'ll| will) sort it|easy fix|it(?:'s| is) fine|can help|sorry|apologies)\b",
    re.I,
)
# A "!" that ends a sentence or clause, not one inside code such as "!=" or "!important".
_EXCLAIM = re.compile(r"!+(?:\s|$|['\")\]])")


STYLE_LOG = STATE_DIR / "voice.log"  # one line per reply: when, which style, the text it was judged on


def _log_style_choice(voice: str, preview: str) -> None:
    """Append the style picked for a reply so `tail -f ~/.local/state/vision/voice.log` shows what fired."""
    log_voice_line(voice, repr(" ".join(preview.split())[:120]))


def log_voice_line(label: str, detail: str) -> None:
    """One line in voice.log: when, a label, what happened."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with STYLE_LOG.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {label:<22} {detail}\n")
    except OSError:
        pass


def select_reply_voice(default: str, text: str, available: list[str]) -> str:
    """Pick a styled voice's reference for a reply from as much of its text as is known; other voices are untouched."""
    match = _STYLED_VOICE.fullmatch(default.strip())
    if match is None:
        return default
    choices = {voice.lower(): voice for voice in available}
    base = match.group(1)
    if _REASSURING.search(text):
        wanted = f"{base}-reassuring"
    elif _EXCLAIM.search(text):
        wanted = f"{base}-expressive"
    else:
        wanted = f"{base}-conversational"
    return choices.get(wanted.lower(), default)


def speechify(text: str, code: str = "I've put the code on screen.") -> str:
    """Convert markdown-ish text into something that reads well aloud. `code` is said in place of a code
    block: a reply being spoken as it arrives has just put it on screen, one read back later has not."""
    text = _FENCE.sub(f" {code} ", text)
    text = _TABLE_ROW.sub(" ", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _IMAGE.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub("a link", text)
    text = _HEADING.sub("", text)
    text = _EMPH.sub(r"\2", text)
    text = _EMOJI.sub("", text)
    # bullets become sentences
    lines = []
    for line in text.splitlines():
        if _BULLET.match(line):
            line = _BULLET.sub("", line).strip()
            if line and line[-1] not in ".!?:":
                line += "."
        lines.append(line)
    text = "\n".join(lines)
    text = _DASH.sub(", ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


READ_ALOUD_CODE = "Code skipped."  # said for a code block in a reply read back from the phone
READ_ALOUD_MIN_CHARS = 40  # a shorter sentence joins the next one: a piece per "OK." would only add gaps
READ_ALOUD_MAX_CHARS = 300  # a longer run is cut at a clause, so no single piece keeps the phone waiting long


def read_aloud_pieces(text: str) -> list[str]:
    """A finished reply as speakable pieces, in order, for the phone's read-aloud button.

    It fetches the voice's WAV for one piece while the one before plays (POST /speak), so the pieces are
    about a sentence each: the first is ready quickly and no later one takes much longer to make than
    the last takes to say. The same pieces suit the phone's own voice when the computer's is off."""
    spoken = speechify(text, code=READ_ALOUD_CODE)
    sentences, start = [], 0
    for m in _SENTENCE_END.finditer(spoken):
        if _ABBREV.search(spoken[start:m.start()].rstrip()):
            continue  # "e.g." is not a sentence end
        sentences.append(spoken[start:m.start()])
        start = m.end()
    sentences.append(spoken[start:])
    pieces: list[str] = []
    buf = ""
    for sentence in (s.strip() for s in sentences):
        if not sentence:
            continue
        if sentence[-1] not in ".!?:;,…\"')]":
            sentence += "."  # a heading or a line on its own: a stop, or it runs into the next sentence
        while len(sentence) > READ_ALOUD_MAX_CHARS:
            cut = max(sentence.rfind(", ", 0, READ_ALOUD_MAX_CHARS), sentence.rfind("; ", 0, READ_ALOUD_MAX_CHARS))
            if cut < 60:
                cut = sentence.rfind(" ", 0, READ_ALOUD_MAX_CHARS)
            if cut < 60:
                break
            pieces.append(((buf + " ") if buf else "") + sentence[: cut + 1].strip())
            buf, sentence = "", sentence[cut + 1:].strip()
        buf = f"{buf} {sentence}".strip()
        if len(buf) >= READ_ALOUD_MIN_CHARS:
            pieces.append(buf)
            buf = ""
    if buf:
        if pieces and len(pieces[-1]) + len(buf) < READ_ALOUD_MAX_CHARS:
            pieces[-1] = f"{pieces[-1]} {buf}"
        else:
            pieces.append(buf)
    return pieces


# ---------------------------------------------------------------- engine
# Orpheus is a Llama 3B that writes SNAC audio codes as tokens. llama.cpp's server runs the model
# (spawned on first use, or reused if another Vision process already started it), tokens stream back
# over HTTP and the SNAC decoder (ONNX) turns every 7-token frame into 85 ms of audio.
_BOS, _EOT = 128000, 128009  # <|begin_of_text|>, <|eot_id|>
_START_HUMAN, _END_HUMAN = 128259, 128260  # wrap "voice: text"
_END_SPEECH = 128258  # the model says it is done
_CODE_BASE = 128266  # first audio code; code = token - base - (position in frame) * 4096
_FRAME_SAMPLES = 2048  # one 7-token frame of SNAC codes
_WINDOW = 4  # frames decoded together; the middle one comes out clean, the edges get context
_SAMPLING = {"temperature": 0.6, "top_p": 0.9, "repeat_penalty": 1.1}  # Canopy's recommendation
_PRE_BUFFER_S = 0.4  # audio to bank before playback starts, so the ~1.3x realtime generator stays ahead
_LOAD_TIMEOUT_S = 180
# Runs llama-server and takes it down when Vision's process is gone, so a crash never leaves a 3B model
# squatting in VRAM. (PR_SET_PDEATHSIG would not do: it tracks the spawning *thread*, and Vision loads
# the voice from short-lived worker threads.)
_WATCHDOG = """
vision=$1; shift
"$@" & p=$!
while kill -0 $p 2>/dev/null; do
  kill -0 $vision 2>/dev/null || { kill $p; break; }
  sleep 1
done
wait $p
"""


def _http(url: str, body: dict | None = None, timeout: float = 5.0):
    import json
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


class OrpheusEngine:
    continuity = False
    """Orpheus 3B: owns the llama.cpp server and the SNAC decoder."""

    sound_tags = True  # the fine-tune acts <laugh>, <sigh>... written inline

    def __init__(self, cfg: VoiceConfig, stop: threading.Event):
        self.cfg = cfg
        self._stop = stop
        self._proc = None
        self._snac = None
        self._voice = VOICE_PRESETS.get(cfg.voice.strip().lower(), cfg.voice.strip().lower())
        self.device = "?"

    # -- loading
    @property
    def _base(self) -> str:
        return f"http://127.0.0.1:{self.cfg.port}"

    def load(self):
        if not orpheus_present():
            raise TTSError("Orpheus voice files missing. Run `vision setup`.")
        self.set_voice(self.cfg.voice)
        self._load_snac()
        self._start_server()
        # First generation compiles CUDA graphs and fills the caches; do it now, not on the first reply.
        for _ in self._generate("Ready."):
            pass

    def _load_snac(self):
        import onnxruntime as rt

        rt.set_default_logger_severity(3)
        so = rt.SessionOptions()
        so.log_severity_level = 3
        _, snac = model_files()
        providers = ["CPUExecutionProvider"]
        if self.cfg.device in ("auto", "cuda") and "CUDAExecutionProvider" in rt.get_available_providers():
            try:
                from vision.cuda import preload

                preload()
                providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
            except Exception:  # noqa: BLE001
                if self.cfg.device == "cuda":
                    raise
        self._snac = rt.InferenceSession(str(snac), so, providers=providers)
        self._snac_inputs = [i.name for i in self._snac.get_inputs()]

    def _server_alive(self) -> bool:
        try:
            with _http(f"{self._base}/health", timeout=1.0) as r:
                return b'"ok"' in r.read()
        except Exception:  # noqa: BLE001
            return False

    def _start_server(self):
        """Spawn llama-server on the voice model, or adopt one another Vision process already runs."""
        import subprocess

        gguf, _ = model_files()
        log = STATE_DIR / "llama-server.log"
        if self._server_alive():
            with _http(f"{self._base}/props") as r:
                props = r.read().decode()
            if gguf.name in props:
                self.device = self._device_from_log(log)
                return
            raise TTSError(f"Port {self.cfg.port} is taken by another llama-server. Change [voice] port.")
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        want = self.cfg.device or "auto"
        for ngl in ([99, 0] if want == "auto" else [99] if want == "cuda" else [0]):
            cmd = [
                "sh", "-c", _WATCHDOG, "vision-voice", str(os.getpid()), str(llama_server_bin()),
                "-m", str(gguf), "-ngl", str(ngl), "-c", "4096", "-np", "1", "-fa", "on",
                "--host", "127.0.0.1", "--port", str(self.cfg.port), "--no-webui", "-lv", "4",  # 4 logs the GPU offload
            ]
            with open(log, "w", encoding="utf-8") as f:
                self._proc = subprocess.Popen(
                    cmd, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True
                )
            deadline = time.time() + _LOAD_TIMEOUT_S
            while time.time() < deadline and self._proc.poll() is None:
                if self._server_alive():
                    self.device = self._device_from_log(log)
                    if self.device == "cuda" or ngl == 0 or want != "cuda":
                        return
                    break  # asked for cuda, got cpu
                time.sleep(0.2)
            self._kill_server()
        raise TTSError(f"llama-server failed to start (see {log}).")

    @staticmethod
    def _device_from_log(log: Path) -> str:
        try:
            m = re.search(r"offloaded (\d+)/\d+ layers to GPU", log.read_text(errors="replace", encoding="utf-8"))
            return "cuda" if m and int(m.group(1)) > 0 else "cpu"
        except OSError:
            return "?"

    def _kill_server(self):
        if self._proc is not None and self._proc.poll() is None:
            os.killpg(self._proc.pid, signal.SIGTERM)  # the watchdog and the server share a group
            try:
                self._proc.wait(5)
            except Exception:  # noqa: BLE001
                os.killpg(self._proc.pid, signal.SIGKILL)
        self._proc = None

    def close(self):
        """Stop the voice server if this process started it."""
        self._kill_server()

    # -- voices
    @property
    def voice(self) -> str:
        return self._voice

    def available_voices(self) -> list[str]:
        return list(VOICES)

    def set_voice(self, spec: str) -> None:
        """spec: a preset name or one of the Orpheus voices."""
        name = VOICE_PRESETS.get(spec.strip().lower(), spec.strip().lower())
        if name not in VOICES:
            raise TTSError(f"Unknown voice {name!r}. Run `vision voices`.")
        self._voice = name

    # -- synthesis
    def _generate(self, text: str):
        """Yield float32 audio pieces for one chunk of text as the model produces them."""
        body = {
            "prompt": [_START_HUMAN, _BOS, f"{self._voice}: {text}", _EOT, _END_HUMAN],
            "n_predict": 2048, "stream": True, "return_tokens": True, "cache_prompt": True,
            "stop": ["<custom_token_2>"], **_SAMPLING,
        }
        import json

        codes: list[int] = []  # codes of the frame being filled
        frames: list[list[int]] = []
        bank: list[np.ndarray] = []
        banked = 0
        started = False
        try:
            with _http(f"{self._base}/completion", body, timeout=60) as r:
                for line in r:
                    if self._stop.is_set():
                        return  # closing the connection cancels generation server-side
                    if not line.startswith(b"data: "):
                        continue
                    ev = json.loads(line[6:])
                    for tok in ev.get("tokens", ()):
                        if tok < _CODE_BASE:
                            continue
                        codes.append(tok - _CODE_BASE - len(codes) * 4096)
                        if len(codes) < 7:
                            continue
                        frames.append(codes)
                        codes = []
                        piece = self._decode_window(frames)
                        if piece is None:
                            continue
                        if started:
                            yield piece
                            continue
                        bank.append(piece)
                        banked += piece.size
                        if banked >= SAMPLE_RATE * _PRE_BUFFER_S:
                            started = True
                            yield np.concatenate(bank)
                            bank = []
                    if ev.get("stop"):
                        break
        except OSError as e:
            raise TTSError(f"voice server: {e}") from e
        if len(frames) >= _WINDOW:  # the last frames never sat in the middle of a window
            tail = self._snac_decode(frames[-_WINDOW:])
            if tail is not None:
                bank.append(tail[_FRAME_SAMPLES * 2 :])
        elif frames:
            whole = self._snac_decode(frames)
            if whole is not None:
                bank.append(whole)
        if bank:
            yield np.concatenate(bank)

    def _decode_window(self, frames: list[list[int]]) -> np.ndarray | None:
        """Audio for the newest frame that has a frame of context on each side."""
        if len(frames) < _WINDOW:
            return None
        audio = self._snac_decode(frames[-_WINDOW:])
        if audio is None:
            return None
        if len(frames) == _WINDOW:  # first window: the opening frame comes along too
            return audio[: _FRAME_SAMPLES * 2]
        return audio[_FRAME_SAMPLES : _FRAME_SAMPLES * 2]

    def _snac_decode(self, frames: list[list[int]]) -> np.ndarray | None:
        # SNAC's three codebooks run at 12, 23 and 47 Hz; Orpheus interleaves them per frame as
        # [c0, c1, c2, c2, c1, c2, c2].
        c0 = np.array([[f[0] for f in frames]], dtype=np.int64)
        c1 = np.array([[x for f in frames for x in (f[1], f[4])]], dtype=np.int64)
        c2 = np.array([[x for f in frames for x in (f[2], f[3], f[5], f[6])]], dtype=np.int64)
        if any(((c < 0) | (c >= 4096)).any() for c in (c0, c1, c2)):
            return None  # the model slipped out of the codebook; drop the frame rather than screech
        out = self._snac.run(None, dict(zip(self._snac_inputs, (c0, c1, c2))))[0]
        return out[0, 0].astype(np.float32)

    def generate(self, text: str):
        """Yield float32 audio pieces for `text` as they are generated (first piece after ~0.5 s)."""
        if not self._server_alive():  # an adopted server went away with its owner: bring up our own
            self._kill_server()
            self._start_server()
        yield from self._generate(text)


# ---------------------------------------------------------------- Qwen3-TTS engine
class _Cancelled(Exception):
    pass


def _gpu_failure(e: Exception) -> str:
    """One line on why a CUDA load failed, naming the other GPU users when it was out of memory."""
    import torch

    if isinstance(e, torch.OutOfMemoryError) or "out of memory" in str(e).lower():
        from vision.cuda import gpu_holders

        free, total = torch.cuda.mem_get_info()
        msg = f"CUDA out of memory ({free / 2**30:.1f} of {total / 2**30:.1f} GB free)"
        holders = gpu_holders()
        if holders:
            msg += "; holding it: " + ", ".join(holders[:4]) + ". Close them and run /speak again."
        return msg
    return f"{type(e).__name__}: {e}"


@contextlib.contextmanager
def _quiet():
    """Send stdout and stderr (file descriptors, so subprocesses too) to /dev/null for the block.

    The library's unused 25 Hz tokenizer prints a flash-attn banner, logs "SoX could not be found" and
    lets a shell say `sox: command not found` while importing and loading; on the chat screen that
    would land in the middle of the transcript."""
    sys.stdout.flush()
    sys.stderr.flush()
    saved = [os.dup(fd) for fd in (1, 2)]
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        for fd in (1, 2):
            os.dup2(devnull, fd)
        yield
    finally:
        sys.stdout.flush()  # whatever was printed meanwhile drains into /dev/null, not the terminal
        sys.stderr.flush()
        for fd, keep in zip((1, 2), saved):
            os.dup2(keep, fd)
            os.close(keep)
        os.close(devnull)


def _import_qwen():
    import logging

    with _quiet():
        from qwen_tts import Qwen3TTSModel
    logging.getLogger("sox").setLevel(logging.ERROR)
    return Qwen3TTSModel


def _sample(logits, do_sample: bool, top_k: int, top_p: float, temperature: float):
    """One token per row, the way transformers' generate() would pick it (temperature → top-k → top-p)."""
    import torch

    if not do_sample:
        return logits.argmax(-1)
    logits = logits.float() / max(float(temperature), 1e-5)
    if top_k and top_k < logits.shape[-1]:
        kth = torch.topk(logits, top_k).values[..., -1:]
        logits = logits.masked_fill(logits < kth, float("-inf"))
    if top_p < 1.0:
        sorted_logits, order = torch.sort(logits, descending=True)
        cum = sorted_logits.softmax(-1).cumsum(-1)
        drop = cum - sorted_logits.softmax(-1) > top_p  # keep the first token that crosses top_p
        logits = logits.masked_fill(drop.scatter(-1, order, drop), float("-inf"))
    return torch.multinomial(logits.softmax(-1), 1)[:, 0]


def _lean_code_predictor(model) -> None:
    """Swap the sub-talker's generate() for a plain sampling loop.

    Every 80 ms frame the talker predicts one code, then hands it to a 175M-parameter "code predictor"
    for the remaining 15; the library runs that through transformers' generate(), whose bookkeeping
    costs more than the model does. The loop below does
    the same prefill + 14 cached steps with the same sampling and nothing else."""
    import types

    import torch
    from transformers import DynamicCache

    cp = model.talker.code_predictor
    groups = model.config.talker_config.num_code_groups
    if not (isinstance(cp.lm_head, torch.nn.ModuleList) and len(cp.lm_head) == groups - 1):
        return  # a layout this shortcut was not written for: keep the library's path

    def generate(self, inputs_embeds=None, max_new_tokens=groups - 1, do_sample=True, top_k=50, top_p=1.0,
                 temperature=0.9, **_ignored):
        cache = DynamicCache()
        out = self(inputs_embeds=inputs_embeds, past_key_values=cache, use_cache=True)
        toks = []
        for i in range(max_new_tokens):
            tok = _sample(out.logits[:, -1, :], do_sample, top_k, top_p, temperature)
            toks.append(tok)
            if i + 1 < max_new_tokens:
                out = self(input_ids=tok[:, None], past_key_values=cache, use_cache=True, generation_steps=out.generation_steps)
        return types.SimpleNamespace(sequences=torch.stack(toks, dim=1))

    cp.generate = types.MethodType(generate, cp)


class Qwen3Engine:
    """Qwen3-TTS 1.7B Base, imitating the reference clip of a saved voice (or any WAV).

    The talker writes one 16-code frame per 80 ms of speech. On CUDA the decode loop in vision/talker.py
    (CUDA graphs) hands each frame over as it is made; on CPU a forward hook lifts it out of the library's
    own loop. Either way the codec decoder (causal, so earlier audio never changes) turns small batches of
    frames into sound with a little left context, the way its own chunked_decode does. The reference
    clip's codes provide the context for the first batch, exactly as the library decodes a clone.

    With continuity on, a chunk that follows another in the same reply is generated as an in-context
    continuation of what was just said: the recent chunks' text and codes go in as the ICL reference (the
    x-vector of the real reference still fixes the timbre), so pitch, energy and pace carry over instead of
    every sentence starting cold, and the codec decoder is seeded with that audio so the join is seamless."""

    sound_tags = False
    continuity = True  # generate(cont=True) is supported
    CARRY_FRAMES = 200  # ~16 s of the reply so far is carried as context (whole chunks, newest first)
    # Left context handed to the codec decoder. Its own chunked_decode uses 25; at 72 (its transformer's
    # sliding window) the streamed audio is indistinguishable from a whole-clip decode (0.2 dB mean mel
    # difference, no seams), and with the CUDA-graph talker there is GPU time to spare for it.
    CONTEXT_FRAMES = 72
    FIRST_FRAMES = 4  # the first pieces: 0.32 s of audio, so sound starts early...
    # ...and the second as well: at ~2x real time a 0.64 s piece takes about as long to make as the
    # 0.32 s before it takes to play, so a second piece that size arrived just as the first ran out.
    SHORT_PIECES = 2
    CHUNK_FRAMES = 8  # later pieces: 0.64 s each, half as many decoder calls
    _ATTN = "sdpa"  # flash-attn would need a compile on this machine; sdpa is a hair slower, same output

    def __init__(self, cfg: VoiceConfig, stop: threading.Event):
        self.cfg = cfg
        self._stop = stop
        self._model = None
        self._voice = ""
        self._ref: tuple[Path, str | None] | None = None
        self._prompt = None  # VoiceClonePromptItem list for the current voice
        self._prompts: dict[tuple[str, str], tuple] = {}  # (voice, clone mode) -> (prompt items, ref codes), built once each
        self._ref_codes = None  # [T, 16] codes of the reference clip (ICL mode) or None
        self._fast = None  # vision.talker.FastTalker on CUDA
        self._talker = None
        self._decoder = None
        self._spoken: list[tuple[str, object]] = []  # (text, [T, 16] codes) of the reply's recent chunks
        self._gen_lock = threading.Lock()  # one generation at a time on the GPU
        self.device = "?"
        self.gpu_error = ""  # why the GPU was not used, when it was not
        self.quant = "none"  # "int8" once the talker weights have been shrunk (CUDA only)
        self._claim = GpuClaim(STATE_DIR / "voice.gpu.lock")  # one Vision at a time keeps the voice on the GPU
        try:
            self.set_voice(cfg.voice)
        except TTSError:
            self._voice = cfg.voice  # not made yet; load() reports it properly

    # -- loading
    def load(self):
        self.gpu_error = ""
        if not qwen_present():
            raise TTSError("Qwen3-TTS model missing. Run `vision setup`.")
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")  # less VRAM lost to fragmentation
        import torch
        import transformers

        Qwen3TTSModel = _import_qwen()
        transformers.logging.set_verbosity_error()  # "Setting pad_token_id..." on every sentence otherwise
        want = self.cfg.device or "auto"
        attempts = []
        if want in ("auto", "cuda") and torch.cuda.is_available():
            attempts.append("cuda")
        if want in ("auto", "cpu"):
            attempts.append("cpu")
        if not attempts:
            raise TTSError("CUDA is not available to PyTorch; set [voice] device = \"auto\" or \"cpu\".")
        if attempts[0] == "cuda":
            try:
                self._claim.acquire()
            except GpuBusy as e:
                self.gpu_error = str(e)
                raise TTSError(_held(e)) from None
        last = None
        quant = (self.cfg.quant or "none").lower()
        for dev in attempts:
            try:
                with _quiet():
                    if dev == "cuda" and quant == "int8":
                        # Load on the CPU, shrink the transformer weights there, then move: the GPU never
                        # holds the 3.6 GB bf16 model (peak 2.2 GB instead of 4.4) and it takes the same ~6 s.
                        from vision.int8 import move_stray_tensors, quantize_talker

                        self._model = Qwen3TTSModel.from_pretrained(
                            QWEN_TTS_BASE, device_map="cpu", dtype=torch.bfloat16,
                            attn_implementation=self._ATTN, local_files_only=True,
                        )
                        quantize_talker(self._model.model)
                        dev0 = torch.device("cuda:0")
                        core = self._model.model
                        core.to(dev0)
                        # The codec is a plain wrapper object, not a submodule, so `.to()` skipped it;
                        # both wrappers also cache their device at construction.
                        core.speech_tokenizer.model.to(dev0)
                        move_stray_tensors(core, dev0)
                        move_stray_tensors(core.speech_tokenizer.model, dev0)
                        core.speech_tokenizer.device = dev0
                        self._model.device = dev0
                    else:
                        self._model = Qwen3TTSModel.from_pretrained(
                            QWEN_TTS_BASE,
                            device_map="cuda:0" if dev == "cuda" else "cpu",
                            dtype=torch.bfloat16 if dev == "cuda" else torch.float32,
                            attn_implementation=self._ATTN,
                            local_files_only=True,
                        )
                self.device = dev
                self.quant = quant if dev == "cuda" else "none"
                break
            except Exception as e:  # noqa: BLE001
                last = e
                self._model = None
                if dev == "cuda":
                    torch.cuda.empty_cache()  # let go of whatever the half-load reserved
                    self._claim.release()
                    self.gpu_error = _gpu_failure(e)
                    if want == "cuda":
                        raise TTSError(f"voice will not run on the GPU: {self.gpu_error}") from e
        if self._model is None:
            raise TTSError(f"Qwen3-TTS failed to load: {last}")
        core = self._model.model
        if self.device == "cuda":
            from vision.talker import install

            self._fast = install(core)
        else:
            _lean_code_predictor(core)
        self._talker = core.talker
        self._eos = core.config.talker_config.codec_eos_token_id
        self._decoder = core.speech_tokenizer.model.decoder
        # The library decodes the whole clip (reference included) once generation ends; we have already
        # streamed every frame, and on a long sentence that decode spikes ~700 MB of VRAM. Skip it.
        core.speech_tokenizer.decode = lambda *_a, **_k: ([], SAMPLE_RATE)
        self._upsample = core.speech_tokenizer.get_decode_upsample_rate()
        if core.speech_tokenizer.get_output_sample_rate() != SAMPLE_RATE:
            raise TTSError("Qwen3-TTS codec is not 24 kHz; this build of Vision expects 24 kHz.")
        if self._ref is None:
            self.set_voice(self.cfg.voice)  # raises the helpful "no such voice" error
        self._build_prompt()
        # First generation warms the kernels and caches; do it now, not on the first reply.
        for _ in self.generate("Ready."):
            pass

    def close(self):
        self._model = None
        self._prompt = self._ref_codes = None
        self._prompts.clear()
        self._spoken.clear()
        self._fast = None
        # These are borrowed submodules of `_model`. Keeping either one here keeps the CUDA weights
        # alive even after `_model` is dropped, so another Vision can take the claim but still find
        # most of the VRAM occupied.
        self._talker = self._decoder = None
        import gc

        gc.collect()
        try:
            import torch

            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
        self._claim.release()

    # -- voices
    @property
    def voice(self) -> str:
        return self._voice

    def available_voices(self) -> list[str]:
        return saved_voices()

    @staticmethod
    def resolve(spec: str) -> tuple[str, Path, str | None]:
        """(name, ref.wav, transcript or None) for a saved voice name or a path to a clip."""
        spec = spec.strip()
        d = voice_dir(spec)
        if (d / "ref.wav").is_file():
            txt = d / "ref.txt"
            return spec, d / "ref.wav", (txt.read_text(encoding="utf-8").strip() or None) if txt.is_file() else None
        p = Path(spec).expanduser()
        if p.is_file():
            txt = p.with_suffix(".txt")
            return p.stem, p, (txt.read_text(encoding="utf-8").strip() or None) if txt.is_file() else None
        have = ", ".join(saved_voices()) or "none yet"
        raise TTSError(f"No voice {spec!r} (have: {have}). Make one: `vision voice design {spec}` or `vision voice add {spec} --from clip.wav`.")

    def set_voice(self, spec: str) -> None:
        name, wav, text = self.resolve(spec)
        self._voice, self._ref = name, (wav, text)
        if self._model is not None:
            self._build_prompt()

    def _build_prompt(self):
        wav, text = self._ref
        clone_mode = self.cfg.clone_mode
        mode_file = wav.parent / "clone_mode.txt"
        if mode_file.is_file():
            saved_mode = mode_file.read_text(encoding="utf-8").strip().lower()
            if saved_mode in ("embedding", "context"):
                clone_mode = saved_mode
        embedding_only = text is None or clone_mode != "context"
        key = (self._voice, "embedding" if embedding_only else "context")
        if key not in self._prompts:
            items = self._model.create_voice_clone_prompt(ref_audio=str(wav), ref_text=text, x_vector_only_mode=embedding_only)
            self._prompts[key] = (items, items[0].ref_code)
        self._prompt, self._ref_codes = self._prompts[key]

    def _continuation(self):
        """ICL prompt that continues the reply so far, and the codes that seed the decoder for the join."""
        import torch
        from dataclasses import replace

        texts = " ".join(t for t, _ in self._spoken)
        codes = torch.cat([c for _, c in self._spoken])
        item = replace(self._prompt[0], ref_code=codes, ref_text=texts, x_vector_only_mode=False, icl_mode=True)
        return [item], codes

    def _carry(self, text: str, frames: list) -> None:
        """Remember this chunk for the next one; keep whole recent chunks within CARRY_FRAMES (at least one)."""
        import torch

        self._spoken.append((text, torch.stack(frames).clamp(min=0)))
        keep, total = 0, 0
        for _, c in reversed(self._spoken):
            if keep and total + len(c) > self.CARRY_FRAMES:
                break
            keep, total = keep + 1, total + len(c)
        del self._spoken[:-keep]

    # -- synthesis
    def _clock_hold(self):
        """With int8 weights, keep the GPU clocks up while a sentence is made (see vision.int8.ClockHold);
        the bf16 talker is heavy enough to hold them on its own, and the hold costs it ~5%."""
        if self._fast is None or self.quant != "int8":
            return contextlib.nullcontext()
        from vision.int8 import ClockHold

        return ClockHold()

    def generate(self, text: str, cont: bool = False):
        """Yield float32 24 kHz pieces for `text` while the talker is still speaking.

        `cont`: this chunk continues the previous one (same reply), so speak it as a continuation."""
        frames_q: queue.Queue = queue.Queue()
        stop = self._stop
        if cont and self.cfg.continuity and self._spoken:
            prompt, seed = self._continuation()
        else:
            self._spoken.clear()
            prompt, seed = self._prompt, self._ref_codes

        def on_frame(codes):  # FastTalker: one [16] frame as soon as it is sampled
            frames_q.put(codes)
            if stop.is_set():
                raise _Cancelled

        def tap(_module, _args, output):  # library loop: the frame the step just completed; None on prefill
            codes = output.hidden_states[1]
            if codes is not None:
                on_frame(codes[0])

        def run():
            handle = None if self._fast is not None else self._talker.register_forward_hook(tap)
            try:
                with self._gen_lock, self._clock_hold():
                    if self._fast is not None:
                        self._fast.on_frame = on_frame
                    self._model.generate_voice_clone(
                        text=text, language=self.cfg.language or "Auto", voice_clone_prompt=prompt
                    )
            except _Cancelled:
                pass
            except Exception as e:  # noqa: BLE001
                frames_q.put(e)
            finally:
                if handle is not None:
                    handle.remove()
                if self._fast is not None:
                    self._fast.on_frame = None
                frames_q.put(None)

        threading.Thread(target=run, daemon=True, name="qwen3-tts").start()
        frames: list = []
        sent = 0
        while True:
            item = frames_q.get()
            if item is None:
                break
            if isinstance(item, Exception):
                raise TTSError(f"Qwen3-TTS: {item}") from item
            if int(item[0]) == self._eos:
                continue  # end marker (only shows up when a batch mate finished first); never audio
            frames.append(item)
            step = self.FIRST_FRAMES if sent < self.FIRST_FRAMES * self.SHORT_PIECES else self.CHUNK_FRAMES
            if len(frames) - sent >= step and not stop.is_set():
                yield self._decode(frames, sent, seed)
                sent = len(frames)
        if len(frames) > sent and not stop.is_set():
            yield self._decode(frames, sent, seed)
        if stop.is_set() or not frames:
            self._spoken.clear()  # cut short: the text no longer matches the audio, so start the next cold
        elif self.cfg.continuity:
            self._carry(text, frames)

    def _decode(self, frames: list, sent: int, seed=None) -> np.ndarray:
        """Audio for frames[sent:], decoded behind up to CONTEXT_FRAMES of what came before
        (`seed`: [T, 16] codes of the audio that precedes frame 0, for the first pieces)."""
        import torch

        context = frames[max(0, sent - self.CONTEXT_FRAMES) : sent]
        if len(context) < self.CONTEXT_FRAMES and seed is not None:
            need = self.CONTEXT_FRAMES - len(context)
            context = list(seed[-need:].to(frames[0].device)) + context
        codes = torch.stack(context + frames[sent:]).clamp(min=0)  # [T, 16]
        with torch.inference_mode():
            wav = self._decoder(codes.T.unsqueeze(0))[0, 0]
        start = len(context) * self._upsample
        end = codes.shape[0] * self._upsample
        return wav[start:end].float().cpu().numpy()


def design_voice(description: str, text: str, language: str = "English", device: str = "auto", takes: int = 1):
    """Invent a voice from a description with the VoiceDesign model: yields one 24 kHz float32 clip per take."""
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    if device in ("auto", "mlx") and mlx_available():
        yield from mlx_design_voice(description, text, language, takes)
        return
    import torch
    import transformers

    Qwen3TTSModel = _import_qwen()
    transformers.logging.set_verbosity_error()
    cuda = device in ("auto", "cuda") and torch.cuda.is_available()
    claim = GpuClaim(STATE_DIR / "voice.gpu.lock")
    if cuda:
        try:
            claim.acquire()
        except GpuBusy as e:
            raise TTSError(_held(e)) from None
    with _quiet():
        model = Qwen3TTSModel.from_pretrained(
            QWEN_TTS_DESIGN,
            device_map="cuda:0" if cuda else "cpu",
            dtype=torch.bfloat16 if cuda else torch.float32,
            attn_implementation=Qwen3Engine._ATTN,
            local_files_only=qwen_present(QWEN_TTS_DESIGN),
        )
    if cuda:
        from vision.talker import install

        install(model.model)
    else:
        _lean_code_predictor(model.model)
    try:
        for _ in range(takes):
            wavs, sr = model.generate_voice_design(text=text, instruct=description, language=language)
            audio = np.asarray(wavs[0], dtype=np.float32)
            if sr != SAMPLE_RATE:
                idx = np.arange(0, len(audio), sr / SAMPLE_RATE)
                audio = np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)
            yield audio
    finally:
        del model
        if cuda:
            torch.cuda.empty_cache()
        claim.release()


# ---------------------------------------------------------------- Qwen3-TTS on MLX (Apple Silicon)
MLX_CHOICE = STATE_DIR / "voice.mlx.json"  # what `vision setup` measured on this Mac: {"model", "rtf", "chip"}


def mlx_choice() -> dict:
    """The model `vision setup` picked for this Mac and how fast it ran ({} before the first setup)."""
    import json

    try:
        data = json.loads(MLX_CHOICE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def mlx_model(cfg: VoiceConfig) -> str:
    """The MLX voice model to run: the config's, else the one setup timed and picked, else the best tier."""
    return cfg.mlx_model.strip() or mlx_choice().get("model") or MLX_TTS_TIERS[0]


def mlx_present(repo: str) -> bool:
    """True when the Hugging Face cache holds the MLX model and its codec (no network involved)."""
    from huggingface_hub import try_to_load_from_cache

    return all(isinstance(try_to_load_from_cache(repo, f), str) for f in ("model.safetensors", "speech_tokenizer/model.safetensors"))


def _mlx_path(repo: str) -> str:
    """The cached snapshot of `repo`, so loading never reaches for the network mid-conversation. (Found
    through a file in it: mlx-audio downloads only the model's files, which local_files_only snapshots
    reject as incomplete.)"""
    from huggingface_hub import try_to_load_from_cache

    config = try_to_load_from_cache(repo, "config.json")
    if not isinstance(config, str):
        raise TTSError(f"MLX voice model missing ({repo}). Run `vision setup`.")
    return str(Path(config).parent)


def _mx():
    """mlx.core, imported when first needed (tests hand in a numpy stand-in)."""
    import mlx.core

    return mlx.core


class _HookedDecoder:
    """Stands in for mlx-audio's codec decoder for one chunk: its own streaming_step and
    reset_streaming_state, everything else from the real one. (The real one is an mx.compile wrapper,
    which takes no attributes, so its methods cannot be swapped in place.)"""

    def __init__(self, inner, streaming_step, reset_streaming_state):
        self._inner = inner
        self.streaming_step = streaming_step
        self.reset_streaming_state = reset_streaming_state

    def __call__(self, *args, **kwargs):
        return self._inner(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class Qwen3MlxEngine:
    """Qwen3-TTS through MLX (mlx-audio) on Apple Silicon: the same clones as Qwen3Engine, on the Mac's GPU.

    PyTorch runs this model slower than real time on both CPU and GPU (MPS), because each 80 ms frame is
    ~16 small sequential model calls; MLX's lower per-call cost makes it suitable for real-time speech.
    mlx-audio streams with its own stateful codec decoder, so pieces join
    without seams.

    Continuity works as in Qwen3Engine: a chunk that follows another in the same reply is generated as an
    in-context continuation of the reply so far (its text and codes are the ICL reference; the real clip
    still gives the x-vector, so the timbre stays), and the codec decoder is primed with the end of that
    audio so the join is seamless. mlx-audio has no parameter for either, so both go through its seams:
    the reply so far is placed in its ICL cache under the key it looks up (reference text + clip
    fingerprint), and its codec decoder's streaming_step / reset_streaming_state are wrapped for the
    chunk to record the codes as they are decoded and to prime a fresh decoder."""

    sound_tags = False
    continuity = True  # generate(cont=True) is supported
    CARRY_FRAMES = Qwen3Engine.CARRY_FRAMES  # ~16 s of the reply so far is carried as context
    CONTEXT_FRAMES = Qwen3Engine.CONTEXT_FRAMES  # decoder left context at a join
    STREAM_S = 0.32  # piece length: 4 frames, the first sound as early as Qwen3Engine's

    def __init__(self, cfg: VoiceConfig, stop: threading.Event):
        self.cfg = cfg
        self._stop = stop
        self._model = None
        self._worker: _MlxWorker | None = None
        self._voice = ""
        self._ref: tuple[Path, str | None] | None = None
        self._clips: dict[Path, object] = {}  # reference clip → its mx.array, loaded once
        self._spoken: list[tuple[str, object]] = []  # (text, [1, 16, T] mx codes) of the reply's recent chunks
        self.repo = mlx_model(cfg)
        self.device = "?"
        self.gpu_error = ""
        self.quant = "none"
        try:
            self.set_voice(cfg.voice)
        except TTSError:
            self._voice = cfg.voice  # not made yet; load() reports it properly

    resolve = staticmethod(Qwen3Engine.resolve)

    def load(self):
        if not mlx_present(self.repo):
            raise TTSError(f"MLX voice model missing ({self.repo}). Run `vision setup`.")
        if self._ref is None:
            self.set_voice(self.cfg.voice)  # raises the helpful "no such voice" error
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")  # "PyTorch was not found": the Mac build has none
        self._worker = self._worker or _MlxWorker("qwen3-mlx")

        def load():
            import transformers
            from mlx_audio.tts.utils import load_model

            from vision.stt import thread_only_tqdm

            thread_only_tqdm()  # mlx-audio makes a (hidden) tqdm bar per sentence, which would build tqdm's semaphore
            transformers.logging.set_verbosity_error()
            with _quiet():  # "Loaded speech tokenizer…" and a transformers type warning on every load
                return load_model(_mlx_path(self.repo))

        self._model = self._worker.call(load)
        if self._model.sample_rate != SAMPLE_RATE:
            raise TTSError("MLX Qwen3-TTS codec is not 24 kHz; this build of Vision expects 24 kHz.")
        self.device = "mlx"
        bits = re.search(r"-(\d+bit|bf16)$", self.repo)
        self.quant = bits.group(1) if bits and bits.group(1) != "bf16" else "none"
        # First generation compiles the kernels and encodes the reference; do it now, not on the first reply.
        for _ in self.generate("Ready."):
            pass

    def close(self):
        if self._worker is not None and self._model is not None:
            def free():
                import mlx.core as mx

                self._model = None
                self._clips.clear()
                mx.clear_cache()

            self._worker.call(free)
        self._model = None

    # -- voices
    @property
    def voice(self) -> str:
        return self._voice

    def available_voices(self) -> list[str]:
        return saved_voices()

    def set_voice(self, spec: str) -> None:
        name, wav, text = self.resolve(spec)
        self._voice, self._ref = name, (wav, text)

    def _ref_text(self) -> str | None:
        """The transcript for an in-context clone, or None for the timbre-only (x-vector) clone; the same rule
        as Qwen3Engine: [voice] clone_mode, unless the voice's own clone_mode.txt says otherwise."""
        wav, text = self._ref
        mode = self.cfg.clone_mode
        mode_file = wav.parent / "clone_mode.txt"
        if mode_file.is_file():
            saved = mode_file.read_text(encoding="utf-8").strip().lower()
            if saved in ("embedding", "context"):
                mode = saved
        return text if text and mode == "context" else None

    def generate(self, text: str, cont: bool = False):
        """Yield float32 24 kHz pieces for `text` while the model is still speaking.

        `cont`: this chunk continues the previous one (same reply), so speak it as a continuation."""
        if self._model is None or self._worker is None:
            raise TTSError("voice is not loaded")
        wav, _ = self._ref
        continuing = cont and self.cfg.continuity and bool(self._spoken)
        if not continuing:
            self._spoken.clear()
        language = (self.cfg.language or "Auto").strip().lower()
        cancel = threading.Event()
        made: list = []  # the chunk's codes, [1, 16, n] each, as the decoder receives them
        chunk: list = []  # all of them joined, once the chunk is finished (MLX work: done on the worker)

        def pieces():
            mx = _mx()
            clip = self._clips.get(wav)
            if clip is None:
                from mlx_audio.utils import load_audio

                clip = self._clips[wav] = load_audio(str(wav), sample_rate=SAMPLE_RATE)
            model = self._model
            tokenizer = model.speech_tokenizer
            decoder = tokenizer.decoder  # mlx-audio's mx.compile wrapper, which forwards attribute reads
            step, reset = decoder.streaming_step, decoder.reset_streaming_state
            ref_text, key, seed = self._ref_text(), None, None
            if continuing:
                ref_text = " ".join(t for t, _ in self._spoken)
                codes = mx.concatenate([c for _, c in self._spoken], axis=2)
                key = (ref_text, (clip.size, float(clip.sum())))  # what _prepare_icl_generation_inputs looks up
                model._icl_cache[key] = (codes, None)  # None: it tokenizes ref_text itself
                seed = codes[:, :, -self.CONTEXT_FRAMES :]

            def recording_step(c):
                made.append(c)
                return step(c)

            def priming_reset():
                reset()
                if seed is not None and not made:  # the chunk's opening reset: pick up where the last one ended
                    step(seed)

            tokenizer.decoder = _HookedDecoder(decoder, recording_step, priming_reset)
            try:
                for result in model.generate(
                    text=text, ref_audio=clip, ref_text=ref_text, lang_code=language,
                    stream=True, streaming_interval=self.STREAM_S,
                ):
                    if self._stop.is_set() or cancel.is_set():
                        return
                    yield np.asarray(result.audio, dtype=np.float32)
                if made:
                    chunk.append(mx.concatenate(made, axis=2) if len(made) > 1 else made[0])
                    mx.eval(chunk[0])
            finally:
                tokenizer.decoder = decoder
                if key is not None:
                    model._icl_cache.pop(key, None)

        for piece in self._worker.stream(pieces, cancel):
            if self._stop.is_set():  # pieces made before the stop may still be queued: drop them
                break
            yield piece
        if self._stop.is_set() or not chunk:
            self._spoken.clear()  # cut short: the text no longer matches the audio, so start the next cold
        elif self.cfg.continuity:
            self._carry(text, chunk[0])

    def _carry(self, text: str, codes) -> None:
        """Remember this chunk ([1, 16, T] codes) for the next one; keep whole recent chunks within
        CARRY_FRAMES (at least one)."""
        self._spoken.append((text, codes))
        keep, total = 0, 0
        for _, c in reversed(self._spoken):
            if keep and total + c.shape[2] > self.CARRY_FRAMES:
                break
            keep, total = keep + 1, total + c.shape[2]
        del self._spoken[:-keep]


MLX_TARGET_RTF = 0.75  # seconds of work per second of speech a tier must beat: headroom for a busy Mac
MLX_TIMING_LINES = (
    "Good evening. Everything is set up and running normally.",
    "I'll keep an eye on the schedule and let you know if anything changes.",
)


def mac_chip() -> str:
    """The Mac's chip, so a timing from another Mac is not taken for this one."""
    import subprocess

    try:
        return subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True,
                              timeout=5, encoding="utf-8").stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def time_mlx(cfg: VoiceConfig, repo: str) -> float:
    """Seconds `repo` takes per second of speech on this Mac, speaking in cfg's voice (after warm-up)."""
    import dataclasses

    engine = Qwen3MlxEngine(dataclasses.replace(cfg, mlx_model=repo), threading.Event())
    engine.load()
    try:
        work = speech = 0.0
        # The second line continues the first, as a reply's sentences do: the context costs time.
        for i, line in enumerate(MLX_TIMING_LINES):
            t = time.monotonic()
            speech += sum(p.size for p in engine.generate(line, cont=i > 0)) / SAMPLE_RATE
            work += time.monotonic() - t
        return work / speech if speech else float("inf")
    finally:
        engine.close()


def save_mlx_choice(repo: str, rtf: float) -> None:
    import json

    MLX_CHOICE.parent.mkdir(parents=True, exist_ok=True)
    MLX_CHOICE.write_text(json.dumps({"model": repo, "rtf": round(rtf, 3), "chip": mac_chip()}) + "\n", encoding="utf-8")


def mlx_design_voice(description: str, text: str, language: str = "English", takes: int = 1):
    """design_voice through MLX: one 24 kHz float32 clip per take from the VoiceDesign model."""
    os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")  # "PyTorch was not found": the Mac build has none
    import transformers
    from mlx_audio.tts.utils import load_model

    transformers.logging.set_verbosity_error()
    path = mlx_fetch(MLX_TTS_DESIGN)  # also gives tqdm its threading lock (see thread_only_tqdm)
    worker = _MlxWorker("qwen3-mlx-design")
    with _quiet():
        model = worker.call(lambda: load_model(path))
    lang = (language or "Auto").strip().lower()
    def take():
        return [np.asarray(r.audio, dtype=np.float32) for r in model.generate(text=text, instruct=description, lang_code=lang)]

    def free():
        import mlx.core as mx

        mx.clear_cache()

    try:
        for _ in range(takes):
            parts = worker.call(take)
            yield np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
    finally:
        worker.call(free)


# ---------------------------------------------------------------- Speaker
class Speaker:
    """Lazy-loading voice: picks the engine from the config and owns playback."""

    def __init__(self, cfg: VoiceConfig):
        self.cfg = cfg
        self._loaded = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._playing = threading.Event()
        self._load_error: tuple[Exception, float] | None = None
        self._out_device = resolve_device(cfg.output_device, "output")
        self._filler_cache: dict[str, np.ndarray] = {}  # cache key → clip (see prepare_fillers)
        self.last_filler = ""  # the phrase said last, so the next turn picks another
        self.engine = _engine(cfg, self._stop)

    # -- loading
    RETRY_S = 20.0  # after a failed load, report the same error this long instead of loading again

    def _load(self):
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            if self._load_error and time.monotonic() - self._load_error[1] < self.RETRY_S:
                raise self._load_error[0]
            try:
                self.engine.load()
            except Exception as e:
                self._load_error = (e, time.monotonic())
                raise
            self._load_error = None
            self._loaded = True

    @property
    def device(self) -> str:
        return self.engine.device

    @property
    def device_note(self) -> str:
        """"cuda", or "cpu — <why the GPU was not used>"."""
        why = getattr(self.engine, "gpu_error", "")
        if why and self.device != "cuda":
            return f"{self.device} — {why}"
        quant = getattr(self.engine, "quant", "none")
        return f"{self.device} {quant}" if quant != "none" else self.device

    def close(self):
        # A talk session can be switched off while its warm-up thread is still loading. Serialise
        # shutdown with that load, then forget a cached GpuBusy error so the next /talk retries at
        # once after the other Vision has released its claim.
        with self._lock:
            try:
                self.engine.close()
            finally:
                self._loaded = False
                self._load_error = None
                self._stop.clear()

    # -- voices
    @property
    def voice(self) -> str:
        return self.engine.voice

    def available_voices(self) -> list[str]:
        return self.engine.available_voices()

    def set_voice(self, spec: str) -> None:
        self.engine.set_voice(spec)

    def _voice_rate(self) -> float:
        rate = self.cfg.rate
        if isinstance(self.engine, (Qwen3Engine, Qwen3MlxEngine)):
            rate_file = voice_dir(self.engine.voice) / "rate.txt"
            if rate_file.is_file():
                try:
                    saved_rate = float(rate_file.read_text(encoding="utf-8").strip())
                    if 0.5 <= saved_rate <= 2.0:
                        rate = saved_rate
                except ValueError:
                    pass
        return rate

    # -- synthesis
    def synth_stream(self, text: str, cont: bool = False):
        """Yield audio pieces for `text` as they are generated, at the configured rate.

        `cont`: `text` follows what was just spoken in the same reply (engines that can, continue it)."""
        self._load()
        if not self.engine.sound_tags:
            text = _SOUND_TAG.sub(" ", text)
        text = text.strip()
        if not text:
            return
        gen = self.engine.generate(text, cont=cont) if self.engine.continuity else self.engine.generate(text)
        rate = self._voice_rate()
        if abs(rate - 1.0) < 0.01:
            yield from gen
            return
        stretch = TimeStretch(rate)
        for piece in gen:
            out = stretch.feed(piece)
            if out.size:
                yield out
        out = stretch.feed(np.zeros(0, dtype=np.float32), final=True)
        if out.size:
            yield out

    def synth(self, text: str) -> np.ndarray:
        pieces = list(self.synth_stream(text))
        if not pieces:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(pieces)

    def save(self, text: str, path: str | Path) -> Path:
        import soundfile as sf

        audio = self.synth(speechify(text))
        sf.write(str(path), audio, SAMPLE_RATE)
        return Path(path)

    # -- fillers: "Hmm." while a spoken reply is still on its way (StreamingSpeaker.arm_filler)
    FILLER_DIR = STATE_DIR / "fillers"

    def _filler_voice(self) -> str:
        """The voice the fillers are in. A styled voice changes style per chunk, so the engine may be
        left on `<name>-expressive` by the last reply; the fillers stay in the configured style."""
        return self.cfg.voice.strip() if _STYLED_VOICE.fullmatch(self.cfg.voice.strip()) else self.voice

    def _filler_key(self, phrase: str) -> str:
        parts = (self.cfg.engine, self._filler_voice(), self.cfg.language, self.cfg.clone_mode, f"{self._voice_rate():.3f}", phrase)
        return hashlib.sha1("|".join(parts).encode()).hexdigest()

    def prepare_fillers(self, phrases: list[str]) -> list[tuple[str, np.ndarray]]:
        """The clips for `phrases` in the current voice: read from the cache on disk, or made now (the
        voice must be loaded and idle: warm-up, or a /voice change). A phrase that fails to synthesise
        is left out. Returns what is ready, and keeps it for `fillers()`."""
        clips: list[tuple[str, np.ndarray]] = []
        wanted = self._filler_voice()
        for phrase in phrases:
            key = self._filler_key(phrase)
            hit = self._filler_cache.get(key)
            if hit is None:
                path = self.FILLER_DIR / f"{key}.npy"
                try:
                    hit = np.load(path).astype(np.float32, copy=False) if path.is_file() else None
                except Exception:  # noqa: BLE001  a damaged cache file: make the clip again
                    hit = None
                if hit is None:
                    try:
                        if self.voice != wanted:
                            self.set_voice(wanted)  # the clip must be in the voice it is filed under
                        hit = self.synth(speechify(phrase))
                    except Exception as e:  # noqa: BLE001
                        print(f"[filler not made: {e}]", file=sys.stderr)
                        continue
                    if not hit.size:
                        continue
                    try:
                        self.FILLER_DIR.mkdir(parents=True, exist_ok=True)
                        np.save(path, hit)
                    except OSError:
                        pass
                self._filler_cache[key] = hit
            clips.append((phrase, hit))
        return clips

    def fillers(self, phrases: list[str]) -> list[tuple[str, np.ndarray]]:
        """The clips already made for `phrases` in the current voice; nothing is synthesised here (a
        reply may be in progress), so a phrase not prepared yet is left out."""
        out = []
        for phrase in phrases:
            clip = self._filler_cache.get(self._filler_key(phrase))
            if clip is not None:
                out.append((phrase, clip))
        return out

    # -- playback
    def stop(self) -> None:
        self._stop.set()

    def is_playing(self) -> bool:
        return self._playing.is_set()

    # Audio the device keeps in hand. PortAudio's default here is ~35 ms, so any moment the process
    # cannot get back to the stream in time (the reply's markdown re-rendering, a burst of kernel
    # launches on the talker thread holding the GIL) plays as a click or a hitch. 200 ms rides those
    # out; stop() aborts the stream, so it costs nothing in responsiveness, and StreamingSpeaker
    # takes the reported latency off its estimate of what has been heard so the text does not run ahead.
    OUTPUT_LATENCY_S = 0.2

    def open_stream(self):
        # At the device's own rate, resampled here: PortAudio's own conversion put static on AirPods.
        from vision.devrate import OutputStream

        return OutputStream(self._out_device, SAMPLE_RATE, latency=self.OUTPUT_LATENCY_S)

    def play(self, audio: np.ndarray, stream=None) -> None:
        """Blocking playback that honours stop(). Pass an open stream to avoid per-chunk gaps."""
        if audio.size == 0:
            return
        self._playing.set()
        try:
            own = stream is None
            if own:
                stream = self.open_stream()
                stream.start()
            step = 2400  # 100 ms
            for i in range(0, len(audio), step):
                if self._stop.is_set():
                    stream.abort()
                    break
                stream.write(audio[i : i + step].reshape(-1, 1))
            if own:
                self.close_stream(stream)
        finally:
            self._playing.clear()

    def close_stream(self, stream) -> None:
        """Let the device play out what it still holds, then stop and close the stream.

        Stopping straight after the last write drops whatever is left in the device's buffer: with
        OUTPUT_LATENCY_S of it, that is the end of the last word. Unless the reply was cut short, push
        that much silence through first, so the real audio has left the buffer before the stream stops."""
        try:
            if not self._stop.is_set():
                try:
                    latency = max(0.0, float(stream.latency))
                except Exception:  # noqa: BLE001
                    latency = self.OUTPUT_LATENCY_S
                tail = np.zeros((int(SAMPLE_RATE * (latency + 0.02)), 1), dtype=np.float32)
                stream.write(tail)
            stream.stop()
            stream.close()
        except Exception:  # noqa: BLE001
            pass

    def say(self, text: str) -> None:
        """Speak a full text now: chunk by sentence so playback starts quickly."""
        self._stop.clear()
        s = StreamingSpeaker(self)
        s.feed(text)
        s.finish()


class TimeStretch:
    """Streaming WSOLA: changes the pace of speech without changing its pitch.

    Output frames of N samples are overlap-added every HOP samples. Each is taken from the input near
    the position the rate dictates, nudged by up to TOL samples to where it best matches (normalised
    cross-correlation) the natural continuation of the previous frame, so the voice's pitch periods stay
    continuous across the join. State carries across feed() calls, so the pieces of one utterance join
    without seams."""

    N = 1200  # 50 ms analysis frame at 24 kHz
    HOP = 600  # synthesis hop (50 % overlap under a Hann window, which sums to one)
    TOL = 360  # ± search range, 15 ms: more than a pitch period of even a deep voice

    def __init__(self, rate: float):
        self.rate = rate
        self._win = np.hanning(self.N).astype(np.float32)
        self._in = np.zeros(0, dtype=np.float32)
        self._consumed = 0  # input samples dropped from the front of _in
        self._fed = 0  # real input samples so far
        self._k = 0  # output frames produced
        self._emitted = 0
        self._tail = np.zeros(self.N, dtype=np.float32)  # overlap carried into the next frame
        self._prev_next = None  # what followed the last chosen frame: the "natural continuation"

    def _best(self, lo: int, hi: int, nominal: int) -> int:
        """Candidate start in [lo, hi] whose frame best continues the previous one."""
        seg = self._in[lo : hi + self.N]
        corr = np.correlate(seg, self._prev_next, mode="valid")  # one score per candidate start
        energy = np.convolve(seg * seg, np.ones(self.N, dtype=np.float32), mode="valid")
        score = corr / (np.sqrt(energy) + 1e-3)
        if not np.isfinite(score).any() or score.max() <= 0:
            return nominal  # silence or noise: nothing to align to
        return lo + int(score.argmax())

    def feed(self, audio: np.ndarray, final: bool = False) -> np.ndarray:
        """Stretch what can be stretched so far; `final` flushes the rest (pass an empty array or the last piece)."""
        audio = np.asarray(audio, dtype=np.float32)
        self._fed += audio.size
        self._in = np.concatenate([self._in, audio]) if self._in.size else audio
        if final:
            self._in = np.concatenate([self._in, np.zeros(self.N + self.TOL + self.HOP, dtype=np.float32)])
        out = []
        while True:
            nominal = int(round(self._k * self.HOP * self.rate)) - self._consumed
            lo, hi = max(0, nominal - self.TOL), nominal + self.TOL
            if hi + self.N > self._in.size:
                break  # need more input for this frame
            pos = nominal if self._prev_next is None or lo == hi else self._best(lo, hi, nominal)
            frame = self._in[pos : pos + self.N] * self._win
            nxt = self._in[pos + self.HOP : pos + self.HOP + self.N]
            self._prev_next = nxt.copy() if nxt.size == self.N and np.abs(nxt).max() > 1e-4 else None
            mixed = self._tail + frame
            out.append(mixed[: self.HOP])
            self._tail = np.concatenate([mixed[self.HOP :], np.zeros(self.HOP, dtype=np.float32)])
            self._k += 1
            keep = max(0, min(pos, nominal) - self.TOL)
            if keep > 0:  # drop input we can never need again
                self._in = self._in[keep:]
                self._consumed += keep
        res = np.concatenate(out) if out else np.zeros(0, dtype=np.float32)
        if final:  # drop the stretched zero padding: keep exactly the real audio's length over the rate
            res = res[: max(0, int(round(self._fed / self.rate)) - self._emitted)]
        self._emitted += res.size
        return res


class StreamingSpeaker:
    """Feed text deltas in; sentences are synthesised and played in order, overlapped.

    Each chunk is followed by the gap its boundary calls for (a breath after a clause cut, a proper
    pause after a sentence, a longer one between paragraphs; see GAPS) and given short edge fades,
    so consecutive chunks join like continuous speech rather than separate clips. The first chunk
    is one sentence, to start fast; after that a chunk takes every sentence the model has written
    meanwhile (up to MAX_CHUNK_CHARS), so the voice phrases across sentences and times the pauses
    between them itself, and each chunk is spoken as a continuation of the last."""

    # Silence wanted between a chunk's last sound and the next chunk, by the boundary that ended it: what
    # the voice leaves after its last word (measured) is topped up to this. Speech has roughly 0.3-0.6 s
    # between sentences and longer between paragraphs. The model itself stops 40-100 ms after the last
    # word, and with a fixed 0.16 s added on the next sentence began ~0.2 s later, so a reply sounded
    # like lines being fired off rather than landed.
    GAPS = {"clause": 0.12, "sentence": 0.38, "paragraph": 0.65, "end": 0.0}
    SILENCE_DB = -40.0  # relative to the chunk's peak: quieter than this counts as silence
    TAIL_S = 0.6  # audio kept from the end of a chunk to measure its trailing silence
    LEAD_S = 0.1  # a chunk's own opening silence is cut to this; the gap before it was sized by GAPS already
    FADE_S = 0.005
    # Audio is queued in pieces this long (or longer) while a chunk is still being generated.
    MIN_PIECE_S = 0.2
    MAX_CHUNK_CHARS = 320  # ~20 s of speech; keeps stop responsive and the continuation context short
    CPS = 14.0  # starting guess at source chars per second of speech, refined per chunk
    BLEND_S = 2.0  # seconds over which the reveal eases onto a chunk's exact timing once it is known
    # A reply's last sentence has no space after it, so _SENTENCE_END cannot see it end: it used to
    # wait for a tool call or finish(), which comes only once the brain's process has exited (seconds
    # after the text, minutes on a turn held open for background agents). Once the text goes quiet
    # the tail is spoken anyway: soon if it ends like a sentence (the wait keeps "3." + "5" whole),
    # later if it does not (a list item, a code block).
    TAIL_IDLE_S = 0.35
    IDLE_S = 1.5
    # A styled voice picks its style (conversational / expressive / reassuring) per chunk, judged from
    # the whole chunk's text, so a reply can open flat, get excited and settle again. A chunk in a new
    # style starts cold from that style's reference clip (continuity would otherwise carry the old
    # delivery straight through it); chunks that keep the style continue the reply as before. Sentences
    # are never split apart over style: every cold start is a fresh clip that ends like one, so the mood
    # changes only at chunk boundaries, where the reply already pauses.

    def __init__(self, speaker: Speaker, min_chars: int = 12, timing=None):
        self.speaker = speaker
        self.timing = timing
        self.min_chars = min_chars
        self._buf = ""
        # (spoken text, boundary kind, length of the source text it came from) per sentence
        self._text_q: queue.Queue[tuple[str, str, int] | None] = queue.Queue()
        # (audio piece or None for a chunk with nothing to say, source length of its chunk, last piece?)
        self._audio_q: queue.Queue[tuple[np.ndarray | None, int, bool] | None] = queue.Queue(maxsize=12)
        # The reply's timeline, one record per chunk in speaking order, kept by the synth thread:
        # [source chars, seconds of speech made so far, trailing pause seconds, finished?]. `spoken`
        # maps the seconds the sound card has got through onto it (ReplyView / ChatScreen._pace in
        # vision.ui cap their reveal at that, so the text keeps step with the voice).
        self._chunks: list[list] = []
        self._mark: tuple[float, float] = (0.0, 0.0)  # (seconds handed to the device before the piece now playing, when it began)
        self._handed = 0.0  # seconds handed to the device so far, the piece now playing included
        self._latency = 0.0  # seconds the device holds after a write returns (the stream's reported latency)
        self._floor = 0  # source chars given up on (stop) or all said (finish): `spoken` never sits below this
        self._est: tuple[int, float] | None = None  # (chunk index, chars) last read off the pace guess
        self._blend: tuple[int, float, float] | None = None  # (chunk index, chars the guess was off by, since when)
        self._cps = self.CPS
        try:
            self._cps = self.CPS * speaker._voice_rate()  # the rate stretches the audio after synthesis
        except Exception:  # noqa: BLE001
            pass
        self._voices: list[str] | None = None
        self._peeked: tuple[str, str, int] | None = None  # a queued sentence in another style, kept for the next chunk
        self._emitted = False
        self._filler_lock = threading.Lock()  # orders "first sentence queued" against "filler due"
        self._filler_timer: threading.Timer | None = None
        self._closed = False
        self._buf_lock = threading.RLock()  # feed/flush run on the brain's thread, the idle flush on a timer's
        self._idle_timer: threading.Timer | None = None
        self._fed = 0  # feed() calls so far: an idle timer only flushes if nothing came after it
        self._synth_thread = threading.Thread(target=self._synth_loop, daemon=True)
        self._play_thread = threading.Thread(target=self._play_loop, daemon=True)
        self.speaker._stop.clear()
        self._synth_thread.start()
        self._play_thread.start()

    def feed(self, delta: str) -> None:
        with self._buf_lock:
            self._buf += delta
            self._fed += 1
            self._flush(final=False)
            self._cancel_idle()
            if self._buf.strip() and not self._closed:
                done = _TAIL_END.search(self._buf) and not _ABBREV.search(self._buf.rstrip().rstrip("\"')]"))
                self._idle_timer = threading.Timer(self.TAIL_IDLE_S if done else self.IDLE_S, self._idle_flush, args=(self._fed,))
                self._idle_timer.daemon = True
                self._idle_timer.start()

    def _cancel_idle(self) -> None:  # under _buf_lock
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _idle_flush(self, fed: int) -> None:
        with self._buf_lock:
            if fed == self._fed and not self._closed and not self.speaker._stop.is_set():
                self._idle_timer = None
                self._flush(final=True, tail="sentence")

    def flush(self, tail: str = "paragraph") -> None:
        """Speak the buffered tail now. The model's text block ended without a trailing space (it went
        off to use a tool), so its last sentence would otherwise wait for the next block or finish()."""
        with self._buf_lock:
            self._cancel_idle()
            self._flush(final=True, tail=tail)

    def _flush(self, final: bool, tail: str = "end") -> None:
        while True:
            found = None
            for m in _SENTENCE_END.finditer(self._buf):
                head = self._buf[: m.start()].rstrip()
                if _ABBREV.search(head):
                    continue  # "Dr." / "e.g." are not sentence ends
                if len(head) < self.min_chars and not final:
                    continue  # too short to be worth a separate chunk ("OK.") - merge with the next
                found = m
                break
            if found is None:
                # Long run without punctuation: cut at a clause boundary to keep latency low.
                if len(self._buf) > 220:
                    cut = max(self._buf.rfind(", "), self._buf.rfind("; "), self._buf.rfind(" - "))
                    if cut > 60:
                        self._emit(self._buf[: cut + 1], "clause")
                        self._buf = self._buf[cut + 1 :]
                        continue
                break
            kind = "paragraph" if "\n" in found.group() else "sentence"
            self._emit(self._buf[: found.end()], kind)
            self._buf = self._buf[found.end() :]
        if final and self._buf.strip():
            self._emit(self._buf, tail)
            self._buf = ""

    def _emit(self, text: str, kind: str = "sentence") -> None:
        # Text with nothing to say (a bare code fence) still goes through, so its reveal is counted.
        with self._filler_lock:
            first = not self._emitted
            self._emitted = True
            if first:
                self._cancel_filler()
        if first and self.timing:
            self.timing.event("speech_queued")
        self._text_q.put((speechify(text), kind, len(text)))

    # -- fillers
    # A spoken reply's first words can be a while coming (transcription, then the model's own thinking
    # before its first sentence: a second on a good turn, twenty on a hard one). Left as dead air the
    # listener wonders whether they were heard. `arm_filler` starts a clock at the top of the turn; if
    # no sentence has been queued when it runs out, a pre-made clip ("Hmm.") plays through this
    # reply's stream, and the reply follows it after an ordinary sentence gap. A sentence that arrives
    # first cancels the clock. The clip is not in the reply's text, so it is counted as a chunk of zero
    # source characters: the text reveal waits on it, nothing runs ahead.
    def arm_filler(self, clips: list[tuple[str, np.ndarray]], after_s: float) -> None:
        """Say one of `clips` unless the reply starts within `after_s`."""
        if not clips or after_s < 0:
            return
        with self._filler_lock:
            if self._emitted or self._closed:
                return
            self._cancel_filler()
            self._filler_timer = threading.Timer(after_s, self._fire_filler, args=(clips,))
            self._filler_timer.daemon = True
            self._filler_timer.start()

    def _cancel_filler(self) -> None:  # under _filler_lock
        if self._filler_timer is not None:
            self._filler_timer.cancel()
            self._filler_timer = None

    def _fire_filler(self, clips) -> None:
        with self._filler_lock:
            if self._emitted or self._closed or self.speaker._stop.is_set():
                return
            self._filler_timer = None
            choices = [c for c in clips if c[0] != self.speaker.last_filler] or clips
            phrase, clip = random.choice(choices)
            self.speaker.last_filler = phrase
            audio = self._trim_lead(np.array(clip, dtype=np.float32, copy=True))
            peak = float(np.abs(audio).max(initial=0.0))
            pause = max(0.0, self.GAPS["sentence"] - self._trailing_silence(audio, peak))
            self._chunks.append([0, audio.size / SAMPLE_RATE, pause, True])
            self._audio_q.put((self._shape(audio, True, True, pause), 0, True))
            if self.timing:
                self.timing.event("filler")

    def _shape(self, audio: np.ndarray, first: bool, last: bool, pause: float = 0.0) -> np.ndarray:
        """Edge fades against clicks on a chunk's first and last piece, then `pause` seconds of silence."""
        n = min(int(SAMPLE_RATE * self.FADE_S), audio.size // 2)
        if n > 0:
            ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
            if first:
                audio[:n] *= ramp
            if last:
                audio[-n:] *= ramp[::-1]
        if pause <= 0:
            return audio
        return np.concatenate([audio, np.zeros(int(SAMPLE_RATE * pause), dtype=np.float32)])

    @classmethod
    def _trailing_silence(cls, audio: np.ndarray, peak: float) -> float:
        """Seconds of silence (quieter than SILENCE_DB below `peak`) at the end of `audio`."""
        thr = max(peak * 10 ** (cls.SILENCE_DB / 20), 1e-4)
        loud = np.flatnonzero(np.abs(audio) > thr)
        end = int(loud[-1]) + 1 if loud.size else 0
        return (audio.size - end) / SAMPLE_RATE

    def _trim_lead(self, audio: np.ndarray) -> np.ndarray:
        """A chunk's opening silence cut down to LEAD_S: the gap before it was already sized by GAPS."""
        thr = max(float(np.abs(audio).max(initial=0.0)) * 10 ** (self.SILENCE_DB / 20), 1e-4)
        loud = np.flatnonzero(np.abs(audio) > thr)
        keep = int(SAMPLE_RATE * self.LEAD_S)
        return audio[int(loud[0]) - keep :] if loud.size and loud[0] > keep else audio

    def _style_of(self, text: str) -> str:
        """The voice this text would be spoken in: a style for a styled voice, else the voice as set."""
        if self._voices is None:
            self._voices = self.speaker.available_voices()
        return select_reply_voice(self.speaker.cfg.voice, text, self._voices)

    def _next_chunk(self, merge: bool = True) -> tuple[str, str, int, str] | None:
        """The next chunk to speak (text, boundary kind, source length, voice): the queued sentence plus
        (with `merge`) every sentence that arrived behind it in the same style, or None at the end.
        A sentence the style rule reads differently (see select_reply_voice) starts the next chunk,
        so the mood changes exactly where the text does."""
        item, self._peeked = (self._peeked, None) if self._peeked is not None else (self._text_q.get(), None)
        if item is None:
            return None
        text, kind, raw = item
        style = self._style_of(text)
        while merge and len(text) < self.MAX_CHUNK_CHARS:
            try:
                more = self._text_q.get_nowait()
            except queue.Empty:
                break
            if more is None:
                self._text_q.put(None)  # the end marker stays for the next call
                break
            if more[0] and self._style_of(more[0]) != style:
                self._peeked = more  # a change of mood: this sentence opens the next chunk
                break
            text, kind, raw = text + ("\n" if kind == "paragraph" else " ") + more[0], more[1], raw + more[2]
        return text, kind, raw, style

    def _min_piece_s(self, first: bool, spoken: int) -> float:
        """Seconds of audio to gather before a piece is handed on. `first`: the chunk's first piece;
        `spoken`: chunks of this reply already made (0 while the opening sentence is being made)."""
        return self.MIN_PIECE_S

    def _synth_loop(self) -> None:
        spoken = 0  # chunks of this reply already generated
        while True:
            item = self._next_chunk(merge=spoken > 0)  # the first chunk is one sentence: quick to start, and it measures the pace
            if item is None:
                self._audio_q.put(None)
                return
            if self.speaker._stop.is_set():
                continue
            text, kind, raw, style = item
            if self.timing:
                self.timing.event("synthesis_start")
            rec = [raw, 0.0, 0.0, False]  # see _chunks; filled in before each piece is queued
            self._chunks.append(rec)
            if not text:
                rec[3] = True
                self._audio_q.put((None, raw, True))  # nothing to say: just let the text show
                continue
            cold = style != self.speaker.voice  # a new style: start from its reference, not the reply so far
            if _STYLED_VOICE.fullmatch(self.speaker.cfg.voice.strip()):
                _log_style_choice(style, text)
            if cold:
                self.speaker.set_voice(style)
                if self.timing:
                    self.timing.event("voice_switched")
            # Pieces stream out while the model is still talking, each queued as soon as it is made:
            # a chunk's first sound is not held up waiting for its second piece. Only the last few
            # milliseconds of what has been made are kept back, so that the chunk's true end can be
            # faded out once the generator finishes.
            held: list[np.ndarray] = []
            held_n = 0
            first = True
            generated = False
            keep = max(1, int(SAMPLE_RATE * self.FADE_S))
            tail = np.zeros(0, dtype=np.float32)  # the last TAIL_S of what has been queued, for the gap
            peak = 0.0
            try:
                for piece in self.speaker.synth_stream(text, cont=spoken > 0 and not cold):
                    if self.timing and not generated and piece.size:
                        self.timing.event("first_audio")
                        generated = True
                    held.append(piece)
                    held_n += piece.size
                    if held_n - keep >= SAMPLE_RATE * self._min_piece_s(first, spoken):
                        made = np.concatenate(held)
                        ready, held, held_n = made[:-keep], [made[-keep:]], keep
                        if first:
                            ready = self._trim_lead(ready)
                        peak = max(peak, float(np.abs(ready).max(initial=0.0)))
                        tail = np.concatenate([tail, ready])[-int(SAMPLE_RATE * self.TAIL_S) :]
                        rec[1] += ready.size / SAMPLE_RATE
                        self._audio_q.put((self._shape(ready, first, False), raw, False))
                        first = False
            except Exception as e:  # keep the pipeline alive on a bad chunk
                print(f"[tts error: {e}]")
            if held:
                ready = np.concatenate(held)
                if first:
                    ready = self._trim_lead(ready)
                peak = max(peak, float(np.abs(ready).max(initial=0.0)))
                # Top the voice's own trailing silence up to the gap this boundary wants.
                silence = self._trailing_silence(np.concatenate([tail, ready]), peak)
                pause = max(0.0, self.GAPS.get(kind, self.GAPS["sentence"]) - silence)
                rec[1] += ready.size / SAMPLE_RATE
                rec[2] = pause
                if rec[1] > 0.5:  # this chunk's pace guides the reveal of the next one until it is made
                    self._cps = raw / rec[1] if spoken == 0 else 0.7 * self._cps + 0.3 * (raw / rec[1])
                rec[3] = True
                self._audio_q.put((self._shape(ready, first, True, pause), raw, True))
                spoken += 1
            else:
                rec[3] = True
                self._audio_q.put((None, raw, True))  # the chunk failed: do not leave its text hidden

    def _open_stream(self):
        """Open the device without starting it: _play_loop starts it with the first piece in hand."""
        stream = self.speaker.open_stream()
        if self.timing:
            self.timing.event("stream_open")
        try:
            self._latency = max(0.0, float(stream.latency))
        except Exception:  # noqa: BLE001
            self._latency = 0.0
        return stream

    def _play_loop(self) -> None:
        stream = None
        started = False
        chunk_start = True
        try:
            # The device is opened now, while the first sentence is still being made: opened at the
            # first piece, as before, PortAudio's and PipeWire's set-up sat inside the time to first sound.
            # It is only started at the first piece, though: started here, it ran dry for the half second
            # the first sentence takes and stumbled into the opening word.
            try:
                stream = self._open_stream()
            except Exception:  # noqa: BLE001  not there yet: the first piece tries again and reports it
                stream = None
            while True:
                item = self._audio_q.get()
                if item is None:
                    return
                audio, _raw, _last = item
                if audio is None or self.speaker._stop.is_set():
                    continue
                if stream is None:
                    stream = self._open_stream()
                if not started:
                    stream.start()
                    started = True
                # play() hands the piece over in 100 ms blocks and blocks once the device's buffer is
                # full, so from this mark on the wall clock tracks playback (see _audible). Count the
                # piece as handed over now: it caps the estimate at the piece's end, not its start.
                if self.timing and chunk_start:
                    self.timing.event("playback_estimated", offset=self._latency)
                chunk_start = _last
                self._mark = (self._handed, time.monotonic())
                self._handed += audio.size / SAMPLE_RATE
                dry = getattr(stream, "underflows", 0)
                self.speaker.play(audio, stream)
                if self.timing and getattr(stream, "underflows", 0) > dry:
                    self.timing.event("underflow")  # the device ran dry while this piece was handed over
        finally:
            if started:
                self.speaker.close_stream(stream)
            elif stream is not None:  # nothing was said: no buffer to play out, and stop() on it would fail
                try:
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass

    def _audible(self) -> float:
        """Seconds of the reply's audio the listener has heard: what was handed to the device, less
        what it still holds in its buffer."""
        before, since = self._mark
        return max(0.0, min(self._handed, before + (time.monotonic() - since) - self._latency))

    @property
    def spoken(self) -> int:
        """Whole source characters said so far, for callers that need a text index."""
        return int(self.spoken_position)

    @property
    def spoken_position(self) -> float:
        """Source chars said so far, read continuously off the timeline: the reply views cap their
        reveal at this, so the text glides along with the voice rather than lurching after each piece.
        A chunk still being made is paced by the reply's measured pace; once it is finished its exact
        timing takes over, eased in over BLEND_S so the switch never shows as a jump or a stall."""
        t = self._audible()
        chars = 0.0
        for i, (raw, secs, pause, done) in enumerate(list(self._chunks)):
            if done and t >= secs + pause:
                chars += raw
                t -= secs + pause
                continue
            if not done:
                est = min(raw, t * self._cps)
                self._est = (i, est)
                chars += est
            else:
                exact = raw * t / secs if secs > 0 else float(raw)
                if self._est is not None and self._est[0] == i:  # finished while the guess was showing it
                    self._blend = (i, exact - self._est[1], time.monotonic())
                    self._est = None
                if self._blend is not None and self._blend[0] == i:
                    _, off, since = self._blend
                    # Ease the difference away; when the guess ran ahead, slow down rather than stop.
                    span = max(self.BLEND_S, 2 * abs(off) / max(self._cps, 1.0))
                    exact -= off * max(0.0, 1.0 - (time.monotonic() - since) / span)
                chars += min(raw, exact)
            break
        return max(self._floor, chars)

    @property
    def speaking(self) -> bool:
        """True while audio is playing or queued to play (drives the buddy's mouth)."""
        return self.speaker.is_playing() or not self._audio_q.empty()

    def finish(self) -> None:
        """Flush remaining text and wait until playback completes (or stop() was called)."""
        with self._buf_lock:
            self._cancel_idle()
            self._flush(final=True)
        with self._filler_lock:
            self._closed = True  # a filler still on its clock is not wanted after the reply
            self._cancel_filler()
        self._text_q.put(None)
        self._synth_thread.join()
        self._play_thread.join()
        self._floor = 1 << 30  # everything is said (or dropped by stop): nothing stays hidden

    def stop(self) -> None:
        with self._buf_lock:
            self._cancel_idle()
        with self._filler_lock:
            self._closed = True
            self._cancel_filler()
        self.speaker.stop()
        # Drain so threads exit promptly.
        try:
            while True:
                self._audio_q.get_nowait()
        except queue.Empty:
            pass


def chime(kind: str = "listen", device: AudioDevice | None = None) -> None:
    """Short UI tones so you know when Vision is listening / done, without looking."""
    import sounddevice as sd

    sr = SAMPLE_RATE
    if kind == "listen":
        notes = [(660, 0.07), (880, 0.09)]
    elif kind == "done":
        notes = [(880, 0.06), (660, 0.08)]
    else:
        notes = [(440, 0.12)]
    parts = []
    for f, d in notes:
        t = np.linspace(0, d, int(sr * d), endpoint=False)
        env = np.minimum(1.0, np.minimum(t / 0.01, (d - t) / 0.02))
        parts.append(0.18 * env * np.sin(2 * np.pi * f * t))
    audio = np.concatenate(parts).astype(np.float32)
    device = device or AudioDevice()
    try:
        from vision import devrate

        devrate.play(audio, sr, device)
        sd.wait()
    except Exception:
        pass
    time.sleep(0.02)
