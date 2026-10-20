"""Validated shapes for what the pages post and the worker reads."""

import re

from pydantic import BaseModel, Field, field_validator

PROVIDERS = ("gmail", "imap", "graph")
MODES = ("full", "limited")
STATUSES = ("new", "ok", "reconnect", "error", "disabled")
COLORS = ("blue", "green", "orange", "purple", "pink", "teal", "red", "slate")

_URL_RE = re.compile(r"^https?://[^\s<>\"']+$", re.I)


class MailboxSettings(BaseModel):
    """Per-mailbox settings. ``signature_html`` and ``project_link`` are
    required before anything is sent from the mailbox; they can be blank
    while the account is being set up."""

    display_name: str = ""
    signature_html: str = ""
    project_link: str = ""
    reply_to: str = ""
    persona_notes: str = ""
    auto_send: bool = False
    daily_cap: int = 50
    excluded: list[str] = Field(default_factory=list)   # senders or domains never auto-replied to
    hours_start: str = ""   # "09:00" or blank for any time
    hours_end: str = ""
    color: str = "blue"

    @field_validator("project_link", mode="before")
    @classmethod
    def _link(cls, v) -> str:
        v = str(v or "").strip()
        if v and not v.lower().startswith(("http://", "https://")):
            v = "https://" + v
        if v and not _URL_RE.match(v):
            raise ValueError("The project link must be a web address such as https://example.com.")
        return v

    @field_validator("reply_to", mode="before")
    @classmethod
    def _reply_to(cls, v) -> str:
        v = str(v or "").strip().lower()
        if v and ("@" not in v or " " in v):
            raise ValueError("Reply-To must be an email address.")
        return v

    @field_validator("daily_cap", mode="before")
    @classmethod
    def _cap(cls, v) -> int:
        try:
            v = int(v or 0)
        except (TypeError, ValueError):
            v = 50
        return max(0, min(v, 2000))

    @field_validator("excluded", mode="before")
    @classmethod
    def _excluded(cls, v) -> list[str]:
        if isinstance(v, str):
            v = re.split(r"[\s,;]+", v)
        out = []
        for x in v or []:
            x = str(x).strip().lower().lstrip("@")
            if x and x not in out:
                out.append(x)
        return out

    @field_validator("hours_start", "hours_end", mode="before")
    @classmethod
    def _hhmm(cls, v) -> str:
        v = str(v or "").strip()
        if v and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("Hours must look like 09:00.")
        return v

    @field_validator("color", mode="before")
    @classmethod
    def _color(cls, v) -> str:
        v = str(v or "blue").strip().lower()
        return v if v in COLORS else "blue"

    def ready_to_send(self) -> tuple[bool, str]:
        """The guarantee that every email carries the signature and the
        project link holds only when both exist."""
        missing = []
        if not self.signature_html.strip():
            missing.append("a signature")
        if not self.project_link.strip():
            missing.append("the project link")
        if missing:
            return False, "Set " + " and ".join(missing) + " in this mailbox's settings before sending from it."
        return True, ""
