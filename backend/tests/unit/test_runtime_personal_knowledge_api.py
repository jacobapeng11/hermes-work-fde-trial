from app.main import create_app


def test_runtime_personal_knowledge_routes_are_registered() -> None:
    routes = {
        (route.path, method)
        for route in create_app().routes
        for method in (route.methods or set())
    }

    assert (
        "/api/v1/runtime-personal-knowledge/{runtime_workspace_id}/state",
        "GET",
    ) in routes
    assert (
        "/api/v1/runtime-personal-knowledge/{runtime_workspace_id}/search",
        "POST",
    ) in routes
    assert (
        "/api/v1/runtime-personal-knowledge/{runtime_workspace_id}/wiki-search",
        "POST",
    ) in routes
    assert (
        "/api/v1/runtime-personal-knowledge/{runtime_workspace_id}/hybrid-search",
        "POST",
    ) in routes
    assert (
        "/api/v1/runtime-personal-knowledge/{runtime_workspace_id}/wiki-read-page",
        "POST",
    ) in routes
