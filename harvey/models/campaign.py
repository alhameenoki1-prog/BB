"""Campaign data model."""

import json
from datetime import datetime, timezone

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    """Naive UTC now (consistent with DB storage; avoids deprecated utcnow)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class EmailStep(BaseModel):
    step: int
    subject: str
    body: str
    delay_days: int = 0  # days after previous step
    # Other wordings of the same email (segment mode writes a few so that
    # a hundred recipients do not get a hundred identical bodies, which
    # mail providers read as bulk). The sender rotates through them.
    subjects: list[str] = Field(default_factory=list)
    bodies: list[str] = Field(default_factory=list)

    def variant(self, key: str) -> tuple[str, str]:
        """(subject, body) for one recipient: a stable pick among the
        variants, the original included, keyed by the recipient id."""
        subjects = [self.subject] + [s for s in self.subjects if s and s != self.subject]
        bodies = [self.body] + [b for b in self.bodies if b and b != self.body]
        n = max(len(subjects), len(bodies))
        if n <= 1:
            return self.subject, self.body
        import zlib
        i = zlib.crc32((key or "").encode()) % n
        return subjects[i % len(subjects)], bodies[i % len(bodies)]


class Campaign(BaseModel):
    id: str
    name: str = ""
    channel: str = "email"  # email/linkedin
    instantly_campaign_id: str = ""
    sequence: list[EmailStep] = Field(default_factory=list)
    status: str = "draft"  # draft/active/paused/completed
    prospect_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)

    def sequence_json(self) -> str:
        return json.dumps([s.model_dump() for s in self.sequence])

    @classmethod
    def sequence_from_json(cls, data: str | None) -> list[EmailStep]:
        """Parse a stored sequence; tolerates NULL/empty/corrupt JSON."""
        if not data:
            return []
        try:
            return [EmailStep(**s) for s in json.loads(data)]
        except (json.JSONDecodeError, TypeError):
            return []
