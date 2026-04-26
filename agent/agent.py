#!/usr/bin/env python3
"""Home Lab Monitor - Agent
Runs on each monitored host. Collects system metrics and serves them via HTTP.

Usage:
    python agent.py [config.yml]

The agent serves metrics at http://0.0.0.0:9100/metrics as JSON.
"""
import glob
import json
import os
import re
import socket
import subprocess
import sys
import time
import threading
from datetime import datetime, timedelta
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


def check_service(svc) -> dict:
    svc_type = svc.get("type", "systemd")
    up = False
    detail = None
    try:
        if svc_type == "systemd":
            unit = svc.get("unit", svc["name"])
            result = subprocess.run(
                ["systemctl", "is-active", "--quiet", unit],
                timeout=5, capture_output=True
            )
            up = result.returncode == 0

        elif svc_type == "port":
            host = svc.get("host", "127.0.0.1")
            port = int(svc["port"])
            with socket.create_connection((host, port), timeout=3):
                up = True

        elif svc_type == "http":
            url = svc.get("url", f"http://localhost:{svc.get('port', 80)}")
            urlopen(url, timeout=5)
            up = True

        elif svc_type == "process":
            name = svc.get("process", svc["name"]).lower()
            for proc in psutil.process_iter(["name"]):
                if name in proc.info["name"].lower():
                    up = True
                    break

        elif svc_type == "colima":
            result = subprocess.run(
                ["colima", "status"], timeout=5, capture_output=True
            )
            up = result.returncode == 0

        elif svc_type == "ollama":
            url = svc.get("url", "http://localhost:11434/api/ps")
            resp = urlopen(url, timeout=5)
            data = json.loads(resp.read())
            models = [m["name"] for m in data.get("models", [])]
            up = True
            detail = ", ".join(models) if models else "no active models"

    except Exception:
        pass

    return {"up": up, "detail": detail}


def get_services(config):
    result = []
    for svc in config.get("services", []):
        check = check_service(svc)
        entry = {
            "name": svc["name"],
            "up": check["up"],
            "type": svc.get("type", "systemd"),
        }
        if check["detail"] is not None:
            entry["detail"] = check["detail"]
        result.append(entry)
    return result


# ---------------------------------------------------------------------------
# Babysit discovery
# ---------------------------------------------------------------------------

_max_iter_cache = {}  # log_path -> max_iter
_orphan_emitted = set()  # stop_file paths already emitted as orphaned


def get_babysit(config):
    """Discover running babysit.sh instances from configured scan paths."""
    babysit_cfg = config.get("babysit", {})
    scan_paths_raw = babysit_cfg.get("scan_paths", ["~/sisyphus-logs"])
    include_last_action = babysit_cfg.get("include_last_action", False)
    
    scan_paths = [os.path.expanduser(p) for p in scan_paths_raw]
    scan_paths = [p for p in scan_paths if os.path.isdir(p)]
    
    if not scan_paths:
        return []
    
    instances = []
    now = int(time.time())
    
    # Find all .stop files and matching logs
    for scan_path in scan_paths:
        for stop_file in glob.glob(os.path.join(scan_path, "*.stop")):
            project = os.path.basename(stop_file).replace(".stop", "")
            
            # Find most recent matching log
            log_pattern = os.path.join(scan_path, f"{project}-*.log")
            logs = glob.glob(log_pattern)
            if not logs:
                continue
            
            log_path = max(logs, key=os.path.getmtime)
            
            # Parse log filename: <project>-YYYYMMDD-HHMMSS-<pid>.log
            log_name = os.path.basename(log_path)
            match = re.match(r"(.+)-(\d{8})-(\d{6})-(\d+)\.log$", log_name)
            if not match:
                continue
            
            _, date_str, time_str, pid_str = match.groups()
            pid = int(pid_str)
            
            # Parse started_at from filename
            try:
                dt = datetime.strptime(date_str + time_str, "%Y%m%d%H%M%S")
                started_at = int(dt.timestamp())
            except ValueError:
                continue
            
            # PID-recycle-safe liveness check
            alive = False
            try:
                proc = psutil.Process(pid)
                cmdline = " ".join(proc.cmdline())
                alive = ("babysit" in cmdline and
                         abs(proc.create_time() - started_at) < 5)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                alive = False
            
            # Check for orphaned stop file (>24h old, no live process)
            stop_mtime = os.path.getmtime(stop_file)
            if stop_mtime < now - 86400 and not alive:
                if stop_file not in _orphan_emitted:
                    _orphan_emitted.add(stop_file)
                    instances.append({
                        "project": project,
                        "pid": pid,
                        "started_at": started_at,
                        "log_path": log_path,
                        "state": "crashed",
                        "iter_current": None,
                        "max_iter": None,
                        "backoff_until": None,
                        "termination_reason": "orphaned stop file (>24h, no live process)",
                    })
                continue
            
            # Read log tail (cap at 64 KB)
            try:
                file_size = os.path.getsize(log_path)
                with open(log_path, "rb") as f:
                    f.seek(max(0, file_size - 65536))
                    tail = f.read().decode("utf-8", errors="replace")
            except Exception:
                continue
            
            # Extract max_iter from head (cache it)
            max_iter = None
            if log_path not in _max_iter_cache:
                try:
                    with open(log_path, "rb") as f:
                        head = f.read(4096).decode("utf-8", errors="replace")
                    m = re.search(r"max_iter:\s*(\d+)", head)
                    if m:
                        max_iter = int(m.group(1))
                        _max_iter_cache[log_path] = max_iter
                except Exception:
                    pass
            else:
                max_iter = _max_iter_cache.get(log_path)
            
            # Extract from tail
            iter_current = None
            m = re.findall(r"=== iter (\d+) @", tail)
            if m:
                iter_current = int(m[-1])
            
            backoff_until = None
            m = re.findall(r"Usage limit — backing off \d+m \(resuming ~(\d{2}:\d{2})\)", tail)
            if m:
                time_str = m[-1]
                try:
                    # Parse HH:MM and convert to unix timestamp
                    hh, mm = map(int, time_str.split(":"))
                    resume_today = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
                    # If the time is in the past, assume tomorrow
                    if resume_today.timestamp() < time.time():
                        resume_today += timedelta(days=1)
                    backoff_until = int(resume_today.timestamp())
                except Exception:
                    pass
            
            termination_reason = None
            for pattern, reason in [
                (r"STOP signal received", "STOP signal received"),
                (r"Hit MAX_ITER", "Hit MAX_ITER"),
                (r"Stuck:", "Stuck"),
                (r"Done after", "Done after"),
            ]:
                if re.search(pattern, tail):
                    termination_reason = reason
                    break
            
            last_action = None
            if include_last_action:
                m = re.findall(r"\s+\[(text|tool)\] (.+)", tail)
                if m:
                    last_action = m[-1][1][:200]
            
            # Derive state
            if alive:
                if backoff_until:
                    state = "backoff"
                else:
                    state = "running"
            else:
                state = "crashed"
            
            entry = {
                "project": project,
                "pid": pid,
                "started_at": started_at,
                "log_path": log_path,
                "state": state,
                "iter_current": iter_current,
                "max_iter": max_iter,
                "backoff_until": backoff_until,
                "termination_reason": termination_reason,
            }
            if include_last_action and last_action is not None:
                entry["last_action"] = last_action
            
            instances.append(entry)
        
        # Check for recently-terminated (log exists, no stop file, mtime < 300s ago)
        for log_path in glob.glob(os.path.join(scan_path, "*-*.log")):
            log_name = os.path.basename(log_path)
            match = re.match(r"(.+)-(\d{8})-(\d{6})-(\d+)\.log$", log_name)
            if not match:
                continue
            
            project, date_str, time_str, pid_str = match.groups()
            stop_file = os.path.join(scan_path, f"{project}.stop")
            
            # Skip if stop file exists (already handled above)
            if os.path.exists(stop_file):
                continue
            
            # Only include if log mtime is recent
            log_mtime = os.path.getmtime(log_path)
            if log_mtime < now - 300:
                continue
            
            pid = int(pid_str)
            try:
                dt = datetime.strptime(date_str + time_str, "%Y%m%d%H%M%S")
                started_at = int(dt.timestamp())
            except ValueError:
                continue
            
            # Read tail
            try:
                file_size = os.path.getsize(log_path)
                with open(log_path, "rb") as f:
                    f.seek(max(0, file_size - 65536))
                    tail = f.read().decode("utf-8", errors="replace")
            except Exception:
                continue
            
            # Extract max_iter
            max_iter = _max_iter_cache.get(log_path)
            if max_iter is None:
                try:
                    with open(log_path, "rb") as f:
                        head = f.read(4096).decode("utf-8", errors="replace")
                    m = re.search(r"max_iter:\s*(\d+)", head)
                    if m:
                        max_iter = int(m.group(1))
                        _max_iter_cache[log_path] = max_iter
                except Exception:
                    pass
            
            # Extract iter
            iter_current = None
            m = re.findall(r"=== iter (\d+) @", tail)
            if m:
                iter_current = int(m[-1])
            
            # Extract termination reason
            termination_reason = None
            for pattern, reason in [
                (r"STOP signal received", "STOP signal received"),
                (r"Hit MAX_ITER", "Hit MAX_ITER"),
                (r"Stuck:", "Stuck"),
                (r"Done after", "Done after"),
            ]:
                if re.search(pattern, tail):
                    termination_reason = reason
                    break
            
            last_action = None
            if include_last_action:
                m = re.findall(r"\s+\[(text|tool)\] (.+)", tail)
                if m:
                    last_action = m[-1][1][:200]
            
            entry = {
                "project": project,
                "pid": pid,
                "started_at": started_at,
                "log_path": log_path,
                "state": "finished",
                "iter_current": iter_current,
                "max_iter": max_iter,
                "backoff_until": None,
                "termination_reason": termination_reason,
            }
            if include_last_action and last_action is not None:
                entry["last_action"] = last_action
            
            instances.append(entry)
    
    return instances


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

_config = {}


def collect_metrics():
    data = {
        "hostname": socket.gethostname(),
        "timestamp": int(time.time()),
        "cpu": get_cpu(),
        "memory": get_memory(),
        "disk": get_disk(),
        "io": get_io(),
        "gpu": get_gpu(),
        "services": get_services(_config),
    }
    
    # Add babysit field
    babysit = get_babysit(_config)
    if babysit or _config.get("babysit"):
        data["babysit"] = babysit
    
    return data


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
