"""FIF 口语 desktop and local WebUI client."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version('fif-oral-desktop')
except PackageNotFoundError:
    __version__ = '1.0.0b1'
