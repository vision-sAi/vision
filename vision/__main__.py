"""Entry point for `vision` and `python -m vision`."""
from __future__ import annotations

import sys

# Optional packages and the extra that brings each one in (see [project.optional-dependencies]).
_EXTRAS = {
    "numpy": "voice",
    "sounddevice": "voice",
    "soundfile": "voice",
    "faster_whisper": "voice",
    "ctranslate2": "voice",
    "webrtcvad": "voice",
    "onnxruntime": "voice",
    "qwen_tts": "voice",
    "torch": "voice",
    "transformers": "voice",
    "fastapi": "serve",
    "starlette": "serve",
    "uvicorn": "serve",
    "qrcode": "serve",
    "multipart": "serve",
    "cryptography": "weather",
}


def run() -> None:
    """Run the CLI; a missing optional dependency becomes an install hint instead of a traceback."""
    try:
        from vision.cli import main

        main()
    except ModuleNotFoundError as e:
        extra = _EXTRAS.get((e.name or "").split(".")[0])
        if extra is None:
            raise
        from vision.install import extra_hint

        install = extra_hint(extra)
        print(
            f"vision: this needs the optional '{extra}' dependencies ({e.name} is not installed).\n"
            f"Install them with:  {install}",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    run()
