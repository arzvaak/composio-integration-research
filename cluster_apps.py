# /// script
# requires-python = ">=3.11"
# dependencies = ["scikit-learn==1.7.2", "numpy==2.3.5"]
# ///
"""Compare HDBSCAN with a partition around actual app profiles. Run with uv run."""

import argparse
import csv
import hashlib
import json
import warnings
from collections import Counter
from html import escape
from pathlib import Path

import numpy as np
from sklearn.cluster import HDBSCAN
from sklearn.metrics import adjusted_rand_score, silhouette_score


def profile(row):
    def values(field):
        return frozenset(row[field].split(" | ")) - {"", "unknown", "unverified"}

    # Treat generic HTTP and REST as the same transport family for this comparison.
    surface = values("api_surface")
    if "REST" in surface:
        surface = (surface - {"REST"}) | {"HTTP"}
    return [
        values("auth"),
        surface,
        values("access_mode"),
        values("gate"),
        values("mcp_availability"),
    ]


def distances(profiles):
    d = np.zeros((len(profiles), len(profiles)))
    for i, left in enumerate(profiles):
        for j, right in enumerate(profiles[:i]):
            scores = []
            for a, b in zip(left, right):
                scores.append(1 - len(a & b) / len(a | b) if a and b else None)
            access = [s for s in scores[2:4] if s is not None]
            blocks = [
                s
                for s in [*scores[:2], np.mean(access) if access else None, scores[-1]]
                if s is not None
            ]
            d[i, j] = d[j, i] = np.mean(blocks) if blocks else 1
    return d


def pam(d, k):
    # BUILD initialization followed by swaps that strictly reduce total distance.
    medoids = [int(np.argmin(d.sum(axis=0)))]
    while len(medoids) < k:
        nearest = d[:, medoids].min(axis=1)
        gains = (nearest[:, None] - d).clip(min=0).sum(axis=0)
        gains[medoids] = -1
        medoids.append(int(np.argmax(gains)))
    while True:
        nearest = d[:, medoids].min(axis=1)
        cost = nearest.sum()
        best = cost
        swap = None
        for slot in range(k):
            others = medoids[:slot] + medoids[slot + 1 :]
            losses = np.minimum(d[:, others].min(axis=1)[:, None], d).sum(axis=0)
            losses[medoids] = np.inf
            candidate = int(np.argmin(losses))
            if losses[candidate] < best - 1e-9:
                best = losses[candidate]
                swap = slot, candidate
        if swap is None:
            break
        medoids[swap[0]] = swap[1]
    labels = d[:, medoids].argmin(axis=1)
    labels[medoids] = np.arange(k)
    return labels


def density(d, size, samples):
    return HDBSCAN(
        metric="precomputed", min_cluster_size=size, min_samples=samples, copy=True
    ).fit_predict(d)


def metrics(d, labels):
    mask = labels >= 0
    groups = len(set(labels[mask]))
    score = (
        silhouette_score(d[np.ix_(mask, mask)], labels[mask])
        if 1 < groups < mask.sum()
        else float("nan")
    )
    return groups, int(mask.sum()), float(score)


def stability(d, fit, labels):
    rng = np.random.default_rng(42)
    scores = []
    for _ in range(25):
        ids = np.sort(rng.choice(len(d), round(0.8 * len(d)), replace=False))
        subset = fit(d[np.ix_(ids, ids)])

        # Treat noise as singleton observations, so shared rejection earns no cluster agreement.
        def singletons(x):
            return np.where(x >= 0, x, -1 - np.arange(len(x)))

        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="The number of unique classes is greater than 50%.*",
                category=UserWarning,
            )
            scores.append(
                adjusted_rand_score(singletons(labels[ids]), singletons(subset))
            )
    return np.median(scores)


def main(study):
    with (study / "classifications.csv").open(newline="") as f:
        rows = list(csv.DictReader(f))
    profiles = [profile(r) for r in rows]
    ids = [
        i
        for i, (a, s, mode, gate, mcp) in enumerate(profiles)
        if sum(map(bool, (a, s, mode or gate, mcp))) >= 2
    ]
    d = distances([profiles[i] for i in ids])
    trials = []
    for k in range(3, 9):
        labels = pam(d, k)
        trials.append(("PAM", str(k), labels, metrics(d, labels)))
    for size in (5, 8, 12):
        for samples in (3, 5):
            labels = density(d, size, samples)
            trials.append(("HDBSCAN", f"{size}/{samples}", labels, metrics(d, labels)))

    # For HDBSCAN, select an informative fit with at least 50% coverage if possible.
    def choose(method):
        pool = [t for t in trials if t[0] == method and np.isfinite(t[3][2])]
        if not pool:
            return None
        eligible = [t for t in pool if t[3][1] >= len(d) / 2]
        return max(eligible or pool, key=lambda t: (t[3][2], t[3][1]))

    partition = choose("PAM")
    dense = choose("HDBSCAN")
    e = escape

    def grade(t):
        if t is None:
            return "No fit yielded two scorable groups."
        method, setting, labels, (n, covered, sil) = t
        if method == "PAM":
            fit = lambda x: pam(x, int(setting))
        else:
            size, samples = map(int, setting.split("/"))
            fit = lambda x: density(x, size, samples)
        ari = stability(d, fit, labels)
        return f"{n} clusters, {covered}/{len(d)} assigned, silhouette {sil:.3f}, median resampling ARI {ari:.3f}."

    records = [
        {
            **r,
            "pam_cluster": "insufficient evidence",
            "hdbscan_cluster": "insufficient evidence",
        }
        for r in rows
    ]
    groups = []
    for t, key in [(partition, "pam_cluster"), (dense, "hdbscan_cluster")]:
        if t is None:
            continue
        labels = t[2]
        for pos, i in enumerate(ids):
            records[i][key] = str(labels[pos] + 1) if labels[pos] >= 0 else "noise"
        for cluster in sorted(set(labels) - {-1}):
            members = np.flatnonzero(labels == cluster)
            rep = members[np.argmin(d[np.ix_(members, members)].sum(axis=1))]
            apps = [rows[ids[m]] for m in members]

            def common(field):
                name, count = Counter(a[field] for a in apps).most_common(1)[0]
                return f"{name} ({count}/{len(apps)})"

            examples = ", ".join(
                rows[ids[m]]["app"]
                for m in sorted(members, key=lambda x: d[rep, x])[:4]
            )
            groups.append(
                "<tr>"
                + "".join(
                    f"<td>{e(str(v))}</td>"
                    for v in [
                        t[0],
                        cluster + 1,
                        len(apps),
                        rows[ids[rep]]["app"],
                        common("gate"),
                        common("access_mode"),
                        examples,
                    ]
                )
                + "</tr>"
            )
    with (study / "clusters.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)
    comparison = "".join(
        f"<tr><td>{m}</td><td>{setting}</td><td>{n}</td><td>{c}</td><td>{s:.3f}</td></tr>"
        for m, setting, _, (n, c, s) in trials
    )
    # Show observed build outcomes after forming clusters without verdicts.
    buckets = {}
    for row in records:
        if row["pam_cluster"] != "insufficient evidence":
            buckets.setdefault(row["pam_cluster"], []).append(row)
    ready = max(
        buckets.values(),
        key=lambda group: sum(r["decision"] == "prototype" for r in group),
    )
    admin = max(
        buckets.values(),
        key=lambda group: sum(r["gate"] == "admin/tenant" for r in group),
    )
    auth_counts = Counter(v for r in rows for v in set(r["auth"].split(" | ")))
    common_auth = auth_counts.most_common(3)
    category_counts = {}
    for r in rows:
        category_counts.setdefault(r["category"], Counter())[r["access_mode"]] += 1

    def leader(mode):
        return max(
            category_counts.items(), key=lambda item: item[1][mode] / item[1].total()
        )

    easy_cat, easy_counts = leader("self-serve")
    gated_cat, gated_counts = leader("gated")
    conditional = [r for r in rows if r["decision"] == "conditional"]
    blockers = Counter(r["gate"] for r in conditional if r["gate"] != "unknown")
    blocker, nblock = blockers.most_common(1)[0]
    easy = [
        r
        for r in rows
        if r["decision"] == "prototype" and r["gate"] == "free self-serve"
    ]
    outreach = [r for r in rows if r["decision"] == "outreach"]
    examples = [
        r["app"] for r in easy if r["app"] in ("Attio", "Telegram", "Firecrawl")
    ]
    findings = [
        [
            "Authentication",
            f"{common_auth[0][0]} leads with {common_auth[0][1]} of {len(rows)} apps. The next most frequent are {common_auth[1][0]} ({common_auth[1][1]}) and {common_auth[2][0]} ({common_auth[2][1]}). Apps can support several methods.",
        ],
        [
            "Category access",
            f"{easy_cat} has the highest self-serve share ({easy_counts['self-serve']}/{easy_counts.total()}). {gated_cat} has the highest explicitly gated share ({gated_counts['gated']}/{gated_counts.total()}); {gated_counts['self-serve']} are self-serve. The category table separates apps with multiple access routes from those whose docs leave credential issuance open.",
        ],
        [
            "Most common documented blocker",
            f"{nblock} of {len(conditional)} conditional apps have {blocker} as the primary gate. For these apps, securing a customer workspace or an administrator’s permission comes before testing.",
        ],
        [
            "Where to start",
            f"{len(easy)} apps combine a free self-serve credential path with a prototype verdict. Examples include {', '.join(examples or [r['app'] for r in easy[:3]])}. These are candidates for a private first call; customer distribution has separate requirements.",
        ],
        [
            "Where outreach pays off",
            f"{len(outreach)} apps have an outreach verdict. {sum(r['gate'] == 'partner/sales' for r in outreach)} list partner or sales access as the primary gate. The outreach queue is {', '.join(r['app'] for r in outreach)}.",
        ],
        [
            "What clustering adds",
            f"K-medoids grouped {len(ready)} apps together, including {sum(r['access_mode'] == 'self-serve' for r in ready)} self-serve and {sum(r['decision'] == 'prototype' for r in ready)} prototype-ready apps. Another group contains {sum(r['gate'] == 'admin/tenant' for r in admin)} admin- or tenant-gated apps among {len(admin)} members. Build verdicts were held out of clustering. The modest silhouette ({partition[3][2]:.2f}) makes these descriptive groups with overlapping boundaries.",
        ],
    ]
    summary = {
        "input_sha256": hashlib.sha256(
            (study / "classifications.csv").read_bytes()
        ).hexdigest(),
        "findings": findings,
    }
    category_table = "".join(
        "<tr>"
        + f"<th>{e(category)}</th>"
        + "".join(
            f"<td>{counts[m]}</td>" for m in ("self-serve", "gated", "mixed", "unknown")
        )
        + "</tr>"
        for category, counts in category_counts.items()
    )
    body = f"""<p>{len(d)} of {len(rows)} apps had enough evidence to compare.</p><details><summary>Access counts for all categories</summary><table><tr><th>Category</th><th>Self-serve</th><th>Gated</th><th>Mixed</th><th>Credential issuance unclear (<a href="#access-findings">see findings</a>)</th></tr>{category_table}</table></details><h2>Method comparison</h2><p><b>K-medoids (PAM)</b> {grade(partition)}</p><p><b>HDBSCAN</b> {grade(dense)}</p>
<p>PAM gives each eligible app a comparison group. HDBSCAN finds dense groups and marks other profiles as noise. The silhouette measures separation among assigned apps. Coverage matters when comparing these scores. The median adjusted Rand index (ARI) measures how closely assignments repeat across 25 seeded 80% subsamples; higher values mean greater stability.</p>
<h2>Groups and representative apps</h2><section><table><thead><tr><th>Method</th><th>Group</th><th>Apps</th><th>Medoid</th><th>Most common credential gate</th><th>Most common access mode</th><th>Near the medoid</th></tr></thead><tbody>{"".join(groups)}</tbody></table></section>
<h2>How similarity was measured</h2><p>Four equally weighted blocks describe authentication, API surface, access conditions and documented MCP availability. Set-valued fields use Jaccard distance. REST and generic HTTP share one family. Access mode and credential gate split one block. Unknown or unverified values are skipped pairwise; no comparable block gives maximum distance. App name, business category, build verdict and audit grades are held out.</p>
<p>We selected the highest silhouette within each method. For HDBSCAN we preferred settings assigning at least half the eligible apps. The table gives minimum cluster size / minimum samples for HDBSCAN and number of groups for PAM. Cluster numbers are local to each method. Results describe this curated list.</p>
<details><summary>All parameter settings</summary><table><tr><th>Method</th><th>Setting</th><th>Groups</th><th>Assigned</th><th>Silhouette</th></tr>{comparison}</table></details>"""
    summary["method_html"] = body
    (study / "cluster-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n"
    )
    print("PAM", partition[1], grade(partition))
    print("HDBSCAN", dense[1] if dense else "", grade(dense))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("study", type=Path)
    main(parser.parse_args().study)
