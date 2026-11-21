"""Studio Autograb substitutions are local and never contact recipients."""
import pytest

from bb.mail.composer import recipient_fields, render_recipient_tokens


@pytest.mark.parametrize(
    ("recipient", "expected"),
    [
        ({"email": "jane.doe@example.test", "name": "Jane Q. Doe"}, "Hello Jane Q. Doe, Jane, jane.doe@example.test"),
        ({"email": "john_smith@example.test"}, "Hello John Smith, John, john_smith@example.test"),
        ({"email": "team42@example.test"}, "Hello , , team42@example.test"),
        ({"email": "a@example.test", "name": "Ada"}, "Hello Ada, Ada, a@example.test"),
    ],
)
def test_autograb_recipient_fields(recipient, expected):
    template = "Hello {{name}}, {{first_name}}, {{email}}"
    assert render_recipient_tokens(template, recipient) == expected


def test_autograb_only_known_tokens_and_preserves_message():
    assert render_recipient_tokens(
        "{{EMAIL}} / {{ name }} / {{company}} / {{password}}",
        {"email": "a@example.test", "name": "Ada"},
    ) == "a@example.test / Ada / {{company}} / {{password}}"


def test_autograb_does_not_modify_template_or_leak_previous_recipient():
    template = "Hi {{name}}, contact {{email}}"
    a = render_recipient_tokens(template, {"name": "Alice", "email": "alice@example.test"})
    b = render_recipient_tokens(template, {"name": "Bob", "email": "bob@example.test"})
    assert a == "Hi Alice, contact alice@example.test"
    assert b == "Hi Bob, contact bob@example.test"
    assert template == "Hi {{name}}, contact {{email}}"


def test_empty_contact_name_is_not_fabricated():
    assert recipient_fields({"email": "1234@example.test"})["name"] == ""
