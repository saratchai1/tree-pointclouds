const DEFAULT_LABELS = [
  "TRUE_MAIN_STEM", "PROP_ROOT_OR_ROOT_ONLY", "BRANCH", "OTHER_VEGETATION",
  "DUPLICATE_OF", "NOT_ENOUGH_INFORMATION", "MANUAL_REVIEW_REQUIRED"
];
const LEGACY_PHASE1_75_ANNOTATION_PATH = "annotations/phase1_75_pilot_review.json";
const $ = (id) => document.getElementById(id);
const state = {
  manifests: [], queueId: null, payload: null, queue: [], filtered: [], current: null,
  evidence: null, crop: null, annotations: {}, manualSeeds: [], selectedLabel: null,
  phase3Candidates: {}, phase3Trees: {}, phase4Trees: {},
  roiCensus: null, roiEditMode: null, selectedReferenceId: null, selectedPredictionId: null,
  roiHeightBand: "STEM", roiAlgorithmOverlay: false, roiPickFeedback: "", roiWorkflowStep: "CENSUS",
  manualMode: false, phase5aPickMode: false, view: { yaw: -0.55, pitch: 0.35, zoom: 190 }, drag: null,
};

function download(name, value, type = "application/json") {
  const blob = new Blob([value], { type });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob); link.download = name; link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}
function unique(values) { return [...new Set(values.filter(Boolean))].sort(); }
function option(select, value, label = value) { const node = document.createElement("option"); node.value = value; node.textContent = label; select.append(node); }
function format(value, digits = 3) { return value == null || value === "" ? "—" : typeof value === "number" ? value.toFixed(digits) : String(value); }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/g, (x) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[x])); }
function itemKey(item) { return item?.candidate_id || item?.review_item_id || item?.queue_id; }
function storageKey(kind) { return `v2-${state.queueId}-${kind}`; }
function currentVersion() { return state.payload?.algorithm_version || "stem-inventory-v2-phase1_5"; }
function allowedLabels() { return state.payload?.annotation_labels || DEFAULT_LABELS; }
function metric(object, ...keys) { for (const key of keys) if (object?.[key] != null) return object[key]; return null; }
function isRoiItem(item = state.current) { return ["GROUND_TRUTH_ROI", "LIDAR_VISIBLE_REFERENCE_CENSUS"].includes(item?.item_type); }
function isPhase5aItem(item = state.current) { return item?.item_type === "PHASE5A_PROP_ROOT_POM"; }
function defaultViewFor(item = state.current) { return isRoiItem(item) ? { yaw: -0.55, pitch: 0.35, zoom: 125 } : isPhase5aItem(item) ? { yaw: -0.55, pitch: 0.28, zoom: 175 } : { yaw: -0.55, pitch: 0.35, zoom: 190 }; }
function roiTopView() { return { yaw: 0, pitch: Math.PI / 2, zoom: 92 }; }
function roiHeightRange() { return state.roiHeightBand === "LOW" ? [0.3, 1.5] : state.roiHeightBand === "STEM" ? [1.5, 3.2] : null; }
function roiHeightLabel() { return state.roiHeightBand === "LOW" ? "โคน 0.3–1.5 ม." : state.roiHeightBand === "STEM" ? "ลำต้น 1.5–3.2 ม." : "ทุกระดับ 0.3–4.2 ม."; }
function clone(value) { return JSON.parse(JSON.stringify(value)); }
function commaIds(value) { return [...new Set(String(value || "").split(",").map((x) => x.trim()).filter(Boolean))].sort(); }

function loadLocal() {
  state.annotations = JSON.parse(localStorage.getItem(storageKey("annotations")) || "{}");
  state.manualSeeds = JSON.parse(localStorage.getItem(storageKey("manual-seeds")) || "[]");
  const savedCensus = localStorage.getItem(storageKey("roi-census"));
  state.roiCensus = savedCensus ? JSON.parse(savedCensus) : state.payload?.annotation_template ? clone(state.payload.annotation_template) : null;
  state.selectedReferenceId = state.roiCensus?.reference_trees?.[0]?.reference_tree_id || null;
  state.selectedPredictionId = state.current?.resolved_tree_ids?.[0] || null;
}
function saveLocal() {
  localStorage.setItem(storageKey("annotations"), JSON.stringify(state.annotations));
  localStorage.setItem(storageKey("manual-seeds"), JSON.stringify(state.manualSeeds));
  if (state.roiCensus) localStorage.setItem(storageKey("roi-census"), JSON.stringify(state.roiCensus));
  $("manualCount").textContent = `${state.manualSeeds.length} manual seeds`;
}

async function init() {
  const manifestRequest = Promise.all([
    fetch("data/queues.json", { cache: "no-store" }).then((r) => {
      if (!r.ok) throw new Error(`queue manifest ${r.status}`); return r.json();
    }),
    fetch("data/phase4-queues.json", { cache: "no-store" })
      .then((r) => r.ok ? r.json() : { queues: [] })
      .catch(() => ({ queues: [] })),
  ]).then(([base, extension]) => {
    const queues = new Map(base.queues.map((item) => [item.queue_id, item]));
    (extension.queues || []).forEach((item) => queues.set(item.queue_id, item));
    return { default_queue_id: base.default_queue_id, queues: [...queues.values()] };
  });
  const phase3Request = Promise.all([
    fetch("data/phase3_candidate_tree_associations.json", { cache: "no-store" }).then((r) => r.ok ? r.json() : null),
    fetch("data/phase3_tree_inventory.json", { cache: "no-store" }).then((r) => r.ok ? r.json() : null),
    fetch("data/phase4_tree_inventory.json", { cache: "no-store" }).then((r) => r.ok ? r.json() : null),
  ]).catch(() => [null, null, null]);
  const [manifest, phase3Payloads] = await Promise.all([manifestRequest, phase3Request]);
  const [phase3Associations, phase3Inventory, phase4Inventory] = phase3Payloads;
  state.phase3Candidates = Object.fromEntries((phase3Associations?.candidate_associations || []).map((item) => [item.candidate_id, item]));
  state.phase3Trees = Object.fromEntries((phase3Inventory?.trees || []).map((item) => [item.tree_id, item]));
  state.phase4Trees = Object.fromEntries((phase4Inventory?.trees || []).map((item) => [item.tree_id, item]));
  state.manifests = manifest.queues;
  state.manifests.forEach((item) => option($("queueSelect"), item.queue_id, item.label));
  bind(); renderLabelActions();
  $("queueSelect").value = manifest.default_queue_id;
  await loadQueue(manifest.default_queue_id);
}

async function loadQueue(queueId) {
  const descriptor = state.manifests.find((item) => item.queue_id === queueId);
  if (!descriptor) throw new Error(`Unknown queue ${queueId}`);
  $("summary").textContent = "กำลังโหลด review queue…";
  const payload = await fetch(descriptor.url, { cache: "no-store" }).then((r) => {
    if (!r.ok) throw new Error(`review queue ${r.status}`); return r.json();
  });
  state.queueId = queueId; state.payload = payload;
  state.queue = payload.entries.map((item) => ({ item_type: "CANDIDATE_EVIDENCE", ...item }));
  state.current = null; state.evidence = null; state.crop = null;
  loadLocal(); saveLocal(); renderLabelActions(); resetFilterOptions();
  $("summary").textContent = payload.annotation_basis === "LIDAR_VISIBLE_REFERENCE"
    ? `${payload.queue_size.toLocaleString()} ROI workflow · LiDAR-visible reference ไม่ใช่ field ground truth`
    : `${payload.queue_size.toLocaleString()} review items · geometry evidence ยังไม่ใช่ verified trees`;
  applyFilters(); resize();
}

function resetFilterOptions() {
  const configs = [
    ["geometryFilter", unique(state.queue.map((x) => x.candidate_geometry_status))],
    ["measurementFilter", unique(state.queue.map((x) => x.measurement_status))],
    ["providerFilter", unique(state.queue.flatMap((x) => x.source_providers || []))],
  ];
  configs.forEach(([id, values]) => { const select = $(id); select.innerHTML = '<option value="">ทั้งหมด</option>'; values.forEach((value) => option(select, value)); });
  ["ruleFilter", "duplicateFilter", "unresolvedFilter"].forEach((id) => { if ($(id).type === "checkbox") $(id).checked = false; else $(id).value = ""; });
}

function bind() {
  $("queueSelect").addEventListener("change", () => loadQueue($("queueSelect").value));
  ["geometryFilter", "measurementFilter", "providerFilter", "ruleFilter", "duplicateFilter", "unresolvedFilter"]
    .forEach((id) => $(id).addEventListener("change", applyFilters));
  $("candidateSelect").addEventListener("change", () => selectCandidate($("candidateSelect").value));
  $("nextUnresolved").addEventListener("click", nextUnresolved);
  $("resetView").addEventListener("click", () => { state.view = defaultViewFor(); draw(); });
  $("topView").addEventListener("click", () => { state.view = isRoiItem() ? roiTopView() : { ...state.view, pitch: Math.PI / 2 }; draw(); });
  $("saveAnnotation").addEventListener("click", saveAnnotation);
  $("savePhase5aAnnotation").addEventListener("click", savePhase5aAnnotation);
  $("phase5aPickAttachment").addEventListener("click", togglePhase5aPickMode);
  $("phase5aUseCandidate").addEventListener("click", usePhase5aCandidate);
  $("phase5aApplicability").addEventListener("change", normalizePhase5aApplicabilityControls);
  ["phase5aAttachmentZ", "phase5aAttachmentStatus"].forEach((id) => $(id).addEventListener("input", updatePhase5aPomPreview));
  $("saveRoiAnnotation").addEventListener("click", saveRoiAnnotation);
  $("exportAnnotations").addEventListener("click", exportAnnotations);
  $("importAnnotations").addEventListener("change", importAnnotations);
  $("exportProgress").addEventListener("click", exportProgress);
  $("manualMode").addEventListener("click", toggleManualMode);
  $("addManualSeed").addEventListener("click", addManualSeed);
  $("exportManualSeeds").addEventListener("click", exportManualSeeds);
  $("addRoiReference").addEventListener("click", () => setRoiEditMode("ADD"));
  $("moveRoiReference").addEventListener("click", () => setRoiEditMode("MOVE"));
  $("deleteRoiReference").addEventListener("click", deleteRoiReference);
  document.querySelectorAll("[data-roi-band]").forEach((button) => button.addEventListener("click", () => setRoiHeightBand(button.dataset.roiBand)));
  $("toggleRoiAlgorithm").addEventListener("click", toggleRoiAlgorithmOverlay);
  $("resetRoiDraft").addEventListener("click", resetRoiDraft);
  $("roiReferenceSelect").addEventListener("change", () => { state.selectedReferenceId = $("roiReferenceSelect").value || null; renderRoiCensus(); draw(); });
  $("saveRoiReference").addEventListener("click", saveRoiReference);
  $("markRoiReferenceNoEvidence").addEventListener("click", markRoiReferenceNoEvidence);
  $("markRoiReferenceUncertain").addEventListener("click", markRoiReferenceUncertain);
  $("roiEvidenceStatus").addEventListener("change", () => { $("roiEvidenceStatus").dataset.explicit = "true"; });
  $("roiPredictionSelect").addEventListener("change", () => { state.selectedPredictionId = $("roiPredictionSelect").value || null; state.roiAlgorithmOverlay = true; renderRoiCensus(); draw(); });
  ["predictionClassification", "predictionReferenceIds", "predictionReviewerNote"].forEach((id) => $(id).addEventListener("input", markPredictionUnsaved));
  $("saveRoiPrediction").addEventListener("click", saveRoiPrediction);
  const canvas = $("pointCanvas");
  canvas.addEventListener("pointerdown", pointerDown); canvas.addEventListener("pointermove", pointerMove);
  canvas.addEventListener("pointerup", pointerUp); canvas.addEventListener("pointercancel", pointerUp);
  canvas.addEventListener("wheel", (event) => { event.preventDefault(); state.view.zoom *= Math.exp(-event.deltaY * .001); state.view.zoom = Math.max(45, Math.min(650, state.view.zoom)); draw(); }, { passive: false });
  window.addEventListener("resize", resize);
}

function applyFilters() {
  const geometry = $("geometryFilter").value, measurement = $("measurementFilter").value;
  const provider = $("providerFilter").value, rule = $("ruleFilter").value;
  state.filtered = state.queue.filter((item) =>
    (!geometry || item.candidate_geometry_status === geometry) &&
    (!measurement || item.measurement_status === measurement) &&
    (!provider || (item.source_providers || []).includes(provider)) &&
    (!rule || (rule === "NONE" ? !item.measurement_rule : item.measurement_rule === rule)) &&
    (!$("duplicateFilter").checked || item.potential_duplicate || (item.alias_relationships || []).length) &&
    (!$("unresolvedFilter").checked || !state.annotations[itemKey(item)])
  );
  const select = $("candidateSelect"); select.innerHTML = "";
  state.filtered.forEach((item) => option(select, itemKey(item), `${itemKey(item)} · ${item.measurement_status || item.item_type}`));
  $("filterCount").textContent = `${state.filtered.length.toLocaleString()} / ${state.queue.length.toLocaleString()} รายการ`;
  if (!state.filtered.length) { state.current = null; state.evidence = null; state.crop = null; renderDetails(); draw(); drawProfile(); return; }
  const prior = itemKey(state.current);
  const desired = prior && state.filtered.some((x) => itemKey(x) === prior) ? prior : itemKey(state.filtered[0]);
  select.value = desired; selectCandidate(desired);
}

async function selectCandidate(key) {
  state.current = state.queue.find((x) => itemKey(x) === key);
  if (!state.current) return;
  state.phase5aPickMode = false; $("pointCanvas").classList.remove("phase5a-pick");
  state.view = defaultViewFor(state.current);
  if (isRoiItem(state.current)) { state.roiHeightBand = "STEM"; state.roiAlgorithmOverlay = false; state.roiWorkflowStep = "CENSUS"; }
  $("candidateSelect").value = key; state.crop = null; state.evidence = null; renderDetails(); draw(); drawProfile();
  const requests = [];
  if (state.current.point_crop_url) requests.push(fetch(state.current.point_crop_url, { cache: "no-store" }).then((r) => r.ok ? r.json() : Promise.reject(new Error(String(r.status)))).then((data) => { state.crop = data; }));
  if (state.current.evidence_url) requests.push(fetch(state.current.evidence_url, { cache: "no-store" }).then((r) => r.ok ? r.json() : Promise.reject(new Error(String(r.status)))).then((data) => { state.evidence = data; }));
  try { await Promise.all(requests); }
  catch (error) { state.crop = state.crop || { sampled_points_xyz: [], full_accepted_points_xyz: [], full_rejected_points_xyz: [], error: error.message }; }
  loadAnnotation(); renderDetails(); draw(); drawProfile();
}

function renderDetails() {
  const item = state.current;
  if (!item) { document.body.classList.remove("roi-mode"); $("phase5aAnnotation").hidden=true; $("candidateTitle").textContent = "—"; $("metrics").innerHTML = ""; $("aliases").innerHTML = ""; $("failures").innerHTML = ""; $("componentRows").innerHTML = ""; return; }
  const placeholder = item.item_type === "MANUAL_SEED_PLACEHOLDER";
  const roiItem = isRoiItem(item);
  const phase4cItem = item.item_type === "PHASE4C_STRUCTURE";
  const phase5aItem = isPhase5aItem(item);
  document.body.classList.toggle("roi-mode", roiItem);
  $("candidateAnnotation").hidden = roiItem || phase5aItem;
  $("phase5aAnnotation").hidden = !phase5aItem;
  $("roiAnnotation").hidden = !roiItem;
  $("roiCanvasTools").hidden = !roiItem; $("roiLegend").hidden = !roiItem; $("geometryLegend").hidden = roiItem;
  $("manualSeedPanel").hidden = roiItem || phase4cItem || phase5aItem;
  $("roiCensusStageA").hidden = !roiItem || state.roiWorkflowStep !== "CENSUS";
  $("roiAssociationWorkflow").hidden = !roiItem || state.roiWorkflowStep !== "ASSOCIATE";
  $("resetView").textContent = roiItem ? "มุมดูลำต้น" : "รีเซ็ตมุม";
  $("topView").hidden = false;
  $("candidateTitle").textContent = itemKey(item);
  $("candidateState").textContent = `${item.candidate_geometry_status || "—"} · ${item.identity_status || "—"}`;
  if (roiItem) {
    const roiId = item.review_item_id || state.roiCensus?.frozen_roi?.roi_id || "ROI";
    $("roiCensusTitle").textContent = `${roiId} · LiDAR-visible reference census`;
    $("roiStepCText").textContent = `ขั้น C: ตรวจ Tree ID ทั้ง ${(item.resolved_tree_ids || []).length} รายการทางขวา แล้วจึงยืนยันว่าตรวจครบ`;
    $("roiPredictionIntro").textContent = `ตรวจทั้ง ${(item.resolved_tree_ids || []).length} รายการว่าคือหนึ่งต้นจริง, ID ซ้ำ, ราก/กิ่ง หรือไม่ใช่ต้นไม้ ระยะ XY เป็นเพียงคำแนะนำ`;
    $("predictionReferenceIds").placeholder = `${String(roiId).replaceAll("-", "_")}_REF_0001`;
  }
  $("queueCategories").innerHTML = (item.categories || []).map((x) => `<span class="chip">${escapeHtml(x)}</span>`).join("");
  const sample = item.sampled_metrics || {}, full = item.full_metrics || {};
  const phase3Association = state.phase3Candidates[item.candidate_id] || null;
  const phase4TreeId = item.phase4_tree_id || phase3Association?.tree_id || null;
  const phase3Tree = phase4TreeId ? (state.phase4Trees[phase4TreeId] || state.phase3Trees[phase4TreeId]) : null;
  const phase3Rows = phase3Tree ? [
    ["Phase 3 Tree ID", phase3Tree.tree_id],
    ["Tree detection", phase3Tree.detection?.status],
    ["Source candidates", (phase3Tree.source_candidates || []).join(", ")],
    ["Vertical stem range", `${format(phase3Tree.stem?.z_min_agl_m, 2)}–${format(phase3Tree.stem?.z_max_agl_m, 2)} m AGL`],
    ["Tree measurement", `${phase3Tree.measurement?.status || "—"} / ${format(phase3Tree.measurement?.pom_m, 2)} m / ${format(phase3Tree.measurement?.circumference_cm, 2)} cm`],
    ["Merged candidates", phase3Tree.association?.candidate_count > 1 ? "YES" : "NO"],
  ] : phase3Association ? [
    ["Phase 3 Tree ID", "not assigned"],
    ["Candidate disposition", phase3Association.disposition],
  ] : [];
  const phase4c = state.evidence?.phase4c || {};
  const proposal = phase4c.classification || {};
  const structureFeature = phase4c.feature || {};
  const phase5aEvidence = state.evidence?.phase5a || {};
  const phase5aProtocol = phase5aEvidence.measurement_protocol || {};
  const phase5aAttachment = phase5aEvidence.highest_prop_root_attachment || {};
  const phase5aPom = phase5aEvidence.protocol_pom || {};
  const phase5aMeasurement = phase5aEvidence.measurement || {};
  const rows = roiItem ? [
    ["ROI", item.review_item_id],
    ["Current hypotheses", item.current_resolved_hypothesis_count],
    ["Unresolved tracks", item.unresolved_track_count],
    ["LiDAR-visible references", state.roiCensus?.reference_trees?.length || 0],
    ["Prediction reviews", `${state.roiCensus?.prediction_reviews?.length || 0} / ${(item.resolved_tree_ids || []).length}`],
    ["Census status", state.roiCensus?.annotation_status || "NOT_STARTED"],
    ["Measurement", "not applicable"],
  ] : placeholder ? [
    ["Workflow", "manual large-root seed"], ["Human label", "TRUE_MAIN_STEM"],
    ["Clean-height hint", "2.50 m (hint only)"], ["Automatic final POM", "NO"],
  ] : phase5aItem ? [
    ["Physical Tree ID", item.phase4_tree_id || item.review_item_id],
    ["Tree detection", item.tree_detection_status],
    ["Protocol applicability", phase5aProtocol.applicability || item.protocol_applicability],
    ["Reference landmark", phase5aProtocol.reference_landmark || "HIGHEST_PROP_ROOT_ATTACHMENT"],
    ["Protocol offset", `${format(phase5aProtocol.offset_m ?? item.protocol_offset_m, 2)} m vertical (not 1.30 m)`],
    ["Slice orientation", phase5aProtocol.slice_orientation || "PERPENDICULAR_TO_LOCAL_STEM_AXIS"],
    ["Main-stem axis", `${phase5aEvidence.main_stem?.axis_status || "—"} · uncertainty ${format(phase5aEvidence.main_stem?.axis_uncertainty_m, 3)} m`],
    ["Highest attachment", `${phase5aAttachment.status || item.attachment_status} · ${format(phase5aAttachment.height_agl_m, 3)} m AGL`],
    ["Protocol POM", `${phase5aPom.status || item.protocol_pom_status} · ${format(phase5aPom.height_agl_m, 3)} m AGL`],
    ["Phase 5A measurement", `${phase5aMeasurement.status || item.measurement_status} · ${format(phase5aMeasurement.circumference_cm, 2)} cm`],
    ["Previous measurement", `${item.historical_measurement?.status || "—"} · ${format(item.historical_measurement?.pom_m, 2)} m · ${format(item.historical_measurement?.circumference_cm ?? item.historical_measurement?.reported_candidate_circumference_cm, 2)} cm`],
    ["Field verification", "NO — LiDAR estimate"],
  ] : phase4cItem ? [
    ["Phase 4 Tree ID", item.phase4_tree_id || item.review_item_id],
    ["Current inventory status", item.current_inventory_status],
    ["Proposed structure class", proposal.structure_class || item.proposed_structure_class],
    ["Possible / proposed parent", proposal.parent_tree_id || phase4c.parent_tree_id_for_display || item.proposed_parent_tree_id || "none"],
    ["Structure confidence", `${format(proposal.confidence ?? item.structure_confidence)} (not calibrated)`],
    ["Vertical stem range", `${format(structureFeature.vertical_persistence?.z_min_agl_m ?? item.vertical_range_agl_m?.[0], 2)}–${format(structureFeature.vertical_persistence?.z_max_agl_m ?? item.vertical_range_agl_m?.[1], 2)} m AGL`],
    ["Attachment height", proposal.attachment_height_agl_m == null ? "not supported" : `${format(proposal.attachment_height_agl_m, 2)} m AGL`],
    ["Source candidates", (proposal.source_candidate_ids || item.source_candidate_ids || []).join(", ") || "none"],
    ["Source tracks", (proposal.source_track_ids || item.source_track_ids || []).join(", ") || "none"],
    ["Baseline inventory eligible", item.baseline_inventory_eligible ? "YES" : "NO"],
    ["Proposed inventory eligible", (proposal.proposed_inventory_eligible ?? item.proposed_inventory_eligible) ? "YES" : "NO — attach as child proposal"],
    ["Effective in shadow mode", proposal.effective_inventory_eligible_in_shadow_mode === false ? "NO" : "YES — Tree ID is preserved"],
    ["Measurement", "not used by Phase 4C classifier"],
  ] : [...phase3Rows,
    ["Geometry", item.candidate_geometry_status], ["Identity", item.identity_status], ["Measurement", item.measurement_status],
    ["Rule / selected POM", `${item.measurement_rule || "—"} / ${format(item.measurement_height_m, 2)} m`],
    ["Sample radius", `${format(metric(sample, "sampled_radius_m", "radius_m"))} m`],
    ["Full radius", `${format(metric(full, "full_radius_m", "radius_m"))} m`],
    ["Sample / full centre p90", `${format(metric(sample, "sampled_centreline_residual_p90_m", "centreline_residual_p90_m"))} / ${format(metric(full, "full_centreline_residual_p90_m", "centreline_residual_p90_m"))} m`],
    ["Radius expansion", `${format(item.comparison_metrics?.radius_delta_m)} m`],
    ["1.30 m plane", "shown in blue"], ["Selected POM", `${format(item.measurement_height_m, 2)} m`],
  ];
  $("metrics").innerHTML = rows.map(([a,b]) => `<div class="metric-row"><span>${escapeHtml(a)}</span><span>${escapeHtml(b)}</span></div>`).join("");
  const graphEdges = item.phase4_graph_edges || [];
  const aliases = item.alias_relationships || item.potential_duplicate_pairs || [];
  const possibleParents = phase4c.possible_parent_relationships || [];
  const phase5aCandidates = phase5aEvidence.attachment_candidates || [];
  $("aliases").innerHTML = phase5aItem ? (phase5aCandidates.length ? phase5aCandidates.map((candidate) => `<div class="alias-item"><strong>${escapeHtml(candidate.attachment_candidate_id)}</strong><br>${format(candidate.height_agl_m, 3)} m AGL · ${escapeHtml(candidate.ownership_status)} · evidence ${format(candidate.evidence_score)} (not calibrated)<br>root tracks: ${escapeHtml((candidate.source_root_track_ids || []).join(", ") || "none")}<br>support: ${escapeHtml(Object.keys(candidate.geometric_support_features || {}).join(" · ") || "none")}<br>contradictions: ${escapeHtml((candidate.contradictory_evidence || []).join(" · ") || "none")}</div>`).join("") : '<p class="muted">No supported attachment candidate; a numeric POM is prohibited.</p>') : phase4cItem ? (possibleParents.length ? possibleParents.map((edge) => `<div class="alias-item"><strong>${escapeHtml(edge.child_tree_id)} → ${escapeHtml(edge.parent_tree_id)}</strong><br>${escapeHtml(edge.proposed_attachment_class || "not supported")} · rank ${format(edge.attachment_rank_score)}<br>overlap ${format(edge.vertical_overlap_m)} m · closest axis ${format(edge.minimum_centerline_distance_m)} m<br>attachment ${format(edge.attachment_height_agl_m, 2)} m AGL · axial continuation safeguard ${edge.axial_continuation_evidence ? "YES" : "NO"}<br>failed checks: ${escapeHtml((edge.failed_parent_checks || []).join(", ") || "none")}</div>`).join("") : '<p class="muted">ไม่พบ plausible parent ภายในช่วงค้นหา จึงห้าม auto-attach</p>') : graphEdges.length ? graphEdges.map((edge) => `<div class="alias-item"><strong>${escapeHtml(edge.track_a)} ↔ ${escapeHtml(edge.track_b)}</strong><br>${escapeHtml(edge.classification)} · score ${format(edge.continuity_score?.score)}<br>gap ${format(edge.features?.vertical_gap_m)} m · seam ${format(edge.features?.seam_centre_distance_m)} m · radius Δ ${format(edge.features?.radius_relative_difference)}<br>target ${escapeHtml([edge.tree_a, edge.tree_b].filter(Boolean).join(", ") || "none")}</div>`).join("") : aliases.length ? aliases.map((pair) => `<div class="alias-item"><strong>${escapeHtml(pair.candidate_a)} ↔ ${escapeHtml(pair.candidate_b)}</strong><br>${escapeHtml(pair.classification)}<br>full center ${format(Number(pair.full_measurement_center_distance_m))} m · containment ${format(Number(pair.accepted_point_containment))}</div>`).join("") : '<p class="muted">ไม่มี graph/alias edge สำหรับรายการนี้</p>';
  const failure = item.failure || {};
  $("failures").innerHTML = placeholder ? `<p class="warning">${escapeHtml(item.review_question)}</p>` : phase5aItem ? `<div class="alias-item"><strong>Attachment QA</strong><br>${escapeHtml((phase5aAttachment.reason_codes || []).join(" · ") || "none")}</div><div class="alias-item"><strong>Measurement QA</strong><br>${escapeHtml((phase5aMeasurement.reason_codes || []).join(" · ") || "none")}</div><p class="warning">Shadow mode: the existing Tree ID and previous measurement provenance are unchanged.</p>` : phase4cItem ? `<div class="alias-item"><strong>Supporting evidence</strong><br>${escapeHtml((proposal.reason_codes || item.reason_codes || []).join(" · ") || "none")}</div><div class="alias-item"><strong>Contradictory / failed evidence</strong><br>${escapeHtml((proposal.contradictory_evidence || item.contradictory_evidence || []).join(" · ") || "none")}</div><p class="warning">Shadow mode: ยังไม่มี Tree ID ใดถูกลบจาก inventory จริง</p>` : `<div class="alias-item"><strong>${escapeHtml(failure.primary_reason || "ไม่มี full-resolution failure")}</strong><br>${escapeHtml(failure.primary_stage || "—")}</div><p class="muted">${escapeHtml((failure.all_reason_codes || item.reason_codes || []).join(" · "))}</p>`;
  const counts = state.crop?.counts_before_display_sampling;
  if (roiItem && counts) {
    const range=roiHeightRange(), shown=range?(state.crop?.sampled_points_xyz||[]).filter((point)=>point[2]-(item.ground_z_m??0)>=range[0]&&point[2]-(item.ground_z_m??0)<=range[1]).length:counts.sampled;
    $("cropStatus").textContent = `${roiHeightLabel()} · แสดง ${shown.toLocaleString()} จาก ${counts.sampled.toLocaleString()} จุด`;
  } else {
    $("cropStatus").textContent = placeholder ? "กรอก XY หรือเลือก candidate ใกล้เคียงแล้วคลิกในโหมดปัก seed" : state.crop?.error ? `ไม่มี crop: ${state.crop.error}` : counts ? `sampled ${counts.sampled.toLocaleString()} · full accepted ${counts.full_accepted.toLocaleString()} · full rejected ${counts.full_rejected.toLocaleString()}` : "กำลังโหลด crop…";
  }
  renderComponents();
}

function renderComponents() {
  const sample = state.evidence?.sampled?.components_by_height || [];
  const full = state.evidence?.full_resolution?.components_by_height || [];
  const byHeight = new Map();
  sample.forEach((x) => byHeight.set(Number(x.height_m).toFixed(2), { sample: x }));
  full.forEach((x) => { const key = Number(x.height_m).toFixed(2); byHeight.set(key, { ...(byHeight.get(key) || {}), full: x }); });
  if (!byHeight.size) { $("componentRows").innerHTML = '<p class="muted">ไม่มี component-centre evidence สำหรับรายการนี้</p>'; return; }
  $("componentRows").innerHTML = '<div class="component-row"><strong>height</strong><strong>sampled</strong><strong>full</strong></div>' + [...byHeight.entries()].sort((a,b) => Number(a[0])-Number(b[0])).map(([height, value]) => {
    const s = value.sample, f = value.full;
    const sampleText = s ? `${(s.fits || []).length} centres${s.selected_fit ? ` · selected r=${format(s.selected_fit.radius_m)}` : ""}` : "—";
    const fullText = f ? `${(f.fits || []).length} centres${f.selected_fit ? ` · selected r=${format(f.selected_fit.radius_m)}` : ""}` : "—";
    return `<div class="component-row"><span>${height} m</span><span>${sampleText}</span><span>${fullText}</span></div>`;
  }).join("");
}

function renderLabelActions() {
  $("labelActions").innerHTML = "";
  allowedLabels().forEach((label) => { const button = document.createElement("button"); button.textContent = label; button.dataset.label = label; button.onclick = () => { state.selectedLabel = label; document.querySelectorAll("#labelActions button").forEach((b) => b.classList.toggle("active", b.dataset.label === label)); }; $("labelActions").append(button); });
  const fragmentQueue = ["phase4_fragments", "phase4b_high_priority"].includes(state.queueId);
  const phase4cQueue = state.queueId === "phase4c_structure_shadow";
  $("duplicateTargetLabel").textContent = phase4cQueue ? "Proposed parent Tree ID" : fragmentQueue ? "Existing Tree / track target" : "Duplicate target";
  $("duplicateTarget").placeholder = (phase4cQueue || fragmentQueue) ? "TREE_0001 or T15-0001" : "C-0000";
}
function loadAnnotation() {
  if (!state.current) return;
  const annotation = state.annotations[itemKey(state.current)] || {};
  if (isRoiItem(state.current)) {
    renderRoiCensus();
    return;
  }
  if (isPhase5aItem(state.current)) {
    const automatic = state.evidence?.phase5a || {};
    const attachment = automatic.highest_prop_root_attachment || {};
    const candidates = automatic.attachment_candidates || [];
    $("phase5aApplicability").value = annotation.protocol_applicability || automatic.measurement_protocol?.applicability || state.current.protocol_applicability || "NOT_REVIEWED";
    $("phase5aAttachmentStatus").value = annotation.attachment_status || attachment.status || "NEEDS_REVIEW";
    $("phase5aCandidateId").value = annotation.selected_automatic_candidate_id || attachment.attachment_candidate_id || candidates.at(-1)?.attachment_candidate_id || "";
    $("phase5aAcceptedRootTracks").value = (annotation.accepted_root_track_ids || attachment.source_root_track_ids || []).join(", ");
    $("phase5aRejectedRootTracks").value = (annotation.rejected_other_tree_root_track_ids || []).join(", ");
    const point = annotation.manual_attachment_point_xyz || null;
    $("phase5aAttachmentX").value = point?.x ?? ""; $("phase5aAttachmentY").value = point?.y ?? ""; $("phase5aAttachmentZ").value = point?.z ?? "";
    $("phase5aPomDecision").value = annotation.pom_decision || "NEEDS_REVIEW";
    $("phase5aMeasurementDecision").value = annotation.measurement_decision || "UNCERTAIN";
    $("phase5aReason").value = annotation.reason || ""; $("phase5aNote").value = annotation.reviewer_note || "";
    $("phase5aSaveStatus").textContent = annotation.timestamp ? `Saved ${annotation.timestamp}` : "Not reviewed";
    normalizePhase5aApplicabilityControls();
    updatePhase5aPomPreview();
    return;
  }
  state.selectedLabel = annotation.human_label || null;
  document.querySelectorAll("#labelActions button").forEach((b) => b.classList.toggle("active", b.dataset.label === state.selectedLabel));
  $("duplicateTarget").value = annotation.duplicate_target || state.current.proposed_parent_tree_id || "";
  $("correctedX").value = annotation.corrected_center?.[0] ?? ""; $("correctedY").value = annotation.corrected_center?.[1] ?? "";
  $("correctedPom").value = annotation.corrected_measurement_height_m ?? ""; $("reviewerNote").value = annotation.reviewer_note || "";
  $("saveStatus").textContent = annotation.timestamp ? `บันทึกล่าสุด ${annotation.timestamp}` : "ยังไม่ตรวจ";
}
function numberOrNull(id) { const value = $(id).value.trim(); return value === "" ? null : Number(value); }
function updatePhase5aPomPreview() {
  const z=numberOrNull("phase5aAttachmentZ"),ground=state.current?.ground_z_m;
  const agl=z==null||ground==null?null:z-ground;
  $("phase5aAttachmentAgl").value=agl==null?"":agl.toFixed(3);
  $("phase5aPomAgl").value=agl==null?"":(agl+0.30).toFixed(3);
}
function togglePhase5aPickMode() {
  if(!isPhase5aItem())return;
  state.phase5aPickMode=!state.phase5aPickMode;state.manualMode=false;
  $("pointCanvas").classList.toggle("phase5a-pick",state.phase5aPickMode);
  $("phase5aPickAttachment").textContent=state.phase5aPickMode?"Cancel attachment click":"Click manual attachment point";
}
function normalizePhase5aApplicabilityControls() {
  if (!isPhase5aItem()) return;
  const nonPropRoot = $("phase5aApplicability").value === "STANDARD_NON_PROP_ROOT_PROTOCOL";
  if (nonPropRoot) {
    $("phase5aAttachmentStatus").value = "NOT_VISIBLE";
    $("phase5aCandidateId").value = "";
    $("phase5aAcceptedRootTracks").value = "";
    $("phase5aRejectedRootTracks").value = "";
    $("phase5aAttachmentX").value = "";
    $("phase5aAttachmentY").value = "";
    $("phase5aAttachmentZ").value = "";
    $("phase5aPomDecision").value = "REJECT_PROTOCOL_POM";
    $("phase5aMeasurementDecision").value = "REJECT";
    state.phase5aPickMode = false;
    $("pointCanvas").classList.remove("phase5a-pick");
    $("phase5aPickAttachment").textContent = "Click manual attachment point";
  }
  [
    "phase5aAttachmentStatus", "phase5aCandidateId", "phase5aAcceptedRootTracks",
    "phase5aRejectedRootTracks", "phase5aAttachmentX", "phase5aAttachmentY",
    "phase5aAttachmentZ", "phase5aPomDecision", "phase5aMeasurementDecision",
    "phase5aPickAttachment", "phase5aUseCandidate",
  ].forEach((id) => { $(id).disabled = nonPropRoot; });
  updatePhase5aPomPreview();
}
function usePhase5aCandidate() {
  if(!isPhase5aItem())return;
  const candidates=state.evidence?.phase5a?.attachment_candidates||[],id=$("phase5aCandidateId").value.trim();
  const candidate=candidates.find((row)=>row.attachment_candidate_id===id);
  if(!candidate)return alert("Select an available automatic attachment candidate first");
  $("phase5aAttachmentX").value=Number(candidate.position_xyz.x).toFixed(3);$("phase5aAttachmentY").value=Number(candidate.position_xyz.y).toFixed(3);$("phase5aAttachmentZ").value=Number(candidate.position_xyz.z).toFixed(3);
  $("phase5aAcceptedRootTracks").value=(candidate.source_root_track_ids||[]).join(", ");$("phase5aAttachmentStatus").value="PROBABLE";updatePhase5aPomPreview();
}
function savePhase5aAnnotation() {
  if(!isPhase5aItem())return;
  const status=$("phase5aAttachmentStatus").value,x=numberOrNull("phase5aAttachmentX"),y=numberOrNull("phase5aAttachmentY"),z=numberOrNull("phase5aAttachmentZ");
  if(["CONFIRMED","PROBABLE"].includes(status)&&(x==null||y==null||z==null))return alert("Confirmed/probable attachment requires a reviewed XYZ point");
  if($("phase5aPomDecision").value==="CONFIRM_PROTOCOL_POM"&&!(["CONFIRMED","PROBABLE"].includes(status)))return alert("Resolve the highest attachment before confirming the +0.30 m POM");
  const automatic=state.evidence?.phase5a||{};
  state.annotations[itemKey(state.current)]={
    tree_id:state.current.review_item_id,review_item_id:state.current.review_item_id,algorithm_version:currentVersion(),
    protocol_applicability:$("phase5aApplicability").value,attachment_status:status,
    selected_automatic_candidate_id:$("phase5aCandidateId").value.trim()||null,
    manual_attachment_point_xyz:x!=null&&y!=null&&z!=null?{x,y,z}:null,
    manual_attachment_height_agl_m:z==null?null:Number((z-state.current.ground_z_m).toFixed(3)),
    accepted_root_track_ids:commaIds($("phase5aAcceptedRootTracks").value),
    rejected_other_tree_root_track_ids:commaIds($("phase5aRejectedRootTracks").value),
    pom_decision:$("phase5aPomDecision").value,confirmed_protocol_offset_m:0.30,
    reviewed_protocol_pom_height_agl_m:z==null?null:Number((z-state.current.ground_z_m+0.30).toFixed(3)),
    measurement_decision:$("phase5aMeasurementDecision").value,field_verified:false,
    automatic_suggestion:clone(automatic.highest_prop_root_attachment||{}),automatic_measurement:clone(automatic.measurement||{}),
    reason:$("phase5aReason").value,reviewer_note:$("phase5aNote").value,timestamp:new Date().toISOString(),
  };
  state.phase5aPickMode=false;$("pointCanvas").classList.remove("phase5a-pick");saveLocal();loadAnnotation();applyFilters();
}
function saveAnnotation() {
  if (!state.current || state.current.item_type === "MANUAL_SEED_PLACEHOLDER") return alert("รายการนี้ใช้แบบฟอร์ม Manual missed-tree seed ทางซ้าย");
  if (!state.selectedLabel) return alert("กรุณาเลือก human label");
  const duplicate = $("duplicateTarget").value.trim() || null;
  if (["DUPLICATE_OF", "PART_OF_EXISTING_TREE"].includes(state.selectedLabel) && !duplicate) return alert("กรุณาระบุ target ID");
  const x = numberOrNull("correctedX"), y = numberOrNull("correctedY");
  state.annotations[itemKey(state.current)] = {
    review_item_id: state.current.review_item_id || null,
    candidate_id: state.current.candidate_id, algorithm_version: currentVersion(),
    automatic_status: { candidate_geometry_status: state.current.candidate_geometry_status, identity_status: state.current.identity_status, measurement_status: state.current.measurement_status },
    human_label: state.selectedLabel, duplicate_target: duplicate,
    corrected_center: x != null && y != null ? [x,y] : null,
    corrected_measurement_height_m: numberOrNull("correctedPom"),
    timestamp: new Date().toISOString(), reviewer_note: $("reviewerNote").value,
  };
  saveLocal(); loadAnnotation(); applyFilters();
}

function roiBoundary(center) {
  const frozen = state.roiCensus?.frozen_roi, bounds = frozen?.bounds, tolerance = frozen?.boundary_rule?.tolerance_m ?? .02;
  if (!bounds) return "UNKNOWN";
  const x = Number(center.x), y = Number(center.y);
  if (x < bounds.x_min-tolerance || x > bounds.x_max+tolerance || y < bounds.y_min-tolerance || y > bounds.y_max+tolerance) return "OUTSIDE_ROI";
  const crossing = Math.abs(x-bounds.x_min)<=tolerance || Math.abs(x-bounds.x_max)<=tolerance || Math.abs(y-bounds.y_min)<=tolerance || Math.abs(y-bounds.y_max)<=tolerance;
  return crossing ? "CROSSING_ROI_BOUNDARY" : "INSIDE_ROI";
}
function nearestRoiSuggestions(reference) {
  const roi = state.evidence?.roi || {}, result = [];
  const add = (kind, id, x, y) => result.push({ kind, id, distance_m: Math.hypot(Number(x)-reference.center.x, Number(y)-reference.center.y) });
  (roi.resolved_tree_predictions || []).forEach((x) => add("TREE", x.tree_id, x.x, x.y));
  (roi.track_centers || []).forEach((x) => add("TRACK", x.track_id, x.x, x.y));
  (roi.candidate_centers || []).forEach((x) => add("CANDIDATE", x.candidate_id, x.x, x.y));
  const grouped = [];
  for (const kind of ["TREE","TRACK","CANDIDATE"]) grouped.push(...result.filter((x)=>x.kind===kind).sort((a,b)=>a.distance_m-b.distance_m||a.id.localeCompare(b.id)).slice(0,3));
  return grouped;
}
function addSuggestedId(kind, id) {
  const field = kind === "TREE" ? "matchedTreeIds" : kind === "TRACK" ? "matchedTrackIds" : "matchedCandidateIds";
  $(field).value = commaIds([$(field).value, id].filter(Boolean).join(",")).join(", ");
  if(kind==="TREE"){$("roiEvidenceStatus").value=commaIds($(field).value).length>1?"DUPLICATE_TREE_IDS":"RESOLVED_TREE";state.selectedPredictionId=id;renderPredictionReview();draw();}
  else if(kind==="TRACK")$("roiEvidenceStatus").value="TRACK_EXISTS_UNRESOLVED";
  else $("roiEvidenceStatus").value="CANDIDATE_EXISTS_NO_TRACK";
  $("roiEvidenceStatus").dataset.explicit="true";
}
function renderRoiSuggestions(reference) {
  const container = $("roiMatchSuggestions");
  if (!reference) { container.textContent = "เพิ่มหรือเลือก reference tree เพื่อดูคำแนะนำใกล้เคียง"; return; }
  const suggestions = nearestRoiSuggestions(reference);
  container.innerHTML = '<strong>รายการใกล้เคียง — ต้องกดยืนยันเอง</strong>' + suggestions.map((item) => `<div class="suggestion-row"><span>${escapeHtml(item.kind)} ${escapeHtml(item.id)} · ${item.distance_m.toFixed(3)} m</span><button type="button" data-kind="${item.kind}" data-id="${item.id}">จับคู่กับ ${escapeHtml(item.id)}</button></div>`).join("");
  container.querySelectorAll("button").forEach((button) => button.onclick = () => addSuggestedId(button.dataset.kind, button.dataset.id));
}
function renderRoiCensus() {
  if (!state.roiCensus) return;
  const references = state.roiCensus.reference_trees || [];
  if (!state.selectedReferenceId || !references.some((x)=>x.reference_tree_id===state.selectedReferenceId)) state.selectedReferenceId = references[0]?.reference_tree_id || null;
  const select = $("roiReferenceSelect"); select.innerHTML = '<option value="">— ยังไม่มี reference —</option>';
  references.forEach((item) => option(select, item.reference_tree_id, `${item.reference_tree_id} · ${item.updated_at ? "จับคู่แล้ว" : "ยังไม่จับคู่"}`));
  select.value = state.selectedReferenceId || "";
  const reference = references.find((x)=>x.reference_tree_id===state.selectedReferenceId) || null;
  const completedReferences=references.filter((item)=>item.updated_at).length,referenceIndex=reference?references.indexOf(reference)+1:0;
  $("roiReferenceProgress").textContent = references.length ? `กำลังตรวจต้นที่ ${referenceIndex}/${references.length} · จับคู่แล้ว ${completedReferences}/${references.length}` : "ยังไม่มี reference tree";
  $("roiReferenceCount").textContent = `${references.length} ต้นที่ปักแล้ว`;
  $("roiPickStatus").textContent = state.roiPickFeedback || "คลิกให้โดนจุดของลำต้น ระบบจะ snap ไปยังจุด LiDAR ใกล้ที่สุด";
  $("addRoiReference").textContent = state.roiEditMode === "ADD" ? "หยุดเพิ่มต้นไม้" : "+ เพิ่มต้นไม้ต่อเนื่อง";
  document.querySelectorAll("[data-roi-band]").forEach((button) => button.classList.toggle("active", button.dataset.roiBand === state.roiHeightBand));
  $("toggleRoiAlgorithm").classList.toggle("active", state.roiWorkflowStep === "ASSOCIATE");
  $("toggleRoiAlgorithm").textContent = state.roiWorkflowStep === "ASSOCIATE" ? "← กลับไปนับต้นไม้" : "จบการนับ → เริ่มจับคู่";
  $("roiReferenceX").value = reference?.center?.x ?? ""; $("roiReferenceY").value = reference?.center?.y ?? "";
  $("roiReferenceConfidence").value = reference?.confidence || "CERTAIN";
  $("roiReferenceVisibility").value = reference?.visibility || "CLEAR";
  $("roiEvidenceStatus").value = reference?.algorithm_evidence_status || "NO_ALGORITHM_EVIDENCE";
  $("roiEvidenceStatus").dataset.explicit = reference?.updated_at ? "true" : "false";
  $("matchedTreeIds").value = (reference?.matched_tree_ids || []).join(", ");
  $("matchedCandidateIds").value = (reference?.matched_candidate_ids || []).join(", ");
  $("matchedTrackIds").value = (reference?.matched_track_ids || []).join(", ");
  $("roiFailureReason").value = reference?.primary_failure_reason || "";
  $("roiReferenceNote").value = reference?.review_notes || "";
  $("roiBoundaryStatus").textContent = reference ? `Boundary: ${roiBoundary(reference.center)} · membership ใช้ reference center` : "";
  $("roiCensusComplete").checked = state.roiCensus.annotation_status === "COMPLETE";
  $("roiReviewerNote").value = state.roiCensus.reviewer?.notes || "";
  $("roiSaveStatus").textContent = state.roiCensus.completed_at ? `COMPLETE · ${state.roiCensus.completed_at}` : state.roiCensus.annotation_status || "NOT_STARTED";
  renderRoiSuggestions(reference); renderPredictionReview(); renderDetails();
}
function renderPredictionReview() {
  if (!isRoiItem() || !state.roiCensus) return;
  const treeIds = state.current.resolved_tree_ids || [];
  const reviews = state.roiCensus.prediction_reviews || [];
  const reviewed = new Map(reviews.map((x)=>[x.tree_id,x]));
  if (!state.selectedPredictionId || !treeIds.includes(state.selectedPredictionId)) state.selectedPredictionId = treeIds[0] || null;
  const select = $("roiPredictionSelect"); select.innerHTML = "";
  treeIds.forEach((treeId) => option(select, treeId, `${treeId} · ${reviewed.has(treeId) ? reviewed.get(treeId).classification : "ยังไม่ตรวจ"}`));
  select.value = state.selectedPredictionId || "";
  const review = reviewed.get(state.selectedPredictionId) || null;
  const manuallyLinkedReferences=(state.roiCensus.reference_trees||[]).filter((item)=>(item.matched_tree_ids||[]).includes(state.selectedPredictionId)).map((item)=>item.reference_tree_id);
  $("predictionClassification").value = review?.classification || "";
  $("predictionReferenceIds").value = (review?.matched_reference_tree_ids || manuallyLinkedReferences).join(", ");
  $("predictionReviewerNote").value = review?.review_notes || "";
  const tree = state.phase4Trees[state.selectedPredictionId] || {};
  $("roiPredictionProgress").textContent = `${reviews.length} / ${treeIds.length} reviewed · selected sources: ${(tree.source_candidates || []).join(", ") || "—"} · measurement ${tree.measurement?.status || "—"}`;
}
function markPredictionUnsaved() {
  if (!isRoiItem()) return;
  const base = $("roiPredictionProgress").textContent.replace(/ · ยังไม่ได้บันทึก$/, "");
  $("roiPredictionProgress").textContent = `${base} · ยังไม่ได้บันทึก`;
}
function setRoiHeightBand(band) {
  if (!isRoiItem() || !["LOW", "STEM", "ALL"].includes(band)) return;
  state.roiHeightBand = band; renderRoiCensus(); draw();
}
function toggleRoiAlgorithmOverlay() {
  if (!isRoiItem()) return;
  if(state.roiWorkflowStep==="CENSUS"){
    if(!(state.roiCensus?.reference_trees||[]).length)return alert("ยังไม่ได้ปักต้นไม้ กรุณากด + เพิ่มต้นไม้ต่อเนื่อง ก่อนเริ่มจับคู่");
    state.roiWorkflowStep="ASSOCIATE";state.roiAlgorithmOverlay=true;state.roiEditMode=null;
    state.selectedReferenceId=(state.roiCensus.reference_trees||[]).find((item)=>!item.updated_at)?.reference_tree_id||state.roiCensus.reference_trees[0]?.reference_tree_id||null;
  }else{
    state.roiWorkflowStep="CENSUS";state.roiAlgorithmOverlay=false;
  }
  $("pointCanvas").classList.remove("roi-edit");renderRoiCensus();draw();
}
function setRoiEditMode(mode) {
  if (!isRoiItem()) return alert("กรุณาเลือก Phase 4B ROI census ก่อน");
  if (mode === "MOVE" && !state.selectedReferenceId) return alert("กรุณาเลือก reference tree ที่จะย้าย");
  state.roiEditMode = state.roiEditMode === mode ? null : mode; state.manualMode = false; state.roiPickFeedback = "";
  if(mode==="ADD"&&state.roiEditMode){state.roiWorkflowStep="CENSUS";state.roiAlgorithmOverlay=false;}
  $("pointCanvas").classList.toggle("roi-edit", Boolean(state.roiEditMode));
  $("addRoiReference").classList.toggle("active", state.roiEditMode === "ADD");
  $("moveRoiReference").classList.toggle("active", state.roiEditMode === "MOVE"); renderRoiCensus(); draw();
}
function nextRoiReferenceId() {
  let sequence = Number(state.roiCensus.next_reference_sequence || 1), id;
  const used = new Set((state.roiCensus.reference_trees || []).map((x)=>x.reference_tree_id));
  const roiId = String(state.roiCensus?.frozen_roi?.roi_id || "ROI-A").toUpperCase().replaceAll("-", "_");
  do { id = `${roiId}_REF_${String(sequence).padStart(4,"0")}`; sequence += 1; } while (used.has(id));
  state.roiCensus.next_reference_sequence = sequence; return id;
}
function addRoiReferenceAt(x, y, z = null, clickEvidence = null) {
  const reference = {
    reference_tree_id: nextRoiReferenceId(), center: { x, y, z }, boundary_relation: "INSIDE_ROI",
    confidence: "CERTAIN", visibility: "CLEAR", matched_tree_ids: [], matched_candidate_ids: [], matched_track_ids: [],
    algorithm_evidence_status: "NO_ALGORITHM_EVIDENCE", automatic_match_suggestions: [], primary_failure_reason: null, review_notes: "",
    click_evidence: clickEvidence,
  };
  reference.boundary_relation = roiBoundary(reference.center); state.roiCensus.reference_trees.push(reference);
  state.roiCensus.annotation_status = "IN_PROGRESS"; state.selectedReferenceId = reference.reference_tree_id; saveLocal(); renderRoiCensus(); draw();
}
function saveRoiReference() {
  const reference = state.roiCensus?.reference_trees?.find((x)=>x.reference_tree_id===state.selectedReferenceId);
  if (!reference) return alert("กรุณาเพิ่มหรือเลือก reference tree");
  const x=numberOrNull("roiReferenceX"), y=numberOrNull("roiReferenceY"); if(x==null||y==null)return alert("กรุณากรอก X/Y");
  const treeIds=commaIds($("matchedTreeIds").value), candidateIds=commaIds($("matchedCandidateIds").value), trackIds=commaIds($("matchedTrackIds").value);
  const status=$("roiEvidenceStatus").value;
  if($("roiEvidenceStatus").dataset.explicit!=="true")return alert("กรุณากดจับคู่กับ ID ที่ตรง หรือเลือก ‘ไม่พบหลักฐานระบบ’ / ‘ดูแล้วไม่แน่ใจ’ ก่อนบันทึก");
  if(status==="NO_ALGORITHM_EVIDENCE"&&(treeIds.length||candidateIds.length||trackIds.length))return alert("NO_ALGORITHM_EVIDENCE ต้องไม่มี matched ID");
  if(status==="RESOLVED_TREE"&&!treeIds.length)return alert("RESOLVED_TREE ต้องระบุ Matched Tree ID");
  if(status==="TRACK_EXISTS_UNRESOLVED"&&!trackIds.length)return alert("TRACK_EXISTS_UNRESOLVED ต้องระบุ Matched track ID");
  if(status==="CANDIDATE_EXISTS_NO_TRACK"&&!candidateIds.length)return alert("CANDIDATE_EXISTS_NO_TRACK ต้องระบุ Matched candidate ID");
  Object.assign(reference, {
    center:{x,y,z:reference.center?.z??null}, boundary_relation:roiBoundary({x,y}), confidence:$("roiReferenceConfidence").value,
    visibility:$("roiReferenceVisibility").value, algorithm_evidence_status:status, matched_tree_ids:treeIds,
    matched_candidate_ids:candidateIds, matched_track_ids:trackIds, primary_failure_reason:$("roiFailureReason").value||null,
    automatic_match_suggestions:nearestRoiSuggestions({center:{x,y}}).map((item)=>({...item,accepted_as_match:false})),
    review_notes:$("roiReferenceNote").value, updated_at:new Date().toISOString(),
  });
  const references=state.roiCensus.reference_trees||[],currentIndex=references.indexOf(reference);
  const ordered=[...references.slice(currentIndex+1),...references.slice(0,currentIndex)];
  state.selectedReferenceId=ordered.find((item)=>!item.updated_at)?.reference_tree_id||reference.reference_tree_id;
  state.roiCensus.annotation_status="IN_PROGRESS"; saveLocal(); renderRoiCensus(); draw();
}
function markRoiReferenceNoEvidence() {
  $("roiEvidenceStatus").value="NO_ALGORITHM_EVIDENCE";$("roiEvidenceStatus").dataset.explicit="true";
  $("matchedTreeIds").value="";$("matchedCandidateIds").value="";$("matchedTrackIds").value="";$("roiFailureReason").value="NO_CANDIDATE_GENERATED";
}
function markRoiReferenceUncertain() {
  $("roiReferenceConfidence").value="UNCERTAIN";$("roiEvidenceStatus").value="UNCERTAIN";
  $("roiEvidenceStatus").dataset.explicit="true";
  $("matchedTreeIds").value="";$("matchedCandidateIds").value="";$("matchedTrackIds").value="";$("roiFailureReason").value="";
  $("roiReferenceNote").value=$("roiReferenceNote").value||"มองจาก LiDAR แล้วไม่สามารถจับคู่หลักฐานของระบบได้อย่างมั่นใจ";
}
function deleteRoiReference() {
  if (!state.selectedReferenceId) return alert("กรุณาเลือก reference tree");
  if (!confirm(`ลบ ${state.selectedReferenceId}?`)) return;
  state.roiCensus.reference_trees = state.roiCensus.reference_trees.filter((x)=>x.reference_tree_id!==state.selectedReferenceId);
  state.roiCensus.prediction_reviews.forEach((x)=>{x.matched_reference_tree_ids=(x.matched_reference_tree_ids||[]).filter((id)=>id!==state.selectedReferenceId);});
  state.selectedReferenceId=state.roiCensus.reference_trees[0]?.reference_tree_id||null; state.roiCensus.annotation_status="IN_PROGRESS"; saveLocal(); renderRoiCensus(); draw();
}
function resetRoiDraft() {
  if(!state.payload?.annotation_template)return alert("ไม่มี annotation template");
  if(!confirm("รีเซ็ต local ROI draft กลับเป็นไฟล์ตั้งต้น? ข้อมูลที่ยังไม่ export จะหาย"))return;
  state.roiCensus=clone(state.payload.annotation_template);state.selectedReferenceId=null;state.selectedPredictionId=state.current?.resolved_tree_ids?.[0]||null;state.roiEditMode=null;state.roiAlgorithmOverlay=false;state.roiPickFeedback="";state.roiWorkflowStep="CENSUS";$("pointCanvas").classList.remove("roi-edit");saveLocal();renderRoiCensus();draw();
}
function saveRoiPrediction() {
  const treeId=state.selectedPredictionId, classification=$("predictionClassification").value; if(!treeId||!classification)return alert("กรุณาเลือก Tree ID และ classification");
  const matchedReferences=commaIds($("predictionReferenceIds").value);
  if(["CORRECT_UNIQUE_TREE","DUPLICATE_OF_REFERENCE_TREE"].includes(classification)&&!matchedReferences.length)return alert("classification นี้ต้องระบุ Matched reference ID อย่างน้อยหนึ่งรายการ");
  const item={tree_id:treeId,classification,matched_reference_tree_ids:matchedReferences,review_notes:$("predictionReviewerNote").value,updated_at:new Date().toISOString(),manual_association_overrides_xy_suggestion:true};
  const index=state.roiCensus.prediction_reviews.findIndex((x)=>x.tree_id===treeId); if(index>=0)state.roiCensus.prediction_reviews[index]=item;else state.roiCensus.prediction_reviews.push(item);
  state.roiCensus.prediction_reviews.sort((a,b)=>a.tree_id.localeCompare(b.tree_id)); state.roiCensus.annotation_status="IN_PROGRESS";
  const reviewed=new Set(state.roiCensus.prediction_reviews.map((entry)=>entry.tree_id));
  state.selectedPredictionId=(state.current.resolved_tree_ids||[]).find((id)=>!reviewed.has(id))||treeId;
  saveLocal(); renderRoiCensus(); draw();
}
function saveRoiAnnotation() {
  if (!isRoiItem() || !state.roiCensus) return;
  const complete=$("roiCensusComplete").checked, expected=state.current.resolved_tree_ids||[], reviewed=new Set(state.roiCensus.prediction_reviews.map((x)=>x.tree_id));
  if(complete&&state.roiCensus.reference_trees.length===0)return alert("ยังไม่มี reference tree กรุณาทำ census ก่อน");
  if(complete&&state.roiCensus.reference_trees.some((item)=>!item.updated_at))return alert("ยังมี reference tree ที่ไม่ได้บันทึกผลการจับคู่ในขั้น B");
  if(complete&&expected.some((id)=>!reviewed.has(id)))return alert(`ต้องตรวจ Tree ID ทั้ง ${expected.length} รายการก่อนปิด census`);
  state.roiCensus.reviewer={name:null,notes:$("roiReviewerNote").value}; state.roiCensus.annotation_status=complete?"COMPLETE":"IN_PROGRESS";
  state.roiCensus.completed_at=complete?new Date().toISOString():null; saveLocal(); renderRoiCensus();
}
function annotationPayload() {
  if (isRoiItem() && state.roiCensus) return { ...clone(state.roiCensus), export_path: state.payload?.annotation_export_path || null };
  if (isPhase5aItem()) return {
    algorithm_version: currentVersion(), annotation_version: "phase5a-prop-root-pom-review-v1",
    interpretation: "HUMAN REVIEW OF PROP-ROOT APPLICABILITY, AUTOMATIC AND MANUAL ATTACHMENTS, +0.30 M POM, AND MEASUREMENT QA",
    export_path: state.payload?.annotation_export_path || null, annotations: Object.values(state.annotations),
  };
  return {
    algorithm_version: currentVersion(),
    interpretation: "HUMAN REVIEW ANNOTATIONS; CLEAN-HEIGHT HINTS ARE NOT AUTOMATIC FINAL POM VALUES",
    export_path: state.payload?.annotation_export_path || null,
    annotations: Object.values(state.annotations), manual_seeds: state.manualSeeds,
  };
}
function exportAnnotations() {
  const name = (state.payload?.annotation_export_path || `annotations-${state.queueId}.json`).split("/").pop();
  download(name, JSON.stringify(annotationPayload(), null, 2));
}
async function importAnnotations(event) {
  const file = event.target.files[0]; if (!file) return;
  const payload = JSON.parse(await file.text());
  if (payload.annotation_basis === "LIDAR_VISIBLE_REFERENCE" && Array.isArray(payload.reference_trees)) {
    const expected=state.payload?.annotation_template?.frozen_roi; if(expected&&(payload.frozen_roi?.roi_id!==expected.roi_id||JSON.stringify(payload.frozen_roi?.bounds)!==JSON.stringify(expected.bounds)))return alert("ไฟล์ annotation ใช้ ROI คนละกรอบ");
    state.roiCensus=payload; saveLocal(); renderRoiCensus(); draw(); event.target.value=""; return;
  }
  if (!Array.isArray(payload.annotations)) return alert("รูปแบบ annotation ไม่ถูกต้อง");
  payload.annotations.forEach((x) => {
    const key=x.candidate_id||x.review_item_id||x.tree_id;
    if(!key)return;
    if(payload.annotation_version==="phase5a-prop-root-pom-review-v1"||x.tree_id)state.annotations[key]=x;
    else if(allowedLabels().includes(x.human_label)||x.human_label==="GROUND_TRUTH_ROI")state.annotations[key]=x;
  });
  if (Array.isArray(payload.manual_seeds)) state.manualSeeds = payload.manual_seeds;
  saveLocal(); applyFilters(); event.target.value = "";
}
function exportProgress() {
  const header = ["review_item_id","candidate_id","priority","geometry_status","measurement_status","human_label","timestamp","reviewer_note"];
  const quote = (x) => `"${String(x ?? "").replaceAll('"','""')}"`;
  const lines = [header.join(","), ...state.queue.map((item) => { const annotation = state.annotations[itemKey(item)] || {}; return [itemKey(item),item.candidate_id,item.priority,item.candidate_geometry_status,item.measurement_status,annotation.human_label,annotation.timestamp,annotation.reviewer_note].map(quote).join(","); })];
  download(`review-progress-${state.queueId}-${Date.now()}.csv`, lines.join("\n"), "text/csv");
}
function nextUnresolved() { const item = state.filtered.find((x) => x.item_type !== "MANUAL_SEED_PLACEHOLDER" && !state.annotations[itemKey(x)]); if (item) selectCandidate(itemKey(item)); else alert("candidate ในตัวกรองนี้ตรวจครบแล้ว"); }

function toggleManualMode() {
  state.phase5aPickMode = false; $("pointCanvas").classList.remove("phase5a-pick");
  state.roiEditMode = null; $("pointCanvas").classList.remove("roi-edit");
  state.manualMode = !state.manualMode; $("pointCanvas").classList.toggle("manual", state.manualMode);
  $("manualMode").textContent = state.manualMode ? "ยกเลิกโหมดปัก seed" : "เปิดโหมดปัก seed";
  if (state.manualMode) { state.view.pitch = Math.PI / 2; draw(); }
}
function addManualSeed() {
  const x = numberOrNull("manualX"), y = numberOrNull("manualY"), hint = numberOrNull("manualHeight");
  if (x == null || y == null) return alert("กรุณาคลิกจุดหรือกรอก approximate X/Y");
  state.manualSeeds.push({
    seed_id: `MANUAL-P175-${String(state.manualSeeds.length + 1).padStart(4,"0")}`,
    source: "MANUAL_REVIEW_CLICK", approximate_xy: [x, y], x, y,
    clean_height_hint_m: hint, human_label: "TRUE_MAIN_STEM",
    reviewer_note: $("manualNote").value, reference_candidate_id: state.current?.candidate_id || null,
    hint_is_automatic_final_pom: false, timestamp: new Date().toISOString(),
  });
  saveLocal(); $("manualNote").value = "";
}
function exportManualSeeds() { download(`manual-seeds-${state.queueId}-${Date.now()}.json`, JSON.stringify({ algorithm_version: currentVersion(), manual_seeds: state.manualSeeds }, null, 2)); }

function resize() {
  for (const id of ["pointCanvas", "profileCanvas"]) { const canvas = $(id), dpr = devicePixelRatio || 1, rect = canvas.getBoundingClientRect(); canvas.width = Math.max(1, Math.floor(rect.width*dpr)); canvas.height = Math.max(1, Math.floor(rect.height*dpr)); canvas.getContext("2d").setTransform(dpr,0,0,dpr,0,0); }
  draw(); drawProfile();
}
function center3() { const item = state.current; return item?.position ? [item.position.x, item.position.y, (item.ground_z_m ?? 0) + 2] : [0,0,0]; }
function project(point, canvas) {
  const center = center3(), x = point[0]-center[0], y = point[1]-center[1], z = point[2]-center[2];
  const cy=Math.cos(state.view.yaw), sy=Math.sin(state.view.yaw), cp=Math.cos(state.view.pitch), sp=Math.sin(state.view.pitch);
  const xr=cy*x-sy*y, yr=sy*x+cy*y, vertical=yr*sp+z*cp, depth=yr*cp-z*sp;
  return [canvas.clientWidth/2+xr*state.view.zoom, canvas.clientHeight/2-vertical*state.view.zoom, depth];
}
function drawPoints(ctx, canvas, points, color, size, alpha) {
  ctx.fillStyle=color; ctx.globalAlpha=alpha;
  [...points].map((point) => [project(point,canvas),point]).sort((a,b)=>a[0][2]-b[0][2]).forEach(([screen]) => ctx.fillRect(screen[0]-size/2,screen[1]-size/2,size,size)); ctx.globalAlpha=1;
}
function visibleSampledPoints() {
  const points=state.crop?.sampled_points_xyz||[], range=isRoiItem()?roiHeightRange():null, ground=state.current?.ground_z_m??0;
  return range?points.filter((point)=>point[2]-ground>=range[0]&&point[2]-ground<=range[1]):points;
}
function drawPolyline(ctx, canvas, points, color, width = 1.8) {
  if (!points?.length) return; ctx.strokeStyle=color; ctx.lineWidth=width; ctx.setLineDash([]); ctx.beginPath();
  points.forEach((point,index) => { const screen=project(point,canvas); index ? ctx.lineTo(screen[0],screen[1]) : ctx.moveTo(screen[0],screen[1]); }); ctx.stroke();
}
function drawDashedPolyline(ctx,canvas,points,color,width=1.8){if(!points?.length)return;ctx.strokeStyle=color;ctx.lineWidth=width;ctx.setLineDash([7,5]);ctx.beginPath();points.forEach((point,index)=>{const screen=project(point,canvas);index?ctx.lineTo(screen[0],screen[1]):ctx.moveTo(screen[0],screen[1]);});ctx.stroke();ctx.setLineDash([]);}
function drawAttachmentMarker(ctx,canvas,position,color,label,selected=false){if(!position)return;const p=project([position.x,position.y,position.z],canvas),d=selected?7:5;ctx.strokeStyle=color;ctx.fillStyle=selected?color:"transparent";ctx.lineWidth=selected?2.5:1.5;ctx.beginPath();ctx.moveTo(p[0],p[1]-d);ctx.lineTo(p[0]+d,p[1]);ctx.lineTo(p[0],p[1]+d);ctx.lineTo(p[0]-d,p[1]);ctx.closePath();ctx.fill();ctx.stroke();if(label){ctx.fillStyle=color;ctx.font="10px system-ui";ctx.fillText(label,p[0]+d+3,p[1]-d);}}
function drawOrientedPlane(ctx,canvas,plane,color,label){if(!plane?.center_xyz||!plane?.basis_u||!plane?.basis_v)return;const c=plane.center_xyz,u=plane.basis_u,v=plane.basis_v,d=.42,point=(su,sv)=>[c[0]+u[0]*d*su+v[0]*d*sv,c[1]+u[1]*d*su+v[1]*d*sv,c[2]+u[2]*d*su+v[2]*d*sv],corners=[point(-1,-1),point(1,-1),point(1,1),point(-1,1),point(-1,-1)];ctx.strokeStyle=color;ctx.lineWidth=1.6;ctx.setLineDash([8,4]);ctx.beginPath();corners.forEach((item,index)=>{const p=project(item,canvas);index?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1]);});ctx.stroke();ctx.setLineDash([]);const p=project(corners[2],canvas);ctx.fillStyle=color;ctx.font="10px system-ui";ctx.fillText(label,p[0]+4,p[1]-4);}
function drawVerticalDimension(ctx,canvas,attachment,offset,color){if(!attachment?.position_xyz||offset==null)return;const a=[attachment.position_xyz.x,attachment.position_xyz.y,attachment.position_xyz.z],b=[a[0],a[1],a[2]+offset],pa=project(a,canvas),pb=project(b,canvas);ctx.strokeStyle=color;ctx.lineWidth=2;ctx.setLineDash([]);ctx.beginPath();ctx.moveTo(pa[0],pa[1]);ctx.lineTo(pb[0],pb[1]);ctx.stroke();for(const p of [pa,pb]){ctx.beginPath();ctx.moveTo(p[0]-4,p[1]);ctx.lineTo(p[0]+4,p[1]);ctx.stroke();}ctx.fillStyle=color;ctx.font="bold 11px system-ui";ctx.fillText("+0.30 m vertical",(pa[0]+pb[0])/2+6,(pa[1]+pb[1])/2);}
function drawPhase5aFit(ctx,canvas,phase5a){const plane=phase5a.measurement_plane,fit=phase5a.fit;if(!plane?.center_xyz||!plane?.basis_u||!plane?.basis_v||!fit?.center||fit.radius_m==null)return;const c=plane.center_xyz,u=plane.basis_u,v=plane.basis_v,ellipse=fit.ellipse||{},useEllipse=phase5a.measurement?.fit_model==="ELLIPSE"&&ellipse.valid,fc=useEllipse?ellipse.center:fit.center,r=fit.radius_m;ctx.strokeStyle="#ffffff";ctx.lineWidth=2;ctx.setLineDash([]);ctx.beginPath();for(let i=0;i<=48;i++){const t=Math.PI*2*i/48;let du,dv;if(useEllipse){const a=ellipse.semi_major_axis_m,b=ellipse.semi_minor_axis_m,q=ellipse.rotation_rad||0,x=Math.cos(t)*a,y=Math.sin(t)*b;du=fc[0]+x*Math.cos(q)-y*Math.sin(q);dv=fc[1]+x*Math.sin(q)+y*Math.cos(q);}else{du=fc[0]+Math.cos(t)*r;dv=fc[1]+Math.sin(t)*r;}const p=[c[0]+u[0]*du+v[0]*dv,c[1]+u[1]*du+v[1]*dv,c[2]+u[2]*du+v[2]*dv],s=project(p,canvas);i?ctx.lineTo(s[0],s[1]):ctx.moveTo(s[0],s[1]);}ctx.stroke();}
function drawPlane(ctx, canvas, height, color, label) {
  const item=state.current, ground=item?.ground_z_m; if (!item?.position || ground == null || height == null) return;
  const cx=item.position.x, cy=item.position.y, z=ground+height, d=.75;
  const corners=[[cx-d,cy-d,z],[cx+d,cy-d,z],[cx+d,cy+d,z],[cx-d,cy+d,z],[cx-d,cy-d,z]];
  ctx.strokeStyle=color; ctx.lineWidth=1; ctx.setLineDash([5,4]); ctx.beginPath(); corners.forEach((p,i)=>{const s=project(p,canvas);i?ctx.lineTo(s[0],s[1]):ctx.moveTo(s[0],s[1]);});ctx.stroke();ctx.setLineDash([]);
  const screen=project([cx+d,cy+d,z],canvas);ctx.fillStyle=color;ctx.font="11px system-ui";ctx.fillText(label,screen[0]+4,screen[1]-4);
}
function drawComponentCentres(ctx, canvas, sections, ground, color, selectedColor) {
  for (const section of sections || []) {
    for (const fit of section.fits || []) { if (!fit?.center) continue; const s=project([fit.center[0],fit.center[1],ground+section.height_m],canvas); ctx.strokeStyle=color;ctx.globalAlpha=.65;ctx.beginPath();ctx.arc(s[0],s[1],2.5,0,Math.PI*2);ctx.stroke(); }
    const fit=section.selected_fit;if(fit?.center){const s=project([fit.center[0],fit.center[1],ground+section.height_m],canvas);ctx.strokeStyle=selectedColor;ctx.globalAlpha=1;ctx.lineWidth=1.5;ctx.beginPath();ctx.arc(s[0],s[1],5,0,Math.PI*2);ctx.stroke();}
  }
  ctx.globalAlpha=1;
}
function draw() {
  const canvas=$("pointCanvas"), ctx=canvas.getContext("2d"); ctx.clearRect(0,0,canvas.clientWidth,canvas.clientHeight); ctx.fillStyle="#06100c"; ctx.fillRect(0,0,canvas.clientWidth,canvas.clientHeight);
  if (!state.current?.position) { ctx.fillStyle="#99ac9f";ctx.font="14px system-ui";ctx.fillText("Manual workflow: enter XY or click while viewing a nearby candidate crop",24,42); return; }
  const crop=state.crop || {}, ground=state.current.ground_z_m ?? 0, heightRange=isRoiItem()?roiHeightRange():null;
  const visible=(points)=>heightRange?(points||[]).filter((point)=>point[2]-ground>=heightRange[0]&&point[2]-ground<=heightRange[1]):(points||[]);
  drawPoints(ctx,canvas,isRoiItem()?visibleSampledPoints():visible(crop.sampled_points_xyz),isRoiItem()?"#9aada2":"#71857a",isRoiItem()?1.55:1.3,isRoiItem() ? .68 : .48); drawPoints(ctx,canvas,visible(crop.full_rejected_points_xyz),"#ff8f5a",1.6,.52); drawPoints(ctx,canvas,visible(crop.full_accepted_points_xyz),"#7ce3a0",1.7,.82);
  const sampled=state.evidence?.sampled, full=state.evidence?.full_resolution;
  drawComponentCentres(ctx,canvas,sampled?.components_by_height,ground,"#8ec5ff","#ffd166");
  drawComponentCentres(ctx,canvas,full?.components_by_height,ground,"#ff79c6","#ffffff");
  drawPolyline(ctx,canvas,sampled?.centreline?.points_xyz,"#ffd166",2.2); drawPolyline(ctx,canvas,full?.centreline?.points_xyz,"#8ec5ff",2.2);
  if (state.evidence?.phase4c) {
    drawPolyline(ctx,canvas,state.evidence.phase4c.child_centreline_points_xyz,"#ffd166",2.6);
    drawPolyline(ctx,canvas,state.evidence.phase4c.parent_centreline_points_xyz,"#ff79c6",2.6);
    const attach=state.evidence.phase4c.attachment_height_agl_m;
    if(attach!=null)drawPlane(ctx,canvas,attach,"#ff9f43",`proposed attachment ${Number(attach).toFixed(2)} m`);
  }
  if (state.evidence?.phase5a) {
    const p5=state.evidence.phase5a,attachment=p5.highest_prop_root_attachment||{};
    drawPolyline(ctx,canvas,p5.main_stem_centerline_points_xyz,"#ffd166",3.0);
    for(const root of p5.candidate_root_tracks||[])drawDashedPolyline(ctx,canvas,root.points_xyz,"#ff79c6",2.2);
    for(const candidate of p5.attachment_candidates||[])drawAttachmentMarker(ctx,canvas,candidate.position_xyz,"#ff9f43",candidate.attachment_candidate_id===attachment.attachment_candidate_id?"highest supported":"candidate",candidate.attachment_candidate_id===attachment.attachment_candidate_id);
    if(attachment.position_xyz)drawAttachmentMarker(ctx,canvas,attachment.position_xyz,"#7ce3a0",attachment.status,true);
    drawVerticalDimension(ctx,canvas,attachment,p5.measurement_protocol?.offset_m,"#8ec5ff");
    drawOrientedPlane(ctx,canvas,p5.measurement_plane,"#8ec5ff","protocol POM plane ⟂ local axis");
    drawPoints(ctx,canvas,p5.extracted_cross_section_points_xyz||[],"#8ec5ff",2.2,.95);
    drawPhase5aFit(ctx,canvas,p5);
    ctx.fillStyle="#ff8f5a";ctx.font="bold 12px system-ui";ctx.fillText("LiDAR estimate — not field verified",18,canvas.clientHeight-46);
  }
  if (state.evidence?.roi) drawRoiEvidence(ctx, canvas, ground, state.evidence.roi);
  if (!sampled && state.current.track?.centreline_coefficients) {
    const track=state.current.track, heights=track.source_heights_m||[], coeff=track.centreline_coefficients;
    const points=heights.length ? Array.from({length:40},(_,i)=>{const h=Math.min(...heights)+(Math.max(...heights)-Math.min(...heights))*i/39;return [coeff[0][0]*h+coeff[0][1],coeff[1][0]*h+coeff[1][1],ground+h];}) : [];
    drawPolyline(ctx,canvas,points,"#ffd166",2);
  }
  if (!isRoiItem() && !isPhase5aItem()) {
    drawPlane(ctx,canvas,1.30,"#8ec5ff","reference 1.30 m");
    if (state.current.measurement_height_m != null && Math.abs(state.current.measurement_height_m-1.3)>.001) drawPlane(ctx,canvas,state.current.measurement_height_m,"#ffd166",`selected POM ${state.current.measurement_height_m.toFixed(2)} m`);
  }
}

function drawRoiEvidence(ctx, canvas, ground, roi) {
  ctx.font = "10px system-ui";
  if (roi.bounds) {
    const b=roi.bounds,z=ground+1.3,corners=[[b.x_min,b.y_min,z],[b.x_max,b.y_min,z],[b.x_max,b.y_max,z],[b.x_min,b.y_max,z],[b.x_min,b.y_min,z]];
    ctx.strokeStyle="#ffd166";ctx.lineWidth=1.5;ctx.setLineDash([7,4]);ctx.beginPath();corners.forEach((item,index)=>{const p=project(item,canvas);index?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1]);});ctx.stroke();ctx.setLineDash([]);
  }
  if (state.roiAlgorithmOverlay) {
    for (const item of roi.current_tree_centers || []) {
      const p = project([item.x,item.y,ground+1.3],canvas), selected=item.tree_id===state.selectedPredictionId;
      ctx.strokeStyle=selected?"#ffd166":"#7ce3a0";ctx.fillStyle=selected?"#ffd166":"#7ce3a0";ctx.lineWidth=selected?3:1.5;ctx.beginPath();ctx.arc(p[0],p[1],selected?8:4.5,0,Math.PI*2);ctx.stroke();
      if(selected)ctx.fillText(item.tree_id,p[0]+10,p[1]+3);
    }
    for (const item of roi.unresolved_track_centers || []) {
      const p = project([item.x,item.y,ground+1.3],canvas);ctx.strokeStyle="#ff79c6";ctx.globalAlpha=.7;ctx.lineWidth=1;ctx.beginPath();ctx.arc(p[0],p[1],2.5,0,Math.PI*2);ctx.stroke();ctx.globalAlpha=1;
    }
  }
  for (const item of state.roiCensus?.reference_trees || []) {
    const markerZ=state.roiWorkflowStep==="ASSOCIATE"?ground+1.3:(item.center.z??ground+1.3),p=project([item.center.x,item.center.y,markerZ],canvas),selected=item.reference_tree_id===state.selectedReferenceId;
    ctx.fillStyle=selected?"#ffd166":"#8ec5ff";ctx.strokeStyle="#06100c";ctx.lineWidth=2;ctx.beginPath();ctx.arc(p[0],p[1],selected?7:5,0,Math.PI*2);ctx.fill();ctx.stroke();
    if(selected){ctx.fillStyle="#ffd166";ctx.fillText(item.reference_tree_id,p[0]+8,p[1]-5);}
  }
}

function drawProfile() {
  const canvas=$("profileCanvas"),ctx=canvas.getContext("2d");ctx.clearRect(0,0,canvas.clientWidth,canvas.clientHeight);
  let sampled=state.evidence?.sampled?.radius_profile_selected || [], full=state.evidence?.full_resolution?.radius_profile_selected || [];
  if (!sampled.length && state.current?.track?.observations) sampled=state.current.track.observations.filter((x)=>x.radius_m!=null).map((x)=>({height_m:x.source_height_m,radius_m:x.radius_m}));
  const all=[...sampled,...full].filter((x)=>x.height_m!=null&&x.radius_m!=null); if(!all.length)return;
  const pad=32,w=canvas.clientWidth-2*pad,h=canvas.clientHeight-2*pad;
  const heights=all.map(x=>x.height_m),radii=all.map(x=>x.radius_m),minH=Math.min(...heights,1.3)-.1,maxH=Math.max(...heights,1.3)+.1,maxR=Math.max(.05,...radii)*1.15;
  ctx.strokeStyle="#365542";ctx.strokeRect(pad,pad,w,h);ctx.fillStyle="#99ac9f";ctx.font="11px system-ui";ctx.fillText(`${minH.toFixed(2)} m`,2,pad+h);ctx.fillText(`${maxH.toFixed(2)} m`,2,pad+8);ctx.fillText(`${maxR.toFixed(2)} m radius`,pad+4,12);
  const yFor=(height)=>pad+(maxH-height)/(maxH-minH)*h, xFor=(radius)=>pad+radius/maxR*w;
  const refY=yFor(1.3);ctx.strokeStyle="#8ec5ff";ctx.setLineDash([4,3]);ctx.beginPath();ctx.moveTo(pad,refY);ctx.lineTo(pad+w,refY);ctx.stroke();ctx.setLineDash([]);
  const series=(values,color)=>{ctx.strokeStyle=color;ctx.fillStyle=color;ctx.beginPath();[...values].sort((a,b)=>a.height_m-b.height_m).forEach((item,i)=>{const x=xFor(item.radius_m),y=yFor(item.height_m);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.stroke();values.forEach((item)=>{ctx.beginPath();ctx.arc(xFor(item.radius_m),yFor(item.height_m),3,0,Math.PI*2);ctx.fill();});};
  series(sampled,"#ffd166");series(full,"#8ec5ff");
}

function pointerDown(event) { const canvas=$("pointCanvas"); canvas.setPointerCapture(event.pointerId); state.drag={x:event.clientX,y:event.clientY,moved:false}; }
function pointerMove(event) { if(!state.drag||state.manualMode||state.phase5aPickMode)return; const dx=event.clientX-state.drag.x,dy=event.clientY-state.drag.y;if(Math.abs(dx)+Math.abs(dy)>2)state.drag.moved=true;state.view.yaw+=dx*.008;state.view.pitch=Math.max(-1.45,Math.min(1.57,state.view.pitch+dy*.006));state.drag.x=event.clientX;state.drag.y=event.clientY;draw(); }
function screenToWorldTop(event) {
  const rect=$("pointCanvas").getBoundingClientRect(),sx=event.clientX-rect.left,sy=event.clientY-rect.top,center=center3(),xr=(sx-rect.width/2)/state.view.zoom,yr=-(sy-rect.height/2)/state.view.zoom,cy=Math.cos(state.view.yaw),ss=Math.sin(state.view.yaw);
  return {x:center[0]+cy*xr+ss*yr,y:center[1]-ss*xr+cy*yr};
}
function canvasClickPosition(event) { const rect=$("pointCanvas").getBoundingClientRect(); return {x:event.clientX-rect.left,y:event.clientY-rect.top}; }
function nearestProjected(items, xyzFor, click, maxPixels = 18) {
  let best=null;
  for(const item of items){
    const xyz=xyzFor(item),screen=project(xyz,$("pointCanvas")),distance2=(screen[0]-click.x)**2+(screen[1]-click.y)**2;
    if(distance2>maxPixels**2)continue;
    if(!best||distance2<best.distance2-1||(Math.abs(distance2-best.distance2)<=1&&screen[2]>best.depth))best={item,xyz,screen,distance2,depth:screen[2]};
  }
  return best;
}
function pickVisibleLidarPoint(event) {
  return nearestProjected(visibleSampledPoints(),(point)=>point,canvasClickPosition(event),20);
}
function selectRoiMarkerAt(event) {
  if (!isRoiItem()) return;
  const click=canvasClickPosition(event),ground=state.current?.ground_z_m??0;
  const referenceHit=nearestProjected(state.roiCensus?.reference_trees||[],(item)=>[item.center.x,item.center.y,state.roiWorkflowStep==="ASSOCIATE"?ground+1.3:(item.center.z??ground+1.3)],click,14);
  if(referenceHit){state.selectedReferenceId=referenceHit.item.reference_tree_id;renderRoiCensus();draw();return;}
  if(!state.roiAlgorithmOverlay)return;
  const treeHit=nearestProjected(state.evidence?.roi?.current_tree_centers||[],(item)=>[item.x,item.y,ground+1.3],click,14);
  if(treeHit){state.selectedPredictionId=treeHit.item.tree_id;renderRoiCensus();draw();}
}
function pointerUp(event) {
  if(state.phase5aPickMode&&state.drag&&!state.drag.moved&&isPhase5aItem()){
    const picked=pickVisibleLidarPoint(event);
    if(!picked){alert("No visible LiDAR point near that click");state.drag=null;return;}
    const point=picked.xyz;$("phase5aAttachmentX").value=Number(point[0]).toFixed(3);$("phase5aAttachmentY").value=Number(point[1]).toFixed(3);$("phase5aAttachmentZ").value=Number(point[2]).toFixed(3);$("phase5aAttachmentStatus").value="PROBABLE";updatePhase5aPomPreview();state.phase5aPickMode=false;$("pointCanvas").classList.remove("phase5a-pick");$("phase5aPickAttachment").textContent="Click manual attachment point";
  } else if(state.roiEditMode&&state.drag&&!state.drag.moved&&isRoiItem()){
    const completedMode=state.roiEditMode;
    const picked=pickVisibleLidarPoint(event);
    if(!picked){state.roiPickFeedback="ไม่พบจุด LiDAR ใกล้ตำแหน่งที่คลิก — กรุณาคลิกให้โดนจุดของลำต้น";renderRoiCensus();draw();state.drag=null;return;}
    const point=picked.xyz,ground=state.current?.ground_z_m??0,clickEvidence={source:"NEAREST_VISIBLE_LIDAR_POINT",source_point_xyz:point.map((value)=>Number(value.toFixed(4))),height_agl_m:Number((point[2]-ground).toFixed(3)),height_band:state.roiHeightBand,view:{...state.view}};
    state.roiPickFeedback=`จับจุด LiDAR ที่ความสูง ${(point[2]-ground).toFixed(2)} ม. เหนือพื้น · XY ${point[0].toFixed(3)}, ${point[1].toFixed(3)}`;
    if(state.roiEditMode==="ADD")addRoiReferenceAt(Number(point[0].toFixed(3)),Number(point[1].toFixed(3)),Number(point[2].toFixed(4)),clickEvidence);
    else {const reference=state.roiCensus.reference_trees.find((x)=>x.reference_tree_id===state.selectedReferenceId);if(reference){reference.center.x=Number(point[0].toFixed(3));reference.center.y=Number(point[1].toFixed(3));reference.center.z=Number(point[2].toFixed(4));reference.click_evidence=clickEvidence;reference.boundary_relation=roiBoundary(reference.center);state.roiCensus.annotation_status="IN_PROGRESS";saveLocal();renderRoiCensus();draw();}}
    if(completedMode==="MOVE"){state.roiEditMode=null;$("pointCanvas").classList.remove("roi-edit");$("moveRoiReference").classList.remove("active");}
    else{$("pointCanvas").classList.add("roi-edit");$("addRoiReference").classList.add("active");renderRoiCensus();}
  } else if(state.manualMode&&state.drag&&!state.drag.moved&&state.current?.position){const point=screenToWorldTop(event);$("manualX").value=point.x.toFixed(3);$("manualY").value=point.y.toFixed(3);$("manualNote").focus();}
  else if(isRoiItem()&&state.drag&&!state.drag.moved)selectRoiMarkerAt(event);
  state.drag=null;
}

init().catch((error) => { $("summary").textContent = `โหลด review interface ไม่สำเร็จ: ${error.message}`; console.error(error); });
