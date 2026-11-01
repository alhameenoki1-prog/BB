"""Explicit destination approval for training-link records."""

from urllib.parse import urlparse

from bb.simulation_links import validate_destination


def validate_approved_destination(value, approved_hostnames):
    """Require HTTPS and an exact, explicitly approved destination hostname."""
    destination = validate_destination(value)
    hostname = urlparse(destination).hostname

    approved = {
        str(item).strip().lower().rstrip(".")
        for item in approved_hostnames
        if str(item).strip()
    }

    if not approved:
        raise ValueError("No training destinations have been approved.")

    if hostname.lower().rstrip(".") not in approved:
        raise ValueError("Destination hostname is not approved.")

    return destination
