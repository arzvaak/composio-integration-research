# Which integrations should Composio build next?

I researched the 100 apps in Composio’s take-home assignment to see where a developer could get credentials and build a private prototype. I’d start with three apps: **Reducto, Paygent Connect, and TranscriptAPI**. All three document a free self-serve path. Of the 25 apps that met that bar and were ready to prototype, the other 22 already had a matching toolkit in Composio’s catalog. [Assignment](https://composio.notion.site/AI-Product-Ops-Intern-The-take-home-assignment-391f261a6dfe80e9946bee899d3a3762)

## What I found

OAuth2 showed up in 71 apps. The more useful question for a builder was who could issue the credentials. Eight of the ten messaging apps had a self-serve route. In support and helpdesk, only three did. Across the full list, 18 of the 39 conditional builds needed a tenant or an administrator’s permission. These verdicts reflect what the vendor documents for a prototype. I didn’t run live integrations against customer accounts.

I tried HDBSCAN and k-medoids to see which apps had similar integration requirements. HDBSCAN grouped only 34 of the 99 apps with enough evidence, so I used k-medoids for the comparison. One group had 41 self-serve apps out of 43. Another had 15 admin- or tenant-gated apps out of 21. The groups overlap, so I used the actual counts to make the shortlist.

I checked the exact products in Composio’s catalog, including aliases and MCP toolkits. One easy match to get wrong was “YouTube Transcript.” The toolkit points to youtubetranscript.dev, while the assignment specifies transcriptapi.com. I kept them separate.

## How I researched each app

I started with a three-app pilot. I used a Composio-managed Firecrawl connection for that pilot. When Firecrawl rate-limited parallel searches on the larger run, I switched to Scrapling running locally in Podman. I used Composio’s Python SDK for toolkit metadata and links to developer docs. Scrapling opened the vendor pages and saved the URLs, excerpts and fetch errors.

I ran three passes at medium reasoning:

1. **GPT-6.2 Sol** drafted eight answers per app: purpose, authentication, commercial access, account and distribution permissions, API type, API breadth, existing MCP servers, and whether I could build a prototype.
2. **GPT-6 Luna** checked Sol’s answers against the cited pages and searched for plan restrictions, sandbox access and deprecated features.
3. **GPT-6.2 Astra** reviewed both passes and checked MCP ownership, distribution rules and approval requirements. The pipeline checked the app identity, the eight required fields and whether the cited URLs appeared in that app’s source packet.

I originally selected ten apps for the audit, one per category. I expanded it to all 100 and manually reviewed the completed results one app at a time. For the field-level comparison, the audit saved a finding, a source, a reviewer and a grade for each original Sol and Astra answer. Sol also served as an automated grader. I kept unresolved grades in the denominator and froze the original answers before reconciliation so later corrections wouldn’t inflate the scores.

## Run it on another list

Install uv, sign in to a Codex CLI with access to `gpt-6.2-sol`, `gpt-6-luna` and `gpt-6.2-astra`, and set `COMPOSIO_API_KEY` in the environment or a local .env file. Install Podman or put a compatible `scrapling-mcp` launcher on PATH. The bundled launcher uses a digest-pinned image. The MCP server needs to expose `make_request` and `browser_fetch_once`. Use `RESEARCH_CODEX_BIN` or `SCRAPLING_MCP_BIN` to select a specific executable.

```sh
./run apps.csv
```

The launcher sets up Python 3.13 and the locked dependencies. For your own list, use `./run my-apps.txt` with one name per line, or pass a CSV with an `app` column. Optional `category` and `hint` columns are useful when a name could refer to several products. The pipeline assigns stable IDs and records the sample before the models run. With names only, it uses a seeded sample of up to ten apps.

Run the same command to resume from saved results. The default is four apps per batch and five concurrent batches. Use `--workers` to change concurrency or `--output <new-directory>` for an independent run. The assignment outputs are in `studies/composio-100/`. Other inputs get a directory based on the normalized list.

## Reproduce the audit and analysis

After the three research passes, install the locked dependencies and generate the reports locally:

```sh
uv venv --python 3.13
uv pip sync requirements.lock
.venv/bin/python full_audit.py studies/composio-100
.venv/bin/python reconcile.py studies/composio-100
uv run cluster_apps.py studies/composio-100
.venv/bin/python toolkit_coverage.py studies/composio-100
.venv/bin/python report.py studies/composio-100
```

The audit reopens sources through Scrapling and preserves completed checks. During reconciliation, Astra revisits disputed answers using the audit findings and source packets. Scrapling looks for more vendor evidence where a question is still open. The catalog step takes a fresh SDK snapshot. Add `--replay` to use the saved catalog instead.

To inspect an individual check, start `.venv/bin/python review.py` and open http://127.0.0.1:8941/. Each check shows the evidence and both model answers. **Mark done & next** saves the grades and rebuilds the report. **Skip for now** leaves the check pending. To rebuild after editing the worksheet directly, run:

```sh
uv run --no-project --python 3.13 --with-requirements requirements.lock report.py studies/composio-100
```

Fresh runs use current vendor pages, model responses and the live Composio catalog. Counts can change. Run the offline recovery test with `.venv/bin/python -m unittest discover`.

## Where to check a result

| Under `studies/composio-100/` | What’s there |
|---|---|
| `manifest.json` | App identities, model settings, batch size and original audit sample |
| `apps/<id>/*-sources.json` | Search queries, candidate URLs, vendor excerpts, fetch methods and errors |
| `apps/<id>/{sol,luna,astra}.json` | Each model’s original answer for the app |
| `batches/<ids>/<stage>/` | Prompts, raw responses, execution details and validation failures |
| `full-audit/` and `human-review.csv` | Source checks and the paired grades |
| `reconciliation/<id>/` | Follow-up sources, correction prompt, model response and revised app row |
| `index.html`, `matrix.json`, `summary.json`, `classifications.csv` | Case study, final answers, counts and categorical data |
| `errors.json` | Batch failures still open at the end of the run |
| `clusters.csv`, `index.html#clusters` | Cluster assignments, representative apps and access patterns |
| `toolkit-coverage.csv` | Matches against the Composio toolkit catalog |
