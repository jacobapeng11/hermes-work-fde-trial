from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "install-metrics-server.sh"
)


def test_metrics_server_script_is_pinned_and_checksum_verified() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "v0.8.1/components.yaml" in source
    assert (
        "4a672c4891902573a3ff753cece5de1bf1f55dd053403dfec39df9d1636b7ff1"
        in source
    )
    assert "sha256sum" in source
    assert "releases/latest" not in source


def test_insecure_kubelet_tls_is_explicit_opt_in() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "METRICS_SERVER_KUBELET_INSECURE_TLS:-0" in source
    assert "--kubelet-insecure-tls" in source
    assert 'if [[ "$INSECURE_TLS" == "1" ]]' in source


def test_script_verifies_api_top_and_hpas() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "v1beta1.metrics.k8s.io" in source
    assert "kubectl top nodes" in source
    assert "kubectl top pods -A" in source
    assert "activator" in source
    assert "webhook" in source
    assert "3scale-kourier-gateway" in source
