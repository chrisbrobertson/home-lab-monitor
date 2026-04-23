"""Slot placement algorithm.

pick_host() selects the best host for a new slot reservation given the
current metrics snapshot and active slot state.
"""
from typing import List, Optional, Tuple

CPU_THRESHOLD = 80.0     # percent
MEM_THRESHOLD = 85.0     # percent


def pick_host(
    host_configs: list,      # List[HostConfig] — docker-eligible hosts
    metrics_by_host: dict,   # host_name -> latest() dict from DB
    slots_by_host: dict,     # host_name -> count of active slots
    policy,                  # SlotPolicyConfig
    used_offsets_by_host: dict,  # host_name -> List[int] of in-use port_offsets
    host_hint: Optional[str] = None,
) -> Tuple[Optional[str], int, str]:
    """Return (host_name, port_offset, detail).

    detail is a human-readable explanation of why a host was chosen or rejected.
    On failure host_name is None and port_offset is -1.
    """
    candidates = []

    for hcfg in host_configs:
        if not hcfg.docker:
            continue

        name = hcfg.name
        max_slots = hcfg.max_slots if hcfg.max_slots > 0 else policy.max_slots_per_host

        # --- online check ---
        m = metrics_by_host.get(name)
        if not m or not m.get("_online"):
            continue

        # --- capacity check ---
        active = slots_by_host.get(name, 0)
        if active >= max_slots:
            continue

        # --- load checks ---
        cpu_pct = m.get("cpu", {}).get("percent", 0.0)
        mem_pct = m.get("memory", {}).get("percent", 0.0)
        if cpu_pct >= CPU_THRESHOLD or mem_pct >= MEM_THRESHOLD:
            continue

        # --- host_hint filter ---
        if host_hint and name != host_hint:
            continue

        # --- find lowest free port_offset ---
        used = set(used_offsets_by_host.get(name, []))
        free_offset = _lowest_free_offset(used, policy.max_port_offsets)
        if free_offset is None:
            continue

        headroom = max_slots - active
        candidates.append((headroom, name, free_offset))

    if not candidates:
        return None, -1, "no eligible host: all offline, at capacity, or over load threshold"

    # Sort by most headroom descending (most available host first)
    candidates.sort(key=lambda x: -x[0])
    _, chosen_host, chosen_offset = candidates[0]
    return chosen_host, chosen_offset, f"placed on {chosen_host} at offset {chosen_offset}"


def _lowest_free_offset(used: set, max_offsets: int) -> Optional[int]:
    for i in range(max_offsets):
        if i not in used:
            return i
    return None
