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

    def wordings(self) -> list[tuple[str, str]]:
        """(subject, body) pairs: the original first, then each other
        wording with its own subject (or the original's when it has none).
        A wording whose body repeats one already listed is dropped."""
        out = [(self.subject, self.body)]
        seen = {(self.body or "").strip()}
        for i, body in enumerate(self.bodies):
            text = (body or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            subject = (self.subjects[i] if i < len(self.subjects) else "") or self.subject
            out.append((subject, body))
        return out

    def variant(self, key: str) -> tuple[str, str]:
        """(subject, body) for one recipient: a stable pick among the
        wordings, the original included, keyed by the recipient id."""
        pairs = self.wordings()
        if len(pairs) <= 1:
            return self.subject, self.body
        import zlib
        return pairs[zlib.crc32((key or "").encode()) % len(pairs)]


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
