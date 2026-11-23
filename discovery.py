"""Discover and reopen public vendor docs through the local Scrapling MCP server."""

import ipaddress
import os
import re
import shutil
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote_plus, unquote, urlsplit

from anyio.from_thread import start_blocking_portal
from bs4 import BeautifulSoup
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TERMS = re.compile(
    r"auth|token|credential|api key|admin|partner|trial|pricing|MCP|endpoint|REST|GraphQL|deprecat|distribut|approval|developer account|command.line",
    re.I,
)
SEARCH_TERMS = {
    "sol": "official developer API reference authentication API key OAuth endpoints",
    "luna": "official API credentials developer free trial paid plan admin partner approval authentication deprecation",
    "astra": "official MCP server developer integration API distribution publishing approval requirements",
}


def public_url(url):
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.port not in (443, None)
        ):
            return False
        addresses = socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)
        return bool(addresses) and all(
            ipaddress.ip_address(a[4][0]).is_global for a in addresses
        )
    except (OSError, ValueError):
        return False


@contextmanager
def scrapling_client():
    command = os.environ.get("SCRAPLING_MCP_BIN") or shutil.which("scrapling-mcp")
    if not command:
        if not shutil.which("podman"):
            raise RuntimeError("Install Podman or set SCRAPLING_MCP_BIN to an existing launcher.")
        command = str(Path(__file__).resolve().parent / "tools" / "scrapling-mcp")
    with ExitStack() as stack:
        portal = stack.enter_context(start_blocking_portal())
        streams = stack.enter_context(
            portal.wrap_async_context_manager(
                stdio_client(StdioServerParameters(command=command))
            )
        )
        client = stack.enter_context(
            portal.wrap_async_context_manager(ClientSession(*streams))
        )
        portal.call(client.initialize)
        available = {item.name for item in portal.call(client.list_tools).tools}
        if not {"make_request", "browser_fetch_once"} <= available:
            raise RuntimeError("Scrapling MCP lacks the required fetch tools.")
        slots = threading.BoundedSemaphore(5)

        def call(tool, args):
            with slots:
                r = portal.call(client.call_tool, tool, args, timedelta(seconds=35))
            if r.isError or not r.structuredContent:
                raise RuntimeError("Scrapling " + tool + " failed")
            return r.structuredContent

        yield call


def catalog_entry(sdk, name):
    slug = re.sub(r"[^a-z0-9]+", "", name.lower())
    try:
        entry = sdk.toolkits.get(slug)
        return {
            "slug": slug,
            "name": entry.name,
            "app_url": entry.meta.app_url,
            "docs_url": entry.docs_url,
            "managed_auth": entry.composio_managed_auth_schemes,
            "connector_tools": entry.meta.tools_count,
        }
    except Exception as exc:
        return {"slug": slug, "lookup_error": type(exc).__name__}


def search(call, query):
    url = "https://search.yahoo.com/search?p=" + quote_plus(query)
    response = call(
        "make_request",
        {
            "method": "GET",
            "url": url,
            "timeout": 14,
            "main_content_only": False,
            "css_selector": "a[data-matarget=algo]",
            "extraction_type": "html",
        },
    )
    soup = BeautifulSoup(" ".join(response.get("content", [])), "html.parser")
    hits = []
    for a in soup.find_all("a", href=True):
        link = a["href"]
        match = re.search(r"/RU=([^/]+)/", link)
        target = unquote(match[1]) if match else link
        if target.startswith("https://") and not urlsplit(target).hostname.endswith(
            "yahoo.com"
        ):
            if target not in {r["url"] for r in hits}:
                hits.append({"url": target, "title": a.get_text(" ", strip=True)[:180]})
    return url, response.get("status"), hits[:10]


def extract(content):
    lines = [" ".join(line.split()) for line in content.splitlines() if line.strip()]
    selected = set(range(min(8, len(lines))))
    for i, line in enumerate(lines):
        if TERMS.search(line):
            selected.update(range(max(0, i - 1), min(len(lines), i + 2)))
    return "\n".join(lines[i][:800] for i in sorted(selected))[:4300]


_BROWSER_LOCK = threading.BoundedSemaphore(2)


def read_source(call, url, *, render_thin=True):
    page = {"url": url, "tool": "make_request"}
    if not public_url(url):
        return {**page, "error": "Unreachable or non-public URL"}
    args = {"url": url, "extraction_type": "markdown", "main_content_only": True}
    try:
        result = call("make_request", {**args, "timeout": 12})
        text = extract("\n".join(result.get("content", [])))
        if render_thin and result.get("status") == 200 and len(text) < 120:
            try:
                with _BROWSER_LOCK:
                    rendered = call(
                        "browser_fetch_once",
                        {
                            **args,
                            "timeout": 14000,
                            "wait": 400,
                            "disable_resources": True,
                            "solve_cloudflare": False,
                        },
                    )
                rendered_text = extract("\n".join(rendered.get("content", [])))
                if len(rendered_text) > len(text):
                    result, text = rendered, rendered_text
                    page["tool"] = "browser_fetch_once"
            except Exception as exc:
                page["render_error"] = type(exc).__name__
        page.update(final_url=result.get("url", url), status=result.get("status"))
        if result.get("status") == 200:
            page["text"] = text
        else:
            page["error"] = "Vendor page unavailable"
    except Exception as exc:
        page["error"] = type(exc).__name__
    return page


def gather(call, app, phase, previous, catalog):
    hint = app.get("hint") or ""
    identity = " ".join(dict.fromkeys(x for x in (app["app"], hint) if x))
    query = f"{identity} {SEARCH_TERMS[phase]}"
    search_url, status, hits = search(call, query)
    seeds = []
    if phase == "sol" and catalog.get("docs_url"):
        seeds.append(catalog["docs_url"])
    hint_url = hint if hint.startswith("https://") else "https://" + hint
    domains = {
        urlsplit(url).hostname
        for url in (
            hint_url,
            catalog.get("app_url") or "",
            catalog.get("docs_url") or "",
        )
        if url
    }
    domains = {host.removeprefix("www.") for host in domains if host}

    def official(hit):
        host = urlsplit(hit["url"]).hostname or ""
        return any(host == domain or host.endswith("." + domain) for domain in domains)

    ranked = sorted(hits, key=lambda hit: not official(hit))
    seeds.extend(hit["url"] for hit in ranked[:4])
    if phase == "sol" and "." in hint and " " not in hint and hint_url not in seeds:
        seeds.append(hint_url)
    if previous:
        seeds.extend(url for field in previous["fields"] for url in field["sources"])
    if not seeds and catalog.get("app_url"):
        seeds.append(catalog["app_url"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        pages = list(pool.map(lambda url: read_source(call, url), dict.fromkeys(seeds)))
    return {
        "provider": "local Scrapling MCP / Podman",
        "search_tool": "make_request",
        "search_url": search_url,
        "query": query,
        "search_status": status,
        "hits": hits,
        "pages": pages,
        "composio_catalog": catalog,
    }
