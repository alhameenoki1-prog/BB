"""Static checks for the BB Studio compose controls."""
from pathlib import Path

from jinja2 import Environment


def test_studio_compose_template_renders_autograb_tokens_literally():
    template = Path(__file__).resolve().parents[1] / "bb" / "templates" / "mail_compose.html"
    source = template.read_text(encoding="utf-8")
    assert 'data-autograb="&#123;&#123;name&#125;&#125;"' in source
    assert 'data-autograb="&#123;&#123;first_name&#125;&#125;"' in source
    assert 'data-autograb="&#123;&#123;email&#125;&#125;"' in source
    assert 'id="fictional-thread-preview"' in source
    # Parse with Jinja so accidental unescaped braces or malformed blocks fail.
    Environment().parse(source)
