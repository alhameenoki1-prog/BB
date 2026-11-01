"""User-scoped storage for authorized training-link records."""

import json
import os
import tempfile
from pathlib import Path

from bb.domains import normalize_domain
from bb.simulation_links import (
    new_link_id,
    normalize_path,
    validate_destination,
    validate_name,
    utc_now,
)


def _file(user_data_dir):
    root = Path(user_data_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root / "simulation_links.json"


def list_links(user_data_dir):
    path = _file(user_data_dir)

    if not path.exists():
        return []

    records = json.loads(path.read_text(encoding="utf-8"))

    if not isinstance(records, list):
        raise ValueError("Invalid link storage format.")

    return records


def _save(user_data_dir, records):
    path = _file(user_data_dir)

    fd, temporary = tempfile.mkstemp(
        prefix=".simulation_links_",
        suffix=".tmp",
        dir=str(path.parent),
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(records, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())

        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def create_link(
    user_data_dir,
    *,
    name,
    hostname,
    path,
    destination,
    allowed_hostnames,
    expires_at=None,
):
    name = validate_name(name)
    hostname = normalize_domain(hostname)
    path = normalize_path(path)
    destination = validate_destination(destination)

    approved = {
        normalize_domain(item)
        for item in allowed_hostnames
    }

    if hostname not in approved:
        raise ValueError("The selected hostname is not approved.")

    records = list_links(user_data_dir)

    if any(
        item["hostname"] == hostname and item["path"] == path
        for item in records
    ):
        raise ValueError("This hostname and path are already in use.")

    if expires_at:
        from datetime import datetime, timezone

        try:
            expires = datetime.fromisoformat(
                str(expires_at).replace("Z", "+00:00")
            )
        except ValueError:
            raise ValueError("Invalid expiration timestamp.") from None

        if expires.tzinfo is None:
            raise ValueError("Expiration must include a timezone.")

        if expires <= datetime.now(timezone.utc):
            raise ValueError("Expiration must be in the future.")

        expires_at = expires.isoformat()

    record = {
        "id": new_link_id(),
        "name": name,
        "hostname": hostname,
        "path": path,
        "destination": destination,
        "status": "active",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "expires_at": expires_at,
        "visits": 0,
    }

    records.append(record)
    _save(user_data_dir, records)

    return record


def set_link_status(user_data_dir, link_id, status):
    if status not in ("active", "disabled"):
        raise ValueError("Unsupported link status.")

    records = list_links(user_data_dir)

    for record in records:
        if record["id"] == link_id:
            record["status"] = status
            record["updated_at"] = utc_now()
            _save(user_data_dir, records)
            return record

    raise ValueError("Link not found.")
