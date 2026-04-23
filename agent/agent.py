#!/usr/bin/env python3
"""Home Lab Monitor - Agent
Runs on each monitored host. Collects system metrics and serves them via HTTP.

Usage:
    python agent.py [config.yml]

The agent serves metrics at http://0.0.0.0:9100/metrics as JSON.
"""
import json
import os
import socket
import subprocess
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen
from urllib.error import URLError

import psutil
import yaml

try:
    import pynvml
    pynvml.nvmlInit()
    _NVIDIA_COUNT = pynvml.nvmlDeviceGetCount()
    NVIDIA_AVAILABLE = _NVIDIA_COUNT > 0
except Exception:
    NVIDIA_AVAILABLE = False
    _NVIDIA_COUNT = 0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path="config.yml"):
    if not os.path.exists(path):
        return {"port": 9100, "services": []}
    with open(path) as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# Metric collectors
# ---------------------------------------------------------------------------

def get_cpu():
    freq = psutil.cpu_freq()
    return {
        "percent": psutil.cpu_percent(interval=None),
        "count_logical": psutil.cpu_count(logical=True),
        "count_physical": psutil.cpu_count(logical=False),
        "freq_mhz": round(freq.current, 1) if freq else None,
        "load_avg_1m": round(os.getloadavg()[0], 2),
        "load_avg_5m": round(os.getloadavg()[1], 2),
        "load_avg_15m": round(os.getloadavg()[2], 2),
    }


def get_memory():
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    return {
        "total_gb": round(vm.total / 1073741824, 2),
        "used_gb": round(vm.used / 1073741824, 2),
        "available_gb": round(vm.available / 1073741824, 2),
        "percent": vm.percent,
        "swap_total_gb": round(sw.total / 1073741824, 2),
        "swap_used_gb": round(sw.used / 1073741824, 2),
        "swap_percent": sw.percent,
    }


def get_disk():
    mounts = []
    seen = set()
    for part in psutil.disk_partitions(all=False):
        if part.fstype in ("", "tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs"):
            continue
        if part.mountpoint in seen:
            continue
        seen.add(part.mountpoint)
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        mounts.append({
            "mountpoint": part.mountpoint,
            "device": part.device,
            "fstype": part.fstype,
            "total_gb": round(usage.total / 1073741824, 2),
            "used_gb": round(usage.used / 1073741824, 2),
            "free_gb": round(usage.free / 1073741824, 2),
            "percent": usage.percent,
        })
    return mounts


_prev_disk_io = None
_prev_net_io = None
_prev_io_time = None
_io_lock = threading.Lock()


def get_io():
    global _prev_disk_io, _prev_net_io, _prev_io_time

    with _io_lock:
     return _get_io_locked()

def _get_io_locked():
    global _prev_disk_io, _prev_net_io, _prev_io_time

    now = time.time()
    disk = psutil.disk_io_counters()
    net = psutil.net_io_counters()

    disk_read_rate = disk_write_rate = net_recv_rate = net_sent_rate = 0.0

    if _prev_io_time and disk and net:
        dt = now - _prev_io_time
        if dt > 0:
            disk_read_rate = round((disk.read_bytes - _prev_disk_io.read_bytes) / dt / 1048576, 2)
            disk_write_rate = round((disk.write_bytes - _prev_disk_io.write_bytes) / dt / 1048576, 2)
            net_recv_rate = round((net.bytes_recv - _prev_net_io.bytes_recv) / dt / 1048576, 2)
            net_sent_rate = round((net.bytes_sent - _prev_net_io.bytes_sent) / dt / 1048576, 2)

    _prev_disk_io = disk
    _prev_net_io = net
    _prev_io_time = now

    return {
        "disk": {
            "read_mbps": max(0.0, disk_read_rate),
            "write_mbps": max(0.0, disk_write_rate),
            "read_total_gb": round(disk.read_bytes / 1073741824, 2) if disk else 0,
            "write_total_gb": round(disk.write_bytes / 1073741824, 2) if disk else 0,
        },
        "network": {
            "recv_mbps": max(0.0, net_recv_rate),
            "sent_mbps": max(0.0, net_sent_rate),
            "recv_total_gb": round(net.bytes_recv / 1073741824, 2),
            "sent_total_gb": round(net.bytes_sent / 1073741824, 2),
        },
    }


def get_gpu():
    gpus = []
    if not NVIDIA_AVAILABLE:
        return gpus
    for i in range(_NVIDIA_COUNT):
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode()
            try:
                power_w = pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0
                power_limit_w = pynvml.nvmlDeviceGetEnforcedPowerLimit(handle) / 1000.0
            except Exception:
                power_w = power_limit_w = None
            gpus.append({
                "index": i,
                "name": name,
                "util_percent": util.gpu,
                "mem_used_gb": round(mem.used / 1073741824, 2),
                "mem_total_gb": round(mem.total / 1073741824, 2),
                "mem_percent": round(mem.used / mem.total * 100, 1) if mem.total else 0,
                "temp_c": temp,
                "power_w": round(power_w, 1) if power_w is not None else None,
                "power_limit_w": round(power_limit_w, 1) if power_limit_w is not None else None,
            })
        except Exception as e:
            gpus.append({"index": i, "error": str(e)})
    return gpus


def check_service(svc):
    svc_type = svc.get("type", "systemd")
    try:
        if svc_type == "systemd":
            unit = svc.get("unit", svc["name"])
            result = subprocess.run(
                ["systemctl", "is-active", "--quiet", unit],
                timeout=5, capture_output=True
            )
            return result.returncode == 0

        elif svc_type == "port":
            host = svc.get("host", "127.0.0.1")
            port = int(svc["port"])
            with socket.create_connection((host, port), timeout=3):
                return True

        elif svc_type == "http":
            url = svc.get("url", f"http://localhost:{svc.get('port', 80)}")
            urlopen(url, timeout=5)
            return True

        elif svc_type == "process":
            name = svc.get("process", svc["name"]).lower()
            for proc in psutil.process_iter(["name"]):
                if name in proc.info["name"].lower():
                    return True
            return False

    except Exception:
        return False
    return False


def get_services(config):
    result = []
    for svc in config.get("services", []):
        result.append({
            "name": svc["name"],
            "up": check_service(svc),
            "type": svc.get("type", "systemd"),
        })
    return result


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

_config = {}


def collect_metrics():
    return {
        "hostname": socket.gethostname(),
        "timestamp": int(time.time()),
        "cpu": get_cpu(),
        "memory": get_memory(),
        "disk": get_disk(),
        "io": get_io(),
        "gpu": get_gpu(),
        "services": get_services(_config),
    }


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/metrics", "/metrics/"):
            try:
                data = collect_metrics()
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_error(500, str(e))
        elif self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        # Only log errors
        if args and str(args[1]) not in ("200", "204"):
            super().log_message(fmt, *args)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    global _config
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yml"
    _config = load_config(config_path)
    port = _config.get("port", 9100)

    # Warm up the IO delta
    get_io()

    server = ThreadingHTTPServer(("0.0.0.0", port), MetricsHandler)
    print(f"[agent] Listening on 0.0.0.0:{port}  hostname={socket.gethostname()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[agent] Shutting down.")


if __name__ == "__main__":
    main()
