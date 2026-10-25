"""The topology layout is written one node at a time, never replaced wholesale.

`GET /sessions/current/topology-layout` reads the whole layout;
`PATCH /sessions/current/topology-layout/{node_id}` moves one node. There is no
bulk write, so a client that tries to PUT a layout must be refused.

Each refusal is asserted as 405 exactly. The earlier form accepted `in (404, 405)`,
which passed whether the route was method-restricted as intended or had
vanished from the router altogether -- the one outcome the test exists to catch.
The GET case below is what makes 405 meaningful: it proves the path is routed,
so the refusal is about the method and not about a missing endpoint.
"""

from __future__ import annotations

import pytest

LAYOUT = "/api/v1/playground/sessions/current/topology-layout"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("put", LAYOUT),
        ("post", LAYOUT),
        ("put", f"{LAYOUT}/node-1"),
        ("post", f"{LAYOUT}/node-1"),
    ],
)
def test_F13_bulk_topology_writes_are_refused_as_method_not_allowed(client, method, path):
    response = getattr(client, method)(path, json={"nodes": []})
    assert response.status_code == 405, response.text


def test_F13_the_layout_path_is_routed_for_reads():
    """Without this, a 405 above could not be distinguished from a deleted route."""
    from astinuslab.main import app

    # Starlette types app.routes as BaseRoute, which carries neither attribute;
    # both exist on the APIRoute instances FastAPI actually registers.
    routed = {
        (getattr(route, "path", ""), verb) for route in app.routes for verb in getattr(route, "methods", None) or set()
    }
    assert (LAYOUT, "GET") in routed
    assert (f"{LAYOUT}/{{node_id}}", "PATCH") in routed
    assert not [verb for path, verb in routed if path == LAYOUT and verb in {"PUT", "POST", "DELETE"}]
