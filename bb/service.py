"""Keep the site running: a launchd agent on macOS or a systemd user unit
on Linux that starts `python -m bb` at login/boot and starts it again if
it stops. Written and loaded by `python -m bb install-service`."""

import os
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "com.bb.outreach"


def service_paths(system: str, home: Path) -> Path:
    if system == "Darwin":
        return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    return home / ".config" / "systemd" / "user" / "bb.service"


def _path_env() -> str:
    """A PATH that finds Homebrew's and the system's tools (cloudflared,
    caffeinate) even though a service does not get the shell's PATH."""
    extra = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    current = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    seen, out = set(), []
    for p in current + extra:
        if p not in seen:
            seen.add(p); out.append(p)
    return os.pathsep.join(out)


def launchd_plist(python: str, project_root: Path, log_file: Path, env: dict) -> str:
    import plistlib

    # caffeinate keeps the Mac from idling to sleep while the site runs; the
    # lid still has to stay open (or "prevent sleeping" set in Energy settings).
    program = ["/usr/bin/caffeinate", "-i", python, "-m", "bb"] if Path("/usr/bin/caffeinate").exists() else [python, "-m", "bb"]
    data = {
        "Label": LABEL,
        "ProgramArguments": program,
        "WorkingDirectory": str(project_root),
        "RunAtLoad": True,
        "KeepAlive": True,
        # The tunnel runs in its own session so the public link survives a
        # restart; launchd must not sweep it up with the site.
        "AbandonProcessGroup": True,
        "ThrottleInterval": 10,
        # A background job is throttled by launchd (CPU, disk, timers), and
        # the workers it starts inherit that: run at normal priority.
        "ProcessType": "Interactive",
        "StandardOutPath": str(log_file),
        "StandardErrorPath": str(log_file),
        "EnvironmentVariables": {"PATH": _path_env(), "PYTHONUNBUFFERED": "1", **env},
    }
    return plistlib.dumps(data, sort_keys=False).decode()


def systemd_unit(python: str, project_root: Path, env: dict) -> str:
    lines = ["[Unit]", "Description=BB Outreach web app", "Wants=network-online.target", "After=network-online.target", "",
             "[Service]", f"WorkingDirectory={project_root}", f"ExecStart={shlex.quote(python)} -m bb",
             "Restart=always", "RestartSec=5", "TimeoutStopSec=30",
             # Only the site itself is stopped on restart; the detached cloudflared keeps the public link.
             "KillMode=process", f"Environment=PATH={_path_env()}", "Environment=PYTHONUNBUFFERED=1"]
    for k, v in env.items():
        lines.append(f"Environment={k}={shlex.quote(v)}")
    lines += ["", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def install(project_root: Path, data_home: Path, *, python: str = "", system: str = "", home: Path | None = None,
            load: bool = True, env: dict | None = None) -> tuple[Path, list[str]]:
    """Write the service file and (unless load=False) start it. Returns the
    file written and the lines to show the operator."""
    system = system or platform.system()
    home = home or Path.home()
    python = python or sys.executable
    if system not in ("Darwin", "Linux"):
        raise RuntimeError("This computer has neither launchd nor systemd. Run `python -m bb` from a Task Scheduler job "
                           "(Windows) or keep a terminal open; a Linux server with deploy/install.sh is the hands-off way.")
    tool = "launchctl" if system == "Darwin" else "systemctl"
    if load and not shutil.which(tool):
        raise RuntimeError(f"{tool} is not available here, so the service cannot be started. Write the file only with --no-start, "
                           "or run the site from deploy/install.sh on a server.")
    path = service_paths(system, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    log_file = data_home / "site.log"
    data_home.mkdir(parents=True, exist_ok=True)
    env = dict(env or {})
    notes = []
    if system == "Darwin":
        path.write_text(launchd_plist(python, project_root, log_file, env))
        notes.append(f"Wrote {path}")
        if load:
            uid = os.getuid()
            subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)], capture_output=True)
            r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)], capture_output=True, text=True)
            if r.returncode != 0:
                r = subprocess.run(["launchctl", "load", "-w", str(path)], capture_output=True, text=True)
            notes.append("Started it with launchd." if r.returncode == 0 else f"launchctl said: {(r.stderr or r.stdout).strip()[:200]}")
        notes.append(f"It starts at login and restarts if it stops. Log: {log_file}")
        notes.append("Keep the Mac from sleeping with the lid closed: System Settings → Energy (or Battery) → prevent automatic sleeping on power.")
        notes.append("It runs while this user is logged in: turn on automatic login (System Settings → Users & Groups) so a restart brings it back without a password.")
        notes.append(f"To stop it for good: python -m bb uninstall-service")
    else:
        path.write_text(systemd_unit(python, project_root, env))
        notes.append(f"Wrote {path}")
        if load:
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            r = subprocess.run(["systemctl", "--user", "enable", "--now", "bb.service"], capture_output=True, text=True)
            notes.append("Started it with systemd." if r.returncode == 0 else f"systemctl said: {(r.stderr or r.stdout).strip()[:200]}")
            lg = subprocess.run(["loginctl", "enable-linger", os.environ.get("USER", "")], capture_output=True, text=True)
            if lg.returncode != 0:
                notes.append("Run `sudo loginctl enable-linger $USER` once so it also runs when you are not logged in.")
        notes.append("It starts at boot and restarts if it stops. Logs: journalctl --user -u bb -f")
        notes.append("To stop it for good: python -m bb uninstall-service")
    return path, notes


def installed(*, system: str = "", home: Path | None = None) -> bool:
    system = system or platform.system()
    return system in ("Darwin", "Linux") and service_paths(system, home or Path.home()).exists()


def restart(*, system: str = "", home: Path | None = None) -> tuple[bool, str]:
    """Restart the login/boot service so it runs the code now on disk."""
    system = system or platform.system()
    home = home or Path.home()
    path = service_paths(system, home)
    if not path.exists():
        return False, "no service is installed"
    if system == "Darwin":
        r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, text=True)
        if r.returncode != 0:
            subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
            r = subprocess.run(["launchctl", "load", "-w", str(path)], capture_output=True, text=True)
        return (r.returncode == 0), ("restarted" if r.returncode == 0 else (r.stderr or r.stdout).strip()[:200])
    r = subprocess.run(["systemctl", "--user", "restart", "bb.service"], capture_output=True, text=True)
    return (r.returncode == 0), ("restarted" if r.returncode == 0 else (r.stderr or r.stdout).strip()[:200])


def uninstall(*, system: str = "", home: Path | None = None, unload: bool = True) -> list[str]:
    system = system or platform.system()
    home = home or Path.home()
    path = service_paths(system, home)
    notes = []
    if unload:
        if system == "Darwin":
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True)
            subprocess.run(["launchctl", "unload", "-w", str(path)], capture_output=True)
        else:
            subprocess.run(["systemctl", "--user", "disable", "--now", "bb.service"], capture_output=True)
    if path.exists():
        path.unlink()
        notes.append(f"Removed {path}. The site no longer starts on its own; python -m bb still runs it by hand.")
    else:
        notes.append("No service was installed.")
    return notes
