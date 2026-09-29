"""A deliberately small Markdown renderer for descriptions.

Everything is HTML-escaped first and only a handful of constructs are turned
back into tags afterwards, so user text can never inject markup or script.
Supported: paragraphs, # headings, - and 1. lists, > quotes, ``` code
blocks, `code`, **bold**, _italic_, [links](https://...) and bare URLs.
Links are limited to http, https and mailto.
"""

import html
import re

_HEADING = re.compile(r"^(#{1,4})\s+(.+)$")
_BULLET = re.compile(r"^[-*+]\s+(.+)$")
_NUMBERED = re.compile(r"^\d{1,3}[.)]\s+(.+)$")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]{1,200})\]\(((?:https?://|mailto:)[^\s)]{1,500})\)")
_BARE_URL = re.compile(r"\bhttps?://[^\s<]{2,500}")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])|(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)")
_TOKEN = re.compile("\x00(\\d+)\x00")


def _anchor(href, text):
    return f'<a href="{href}" rel="nofollow ugc noopener noreferrer" target="_blank">{text}</a>'


def render_inline(raw):
    tokens = []

    def stash(fragment):
        tokens.append(fragment)
        return f"\x00{len(tokens) - 1}\x00"

    def bare(match):
        url, trail = match.group(0), ""
        while url and url[-1] in ".,;:!?)'\"":
            trail = url[-1] + trail
            url = url[:-1]
        return stash(_anchor(url, url)) + trail

    text = html.escape(raw.replace("\x00", ""), quote=True)
    text = _CODE_SPAN.sub(lambda m: stash(f"<code>{m.group(1)}</code>"), text)
    text = _LINK.sub(lambda m: stash(_anchor(m.group(2), m.group(1))), text)
    text = _BARE_URL.sub(bare, text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", text)
    return _TOKEN.sub(lambda m: tokens[int(m.group(1))], text)


def render(source):
    if not source:
        return ""
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out, para, quote, items = [], [], [], []
    list_tag = None
    code, in_code = [], False

    def flush_para():
        if para:
            out.append("<p>" + "<br>".join(render_inline(p) for p in para) + "</p>")
            para.clear()

    def flush_quote():
        if quote:
            out.append("<blockquote><p>" + "<br>".join(render_inline(q) for q in quote) + "</p></blockquote>")
            quote.clear()

    def flush_list():
        nonlocal list_tag
        if items:
            body = "".join(f"<li>{render_inline(i)}</li>" for i in items)
            out.append(f"<{list_tag}>{body}</{list_tag}>")
            items.clear()
        list_tag = None

    def flush_all():
        flush_para()
        flush_quote()
        flush_list()

    for line in lines:
        stripped = line.strip()
        if in_code:
            if stripped.startswith("```"):
                out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
                code.clear()
                in_code = False
            else:
                code.append(line)
            continue
        if stripped.startswith("```"):
            flush_all()
            in_code = True
            continue
        if not stripped:
            flush_all()
            continue
        heading = _HEADING.match(stripped)
        if heading:
            flush_all()
            # "#" becomes h3: the page already owns h1 and h2.
            level = min(len(heading.group(1)) + 2, 6)
            out.append(f"<h{level}>{render_inline(heading.group(2))}</h{level}>")
            continue
        bullet, numbered = _BULLET.match(stripped), _NUMBERED.match(stripped)
        if bullet or numbered:
            tag = "ul" if bullet else "ol"
            flush_para()
            flush_quote()
            if list_tag and list_tag != tag:
                flush_list()
            list_tag = tag
            items.append((bullet or numbered).group(1))
            continue
        if stripped.startswith(">"):
            flush_para()
            flush_list()
            quote.append(stripped.lstrip(">").strip())
            continue
        flush_list()
        flush_quote()
        para.append(stripped)

    if in_code:
        out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
    flush_all()
    return "\n".join(out)
