"""Group the hosted BB pages by task and track their active section."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Page:
    label: str
    path: str
    children: bool = False
    aliases: tuple[str, ...] = ()
    campaign_scoped: bool = False

    def contains(self, path: str) -> bool:
        return path in (self.path, *self.aliases) or (
            self.children and path.startswith(self.path + "/")
        )


@dataclass(frozen=True)
class Section:
    label: str
    icon: str
    pages: tuple[Page, ...]

    @property
    def path(self) -> str:
        return self.pages[0].path


SECTIONS = (
    Section("Overview", "home", (Page("Overview", "/", campaign_scoped=True),)),
    Section("Studio", "send", (
        Page("New email", "/mail/compose"),
        Page("Review queue", "/mail/review"),
    )),
    Section("Campaigns", "play", (
        Page("All campaigns", "/campaigns"),
        Page("Recipients", "/recipients", campaign_scoped=True),
        Page("Outbox", "/outbox", campaign_scoped=True),
    )),
    Section("Inbox", "mail", (
        Page("Threads", "/mail/threads", children=True),
        Page("Reply runs", "/mail/runs", children=True),
        Page("Campaign replies", "/replies", campaign_scoped=True),
        Page("Auto-reply", "/inbox", campaign_scoped=True),
    )),
    Section("Calendar", "calendar", (Page("Invitations", "/mail/studio/calendar"),)),
    Section("Delivery", "activity", (
        Page("Mail status", "/mail"),
        Page("Jobs", "/mail/jobs"),
        Page("Audit log", "/mail/audit"),
    )),
)

SETTINGS = Section("Settings", "settings", (
    Page("Sender accounts", "/mail/accounts", children=True, aliases=("/mail/ses",)),
    Page("Sending providers", "/sending-providers", campaign_scoped=True),
    Page("Campaign mailbox", "/mailbox", campaign_scoped=True),
    Page("Campaign profile", "/profile", campaign_scoped=True),
    Page("AI provider", "/apikey"),
    Page("Account security", "/security/mfa"),
))
ADMIN = Section("Administration", "shield", (
    Page("Users", "/admin/users"),
    Page("Sign-in setup", "/admin/signin"),
    Page("Microsoft diagnostics", "/diagnostics/microsoft"),
))


def workspace_navigation(path: str, is_admin: bool) -> dict:
    settings = (SETTINGS, ADMIN) if is_admin else (SETTINGS,)
    for section in (*SECTIONS, *settings):
        for page in section.pages:
            if page.contains(path):
                return {"sections": SECTIONS, "settings": settings, "section": section, "page": page}
    return {"sections": SECTIONS, "settings": settings, "section": None, "page": None}
