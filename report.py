"""Render the study, its preregistered paired audit, and a machine-readable matrix."""

import argparse
import json
import csv
import hashlib
import os
from collections import Counter, defaultdict
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from pipeline import FIELDS, load, save

COLUMNS = [
    "id",
    "app",
    "field",
    "human_answer",
    "source_url",
    "sol_grade",
    "astra_grade",
    "notes",
    "reviewer",
]
LABELS = dict(
    zip(
        FIELDS,
        [
            "Category and purpose",
            "Authentication",
            "Commercial access",
            "Account and distribution permissions",
            "API type",
            "API breadth",
            "Existing MCP",
            "Build decision and blocker",
        ],
    )
)


def worksheet(out, manifest):
    path = out / "human-review.csv"
    sampled = [a for a in manifest["apps"] if a["id"] in manifest["sample"]]
    expected = {(a["id"], a["app"], f) for a in sampled for f in FIELDS}
    if not path.exists():
        with path.open("x", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=COLUMNS)
            writer.writeheader()
            for app in sampled:
                for field in FIELDS:
                    writer.writerow(
                        {"id": app["id"], "app": app["app"], "field": field}
                    )
    with path.open(newline="") as file:
        rows = list(csv.DictReader(file))
    full = {(a["id"], a["app"], f) for a in manifest["apps"] for f in FIELDS}
    if {(r["id"], r["app"], r["field"]) for r in rows} == full:
        expected = full
    if (
        len(rows) != len(expected)
        or {(r["id"], r["app"], r["field"]) for r in rows} != expected
    ):
        raise ValueError(
            "Audit sample changed; review the original manifest and worksheet."
        )
    done = []
    for r in rows:
        grades = [r["sol_grade"], r["astra_grade"]]
        if not any(grades):
            continue
        if not all(
            g in {"correct", "incorrect", "unresolved"} for g in grades
        ) or not all(r[k].strip() for k in ["human_answer", "source_url"]):
            raise ValueError(
                "Both grades, human answer, and source required: "
                + r["app"]
                + " / "
                + r["field"]
            )
        done.append(r)
    return {
        "planned": len(rows),
        "apps": len({r["id"] for r in done}),
        "grade_counts": {s: dict(Counter(r[s + "_grade"] for r in done)) for s in ("sol", "astra")},
        "graded": len(done),
        "ai_reviewed": sum(r.get("reviewer") == "AI" for r in done),
        "user_confirmed": sum(r.get("reviewer") != "AI" for r in done),
        "sol_correct": sum(r["sol_grade"] == "correct" for r in done),
        "astra_correct": sum(r["astra_grade"] == "correct" for r in done),
    }


def source_link(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return escape(url)
    return f'<a href="{escape(url, quote=True)}">{escape(parts.hostname or url)}</a>'


def render(out):
    manifest = load(out / "manifest.json")
    audit = worksheet(out, manifest)
    complete = []
    phases = Counter()
    category_gates = defaultdict(Counter)
    reconciled = (out / "reconciliation" / "complete.json").exists()
    for a in manifest["apps"]:
        for stage in manifest["models"]:
            p = out / "apps" / a["id"] / f"{stage}.json"
            if p.exists():
                phases[stage] += 1
        p = out / "apps" / a["id"] / "astra.json"
        if reconciled:
            p = out / "reconciliation" / a["id"] / "reviewed.json"
        if p.exists():
            row = load(p)
            complete.append(row)
            category_gates[a["category"] or row["category"]][row["gate"]] += 1
    decisions = Counter(r["decision"] for r in complete)
    auth = Counter(a for r in complete for a in set(r["auth"]))
    gates = Counter(r["gate"] for r in complete)
    status = Counter(f["status"] for r in complete for f in r["fields"])
    with (out / "human-review.csv").open(newline="") as stream:
        checks = {(r["id"], r["field"]): r for r in csv.DictReader(stream) if r["astra_grade"]}
    resolution = Counter()
    if reconciled:
        for row in complete:
            for field in row["fields"]:
                if checks[row["id"], field["field"]]["astra_grade"] == "unresolved":
                    resolution[field["status"]] += 1
        columns = ["id","app","category","auth","access_mode","gate","api_surface","mcp_availability","decision"]
        temp_csv = out / f"classifications.{os.getpid()}.tmp"
        with temp_csv.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            for row in complete:
                writer.writerow({key: " | ".join(row[key]) if isinstance(row[key], list) else row[key] for key in columns})
        temp_csv.replace(out / "classifications.csv")
    save(out / "matrix.json", complete)
    save(
        out / "summary.json",
        {
            "input": len(manifest["apps"]),
            "complete": len(complete),
            "stage_counts": dict(phases),
            "decisions": decisions,
            "auth": auth,
            "gates": gates,
            "field_status": status,
            "category_gates": category_gates,
            "audit": audit,
            **({"previously_unresolved": dict(resolution)} if reconciled else {}),
        },
    )
    rows = []
    details = []
    for r in complete:
        fields = {f["field"]: f for f in r["fields"]}
        name = escape(r["app"])
        def reviewed_cell(value, field):
            values = value if isinstance(value, list) else [value]
            if not reconciled or "unknown" not in values:
                return escape(", ".join(values))
            finding = fields[field]
            sources = " · ".join(source_link(url) for url in finding["sources"])
            known = [v for v in values if v != "unknown"]
            prefix = escape(", ".join(known)) + ". " if known else ""
            return f"{prefix}{escape(finding['answer'])} <small>{sources}</small>"

        gate = reviewed_cell(r["gate"], "commercial_access")
        auth_cell = reviewed_cell(r["auth"], "auth_methods")
        verdict = reviewed_cell(r["decision"], "buildability_blocker")
        classifications = ""
        if reconciled:
            access = reviewed_cell(r["access_mode"], "commercial_access")
            if r["gate"] == r["access_mode"] == "unknown":
                access = "See credential finding"
            surface = reviewed_cell(r["api_surface"], "api_type")
            classifications = f"<td>{access}</td><td>{surface}</td><td>{escape(r['mcp_availability'])}</td>"
        rows.append(
            f'<tr><th><a href="#app-{r["id"]}">{name}</a></th><td>{escape(r["category"])}</td><td>{auth_cell}</td><td>{gate}</td><td>{verdict}</td>{classifications}</tr>'
        )
        content = []
        for k, f in fields.items():
            links = " · ".join(source_link(s) for s in f["sources"])
            check = checks.get((r["id"], k))
            comparison = ""
            if check:
                comparison = (
                    f"<p><b>Source check</b> · Sol {escape(check['sol_grade'])} · Astra {escape(check['astra_grade'])}"
                    f"<br>{escape(check['human_answer'])} {source_link(check['source_url'])}</p>"
                )
            content.append(
                f"<dt>{LABELS[k]} <small>{escape(f['status'])}</small></dt><dd>{escape(f['answer'])}<p>{escape(f['evidence'])} {links}</p>{comparison}</dd>"
            )
        details.append(
            f'<details id="app-{r["id"]}"><summary>{name}</summary><dl>{"".join(content)}</dl></details>'
        )

    def counts(c):
        return ", ".join(f"{escape(k)} ({n})" for k, n in c.most_common() if k != "unknown")

    def findings(rows, field, label):
        if not rows:
            return ""
        items = []
        for row in rows:
            result = next(f for f in row["fields"] if f["field"] == field)
            links = " · ".join(source_link(url) for url in result["sources"])
            items.append(f'<li><a href="#app-{escape(row["id"])}">{escape(row["app"])}</a> — {escape(result["answer"])} {links}</li>')
        return f"<details><summary>{label} ({len(rows)} apps)</summary><ul>{''.join(items)}</ul></details>"

    open_gates = [row for row in complete if row["gate"] == "unknown"]
    open_auth = [row for row in complete if "unknown" in row["auth"]]
    gate_notes = findings(open_gates, "commercial_access", "Credential requirements needing clarification")
    auth_notes = findings(open_auth, "auth_methods", "Authentication findings with open questions")
    app_categories = {a["id"]: a["category"] for a in manifest["apps"]}
    categories = ""
    for cat, c in sorted(category_gates.items()):
        pending = [r for r in open_gates if (app_categories[r["id"]] or r["category"]) == cat]
        notes = findings(pending, "commercial_access", "Read the credential findings")
        categories += f"<li><b>{escape(cat)}</b> — {counts(c) or 'See findings below'}{notes}</li>"
    measured = (
        f"First pass {audit['sol_correct']}/{audit['graded']} correct. Final pass {audit['astra_correct']}/{audit['graded']} correct."
        if audit["graded"]
        else "Accuracy pending human review."
    )
    scope = ""
    if audit["graded"] and audit["apps"] == len(manifest["apps"]):
        scope = f"The source audit covers all {audit['apps']} apps. Unresolved grades count in each denominator. "
        if (out / "full-audit" / "schema.json").exists():
            scope += "GPT-6.2 Sol graded the additional checks against fresh public source excerpts. "
    patterns = ""
    pattern_file = out / "cluster-summary.json"
    if reconciled and pattern_file.exists():
        result = load(pattern_file)
        if result["input_sha256"] == hashlib.sha256((out / "classifications.csv").read_bytes()).hexdigest():
            pattern_text = "".join(f"<p><b>{escape(label)}.</b> {escape(text)}</p>" for label,text in result["findings"])
            headline_patterns = []
            for label, explanation in result["findings"]:
                if label in ("Authentication", "Category access", "Most common documented blocker", "What clustering adds"):
                    sentences = explanation.split(". ")
                    count = 2 if label in ("Category access", "What clustering adds") else 1
                    headline_patterns.append(f"<p><b>{escape(label)}.</b> {escape('. '.join(sentences[:count]).rstrip('.'))}.</p>")
            methods = result.get("method_html", "")
            patterns = f'<section id="clusters"><h2>Patterns across the apps</h2>{"".join(headline_patterns)}<details><summary>Full findings, outreach list and cluster comparison</summary>{pattern_text}{methods}</details></section>'
    shortlist = ""
    coverage_file = out / "toolkit-coverage.csv"
    if coverage_file.exists():
        with coverage_file.open(newline="") as stream:
            coverage = list(csv.DictReader(stream))
        gaps = [r for r in coverage if r["coverage"] == "No product match"]
        mcp_only = sum(r["coverage"] == "MCP only" for r in coverage)
        easy = [r["app"] for r in gaps if r["gate"] == "free self-serve" and r["decision"] == "prototype"]
        if easy:
            shortlist = f"<p>I’d start with <b>{escape(', '.join(easy))}</b>. A developer can get credentials for free, and I couldn’t find a matching Composio toolkit for any of them.</p>"
        patterns += (
            f"<details><summary>Composio toolkit coverage</summary><p>{len(coverage)-len(gaps)} of {len(coverage)} apps have a matching toolkit in Composio’s catalog, "
            f"including {mcp_only} available only through an MCP toolkit. Of the {len(gaps)} without a product match, "
            f"{sum(r['decision']=='prototype' for r in gaps)} are ready to prototype. Free self-serve candidates: {escape(', '.join(easy))}.</p></details>"

        )
    final_fields = {(r["id"], f["field"]): f for r in complete for f in r["fields"]}
    examples = []
    for before, after in [("incorrect", "correct"), ("incorrect", "incorrect")]:
        check = next((v for v in checks.values() if v["reviewer"] == "User" and (v["sol_grade"], v["astra_grade"]) == (before, after)), None)
        if check:
            finding = final_fields.get((check["id"], check["field"]), {}) if reconciled else {}
            answer = finding.get("answer", check["human_answer"])
            source = next(iter(finding.get("sources", [])), check["source_url"])
            examples.append(f'<p><b>{escape(check["app"])} · {escape(LABELS[check["field"]])}</b> (Sol {before}, Astra {after}). {escape(answer)} {source_link(source)}</p>')
    accuracy = ""
    if audit["graded"]:
        accuracy = f"Across the same {audit['graded']} checks, Sol got {audit['sol_correct']/audit['graded']:.1%} right on the first pass and Astra got {audit['astra_correct']/audit['graded']:.1%} right on the final pass."
    access_questions = findings([r for r in complete if r.get("access_mode") == "unknown"], "commercial_access", "See app-level findings on credential issuance")
    statement_path = out / "author-review.txt"
    statement = escape(statement_path.read_text().strip()) if statement_path.exists() else ""
    machine_data = json.dumps(complete, ensure_ascii=False).replace("<", "\\u003c")
    html = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Integration research | Build decisions</title><style>
*{{box-sizing:border-box}}body{{max-width:74rem;margin:0 auto;padding:2.5rem 4vw;color:#182a2a;background:#fafaf6;font:16px/1.5 system-ui,sans-serif}}h1,h2{{font-family:Georgia,serif;line-height:1.15}}h1{{font-size:clamp(2rem,5vw,3.8rem);margin:.25em 0}}a,summary{{color:#075d51}}summary{{cursor:pointer;font-weight:650}}table{{border-collapse:collapse;width:100%;margin:1rem 0}}td,th{{padding:.55rem;text-align:left;border-bottom:1px solid #c9d5cf}}.scroll{{overflow:auto}}details{{padding:.6rem 0;border-bottom:1px solid #c9d5cf}}dt{{font-weight:bold}}dd{{margin:.25rem 0 1rem}}small,dd p{{color:#465c54}}small{{font-weight:400}}h2{{margin-top:2rem}}input{{font:inherit;padding:.4rem}}:target{{outline:2px solid #168772}}
</style><main><header><small>Integration research · {len(complete)} of {len(manifest["apps"])} apps through all stages</small><h1>Which integrations would I build first?</h1></header>
<p><b>{decisions["prototype"]} ready to prototype · {decisions["conditional"]} conditional · {decisions["outreach"]} need outreach · {decisions["unknown"]} unresolved.</b></p>
{shortlist}{patterns}
<h2>How I checked the results</h2><p>I used the Composio SDK to find developer docs and Scrapling to read them. Sol answered eight questions per app. Luna checked restrictions like paid plans and admin approval, then Astra checked both passes against the docs.</p>
<p><b>{accuracy}</b> {statement}</p>
<details><summary>Scoring details and evidence quality</summary><p>{"The matrix uses the reconciled classifications. Grades apply to the original passes. " if reconciled else ""}{scope}{measured} {audit["graded"]}/{audit["planned"]} paired fields graded. Field evidence labels: {counts(status) or "pending"}. Fields without an established answer: {status["unknown"]}.</p></details>
{"".join(examples)}
<p><a href="https://github.com/arzvaak/composio-integration-research">Source and setup</a> · Run <code>./run apps.csv</code> to research the list.</p>
<details><summary>Authentication and credential counts</summary><p>Most frequently observed authentication methods: {counts(auth) or "pending"}. An app may have several.</p>{auth_notes}<p>Primary credential-access conditions: {counts(gates) or "pending"}.</p>{gate_notes}</details>
<div id="access-findings">{access_questions}</div><details><summary>Access conditions by category</summary><ul>{categories}</ul></details>
<h2>Decisions</h2><label>Find an app <input id="filter" type="search"></label><div class="scroll"><table><thead><tr><th>App</th><th>Category</th><th>Authentication</th><th>Primary access condition</th><th>Decision</th>{"<th>Access mode</th><th>API surface</th><th>MCP availability</th>" if reconciled else ""}</tr></thead><tbody id="apps">{"".join(rows)}</tbody></table></div>
<h2>Evidence by app</h2>{"".join(details)}</main><script type="application/json" id="matrix-data">{machine_data}</script><script>const filter=document.querySelector('#filter');filter.oninput=()=>document.querySelectorAll('#apps tr').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(filter.value.toLowerCase()));function openTarget(){{const t=document.getElementById(location.hash.slice(1));if(t?.tagName==='DETAILS')t.open=true}}addEventListener('hashchange',openTarget);openTarget();</script></html>"""
    temp = out / f"index.{os.getpid()}.tmp"
    temp.write_text(html)
    temp.replace(out / "index.html")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("study", type=Path)
    render(parser.parse_args().study)
