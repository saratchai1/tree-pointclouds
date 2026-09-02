const $ = (id) => document.getElementById(id);
const state = {
  payload: null,
  summary: null,
  evidenceIndex: null,
  shardCache: new Map(),
  records: [],
  filtered: [],
  current: null,
  evidence: null,
  view: { yaw: -0.55, pitch: 0.32, zoom: 165 },
  drag: null,
  overview: {
    metadata: null,
    positions: null,
    filter: "ALL",
    selectedTreeId: null,
    THREE: null,
    renderer: null,
    scene: null,
    camera: null,
    controls: null,
    cloud: null,
    grid: null,
    markerLayer: null,
    markers: new Map(),
    markerHitTargets: [],
    markerTextures: new Map(),
    selectedLabel: null,
    raycaster: null,
    pointer: null,
    pointerStart: null,
    frameMode: "TREES",
    displayedPointCount: 0,
    animationFrame: null,
  },
};

const STATUS_COLORS = { STANDARD_DBH: "#75bfff", ALTERNATIVE_POM: "#ffd166", MANUAL_REVIEW: "#ff915f" };
const OVERVIEW_POSITION_CHUNKS = ["positions-00.glbin", "positions-01.glbin", "positions-02.glbin"];
const OVERVIEW_POINT_BUDGET = 300000;
const QUALITY_LABELS = {
  angular_coverage: "arc coverage",
  fit_quality: "คุณภาพ circle fit",
  circularity: "ความเป็นวงกลม",
  radius_stability: "รัศมีคงที่",
  axis_alignment: "ตรงกับแกนลำต้น",
  vertical_continuity: "ต่อเนื่องหลายระดับ",
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[character]);
}

function format(value, digits = 2) {
  if (value == null || value === "") return "—";
  return Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : String(value);
}

function option(select, value, label = value) {
  const node = document.createElement("option");
  node.value = value;
  node.textContent = label;
  select.append(node);
}

function focusCandidate() {
  return state.current?.selected_candidate || state.current?.best_review_candidate || null;
}

function focusPlane() {
  return state.current?.measurement_plane || state.current?.best_review_plane || null;
}

async function fetchJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

async function init() {
  [state.payload, state.summary, state.evidenceIndex] = await Promise.all([
    fetchJson("data/measurements.json"),
    fetchJson("data/summary.json"),
    fetchJson("data/evidence-index.json"),
  ]);
  state.records = state.payload.records;
  renderSummary();
  [...new Set(state.records.map((record) => record.detection_status))].sort().forEach((value) => option($("detectionFilter"), value));
  bind();
  resize();
  applyFilters(new URLSearchParams(location.search).get("tree"));
  initOverview().catch((error) => {
    $("overviewStatus").textContent = `โหลด point cloud ภาพรวมไม่สำเร็จ: ${error.message}`;
    console.error(error);
  });
}

function bind() {
  ["statusFilter", "detectionFilter"].forEach((id) => $(id).addEventListener("change", () => applyFilters()));
  $("treeSearch").addEventListener("input", () => applyFilters());
  $("treeSelect").addEventListener("change", () => selectTree($("treeSelect").value));
  $("resetView").addEventListener("click", () => {
    state.view = { yaw: -0.55, pitch: 0.32, zoom: 165 };
    drawCloud();
  });
  $("topView").addEventListener("click", () => {
    state.view = { ...state.view, yaw: 0, pitch: Math.PI / 2 };
    drawCloud();
  });
  const canvas = $("cloudCanvas");
  canvas.addEventListener("pointerdown", pointerDown);
  canvas.addEventListener("pointermove", pointerMove);
  canvas.addEventListener("pointerup", pointerUp);
  canvas.addEventListener("pointercancel", pointerUp);
  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    state.view.zoom = Math.max(35, Math.min(550, state.view.zoom * Math.exp(-event.deltaY * 0.001)));
    drawCloud();
  }, { passive: false });
  bindOverview();
  window.addEventListener("resize", resize);
}

function renderSummary() {
  const counts = state.summary.status_counts;
  $("automaticCount").textContent = state.summary.automatic_measurement_count;
  $("standardCount").textContent = counts.STANDARD_DBH || 0;
  $("alternativeCount").textContent = counts.ALTERNATIVE_POM || 0;
  $("manualCount").textContent = counts.MANUAL_REVIEW || 0;
  const comparison = state.summary.coverage_comparison;
  $("headline").textContent = `${state.summary.automatic_measurement_count} / ${state.summary.tree_count} Tree IDs มีผลอัตโนมัติจาก full LAS · ยังไม่ field-verified`;
  $("coverageDelta").textContent = `Coverage: V2 ${comparison.v2_phase4_measurable_count} → V3 ${comparison.v3_sampled_evidence_automatic_count} → V3.1 ${comparison.v3_1_full_las_automatic_count} ต้น (เทียบ V3 ${comparison.net_change_from_v3 >= 0 ? "+" : ""}${comparison.net_change_from_v3}); ไม่ใช่การเทียบ accuracy`;
}

function applyFilters(preferredTree = null) {
  const status = $("statusFilter").value;
  const detection = $("detectionFilter").value;
  const query = $("treeSearch").value.trim().toUpperCase();
  state.filtered = state.records.filter((record) =>
    (!status || record.status === status)
    && (!detection || record.detection_status === detection)
    && (!query || record.tree_id.includes(query))
  );
  const select = $("treeSelect");
  const prior = preferredTree || state.current?.tree_id;
  select.innerHTML = "";
  state.filtered.forEach((record) => option(select, record.tree_id, `${record.tree_id} · ${record.status}`));
  $("filterCount").textContent = `${state.filtered.length} / ${state.records.length} Tree IDs`;
  const next = state.filtered.some((record) => record.tree_id === prior) ? prior : state.filtered[0]?.tree_id;
  if (next) {
    select.value = next;
    selectTree(next);
  } else {
    state.current = null;
    state.evidence = null;
    render();
  }
}

async function selectTree(treeId) {
  state.current = state.records.find((record) => record.tree_id === treeId) || null;
  state.evidence = null;
  state.view = { yaw: -0.55, pitch: 0.32, zoom: 165 };
  $("treeSelect").value = treeId;
  updateOverviewSelection(state.current);
  history.replaceState(null, "", `${location.pathname}?tree=${encodeURIComponent(treeId)}`);
  render();
  const shardName = state.evidenceIndex.trees[treeId];
  try {
    if (!state.shardCache.has(shardName)) state.shardCache.set(shardName, await fetchJson(`data/${shardName}`));
    state.evidence = state.shardCache.get(shardName).evidence[treeId];
  } catch (error) {
    state.evidence = { error: error.message, candidate_profile: [], tube_sample_xyz: [] };
  }
  render();
}

function render() {
  renderMetrics();
  renderQuality();
  renderCandidates();
  renderProvenance();
  drawCloud();
  drawProfile();
  drawCross();
}

function renderMetrics() {
  const record = state.current;
  if (!record) {
    $("treeTitle").textContent = "—";
    $("treeState").textContent = "";
    $("measurementMetrics").innerHTML = "";
    return;
  }
  $("treeTitle").textContent = record.tree_id;
  $("treeState").textContent = `${record.status} · ${record.confidence_label}`;
  const axis = record.local_axis || {};
  const rows = [
    ["สถานะ", record.status],
    ["POM", record.measurement_height_agl_m == null ? `review candidate ${format(record.candidate_height_agl_m)} ม.` : `${format(record.measurement_height_agl_m)} ม. AGL`],
    ["เส้นผ่านศูนย์กลาง", record.diameter_cm == null ? "—" : `${format(record.diameter_cm)} ซม.`],
    ["DBH", record.dbh_cm == null ? "ไม่ใช้คำว่า DBH" : `${format(record.dbh_cm)} ซม.`],
    ["เส้นรอบวง", record.circumference_cm == null ? "—" : `${format(record.circumference_cm)} ซม.`],
    ["Local ground", `${format(record.local_ground_z_m, 3)} ม.`],
    ["แกนเอียงจากแนวดิ่ง", `${format(axis.inclination_deg)}°`],
    ["Axis support / uncertainty", `${axis.supporting_slice_count ?? "—"} slices / ${format(axis.uncertainty_p90_m, 3)} ม.`],
    ["Fit RMSE", `${format(record.fit_rmse_m, 4)} ม.`],
    ["Circularity", format(record.circularity, 3)],
    ["Arc coverage", `${format(record.arc_coverage_deg, 0)}°`],
    ["Radius MAD", `${format(record.radius_stability_mad_m, 4)} ม.`],
    ["Slice points / inliers", `${record.point_count ?? "—"} / ${record.inlier_count ?? "—"}`],
    ["Full-LAS tube", `${Number(record.full_resolution_tube_point_count || 0).toLocaleString()} จุด`],
    ["Field verified", "NO"],
  ];
  $("measurementMetrics").innerHTML = rows.map(([label, value]) => `<div class="metric-row"><span>${escapeHtml(label)}</span><span>${escapeHtml(value)}</span></div>`).join("");
}

function renderQuality() {
  const record = state.current;
  const score = record?.quality_score;
  const ring = $("qualityScore");
  ring.textContent = score == null ? "—" : format(score, 1);
  ring.style.borderColor = score == null ? "#294638" : score >= 82 ? "#71e3a0" : score >= 68 ? "#ffd166" : "#ff915f";
  const components = record?.quality_components || {};
  $("qualityBars").innerHTML = Object.entries(QUALITY_LABELS).map(([key, label]) => {
    const value = Number(components[key] || 0);
    return `<div class="quality-row"><span>${escapeHtml(label)}</span><div class="quality-track"><div class="quality-fill" style="width:${Math.round(value * 100)}%"></div></div><strong>${Math.round(value * 100)}</strong></div>`;
  }).join("");
  $("reasonCodes").innerHTML = (record?.reason_codes || []).map((reason) => `<span class="chip">${escapeHtml(reason)}</span>`).join("");
}

function renderCandidates() {
  const profile = state.evidence?.candidate_profile || [];
  const focusHeight = state.evidence?.focus_height_agl_m;
  $("candidateRows").innerHTML = profile.map((candidate) => {
    const lane = Math.abs(candidate.height_agl_m - 1.3) < 0.001 ? "standard" : "alternative";
    const failures = candidate[`${lane}_failures`] || [];
    const chosen = focusHeight != null && Math.abs(candidate.height_agl_m - focusHeight) < 0.001;
    const decision = chosen
      ? (state.current?.automatic_measurement ? "SELECTED" : "BEST · REVIEW")
      : candidate.fit_valid && !failures.length ? "PASS" : failures.slice(0, 2).join(" · ") || "NO FIT";
    const failed = !candidate.fit_valid || failures.length;
    return `<tr class="${chosen ? "selected" : failed ? "failed" : ""}">
      <td>${format(candidate.height_agl_m, 1)}</td>
      <td>${format(candidate.diameter_cm, 2)} ซม.</td>
      <td>${format(candidate.quality_score, 1)}</td>
      <td>${candidate.point_count ?? "—"} / ${candidate.inlier_count ?? "—"}</td>
      <td>${format(candidate.angular_coverage_deg, 0)}°</td>
      <td>${format(candidate.circularity, 3)}</td>
      <td>${format(candidate.radius_stability_mad_m, 4)}</td>
      <td>${format(candidate.relative_radius_range, 3)}</td>
      <td>${format(candidate.fit_rmse_m, 4)}</td>
      <td>${format(candidate.axis_center_offset_m, 3)}</td>
      <td class="${failed ? "decision-fail" : "decision-ok"}">${escapeHtml(decision)}</td>
    </tr>`;
  }).join("") || '<tr><td colspan="11" class="muted">กำลังโหลด evidence หรือไม่มีหน้าตัดที่ fit ได้</td></tr>';
}

function renderProvenance() {
  const record = state.current;
  if (!record || !state.summary) {
    $("provenance").innerHTML = "";
    return;
  }
  const source = state.summary.source.source_las;
  const baseline = record.v3_baseline || {};
  $("provenance").innerHTML = `<div class="provenance-grid">
    <div><strong>Raw source</strong><span class="muted">${escapeHtml(source.file_name)} · ${Number(source.point_count).toLocaleString()} points · SHA-256 ${escapeHtml(source.sha256.slice(0, 16))}…</span></div>
    <div><strong>Geometry</strong><span class="muted">หน้าตัดทุกระดับ fit จากจุดเต็มความละเอียดบนระนาบตั้งฉากกับ local axis; สแกนจริง 1.30–4.00 ม.</span></div>
    <div><strong>Selection</strong><span class="muted">ให้ 1.30 ม. ก่อนเมื่อผ่าน QA; มิฉะนั้นเลือกช่วงต่ำสุดในกลุ่ม near-best ที่สะอาดและต่อเนื่อง</span></div>
    <div><strong>Root-crown guardrail</strong><span class="muted">กันวงกลมที่ครอบหลายรากด้วย radius range, cleaner-upper-section test และ cohort radius guardrail</span></div>
    <div><strong>V3 context (read-only)</strong><span class="muted">${escapeHtml(baseline.status || "—")} · POM ${format(baseline.measurement_height_agl_m)} ม. · circumference ${format(baseline.circumference_cm)} ซม.</span></div>
    <div><strong>Confidence</strong><span class="muted">${escapeHtml(record.confidence_label)} เป็น deterministic geometry QA label; ยังไม่ calibrated และยังไม่ field-verified</span></div>
  </div>`;
}

function resizeCanvas(canvas) {
  const ratio = devicePixelRatio || 1;
  const rectangle = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.floor(rectangle.width * ratio));
  canvas.height = Math.max(1, Math.floor(rectangle.height * ratio));
  canvas.getContext("2d").setTransform(ratio, 0, 0, ratio, 0, 0);
}

function resize() {
  ["cloudCanvas", "profileCanvas", "crossCanvas"].forEach((id) => resizeCanvas($(id)));
  resizeOverviewCanvas();
  render();
}

function cloudCenter() {
  const record = state.current;
  return record ? [record.location.x || 0, record.location.y || 0, (record.local_ground_z_m || 0) + 2] : [0, 0, 0];
}

function project(point, canvas) {
  const center = cloudCenter();
  const x = point[0] - center[0], y = point[1] - center[1], z = point[2] - center[2];
  const cosineYaw = Math.cos(state.view.yaw), sineYaw = Math.sin(state.view.yaw);
  const cosinePitch = Math.cos(state.view.pitch), sinePitch = Math.sin(state.view.pitch);
  const xRotated = cosineYaw * x - sineYaw * y;
  const yRotated = sineYaw * x + cosineYaw * y;
  return [
    canvas.clientWidth / 2 + xRotated * state.view.zoom,
    canvas.clientHeight / 2 - (yRotated * sinePitch + z * cosinePitch) * state.view.zoom,
    yRotated * cosinePitch - z * sinePitch,
  ];
}

function drawPoints(context, canvas, points, colors) {
  const ordered = points.map((point, index) => [project(point, canvas), colors?.[index]]).sort((left, right) => left[0][2] - right[0][2]);
  context.globalAlpha = 0.7;
  ordered.forEach(([screen, color]) => {
    context.fillStyle = color ? `rgb(${color[0]},${color[1]},${color[2]})` : "#84978b";
    context.fillRect(screen[0] - 0.8, screen[1] - 0.8, 1.6, 1.6);
  });
  context.globalAlpha = 1;
}

function drawPolyline(context, canvas, points, color, width = 2, dashed = false) {
  if (!points?.length) return;
  context.strokeStyle = color;
  context.lineWidth = width;
  context.setLineDash(dashed ? [7, 5] : []);
  context.beginPath();
  points.forEach((point, index) => {
    const screen = project(point, canvas);
    if (index) context.lineTo(screen[0], screen[1]);
    else context.moveTo(screen[0], screen[1]);
  });
  context.stroke();
  context.setLineDash([]);
}

function planePoint(plane, u, v) {
  return plane.center_xyz.map((center, index) => center + plane.basis_u[index] * u + plane.basis_v[index] * v);
}

function drawPlane(context, canvas, plane, radius, color) {
  if (!plane) return;
  const extent = Math.max(0.22, Number(radius || 0) * 1.7);
  const corners = [[-1, -1], [1, -1], [1, 1], [-1, 1], [-1, -1]].map(([u, v]) => planePoint(plane, u * extent, v * extent));
  drawPolyline(context, canvas, corners, color, 1.5, true);
  const circle = Array.from({ length: 65 }, (_, index) => {
    const angle = Math.PI * 2 * index / 64;
    return planePoint(plane, Math.cos(angle) * radius, Math.sin(angle) * radius);
  });
  drawPolyline(context, canvas, circle, color, 2.2, false);
  const axis = plane.axis_direction;
  const center = plane.axis_center_xyz || plane.center_xyz;
  drawPolyline(context, canvas, [
    center.map((value, index) => value - axis[index] * 1.4),
    center.map((value, index) => value + axis[index] * 1.4),
  ], "#71e3a0", 2.1, false);
}

function drawReferencePlane(context, canvas, record) {
  if (record.local_ground_z_m == null) return;
  const extent = 0.42, z = record.local_ground_z_m + 1.3, x = record.location.x, y = record.location.y;
  drawPolyline(context, canvas, [[x - extent, y - extent, z], [x + extent, y - extent, z], [x + extent, y + extent, z], [x - extent, y + extent, z], [x - extent, y - extent, z]], "#75bfff", 1, true);
}

function drawCloud() {
  const canvas = $("cloudCanvas"), context = canvas.getContext("2d");
  context.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
  context.fillStyle = "#06100c";
  context.fillRect(0, 0, canvas.clientWidth, canvas.clientHeight);
  const record = state.current;
  if (!record) return;
  const points = state.evidence?.tube_sample_xyz || [];
  drawPoints(context, canvas, points, state.evidence?.tube_sample_rgb || []);
  drawReferencePlane(context, canvas, record);
  const candidate = focusCandidate(), plane = focusPlane();
  if (plane && candidate?.radius_m) drawPlane(context, canvas, plane, candidate.radius_m, STATUS_COLORS[record.status]);
  $("cloudStatus").textContent = state.evidence?.error
    ? `โหลด evidence ไม่สำเร็จ: ${state.evidence.error}`
    : state.evidence
      ? `แสดง ${points.length.toLocaleString()} จุดจาก full-LAS tube ${Number(state.evidence.full_resolution_tube_point_count || 0).toLocaleString()} จุด · ลากเพื่อหมุน · scroll เพื่อซูม`
      : "กำลังโหลด full-LAS evidence shard…";
}

function drawProfile() {
  const canvas = $("profileCanvas"), context = canvas.getContext("2d");
  const width = canvas.clientWidth, height = canvas.clientHeight;
  context.clearRect(0, 0, width, height);
  context.fillStyle = "#07130e";
  context.fillRect(0, 0, width, height);
  const profile = (state.evidence?.candidate_profile || []).filter((candidate) => candidate.fit_valid && candidate.diameter_cm != null);
  if (!profile.length) return;
  const margin = { left: 42, right: 16, top: 15, bottom: 30 };
  const maximumDiameter = Math.max(10, ...profile.map((candidate) => candidate.diameter_cm)) * 1.12;
  const x = (value) => margin.left + (value - 1.3) / (4.0 - 1.3) * (width - margin.left - margin.right);
  const y = (value) => height - margin.bottom - value / maximumDiameter * (height - margin.top - margin.bottom);
  context.strokeStyle = "#294638";
  context.fillStyle = "#9ab3a5";
  context.font = "11px system-ui";
  [0, 0.5, 1].forEach((fraction) => {
    const value = maximumDiameter * fraction;
    context.beginPath(); context.moveTo(margin.left, y(value)); context.lineTo(width - margin.right, y(value)); context.stroke();
    context.fillText(`${value.toFixed(0)} cm`, 3, y(value) + 4);
  });
  context.strokeStyle = "#8aa99a";
  context.beginPath();
  profile.forEach((candidate, index) => index ? context.lineTo(x(candidate.height_agl_m), y(candidate.diameter_cm)) : context.moveTo(x(candidate.height_agl_m), y(candidate.diameter_cm)));
  context.stroke();
  const focusHeight = state.evidence?.focus_height_agl_m;
  profile.forEach((candidate) => {
    const chosen = focusHeight != null && Math.abs(candidate.height_agl_m - focusHeight) < 0.001;
    context.fillStyle = chosen ? STATUS_COLORS[state.current.status] : candidate.cleaner_smaller_upper_section_available ? "#ff915f" : "#71e3a0";
    context.beginPath(); context.arc(x(candidate.height_agl_m), y(candidate.diameter_cm), chosen ? 5 : 2.8, 0, Math.PI * 2); context.fill();
  });
  context.strokeStyle = "#75bfff"; context.setLineDash([5, 4]);
  context.beginPath(); context.moveTo(x(1.3), margin.top); context.lineTo(x(1.3), height - margin.bottom); context.stroke(); context.setLineDash([]);
  context.fillStyle = "#9ab3a5";
  [1.3, 2, 3, 4].forEach((value) => context.fillText(`${value.toFixed(1)} m`, x(value) - 12, height - 8));
}

function drawCross() {
  const canvas = $("crossCanvas"), context = canvas.getContext("2d");
  const width = canvas.clientWidth, height = canvas.clientHeight;
  context.clearRect(0, 0, width, height);
  context.fillStyle = "#07130e";
  context.fillRect(0, 0, width, height);
  const accepted = state.evidence?.accepted_projected_points_xy || [];
  const rejected = state.evidence?.rejected_projected_points_xy || [];
  const candidate = focusCandidate();
  if (!candidate || !accepted.length) {
    $("crossNote").textContent = state.evidence ? "ไม่มีหน้าตัดที่ fit ได้สำหรับแสดง" : "กำลังโหลด evidence…";
    return;
  }
  const center = candidate.fit_center_xy || [0, 0];
  const extent = Math.max(candidate.radius_m * 1.65, ...accepted.flatMap((point) => point.map(Math.abs)), 0.08);
  const scale = Math.min(width, height) * 0.42 / extent;
  const map = (point) => [width / 2 + point[0] * scale, height / 2 - point[1] * scale];
  context.fillStyle = "#64786d"; context.globalAlpha = 0.38;
  rejected.forEach((point) => { const screen = map(point); context.fillRect(screen[0] - 1, screen[1] - 1, 2, 2); });
  context.fillStyle = "#71e3a0"; context.globalAlpha = 0.8;
  accepted.forEach((point) => { const screen = map(point); context.fillRect(screen[0] - 1.2, screen[1] - 1.2, 2.4, 2.4); });
  context.globalAlpha = 1;
  const circleCenter = map(center);
  context.strokeStyle = STATUS_COLORS[state.current.status]; context.lineWidth = 2; context.beginPath();
  context.arc(circleCenter[0], circleCenter[1], candidate.radius_m * scale, 0, Math.PI * 2); context.stroke();
  context.strokeStyle = "#75bfff"; context.setLineDash([4, 4]); context.beginPath();
  context.moveTo(width / 2 - 6, height / 2); context.lineTo(width / 2 + 6, height / 2);
  context.moveTo(width / 2, height / 2 - 6); context.lineTo(width / 2, height / 2 + 6); context.stroke(); context.setLineDash([]);
  $("crossNote").textContent = `${accepted.length} accepted + ${rejected.length} rejected display points · POM ${format(candidate.height_agl_m)} ม. · diameter ${format(candidate.diameter_cm)} ซม.`;
}

function pointerDown(event) {
  state.drag = { x: event.clientX, y: event.clientY, yaw: state.view.yaw, pitch: state.view.pitch };
  event.currentTarget.setPointerCapture(event.pointerId);
}

function pointerMove(event) {
  if (!state.drag) return;
  state.view.yaw = state.drag.yaw + (event.clientX - state.drag.x) * 0.008;
  state.view.pitch = Math.max(-1.35, Math.min(1.35, state.drag.pitch + (event.clientY - state.drag.y) * 0.006));
  drawCloud();
}

function pointerUp(event) {
  if (state.drag && event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  state.drag = null;
}

function bindOverview() {
  document.querySelectorAll("[data-overview-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      state.overview.filter = button.dataset.overviewFilter;
      document.querySelectorAll("[data-overview-filter]").forEach((item) => {
        item.setAttribute("aria-pressed", String(item === button));
      });
      syncOverviewMarkerVisibility();
    });
  });
  $("overviewFrameTrees").addEventListener("click", frameOverviewTrees);
  $("overviewFrameCloud").addEventListener("click", frameOverviewCloud);
  $("overviewTopView").addEventListener("click", frameOverviewTop);
  $("overviewOpenDetail").addEventListener("click", () => {
    if (!state.overview.selectedTreeId) return;
    $("detailViewer").scrollIntoView({ behavior: "smooth", block: "start" });
  });

  const canvas = $("overviewCanvas");
  canvas.addEventListener("pointerdown", overviewPointerDown);
  canvas.addEventListener("pointermove", overviewPointerMove);
  canvas.addEventListener("pointerup", overviewPointerUp);
  canvas.addEventListener("pointercancel", overviewPointerUp);
  canvas.addEventListener("pointerleave", () => {
    if (!state.overview.pointerStart) canvas.removeAttribute("title");
  });
}

async function fetchArrayBuffer(path) {
  const response = await fetch(path, { cache: "force-cache" });
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.arrayBuffer();
}

async function initOverview() {
  const [THREE, { OrbitControls }] = await Promise.all([
    import("three"),
    import("../viewer/vendor/OrbitControls.js"),
  ]);
  state.overview.THREE = THREE;

  const canvas = $("overviewCanvas");
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, logarithmicDepthBuffer: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  renderer.setClearColor(0x050c09, 1);
  if ("outputColorSpace" in renderer) renderer.outputColorSpace = THREE.SRGBColorSpace;
  state.overview.renderer = renderer;

  const scene = new THREE.Scene();
  scene.add(new THREE.HemisphereLight(0xd8f5e2, 0x13251a, 1.15));
  state.overview.scene = scene;

  const camera = new THREE.PerspectiveCamera(52, 1, 0.03, 600);
  camera.up.set(0, 1, 0);
  state.overview.camera = camera;

  const controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  controls.dampingFactor = 0.07;
  controls.screenSpacePanning = true;
  controls.rotateSpeed = 0.72;
  controls.zoomSpeed = 0.9;
  controls.minDistance = 0.35;
  controls.maxDistance = 480;
  state.overview.controls = controls;
  state.overview.raycaster = new THREE.Raycaster();
  state.overview.pointer = new THREE.Vector2();

  const metadata = await fetchJson("point-cloud/metadata.json");
  state.overview.metadata = metadata;
  $("overviewStatus").textContent = `กำลังโหลด browser sample ${Number(metadata.points).toLocaleString()} จุด…`;
  const buffers = await Promise.all(OVERVIEW_POSITION_CHUNKS.map((name) => fetchArrayBuffer(`point-cloud/${name}?v=v31-overview`)));
  const totalPointCount = buffers.reduce((total, buffer) => total + buffer.byteLength / 12, 0);
  const stride = Math.max(1, Math.ceil(totalPointCount / OVERVIEW_POINT_BUDGET));
  const sampledPointCount = Math.ceil(totalPointCount / stride);
  const positions = new Float32Array(sampledPointCount * 3);
  let sourceIndex = 0;
  let outputIndex = 0;
  buffers.forEach((buffer) => {
    const chunk = new Float32Array(buffer);
    for (let index = 0; index < chunk.length; index += 3, sourceIndex += 1) {
      if (sourceIndex % stride !== 0) continue;
      positions[outputIndex * 3] = chunk[index];
      positions[outputIndex * 3 + 1] = chunk[index + 1];
      positions[outputIndex * 3 + 2] = chunk[index + 2];
      outputIndex += 1;
    }
  });
  state.overview.positions = outputIndex === sampledPointCount ? positions : positions.slice(0, outputIndex * 3);
  state.overview.displayedPointCount = outputIndex;
  buildOverviewCloud();
  buildOverviewMarkers();
  updateOverviewSelection(state.current);
  resizeOverviewCanvas();
  frameOverviewTrees();
  animateOverview();
  $("overviewStatus").textContent = overviewStatusText();
}

function overviewStatusText() {
  const metadata = state.overview.metadata;
  if (!metadata || !state.overview.positions) return "กำลังโหลด point cloud ภาพรวม…";
  return `3D แสดง ${state.overview.displayedPointCount.toLocaleString()} จุดจาก browser sample ${Number(metadata.points).toLocaleString()} จุด · LAS ต้นฉบับ ${Number(metadata.sourcePointCount).toLocaleString()} จุด`;
}

function resizeOverviewCanvas() {
  const { renderer, camera } = state.overview;
  if (!renderer || !camera) return;
  const canvas = renderer.domElement;
  const width = Math.max(canvas.clientWidth, 1);
  const height = Math.max(canvas.clientHeight, 1);
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  renderer.setSize(width, height, false);
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
}

function overviewVisibleRecords() {
  if (["STANDARD_DBH", "ALTERNATIVE_POM", "MANUAL_REVIEW"].includes(state.overview.filter)) {
    return state.records.filter((record) => record.status === state.overview.filter);
  }
  return state.records;
}

function overviewSourceToScene(source) {
  const THREE = state.overview.THREE;
  return new THREE.Vector3(source[0], source[2], -source[1]);
}

function buildOverviewCloud() {
  const { THREE, positions, metadata, scene } = state.overview;
  const scenePositions = new Float32Array(positions.length);
  const colors = new Uint8Array(positions.length);
  const positionAttribute = metadata.attributes?.find((attribute) => attribute.name === "position");
  const zMin = positionAttribute?.min?.[2] ?? metadata.boundingBox?.min?.[2] ?? -9;
  const zMax = positionAttribute?.max?.[2] ?? metadata.boundingBox?.max?.[2] ?? 9;
  const zSpan = Math.max(zMax - zMin, 0.1);
  for (let index = 0; index < positions.length; index += 3) {
    scenePositions[index] = positions[index];
    scenePositions[index + 1] = positions[index + 2];
    scenePositions[index + 2] = -positions[index + 1];
    const heightRatio = Math.max(0, Math.min(1, (positions[index + 2] - zMin) / zSpan));
    colors[index] = 46 + Math.round(heightRatio * 58);
    colors[index + 1] = 76 + Math.round(heightRatio * 86);
    colors[index + 2] = 62 + Math.round(heightRatio * 65);
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(scenePositions, 3));
  geometry.setAttribute("color", new THREE.Uint8BufferAttribute(colors, 3, true));
  geometry.computeBoundingBox();
  geometry.computeBoundingSphere();
  const material = new THREE.PointsMaterial({
    size: 0.065,
    sizeAttenuation: true,
    vertexColors: true,
    transparent: true,
    opacity: 0.92,
  });
  const cloud = new THREE.Points(geometry, material);
  cloud.name = "td008-browser-sample";
  scene.add(cloud);
  state.overview.cloud = cloud;

  const size = geometry.boundingBox.getSize(new THREE.Vector3());
  const gridSize = Math.ceil(Math.max(size.x, size.z) / 10) * 10;
  const grid = new THREE.GridHelper(gridSize, Math.max(10, Math.round(gridSize / 5)), 0x3d7050, 0x193726);
  grid.position.y = geometry.boundingBox.min.y - 0.04;
  grid.material.opacity = 0.34;
  grid.material.transparent = true;
  scene.add(grid);
  state.overview.grid = grid;
}

function overviewMarkerTexture(status, selected = false) {
  const key = `${status}-${selected ? "selected" : "normal"}`;
  if (state.overview.markerTextures.has(key)) return state.overview.markerTextures.get(key);
  const { THREE } = state.overview;
  const canvas = document.createElement("canvas");
  canvas.width = 128;
  canvas.height = 128;
  const context = canvas.getContext("2d");
  context.translate(64, 64);
  if (selected) {
    context.strokeStyle = "#ffffff";
    context.lineWidth = 9;
    context.beginPath();
    context.arc(0, 0, 48, 0, Math.PI * 2);
    context.stroke();
  }
  context.shadowColor = "rgba(0, 0, 0, .95)";
  context.shadowBlur = 12;
  context.strokeStyle = STATUS_COLORS[status];
  context.fillStyle = status === "MANUAL_REVIEW" ? "rgba(5, 12, 9, .92)" : STATUS_COLORS[status];
  context.lineWidth = status === "MANUAL_REVIEW" ? 10 : 6;
  context.beginPath();
  context.arc(0, 0, 30, 0, Math.PI * 2);
  context.fill();
  context.stroke();
  if (status === "MANUAL_REVIEW") {
    context.shadowBlur = 0;
    context.lineWidth = 8;
    context.beginPath();
    context.moveTo(-13, -13);
    context.lineTo(13, 13);
    context.moveTo(13, -13);
    context.lineTo(-13, 13);
    context.stroke();
  }
  const texture = new THREE.CanvasTexture(canvas);
  if ("colorSpace" in texture) texture.colorSpace = THREE.SRGBColorSpace;
  texture.needsUpdate = true;
  state.overview.markerTextures.set(key, texture);
  return texture;
}

function overviewMarkerSource(record) {
  const plane = record.measurement_plane || record.best_review_plane;
  if (plane?.center_xyz) return plane.center_xyz;
  return [record.location.x, record.location.y, (record.local_ground_z_m || 0) + 1.3];
}

function buildOverviewMarkers() {
  const { THREE, scene } = state.overview;
  const layer = new THREE.Group();
  layer.name = "v31-tree-status-markers";
  scene.add(layer);
  state.overview.markerLayer = layer;
  state.records.forEach((record) => {
    const material = new THREE.SpriteMaterial({
      map: overviewMarkerTexture(record.status),
      transparent: true,
      depthTest: false,
      depthWrite: false,
    });
    const sprite = new THREE.Sprite(material);
    sprite.position.copy(overviewSourceToScene(overviewMarkerSource(record)));
    sprite.renderOrder = 20;
    sprite.userData.treeId = record.tree_id;
    sprite.userData.pixelWidth = 28;
    sprite.userData.pixelHeight = 28;
    layer.add(sprite);
    state.overview.markers.set(record.tree_id, { record, sprite });
    state.overview.markerHitTargets.push(sprite);
  });
  syncOverviewMarkerVisibility();
}

function syncOverviewMarkerVisibility() {
  if (!state.overview.markers.size) return;
  const visible = new Set(overviewVisibleRecords().map((record) => record.tree_id));
  state.overview.markers.forEach(({ sprite }, treeId) => {
    sprite.visible = visible.has(treeId);
  });
  if (state.overview.selectedLabel) {
    state.overview.selectedLabel.visible = visible.has(state.overview.selectedTreeId);
  }
}

function overviewFrameBox(box, direction, mode) {
  const { THREE, camera, controls } = state.overview;
  if (!THREE || !camera || !controls || !box || box.isEmpty()) return;
  const sphere = box.getBoundingSphere(new THREE.Sphere());
  const radius = Math.max(sphere.radius, 1.2);
  const verticalFov = THREE.MathUtils.degToRad(camera.fov);
  const fitDistance = radius / Math.sin(verticalFov / 2);
  camera.position.copy(sphere.center).add(direction.clone().normalize().multiplyScalar(fitDistance * 1.08));
  controls.target.copy(sphere.center);
  controls.minDistance = Math.max(0.25, radius * 0.025);
  controls.maxDistance = Math.max(80, radius * 12);
  controls.update();
  state.overview.frameMode = mode;
}

function overviewTreeBox() {
  const { THREE } = state.overview;
  if (!THREE || !state.overview.markers.size) return null;
  const box = new THREE.Box3();
  state.overview.markers.forEach(({ sprite }) => box.expandByPoint(sprite.position));
  return box.expandByScalar(1.8);
}

function frameOverviewTrees() {
  const { THREE } = state.overview;
  if (!THREE) return;
  overviewFrameBox(overviewTreeBox(), new THREE.Vector3(1.15, 0.92, 1.15), "TREES");
}

function frameOverviewCloud() {
  const { THREE, cloud } = state.overview;
  if (!THREE || !cloud?.geometry?.boundingBox) return;
  overviewFrameBox(cloud.geometry.boundingBox.clone(), new THREE.Vector3(1.2, 0.82, 1.2), "CLOUD");
}

function frameOverviewTop() {
  const { THREE, cloud } = state.overview;
  if (!THREE) return;
  const box = state.overview.frameMode === "CLOUD" ? cloud?.geometry?.boundingBox?.clone() : overviewTreeBox();
  overviewFrameBox(box, new THREE.Vector3(0, 1, 0.001), state.overview.frameMode);
}

function disposeOverviewSelectedLabel() {
  const label = state.overview.selectedLabel;
  if (!label) return;
  label.removeFromParent();
  label.material.map?.dispose();
  label.material.dispose();
  state.overview.selectedLabel = null;
}

function makeOverviewSelectedLabel(record, anchor) {
  const { THREE } = state.overview;
  const labelText = `${record.tree_id} · ${record.automatic_measurement ? "วัดได้" : "ยังวัดไม่ได้"}`;
  const canvas = document.createElement("canvas");
  const context = canvas.getContext("2d");
  context.font = "700 34px system-ui, sans-serif";
  canvas.width = Math.ceil(context.measureText(labelText).width + 48);
  canvas.height = 64;
  context.font = "700 34px system-ui, sans-serif";
  context.fillStyle = "rgba(3, 10, 7, .95)";
  context.strokeStyle = STATUS_COLORS[record.status];
  context.lineWidth = 4;
  context.beginPath();
  context.roundRect(2, 2, canvas.width - 4, canvas.height - 4, 12);
  context.fill();
  context.stroke();
  context.fillStyle = "#edf6ef";
  context.fillText(labelText, 24, 43);
  const texture = new THREE.CanvasTexture(canvas);
  if ("colorSpace" in texture) texture.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
    map: texture,
    transparent: true,
    depthTest: false,
    depthWrite: false,
  }));
  sprite.position.copy(anchor);
  sprite.renderOrder = 21;
  sprite.userData.anchor = anchor.clone();
  sprite.userData.pixelWidth = Math.min(260, canvas.width / 2);
  sprite.userData.pixelHeight = 32;
  sprite.userData.labelOffsetPixels = 34;
  return sprite;
}

function scaleOverviewSprites() {
  const { THREE, camera, renderer } = state.overview;
  if (!THREE || !camera || !renderer) return;
  const canvasHeight = Math.max(renderer.domElement.clientHeight, 1);
  const verticalFov = THREE.MathUtils.degToRad(camera.fov);
  const scaleSprite = (sprite) => {
    const anchor = sprite.userData.anchor || sprite.position;
    const distance = Math.max(camera.position.distanceTo(anchor), 0.1);
    const worldPerPixel = 2 * Math.tan(verticalFov / 2) * distance / canvasHeight;
    if (sprite.userData.anchor) {
      sprite.position.copy(anchor).addScaledVector(camera.up, worldPerPixel * sprite.userData.labelOffsetPixels);
    }
    sprite.scale.set(
      worldPerPixel * sprite.userData.pixelWidth,
      worldPerPixel * sprite.userData.pixelHeight,
      1,
    );
  };
  state.overview.markerHitTargets.forEach(scaleSprite);
  if (state.overview.selectedLabel) scaleSprite(state.overview.selectedLabel);
}

function drawOverview() {
  const { renderer, scene, camera, controls } = state.overview;
  if (!renderer || !scene || !camera || !controls) return;
  controls.update();
  scaleOverviewSprites();
  renderer.render(scene, camera);
}

function animateOverview() {
  if (state.overview.animationFrame) cancelAnimationFrame(state.overview.animationFrame);
  const tick = () => {
    drawOverview();
    state.overview.animationFrame = requestAnimationFrame(tick);
  };
  tick();
}

function updateOverviewSelection(record) {
  if (!record) return;
  state.overview.selectedTreeId = record.tree_id;
  $("overviewSelectedTree").textContent = `${record.tree_id} · ${record.automatic_measurement ? "วัดได้" : "ยังวัดไม่ได้"}`;
  if (record.automatic_measurement) {
    const pom = `${format(record.measurement_height_agl_m)} ม. AGL`;
    $("overviewSelectedDetail").textContent = `${record.status} · POM ${pom} · เส้นรอบวง ${format(record.circumference_cm)} ซม. · ยังไม่ field-verified`;
  } else {
    const reasons = (record.reason_codes || []).slice(0, 3).join(" · ") || "ต้องตรวจด้วยคน";
    $("overviewSelectedDetail").textContent = `MANUAL_REVIEW · ไม่ปล่อยตัวเลขอัตโนมัติ · ${reasons}`;
  }
  $("overviewOpenDetail").disabled = false;
  const marker = state.overview.markers.get(record.tree_id);
  if (marker) {
    state.overview.markers.forEach(({ record: item, sprite }) => {
      const selected = item.tree_id === record.tree_id;
      sprite.material.map = overviewMarkerTexture(item.status, selected);
      sprite.userData.pixelWidth = selected ? 36 : 28;
      sprite.userData.pixelHeight = selected ? 36 : 28;
      sprite.material.needsUpdate = true;
    });
    disposeOverviewSelectedLabel();
    state.overview.selectedLabel = makeOverviewSelectedLabel(record, marker.sprite.position);
    state.overview.markerLayer.add(state.overview.selectedLabel);
    syncOverviewMarkerVisibility();
  }
}

function overviewHitTest(event) {
  const { camera, raycaster, pointer, markerHitTargets } = state.overview;
  if (!camera || !raycaster || !pointer) return null;
  const rectangle = $("overviewCanvas").getBoundingClientRect();
  pointer.x = ((event.clientX - rectangle.left) / rectangle.width) * 2 - 1;
  pointer.y = -((event.clientY - rectangle.top) / rectangle.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const hit = raycaster.intersectObjects(markerHitTargets.filter((sprite) => sprite.visible), false)[0];
  return hit ? state.overview.markers.get(hit.object.userData.treeId)?.record || null : null;
}

function selectOverviewTree(record) {
  if (!record) return;
  $("statusFilter").value = "";
  $("detectionFilter").value = "";
  $("treeSearch").value = "";
  applyFilters(record.tree_id);
}

function overviewPointerDown(event) {
  if (!state.overview.renderer) return;
  state.overview.pointerStart = {
    pointerId: event.pointerId,
    x: event.clientX,
    y: event.clientY,
    moved: false,
  };
  event.currentTarget.classList.add("is-dragging");
}

function overviewPointerMove(event) {
  const start = state.overview.pointerStart;
  if (start?.pointerId === event.pointerId) {
    start.moved = start.moved || Math.hypot(event.clientX - start.x, event.clientY - start.y) > 5;
    return;
  }
  const record = overviewHitTest(event);
  event.currentTarget.title = record
    ? `${record.tree_id} · ${record.automatic_measurement ? "วัดได้" : "ยังวัดไม่ได้"} · ${record.status}`
    : "";
}

function overviewPointerUp(event) {
  const start = state.overview.pointerStart;
  if (!start || start.pointerId !== event.pointerId) return;
  event.currentTarget.classList.remove("is-dragging");
  state.overview.pointerStart = null;
  if (!start.moved) selectOverviewTree(overviewHitTest(event));
}

init().catch((error) => {
  $("headline").textContent = `โหลด V3.1 ไม่สำเร็จ: ${error.message}`;
  console.error(error);
});
