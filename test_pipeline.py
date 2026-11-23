"""Exercise names-only input, partial-batch recovery, and preservation of human grading."""

import csv
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pipeline
from report import render


class PipelineRecoveryTest(unittest.TestCase):
    def test_names_only_restart_and_paired_audit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            names = root / "list.txt"
            names.write_text("Example One\nExample Two\n")
            out = root / "study"
            args = SimpleNamespace(list=names, output=out, batch_size=2, workers=1)
            url = "https://example.org/guide"
            packet = {
                "query": "test",
                "credits": 0,
                "hits": [{"url": url}],
                "pages": [],
            }
            fail_luna = True

            def model(phase, batch, records, folder, schema, allowed):
                if phase == "luna" and fail_luna:
                    raise RuntimeError("test interruption")
                data = {
                    "rows": [
                        {
                            "id": a["id"],
                            "app": a["app"],
                            "category": "example",
                            "auth": ["unknown"],
                            "gate": "unknown",
                            "decision": "unknown",
                            "fields": [
                                {
                                    "field": f,
                                    "answer": "unresolved",
                                    "status": "unknown",
                                    "sources": [],
                                    "review_note": "test",
                                    "evidence": "",
                                }
                                for f in pipeline.FIELDS
                            ],
                        }
                        for a in batch
                    ]
                }
                pipeline.validate(data, batch, allowed)
                pipeline.save(folder / "response.json", data)
                return data

            with (
                patch.object(pipeline, "Composio"),
                patch.object(pipeline, "scrapling_client"),
                patch.object(
                    pipeline, "catalog_entry", return_value={"slug": "example"}
                ),
                patch.object(pipeline, "gather", return_value=packet) as get,
                patch.object(pipeline, "run_model", side_effect=model) as run,
            ):
                with self.assertRaises(SystemExit):
                    pipeline.launch(args)
                self.assertEqual(run.call_count, 2)
                baseline = (out / "apps/001/sol.json").read_bytes()
                # Simulate interruption during fan-out of an already saved batch response.
                (out / "apps/002/sol.json").unlink()
                fail_luna = False
                pipeline.launch(args)
                self.assertEqual(run.call_count, 4)
                self.assertEqual(get.call_count, 6)
                self.assertEqual(baseline, (out / "apps/001/sol.json").read_bytes())
                self.assertEqual(pipeline.load(out / "summary.json")["complete"], 2)
                with (out / "human-review.csv").open(newline="") as f:
                    audit = list(csv.DictReader(f))
                audit[0].update(
                    human_answer="tested",
                    source_url=url,
                    sol_grade="incorrect",
                    astra_grade="correct",
                )
                with (out / "human-review.csv").open("w", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=audit[0].keys())
                    writer.writeheader()
                    writer.writerows(audit)
                pipeline.launch(args)
                self.assertEqual((run.call_count, get.call_count), (4, 6))
                scores = pipeline.load(out / "summary.json")["audit"]
                self.assertEqual(
                    (scores["graded"], scores["sol_correct"], scores["astra_correct"]),
                    (1, 0, 1),
                )
                render(out)
