"""Tests for AWS user-owned role validation and temporary credentials."""
from unittest.mock import Mock

import pytest

from bb.mail.ses_auth import assume_ses_role, validate_role_arn
from bb.mail.providers.base import AuthError


ROLE = "arn:aws:iam::123456789012:role/bb-ses"
EXTERNAL = "workspace-specific-random-external-id-123"


def test_role_rejects_different_account():
    with pytest.raises(ValueError):
        validate_role_arn(ROLE, "999999999999")


def test_role_rejects_user_access_key_arn():
    with pytest.raises(ValueError):
        validate_role_arn("arn:aws:iam::123456789012:user/root", "123456789012")


def test_external_id_required():
    with pytest.raises(ValueError):
        assume_ses_role(role_arn=ROLE, external_id="short", account_id="123456789012",
                        region="us-east-1", session=Mock())


def test_assume_role_uses_external_id_and_short_session():
    sts = Mock()
    sts.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "temporary",
            "SecretAccessKey": "temporary-secret",
            "SessionToken": "temporary-token",
        }
    }
    session = Mock()
    session.client.return_value = sts
    factory = Mock(return_value="temporary-session")
    result = assume_ses_role(role_arn=ROLE, external_id=EXTERNAL,
                             account_id="123456789012", region="us-east-1",
                             session=session, session_factory=factory)
    assert result.session == "temporary-session"
    assert result.account_id == "123456789012"
    assert sts.assume_role.call_args.kwargs["ExternalId"] == EXTERNAL
    assert sts.assume_role.call_args.kwargs["DurationSeconds"] == 900
    assert factory.call_args.kwargs["aws_session_token"] == "temporary-token"


def test_assume_role_denial():
    class Denied(Exception):
        response = {"Error": {"Code": "AccessDenied"}}
    sts = Mock()
    sts.assume_role.side_effect = Denied()
    session = Mock()
    session.client.return_value = sts
    with pytest.raises(AuthError):
        assume_ses_role(role_arn=ROLE, external_id=EXTERNAL,
                        account_id="123456789012", region="us-east-1",
                        session=session, session_factory=Mock())
