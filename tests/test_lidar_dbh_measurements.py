import csv
import hashlib
import io
import json
import math
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent.parent
OUTPUT_JSON = ROOT / "outputs/lidar_tree_measurements.json"
OUTPUT_CSV = ROOT / "outputs/lidar_tree_measurements.csv"
SUMMARY = ROOT / "outputs/lidar_tree_measurement_summary.json"
MARKINGS = ROOT / "site/public/data/lidar-measurements/markings"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class LidarDbhMeasurementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = read_json(OUTPUT_JSON)
        cls.summary = read_json(SUMMARY)
        cls.rows = cls.payload["records"]
        cls.by_tree = {row["tree_id"]: row for row in cls.rows}

    def test_01_exact_frozen_118_tree_ids_are_preserved(self):
        inventory_ids = sorted(row["tree_id"] for row in read_json(ROOT / "outputs/phase4_tree_inventory.json")["trees"])
        output_ids = [row["tree_id"] for row in self.rows]
        self.assertEqual(len(output_ids), 118)
        self.assertEqual(output_ids, sorted(set(output_ids)))
        self.assertEqual(output_ids, inventory_ids)

    def test_02_source_las_is_scanned_once_and_identity_is_recorded(self):
        source = self.payload["source"]
        self.assertEqual(source["source_las_scan_count"], 1)
        self.assertEqual(source["source_las_point_count"], 67_177_038)
        self.assertEqual(source["size_bytes"], 1_746_603_215)
        self.assertEqual(source["sha256"], "195725dbbc7f853994f926027dfea9c9e4d986ac06cd6d9187f30ffebe528276")
        self.assertLess(source["alignment_error_m"], 0.001)

    def test_03_only_four_protocol_and_identity_gated_results_are_final(self):
        final = [row for row in self.rows if row["acceptance_status"] == "FINAL_LIDAR_ESTIMATE"]
        self.assertEqual([row["tree_id"] for row in final], ["TREE_0017", "TREE_0097", "TREE_0103", "TREE_0105"])
        self.assertEqual(self.summary["final_lidar_measurement_count"], 4)
        expected = {
            "TREE_0017": (47.48, None, 15.11),
            "TREE_0097": (24.58, 7.83, 7.83),
            "TREE_0103": (22.45, None, 7.15),
            "TREE_0105": (25.98, 8.27, 8.27),
        }
        for row in final:
            self.assertEqual(
                (row["circumference_cm"], row["dbh_cm"], row["diameter_at_measurement_height_cm"]),
                expected[row["tree_id"]],
            )

    def test_04_dbh_and_prop_root_diameter_are_not_conflated(self):
        for row in self.rows:
            if row["dbh_cm"] is not None:
                self.assertEqual(row["measurement_kind"], "STANDARD_DBH_1_30")
                self.assertAlmostEqual(row["measurement_height_agl_m"], 1.30, places=9)
                self.assertAlmostEqual(row["dbh_cm"], row["circumference_cm"] / math.pi, delta=0.02)
            if row["measurement_kind"] == "PROP_ROOT_PLUS_030" and row["circumference_cm"] is not None:
                self.assertIsNone(row["dbh_cm"])
                self.assertIsNotNone(row["diameter_at_measurement_height_cm"])

    def test_05_nonfinal_numeric_results_are_null_but_candidates_are_traceable(self):
        for row in self.rows:
            self.assertFalse(row["field_verified"])
            self.assertFalse(row["protocol_plane_moved_to_cleaner_height"])
            if row["acceptance_status"] != "FINAL_LIDAR_ESTIMATE":
                self.assertIsNone(row["circumference_cm"], row["tree_id"])
                self.assertIsNone(row["dbh_cm"], row["tree_id"])
                self.assertIsNone(row["diameter_at_measurement_height_cm"], row["tree_id"])
        self.assertEqual(self.by_tree["TREE_0098"]["identity_review_status"], "DUPLICATE")
        self.assertEqual(self.by_tree["TREE_0098"]["acceptance_status"], "PROVISIONAL_TREE_IDENTITY_REVIEW_REQUIRED")
        self.assertEqual(self.by_tree["TREE_0098"]["candidate_dbh_cm"], 7.81)

    def test_06_every_tree_has_a_geometric_marking_file(self):
        files = sorted(MARKINGS.glob("TREE_*.json"))
        self.assertEqual(len(files), 118)
        for row in self.rows:
            marking = read_json(ROOT / "site/public" / row["marking_url"])
            self.assertEqual(marking["tree_id"], row["tree_id"])
            plane = marking["measurement_plane"]
            axis = plane["axis_direction"]
            for basis in (plane["basis_u"], plane["basis_v"]):
                self.assertAlmostEqual(sum(a * b for a, b in zip(axis, basis)), 0.0, places=6)
            self.assertEqual(plane["orientation"], "PERPENDICULAR_TO_LOCAL_STEM_AXIS")

    def test_07_csv_matches_json_tree_order_and_final_values(self):
        csv_rows = list(csv.DictReader(io.StringIO(OUTPUT_CSV.read_text(encoding="utf-8-sig"))))
        self.assertEqual([row["tree_id"] for row in csv_rows], [row["tree_id"] for row in self.rows])
        final_csv = [row for row in csv_rows if row["acceptance_status"] == "FINAL_LIDAR_ESTIMATE"]
        self.assertEqual(len(final_csv), 4)

    def test_08_local_marking_viewer_and_downloads_exist(self):
        html = (ROOT / "site/public/lidar-measurements/index.html").read_text(encoding="utf-8")
        script = (ROOT / "site/public/lidar-measurements/app.js").read_text(encoding="utf-8")
        for required in ("cloudCanvas", "crossCanvas", "measurements.csv", "measurements.json"):
            self.assertIn(required, html)
        for required in ("measurement_plane", "accepted_slice_points_xyz", "fitOutline3d", "drawCross"):
            self.assertIn(required, script)

    def test_09_frozen_phase5a_outputs_remain_byte_identical(self):
        manifest = read_json(ROOT / "outputs/pilot_release_manifest.json")
        phase5a_paths = [path for path in manifest["protected_outputs"] if Path(path).name.startswith("phase5a_")]
        self.assertEqual(len(phase5a_paths), 8)
        for relative in phase5a_paths:
            actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
            self.assertEqual(actual, manifest["protected_outputs"][relative], relative)

    def test_10_all_29_earlier_full_resolution_measurements_are_restored(self):
        expected_standard = {
            "TREE_0014", "TREE_0037", "TREE_0039", "TREE_0041", "TREE_0044", "TREE_0050",
            "TREE_0056", "TREE_0061", "TREE_0062", "TREE_0065", "TREE_0069", "TREE_0073",
            "TREE_0074", "TREE_0075", "TREE_0076", "TREE_0081", "TREE_0104", "TREE_0105",
        }
        expected_adaptive = {
            "TREE_0011", "TREE_0017", "TREE_0018", "TREE_0026", "TREE_0035", "TREE_0048",
            "TREE_0083", "TREE_0090", "TREE_0093", "TREE_0100", "TREE_0117",
        }
        legacy = {row["tree_id"] for row in self.rows if row["legacy_full_resolution_status"] == "ACCEPTED"}
        self.assertEqual(legacy, expected_standard | expected_adaptive)
        self.assertEqual(self.summary["legacy_full_resolution_accepted_count"], 29)
        self.assertEqual(self.summary["legacy_standard_1_30_count"], 18)
        self.assertEqual(self.summary["legacy_adaptive_plus_030_count"], 11)

    def test_11_protocol_is_a_confidence_label_not_a_field_aid_gate(self):
        ready = [row for row in self.rows if row["field_aid_status"] == "READY_FOR_FIELD_USE"]
        check = [row for row in self.rows if row["field_aid_status"] == "CHECK_ON_SITE"]
        no_estimate = [row for row in self.rows if row["field_aid_status"] == "NO_ESTIMATE"]
        excluded = [row for row in self.rows if row["field_aid_status"] == "EXCLUDED_CONFIRMED_WRONG"]
        self.assertEqual((len(ready), len(check), len(no_estimate), len(excluded)), (41, 67, 2, 8))
        self.assertEqual(self.summary["field_aid_ready_count"], 41)
        self.assertEqual(self.summary["field_aid_check_on_site_count"], 67)
        self.assertEqual(self.summary["field_aid_no_estimate_count"], 2)
        for row in ready + check:
            self.assertIsNotNone(row["field_aid_circumference_cm"], row["tree_id"])
            self.assertIsNotNone(row["field_aid_diameter_cm"], row["tree_id"])
        for row in no_estimate:
            self.assertIsNone(row["field_aid_circumference_cm"], row["tree_id"])
        for row in excluded:
            self.assertIsNone(row["field_aid_circumference_cm"], row["tree_id"])
            self.assertIsNone(row["field_aid_diameter_cm"], row["tree_id"])

    def test_12_legacy_adaptive_values_and_markings_are_not_mislabelled_dbh(self):
        adaptive = [row for row in self.rows if row["legacy_measurement_rule"] == "ADAPTIVE_IRREGULAR_ZONE_PLUS_030"]
        self.assertEqual(len(adaptive), 11)
        for row in adaptive:
            self.assertNotAlmostEqual(row["legacy_measurement_height_agl_m"], 1.30, places=6)
            self.assertIsNone(row["legacy_dbh_cm"])
            marking = read_json(ROOT / "site/public" / row["marking_url"])
            self.assertAlmostEqual(
                marking["measurement_plane"]["height_agl_m"],
                row["field_aid_measurement_height_agl_m"],
                places=6,
            )
            self.assertIn("field_aid_fit", marking)

    def test_13_legacy_only_rows_use_archived_full_resolution_fit_markings(self):
        legacy_only = [
            row for row in self.rows
            if row["legacy_full_resolution_status"] == "ACCEPTED"
            and row["acceptance_status"] != "FINAL_LIDAR_ESTIMATE"
        ]
        self.assertEqual(len(legacy_only), 27)
        for row in legacy_only:
            self.assertEqual(row["field_aid_marking_source"], "ARCHIVED_FULL_RESOLUTION_FIT")
            marking = read_json(ROOT / "site/public" / row["marking_url"])
            self.assertIsNotNone(marking["field_aid_fit"])
            self.assertEqual(marking["field_aid_marking_source"], "ARCHIVED_FULL_RESOLUTION_FIT")

    def test_14_human_confirmed_wrong_tree_ids_are_operationally_excluded(self):
        expected = {
            "TREE_0003", "TREE_0024", "TREE_0056", "TREE_0079",
            "TREE_0088", "TREE_0090", "TREE_0092", "TREE_0093",
        }
        actual = {row["tree_id"] for row in self.rows if row["operationally_excluded"]}
        self.assertEqual(actual, expected)
        self.assertEqual(set(self.summary["operational_excluded_tree_ids"]), expected)
        self.assertEqual(self.summary["operational_excluded_count"], 8)
        self.assertEqual(self.summary["legacy_operational_count"], 26)

    def test_15_all_four_highest_prop_root_plus_030_measurements_are_visible(self):
        expected = {"TREE_0017", "TREE_0103", "TREE_0106", "TREE_0107"}
        actual = {row["tree_id"] for row in self.rows if row["measurement_kind"] == "PROP_ROOT_PLUS_030"}
        self.assertEqual(actual, expected)
        self.assertEqual(set(self.summary["prop_root_plus_030_tree_ids"]), expected)
        self.assertEqual(self.summary["prop_root_plus_030_count"], 4)
        self.assertEqual(self.summary["prop_root_plus_030_ready_count"], 2)
        self.assertEqual(self.summary["prop_root_plus_030_check_on_site_count"], 2)


if __name__ == "__main__":
    unittest.main()
