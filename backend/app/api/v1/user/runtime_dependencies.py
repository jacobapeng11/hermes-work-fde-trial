from __future__ import annotations

from app.hermes_runtime.dashboard_protocol import HermesDashboardProtocolAdapter
from app.services.runtime_infrastructure import build_runtime_protocol_adapters


async def get_hermes_protocol_adapter() -> HermesDashboardProtocolAdapter:
    return build_runtime_protocol_adapters().dashboard_adapter
