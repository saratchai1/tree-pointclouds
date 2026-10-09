#!/usr/bin/env python3
"""Learn reusable, advisory classifiers from human LiDAR review.

No LAS download, no deployment, no circumference certification. The legacy
pilot labels supervise *stem identity*, never measurement correctness.
Training requires numpy/scikit-learn; JSON-model scoring uses the stdlib only.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any

VERSION = "review-learning-v1.0.0"
SCHEMA = "pilot-geometry-features-v1"
DEFAULT_QUEUE = "outputs/review_queue_v2_phase1_75_pilot.json"
DEFAULT_ANNOTATIONS = "annotations/phase1_75_pilot_review.json"
DEFAULT_SITE = "SAMUT_SONGKHRAM_TD_008"
DEFAULT_SURVEY = "TD_008_2026_08_07_07_04_07"
IDENTITY = {"TRUE_MAIN_STEM": 1, "PROP_ROOT_OR_ROOT_ONLY": 0,
            "BRANCH": 0, "OTHER_VEGETATION": 0}
VALIDITY = {"MEASUREMENT_CORRECT": 1, "MEASUREMENT_INCORRECT": 0}
LABELS = {"stem_identity": IDENTITY, "measurement_validity": VALIDITY}
FEATURES = (
    "sampled_arc_fraction", "full_arc_fraction",
    "sampled_log_relative_fit_error", "full_log_relative_fit_error",
    "sampled_log_relative_axis_error", "full_log_relative_axis_error",
    "sampled_log_relative_radius_mad", "full_log_relative_radius_mad",
    "log_radius_expansion", "log_relative_center_shift", "full_inlier_fraction",
    "full_valid_slices", "log_sampled_point_count", "log_full_point_count",
    "sampled_components", "full_components", "source_height_count",
    "track_height_count",
)
PARAMETERS = {"C": 0.2, "solver": "liblinear", "class_weight": "balanced",
              "max_iter": 2000, "random_state": 0}


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                      allow_nan=False) + "\n"
    fd, name = tempfile.mkstemp(prefix=".review-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def nonnegative(value: Any) -> float | None:
    value = number(value)
    return value if value is not None and value >= 0 else None


def ratio(value: Any, denominator: Any) -> float | None:
    value, denominator = nonnegative(value), number(denominator)
    return value / denominator if value is not None and denominator is not None and denominator > 0 else None


def logged(value: Any) -> float | None:
    value = nonnegative(value)
    return math.log1p(value) if value is not None else None


def features(entry: dict) -> list[float | None]:
    """Allowlist only pre-review geometry. IDs, coordinates, labels are excluded."""
    sampled, full = entry.get("sampled_metrics") or {}, entry.get("full_metrics") or {}
    comparison = entry.get("comparison_metrics") or {}
    values = {}
    for prefix, source in (("sampled", sampled), ("full", full)):
        radius = source.get(prefix + "_radius_m")
        arc = ratio(source.get(prefix + "_angular_coverage_deg"), 360)
        values[prefix + "_arc_fraction"] = arc if arc is not None and 0 <= arc <= 1 else None
        for short, field in (("fit_error", "fit_residual_m"),
                             ("axis_error", "centreline_residual_p90_m"),
                             ("radius_mad", "radius_residual_mad_m")):
            values[prefix + "_log_relative_" + short] = logged(ratio(source.get(prefix + "_" + field), radius))
        values["log_" + prefix + "_point_count"] = logged(source.get(prefix + "_point_count"))
        values[prefix + "_components"] = nonnegative(source.get(prefix + "_connected_component_count"))
    expansion = ratio(full.get("full_radius_m"), sampled.get("sampled_radius_m"))
    values["log_radius_expansion"] = abs(math.log(expansion)) if expansion is not None and expansion > 0 else None
    values["log_relative_center_shift"] = logged(ratio(comparison.get("center_shift_m"), full.get("full_radius_m")))
    fraction = ratio(full.get("full_accepted_point_count"), full.get("full_point_count"))
    values["full_inlier_fraction"] = fraction if fraction is not None and 0 <= fraction <= 1 else None
    values["full_valid_slices"] = nonnegative(full.get("full_valid_slice_count"))
    values["source_height_count"] = nonnegative(entry.get("source_height_count_phase1"))
    values["track_height_count"] = nonnegative(entry.get("track_source_height_count"))
    return [values.get(name) for name in FEATURES]


def candidate_ids(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(re.findall(r"\bC-\d{4,}\b", value))
    if isinstance(value, dict):
        return set().union(*(candidate_ids(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(candidate_ids(v) for v in value)) if value else set()
    return set()


def group_candidates(entries: list[dict], annotations: list[dict]) -> dict[str, str]:
    """Conservatively keep aliases/shared tracks/nearby candidates together.

    Grouping may use human duplicate relations, but these never enter features.
    This is within-survey validation, NOT independent-plot validation.
    """
    parents: dict[str, str] = {}

    def find(key: str) -> str:
        parents.setdefault(key, key)
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        parents[max(a, b)] = min(a, b)

    tracks: dict[str, str] = {}
    for entry in entries:
        cid = entry["candidate_id"]
        find(cid)
        for other in candidate_ids(entry.get("alias_relationships", [])):
            union(cid, other)
        canonical_id = entry.get("canonical_phase1_candidate_id")
        if canonical_id:
            union(cid, str(canonical_id))
        for field in ("track_id", "physical_tree_id"):
            value = entry.get(field)
            if value:
                key = field + ":" + str(value)
                if key in tracks:
                    union(cid, tracks[key])
                tracks[key] = cid
    for annotation in annotations:
        target = annotation.get("duplicate_target")
        if annotation.get("human_label") == "DUPLICATE_OF" and target:
            union(annotation["candidate_id"], str(target))
    for i, left in enumerate(entries):
        lp = left.get("position") or {}
        x, y = number(lp.get("x")), number(lp.get("y"))
        if x is None or y is None:
            continue
        for right in entries[i + 1:]:
            rp = right.get("position") or {}
            rx, ry = number(rp.get("x")), number(rp.get("y"))
            if rx is not None and ry is not None and math.hypot(x - rx, y - ry) <= 0.75:
                union(left["candidate_id"], right["candidate_id"])
    return {entry["candidate_id"]: find(entry["candidate_id"]) for entry in entries}


def timestamp(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Review timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def newest(annotations: list[dict]) -> tuple[dict[str, dict], list[dict]]:
    grouped: dict[str, list[dict]] = {}
    ignored = []
    for row in annotations:
        if row.get("candidate_id"):
            grouped.setdefault(row["candidate_id"], []).append(row)
    selected = {}
    for cid, rows in grouped.items():
        latest = max(timestamp(row.get("timestamp")) for row in rows)
        tied = [row for row in rows if timestamp(row.get("timestamp")) == latest]
        if len({(row.get("human_label"), row.get("duplicate_target")) for row in tied}) > 1:
            ignored.append({"candidate_id": cid, "reason": "CONFLICTING_LATEST_REVIEWS"})
        else:
            selected[cid] = tied[-1]
    return selected, ignored


def make_dataset(queue: dict, annotations: list[dict], site_id: str, survey_id: str) -> dict:
    entries = [row for row in queue.get("entries", []) if row.get("candidate_id")
               and row.get("item_type", "CANDIDATE_EVIDENCE") == "CANDIDATE_EVIDENCE"]
    ids = [entry["candidate_id"] for entry in entries]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate candidate_id in queue; reconcile before learning")
    groups = group_candidates(entries, annotations)
    latest, ignored = newest(annotations)
    records = []
    pipeline = queue.get("algorithm_version")
    for entry in entries:
        cid = entry["candidate_id"]
        annotation = latest.get(cid)
        label = annotation.get("human_label") if annotation else None
        # Legacy annotation must match the reviewed generation, not a newer fit.
        if annotation and annotation.get("algorithm_version") != pipeline:
            ignored.append({"candidate_id": cid, "reason": "REVIEW_PIPELINE_MISMATCH"})
            label = None
        values = features(entry)
        evidence_hash = digest({"site": site_id, "survey": survey_id, "pipeline": pipeline,
                                "entry": entry, "inputs": queue.get("locked_input_hashes", {})})
        records.append({
            "site_id": site_id, "survey_id": survey_id, "candidate_id": cid,
            "group_id": site_id + "/" + survey_id + "/" + groups[cid],
            "pipeline_version": pipeline, "evidence_hash": evidence_hash,
            "feature_schema": SCHEMA, "features": values,
            "targets": {"stem_identity": IDENTITY.get(label), "measurement_validity": None},
            "human_labels": {"stem_identity": label, "measurement_validity": None},
            "baseline_stem_like": entry.get("candidate_geometry_status") == "STEM_LIKE",
            "missing_feature_count": sum(value is None for value in values),
        })
    for cid in set(latest) - set(ids):
        ignored.append({"candidate_id": cid, "reason": "NO_MATCHING_CANDIDATE_EVIDENCE"})
    counts = Counter(row["human_labels"]["stem_identity"] or "UNREVIEWED" for row in records)
    return {"schema_version": 1, "feature_schema": SCHEMA, "feature_names": list(FEATURES),
            "records": records, "audit": {"legacy_label_counts": dict(counts), "ignored": ignored,
            "manual_seeds_policy": "Retain upstream; hints are not measurement-correct labels",
            "duplicate_policy": "Grouped, never negative identity examples",
            "unknown_policy": "Unlabelled, never negative examples"}}


def load_dataset(root: Path, manifest_path: Path | None = None) -> dict:
    manifest = read_json(manifest_path) if manifest_path else {"datasets": [{
        "site_id": DEFAULT_SITE, "survey_id": DEFAULT_SURVEY,
        "queue": DEFAULT_QUEUE, "annotations": [DEFAULT_ANNOTATIONS]}]}
    combined = {"schema_version": 1, "feature_schema": SCHEMA,
                "feature_names": list(FEATURES), "records": [], "audit": [], "source_files": []}
    seen = set()
    for item in manifest["datasets"]:
        site, survey = item["site_id"], item["survey_id"]
        if (site, survey) in seen:
            raise ValueError("Duplicate survey in manifest; list its annotation files together")
        seen.add((site, survey))
        queue_path = root / item["queue"]
        queue = read_json(queue_path)
        labels = []
        for relative in item["annotations"]:
            path = root / relative
            labels.extend(read_json(path).get("annotations", []))
            combined["source_files"].append({"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        data = make_dataset(queue, labels, site, survey)
        combined["records"].extend(data["records"])
        combined["audit"].append({"site_id": site, "survey_id": survey, **data["audit"]})
        combined["source_files"].append({"path": item["queue"], "sha256": hashlib.sha256(queue_path.read_bytes()).hexdigest()})
    return combined


def apply_feedback(data: dict, events: list[dict]) -> None:
    """Only exact-snapshot, explicit task labels are eligible for reuse/training."""
    index = {(r["site_id"], r["survey_id"], r["candidate_id"], r["evidence_hash"]): r for r in data["records"]}
    grouped: dict[tuple, list[dict]] = {}
    ignored = []
    for event in events:
        key = tuple(event.get(k) for k in ("site_id", "survey_id", "candidate_id", "evidence_hash"))
        task = event.get("task")
        if key not in index or task not in LABELS or event.get("feature_schema") != SCHEMA:
            ignored.append({"event_id": event.get("event_id"), "reason": "STALE_OR_INCOMPATIBLE_FEEDBACK"})
            continue
        grouped.setdefault((*key, task), []).append(event)
    for key, rows in grouped.items():
        task = key[-1]
        newest_time = max(timestamp(row.get("timestamp")) for row in rows)
        latest = [row for row in rows if timestamp(row.get("timestamp")) == newest_time]
        record = index[key[:-1]]
        if len({row.get("label") for row in latest}) > 1:
            record["targets"][task] = None
            record["human_labels"][task] = "CONFLICTING_REVIEWS"
            ignored.append({"candidate_id": record["candidate_id"], "reason": "CONFLICTING_FEEDBACK"})
        else:
            label = latest[-1]["label"]
            if label not in LABELS[task] and label != "NOT_ENOUGH_INFORMATION":
                raise ValueError(f"Invalid {task} label: {label}")
            record["targets"][task] = LABELS[task].get(label)
            record["human_labels"][task] = label
    data["feedback_audit"] = ignored


def metric(y: list[int], predictions: list[int]) -> dict:
    tp = sum(a == 1 and b == 1 for a, b in zip(y, predictions))
    tn = sum(a == 0 and b == 0 for a, b in zip(y, predictions))
    fp = sum(a == 0 and b == 1 for a, b in zip(y, predictions))
    fn = sum(a == 1 and b == 0 for a, b in zip(y, predictions))
    safe = lambda a, b: a / b if b else None
    tpr, tnr = safe(tp, tp + fn), safe(tn, tn + fp)
    return {"count": len(y), "true_positive": tp, "true_negative": tn,
            "false_positive": fp, "false_negative": fn, "precision": safe(tp, tp + fp),
            "recall": tpr, "specificity": tnr,
            "balanced_accuracy": (tpr + tnr) / 2 if tpr is not None and tnr is not None else None}


def fit_json(rows: list[dict], task: str) -> dict:
    import numpy as np
    from sklearn.impute import SimpleImputer
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import LogisticRegression
    x = np.asarray([[float("nan") if v is None else v for v in r["features"]] for r in rows], dtype=float)
    y = np.asarray([r["targets"][task] for r in rows], dtype=int)
    imputer = SimpleImputer(strategy="median", keep_empty_features=True)
    scaler = StandardScaler()
    filled = imputer.fit_transform(x)
    scaled = scaler.fit_transform(filled)
    design = np.column_stack((scaled, np.isnan(x).astype(float)))
    model = LogisticRegression(**PARAMETERS).fit(design, y)
    result = {"schema_version": 1, "feature_schema": SCHEMA, "feature_names": list(FEATURES),
              "task": task, "estimator": "standardized-logistic-regression-with-missing-flags",
              "impute": imputer.statistics_.tolist(), "mean": scaler.mean_.tolist(),
              "scale": scaler.scale_.tolist(), "coef": model.coef_[0].tolist(),
              "intercept": float(model.intercept_[0]), "parameters": PARAMETERS,
              "usage_mode": "ADVISORY_ONLY", "score_is_calibrated_probability": False,
              "certifies_circumference": False}
    # Verify exported JSON scoring matches the fitted estimator before saving it.
    expected = model.predict_proba(design)[:, 1]
    actual = [score(result, r["features"]) for r in rows]
    if not np.allclose(expected, actual, atol=1e-10, rtol=1e-10):
        raise RuntimeError("JSON inference parity failed")
    return result


def score(model: dict, values: list[float | None]) -> float:
    n = len(FEATURES)
    if model.get("feature_schema") != SCHEMA or model.get("feature_names") != list(FEATURES) or len(values) != n:
        raise ValueError("Feature schema mismatch")
    if model.get("task") not in LABELS:
        raise ValueError("Unknown model task")
    for field in ("impute", "mean", "scale"):
        if len(model.get(field, [])) != n or any(number(v) is None for v in model[field]):
            raise ValueError("Invalid model vector: " + field)
    if len(model.get("coef", [])) != 2 * n or any(number(v) is None for v in model["coef"]):
        raise ValueError("Invalid coefficients")
    if number(model.get("intercept")) is None or any(v <= 0 for v in model["scale"]):
        raise ValueError("Invalid model intercept/scale")
    z = model["intercept"]
    for i, value in enumerate(values):
        missing = number(value) is None
        value = model["impute"][i] if missing else float(value)
        z += model["coef"][i] * ((value - model["mean"][i]) / model["scale"][i])
        z += model["coef"][n + i] * int(missing)
    return 1 / (1 + math.exp(-max(-700, min(700, z))))


def train_task(data: dict, task: str) -> tuple[dict | None, dict]:
    rows = [r for r in data["records"] if r["targets"][task] in (0, 1)
            and r["missing_feature_count"] <= len(FEATURES) // 2]
    counts = Counter(r["targets"][task] for r in rows)
    report = {"task": task, "labelled_rows": len(rows), "positive": counts[1], "negative": counts[0],
              "site_count": len({r["site_id"] for r in rows}), "parameters_frozen_before_evaluation": PARAMETERS,
              "production_ready": False, "field_accuracy_measured": False,
              "reason": "Advisory model; independent-plot validation and calibration are not established"}
    if min(counts[0], counts[1]) < 2:
        report["status"] = "NOT_ENOUGH_EXPLICIT_LABELS"
        return None, report
    model = fit_json(rows, task)
    groups = sorted({r["group_id"] for r in rows})
    evaluations, skipped = [], []
    for group in groups:
        training = [r for r in rows if r["group_id"] != group]
        testing = [r for r in rows if r["group_id"] == group]
        if len({r["targets"][task] for r in training}) < 2:
            skipped.append(group)
            continue
        fold_model = fit_json(training, task)
        majority = int(sum(r["targets"][task] for r in training) >= len(training) / 2)
        for row in testing:
            evaluations.append({"candidate_id": row["candidate_id"], "site_id": row["site_id"],
                "group_id": group, "truth": row["targets"][task], "score": score(fold_model, row["features"]),
                "baseline_majority": majority, "baseline_geometry": int(row["baseline_stem_like"])})
    truth = [r["truth"] for r in evaluations]
    learned = metric(truth, [int(r["score"] >= 0.5) for r in evaluations])
    baseline = metric(truth, [r["baseline_majority"] for r in evaluations])
    report.update({"status": "TRAINED_ADVISORY", "evaluation": "leave-one-conservative-candidate-group-out",
                   "evaluated_rows": len(evaluations), "skipped_groups": skipped, "group_count": len(groups),
                   "learned_at_0_5": learned, "majority_baseline": baseline,
                   "cross_plot_validation_performed": False, "oof_predictions": evaluations,
                   "thresholds": {}})
    if task == "stem_identity":
        report["existing_geometry_stem_like_baseline"] = metric(truth, [r["baseline_geometry"] for r in evaluations])
    for threshold in (0.5, 0.8, 0.9):
        prediction = [int(r["score"] >= threshold) for r in evaluations]
        report["thresholds"][str(threshold)] = metric(truth, prediction)
    learned_ba, base_ba = learned["balanced_accuracy"], baseline["balanced_accuracy"]
    report["beats_majority_on_grouped_balanced_accuracy"] = (
        learned_ba is not None and base_ba is not None and learned_ba > base_ba)
    model["training_fingerprint"] = digest({"records": rows, "parameters": PARAMETERS, "version": VERSION})
    model["training_count"] = len(rows)
    model["training_sites"] = sorted({r["site_id"] for r in rows})
    model["model_id"] = task + "-" + digest(model)[:16]
    return model, report


def suggest(model: dict, record: dict) -> dict:
    task = model["task"]
    label = record.get("human_labels", {}).get(task)
    base = {"candidate_id": record["candidate_id"], "site_id": record["site_id"],
            "survey_id": record["survey_id"], "evidence_hash": record["evidence_hash"],
            "model_id": model.get("model_id"), "task": task, "circumference_approved": False}
    if label:
        return {**base, "action": "REUSE_HUMAN_DECISION", "human_label": label, "score": None}
    if record.get("feature_schema") != SCHEMA or record["missing_feature_count"] > len(FEATURES) // 2:
        return {**base, "action": "ABSTAIN_INCOMPATIBLE_OR_INSUFFICIENT_FEATURES", "score": None}
    value = score(model, record["features"])
    action = "REVIEW_UNCERTAIN"
    if value <= 0.2:
        action = "REVIEW_LIKELY_NON_STEM" if task == "stem_identity" else "REVIEW_LIKELY_BAD_FIT"
    elif value >= 0.8:
        action = "LIKELY_STEM_NOT_MEASUREMENT_APPROVAL" if task == "stem_identity" else "LIKELY_VALID_FIT_STILL_UNVERIFIED"
    return {**base, "action": action, "score": value,
            "new_site_warning": record["site_id"] not in model.get("training_sites", [])}


def train_and_save(data: dict, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    report = {"version": VERSION, "dataset_fingerprint": digest(data), "record_count": len(data["records"]),
              "audit": data.get("audit"), "feedback_audit": data.get("feedback_audit", []),
              "source_files": data.get("source_files", []), "tasks": {}}
    atomic_write(output / "dataset.json", data)
    suggestions = []
    for task in LABELS:
        model, task_report = train_task(data, task)
        report["tasks"][task] = task_report
        if model:
            atomic_write(output / (model["model_id"] + ".json"), model)
            # Pointer changes only inside this local run directory, never deployed models.
            atomic_write(output / (task + ".latest.json"), {"model_file": model["model_id"] + ".json"})
            suggestions.extend(suggest(model, row) for row in data["records"])
        else:
            (output / (task + ".latest.json")).unlink(missing_ok=True)
    atomic_write(output / "reports" / (report["dataset_fingerprint"] + ".json"), report)
    atomic_write(output / "report.json", report)
    atomic_write(output / "suggestions.json", {"usage_mode": "ADVISORY_ONLY", "records": suggestions})
    return report


def record_review(data: dict, ledger: Path, candidate: str, task: str, label: str,
                  site_id: str, survey_id: str) -> dict:
    if task not in LABELS or label not in {*LABELS[task], "NOT_ENOUGH_INFORMATION"}:
        raise ValueError("Unknown task or label")
    matches = [r for r in data["records"] if r["candidate_id"] == candidate
               and r["site_id"] == site_id and r["survey_id"] == survey_id]
    if len(matches) != 1:
        raise ValueError("Review requires exactly one site/survey/candidate match")
    row = matches[0]
    event = {k: row[k] for k in ("site_id", "survey_id", "candidate_id", "evidence_hash", "feature_schema")}
    event.update({"task": task, "label": label, "timestamp": datetime.now(timezone.utc).isoformat()})
    event["event_id"] = digest(event)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    lock = ledger.with_suffix(ledger.suffix + ".lock")
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise ValueError("Review ledger is locked; retry after the other writer finishes") from exc
    try:
        events = read_json(ledger) if ledger.exists() else []
        events.append(event)
        atomic_write(ledger, events)
    finally:
        lock.rmdir()
    return event


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("train", "review", "score"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path, default=Path(".feedback-learning"))
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--candidate")
    parser.add_argument("--site-id", default=DEFAULT_SITE)
    parser.add_argument("--survey-id", default=DEFAULT_SURVEY)
    parser.add_argument("--task", choices=tuple(LABELS), default="stem_identity")
    parser.add_argument("--label")
    parser.add_argument("--model", type=Path)
    parser.add_argument("--queue", type=Path)
    args = parser.parse_args()
    if args.command == "score":
        if not args.model or not args.queue:
            parser.error("score requires --model and --queue")
        model = read_json(args.model)
        dataset = make_dataset(read_json(args.queue), [], args.site_id, args.survey_id)
        if args.ledger and args.ledger.exists():
            apply_feedback(dataset, read_json(args.ledger))
        atomic_write(args.output / "suggestions.json", {"usage_mode": "ADVISORY_ONLY",
            "records": [suggest(model, r) for r in dataset["records"]]})
        print("Saved advisory suggestions; original measurements unchanged.")
        return
    if args.command == "review" and (not args.candidate or not args.label):
        parser.error("review requires --candidate and --label")
    args.output.mkdir(parents=True, exist_ok=True)
    run_lock = args.output / ".training.lock"
    try:
        run_lock.mkdir()
    except FileExistsError as exc:
        raise ValueError("Another training/review command owns this output directory") from exc
    try:
        dataset = load_dataset(args.root, args.manifest)
        ledger = args.ledger or args.output / "human-review-ledger.json"
        if args.command == "review":
            record_review(dataset, ledger, args.candidate, args.task, args.label, args.site_id, args.survey_id)
        if ledger.exists():
            apply_feedback(dataset, read_json(ledger))
        result = train_and_save(dataset, args.output)
    finally:
        run_lock.rmdir()
    compact = {k: v for k, v in result.items() if k not in ("audit", "source_files")}
    compact["tasks"] = {k: {a: b for a, b in v.items() if a != "oof_predictions"} for k, v in result["tasks"].items()}
    print(json.dumps(compact, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, ImportError) as error:
        print(f"review-learning: {error}", file=sys.stderr)
        sys.exit(2)
