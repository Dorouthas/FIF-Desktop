"""Resolve bundled resources separately from writable user data."""
import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
RESOURCE_ROOT = PACKAGE_ROOT / 'resources'


def default_data_dir():
    override = os.environ.get('FIF_ORAL_DATA_DIR')
    if override:
        return Path(override).expanduser().resolve()
    source_root = PACKAGE_ROOT.parents[1]
    if (source_root / 'pyproject.toml').is_file() and source_root / 'src' / 'fif_oral' == PACKAGE_ROOT:
        # Keep existing checkout accounts and queues at the original location.
        return source_root / 'data'
    local = os.environ.get('LOCALAPPDATA')
    return (Path(local) / 'FIF-Oral' / 'data' if local
            else Path.home() / '.local' / 'share' / 'fif-oral' / 'data')
