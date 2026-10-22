"""Run the hosted web app, or manage its accounts from the terminal.

    python -m bb                                  # start the site
    python -m bb create-user EMAIL [--admin]      # make a login (asks for the password)
    python -m bb reset-password EMAIL             # change a login's password
    python -m bb make-admin EMAIL                 # give an existing login the admin role
    python -m bb list-users                       # who exists
    python -m bb status                           # address, data folder, admins, settings file
    python -m bb install-service                  # keep it running: starts at login/boot, restarts if it stops
    python -m bb uninstall-service
    python -m bb doctor                           # ready to publish? address, tunnel, admins, settings, service
    python -m bb update                           # git pull + install + restart, in one go
    python -m bb restart                          # restart the running service on the code now on disk

Settings (BB_BASE_URL, BB_ADMIN_EMAIL, BB_ADMIN_PASSWORD, ...) are read from
the environment, and from bb.env or .env next to the code when present.

Accounts live in BB_HOME/bb.db, the same database the running site reads,
so these work whether or not the server is up.
"""

import argparse
import getpass
import os
import sys
from pathlib import Path

from harvey.paths import PROJECT_ROOT

# Site settings can live in a file next to the code, so a restart in a new
# terminal does not forget them. Exported variables still win.
ENV_FILES = (PROJECT_ROOT / "bb.env", PROJECT_ROOT / ".env")


def load_env_files(files=None) -> list[str]:
    """Read KEY=VALUE lines from the files that exist; never override a
    variable that is already exported. Returns the files that were read."""
    from dotenv import load_dotenv

    read = []
    for path in (ENV_FILES if files is None else files):
        path = Path(path)
        if path.is_file():
            load_dotenv(path, override=False)
            read.append(str(path))
    return read


def _accounts():
    from bb.accounts import Accounts
    from bb.settings import Settings

    settings = Settings()
    return settings, Accounts(settings.accounts_db)


def _password(args) -> str:
    if getattr(args, "password", None):
        return args.password
    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Password (10+ characters): ")
    if getpass.getpass("Again: ") != first:
        print("The two passwords don't match.")
        raise SystemExit(2)
    return first


def startup_report(settings, accounts) -> list[str]:
    """What an operator needs to see when the site starts: where it is,
    where its data lives, who can create logins."""
    users = accounts.list_users()
    admins = [u["email"] for u in users if u["role"] == "admin" and not u["disabled"]]
    lines = [f"BB Outreach at {settings.base_url}  (listening on {settings.host}:{settings.port})",
             f"Accounts and workspaces: {settings.home}"]
    if admins:
        lines.append(f"Admins (they see Users at {settings.base_url}/admin/users): {', '.join(admins)}")
    elif users:
        lines.append("No admin account: nobody can create logins. Fix: python -m bb make-admin EMAIL")
    elif settings.admin_email and settings.admin_password:
        lines.append(f"First start: the admin account {settings.admin_email} will be created now.")
    else:
        lines.append("No accounts and no BB_ADMIN_EMAIL / BB_ADMIN_PASSWORD set. Fix: python -m bb create-user EMAIL --admin")
    if settings.tunnel_mode == "quick":
        lines.append("Tunnel: cloudflared quick tunnel; the public link is printed when it is up and shown on the Users page.")
    elif settings.tunnel_mode == "named":
        lines.append(f"Tunnel: named cloudflared tunnel at {settings.base_url}.")
    elif settings.base_url.startswith("http://localhost"):
        lines.append("Only this computer can reach it. For other people, add BB_TUNNEL=cloudflare to bb.env and install cloudflared (docs/HOSTING.md).")
    return lines


def running_site(port: int) -> dict | None:
    """The site already answering on this port, if any: {version} from
    /health, or {} when something else holds the port."""
    import json as _json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
            data = _json.loads(r.read().decode() or "{}")
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return {}
    except OSError:
        return None


def cmd_serve(args):
    import uvicorn

    from bb.app import create_app
    from bb.workspaces import code_version

    settings, accounts = _accounts()
    port = int(os.environ.get("BB_PORT", "8000"))
    already = running_site(port)
    if already is not None:
        from bb import service
        old = already.get("version") and already["version"] != code_version()
        print(f"The site is already running on port {port}" + (" on OLDER code than the files on disk." if old else "."))
        if service.installed():
            print("It is the login/boot service. To restart it on the current code: python -m bb restart")
        else:
            print("Stop that one first (Ctrl+C in its window), or run `python -m bb restart` if it was started by the service.")
        raise SystemExit(1)
    for line in startup_report(settings, accounts):
        print(line)
    note = keep_mac_awake()
    if note:
        print(note)
    uvicorn.run(create_app(), host=settings.host, port=port, proxy_headers=True,
                forwarded_allow_ips=settings.trusted_proxies)


def keep_mac_awake(pid: int | None = None, platform: str | None = None) -> str:
    """On a Mac, keep it from idling to sleep for as long as this site runs
    (caffeinate -i, tied to this process). A Mac that dozes off stops every
    worker in the middle of its sleep, which looks like a worker that never
    wakes. The login service already does this; a site started by hand now
    does too. Returns a line for the startup report, or ""."""
    import subprocess as _sp
    platform = platform or sys.platform
    caffeinate = Path("/usr/bin/caffeinate")
    if platform != "darwin" or not caffeinate.exists() or os.environ.get("BB_ALLOW_SLEEP"):
        return ""
    try:
        _sp.Popen([str(caffeinate), "-i", "-w", str(pid or os.getpid())], stdin=_sp.DEVNULL,
                  stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, start_new_session=True)
    except OSError:
        return ""
    return ("Keeping this Mac awake while the site runs (it still sleeps if the lid is closed; "
            "set BB_ALLOW_SLEEP=1 to let it sleep).")


def cmd_restart(args):
    from bb import service
    from bb.workspaces import code_version

    port = int(os.environ.get("BB_PORT", "8000"))
    ok, note = service.restart()
    if ok:
        print(f"Service {note}. Waiting for it to answer...")
        import time as _time
        for _ in range(40):
            _time.sleep(0.5)
            site = running_site(port)
            if site and site.get("version"):
                print("Up on the current code." if site["version"] == code_version() else
                      "Up, but still on older code: wait a few seconds and run python -m bb status.")
                return
        print("Started; it is not answering yet. Check the log (python -m bb status shows where).")
        return
    site = running_site(port)
    if site is None:
        print("Nothing is running on the port and no service is installed. Start it with: python -m bb")
    else:
        print(f"No service is installed ({note}); the site was started by hand. Press Ctrl+C in its window, then run python -m bb.")
    raise SystemExit(1)


def cmd_update(args):
    """git pull, install, restart: the whole update in one command."""
    import subprocess as _sp

    from bb import service

    print(f"Updating {PROJECT_ROOT} ...")
    pull = _sp.run(["git", "-C", str(PROJECT_ROOT), "pull", "--ff-only"], capture_output=True, text=True)
    print((pull.stdout or pull.stderr).strip()[-400:])
    if pull.returncode != 0:
        print("git pull failed. If you edited files here, run `git stash` first, then update again.")
        raise SystemExit(1)
    pip = _sp.run([sys.executable, "-m", "pip", "install", "-q", "-e", str(PROJECT_ROOT)], capture_output=True, text=True)
    if pip.returncode != 0:
        print(pip.stderr.strip()[-600:])
        print("pip install failed; the code is updated but a dependency is not.")
        raise SystemExit(1)
    print("Dependencies are current.")
    if service.installed():
        cmd_restart(args)
    else:
        port = int(os.environ.get("BB_PORT", "8000"))
        if running_site(port) is not None:
            print("The site is running from a terminal window: press Ctrl+C there and run python -m bb again, "
                  "or install the service once (python -m bb install-service) so updates restart it for you.")
        else:
            print("Start it with: python -m bb")


SERVICE_ENV_KEYS = ("BB_BASE_URL", "BB_ADMIN_EMAIL", "BB_ADMIN_PASSWORD", "BB_HOME", "BB_PORT", "BB_HOST", "BB_SECRET_KEY",
                    "BB_TUNNEL", "BB_TUNNEL_TOKEN", "BB_CLOUDFLARED", "BB_DEFAULT_MODEL", "BB_DEFAULT_DAILY_COST_USD",
                    "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "REOON_API_KEY", "ZEROBOUNCE_API_KEY", "HUNTER_API_KEY",
                    "SERPER_API_KEY", "PRECLEARED_API_KEY", "ANTHROPIC_BASE_URL")


def fold_exports_into_env_file(path: Path, keys=SERVICE_ENV_KEYS) -> list[str]:
    """A service does not see the shell's exports. Anything exported now that
    bb.env lacks is appended to bb.env so the service starts the same way."""
    existing = ""
    if path.is_file():
        existing = path.read_text()
    present = {line.split("=", 1)[0].strip() for line in existing.splitlines() if "=" in line and not line.lstrip().startswith("#")}
    added = []
    for key in keys:
        value = os.environ.get(key)
        if value and key not in present:
            added.append(key)
            existing += ("" if not existing or existing.endswith("\n") else "\n") + f"{key}={value}\n"
    if added:
        path.write_text(existing)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return added


def cmd_install_service(args):
    from bb import service

    settings, accounts = _accounts()
    env_file = Path(ENV_FILES[0])
    folded = fold_exports_into_env_file(env_file)
    if folded:
        print(f"Added to {env_file} so the service sees them: {', '.join(folded)}")
    try:
        path, notes = service.install(PROJECT_ROOT, settings.home, load=not args.no_start, env={})
    except RuntimeError as e:
        print(e)
        raise SystemExit(2)
    for line in notes:
        print(line)
    if not env_file.is_file():
        print("Note: no bb.env next to the code. The service reads it on every start; put BB_BASE_URL, BB_ADMIN_EMAIL, "
              "BB_ADMIN_PASSWORD (and BB_TUNNEL=cloudflare or BB_TUNNEL_TOKEN) there.")


def _tunnel_handle():
    from bb.tunnel import Tunnel
    settings, _ = _accounts()
    return settings, Tunnel(settings.tunnel_mode, settings.port, token=settings.tunnel_token,
                            public_url=settings.base_url, binary=settings.cloudflared, state_dir=settings.home)


def cmd_tunnel(args):
    settings, tunnel = _tunnel_handle()
    rec = tunnel.recorded_process()
    if args.action == "status":
        if not settings.tunnel_mode:
            print("No tunnel is configured (BB_TUNNEL=cloudflare or BB_TUNNEL_TOKEN in bb.env).")
        elif rec["alive"]:
            print(f"The tunnel is running (cloudflared pid {rec['pid']}). Link: {rec['url'] or settings.base_url}")
            print("It keeps running when the site restarts or updates, so this link stays.")
        else:
            print("No tunnel process is running. The site starts one when it starts; the link appears on the Users page.")
        return
    if args.action in ("stop", "restart"):
        stopped = tunnel.kill_recorded()
        print("Stopped the tunnel." if stopped else "No tunnel process was running.")
        if args.action == "restart":
            print("A running site starts a new one within a few seconds and prints the new link; it is also on the Users "
                  "page and `python -m bb tunnel status`. Send the new link to your users.")


def cmd_uninstall_service(args):
    from bb import service

    try:
        for line in service.uninstall():
            print(line)
    except RuntimeError as e:
        print(e)
        raise SystemExit(2)
    try:
        _settings, tunnel = _tunnel_handle()
        if tunnel.kill_recorded():
            print("Stopped the tunnel the site was running.")
    except Exception:
        pass


def doctor_report(settings, accounts, env_files_read: list[str]) -> list[tuple[str, str]]:
    """Is this site ready for other people? [(ok|warn|fail, line)]."""
    import shutil as _shutil
    out = []
    url = settings.base_url
    if url.startswith("https://"):
        out.append(("ok", f"Public address: {url}"))
    elif settings.tunnel_mode == "quick":
        out.append(("warn", "Quick tunnel: the address changes on every restart. For a link that lasts, use BB_TUNNEL_TOKEN with "
                            "a named Cloudflare tunnel on your domain, or a server (deploy/install.sh)."))
    else:
        out.append(("fail", f"Address {url} is not https and reachable only from this computer. Set BB_TUNNEL=cloudflare, a named "
                            "tunnel (BB_TUNNEL_TOKEN + BB_BASE_URL), or move to a server."))
    if settings.tunnel_mode and not _shutil.which(settings.cloudflared):
        out.append(("fail", f"{settings.cloudflared} is not installed, so the tunnel cannot start (brew install cloudflared)."))
    if settings.tunnel_mode == "named":
        out.append(("ok", "Named tunnel: the address stays the same across restarts."))
    if not url.startswith("http://localhost") and "127.0.0.1" not in url:
        import asyncio as _asyncio
        from bb.linkcheck import check_public_url
        link = _asyncio.run(check_public_url(url, use_cache=False))
        out.append(("ok", f"Link check: {link['detail']}") if link["ok"] else ("fail", f"Link check: {link['detail']}"))
    try:
        import subprocess as _sp
        from bb.tunnel import Tunnel
        own = Tunnel(settings.tunnel_mode, settings.port, token=settings.tunnel_token, public_url=settings.base_url,
                     binary=settings.cloudflared, state_dir=settings.home).recorded_process()
        procs = _sp.run(["pgrep", "-fl", "cloudflared"], capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
        others = [p for p in procs if not (own["alive"] and p.split()[0] == str(own["pid"]))]
        if own["alive"]:
            out.append(("ok", f"The site's tunnel is running (pid {own['pid']}); it survives restarts and updates, so the link "
                              f"{own['url'] or settings.base_url} stays."))
        if others and (len(others) > 1 or own["alive"]):
            out.append(("warn", f"{len(others)} other cloudflared process(es) are running; each has its own address. Stop the "
                                "ones you started by hand and let the site run the tunnel (python -m bb tunnel status)."))
    except Exception:
        pass
    users = accounts.list_users()
    admins = [u["email"] for u in users if u["role"] == "admin" and not u["disabled"]]
    out.append(("ok", f"Admins: {', '.join(admins)}") if admins else ("fail", "No admin account: python -m bb make-admin EMAIL"))
    out.append(("ok", f"Settings file: {', '.join(env_files_read)}") if env_files_read
               else ("warn", "No bb.env next to the code: settings live only in this shell and are lost on restart."))
    secret_file = settings.home / "secret.key"
    out.append(("ok", "Session secret persisted (sessions survive restarts).") if settings._secret or secret_file.exists()
               else ("warn", "No session secret yet; one is written on first start."))
    if settings.gmail_client_id and settings.gmail_client_secret:
        out.append(("ok", "Google OAuth client set: users can connect Gmail in one click."))
    else:
        out.append(("warn", "No Google OAuth client: users connect mailboxes with SMTP settings or an app password instead."))
    try:
        settings.home.mkdir(parents=True, exist_ok=True)
        probe = settings.home / ".write-test"
        probe.write_text("ok"); probe.unlink()
        out.append(("ok", f"Data folder writable: {settings.home}"))
    except OSError as e:
        out.append(("fail", f"Data folder not writable: {settings.home} ({e})"))
    from bb.workspaces import code_version
    site = running_site(settings.port)
    if site is not None and site.get("version") and site["version"] != code_version():
        out.append(("fail", "The running site is on older code than the files on disk: python -m bb restart"))
    from bb import service
    import platform as _platform
    sys_name = _platform.system()
    if sys_name in ("Darwin", "Linux"):
        installed = service.service_paths(sys_name, Path.home()).exists()
        out.append(("ok", "Login/boot service installed: the site restarts on its own.") if installed
                   else ("warn", "Not installed as a service: python -m bb install-service keeps it running after a reboot."))
    return out


def cmd_doctor(args):
    settings, accounts = _accounts()
    rows = doctor_report(settings, accounts, getattr(args, "_env_files_read", None) or [])
    marks = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
    for level, line in rows:
        print(f"[{marks[level]}] {line}")
    fails = sum(1 for level, _ in rows if level == "fail")
    print("Ready for other people." if not fails else f"{fails} thing(s) to fix before other people can use it.")
    raise SystemExit(1 if fails else 0)


def cmd_status(args):
    settings, accounts = _accounts()
    for line in startup_report(settings, accounts):
        print(line)
    from bb.workspaces import code_version
    port = int(os.environ.get("BB_PORT", "8000"))
    site = running_site(port)
    if site is None:
        print(f"Running: no (nothing answers on port {port}).")
    elif site.get("version") == code_version():
        print("Running: yes, on the current code.")
    else:
        print("Running: yes, but on OLDER code than the files on disk. Run: python -m bb restart")
    read = getattr(args, "_env_files_read", None) or []
    print("Settings file: " + (", ".join(read) if read else "none (put BB_BASE_URL, BB_ADMIN_EMAIL, BB_ADMIN_PASSWORD in bb.env next to the code)"))


def cmd_create_user(args):
    settings, accounts = _accounts()
    email = args.email.strip().lower()
    if accounts.get_by_email(email):
        print(f"{email} already exists. Use: python -m bb reset-password {email}")
        raise SystemExit(1)
    try:
        user = accounts.create_user(email, _password(args), role="admin" if args.admin else "user")
    except ValueError as e:
        print(e)
        raise SystemExit(2)
    print(f"Created {user['role']} {user['email']} (accounts database: {settings.accounts_db})")
    print(f"Sign in at {settings.base_url}")


def cmd_reset_password(args):
    settings, accounts = _accounts()
    user = accounts.get_by_email(args.email)
    if not user:
        print(f"No account for {args.email}. Use: python -m bb create-user {args.email}")
        raise SystemExit(1)
    try:
        accounts.set_password(user["id"], _password(args))
    except ValueError as e:
        print(e)
        raise SystemExit(2)
    if user["disabled"]:
        accounts.set_disabled(user["id"], False)
        print(f"{user['email']} was disabled; enabled again.")
    print(f"Password changed for {user['email']}. Their open sessions were signed out.")


def cmd_make_admin(args):
    settings, accounts = _accounts()
    user = accounts.get_by_email(args.email)
    if not user:
        print(f"No account for {args.email}. Use: python -m bb create-user {args.email} --admin")
        raise SystemExit(1)
    accounts.set_role(user["id"], "admin")
    print(f"{user['email']} is now an admin. Sign in again and open {settings.base_url}/admin/users to create logins.")


def cmd_list_users(args):
    settings, accounts = _accounts()
    users = accounts.list_users()
    if not users:
        print(f"No accounts yet in {settings.accounts_db}. Create one: python -m bb create-user EMAIL --admin")
        return
    for u in users:
        print(f"{u['email']:<40} {u['role']:<6} {'disabled' if u['disabled'] else 'active'}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="bb", description="BB Outreach: the hosted web app.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="start the web app (the default)").set_defaults(func=cmd_serve)
    p = sub.add_parser("create-user", help="create a login")
    p.add_argument("email")
    p.add_argument("--admin", action="store_true", help="make this account an admin")
    p.add_argument("--password", help="set it here instead of being asked (visible in shell history)")
    p.set_defaults(func=cmd_create_user)
    p = sub.add_parser("reset-password", help="change a login's password (and re-enable it)")
    p.add_argument("email")
    p.add_argument("--password", help="set it here instead of being asked")
    p.set_defaults(func=cmd_reset_password)
    p = sub.add_parser("make-admin", help="give an existing login the admin role")
    p.add_argument("email")
    p.set_defaults(func=cmd_make_admin)
    sub.add_parser("list-users", help="list accounts").set_defaults(func=cmd_list_users)
    sub.add_parser("status", help="where the site is, where its data lives, who the admins are").set_defaults(func=cmd_status)
    p = sub.add_parser("install-service", help="start the site at login/boot and restart it if it stops (launchd on a Mac, systemd on Linux)")
    p.add_argument("--no-start", action="store_true", help="write the service file but do not start it")
    p.set_defaults(func=cmd_install_service)
    sub.add_parser("uninstall-service", help="remove the login/boot service").set_defaults(func=cmd_uninstall_service)
    sub.add_parser("doctor", help="is the site ready for other people? address, tunnel, admins, settings, service").set_defaults(func=cmd_doctor)
    tun = sub.add_parser("tunnel", help="the public link's tunnel: status, stop, restart (a restart gives a new quick-tunnel link)")
    tun.add_argument("action", choices=["status", "stop", "restart"])
    tun.set_defaults(func=cmd_tunnel)
    sub.add_parser("restart", help="restart the site (the login/boot service) on the code now on disk").set_defaults(func=cmd_restart)
    sub.add_parser("update", help="git pull, install, restart: the whole update in one command").set_defaults(func=cmd_update)
    args = parser.parse_args(argv)
    args._env_files_read = load_env_files()
    (getattr(args, "func", None) or cmd_serve)(args)


if __name__ == "__main__":
    main()
