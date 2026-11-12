"""Send and reconcile mail using a workspace's authorized AWS role."""

import asyncio
import json
import re
import secrets
from email import policy
from email.utils import parseaddr
from importlib.util import find_spec
from sqlite3 import IntegrityError

from bb.mail.composer import parse_copy_recipients
from bb.mail.providers.base import AuthError, MailAccount, PermanentError
from bb.mail.ses_auth import assume_ses_role_async, validate_role_arn
from bb.mail.ses_transport import SESAPITransport

_REGION = re.compile(r"[a-z][a-z0-9-]{2,30}")
_CONFIG_SET = re.compile(r"[a-zA-Z0-9_-]{1,64}")


def available(settings) -> bool:
    return bool(settings.ses_principal_arn and find_spec("boto3"))


async def external_id(db) -> str:
    await db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('ses_external_id', ?)", (secrets.token_urlsafe(32),))
    return await db.get_setting("ses_external_id")


def trust_policy(principal: str, identifier: str) -> dict:
    return {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"AWS": principal}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"sts:ExternalId": identifier}},
    }]}


def validate_feedback_queue(url: str, account_id: str, region: str) -> str:
    suffix = "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"
    prefix = f"https://sqs.{region}.{suffix}/{account_id}/"
    name = url.removeprefix(prefix)
    if name == url or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", name):
        raise ValueError("Enter a standard SQS queue URL from this AWS account and region.")
    return name


class SESAccount(MailAccount):
    name = "ses"

    def __init__(self, mailbox, creds, env):
        super().__init__(mailbox, creds, env)
        self._assumed = None

    async def role(self):
        if self._assumed is None:
            self._assumed = await assume_ses_role_async(
                role_arn=self.creds["role_arn"], external_id=self.creds["external_id"],
                account_id=self.creds["account_id"], region=self.creds["region"],
            )
        return self._assumed

    async def transport(self) -> SESAPITransport:
        role = await self.role()
        return SESAPITransport(region=role.region, session=role.session)

    async def test(self) -> str:
        transport = await self.transport()
        capacity = await transport.capacity()
        if not capacity.sending_enabled:
            raise AuthError("SES sending is disabled for this account and region.")
        labels = self.address.rsplit("@", 1)[-1].split(".")
        domains = (".".join(labels[offset:]) for offset in range(len(labels) - 1))
        denied = False
        for identity in (self.address, *domains):
            try:
                info = await transport.identity(identity)
            except AuthError:
                denied = True
                continue
            except PermanentError:
                continue
            if info.get("VerifiedForSendingStatus"):
                break
        else:
            if denied:
                raise AuthError("Grant the AWS role ses:GetEmailIdentity access to the verified sending address or domain.")
            raise AuthError("Verify the sending address or its domain in this SES region.")
        tier = "Production access" if capacity.production_access else "Sandbox"
        return (f"{tier} · {capacity.remaining:,} of {int(capacity.max_24_hour_send):,} recipient slots "
                f"available in the rolling 24-hour quota · {capacity.max_send_rate:g}/second · {capacity.region}")

    async def test_feedback_queue(self):
        url = self.creds.get("queue_url")
        if not url:
            return
        role = await self.role()
        queue_name = validate_feedback_queue(url, role.account_id, role.region)
        client = role.session.client("sqs", region_name=role.region)
        response = await asyncio.to_thread(SESAPITransport._call, client, "get_queue_attributes", QueueUrl=url,
                                           AttributeNames=["QueueArn"])
        queue_arn = (response.get("Attributes") or {}).get("QueueArn") or ""
        if not queue_arn.endswith(f":sqs:{role.region}:{role.account_id}:{queue_name}"):
            raise AuthError("AWS did not confirm the feedback queue belongs to this SES connection.")

    async def send(self, *, to, subject, body_text, body_html, in_reply_to="", references="",
                   reply_to="", from_name="", attachments=None, cc=None, bcc=None) -> tuple[str, str, str]:
        if len(to) + len(cc or []) + len(bcc or []) > 50:
            raise PermanentError("Amazon SES allows up to 50 recipients per message.")
        msg = self.build_mime(to=to, cc=cc, subject=subject, body_text=body_text, body_html=body_html,
                              in_reply_to=in_reply_to, references=references, reply_to=reply_to, from_name=from_name,
                              attachments=attachments)
        transport = await self.transport()
        provider_id = await transport.send_raw(
            from_address=self.address, recipients=to, cc=cc, bcc=bcc, raw_message=msg.as_bytes(policy=policy.SMTP),
            configuration_set=self.creds.get("configuration_set"),
        )
        return msg["Message-ID"], provider_id, ""


async def connect_ses(db, secret: str, form, env: dict) -> tuple[str, str]:
    from bb.mail import service

    addresses = parse_copy_recipients(str(form.get("address", "")), field_name="Sender address", limit=1)
    if not addresses:
        raise ValueError("Enter the verified SES sender address.")
    address = addresses[0]
    account_id = str(form.get("account_id", "")).strip()
    role_arn = str(form.get("role_arn", "")).strip()
    validate_role_arn(role_arn, account_id)
    region = str(form.get("region", "")).strip()
    if not _REGION.fullmatch(region):
        raise ValueError("Enter an AWS region, such as us-east-1.")
    config_set = str(form.get("configuration_set", "")).strip()
    if config_set and not _CONFIG_SET.fullmatch(config_set):
        raise ValueError("Enter a valid SES configuration set name.")
    queue_url = str(form.get("queue_url", "")).strip()
    if queue_url:
        validate_feedback_queue(queue_url, account_id, region)
        if not config_set:
            raise ValueError("Enter the SES configuration set that publishes to this feedback queue.")
    creds = {"account_id": account_id, "role_arn": role_arn, "region": region,
             "external_id": await external_id(db), "configuration_set": config_set, "queue_url": queue_url}
    account = SESAccount({"address": address, "provider": "ses"}, creds, env)
    note = await account.test()
    await account.test_feedback_queue()
    sealed = service.crypto.seal_json(secret, creds)
    existing = await db.mailbox_by_address(address, ses=True)
    if existing is None:
        try:
            mailbox_id = await db.add_mailbox(address=address, provider="ses", creds=sealed, mode="send_only", status="ok")
        except IntegrityError:
            existing = await db.mailbox_by_address(address, ses=True)
            if existing is None:
                raise
    if existing:
        mailbox_id = existing["id"]
    await db.update_mailbox(mailbox_id, creds=sealed, status="ok", status_detail=note, enabled=1,
                            sync={"ses_feedback": bool(queue_url)}, last_checked_at=service.now_iso())
    await db.log("connected", f"Connected {address} through Amazon SES in {region}.", mailbox_id)
    return mailbox_id, note


def feedback_event(body: str, *, address: str, account_id: str) -> tuple[str, str, list[str]] | None:
    try:
        event = json.loads(body)
        if isinstance(event, dict) and isinstance(event.get("Message"), str):
            event = json.loads(event["Message"])
        mail = event["mail"]
        provider_id = mail["messageId"]
        sending_account = mail.get("sendingAccountId")
        if sending_account and str(sending_account) != account_id:
            return None
        if not isinstance(provider_id, str) or not provider_id or len(provider_id) > 256:
            return None
        if parseaddr(mail["source"])[1].lower() != address:
            return None
        event_type = event.get("eventType") or event.get("notificationType")
        if event_type == "Delivery":
            status, recipients = "delivered", event["delivery"]["recipients"]
        elif event_type == "Bounce":
            bounce = event["bounce"]
            status = "hard bounce" if bounce.get("bounceType") == "Permanent" else "bounce"
            recipients = [row["emailAddress"] for row in bounce["bouncedRecipients"]]
        elif event_type == "Complaint":
            status = "complaint"
            recipients = [row["emailAddress"] for row in event["complaint"]["complainedRecipients"]]
        elif event_type == "DeliveryDelay":
            status = "delayed"
            recipients = [row["emailAddress"] for row in event["deliveryDelay"]["delayedRecipients"]]
        elif event_type == "Reject":
            status, recipients = "rejected", mail["destination"]
        else:
            return None
        if not isinstance(recipients, list) or len(recipients) > 50 or not all(isinstance(item, str) for item in recipients):
            return None
        return provider_id, status, parse_copy_recipients(", ".join(recipients), field_name="Feedback", limit=50)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


async def poll_feedback(db, secret: str, env: dict, mailbox: dict) -> dict:
    from bb.mail import service
    from bb.mail.runs import ledger_for

    creds = service.open_creds(secret, mailbox)
    queue_url = creds.get("queue_url")
    if not queue_url:
        return {"events": 0}
    role = await SESAccount(mailbox, creds, env).role()
    validate_feedback_queue(queue_url, role.account_id, role.region)
    client = role.session.client("sqs", region_name=role.region)
    ledger = ledger_for(env)
    saved = 0
    for _ in range(10):
        response = await asyncio.to_thread(SESAPITransport._call, client, "receive_message", QueueUrl=queue_url,
                                           MaxNumberOfMessages=10, WaitTimeSeconds=0, VisibilityTimeout=60)
        messages = response.get("Messages") or []
        if not messages:
            break
        for record in messages:
            event = feedback_event(record.get("Body", ""), address=mailbox["address"], account_id=role.account_id)
            if event is None:
                continue
            provider_id, status, recipients = event
            known = await db.one("SELECT 1 FROM messages WHERE mailbox_id = ? AND provider_id = ?",
                                 (mailbox["id"], provider_id))
            if not known or not recipients:
                continue
            for recipient in recipients:
                saved += await db.execute(
                    "INSERT OR IGNORE INTO ses_events(mailbox_id, event_id, provider_id, recipient, event_type, received_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (mailbox["id"], record["MessageId"], provider_id, recipient, status, service.now_iso()),
                )
                if ledger is not None and status in ("hard bounce", "complaint"):
                    ledger.suppress(recipient, reason=f"SES {status}", source="ses")
            # Delete only after the record is durably stored. A repeated SQS
            # delivery is an idempotent insert for the same event/recipient.
            await asyncio.to_thread(SESAPITransport._call, client, "delete_message", QueueUrl=queue_url,
                                    ReceiptHandle=record["ReceiptHandle"])
    return {"events": saved}
