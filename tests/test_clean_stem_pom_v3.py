import csv
import hashlib
import io
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import clean_stem_pom_v3 as v3


DATA = ROOT / "site/public/viewer-v3-clean-stem/data"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CleanStemPomV3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = read_json(DATA / "measurements.json")
        cls.summary = read_json(DATA / "summary.json")
        cls.queue = read_json(DATA / "review_queue.json")
        cls.records = cls.payload["records"]
        cls.by_tree = {row["tree_id"]: row for row in cls.records}
        cls.config = read_json(ROOT / "config/clean_stem_pom_v3.json")

    def test_01_all_preserved_physical_tree_ids_have_one_record(self):
        inventory = read_json(ROOT / "site/public/viewer-v2-review/data/phase4_tree_inventory.json")
        expected = sorted(tree["tree_id"] for tree in inventory["trees"])
        actual = [row["tree_id"] for row in self.records]
        self.assertEqual(len(actual), 118)
        self.assertEqual(actual, expected)
        self.assertEqual(actual, sorted(set(actual)))
        self.assertEqual(self.queue["queue_size"], 118)
        self.assertEqual([row["review_item_id"] for row in self.queue["entries"]], actual)

    def test_02_v3_is_a_separate_unverified_workflow(self):
        self.assertEqual(self.payload["workflow"], "SEPARATE_CLEAN_STEM_POM_V3")
        self.assertFalse(self.payload["field_verified"])
        for row in self.records:
            self.assertFalse(row["field_verified"])
            self.assertFalse(row["protocol_final"])
            self.assertFalse(row["confidence_is_calibrated"])
            self.assertFalse(row["perpendicular_refit_performed"])
            self.assertEqual(row["source_slice_orientation"], "HORIZONTAL_XY_PROFILE")

    def test_03_statuses_and_coverage_are_consistent(self):
        counts = self.summary["status_counts"]
        self.assertEqual(counts, {"ALTERNATIVE_POM": 29, "MANUAL_REVIEW": 76, "STANDARD_DBH": 13})
        comparison = self.summary["v2_coverage_comparison"]
        self.assertEqual(comparison["v2_phase4_measurable_count"], 29)
        self.assertEqual(comparison["v3_automatic_count"], 42)
        self.assertGreater(comparison["v3_automatic_count"], comparison["v2_phase4_measurable_count"])
        self.assertFalse(comparison["accuracy_comparison_performed"])

    def test_04_automatic_measurements_are_numerically_and_semantically_valid(self):
        automatic = [row for row in self.records if row["automatic_measurement"]]
        self.assertEqual(len(automatic), 42)
        for row in automatic:
            self.assertIn(row["status"], v3.AUTOMATIC_STATUSES)
            self.assertIsNotNone(row["selected_window"])
            self.assertIsNone(row["best_review_window"])
            self.assertAlmostEqual(row["diameter_cm"], row["radius_m"] * 200.0, delta=0.011)
            self.assertAlmostEqual(row["circumference_cm"], math.pi * row["diameter_cm"], delta=0.02)
            self.assertIsNotNone(row["measurement_plane"])
            self.assertEqual(row["measurement_plane_orientation"], "PERPENDICULAR_TO_LOCAL_STEM_AXIS")
            self.assertGreaterEqual(row["quality_score"], 60.0)
            selected = row["selected_window"]
            if selected["cross_lane_consistent"] is not None:
                self.assertTrue(selected["cross_lane_consistent"], row["tree_id"])
            if row["status"] == "STANDARD_DBH":
                self.assertAlmostEqual(row["measurement_height_agl_m"], 1.30, places=9)
                self.assertEqual(row["dbh_cm"], row["diameter_cm"])
            else:
                self.assertGreater(row["measurement_height_agl_m"], 1.30)
                self.assertIsNone(row["dbh_cm"])
                standard = row["standard_height_diagnostics"]
                if standard["candidate_available"]:
                    self.assertTrue(standard["failure_reasons"])
                    self.assertTrue(any(reason.startswith("STANDARD_REJECTED_") for reason in row["reason_codes"]))

    def test_05_manual_review_never_exposes_a_v3_measurement(self):
        manual = [row for row in self.records if row["status"] == "MANUAL_REVIEW"]
        self.assertEqual(len(manual), 76)
        for row in manual:
            self.assertFalse(row["automatic_measurement"])
            self.assertIsNone(row["measurement_height_agl_m"])
            self.assertIsNone(row["radius_m"])
            self.assertIsNone(row["diameter_cm"])
            self.assertIsNone(row["circumference_cm"])
            self.assertIsNone(row["measurement_plane"])
            self.assertTrue(row["reason_codes"])

    def test_06_exclusions_and_blocked_identity_reviews_cannot_measure_automatically(self):
        current = read_json(ROOT / "site/public/data/lidar-measurements/measurements.json")
        blocked = {
            row["tree_id"] for row in current["records"]
            if row["operationally_excluded"]
            or row["identity_review_status"] in self.config["blocked_identity_review_statuses"]
        }
        for tree_id in blocked:
            self.assertEqual(self.by_tree[tree_id]["status"], "MANUAL_REVIEW", tree_id)
        for row in self.records:
            labels = row["source_human_labels"]
            source_is_blocked_only = labels and "TRUE_MAIN_STEM" not in labels and all(
                label in self.config["blocked_human_labels"] for label in labels
            )
            if source_is_blocked_only:
                self.assertEqual(row["status"], "MANUAL_REVIEW", row["tree_id"])

    def test_07_local_axis_and_plane_are_orthonormal(self):
        for row in self.records:
            direction = row["local_axis"]["direction_unit"]
            if direction is None:
                continue
            self.assertAlmostEqual(sum(value * value for value in direction), 1.0, places=5)
            plane = row["measurement_plane"] or row["best_review_plane"]
            if plane is None:
                continue
            axis, u, basis_v = plane["axis_direction"], plane["basis_u"], plane["basis_v"]
            self.assertEqual(plane["orientation"], "PERPENDICULAR_TO_LOCAL_STEM_AXIS")
            self.assertAlmostEqual(sum(a * b for a, b in zip(axis, u)), 0.0, places=5)
            self.assertAlmostEqual(sum(a * b for a, b in zip(axis, basis_v)), 0.0, places=5)
            self.assertAlmostEqual(sum(a * b for a, b in zip(u, basis_v)), 0.0, places=5)

    def test_08_height_search_does_not_extrapolate_beyond_evidence(self):
        height = self.summary["height_search"]
        self.assertEqual(height["requested_maximum_height_m"], 4.0)
        self.assertEqual(height["published_evidence_maximum_height_m"], 3.5)
        self.assertEqual(height["maximum_robust_window_center_m"], 3.35)
        self.assertFalse(height["four_metre_search_executed"])
        measured = [row["measurement_height_agl_m"] for row in self.records if row["automatic_measurement"]]
        self.assertLessEqual(max(measured), 3.35)

    def test_09_source_hashes_match_read_only_v2_inputs(self):
        for metadata in self.payload["source"]["files"].values():
            path = ROOT / metadata["path"]
            self.assertTrue(path.is_file(), path)
            self.assertEqual(sha256(path), metadata["sha256"], path)
        self.assertNotIn("viewer-v3-clean-stem", " ".join(
            metadata["path"] for metadata in self.payload["source"]["files"].values()
        ))

    def test_10_csv_matches_compact_json(self):
        csv_rows = list(csv.DictReader(io.StringIO((DATA / "measurements.csv").read_text(encoding="utf-8-sig"))))
        self.assertEqual([row["tree_id"] for row in csv_rows], [row["tree_id"] for row in self.records])
        self.assertEqual(len(csv_rows), 118)
        self.assertNotIn("scored_windows", self.records[0])
        self.assertNotIn("track", self.records[0])

    def test_11_every_referenced_point_crop_exists_in_the_base_commit(self):
        urls = {row["point_crop_url"] for row in self.records if row["point_crop_url"]}
        self.assertGreater(len(urls), 0)
        tree = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "HEAD", "--", "site/public/viewer-v2-review/data/points"],
            cwd=ROOT,
            capture_output=True,
            check=True,
            text=True,
        )
        tracked_paths = set(tree.stdout.splitlines())
        for url in urls:
            self.assertTrue(url.startswith("../viewer-v2-review/data/points/"))
            git_path = "site/public/" + url.removeprefix("../")
            self.assertIn(git_path, tracked_paths)

    def test_12_standalone_viewer_has_required_debug_views_and_no_rayong_reference(self):
        directory = ROOT / "site/public/viewer-v3-clean-stem"
        html = (directory / "index.html").read_text(encoding="utf-8")
        script = (directory / "app.js").read_text(encoding="utf-8")
        for required in ("cloudCanvas", "profileCanvas", "crossCanvas", "measurements.csv", "measurements.json"):
            self.assertIn(required, html)
        for required in ("drawOrientedPlane", "drawFitCircle3d", "drawProfile", "drawCross", "quality_components"):
            self.assertIn(required, script)
        self.assertNotIn("rayong", (html + script).lower())

    def test_13_generator_is_reproducible_and_does_not_write_v2(self):
        source_hashes_before = {
            key: sha256(ROOT / metadata["path"])
            for key, metadata in self.payload["source"]["files"].items()
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "v3"
            v3.write_artifacts(ROOT, output)
            for name in ("measurements.json", "measurements.csv", "summary.json", "review_queue.json"):
                self.assertEqual((output / name).read_bytes(), (DATA / name).read_bytes(), name)
        source_hashes_after = {
            key: sha256(ROOT / metadata["path"])
            for key, metadata in self.payload["source"]["files"].items()
        }
        self.assertEqual(source_hashes_before, source_hashes_after)


if __name__ == "__main__":
    unittest.main()
