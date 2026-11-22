"""Normalisers behind the kit fingerprints: pure functions over bytes and text.

Scam kits randomise asset file names, CSS class names, ids and per-deployment strings, so every
fingerprint here is computed from content or structure and never from a name. Nothing is
executed: HTML is parsed by lxml into a tree, scripts and stylesheets are treated as text.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from urllib.parse import parse_qsl

from lxml import etree

# --------------------------------------------------------------------------- byte sniffing

_BINARY_MAGIC = (b"PK", b"%PDF", b"\x1aE\xdf\xa3")  # zip, pdf, webm/mkv


def sniff(data: bytes) -> str:
    """Coarse content class from the bytes alone: 'image', 'font', 'binary', 'html' or 'text'."""
    head = data[:16]
    if head.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"\x00\x00\x01\x00")):
        return "image"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image"
    if head[4:12] in (b"ftypavif", b"ftypheic"):
        return "image"
    if head.startswith((b"wOF2", b"wOFF", b"\x00\x01\x00\x00", b"OTTO")):
        return "font"
    if head.startswith(_BINARY_MAGIC) or head[4:8] == b"ftyp" or b"\x00" in data[:4096]:
        return "binary"
    sample = data[:4096].decode("utf-8", "replace")
    if sample.count("�") > len(sample) // 100 + 1:
        return "binary"
    lead = data[:600].lstrip().lower()
    if lead.startswith((b"<!doctype html", b"<html", b"<!-- saved from")) or b"<html" in lead[:300]:
        return "html"
    return "text"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- text normalisation

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
# A line comment starts at a line start or after whitespace or ; { } ( , and never right after ':'
# (so 'https://' survives). Imperfect inside string literals, but deterministic, which is all a
# hash needs: two copies of one file normalise the same way.
_LINE_COMMENT = re.compile(r"(^|[\s;{}(,])//[^\n]*", re.M)
_WS = re.compile(r"\s+")


def strip_comments(text: str) -> str:
    text = _BLOCK_COMMENT.sub("", text)
    text = _HTML_COMMENT.sub("", text)
    return _LINE_COMMENT.sub(r"\1", text)


def normalise_text(text: str) -> str:
    """Comments and all whitespace removed, so re-minified or re-indented copies compare equal."""
    return _WS.sub("", strip_comments(text))


# --------------------------------------------------------------------------- inline scripts

_STRING = re.compile(r"""(["'])(?:\\.|(?!\1)[^\\\n])*\1|`(?:\\.|[^\\`])*`""", re.S)
_DEPLOYMENT = re.compile(
    r"://|www\.|"  # URLs
    r"\b[a-z0-9-]+\.(?:com|net|org|pl|pro|live|click|top|info|io|app|xyz|site|online|store|shop|"
    r"ink|link|space|icu|cc|me|co|biz|eu|de|aws)\b|"  # bare domains
    r"\d{5,}|"  # long numbers: ids, pixel ids, timestamps
    r"[0-9a-fA-F]{8,}",  # hex ids, hashes, uuids
    re.I,
)
_TOKEN_RUN = re.compile(r"[A-Za-z0-9_\-+/=]{12,}")
_RANDOM_IDENT = re.compile(r"\b(?:_0x[0-9a-fA-F]+|[A-Za-z_$][A-Za-z0-9_$]{5,})\b")
_NUMBER = re.compile(r"\d+")


def _is_token(run: str) -> bool:
    return any(c.isdigit() for c in run) and any(c.isalpha() for c in run)


def _per_deployment(body: str) -> bool:
    return bool(_DEPLOYMENT.search(body)) or any(_is_token(m.group(0)) for m in _TOKEN_RUN.finditer(body))


def _literal(match: re.Match[str]) -> str:
    body = match.group(0)[1:-1]
    return '"S"' if _per_deployment(body) else '"' + body + '"'


def _random_ident(match: re.Match[str]) -> str:
    name = match.group(0)
    if name.startswith("_0x") or sum(c.isdigit() for c in name) >= 2:
        return "I"
    return name


def normalise_script(code: str) -> str:
    """Inline-script form that survives per-deployment values and randomised identifiers.

    Comments and whitespace go; string literals holding URLs, domains, ids, tokens or
    timestamps become "S"; identifiers with two or more digits (obfuscator names such as
    `_0x3fa2` or `a7Kx9q`) become I; remaining digit runs become 0.
    """
    code = strip_comments(code)
    parts: list[str] = []
    pos = 0
    for m in _STRING.finditer(code):
        parts.append(_code_part(code[pos : m.start()]))
        parts.append(_NUMBER.sub("0", _literal(m)))
        pos = m.end()
    parts.append(_code_part(code[pos:]))
    return _WS.sub("", "".join(parts))


def _code_part(code: str) -> str:
    return _NUMBER.sub("0", _RANDOM_IDENT.sub(_random_ident, code))


# --------------------------------------------------------------------------- HTML

def parse_html(data: bytes) -> etree._Element | None:
    """Parse HTML into an inert lxml tree (no network, no scripts, no entities fetched)."""
    if not data.strip():
        return None
    parser = etree.HTMLParser(no_network=True, remove_comments=False, remove_pis=True, huge_tree=True)
    try:
        return etree.fromstring(data, parser)
    except (etree.LxmlError, ValueError):
        return None


def _is_element(node: object) -> bool:
    return isinstance(getattr(node, "tag", None), str)


def skeleton_tokens(root: etree._Element) -> list[str]:
    """Pre-order tag sequence with explicit closes; class/id/attribute names and text dropped."""
    tokens: list[str] = []
    for event, el in etree.iterwalk(root, events=("start", "end")):
        if not _is_element(el):
            continue
        tag = el.tag.lower()
        if "}" in tag:  # namespaced (svg/math in XHTML)
            tag = tag.rsplit("}", 1)[1]
        tokens.append(tag if event == "start" else "/")
    return tokens


def skeleton_shingles(tokens: list[str], k: int = 6) -> set[str]:
    return {" ".join(tokens[i : i + k]) for i in range(max(1, len(tokens) - k + 1))}


def inline_scripts(root: etree._Element) -> Iterator[tuple[int, str]]:
    """(index among <script> elements, code) for inline scripts with no src."""
    for n, el in enumerate(root.iter("script")):
        if el.get("src"):
            continue
        typ = (el.get("type") or "").lower()
        if typ and "javascript" not in typ and "module" not in typ and "json" not in typ:
            continue  # templates and other non-script payloads
        code = el.text or ""
        if code.strip():
            yield n, code


_URL_ATTRS = ("src", "href", "action", "data-src", "data-href", "poster", "formaction", "data-url", "content")
URL_IN_TEXT = re.compile(r"""(?:https?:)?//[A-Za-z0-9.\-]+\.[A-Za-z]{2,}(?::\d+)?(?:/[^\s"'<>()\\`]*)?""")


def html_urls(root: etree._Element) -> Iterator[tuple[str, str]]:
    """(url, locator) for absolute or protocol-relative URLs in attributes and inline scripts."""
    for el in root.iter():
        if not _is_element(el):
            continue
        tag = el.tag.lower()
        for attr in _URL_ATTRS:
            value = el.get(attr)
            if value and "//" in value:
                for m in URL_IN_TEXT.finditer(value):
                    yield m.group(0), f"{tag}@{attr}"
        srcset = el.get("srcset")
        if srcset:
            for m in URL_IN_TEXT.finditer(srcset):
                yield m.group(0), f"{tag}@srcset"
    for n, code in inline_scripts(root):
        for m in URL_IN_TEXT.finditer(code):
            yield m.group(0), f"script[{n}]"


def form_actions(root: etree._Element) -> Iterator[tuple[int, str]]:
    for n, el in enumerate(root.iter("form")):
        action = el.get("action")
        if action:
            yield n, action


_SAVED_FROM = re.compile(rb"<!--\s*saved from url=\(\d+\)(\S+?)\s*-->", re.I)


def page_urls(data: bytes, root: etree._Element | None) -> list[tuple[str, str]]:
    """(url, locator) of the page itself: browser 'saved from' comment, canonical, og:url."""
    out: list[tuple[str, str]] = []
    m = _SAVED_FROM.search(data[:4096])
    if m:
        out.append((m.group(1).decode("utf-8", "replace"), "comment:saved-from"))
    if root is not None:
        for el in root.iter("link"):
            if "canonical" in (el.get("rel") or "").lower().split() and el.get("href"):
                out.append((el.get("href"), "link[rel=canonical]"))
        for el in root.iter("meta"):
            if (el.get("property") or "").lower() == "og:url" and el.get("content"):
                out.append((el.get("content"), "meta[og:url]"))
    return [(u.strip(), loc) for u, loc in out if u.strip().startswith(("http://", "https://"))]


# --------------------------------------------------------------------------- URL shapes

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_TOKEN_NAME = re.compile(r"EAA[A-Za-z0-9]{10,}|[A-Za-z0-9_\-]{65,}")


def value_type(value: str) -> str:
    """Type token for a parameter value; the value itself is never kept."""
    if value == "":
        return "{empty}"
    if _UUID.fullmatch(value):
        return "{uuid}"
    if value.isdigit():
        return f"{{num{len(value)}}}"
    if len(value) >= 8 and re.fullmatch(r"[0-9a-f]+", value):
        return f"{{hex{len(value)}}}"
    if re.fullmatch(r"[A-Za-z0-9]+", value):
        return f"{{alnum{len(value)}}}"
    if value.startswith("{") and value.endswith("}"):
        return "{macro}"
    return "{long}" if len(value) > 40 else "{str}"


def param_shape(query: str) -> str | None:
    """Sorted parameter names with value type tokens, e.g. 'a={num3}&b={uuid}'; None if no params.

    A parameter name that itself looks like an access token or an over-long blob becomes {token}.
    """
    pairs = parse_qsl(query, keep_blank_values=True)
    if not pairs:
        return None
    shapes: dict[str, set[str]] = {}
    for name, value in pairs:
        name = "{token}" if _TOKEN_NAME.fullmatch(name) else name
        shapes.setdefault(name, set()).add(value_type(value))
    return "&".join(f"{k}={'|'.join(sorted(v))}" for k, v in sorted(shapes.items()))
