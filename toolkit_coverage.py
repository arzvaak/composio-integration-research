"""Compare the reconciled app list with the paginated Composio-managed catalog."""

import argparse
import csv
import re
from datetime import datetime, timezone
from pathlib import Path

from composio import Composio
from dotenv import load_dotenv
from pipeline import load, save

# Reviewed product aliases and suite-specific toolkits. Zoho exposes CRM lead and deal operations.
ALIASES = {
    "Zoho CRM": ["zoho"],
    "Lark (Larksuite)": ["lark"],
    "WhatsApp Business": ["whatsapp"],
    "GoHighLevel": ["highlevel_agency", "highlevel_location", "highlevel_mcp"],
    "Threads (Meta)": ["threads"],
    "MongoDB Atlas": ["mongodb", "mongodb_mcp"],
    "Monday.com": ["monday", "monday_mcp"],
    "Slack": ["slackbot"],
    "Discord": ["discordbot"],
}
IDENTITY_NOTES = {
    "YouTube Transcript": "The youtube_transcript toolkit identifies youtubetranscript.dev. The requested provider is transcriptapi.com.",
    "Mermaid CLI": "Mermaid Chart MCP is a hosted product. The requested target is the Mermaid CLI.",
    "Amazon Selling Partner": "The Amazon catalog listing does not establish Selling Partner API coverage.",
    "Salesforce Commerce Cloud": "Salesforce CRM, Service Cloud and Data 360 are distinct products.",
}


def normalized(value):
    return re.sub("[^a-z0-9]", "", value.lower())


def snapshot(study):
    sdk = Composio()
    cursor = None
    entries = {}
    seen = set()
    while True:
        page = sdk.toolkits.list(
            cursor=cursor, limit=500, managed_by="composio", sort_by="alphabetically"
        )
        for x in page.items:
            entries[x.slug] = {
                "slug": x.slug,
                "name": x.name,
                "app_url": x.meta.app_url,
                "description": x.meta.description,
                "tools": int(x.meta.tools_count or 0),
            }
        cursor = page.next_cursor
        if not cursor:
            break
        if cursor in seen:
            raise RuntimeError("Repeated toolkit pagination cursor")
        seen.add(cursor)
    catalog = {
        "checked_utc": datetime.now(timezone.utc).isoformat(),
        "pages": 1 + len(seen),
        "entries": list(entries.values()),
    }
    save(study / "toolkit-catalog.json", catalog)
    return catalog


def main(study, replay):
    catalog = load(study / "toolkit-catalog.json") if replay else snapshot(study)
    with (study / "classifications.csv").open(newline="") as file:
        apps = list(csv.DictReader(file))
    rows = []
    for a in apps:
        name = a["app"]
        n = normalized(name)
        matches = [
            x
            for x in catalog["entries"]
            if x["tools"] > 0
            and not (
                name == "YouTube Transcript"
                and "youtubetranscript.dev" in str(x["app_url"])
            )
            and (
                x["slug"] in ALIASES.get(name, [])
                or normalized(x["slug"].removesuffix("_mcp")) == n
                or normalized(x["name"]) == n
            )
        ]
        route = "No product match"
        if matches:
            route = (
                "MCP only"
                if all(x["slug"].endswith("_mcp") for x in matches)
                else "Toolkit"
            )
        rows.append(
            {
                **{k: a[k] for k in ["id", "app", "category", "gate", "decision"]},
                "coverage": route,
                "toolkits": " | ".join(x["slug"] for x in matches),
                "vendor_urls": " | ".join(str(x["app_url"]) for x in matches),
                "toolkit_evidence": " | ".join(
                    f"https://docs.composio.dev/toolkits/{x['slug']}" for x in matches
                ),
                "note": IDENTITY_NOTES.get(name, ""),
            }
        )
    with (study / "toolkit-coverage.csv").open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    covered = [x for x in rows if x["coverage"] != "No product match"]
    gaps = [x for x in rows if x["coverage"] == "No product match"]
    free = [
        x
        for x in rows
        if x["gate"] == "free self-serve" and x["decision"] == "prototype"
    ]
    print(
        len(covered),
        "covered;",
        sum(x["coverage"] == "MCP only" for x in covered),
        "MCP only;",
        len(gaps),
        "without a match.",
    )
    print(
        "Free self-serve gaps:", [r["app"] for r in free if r in gaps], "of", len(free)
    )
    print("Prototype gaps:", [r["app"] for r in gaps if r["decision"] == "prototype"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("study", type=Path)
    parser.add_argument("--replay", action="store_true")
    args = parser.parse_args()
    load_dotenv()
    main(args.study, args.replay)
