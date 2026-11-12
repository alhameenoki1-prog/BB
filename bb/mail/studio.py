"""Manual Studio drafts share one renderer for review and delivery."""

import re
from html import escape
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Comment, Declaration, Doctype, ProcessingInstruction
from starlette.datastructures import UploadFile

from bb.mail.composer import render_recipient_tokens
from bb.mail.providers.base import Attachment
from harvey.integrations.mail_provider import body_to_html, html_to_text, links_to_text

FORMATS = ("rich", "text", "html", "image")
IMAGE_ID = "studio-image"
MAX_FILES = 10
MAX_TOTAL_BYTES = 15_000_000
GRAPH_FILE_LIMIT = 3_000_000

_TAGS = set("a b blockquote br caption center code col colgroup div em h1 h2 h3 h4 h5 h6 hr i img li ol p pre s small span strong sub sup table tbody td tfoot th thead tr u ul".split())
_DROP_CONTENT = {"script", "style", "iframe", "object", "embed", "svg", "math", "template", "head"}
_ATTRIBUTES = {
    "a": {"href", "title"}, "img": {"src", "alt", "title", "width", "height"},
    "table": {"cellpadding", "cellspacing", "width", "align", "role", "border"},
    "td": {"colspan", "rowspan", "width", "height", "align", "valign"},
    "th": {"colspan", "rowspan", "width", "height", "align", "valign", "scope"},
    "col": {"span", "width"},
}
_CSS_PROPERTIES = set(
    "background-color border border-bottom border-collapse border-color border-left border-radius border-right "
    "border-spacing border-style border-top border-width color display font-family font-size font-style font-weight "
    "height letter-spacing line-height margin margin-bottom margin-left margin-right margin-top max-width min-width "
    "padding padding-bottom padding-left padding-right padding-top text-align text-decoration vertical-align width "
    "white-space word-break".split()
)
_CSS_VALUE = re.compile(r"[a-zA-Z0-9#%.,() /'\x22-]+")


def web_url(value: str) -> bool:
    if not value or any(c.isspace() or ord(c) < 32 or c == "\\" for c in value):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname) and parsed.username is None
    except ValueError:
        return False


def _safe_href(value: str) -> bool:
    if web_url(value):
        return True
    return bool(re.fullmatch(r"mailto:[^\s<>@?%]+@[^\s<>@?%]+", value, re.IGNORECASE))


def _style(value: str) -> str:
    kept = []
    for declaration in value.split(";"):
        name, _, setting = declaration.partition(":")
        name, setting = name.strip().lower(), setting.strip()
        if name in _CSS_PROPERTIES and _CSS_VALUE.fullmatch(setting):
            if not re.search(r"\b(?:url|expression|image-set)\s*\(", setting, re.IGNORECASE):
                kept.append(f"{name}: {setting}")
    return "; ".join(kept)


def sanitize_html(source: str) -> str:
    soup = BeautifulSoup(source, "html.parser")
    for node in soup.find_all(string=lambda text: isinstance(text, (Comment, Declaration, Doctype, ProcessingInstruction))):
        node.extract()
    for tag in soup.find_all(_DROP_CONTENT):
        if tag.parent is not None:
            tag.decompose()
    for tag in soup.find_all(True):
        if tag.name not in _TAGS:
            tag.unwrap()
            continue
        allowed = _ATTRIBUTES.get(tag.name, set())
        tag.attrs = {name: str(value) for name, value in tag.attrs.items() if name in allowed or name == "style"}
        if "style" in tag.attrs:
            tag["style"] = _style(tag["style"])
        if tag.name == "a" and not _safe_href(tag.get("href", "")):
            tag.attrs.pop("href", None)
        if tag.name == "img" and not web_url(tag.get("src", "")):
            tag.decompose()
    return str(soup)


def _image_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    raise ValueError("Upload a PNG, JPEG, or GIF for the inline image.")


async def collect_attachments(form, workspace, provider: str, message_format: str) -> list[Attachment]:
    uploads = [item for item in form.getlist("files") if isinstance(item, UploadFile) and item.filename]
    saved = form.getlist("saved_files")
    if any(not isinstance(name, str) for name in saved):
        raise ValueError("Select files from your saved attachments.")
    saved = list(dict.fromkeys(saved))
    inline = form.get("inline_image") if message_format == "image" else None
    if message_format == "image" and (not isinstance(inline, UploadFile) or not inline.filename):
        raise ValueError("Select an image to use the Image format.")
    if len(uploads) + len(saved) + bool(inline) > MAX_FILES:
        raise ValueError(f"Select up to {MAX_FILES} files in total.")
    per_file = GRAPH_FILE_LIMIT - 1 if provider == "graph" else MAX_TOTAL_BYTES
    files = []
    remaining = MAX_TOTAL_BYTES

    def add(name: str, data: bytes, *, is_image: bool = False):
        nonlocal remaining
        if not name:
            raise ValueError("A selected file has no usable name.")
        if len(data) > per_file:
            raise ValueError("Outlook attachments must each be smaller than 3 MB." if provider == "graph"
                             else "The message's files must total 15 MB or less.")
        if len(data) > remaining:
            raise ValueError("The message's files must total 15 MB or less.")
        remaining -= len(data)
        content_type = _image_type(data) if is_image else Attachment.media_type(name)
        files.append(Attachment(name, data, content_type, IMAGE_ID if is_image else ""))

    if inline is not None:
        image_limit = min(per_file, GRAPH_FILE_LIMIT - 1)
        data = await inline.read(image_limit + 1)
        if len(data) > image_limit:
            raise ValueError("The inline image must be smaller than 3 MB.")
        add(workspace.safe_filename(inline.filename), data, is_image=True)
    for upload in uploads:
        add(workspace.safe_filename(upload.filename), await upload.read(min(remaining, per_file) + 1))
    folder = workspace.attachments_dir.resolve()
    for name in saved:
        if not name or workspace.safe_filename(name) != name:
            raise ValueError("Select files from your saved attachments.")
        path = (folder / name).resolve()
        if path.parent != folder or not path.is_file():
            raise ValueError(f"The saved attachment {name} is no longer available.")
        with path.open("rb") as source:
            add(name, source.read(min(remaining, per_file) + 1))
    return files


def message_parts(body: str, recipient: dict, signature: str, message_format: str, *,
                  image: Attachment | None = None, image_link: str = "") -> tuple[str, str]:
    body = render_recipient_tokens(body, recipient, html=message_format == "html")
    signature = sanitize_html(signature)
    if message_format == "html":
        html = sanitize_html(body)
        plain = html_to_text(html)
    elif message_format == "text":
        plain, html = body, ""
    else:
        plain, html = links_to_text(body), body_to_html(body)
    if message_format == "image" and image:
        tag = f'<img src="cid:{IMAGE_ID}" alt="{escape(image.name)}" style="max-width:100%;height:auto">'
        if image_link:
            tag = f'<a href="{escape(image_link, quote=True)}">{tag}</a>'
        html += f"<p>{tag}</p>"
        fallback = f"{image.name} ({image_link})" if image_link else image.name
        plain = "\n\n".join(filter(None, [plain, fallback]))
    signature_text = html_to_text(signature)
    if signature_text:
        plain += f"\n\n-- \n{signature_text}"
    if signature and message_format != "text":
        html += f'<div style="margin-top:18px">{signature}</div>'
    return plain.strip(), html
