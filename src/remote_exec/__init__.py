"""Remote command execution over gRPC."""

from importlib.metadata import PackageNotFoundError, version

# Single source of truth: the version in pyproject.toml (via installed metadata),
# shared by remote-exec-server, remote-exec-client and the GetServerInfo RPC.
try:
    __version__ = version("remote-exec")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0.0.0+unknown"
