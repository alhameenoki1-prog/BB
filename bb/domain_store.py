"""Workspace-scoped domain records for BB."""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from bb.domains import normalize_domain, new_challenge, verification_record
from bb.workspace_file_lock import workspace_file_lock


def _file(workspace_root):
    root = Path(workspace_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root / "managed_domains.json"


def list_domains(workspace_root):
    path = _file(workspace_root)
    if not path.exists():
        return []

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Invalid domain storage format.")

    return data


def add_domain(workspace_root, hostname, provider="cloudflare"):
    hostname = normalize_domain(hostname)

    if provider not in ("cloudflare", "namecheap", "other"):
        raise ValueError("Unsupported DNS provider.")

    with workspace_file_lock(workspace_root, 'managed_domains.json'):
        domains = list_domains(workspace_root)

        if any(item["hostname"] == hostname for item in domains):
            raise ValueError("Domain already registered in this workspace.")

        challenge = new_challenge()
        record = {
            "hostname": hostname,
            "provider": provider,
            "status": "pending_verification",
            "challenge": challenge,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "verified_at": None,
        }

        domains.append(record)
        _save(workspace_root, domains)

        return record


def _save(workspace_root, domains):
    path = _file(workspace_root)

    fd, temporary = tempfile.mkstemp(
        prefix=".managed_domains_",
        suffix=".tmp",
        dir=str(path.parent),
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(domains, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())

        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def domain_instructions(record):
    dns = verification_record(
        record["hostname"],
        record["challenge"],
    )

    provider = record["provider"]

    if provider == "cloudflare":
        steps = [
            "Open the Cloudflare dashboard.",
            "Select the domain's DNS zone.",
            "Navigate to DNS > Records.",
            "Add the TXT record shown below.",
            "Save the record and return to BB to verify ownership.",
        ]
    elif provider == "namecheap":
        steps = [
            "Open Namecheap and select Domain List.",
            "Choose Manage > Advanced DNS.",
            "Add the TXT record shown below.",
            "Save the record and return to BB to verify ownership.",
        ]
    else:
        steps = [
            "Sign in to your DNS hosting provider.",
            "Open the DNS records for your domain.",
            "Add the TXT record shown below.",
            "Save the record and return to BB to verify ownership.",
        ]

    return {
        "steps": steps,
        "record": dns,
    }


def mark_verified(workspace_root, hostname):
    """Save a successful DNS ownership verification."""
    hostname = normalize_domain(hostname)
    with workspace_file_lock(workspace_root, 'managed_domains.json'):
        domains = list_domains(workspace_root)

        for record in domains:
            if record["hostname"] == hostname:
                record["status"] = "verified"
                record["verified_at"] = datetime.now(timezone.utc).isoformat()
                _save(workspace_root, domains)
                return record

        raise ValueError("Domain not found.")
