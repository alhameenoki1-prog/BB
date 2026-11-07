"""Amazon SES v2 send-only transport for user-owned AWS accounts.

This adapter is intentionally not registered as a full MailAccount: SES does
not provide IMAP inbox sync or a Sent folder. Integration into the BB sender
registry follows the separate send-transport refactor.
"""
import asyncio
from dataclasses import dataclass
from typing import Any

from bb.mail.providers.base import AuthError, PermanentError, TransientError


@dataclass(frozen=True)
class SESCapacity:
    region: str
    production_access: bool
    max_24_hour_send: float
    sent_last_24_hours: float
    max_send_rate: float

    @property
    def remaining(self) -> int:
        return max(0, int(self.max_24_hour_send - self.sent_last_24_hours))


class SESAPITransport:
    """Uses AWS SDK's credential chain (prefer assumed-role temporary credentials).

    The calling service must enforce workspace ownership, approved recipients,
    consent, sender identity verification, and per-user quota reservations.
    """

    def __init__(self, *, region: str, session: Any = None):
        if not region or not region.strip():
            raise ValueError("AWS region is required")
        self.region = region.strip()
        self._session = session

    def _client(self):
        if self._session is None:
            try:
                import boto3
            except ImportError as exc:
                raise RuntimeError("Install the optional boto3 dependency for SES API support") from exc
            self._session = boto3.Session()
        return self._session.client("sesv2", region_name=self.region)

    @staticmethod
    def _call(client, operation, **kwargs):
        try:
            return getattr(client, operation)(**kwargs)
        except Exception as exc:
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
            if code in {"AccessDeniedException", "UnauthorizedException", "InvalidClientTokenId",
                        "UnrecognizedClientException", "SignatureDoesNotMatch"}:
                raise AuthError("AWS credentials or SES permissions were rejected") from exc
            if code in {"TooManyRequestsException", "ThrottlingException", "ServiceUnavailableException"}:
                raise TransientError("SES is throttling or temporarily unavailable") from exc
            if code in {"BadRequestException", "NotFoundException", "MessageRejected"}:
                raise PermanentError("SES rejected the request; review identity, recipients and content") from exc
            # Unknown transport failures can have ambiguous acceptance; callers
            # must quarantine uncertain sends rather than replay automatically.
            raise TransientError("SES request failed with uncertain delivery outcome") from exc

    async def capacity(self) -> SESCapacity:
        result = await asyncio.to_thread(self._call, self._client(), "get_account")
        quota = result.get("SendQuota") or {}
        return SESCapacity(
            region=self.region,
            production_access=bool(result.get("ProductionAccessEnabled", False)),
            max_24_hour_send=float(quota.get("Max24HourSend") or 0),
            sent_last_24_hours=float(quota.get("SentLast24Hours") or 0),
            max_send_rate=float(quota.get("MaxSendRate") or 0),
        )

    async def identity(self, email_or_domain: str) -> dict:
        return await asyncio.to_thread(
            self._call, self._client(), "get_email_identity",
            EmailIdentity=email_or_domain,
        )

    async def send_raw(self, *, from_address: str, recipients: list[str],
                       raw_message: bytes, configuration_set: str | None = None) -> str:
        if not from_address or not recipients or not raw_message:
            raise ValueError("Sender, recipients, and MIME content are required")
        if len(recipients) > 50:
            raise ValueError("SES SendEmail supports at most 50 recipients per message")
        args = {
            "FromEmailAddress": from_address,
            "Destination": {"ToAddresses": recipients},
            "Content": {"Raw": {"Data": raw_message}},
        }
        if configuration_set:
            args["ConfigurationSetName"] = configuration_set
        result = await asyncio.to_thread(self._call, self._client(), "send_email", **args)
        return str(result["MessageId"])
