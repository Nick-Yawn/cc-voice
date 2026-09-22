"""Configuration: defaults, ~/.config/earshot/config.toml, a project's
.earshot.toml overlay, and the API keys from the environment.

Keys never live in the config file. They are read from DEEPGRAM_API_KEY
and CARTESIA_API_KEY and removed from the child claude's environment:
the agent runs shell commands and has no need to see them.
"""

import copy
import os
import tomllib
from pathlib import Path

USER_CONFIG_PATH = Path("~/.config/earshot/config.toml")
PROJECT_CONFIG_NAME = ".earshot.toml"

DEEPGRAM_KEY_ENV = "DEEPGRAM_API_KEY"
CARTESIA_KEY_ENV = "CARTESIA_API_KEY"
KEY_ENV_VARS = (DEEPGRAM_KEY_ENV, CARTESIA_KEY_ENV)

DEFAULTS: dict = {
    "stt": {
        "provider": "deepgram",
        "model": "nova-3",
        "language": "en",
    },
    "tts": {
        "provider": "cartesia",
        "voice": "",          # a Cartesia voice id; required for voice mode
        "model": "sonic-3.6-2026-08-27",
    },
    "words": {
        "address": "operator",
        "closer": "over",
        "filler_before_address": False,
    },
    "turns": {
        "closer_settle_s": 0.4,   # silence after the closer before it counts
        "readdress_gap_s": 2.0,   # quiet before a new address discards a turn
        "idle_turn_s": 60.0,      # an open turn with no closer expires
    },
    "volumes": {
        "speech": 1.0,
        "narration": 0.5,
        "earcons": 0.6,
    },
    "seat": {
        "claude": "claude",       # the binary to drive
        "claude_args": [],        # extra flags, e.g. ["--permission-mode", "acceptEdits"]
        "idle_close_min": 30,
        "still_here_s": 15,
        "closer_fallback": "Done.",  # spoken when a query reports no usage
    },
    "audio": {
        "input_device": None,
        "output_device": None,
    },
    "respell": {},                # spoken form for jargon, e.g. dev = "devv"
}


def deep_merge(base: dict, overlay: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _read_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def load_config(project_dir: str | os.PathLike | None = None,
                user_path: str | os.PathLike | None = None) -> dict:
    """Defaults, then the user file, then the project overlay."""
    user = Path(user_path).expanduser() if user_path else USER_CONFIG_PATH.expanduser()
    cfg = deep_merge(DEFAULTS, _read_toml(user))
    if project_dir is not None:
        cfg = deep_merge(cfg, _read_toml(Path(project_dir) / PROJECT_CONFIG_NAME))
    return cfg


def api_keys(environ: dict | None = None) -> dict[str, str | None]:
    env = os.environ if environ is None else environ
    return {"deepgram": env.get(DEEPGRAM_KEY_ENV) or None,
            "cartesia": env.get(CARTESIA_KEY_ENV) or None}


def child_env(environ: dict | None = None,
              strip: tuple[str, ...] = KEY_ENV_VARS) -> dict:
    """The environment the child claude gets: ours minus the speech keys."""
    env = os.environ if environ is None else environ
    return {k: v for k, v in env.items() if k not in strip}
