from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "ops" / "host" / "prebake_attacker_image.sh"


def test_prebake_attacker_image_enables_dhcp_and_guest_agent_on_boot():
    text = SCRIPT.read_text(encoding="utf-8")

    assert "--install openssh-server,qemu-guest-agent,sudo,network-manager" in text
    assert "nmcli connection add type ethernet" in text
    assert "ipv4.method auto" in text
    assert "connection.autoconnect yes" in text
    assert "systemctl enable --now qemu-guest-agent" in text
