"""Profile-correct Hermes home resolution."""
import os
from pathlib import Path


def hermes_home() -> Path:
    """Active profile's Hermes home via the public hermes_constants helper.

    hermes_constants.get_hermes_home honours the context-local profile override
    (multi-profile gateways) before HERMES_HOME; reading os.environ directly can
    resolve another profile's directories. Falls back to HERMES_HOME only when
    Hermes is not importable (isolated tests).
    """
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        return Path(os.environ.get('HERMES_HOME', '~/.hermes')).expanduser()
    return Path(get_hermes_home()).expanduser()
