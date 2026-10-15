"""`--bind-host`: which address the device-facing listeners and the panel take.

A host that already runs another server on port 80 or 443 (a reverse proxy, a
second web app) cannot give this one every interface. The flag lets it bind a
dedicated address instead.
"""
from petkit_local.main.cli import build_config, parse_args


def test_listeners_take_every_interface_by_default():
    assert build_config(parse_args([])).bind_host == "0.0.0.0"


def test_bind_host_flag_sets_the_listener_address():
    config = build_config(parse_args(["--bind-host", "192.168.0.121"]))
    assert config.bind_host == "192.168.0.121"
