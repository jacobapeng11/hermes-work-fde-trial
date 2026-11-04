"""Backend-owned personal-Agent IM Gateway.

The module deliberately has no deployment boundary of its own.  ``start`` and
``stop`` are imported lazily so API-only processes can load schemas without
starting provider connections.
"""

from app.im_gateway.coordinator import im_gateway

__all__ = ["im_gateway"]
