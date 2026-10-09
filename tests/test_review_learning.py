"""Synthetic contract tests. Real-data metrics must come from the training report."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import review_learning as rl


def fixture(n=20):
    entries, annotations = [], []
    for i in range(n):
        good = i % 2 == 0
        cid = f"C-{i+1:04d}"
        radius = 0.06 if good else 0.4
        metrics = lambda p: {p+"_radius_m": radius,
            p+"_angular_coverage_deg": 310 if good else 100,
            p+"_fit_residual_m": 0.004 if good else 0.1,
            p+"_centreline_residual_p90_m": 0.015 if good else 0.5,
            p+"_radius_residual_mad_m": 0.002 if good else 0.2,
            p+"_point_count": 400 if good else 1000,
            p+"_connected_component_count": 1 if good else 6}
        full = metrics("full")
        full.update({"full_accepted_point_count": 360 if good else 250, "full_valid_slice_count": 7 if good else 3})
        entries.append({"candidate_id": cid, "item_type": "CANDIDATE_EVIDENCE",
            "position": {"x": i*2, "y": 0}, "canonical_phase1_candidate_id": cid,
            "track_id": f"T-{i}", "source_height_count_phase1": 7, "track_source_height_count": 4,
            "sampled_metrics": metrics("sampled"), "full_metrics": full,
            "comparison_metrics": {"center_shift_m": 0.01 if good else 0.4},
            "candidate_geometry_status": "STEM_LIKE"})
        annotations.append({"candidate_id": cid, "algorithm_version": "pilot-v1",
            "human_label": "TRUE_MAIN_STEM" if good else "PROP_ROOT_OR_ROOT_ONLY",
            "timestamp": "2026-08-09T01:00:00Z"})
    return {"algorithm_version": "pilot-v1", "entries": entries}, annotations


def dataset(n=20):
    q, a = fixture(n)
    d = rl.make_dataset(q, a, "SITE-A", "SURVEY-1")
    d["audit"] = [d["audit"]]
    return d


class ReviewLearningTests(unittest.TestCase):
    def test_identity_not_measurement_validity(self):
        d = dataset()
        self.assertEqual(d["records"][0]["targets"]["stem_identity"], 1)
        self.assertTrue(all(r["targets"]["measurement_validity"] is None for r in d["records"]))

    def test_unknown_and_duplicate_are_not_negatives(self):
        q,a = fixture()
        a[0]["human_label"] = "NOT_ENOUGH_INFORMATION"
        a[1].update(human_label="DUPLICATE_OF", duplicate_target="C-0001")
        d = rl.make_dataset(q,a,"S","D")
        self.assertIsNone(d["records"][0]["targets"]["stem_identity"])
        self.assertIsNone(d["records"][1]["targets"]["stem_identity"])
        self.assertEqual(d["records"][0]["group_id"], d["records"][1]["group_id"])

    def test_features_ignore_ids_labels_status_and_coordinates(self):
        q,_ = fixture()
        entry = q["entries"][0]
        before = rl.features(entry)
        entry.update(candidate_id="C-9999", human_label="BRANCH", identity_status="FALSE_POSITIVE",
                     corrected_measurement_height_m=3, position={"x":99999,"y":99999})
        entry["full_metrics"]["full_resolution_accepted"] = True
        self.assertEqual(before,rl.features(entry))

    def test_group_aliases_transitive_and_tracks(self):
        q,a = fixture()
        q["entries"][0]["alias_relationships"] = [{"other_candidate_id":"C-0002"}]
        q["entries"][1]["alias_relationships"] = [{"target":"C-0003"}]
        q["entries"][3]["track_id"] = q["entries"][0]["track_id"]
        groups=rl.group_candidates(q["entries"],a)
        self.assertEqual(len({groups[f"C-{i:04d}"] for i in (1,2,3,4)}),1)

    def test_group_nearby_evidence(self):
        q,a=fixture()
        q["entries"][1]["position"]={"x":0.5,"y":0}
        g=rl.group_candidates(q["entries"],a)
        self.assertEqual(g["C-0001"],g["C-0002"])

    def test_duplicate_candidate_error(self):
        q,a=fixture()
        q["entries"].append(q["entries"][0])
        with self.assertRaises(ValueError):rl.make_dataset(q,a,"S","D")

    def test_mismatched_legacy_pipeline_not_used(self):
        q,a=fixture()
        a[0]["algorithm_version"]="later-v3"
        d=rl.make_dataset(q,a,"S","D")
        self.assertIsNone(d["records"][0]["targets"]["stem_identity"])
        self.assertEqual(d["audit"]["ignored"][0]["reason"],"REVIEW_PIPELINE_MISMATCH")

    def test_conflicting_same_time_annotations_not_used(self):
        q,a=fixture()
        a.append({**a[0],"human_label":"BRANCH"})
        d=rl.make_dataset(q,a,"S","D")
        self.assertIsNone(d["records"][0]["targets"]["stem_identity"])

    def test_latest_annotation_with_timezone(self):
        q,a=fixture()
        a.append({**a[0],"human_label":"BRANCH","timestamp":"2026-08-09T09:00:00+07:00"})
        d=rl.make_dataset(q,a,"S","D")
        self.assertEqual(d["records"][0]["targets"]["stem_identity"],0)

    def test_evidence_change_invalidates_feedback(self):
        d=dataset()
        r=d["records"][0]
        event={k:r[k] for k in ("site_id","survey_id","candidate_id","evidence_hash","feature_schema")}
        event.update(task="measurement_validity",label="MEASUREMENT_CORRECT",timestamp="2026-10-09T00:00:00Z")
        event["evidence_hash"]="wrong-snapshot"
        rl.apply_feedback(d,[event])
        self.assertIsNone(r["targets"]["measurement_validity"])
        self.assertEqual(len(d["feedback_audit"]),1)

    def test_feedback_tasks_remain_separate(self):
        d=dataset();r=d["records"][0]
        event={k:r[k] for k in ("site_id","survey_id","candidate_id","evidence_hash","feature_schema")}
        event.update(task="measurement_validity",label="MEASUREMENT_INCORRECT",timestamp="2026-10-09T00:00:00Z")
        rl.apply_feedback(d,[event])
        self.assertEqual(r["targets"],{"stem_identity":1,"measurement_validity":0})

    def test_evidence_hash_includes_survey_and_geometry(self):
        q,a=fixture()
        a1=rl.make_dataset(q,a,"S","D")["records"][0]["evidence_hash"]
        a2=rl.make_dataset(q,a,"S","D2")["records"][0]["evidence_hash"]
        q["entries"][0]["full_metrics"]["full_radius_m"]=0.07
        a3=rl.make_dataset(q,a,"S","D")["records"][0]["evidence_hash"]
        self.assertEqual(len({a1,a2,a3}),3)

    def test_model_is_trained_and_replay_deterministic(self):
        d=dataset()
        m,r=rl.train_task(d,"stem_identity")
        m2,r2=rl.train_task(d,"stem_identity")
        self.assertEqual(m,m2)
        self.assertEqual(r,r2)
        self.assertEqual(len(m["coef"]),2*len(rl.FEATURES))
        self.assertEqual(r["evaluated_rows"],20)
        self.assertFalse(r["production_ready"])
        self.assertFalse(m["certifies_circumference"])
        self.assertFalse(m["score_is_calibrated_probability"])

    def test_grouped_training_test_no_overlap(self):
        d=dataset()
        d["records"][2]["group_id"]=d["records"][0]["group_id"]
        m,r=rl.train_task(d,"stem_identity")
        rows={v["candidate_id"]:v for v in r["oof_predictions"]}
        self.assertEqual(rows["C-0001"]["group_id"], rows["C-0003"]["group_id"])
        self.assertEqual(r["group_count"],19)

    def test_no_single_class_fake_model(self):
        d=dataset()
        for row in d["records"]:row["targets"]["stem_identity"]=1
        m,r=rl.train_task(d,"stem_identity")
        self.assertIsNone(m)
        self.assertEqual(r["status"],"NOT_ENOUGH_EXPLICIT_LABELS")

    def test_missing_label_task_does_not_train(self):
        m,r=rl.train_task(dataset(),"measurement_validity")
        self.assertIsNone(m)
        self.assertEqual(r["labelled_rows"],0)

    def test_missing_features_abstains(self):
        d=dataset();m,_=rl.train_task(d,"stem_identity")
        row=copy.deepcopy(d["records"][0]);row["human_labels"]={}
        row["features"]=[None]*len(rl.FEATURES);row["missing_feature_count"]=len(rl.FEATURES)
        self.assertIn("ABSTAIN",rl.suggest(m,row)["action"])

    def test_reuses_human_instead_of_overwriting(self):
        d=dataset();m,_=rl.train_task(d,"stem_identity")
        suggestion=rl.suggest(m,d["records"][0])
        self.assertEqual(suggestion["action"],"REUSE_HUMAN_DECISION")
        self.assertEqual(suggestion["human_label"],"TRUE_MAIN_STEM")
        self.assertFalse(suggestion["circumference_approved"])

    def test_json_model_validation(self):
        d=dataset();m,_=rl.train_task(d,"stem_identity")
        m["scale"][0]=0
        with self.assertRaises(ValueError):rl.score(m,d["records"][0]["features"])

    def test_ledger_persists_and_retrain_uses_new_vote(self):
        d=dataset()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"ledger.json"
            rl.record_review(d,path,"C-0001","stem_identity","BRANCH","SITE-A","SURVEY-1")
            rl.record_review(d,path,"C-0001","measurement_validity","MEASUREMENT_INCORRECT","SITE-A","SURVEY-1")
            events=rl.read_json(path)
            self.assertEqual(len(events),2)
            rl.apply_feedback(d,events)
            self.assertEqual(d["records"][0]["targets"],{"stem_identity":0,"measurement_validity":0})
            m,_=rl.train_task(d,"stem_identity")
            self.assertIsNotNone(m)

    def test_existing_output_stale_pointer_is_removed(self):
        d=dataset()
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)
            (out/"measurement_validity.latest.json").write_text('{"model_file":"stale.json"}')
            report=rl.train_and_save(d,out)
            self.assertFalse((out/"measurement_validity.latest.json").exists())
            self.assertTrue((out/"stem_identity.latest.json").exists())
            self.assertTrue((out/"reports"/(report["dataset_fingerprint"]+".json")).exists())

    def test_locked_ledger_refuses_second_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"ledger.json"
            path.with_suffix(".json.lock").mkdir()
            with self.assertRaises(ValueError):rl.record_review(dataset(),path,"C-0001","stem_identity","BRANCH","SITE-A","SURVEY-1")

    def test_multi_survey_manifest_and_input_hashes(self):
        q,a=fixture()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/"q.json").write_text(json.dumps(q))
            (root/"a.json").write_text(json.dumps({"annotations":a}))
            manifest={"datasets":[{"site_id":"S","survey_id":"D","queue":"q.json","annotations":["a.json"]}]}
            (root/"manifest.json").write_text(json.dumps(manifest))
            data=rl.load_dataset(root,root/"manifest.json")
            self.assertEqual(len(data["records"]),20)
            self.assertEqual(len(data["source_files"]),2)
            manifest["datasets"].append(manifest["datasets"][0])
            (root/"manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):rl.load_dataset(root,root/"manifest.json")


if __name__=="__main__":unittest.main()
