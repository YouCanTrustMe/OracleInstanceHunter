"""
server_stats.py — read-only server load snapshot.

Can be used standalone:
    python server_stats.py
Or imported:
    import server_stats
    print(server_stats.format_report())
"""

import os
import time


def _read_proc(path: str) -> str:
    with open(path) as f:
        return f.read()


def cpu_percent(interval: float = 0.5) -> float:
    """Return CPU usage % averaged over `interval` seconds."""
    def _read_stat():
        line = _read_proc("/proc/stat").splitlines()[0].split()
        vals = list(map(int, line[1:]))
        idle = vals[3]
        total = sum(vals)
        return idle, total

    idle0, total0 = _read_stat()
    time.sleep(interval)
    idle1, total1 = _read_stat()
    diff_total = total1 - total0
    diff_idle = idle1 - idle0
    if diff_total == 0:
        return 0.0
    return round(100.0 * (1 - diff_idle / diff_total), 1)


def load_avg() -> tuple[float, float, float]:
    """Return (1m, 5m, 15m) load averages."""
    vals = os.getloadavg()
    return round(vals[0], 2), round(vals[1], 2), round(vals[2], 2)


def memory() -> dict:
    """Return dict with total_mb, used_mb, free_mb, percent."""
    info = {}
    for line in _read_proc("/proc/meminfo").splitlines():
        parts = line.split()
        if parts[0] in ("MemTotal:", "MemAvailable:"):
            info[parts[0].rstrip(":")] = int(parts[1])  # kB
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", 0)
    used = total - available
    pct = round(100.0 * used / total, 1) if total else 0.0
    return {
        "total_mb": total // 1024,
        "used_mb": used // 1024,
        "free_mb": available // 1024,
        "percent": pct,
    }


def disk(path: str = "/") -> dict:
    """Return dict with total_gb, used_gb, free_gb, percent for `path`."""
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - free
    pct = round(100.0 * used / total, 1) if total else 0.0
    return {
        "total_gb": round(total / 1024**3, 1),
        "used_gb": round(used / 1024**3, 1),
        "free_gb": round(free / 1024**3, 1),
        "percent": pct,
    }


def uptime_str() -> str:
    """Return human-readable uptime string."""
    seconds = float(_read_proc("/proc/uptime").split()[0])
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    return f"{h}h {m:02d}m"


def format_report() -> str:
    """Return a multi-line server load report."""
    l1, l5, l15 = load_avg()
    mem = memory()
    dsk = disk("/")
    cpu = cpu_percent(0.5)
    up = uptime_str()

    bar = lambda pct: ("█" * int(pct // 10)).ljust(10, "░")

    lines = [
        f"Server load  (uptime {up})",
        f"",
        f"CPU   {bar(cpu)} {cpu}%",
        f"RAM   {bar(mem['percent'])} {mem['percent']}%  ({mem['used_mb']}/{mem['total_mb']} MB)",
        f"Disk  {bar(dsk['percent'])} {dsk['percent']}%  ({dsk['used_gb']}/{dsk['total_gb']} GB)",
        f"",
        f"Load avg  {l1} / {l5} / {l15}  (1m/5m/15m)",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_report())
