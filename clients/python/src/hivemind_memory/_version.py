from importlib import metadata

try:
    __version__ = metadata.version("hivemind-memory")
except metadata.PackageNotFoundError:
    __version__ = "0+unknown"
