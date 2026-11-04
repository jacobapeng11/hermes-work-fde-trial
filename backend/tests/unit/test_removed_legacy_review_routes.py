from app.main import app


def test_legacy_review_routes_are_not_exposed() -> None:
    paths = set(app.openapi()["paths"])

    assert not any(path.startswith("/api/v1/admin/plaza-reviews") for path in paths)
    assert not any(path.startswith("/api/v1/admin/asset-reviews") for path in paths)
    assert not any(
        path.endswith("/share")
        and path.startswith("/api/v1/user/personal-assets/skills/")
        for path in paths
    )
    assert "/api/v1/admin/experts" in paths
