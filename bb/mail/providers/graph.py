"""Microsoft 365 through Graph (phase 9). The test refuses cleanly until then."""

from bb.mail.providers.base import MailAccount, TransientError


class GraphAccount(MailAccount):
    name = "graph"

    async def test(self) -> str:
        raise TransientError("Microsoft 365 accounts arrive in a later phase")
