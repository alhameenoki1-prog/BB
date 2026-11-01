"""Domain eligibility helpers for authorized training links."""

from bb.domains import default_domain, normalize_domain
from bb.domain_store import list_domains


def eligible_hostnames(user_data_dir, base_url):
    """Return the default hostname and DNS-verified user domains."""
    hostnames = set()

    default = default_domain(base_url)

    if default:
        hostnames.add(normalize_domain(default))

    for record in list_domains(user_data_dir):
        if record.get("status") != "verified":
            continue

        try:
            hostname = normalize_domain(record["hostname"])
        except (KeyError, TypeError, ValueError):
            continue

        hostnames.add(hostname)

    return sorted(hostnames)


def domain_options(user_data_dir, base_url):
    """Return selection metadata without claiming routing readiness."""
    default = default_domain(base_url)

    return [
        {
            "hostname": hostname,
            "is_default": hostname == default,
            "ownership_verified": True,
            "routing_ready": False,
            "https_ready": False,
        }
        for hostname in eligible_hostnames(user_data_dir, base_url)
    ]
