from astinuslab.playground.orchestrator import generate_mac


def test_F12_mac_has_32_bit_random_suffix():
    macs = {generate_mac() for _ in range(2000)}
    # 32-bit random space keeps collisions rare, but not impossible.
    assert len(macs) >= 1999
    for mac in macs:
        parts = mac.split(":")
        assert len(parts) == 6
        assert parts[0] == "52"
        assert parts[1] == "54"
