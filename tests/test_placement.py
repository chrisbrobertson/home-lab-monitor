"""Unit tests for placement.pick_host()."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from config import SlotPolicyConfig, HostConfig
from placement import pick_host

POLICY = SlotPolicyConfig(
    max_slots_per_host=4,
    default_ttl_seconds=3600,
    port_base=20000,
    port_stride=10,
    max_port_offsets=50,
)

HOST_A = HostConfig(name="host-a", address="10.0.0.1", docker=True, max_slots=4)
HOST_B = HostConfig(name="host-b", address="10.0.0.2", docker=True, max_slots=2)
HOST_C = HostConfig(name="host-c", address="10.0.0.3", docker=False)


def _online(cpu=10.0, mem=40.0) -> dict:
    return {"_online": True, "cpu": {"percent": cpu}, "memory": {"percent": mem}}


def _offline() -> dict:
    return {"_online": False}


def test_picks_online_docker_host():
    metrics = {"host-a": _online(), "host-b": _online(), "host-c": _online()}
    host, offset, _ = pick_host(
        host_configs=[HOST_A, HOST_B, HOST_C],
        metrics_by_host=metrics,
        slots_by_host={},
        policy=POLICY,
        used_offsets_by_host={},
    )
    assert host in ("host-a", "host-b")
    assert offset == 0


def test_skips_offline_host():
    metrics = {"host-a": _offline(), "host-b": _online()}
    host, offset, _ = pick_host(
        host_configs=[HOST_A, HOST_B],
        metrics_by_host=metrics,
        slots_by_host={},
        policy=POLICY,
        used_offsets_by_host={},
    )
    assert host == "host-b"


def test_skips_non_docker_host():
    metrics = {"host-c": _online()}
    host, _, _ = pick_host(
        host_configs=[HOST_C],
        metrics_by_host=metrics,
        slots_by_host={},
        policy=POLICY,
        used_offsets_by_host={},
    )
    assert host is None


def test_skips_host_at_capacity():
    metrics = {"host-b": _online()}
    host, _, _ = pick_host(
        host_configs=[HOST_B],
        metrics_by_host=metrics,
        slots_by_host={"host-b": 2},  # host-b max_slots=2
        policy=POLICY,
        used_offsets_by_host={},
    )
    assert host is None


def test_skips_host_over_cpu_threshold():
    metrics = {"host-a": _online(cpu=85.0, mem=40.0)}
    host, _, _ = pick_host(
        host_configs=[HOST_A],
        metrics_by_host=metrics,
        slots_by_host={},
        policy=POLICY,
        used_offsets_by_host={},
    )
    assert host is None


def test_skips_host_over_mem_threshold():
    metrics = {"host-a": _online(cpu=10.0, mem=90.0)}
    host, _, _ = pick_host(
        host_configs=[HOST_A],
        metrics_by_host=metrics,
        slots_by_host={},
        policy=POLICY,
        used_offsets_by_host={},
    )
    assert host is None


def test_assigns_lowest_free_offset():
    metrics = {"host-a": _online()}
    host, offset, _ = pick_host(
        host_configs=[HOST_A],
        metrics_by_host=metrics,
        slots_by_host={"host-a": 2},
        policy=POLICY,
        used_offsets_by_host={"host-a": [0, 1]},
    )
    assert host == "host-a"
    assert offset == 2


def test_host_hint_respected():
    metrics = {"host-a": _online(), "host-b": _online()}
    host, _, _ = pick_host(
        host_configs=[HOST_A, HOST_B],
        metrics_by_host=metrics,
        slots_by_host={},
        policy=POLICY,
        used_offsets_by_host={},
        host_hint="host-b",
    )
    assert host == "host-b"


def test_host_hint_unavailable_returns_none():
    metrics = {"host-a": _online(), "host-b": _offline()}
    host, _, _ = pick_host(
        host_configs=[HOST_A, HOST_B],
        metrics_by_host=metrics,
        slots_by_host={},
        policy=POLICY,
        used_offsets_by_host={},
        host_hint="host-b",
    )
    assert host is None


def test_prefers_host_with_more_headroom():
    # host-a has 4 slots, 1 used → 3 free; host-b has 2 slots, 0 used → 2 free
    metrics = {"host-a": _online(), "host-b": _online()}
    host, _, _ = pick_host(
        host_configs=[HOST_A, HOST_B],
        metrics_by_host=metrics,
        slots_by_host={"host-a": 1, "host-b": 0},
        policy=POLICY,
        used_offsets_by_host={"host-a": [0]},
    )
    assert host == "host-a"


def test_no_free_port_offsets_skips_host():
    metrics = {"host-a": _online()}
    used = list(range(POLICY.max_port_offsets))
    host, _, _ = pick_host(
        host_configs=[HOST_A],
        metrics_by_host=metrics,
        slots_by_host={"host-a": 0},
        policy=POLICY,
        used_offsets_by_host={"host-a": used},
    )
    assert host is None
