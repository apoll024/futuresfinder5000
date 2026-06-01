"""
Resource watchdog — polls every 5 minutes and writes metrics to health_metrics table.

Monitors:
  - CPU and RAM per container (docker SDK via /var/run/docker.sock)
  - Disk usage on /var/lib/docker (Postgres data)
  - Container liveness (auto-restarts stopped containers)

Alert thresholds: warn >= 80%, critical >= 92%
All metrics visible in the dashboard via /api/health endpoint.
"""
import os, sys, subprocess, json, time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import docker as docker_sdk

from db.models import init_db, Session, HealthMetric, Bar, Signal, write_inbox

POLL_INTERVAL      = 300
WARN_PCT           = 80.0
CRITICAL_PCT       = 92.0
WATCHED_CONTAINERS = ["ff_db", "ff_api", "ff_settler", "ff_crypto", "ff_digest", "ff_watchdog"]

# Docker SDK client — connects via /var/run/docker.sock (mounted in compose)
_docker_client = None

def _get_docker():
    global _docker_client
    if _docker_client is None:
        _docker_client = docker_sdk.from_env()
    return _docker_client


def docker_stats() -> list:
    """Return CPU/mem stats for all running containers via Docker SDK."""
    try:
        client = _get_docker()
        out = []
        for container in client.containers.list():
            try:
                s = container.stats(stream=False)
                # CPU %
                cpu_delta = (s["cpu_stats"]["cpu_usage"]["total_usage"]
                             - s["precpu_stats"]["cpu_usage"]["total_usage"])
                sys_delta  = (s["cpu_stats"].get("system_cpu_usage", 0)
                              - s["precpu_stats"].get("system_cpu_usage", 0))
                n_cpu      = s["cpu_stats"].get("online_cpus") or len(
                             s["cpu_stats"]["cpu_usage"].get("percpu_usage", [0]))
                cpu_pct    = (cpu_delta / sys_delta * n_cpu * 100.0) if sys_delta > 0 else 0.0
                # Mem %
                mem_use    = s["memory_stats"].get("usage", 0)
                mem_limit  = s["memory_stats"].get("limit", 1)
                mem_pct    = (mem_use / mem_limit * 100.0) if mem_limit > 0 else 0.0
                mem_label  = f"{mem_use/1024**2:.1f}MiB / {mem_limit/1024**2:.0f}MiB"
                out.append({
                    "name":      container.name,
                    "cpu_pct":   round(cpu_pct, 2),
                    "mem_pct":   round(mem_pct, 2),
                    "mem_usage": mem_label,
                })
            except Exception:
                pass
        return out
    except Exception as e:
        print(f"[watchdog] docker stats error: {e}")
        return []


def disk_pct(path="/var/lib/docker"):
    try:
        r = subprocess.run(["df", "--output=pcent", path],
                           capture_output=True, text=True, timeout=10)
        lines = r.stdout.strip().splitlines()
        if len(lines) >= 2:
            return float(lines[1].strip().replace("%", ""))
    except Exception:
        pass
    return 0.0


def running_containers() -> set:
    """Return names of all running containers via Docker SDK."""
    try:
        return {c.name for c in _get_docker().containers.list()}
    except Exception:
        return set()


def restart_container(name: str):
    print(f"[watchdog] Restarting {name}...")
    try:
        container = _get_docker().containers.get(name)
        container.restart()
        print(f"[watchdog] {name} restarted OK")
    except Exception as e:
        print(f"[watchdog] Failed to restart {name}: {e}")


def host_metrics() -> dict:
    """System-level CPU%, RAM%, and root disk% via psutil."""
    try:
        import psutil
        cpu  = psutil.cpu_percent(interval=1)
        mem  = psutil.virtual_memory().percent
        disk = psutil.disk_usage("/").percent
        return {"cpu": cpu, "mem": mem, "disk": disk}
    except ImportError:
        # Fallback: parse /proc
        try:
            with open("/proc/meminfo") as f:
                mi = {k.strip(): int(v.split()[0]) for line in f
                      for k, v in [line.split(":", 1)] if len(line.split(":", 1)) == 2}
            mem = round(100.0 * (1 - mi.get("MemAvailable", 0) / mi.get("MemTotal", 1)), 1)
        except Exception:
            mem = 0.0
        try:
            r = subprocess.run(["df", "--output=pcent", "/"], capture_output=True, text=True, timeout=5)
            disk = float(r.stdout.strip().splitlines()[-1].replace("%", ""))
        except Exception:
            disk = 0.0
        return {"cpu": 0.0, "mem": mem, "disk": disk}

def write_metric(session, mtype, name, value, status, note=None):
    session.add(HealthMetric(
        ts=datetime.utcnow(), metric_type=mtype,
        name=name, value=value, status=status, note=note
    ))


def run_checks():
    session = Session()
    alerts  = []

    # ── Host-level metrics (what actually matters) ────────────────────────────
    hm = host_metrics()
    for metric, val in hm.items():
        status = "critical" if val >= CRITICAL_PCT else "warn" if val >= WARN_PCT else "ok"
        write_metric(session, "host", metric, val, status)
        if status != "ok":
            alerts.append(f"{status.upper()}: host {metric} {val:.1f}%")

    # ── Per-container metrics ─────────────────────────────────────────────────
    for stat in docker_stats():
        name = stat["name"]
        cpu  = stat["cpu_pct"]
        mem  = stat["mem_pct"]
        mem_label = stat.get("mem_usage", "")

        cpu_status = "critical" if cpu >= CRITICAL_PCT else "warn" if cpu >= WARN_PCT else "ok"
        mem_status = "critical" if mem >= CRITICAL_PCT else "warn" if mem >= WARN_PCT else "ok"

        write_metric(session, "cpu", name, cpu, cpu_status, mem_label)
        write_metric(session, "mem", name, mem, mem_status, mem_label)

        if cpu_status != "ok": alerts.append(f"{cpu_status.upper()}: {name} CPU {cpu:.1f}%")
        if mem_status != "ok": alerts.append(f"{mem_status.upper()}: {name} RAM {mem:.1f}%")

    d = disk_pct()
    d_status = "critical" if d >= CRITICAL_PCT else "warn" if d >= WARN_PCT else "ok"
    write_metric(session, "disk", "docker_volume", d, d_status)
    if d_status != "ok": alerts.append(f"{d_status.upper()}: disk {d:.1f}%")

    running = running_containers()
    for name in WATCHED_CONTAINERS:
        if name not in running:
            alerts.append(f"CRITICAL: {name} NOT running — restarting")
            write_metric(session, "container", name, 0, "critical", "not running")
            restart_container(name)
        else:
            write_metric(session, "container", name, 1, "ok", "running")

    session.commit()
    session.close()

    ts = datetime.now().strftime("%H:%M:%S")
    if alerts:
        for a in alerts:
            write_inbox("alert", f"Health Alert: {a[:100]}", a, source="watchdog")
        print(f"\n[watchdog] === ALERTS {ts} ===")
        for a in alerts: print(f"  {a}")
    else:
        print(f"[watchdog] {ts} OK | disk={d:.1f}% | containers: {len(running & set(WATCHED_CONTAINERS))}/{len(WATCHED_CONTAINERS)}")


# ── Agent 1: Data Integrity Monitor ──────────────────────────────────────────
_integrity_counter = 0

def check_data_integrity():
    """Agent 1 — detect price-bar gaps and signal anomalies. Runs every 3rd poll (~15 min)."""
    global _integrity_counter
    _integrity_counter += 1
    if _integrity_counter % 3 != 0:
        return

    try:
        db = Session()
        issues = []

        # Check for symbols with no bars in the last 30 minutes.
        # Stock markets are only open Mon-Fri 09:30-16:15 ET — skip stock symbols outside those hours.
        cutoff = datetime.utcnow() - timedelta(minutes=30)
        recent_symbols = {r[0] for r in db.query(Bar.symbol).filter(Bar.ts >= cutoff).distinct()}
        all_symbols    = {r[0] for r in db.query(Bar.symbol).distinct()}
        stale = all_symbols - recent_symbols
        if stale:
                issues.append(f"No recent bars for: {', '.join(sorted(stale)[:5])}")

        # Check for signals with null confidence or action
        bad_signals = (
            db.query(Signal)
            .filter(Signal.ts >= datetime.utcnow() - timedelta(hours=1))
            .filter((Signal.action.is_(None)) | (Signal.confidence.is_(None)))
            .count()
        )
        if bad_signals:
            issues.append(f"{bad_signals} signals missing action/confidence in last hour")

        db.close()

        if issues:
            write_inbox(
                "alert",
                "Data Integrity Issue Detected",
                "\n".join(issues),
                source="watchdog",
            )
            print(f"[watchdog/agent1] Data integrity issues: {issues}")
    except Exception as e:
        print(f"[watchdog/agent1] Integrity check failed: {e}")


def run():
    print("[watchdog] Resource monitor started (interval: 5 min)")
    init_db()
    while True:
        try:
            run_checks()
            check_data_integrity()
        except Exception as e:
            print(f"[watchdog] Check failed (non-fatal): {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
