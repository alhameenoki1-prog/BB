"""Amazon SES v2 send-only transport for user-owned AWS accounts."""
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
    sending_enabled: bool = False

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

    def _client(self, *, sending: bool = False):
        if self._session is None:
            try:
                import boto3
            except ImportError as exc:
                raise RuntimeError("Install the optional boto3 dependency for SES API support") from exc
            self._session = boto3.Session()
        options = {}
        if sending:
            # A network failure after submission can leave the outcome unknown.
            # The SDK must not automatically submit the same message again.
            try:
                from botocore.config import Config
                options["config"] = Config(retries={"total_max_attempts": 1})
            except ImportError:
                pass  # offline tests supply a stand-in SDK session
        return self._session.client("sesv2", region_name=self.region, **options)

    @staticmethod
    def _call(client, operation, **kwargs):
        try:
            return getattr(client, operation)(**kwargs)
        except Exception as exc:
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
            if code in {"AccessDenied", "AccessDeniedException", "UnauthorizedException", "InvalidClientTokenId",
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
            sending_enabled=bool(result.get("SendingEnabled", False)),
        )

    async def identity(self, email_or_domain: str) -> dict:
        return await asyncio.to_thread(
            self._call, self._client(), "get_email_identity",
            EmailIdentity=email_or_domain,
        )

    async def send_raw(self, *, from_address: str, recipients: list[str],
                       raw_message: bytes, configuration_set: str | None = None,
                       cc: list[str] | None = None, bcc: list[str] | None = None) -> str:
        if not from_address or not recipients or not raw_message:
            raise ValueError("Sender, recipients, and MIME content are required")
        if len(recipients) + len(cc or []) + len(bcc or []) > 50:
            raise ValueError("SES SendEmail supports at most 50 recipients per message")
        destination = {"ToAddresses": recipients}
        if cc:
            destination["CcAddresses"] = cc
        if bcc:
            destination["BccAddresses"] = bcc
        args = {
            "FromEmailAddress": from_address,
            "Destination": destination,
            "Content": {"Raw": {"Data": raw_message}},
        }
        if configuration_set:
            args["ConfigurationSetName"] = configuration_set
        result = await asyncio.to_thread(self._call, self._client(sending=True), "send_email", **args)
        if not result.get("MessageId"):
            raise TransientError("SES returned no message identifier. Check delivery before retrying.")
        return str(result["MessageId"])
