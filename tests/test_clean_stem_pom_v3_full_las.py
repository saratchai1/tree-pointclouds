import csv
import hashlib
import io
import json
import math
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import clean_stem_pom_v3_full_las as v31


DATA = ROOT / "site/public/viewer-v3-full-las/data"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class CleanStemPomV31FullLasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = read_json(DATA / "measurements.json")
        cls.summary = read_json(DATA / "summary.json")
        cls.queue = read_json(DATA / "review_queue.json")
        cls.index = read_json(DATA / "evidence-index.json")
        cls.config = read_json(ROOT / "config/clean_stem_pom_v3_full_las.json")
        cls.records = cls.payload["records"]
        cls.by_tree = {row["tree_id"]: row for row in cls.records}
        cls.evidence = {}
        for shard_name in sorted(set(cls.index["trees"].values())):
            cls.evidence.update(read_json(DATA / shard_name)["evidence"])

    def test_01_all_preserved_tree_ids_have_one_record(self):
        inventory = read_json(ROOT / "site/public/viewer-v2-review/data/phase4_tree_inventory.json")
        expected = sorted(tree["tree_id"] for tree in inventory["trees"])
        actual = [record["tree_id"] for record in self.records]
        self.assertEqual(len(actual), 118)
        self.assertEqual(actual, expected)
        self.assertEqual(actual, sorted(set(actual)))
        self.assertEqual(self.queue["queue_size"], 118)
        self.assertEqual(sorted(self.index["trees"]), expected)
        self.assertEqual(sorted(self.evidence), expected)

    def test_02_v31_is_separate_full_las_unverified_workflow(self):
        self.assertEqual(self.payload["workflow"], v31.WORKFLOW)
        self.assertEqual(self.payload["algorithm_version"], "clean-stem-pom-v3.1.0-full-las")
        self.assertFalse(self.payload["field_verified"])
        self.assertTrue(self.summary["perpendicular_full_resolution_refit_performed"])
        for record in self.records:
            self.assertFalse(record["field_verified"])
            self.assertFalse(record["protocol_final"])
            self.assertFalse(record["confidence_is_calibrated"])
            self.assertEqual(record["source_slice_orientation"], "PERPENDICULAR_TO_LOCAL_STEM_AXIS")

    def test_03_status_counts_and_coverage_comparison_are_exact(self):
        self.assertEqual(self.summary["status_counts"], {
            "ALTERNATIVE_POM": 39,
            "MANUAL_REVIEW": 58,
            "STANDARD_DBH": 21,
        })
        comparison = self.summary["coverage_comparison"]
        self.assertEqual(comparison["v2_phase4_measurable_count"], 29)
        self.assertEqual(comparison["v3_sampled_evidence_automatic_count"], 42)
        self.assertEqual(comparison["v3_1_full_las_automatic_count"], 60)
        self.assertEqual(comparison["net_change_from_v3"], 18)
        self.assertEqual(comparison["newly_automatic_vs_v3_count"], 21)
        self.assertEqual(comparison["v3_automatic_now_manual_count"], 3)
        self.assertFalse(comparison["accuracy_comparison_performed"])

    def test_04_automatic_measurements_are_numerically_valid(self):
        automatic = [record for record in self.records if record["automatic_measurement"]]
        self.assertEqual(len(automatic), 60)
        for record in automatic:
            self.assertIn(record["status"], v31.AUTOMATIC_STATUSES)
            self.assertIsNotNone(record["selected_candidate"])
            self.assertIsNone(record["best_review_candidate"])
            self.assertAlmostEqual(record["diameter_cm"], record["radius_m"] * 200.0, delta=0.011)
            self.assertAlmostEqual(record["circumference_cm"], math.pi * record["diameter_cm"], delta=0.03)
            self.assertLessEqual(record["radius_m"], self.config["reliability"]["maximum_automatic_radius_m"])
            self.assertGreaterEqual(record["measurement_height_agl_m"], 1.3)
            self.assertLessEqual(record["measurement_height_agl_m"], 4.0)
            self.assertAlmostEqual((record["measurement_height_agl_m"] - 1.3) / 0.1, round((record["measurement_height_agl_m"] - 1.3) / 0.1), places=6)
            self.assertIsNotNone(record["measurement_plane"])
            self.assertTrue(record["perpendicular_refit_performed"])
            if record["status"] == "STANDARD_DBH":
                self.assertEqual(record["measurement_height_agl_m"], 1.3)
                self.assertEqual(record["dbh_cm"], record["diameter_cm"])
            else:
                self.assertGreater(record["measurement_height_agl_m"], 1.3)
                self.assertIsNone(record["dbh_cm"])

    def test_05_manual_review_never_releases_an_automatic_measurement(self):
        manual = [record for record in self.records if record["status"] == "MANUAL_REVIEW"]
        self.assertEqual(len(manual), 58)
        for record in manual:
            self.assertFalse(record["automatic_measurement"])
            self.assertIsNone(record["measurement_height_agl_m"])
            self.assertIsNone(record["radius_m"])
            self.assertIsNone(record["diameter_cm"])
            self.assertIsNone(record["circumference_cm"])
            self.assertIsNone(record["measurement_plane"])
            self.assertTrue(record["reason_codes"])

    def test_06_identity_and_detection_gates_are_conservative(self):
        current = {row["tree_id"]: row for row in read_json(ROOT / "site/public/data/lidar-measurements/measurements.json")["records"]}
        inventory = {tree["tree_id"]: tree for tree in read_json(ROOT / "site/public/viewer-v2-review/data/phase4_tree_inventory.json")["trees"]}
        for record in self.records:
            tree_id = record["tree_id"]
            blocked = (
                current[tree_id].get("operationally_excluded")
                or current[tree_id].get("identity_review_status") in self.config["blocked_identity_review_statuses"]
                or (inventory[tree_id].get("detection") or {}).get("status") not in self.config["eligible_detection_statuses"]
            )
            if blocked:
                self.assertEqual(record["status"], "MANUAL_REVIEW", tree_id)

    def test_07_every_measurement_plane_is_orthonormal(self):
        for record in self.records:
            plane = record["measurement_plane"] or record["best_review_plane"]
            if not plane:
                continue
            axis, basis_u, basis_v = plane["axis_direction"], plane["basis_u"], plane["basis_v"]
            self.assertEqual(plane["orientation"], "PERPENDICULAR_TO_LOCAL_STEM_AXIS")
            self.assertAlmostEqual(sum(value * value for value in axis), 1.0, places=5)
            self.assertAlmostEqual(sum(value * value for value in basis_u), 1.0, places=5)
            self.assertAlmostEqual(sum(value * value for value in basis_v), 1.0, places=5)
            self.assertAlmostEqual(sum(a * b for a, b in zip(axis, basis_u)), 0.0, places=5)
            self.assertAlmostEqual(sum(a * b for a, b in zip(axis, basis_v)), 0.0, places=5)
            self.assertAlmostEqual(sum(a * b for a, b in zip(basis_u, basis_v)), 0.0, places=5)

    def test_08_full_height_scan_and_evidence_profiles_are_complete(self):
        search = self.summary["height_search"]
        self.assertTrue(search["four_metre_search_executed"])
        self.assertEqual(search["standard_height_m"], 1.3)
        self.assertEqual(search["maximum_height_m"], 4.0)
        expected_heights = [round(1.3 + 0.1 * index, 1) for index in range(28)]
        for tree_id, evidence in self.evidence.items():
            profile = evidence["candidate_profile"]
            self.assertEqual(len(profile), 28, tree_id)
            self.assertEqual([row["height_agl_m"] for row in profile], expected_heights, tree_id)
            self.assertGreater(evidence["full_resolution_tube_point_count"], 0)

    def test_09_decisions_replay_from_committed_evidence(self):
        for record in self.records:
            evidence = self.evidence[record["tree_id"]]
            candidates = evidence["candidate_profile"]
            decision = v31.choose_measurement(
                candidates,
                record["blocked_reason_codes"],
                self.config,
                evidence["axis_refit"]["supporting_slice_count"],
            )
            self.assertEqual(decision["status"], record["status"], record["tree_id"])
            selected_height = decision["selected"]["height_agl_m"] if decision["selected"] else None
            self.assertEqual(selected_height, record["measurement_height_agl_m"], record["tree_id"])

    def test_10_root_crown_guardrails_reject_obvious_large_circles(self):
        for tree_id in ("TREE_0005", "TREE_0085", "TREE_0111"):
            self.assertEqual(self.by_tree[tree_id]["status"], "MANUAL_REVIEW")
            self.assertIn("SECTION_RADIUS_EXCEEDS_AUTOMATIC_COHORT_GUARDRAIL", self.by_tree[tree_id]["reason_codes"])
        recovered = self.by_tree["TREE_0054"]
        self.assertEqual(recovered["status"], "ALTERNATIVE_POM")
        self.assertEqual(recovered["measurement_height_agl_m"], 2.6)
        self.assertLess(recovered["diameter_cm"], 10.0)

    def test_11_las_provenance_is_exact_and_raw_file_is_not_tracked(self):
        source = self.payload["source"]["source_las"]
        expected = self.config["source_las"]
        self.assertEqual(source["file_name"], expected["file_name"])
        self.assertEqual(source["size_bytes"], expected["expected_size_bytes"])
        self.assertEqual(source["point_count"], expected["expected_point_count"])
        self.assertEqual(source["sha256"], expected["expected_sha256"])
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, check=True, text=True
        ).stdout.splitlines()
        self.assertFalse(any(path.endswith(".las") or "td008_v31_tubes" in path for path in tracked))

    def test_12_read_only_input_hashes_still_match(self):
        for key, metadata in self.payload["source"]["files"].items():
            if key == "marking_manifest":
                paths = sorted((ROOT / "site/public/data/lidar-measurements/markings").glob("TREE_*.json"))
                self.assertEqual(len(paths), metadata["file_count"])
                self.assertEqual(v31.sha256_directory(paths, ROOT), metadata["sha256_manifest"])
            else:
                path = ROOT / metadata["path"]
                self.assertTrue(path.is_file(), path)
                self.assertEqual(sha256(path), metadata["sha256"], path)

    def test_13_csv_and_review_queue_match_json(self):
        csv_rows = list(csv.DictReader(io.StringIO((DATA / "measurements.csv").read_text(encoding="utf-8-sig"))))
        self.assertEqual([row["tree_id"] for row in csv_rows], [record["tree_id"] for record in self.records])
        self.assertEqual([row["review_item_id"] for row in self.queue["entries"]], [record["tree_id"] for record in self.records])

    def test_14_review_artifacts_are_connector_safe_and_viewer_is_standalone(self):
        for path in DATA.glob("*.json"):
            self.assertLess(path.stat().st_size, 1_000_000, path)
        directory = ROOT / "site/public/viewer-v3-full-las"
        html = (directory / "index.html").read_text(encoding="utf-8")
        script = (directory / "app.js").read_text(encoding="utf-8")
        styles = (directory / "styles.css").read_text(encoding="utf-8")
        for required in ("overviewCanvas", "cloudCanvas", "profileCanvas", "crossCanvas", "measurements.csv", "measurements.json"):
            self.assertIn(required, html)
        for required in (
            "drawOverview", "overviewVisibleRecords", "OVERVIEW_POSITION_CHUNKS",
            "drawPlane", "drawProfile", "drawCross", "candidate_profile", "full_resolution_tube_point_count",
        ):
            self.assertIn(required, script)
        for required in (".overview-panel", "#overviewCanvas", "max-width: 100%", "@media (max-width: 900px)", "@media (max-width: 620px)"):
            self.assertIn(required, styles)
        self.assertIn('href="styles.css?v=full-las-v3-1"', html)
        self.assertNotIn("rayong", (html + script + styles).lower())

    def test_15_overview_maps_every_tree_and_preserves_measurement_truth(self):
        metadata = read_json(ROOT / "site/public/data/metadata.json")
        position = next(attribute for attribute in metadata["attributes"] if attribute["name"] == "position")
        minimum, maximum = position["min"], position["max"]
        automatic = manual = 0
        for record in self.records:
            x = record["location"]["x"]
            y = record["location"]["y"]
            z = record["local_ground_z_m"]
            self.assertTrue(all(math.isfinite(value) for value in (x, y, z)), record["tree_id"])
            self.assertGreaterEqual(x, minimum[0], record["tree_id"])
            self.assertLessEqual(x, maximum[0], record["tree_id"])
            self.assertGreaterEqual(y, minimum[1], record["tree_id"])
            self.assertLessEqual(y, maximum[1], record["tree_id"])
            self.assertGreaterEqual(z, minimum[2], record["tree_id"])
            self.assertLessEqual(z, maximum[2], record["tree_id"])
            if record["automatic_measurement"]:
                automatic += 1
                self.assertIn(record["status"], v31.AUTOMATIC_STATUSES)
            else:
                manual += 1
                self.assertEqual(record["status"], "MANUAL_REVIEW")
                self.assertIsNone(record["circumference_cm"])
        self.assertEqual((automatic, manual), (60, 58))

        directory = ROOT / "site/public/viewer-v3-full-las"
        html = (directory / "index.html").read_text(encoding="utf-8")
        script = (directory / "app.js").read_text(encoding="utf-8")
        for label in ("ภาพรวมตำแหน่ง Tree ID ทั้งแปลง", "วัดได้", "ยังวัดไม่ได้"):
            self.assertIn(label, html)
        self.assertIn('data-overview-filter="MEASURABLE"', html)
        self.assertIn('data-overview-filter="MANUAL_REVIEW"', html)
        self.assertIn('const OVERVIEW_POINT_BUDGET = 300000', script)
        self.assertIn('record.automatic_measurement ? "วัดได้" : "ยังวัดไม่ได้"', script)
        self.assertNotIn("lidar-measurements/viewer-index.json", script)


if __name__ == "__main__":
    unittest.main()
