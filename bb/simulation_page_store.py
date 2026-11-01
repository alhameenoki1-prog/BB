"""Workspace-scoped training page drafts; never served as public HTML."""
import json
from pathlib import Path

from bb.workspace_file_lock import workspace_file_lock
from bb.simulation_link_store import list_links, _save


MAX_PAGE_BYTES = 100_000


def get_page(user_data_dir, link_id):
    for link in list_links(user_data_dir):
        if link.get("id") == link_id:
            return link
    raise ValueError("Training link not found.")


def save_page(user_data_dir, link_id, html):
    html = str(html)
    if len(html.encode("utf-8")) > MAX_PAGE_BYTES:
        raise ValueError("Page content is too large.")
    # Keep arbitrary markup as inert draft data. Never return it as a live route.
    with workspace_file_lock(user_data_dir, "simulation_links.json"):
        links = list_links(user_data_dir)
        for link in links:
            if link.get("id") == link_id:
                if link.get("status") != "draft":
                    raise ValueError("Only draft links can be edited.")
                link["page_html"] = html
                from bb.simulation_links import utc_now
                link["updated_at"] = utc_now()
                _save(user_data_dir, links)
                return link
    raise ValueError("Training link not found.")
