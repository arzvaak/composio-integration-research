"""Apply the paired source audit to the final classifications and field answers."""

import argparse
import csv
import fcntl
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from discovery import read_source, scrapling_client, search
from pipeline import load, run_model, save
from report import render

TERMS = {
    "category_purpose": "product overview what it does",
    "commercial_access": "developer API access free plan paid trial credentials pricing",
    "auth_methods": "API authentication OAuth access token API key",
    "permissions_distribution": "developer app permissions external distribution approval marketplace",
    "api_type": "developer API reference REST GraphQL",
    "api_breadth": "API reference resources endpoints",
    "existing_mcp": "official Model Context Protocol MCP server tools",
    "buildability_blocker": "developer API getting started credentials sandbox prerequisites",
}
RULES = (
    "Reconcile the frozen Astra classifications and answers with the source audit. "
    "The field grades concern the original answers. For incorrect fields replace the erroneous claim, and for unresolved fields "
    "use fresh primary sources when available. If a question has several parts, keep supported specifics, state exactly what remains unknown, "
    "and use partial. Use supported only for direct evidence covering the material claim. For correct fields retain the approved meaning. "
    "Give access_mode for independent prototype credential issuance: self-serve includes documented paid signup or local setup; "
    "gated needs vendor, partner, or customer admin approval, mixed has distinct documented paths, unknown means insufficient evidence. "
    "Use first-party documented MCP when vendor ownership is established, third-party documented if only third-party evidence is available, else unverified. "
    "For api_surface list documented interfaces, use HTTP when REST is unestablished, and avoid duplicating REST with generic HTTP. "
    "The assigned category is fixed. Recalculate auth, gate, and decision from the corrected facts, using unknown where needed. "
    "Gate describes the primary access condition for privately prototyping. Free signup alone is insufficient. "
    "An independently usable open-source CLI can be local/open-source and auth none; label any hosted API separately. "
    "Use only identity-matched documentation. An unverified related or similarly named product cannot establish access. "
    "Do not say the reviewer, Sol or Astra found something in the answer. Write the product fact and its limitation."
)


def reconcile(study):
    apps = load(study / "manifest.json")["apps"]
    schema = load(study / "schema.json")
    row_schema = schema["properties"]["rows"]["items"]
    enum_fields = {
        "access_mode": ["self-serve", "gated", "mixed", "unknown"],
        "mcp_availability": [
            "first-party documented",
            "third-party documented",
            "unverified",
        ],
    }
    for name, values in enum_fields.items():
        row_schema["properties"][name] = {"type": "string", "enum": values}
    row_schema["properties"]["api_surface"] = {
        "type": "array",
        "items": {
            "type": "string",
            "enum": [
                "REST",
                "HTTP",
                "GraphQL",
                "RPC",
                "WebSocket",
                "webhooks",
                "CLI",
                "SDK",
                "other",
                "unknown",
            ],
        },
    }
    row_schema["required"] += [*enum_fields, "api_surface"]
    schema_path = study / "reconciliation" / "schema.json"
    save(schema_path, schema)
    with (study / "human-review.csv").open(newline="") as stream:
        audit = {(r["id"], r["field"]): r for r in csv.DictReader(stream)}
    with scrapling_client() as call, ThreadPoolExecutor(12) as pool:

        def process(app):
            ident = app["id"]
            base = study / "apps" / ident
            dest = study / "reconciliation" / ident
            dest.mkdir(exist_ok=True, parents=True)
            out = dest / "reviewed.json"
            if out.exists() and all(k in load(out) for k in row_schema["required"]):
                return
            original = load(base / "astra.json")
            checks = {f["field"]: audit[ident, f["field"]] for f in original["fields"]}
            pages = {}
            for stage in ("sol", "luna", "astra"):
                for p in load(base / (stage + "-sources.json"))["pages"]:
                    if p.get("text"):
                        pages[p["url"]] = p["text"]
            full = study / "full-audit" / ident / "sources.json"
            if full.exists():
                for p in load(full):
                    if len(p.get("text", "")) > 120:
                        pages[p["url"]] = p["text"]
            fresh = dest / "followup-sources.json"
            if not fresh.exists():
                found = {}
                for field, r in checks.items():
                    if r["astra_grade"] != "unresolved":
                        continue
                    host = urlsplit(
                        "https://"
                        + app["hint"].removeprefix("https://").removeprefix("http://")
                    ).hostname
                    if not host or " " in host or "." not in host:
                        host = urlsplit(r["source_url"]).hostname
                    if not host:
                        continue
                    try:
                        _, _, hits = search(
                            call, f"{app['app']} site:{host} {TERMS[field]}"
                        )
                    except (ValueError, RuntimeError, OSError):
                        continue
                    count = 0
                    for hit in hits:
                        u = hit["url"]
                        h = urlsplit(u).hostname or ""
                        if h != host and not h.endswith("." + host):
                            continue
                        if u not in found:
                            found[u] = read_source(call, u, render_thin=False)
                        count += 1
                        if count >= 2:
                            break
                save(fresh, list(found.values()))
            for p in load(fresh):
                if len(p.get("text", "")) > 120:
                    pages[p["url"]] = p["text"]
            # Saved audit findings carry their own citations, including pages checked in Chrome.
            allowed = set(pages) | {r["source_url"] for r in checks.values()}
            evidence = {u: t[:3500] for u, t in pages.items()}
            records = [
                {
                    "app": app,
                    "original": original,
                    "audit": list(checks.values()),
                    "public_pages": evidence,
                }
            ]
            data = run_model(
                "astra",
                [app],
                records,
                dest,
                schema_path,
                {ident: allowed},
                instructions=RULES,
            )
            reviewed = data["rows"][0]
            reviewed["category"] = app["category"] or reviewed["category"]
            old = {f["field"]: f for f in original["fields"]}
            for n, field in enumerate(reviewed["fields"]):
                if checks[field["field"]]["astra_grade"] == "correct":
                    reviewed["fields"][n] = old[field["field"]]
            save(out, reviewed)
            print("Reconciled", ident, flush=True)

        list(pool.map(process, apps))
    with (study / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        save(
            study / "reconciliation" / "complete.json",
            {"apps": [a["id"] for a in apps]},
        )
        render(study)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("study", type=Path)
    reconcile(parser.parse_args().study.resolve())
