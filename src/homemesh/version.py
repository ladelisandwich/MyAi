"""Single source of truth for the HomeMesh version and protocol version."""

__version__ = "0.1.0"

# Version of the coordinator <-> worker wire protocol (see docs/PROTOCOL.md).
# Bump on incompatible message changes; workers and coordinators negotiate the
# highest version both support.
PROTOCOL_VERSION = 1
SUPPORTED_PROTOCOL_VERSIONS = (1,)
