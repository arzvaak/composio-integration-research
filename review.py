"""Local paired human review of the Composio study. Run with .venv/bin/python review.py."""
import argparse
import csv
import fcntl
import json
import os
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from pathlib import Path
from urllib.parse import urlsplit

from report import COLUMNS, LABELS, render
from pipeline import load

ROOT = Path(__file__).resolve().parent
EDITABLE = [k for k in COLUMNS[3:] if k != "reviewer"]


def rows(study):
    with (study / "human-review.csv").open(newline="") as stream:
        return list(csv.DictReader(stream))


def queue(study):
    result = []
    path = study / "review-suggestions.json"
    suggestions = load(path) if path.exists() else {}
    for row in rows(study):
        entry = {"row": row, "label": LABELS[row["field"]]}
        for stage in ("sol", "astra"):
            data = load(study / "apps" / row["id"] / f"{stage}.json")
            entry[stage] = next(f for f in data["fields"] if f["field"] == row["field"])
        if not (row["sol_grade"] and row["astra_grade"]):
            entry["suggestion"] = suggestions.get(f'{row["id"]}/{row["field"]}')
        result.append(entry)
    return result


def store(study, data):
    existing = rows(study)
    item = next(r for r in existing if [r["id"], r["field"]] == data["key"])
    if item != data["before"]:
        raise FileExistsError("This check was edited elsewhere. Reload before editing.")
    after = {k: data["values"][k].strip() for k in EDITABLE}
    url = urlsplit(after["source_url"])
    if not after["human_answer"] or url.scheme not in {"https","http"} or not url.hostname:
        raise ValueError("Enter your finding and the source URL you checked.")
    if any(after[k] not in {"correct","incorrect","unresolved"} for k in ("sol_grade","astra_grade")):
        raise ValueError("Grade both answers to finish this check.")
    item.update(after, reviewer="User")
    with tempfile.NamedTemporaryFile(mode="w", newline="", dir=study, delete=False) as tmp:
        writer = csv.DictWriter(tmp, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(existing)
        tmp.flush()
        os.fsync(tmp.fileno())
    os.replace(tmp.name, study / "human-review.csv")
    render(study)
    return item


class ReviewHandler(BaseHTTPRequestHandler):
    timeout = 15

    def reply(self, body, kind="application/json", status=200):
        content = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.path == "/":
            return self.reply((ROOT/"review.html").read_bytes(),"text/html; charset=utf-8")
        if self.path == "/checks":
            return self.reply(queue(self.server.study))
        self.send_error(404)

    def do_POST(self):
        host = f"127.0.0.1:{self.server.server_port}"
        if self.path != "/save" or self.headers.get("Host") != host or self.headers.get("Origin") != f"http://{host}" or self.headers.get_content_type() != "application/json":
            return self.send_error(403)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size < 100000:
                raise ValueError("Invalid request size.")
            data = json.loads(self.rfile.read(size))
            with self.server.save_lock, (self.server.study / ".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                saved = store(self.server.study, data)
        except FileExistsError as exc:
            return self.reply({"error":str(exc)}, status=409)
        except (ValueError, KeyError, AttributeError, TypeError, StopIteration):
            return self.reply({"error":"Check the finding, source URL, and both grades."},status=400)
        self.reply(saved)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("study",type=Path,nargs="?",default=ROOT/"studies/composio-100")
    parser.add_argument("--port",type=int,default=8941)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1",args.port),ReviewHandler)
    server.save_lock = Lock()
    server.study = args.study.resolve()
    server.serve_forever()
