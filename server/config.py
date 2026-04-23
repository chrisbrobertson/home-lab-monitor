"""Typed configuration loader for Home Lab Monitor server."""
from dataclasses import dataclass, field
from typing import List, Optional
import yaml


@dataclass
class RegistryConfig:
    host: str
    port: int = 5000
    scheme: str = "http"

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def catalog_url(self) -> str:
        return f"{self.url}/v2/_catalog"

    @property
    def v2_url(self) -> str:
        return f"{self.url}/v2/"


@dataclass
class SlotPolicyConfig:
    max_slots_per_host: int = 4
    default_ttl_seconds: int = 3600
    port_base: int = 20000
    port_stride: int = 10
    max_port_offsets: int = 50


@dataclass
class HostConfig:
    name: str
    address: str
    port: int = 9100
    docker: bool = False
    max_slots: int = 0
    ssh_user: str = ""
    role: str = "monitor"


@dataclass
class ServerConfig:
    hosts: List[HostConfig]
    registry: Optional[RegistryConfig]
    slot_policy: SlotPolicyConfig
    server_port: int = 8888
    poll_interval: int = 60


def load_raw(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def parse_registry(raw: dict) -> Optional[RegistryConfig]:
    r = raw.get("registry")
    if not r or not r.get("host"):
        return None
    return RegistryConfig(
        host=r["host"],
        port=r.get("port", 5000),
        scheme=r.get("scheme", "http"),
    )


def parse_slot_policy(raw: dict) -> SlotPolicyConfig:
    sp = raw.get("slot_policy", {})
    return SlotPolicyConfig(
        max_slots_per_host=sp.get("max_slots_per_host", 4),
        default_ttl_seconds=sp.get("default_ttl_seconds", 3600),
        port_base=sp.get("port_base", 20000),
        port_stride=sp.get("port_stride", 10),
        max_port_offsets=sp.get("max_port_offsets", 50),
    )


def parse_hosts(raw: dict) -> List[HostConfig]:
    hosts = []
    for h in raw.get("hosts", []):
        hosts.append(HostConfig(
            name=h["name"],
            address=h["address"],
            port=h.get("port", 9100),
            docker=h.get("docker", False),
            max_slots=h.get("max_slots", 0),
            ssh_user=h.get("ssh_user", ""),
            role=h.get("role", "monitor"),
        ))
    return hosts


def load_server_config(path: str) -> ServerConfig:
    raw = load_raw(path)
    return ServerConfig(
        hosts=parse_hosts(raw),
        registry=parse_registry(raw),
        slot_policy=parse_slot_policy(raw),
        server_port=raw.get("server", {}).get("port", 8888),
        poll_interval=int(raw.get("server", {}).get("poll_interval", 60)),
    )
