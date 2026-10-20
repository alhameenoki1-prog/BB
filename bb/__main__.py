"""Run the hosted web app, or manage its accounts from the terminal.

    python -m bb                                  # start the site
    python -m bb create-user EMAIL [--admin]      # make a login (asks for the password)
    python -m bb reset-password EMAIL             # change a login's password
    python -m bb make-admin EMAIL                 # give an existing login the admin role
    python -m bb list-users                       # who exists
    python -m bb status                           # address, data folder, admins, settings file

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
    lines = [f"BB Outreach at {settings.base_url}  (listening on port {os.environ.get('BB_PORT', '8000')})",
             f"Accounts and workspaces: {settings.home}"]
    if admins:
        lines.append(f"Admins (they see Users at {settings.base_url}/admin/users): {', '.join(admins)}")
    elif users:
        lines.append("No admin account: nobody can create logins. Fix: python -m bb make-admin EMAIL")
    elif settings.admin_email and settings.admin_password:
        lines.append(f"First start: the admin account {settings.admin_email} will be created now.")
    else:
        lines.append("No accounts and no BB_ADMIN_EMAIL / BB_ADMIN_PASSWORD set. Fix: python -m bb create-user EMAIL --admin")
    if settings.base_url.startswith("http://localhost"):
        lines.append("Only this computer can reach it. For other people, run a tunnel and set BB_BASE_URL to its address (docs/HOSTING.md).")
    return lines


def cmd_serve(args):
    import uvicorn

    from bb.app import create_app

    settings, accounts = _accounts()
    for line in startup_report(settings, accounts):
        print(line)
    host = os.environ.get("BB_HOST", "0.0.0.0")
    port = int(os.environ.get("BB_PORT", "8000"))
    uvicorn.run(create_app(), host=host, port=port, proxy_headers=True,
                forwarded_allow_ips="*")


def cmd_status(args):
    settings, accounts = _accounts()
    for line in startup_report(settings, accounts):
        print(line)
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
    args = parser.parse_args(argv)
    args._env_files_read = load_env_files()
    (getattr(args, "func", None) or cmd_serve)(args)


if __name__ == "__main__":
    main()
