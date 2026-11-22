"""Shared setup for the scripts in tools/: read the gitignored env files and import the live server code
with the repo's personas and prompts, so evals and demos run exactly what /api/* runs."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_env(name):
    """KEY=VALUE lines from .secrets/<name> (comments and blank lines skipped)."""
    env = {}
    with open(os.path.join(ROOT, ".secrets", name), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v
    return env


def server():
    """The server module, configured to read this checkout's personas/ and prompts/ and keys."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for k, v in read_env("mylife.env").items():
        os.environ.setdefault(k, v)
    prompts = os.path.join(ROOT, "prompts")
    os.environ["MYLIFE_PERSONAS"] = os.path.join(ROOT, "personas")
    os.environ["MYLIFE_STYLE"] = os.path.join(prompts, "de-ai-style.txt")
    os.environ["MYLIFE_SELF_FRAME"] = os.path.join(prompts, "self-frame.txt")
    os.environ["MYLIFE_CLOSING_STYLE"] = os.path.join(prompts, "closing-style.txt")
    os.environ["MYLIFE_CARDS"] = os.path.join(prompts, "rainbow-cards.json")
    os.environ["MYLIFE_PORTRAIT"] = os.path.join(prompts, "portrait.txt")
    os.environ.setdefault("MYLIFE_CACHE", os.path.join(ROOT, ".eval-cache"))
    os.environ.setdefault("MYLIFE_DATA", os.path.join(ROOT, ".eval-cache", "data"))
    sys.path.insert(0, os.path.join(ROOT, "server"))
    import mylife_server
    return mylife_server
