from __future__ import annotations

from importlib.util import module_from_spec
from importlib.util import spec_from_file_location
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = BACKEND_ROOT / "scripts" / "runtime_code_reload.py"


def _module():
    spec = spec_from_file_location("runtime_code_reload_cancel_active", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unavailable_runtime_defers_cancel_only_when_backend_is_idle() -> None:
    module = _module()
    error = RuntimeError("Runtime command delivery failed")

    receipt = module._unavailable_runtime_cancel_receipt(
        error=error,
        backend_idle=True,
    )

    assert receipt == {
        "runs": [],
        "deferred_cancel": "runtime_endpoint_unavailable_backend_idle",
    }


def test_unavailable_runtime_cancel_stays_fail_closed() -> None:
    module = _module()
    delivery_error = RuntimeError("Runtime command delivery failed")
    protocol_error = RuntimeError("Runtime command rejected with HTTP 400")

    assert module._unavailable_runtime_cancel_receipt(
        error=delivery_error,
        backend_idle=False,
    ) is None
    assert module._unavailable_runtime_cancel_receipt(
        error=protocol_error,
        backend_idle=True,
    ) is None
    assert module._unavailable_runtime_cancel_receipt(
        error=RuntimeError("Runtime command rejected with HTTP 408: command deadline exceeded"),
        backend_idle=True,
    ) is not None
