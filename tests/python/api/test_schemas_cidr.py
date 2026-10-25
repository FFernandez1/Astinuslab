from __future__ import annotations

import pytest

from astinuslab.api.schemas import (
    PlaygroundCreateNetworkRequest,
    PlaygroundRecipeNetwork,
    _validate_cidr_str,
)


@pytest.mark.parametrize(
    "cidr",
    ["10.0.0.0/24", "192.168.1.0/30", "172.16.0.0/16"],
)
def test_validate_cidr_str_accepts_valid(cidr: str) -> None:
    assert _validate_cidr_str(cidr) == cidr


@pytest.mark.parametrize(
    "cidr",
    ["not-a-cidr", "10.0.0.0", "10.0.0.1/24", "10.0.0.0/31", "10.0.0.0/32"],
)
def test_validate_cidr_str_rejects_invalid(cidr: str) -> None:
    with pytest.raises(ValueError):
        _validate_cidr_str(cidr)


def test_request_and_recipe_use_same_validator() -> None:
    req = PlaygroundCreateNetworkRequest(name="n1", subnet_cidr="10.1.0.0/30")
    rec = PlaygroundRecipeNetwork(name="n1", subnet_cidr="10.1.0.0/30")
    assert req.subnet_cidr == rec.subnet_cidr

    with pytest.raises(ValueError):
        PlaygroundCreateNetworkRequest(name="n1", subnet_cidr="10.1.0.0/31")
    with pytest.raises(ValueError):
        PlaygroundRecipeNetwork(name="n1", subnet_cidr="10.1.0.0/31")
