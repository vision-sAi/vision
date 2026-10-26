# Contributing

Vision isn't taking outside contributions for now; you're welcome to fork it. This is how the
maintainers work on it, and a guide to the code for anyone running a fork.

## Setup

```bash
git clone https://github.com/vision-sAi/vision.git && cd vision
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[all]'       # NVIDIA GPU; '.[all-cpu]' without one,
                                                            # '.[voice-common,voice-mac,serve,weather]' on Apple Silicon
```

Text-only work doesn't need a voice extra or a GPU. To reproduce CI without touching your working
`.venv`, sync into a separate one: `UV_PROJECT_ENVIRONMENT=.venv-ci uv sync --frozen --extra serve --extra weather`
(`uv sync` removes whatever the named extras don't include).

## Tests

```bash
env -u CODEX_HOME .venv/bin/python -m unittest discover -s tests
```

The tests use fakes for the CLIs, the GPU and the microphone, so they don't touch your subscriptions.
Run them with `CODEX_HOME` unset, as above, so the Codex tests don't pick up a real config.

## Style

- Match the code around you: plain functions and dataclasses, comments that say *why*, no framework
  for its own sake.
- Anything heavy (torch, numpy, audio, the server) is imported inside the function that needs it, so
  the text chat installs and starts without the optional extras. Keep it that way.
- Don't add anything personal to the repo: hostnames, IPs, keys, home paths, locations.
- New settings go in `DEFAULT_CONFIG` in `vision/config.py` with a comment, and in
  [docs/guide.md](docs/guide.md) if users will look for them.

## The phone and the server

They are updated separately (the app on the phone, the server by its installer), so one can be ahead
of the other. A change that makes the app need something new from the server, or the server from the
app, raises the protocol number in the same commit, in three places: `PROTOCOL` in `vision/server.py`,
`VisionClient.protocolVersion` in the app, and `PROTOCOL` in `ios/fake_server.py`. The app then tells
whoever is behind to update. A change the other side can simply not know about leaves the number alone.
`tests/test_selfupdate.py` fails if the three disagree.

## Licence

By contributing you agree that your work is released under the [GPL-3.0-or-later](LICENSE).
