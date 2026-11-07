"""Temporary, user-owned AWS role authorization for SES.

Never accepts AWS console passwords or long-lived user access keys.
A tenant-specific ExternalId must be created and stored by BB before
the customer authorizes the role in their AWS account.
"""
import asyncio
import re
from dataclasses import dataclass
from typing import Any

from bb.mail.providers.base import AuthError, TransientError

_ROLE = re.compile(r"^arn:aws(?:-us-gov|-cn)?:iam::([0-9]{12}):role/([A-Za-z0-9+=,.@_/-]{1,512})$")


@dataclass(frozen=True)
class AssumedRole:
    account_id: str
    role_arn: str
    region: str
    session: Any


def validate_role_arn(role_arn: str, expected_account_id: str) -> None:
    match = _ROLE.fullmatch(role_arn)
    if not match or match.group(1) != expected_account_id:
        raise ValueError("AWS role ARN must belong to the authorized 12-digit account")


def assume_ses_role(*, role_arn: str, external_id: str, account_id: str,
                    region: str, session: Any = None) -> AssumedRole:
    """Assume a previously approved cross-account role with ExternalId."""
    validate_role_arn(role_arn, account_id)
    if len(external_id) < 24 or not region:
        raise ValueError("Missing ExternalId or region")
    if session is None:
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError("Install boto3 for SES authorization") from exc
        session = boto3.Session()
    try:
        response = session.client("sts").assume_role(
            RoleArn=role_arn,
            RoleSessionName="bb-ses-connection",
            ExternalId=external_id,
            DurationSeconds=900,
        )
    except Exception as exc:
        code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
        if code in {"AccessDenied", "InvalidClientTokenId", "UnrecognizedClientException"}:
            raise AuthError("AWS role authorization denied") from exc
        raise TransientError("Could not verify AWS role authorization") from exc
    creds = response["Credentials"]
    import boto3
    temporary_session = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )
    return AssumedRole(account_id=account_id, role_arn=role_arn, region=region,
                       session=temporary_session)


async def assume_ses_role_async(**kwargs) -> AssumedRole:
    return await asyncio.to_thread(assume_ses_role, **kwargs)
