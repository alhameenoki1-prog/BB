"""Canonical filesystem locations for Harvey.

Two roots:

``PROJECT_ROOT`` is the code checkout: the package, ``prompts/`` and the
shipped ``skills/``. ``resolve()`` matters — when the package is reached
through a symlink (some editable installs), a bare ``Path(__file__).parent``
would point into site-packages and Harvey would silently read the wrong
files.

``HARVEY_HOME`` is where one Harvey *instance* keeps its own files: its
``harvey.yaml``, ``.env``, ``data/`` (database, mail tokens, logs) and any
per-instance ``skills/`` overrides. It defaults to the checkout, so a plain
``git clone && harvey run`` behaves as it always has. Setting the
``HARVEY_HOME`` environment variable points one process at a different
home, which is how the hosted web app (``bb/``) runs one isolated Harvey per
user from a single install.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARVEY_HOME = Path(os.environ.get("HARVEY_HOME") or PROJECT_ROOT).expanduser().resolve()

DATA_DIR = HARVEY_HOME / "data"
CONFIG_FILE = HARVEY_HOME / "harvey.yaml"
LOCAL_CONFIG_FILE = HARVEY_HOME / "harvey.local.yaml"
ENV_FILE = HARVEY_HOME / ".env"
PROMPTS_DIR = PROJECT_ROOT / "prompts"
# Shipped knowledge, and the per-home overrides that win over it.
SKILLS_DIR = PROJECT_ROOT / "skills"
HOME_SKILLS_DIR = HARVEY_HOME / "skills"
