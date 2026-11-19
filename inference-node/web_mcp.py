#!/usr/bin/env python3
import ipaddress
import json
import re
import socket
import sys
import urllib.parse

import httpx
from lxml import html

MAX_FETCH = 500_000
HTTP_TIMEOUT = 20
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

BLOCKED_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("0.0.0.0/32"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def check_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"scheme must be http or https, got {parsed.scheme}")
    host = parsed.hostname
    if not host:
        raise ValueError("missing hostname")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError(f"cannot resolve hostname: {host}")
    for _, _, _, _, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if any(ip in net for net in BLOCKED_NETS):
            raise ValueError(f"blocked address: {ip}")
    return url


def html_to_text(raw):
    try:
        tree = html.fromstring(raw)
    except Exception:
        return raw
    for bad in tree.xpath("//script|//style|//noscript|//nav|//footer|//header"):
        bad.drop_tree()
    text = tree.text_content()
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def tool_fetch(args):
    url = str(args.get("url", "")).strip()
    if not url:
        raise ValueError("url is required")
    check_url(url)
    with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True, headers={"User-Agent": UA}) as client:
        resp = client.get(url)
        resp.raise_for_status()
        final = str(resp.url)
        if final != url:
            check_url(final)
        ctype = resp.headers.get("content-type", "")
        if "html" in ctype:
            text = html_to_text(resp.text)
        elif ctype.startswith("text/") or "json" in ctype or "xml" in ctype:
            text = resp.text
        else:
            text = f"[non-text content: {ctype}, {len(resp.content)} bytes]"
        truncated = False
        if len(text) > MAX_FETCH:
            text = text[:MAX_FETCH]
            truncated = True
        return json.dumps({
            "url": final,
            "status": resp.status_code,
            "content_type": ctype,
            "content": text,
            "truncated": truncated,
        }, indent=2)


def clean_ddg_url(href):
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        qs = urllib.parse.parse_qs(parsed.query)
        if "uddg" in qs:
            return urllib.parse.unquote(qs["uddg"][0])
    return href


def tool_search(args):
    query = str(args.get("query", "")).strip()
    if not query:
        raise ValueError("query is required")
    with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True, headers={"User-Agent": UA}) as client:
        resp = client.get("https://html.duckduckgo.com/html/", params={"q": query})
        resp.raise_for_status()
    try:
        tree = html.fromstring(resp.text)
    except Exception:
        raise ValueError("could not parse search response")
    results = []
    blocks = tree.xpath('//div[contains(concat(" ", normalize-space(@class), " "), " result ")]')
    for block in blocks[:10]:
        anchors = block.xpath('.//a[contains(@class, "result__a")]')
        if not anchors:
            continue
        a = anchors[0]
        snippets = block.xpath('.//*[contains(@class, "result__snippet")]')
        results.append({
            "title": a.text_content().strip(),
            "url": clean_ddg_url(a.get("href", "")),
            "snippet": snippets[0].text_content().strip() if snippets else "",
        })
    return json.dumps({"query": query, "results": results}, indent=2)


TOOLS = [
    {
        "name": "fetch",
        "description": "Fetch a public URL and return readable text. Use for docs, pages, release notes.",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Full http or https URL."}},
            "required": ["url"],
        },
    },
    {
        "name": "search",
        "description": "Search the public web via DuckDuckGo. Returns top results with title, URL, snippet.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
]

HANDLERS = {"fetch": tool_fetch, "search": tool_search}


def reply(mid, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")
    sys.stdout.flush()


def reply_error(mid, code, message):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            reply(mid, {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "web", "version": "1.0"},
            })
        elif method == "notifications/initialized":
            continue
        elif method == "ping":
            reply(mid, {})
        elif method == "tools/list":
            reply(mid, {"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            handler = HANDLERS.get(name)
            if handler is None:
                reply(mid, {"content": [{"type": "text", "text": f"ERROR: unknown tool {name}"}], "isError": True})
                continue
            try:
                text = handler(args)
                reply(mid, {"content": [{"type": "text", "text": text}], "isError": False})
            except Exception as e:
                reply(mid, {"content": [{"type": "text", "text": f"ERROR: {e}"}], "isError": True})
        elif isinstance(method, str) and method.startswith("notifications/"):
            continue
        elif mid is not None:
            reply_error(mid, -32601, f"method not found: {method}")


if __name__ == "__main__":
    main()
