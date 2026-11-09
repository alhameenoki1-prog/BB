"""Offline contract tests: no Microsoft calls and no real invitations."""
import pytest

from bb.mail.teams_calendar import build_teams_event


def valid(**changes):
    args = dict(subject="Project discussion", start="2026-12-01T10:00:00",
                end="2026-12-01T10:30:00", timezone="UTC",
                attendees=["owner@example.test"], description="Review the scope",
                project_url="https://example.test/scope")
    args.update(changes)
    return build_teams_event(**args)


def test_real_graph_event_contract_has_teams_flag_and_attendees():
    event = valid(attendees=["Owner@Example.test", "owner@example.test"])
    assert event["isOnlineMeeting"] is True
    assert event["onlineMeetingProvider"] == "teamsForBusiness"
    assert event["attendees"] == [{"emailAddress": {"address": "owner@example.test"}, "type": "required"}]
    assert "https://example.test/scope" in event["body"]["content"]
    assert event["start"]["timeZone"] == "UTC"


@pytest.mark.parametrize("change", [
    {"start": "2026-12-01T11:00:00"},
    {"attendees": []},
    {"project_url": "javascript:alert(1)"},
    {"project_url": "http://example.test"},
    {"subject": ""},
    {"timezone": ""},
])
def test_invalid_event_rejected_before_any_graph_request(change):
    with pytest.raises(ValueError):
        valid(**change)


def test_description_is_html_escaped():
    event = valid(description="<script>alert('x')</script>")
    assert "<script>" not in event["body"]["content"]
    assert "&lt;script&gt;" in event["body"]["content"]
