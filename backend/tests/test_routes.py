from fastapi.routing import APIRoute

from main import app


def _route(path: str, method: str) -> APIRoute:
    matches = [
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path == path
        and method in (route.methods or set())
    ]
    assert len(matches) == 1
    return matches[0]


def test_release_child_routes_are_registered_before_generic_release_route() -> None:
    paths = [
        (route.path, route.methods)
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path.startswith("/api/v1/config/releases")
    ]
    detail_index = next(index for index, (path, _) in enumerate(paths) if path.endswith("/detail"))
    acks_index = next(index for index, (path, _) in enumerate(paths) if path.endswith("/acks"))
    generic_index = next(
        index
        for index, (path, methods) in enumerate(paths)
        if path.endswith("{release_id}") and methods == {"GET"}
    )
    assert detail_index < generic_index
    assert acks_index < generic_index


def test_release_child_routes_have_the_expected_handlers() -> None:
    assert _route("/api/v1/config/releases/{release_id}/detail", "GET").endpoint.__name__ == "get_release_detail"
    assert _route("/api/v1/config/releases/{release_id}/acks", "GET").endpoint.__name__ == "list_release_acks"
