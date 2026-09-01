const $ = (id) => document.getElementById(id);
const state = {
  payload: null,
  queue: [],
  filtered: [],
  current: null,
  crop: null,
  view: { yaw: -0.55, pitch: 0.32, zoom: 190 },
  drag: null,
};

const STATUS_COLORS = { STANDARD_DBH: "#75bfff", ALTERNATIVE_POM: "#ffd166", MANUAL_REVIEW: "#ff915f" };
const QUALITY_LABELS = {
  axis_verticality: "แกนตั้งตรง",
  vertical_continuity: "ความต่อเนื่องแนวดิ่ง",
  circularity: "ความเป็นวงกลม",
  radius_stability: "รัศมีคงที่",
  angular_coverage: "arc coverage",
  fit_quality: "คุณภาพการ fit",
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[char]);
}
function format(value, digits = 2) { return value == null || value === "" ? "—" : Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : String(value); }
function option(select, value, label = value) { const node = document.createElement("option"); node.value = value; node.textContent = label; select.append(node); }
function record() { return state.current?.v3 || null; }
function chosenWindow() { return record()?.selected_window || record()?.best_review_window || null; }
function chosenPlane() { return record()?.measurement_plane || record()?.best_review_plane || null; }

async function init() {
  state.payload = await fetch("data/review_queue.json", { cache: "no-store" }).then((response) => {
    if (!response.ok) throw new Error(`V3 queue ${response.status}`);
    return response.json();
  });
  state.queue = state.payload.entries;
  renderSummary();
  [...new Set(state.queue.map((item) => item.identity_status))].sort().forEach((value) => option($("confidenceFilter"), value));
  bind();
  applyFilters(new URLSearchParams(location.search).get("tree"));
  resize();
}

function bind() {
  ["statusFilter", "confidenceFilter"].forEach((id) => $(id).addEventListener("change", () => applyFilters()));
  $("treeSearch").addEventListener("input", () => applyFilters());
  $("treeSelect").addEventListener("change", () => selectTree($("treeSelect").value));
  $("resetView").addEventListener("click", () => { state.view = { yaw: -0.55, pitch: 0.32, zoom: 190 }; drawCloud(); });
  $("topView").addEventListener("click", () => { state.view = { ...state.view, yaw: 0, pitch: Math.PI / 2 }; drawCloud(); });
  const canvas = $("cloudCanvas");
  canvas.addEventListener("pointerdown", pointerDown);
  canvas.addEventListener("pointermove", pointerMove);
  canvas.addEventListener("pointerup", pointerUp);
  canvas.addEventListener("pointercancel", pointerUp);
  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    state.view.zoom = Math.max(50, Math.min(650, state.view.zoom * Math.exp(-event.deltaY * .001)));
    drawCloud();
  }, { passive: false });
  window.addEventListener("resize", resize);
}

function renderSummary() {
  const summary = state.payload.summary;
  const counts = summary.status_counts;
  $("automaticCount").textContent = summary.automatic_measurement_count;
  $("standardCount").textContent = counts.STANDARD_DBH || 0;
  $("alternativeCount").textContent = counts.ALTERNATIVE_POM || 0;
  $("manualCount").textContent = counts.MANUAL_REVIEW || 0;
  const comparison = summary.v2_coverage_comparison;
  $("headline").textContent = `${summary.automatic_measurement_count} / ${summary.tree_count} Tree IDs มีผล V3 อัตโนมัติ · ยังไม่ field-verified`;
  $("coverageDelta").textContent = `เทียบ lane geometry เดิม: V2 ${comparison.v2_phase4_measurable_count} → V3 ${comparison.v3_automatic_count} ต้น (เพิ่มสุทธิ ${comparison.net_change_count >= 0 ? "+" : ""}${comparison.net_change_count}); เป็นการเทียบ coverage ไม่ใช่ accuracy`;
  $("statusLegend").innerHTML = '<span class="standard">STANDARD_DBH · 1.30 ม. ผ่านเกณฑ์</span><span class="alternative">ALTERNATIVE_POM · เลือกช่วงสะอาดด้านบน</span><span class="manual">MANUAL_REVIEW · หลักฐาน/identity ยังไม่พอ</span>';
}

function applyFilters(preferredTree = null) {
  const status = $("statusFilter").value;
  const confidence = $("confidenceFilter").value;
  const query = $("treeSearch").value.trim().toUpperCase();
  state.filtered = state.queue.filter((item) =>
    (!status || item.measurement_status === status)
    && (!confidence || item.identity_status === confidence)
    && (!query || item.review_item_id.includes(query))
  );
  const select = $("treeSelect");
  const prior = preferredTree || state.current?.review_item_id;
  select.innerHTML = "";
  state.filtered.forEach((item) => option(select, item.review_item_id, `${item.review_item_id} · ${item.measurement_status}`));
  $("filterCount").textContent = `${state.filtered.length} / ${state.queue.length} Tree IDs`;
  const next = state.filtered.some((item) => item.review_item_id === prior) ? prior : state.filtered[0]?.review_item_id;
  if (next) { select.value = next; selectTree(next); }
  else { state.current = null; state.crop = null; render(); }
}

async function selectTree(treeId) {
  state.current = state.queue.find((item) => item.review_item_id === treeId) || null;
  state.crop = null;
  state.view = { yaw: -0.55, pitch: 0.32, zoom: 190 };
  $("treeSelect").value = treeId;
  history.replaceState(null, "", `${location.pathname}?tree=${encodeURIComponent(treeId)}`);
  render();
  if (state.current?.point_crop_url) {
    try {
      state.crop = await fetch(state.current.point_crop_url, { cache: "no-store" }).then((response) => {
        if (!response.ok) throw new Error(String(response.status));
        return response.json();
      });
    } catch (error) {
      state.crop = { sampled_points_xyz: [], error: error.message };
    }
  }
  render();
}

function render() {
  renderMetrics();
  renderQuality();
  renderWindows();
  renderProvenance();
  drawCloud();
  drawProfile();
  drawCross();
}

function renderMetrics() {
  const item = record();
  if (!item) { $("treeTitle").textContent = "—"; $("treeState").textContent = ""; $("measurementMetrics").innerHTML = ""; return; }
  $("treeTitle").textContent = item.tree_id;
  $("treeState").textContent = `${item.status} · ${item.confidence_label}`;
  const axis = item.local_axis || {};
  const rows = [
    ["สถานะ", item.status],
    ["POM", item.measurement_height_agl_m == null ? `candidate ${format(item.candidate_height_agl_m)} ม.` : `${format(item.measurement_height_agl_m)} ม. AGL`],
    ["เส้นผ่านศูนย์กลาง", item.diameter_cm == null ? "—" : `${format(item.diameter_cm)} ซม.`],
    ["DBH", item.dbh_cm == null ? "ไม่ใช้คำว่า DBH" : `${format(item.dbh_cm)} ซม.`],
    ["เส้นรอบวง", item.circumference_cm == null ? "—" : `${format(item.circumference_cm)} ซม.`],
    ["Local ground", `${format(item.local_ground_z_m, 3)} ม.`],
    ["แกนเอียงจากแนวดิ่ง", `${format(axis.inclination_deg)}°`],
    ["Fit RMSE", `${format(item.fit_rmse_m, 4)} ม.`],
    ["Fit source", item.source_slice_orientation],
    ["Marking plane", item.measurement_plane_orientation],
    ["Circularity", format(item.circularity, 3)],
    ["Arc coverage", `${format(item.arc_coverage_deg, 0)}°`],
    ["Radius MAD", `${format(item.radius_stability_mad_m, 4)} ม.`],
    ["Slices / points", `${item.supporting_slice_count ?? "—"} / ${item.point_count ?? "—"}`],
    ["Field verified", "NO"],
  ];
  $("measurementMetrics").innerHTML = rows.map(([label, value]) => `<div class="metric-row"><span>${escapeHtml(label)}</span><span>${escapeHtml(value)}</span></div>`).join("");
}

function renderQuality() {
  const item = record();
  const score = item?.quality_score;
  const ring = $("qualityScore");
  ring.textContent = score == null ? "—" : `${format(score, 1)}`;
  ring.style.borderColor = score == null ? "#294638" : score >= 80 ? "#71e3a0" : score >= 65 ? "#ffd166" : "#ff915f";
  const components = item?.quality_components || {};
  $("qualityBars").innerHTML = Object.entries(QUALITY_LABELS).map(([key, label]) => {
    const value = Number(components[key] || 0);
    return `<div class="quality-row"><span>${escapeHtml(label)}</span><div class="quality-track"><div class="quality-fill" style="width:${Math.round(value * 100)}%"></div></div><strong>${Math.round(value * 100)}</strong></div>`;
  }).join("");
  $("reasonCodes").innerHTML = (item?.reason_codes || []).map((reason) => `<span class="chip">${escapeHtml(reason)}</span>`).join("");
}

function renderWindows() {
  const item = record();
  const selected = chosenWindow();
  const status = item?.status;
  const windows = state.current?.scored_windows || [];
  $("windowRows").innerHTML = windows.map((window) => {
    const atStandard = window.start_height_m <= 1.3 && window.end_height_m >= 1.3;
    const decisionReason = atStandard ? window.standard_decision : window.alternative_decision;
    const failed = decisionReason !== "PASS";
    const chosen = selected && window.source_candidate_id === selected.source_candidate_id && Math.abs(window.center_height_m - selected.center_height_m) < .001;
    const decision = chosen ? (status === "MANUAL_REVIEW" ? "BEST · REVIEW" : "SELECTED") : decisionReason;
    const crossLane = window.cross_lane_relative_diameter_difference == null ? "—" : `${format(window.cross_lane_relative_diameter_difference * 100, 0)}%`;
    return `<tr class="${chosen ? "selected" : failed ? "failed" : ""}"><td>${format(window.center_height_m)}</td><td>${format(window.start_height_m)}–${format(window.end_height_m)}</td><td>${format(window.quality_score, 1)}</td><td>${window.supporting_slice_count}/${window.expected_slice_count}</td><td>${format(window.radius_m, 3)}</td><td>${format(window.angular_coverage_deg, 0)}°</td><td>${format(window.circularity, 3)}</td><td>${format(window.radius_mad_m, 4)}</td><td>${format(window.fit_rmse_m, 4)}</td><td>${format(window.inclination_deg, 1)}°</td><td>${crossLane}</td><td class="${failed ? "decision-fail" : "decision-ok"}">${escapeHtml(decision)}</td></tr>`;
  }).join("") || '<tr><td colspan="12" class="muted">ไม่มี stable-window evidence ที่เผยแพร่สำหรับ Tree ID นี้</td></tr>';
}

function renderProvenance() {
  const item = record();
  if (!item) { $("provenance").innerHTML = ""; return; }
  const source = state.payload.summary.source;
  const baseline = item.v2_baseline || {};
  $("provenance").innerHTML = `<div class="provenance-grid">
    <div><strong>V3 source</strong><span class="muted">Preserved sampled Phase 1.5 multi-height geometry · candidate ${escapeHtml(item.source_candidate_id || "none")} · track ${escapeHtml(item.source_track_id || "none")}</span></div>
    <div><strong>V2 context (read-only)</strong><span class="muted">Phase 4: ${escapeHtml(baseline.phase4_measurement_status || "—")} · field-aid: ${escapeHtml(baseline.current_field_aid_status || "—")}</span></div>
    <div><strong>Height limit</strong><span class="muted">ขอค้นถึง ${format(source.requested_maximum_height_m)} ม. แต่หลักฐานมีถึง ${format(source.published_evidence_maximum_height_m)} ม.; robust-window center สูงสุด ${format(source.maximum_robust_window_center_m)} ม.</span></div>
    <div><strong>Measurement semantics</strong><span class="muted">STANDARD_DBH เท่านั้นที่ใช้คำว่า DBH; ALTERNATIVE_POM รายงาน diameter/circumference ที่ความสูงจริง</span></div>
    <div><strong>Confidence</strong><span class="muted">${escapeHtml(item.confidence_label)} เป็น geometry QA label แบบ deterministic และยังไม่ calibrated</span></div>
    <div><strong>Geometry limit</strong><span class="muted">รัศมีมาจาก published horizontal stable-window; ระนาบตั้งฉากใช้สำหรับ marking/debug และยังไม่ได้ full-resolution perpendicular refit เพราะ raw LAS ไม่อยู่ใน repo</span></div>
  </div>`;
}

function resizeCanvas(canvas) {
  const ratio = devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.floor(rect.width * ratio));
  canvas.height = Math.max(1, Math.floor(rect.height * ratio));
  canvas.getContext("2d").setTransform(ratio, 0, 0, ratio, 0, 0);
}
function resize() { ["cloudCanvas", "profileCanvas", "crossCanvas"].forEach((id) => resizeCanvas($(id))); render(); }

function cloudCenter() {
  const item = record();
  return item ? [item.location.x || 0, item.location.y || 0, (item.local_ground_z_m || 0) + 2] : [0, 0, 0];
}
function project(point, canvas) {
  const center = cloudCenter();
  const x = point[0] - center[0], y = point[1] - center[1], z = point[2] - center[2];
  const cy = Math.cos(state.view.yaw), sy = Math.sin(state.view.yaw), cp = Math.cos(state.view.pitch), sp = Math.sin(state.view.pitch);
  const xr = cy * x - sy * y, yr = sy * x + cy * y;
  return [canvas.clientWidth / 2 + xr * state.view.zoom, canvas.clientHeight / 2 - (yr * sp + z * cp) * state.view.zoom, yr * cp - z * sp];
}
function drawPoints(ctx, canvas, points) {
  ctx.fillStyle = "#84978b"; ctx.globalAlpha = .58;
  [...points].map((point) => [project(point, canvas), point]).sort((a, b) => a[0][2] - b[0][2]).forEach(([screen]) => ctx.fillRect(screen[0] - .7, screen[1] - .7, 1.4, 1.4));
  ctx.globalAlpha = 1;
}
function drawPolyline(ctx, canvas, points, color, width = 2, dashed = false) {
  if (!points?.length) return;
  ctx.strokeStyle = color; ctx.lineWidth = width; ctx.setLineDash(dashed ? [7, 5] : []); ctx.beginPath();
  points.forEach((point, index) => { const screen = project(point, canvas); index ? ctx.lineTo(screen[0], screen[1]) : ctx.moveTo(screen[0], screen[1]); });
  ctx.stroke(); ctx.setLineDash([]);
}
function planePoint(plane, u, v) {
  return plane.center_xyz.map((center, index) => center + plane.basis_u[index] * u + plane.basis_v[index] * v);
}
function drawOrientedPlane(ctx, canvas, plane, color, label, dashed = true) {
  if (!plane) return;
  const extent = Math.max(.38, Number(chosenWindow()?.radius_m || 0) * 2.6);
  const corners = [[-1, -1], [1, -1], [1, 1], [-1, 1], [-1, -1]].map(([u, v]) => planePoint(plane, u * extent, v * extent));
  drawPolyline(ctx, canvas, corners, color, 1.6, dashed);
  const text = project(corners[2], canvas); ctx.fillStyle = color; ctx.font = "11px system-ui"; ctx.fillText(label, text[0] + 4, text[1] - 4);
}
function drawFitCircle3d(ctx, canvas, plane, radius, color, dashed = false) {
  if (!plane || radius == null) return;
  const points = Array.from({ length: 65 }, (_, index) => {
    const angle = Math.PI * 2 * index / 64;
    return planePoint(plane, Math.cos(angle) * radius, Math.sin(angle) * radius);
  });
  drawPolyline(ctx, canvas, points, color, 2.2, dashed);
}
function drawReferencePlane(ctx, canvas, item) {
  if (item.local_ground_z_m == null) return;
  const d = .55, z = item.local_ground_z_m + 1.3, x = item.location.x, y = item.location.y;
  drawPolyline(ctx, canvas, [[x-d,y-d,z],[x+d,y-d,z],[x+d,y+d,z],[x-d,y+d,z],[x-d,y-d,z]], "#75bfff", 1, true);
}

function drawCloud() {
  const canvas = $("cloudCanvas"), ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight); ctx.fillStyle = "#06100c"; ctx.fillRect(0, 0, canvas.clientWidth, canvas.clientHeight);
  const item = record();
  if (!item) return;
  const points = state.crop?.sampled_points_xyz || [];
  drawPoints(ctx, canvas, points);
  drawReferencePlane(ctx, canvas, item);
  const plane = chosenPlane(), window = chosenWindow(), manual = item.status === "MANUAL_REVIEW";
  if (plane) {
    const axis = plane.axis_direction;
    drawPolyline(ctx, canvas, [plane.center_xyz.map((v, i) => v - axis[i] * 1.15), plane.center_xyz.map((v, i) => v + axis[i] * 1.15)], "#71e3a0", 2.6);
    drawOrientedPlane(ctx, canvas, plane, manual ? "#ff915f" : "#ffd166", manual ? "best candidate · manual review" : `selected POM ${format(item.measurement_height_agl_m)} m`);
    drawFitCircle3d(ctx, canvas, plane, window?.radius_m, manual ? "#ff915f" : "#ffffff", manual);
  }
  ctx.fillStyle = manual ? "#ff915f" : "#9caf9f"; ctx.font = "bold 12px system-ui";
  ctx.fillText(manual ? "No automatic measurement · candidate geometry only" : "LiDAR screening estimate · not field verified", 18, canvas.clientHeight - 44);
  const counts = state.crop?.counts_before_display_sampling;
  $("cloudStatus").textContent = state.crop?.error ? `โหลด crop ไม่สำเร็จ: ${state.crop.error}` : counts ? `แสดง ${Number(counts.sampled || points.length).toLocaleString()} sampled points` : points.length ? `แสดง ${points.length.toLocaleString()} sampled points` : "ไม่มี published point crop สำหรับ Tree ID นี้";
}

function drawProfile() {
  const canvas = $("profileCanvas"), ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
  const observations = (state.current?.track?.observations || []).filter((row) => row.radius_m != null && row.source_height_m != null).sort((a, b) => a.source_height_m - b.source_height_m);
  if (!observations.length) { ctx.fillStyle = "#9caf9f"; ctx.fillText("ไม่มี radius observations ที่เผยแพร่", 16, 28); return; }
  const pad = 34, width = canvas.clientWidth - 2 * pad, height = canvas.clientHeight - 2 * pad;
  const minH = Math.min(.9, ...observations.map((row) => row.source_height_m)) - .1;
  const maxH = Math.max(3.5, ...observations.map((row) => row.source_height_m)) + .1;
  const maxR = Math.max(.05, ...observations.map((row) => row.radius_m)) * 1.18;
  const xFor = (radius) => pad + radius / maxR * width, yFor = (h) => pad + (maxH - h) / (maxH - minH) * height;
  ctx.strokeStyle = "#355746"; ctx.strokeRect(pad, pad, width, height);
  const window = chosenWindow();
  if (window) { ctx.fillStyle = record()?.status === "MANUAL_REVIEW" ? "rgba(255,145,95,.12)" : "rgba(255,209,102,.12)"; ctx.fillRect(pad, yFor(window.end_height_m), width, yFor(window.start_height_m) - yFor(window.end_height_m)); }
  ctx.strokeStyle = "#75bfff"; ctx.setLineDash([4, 3]); ctx.beginPath(); ctx.moveTo(pad, yFor(1.3)); ctx.lineTo(pad + width, yFor(1.3)); ctx.stroke(); ctx.setLineDash([]);
  ctx.strokeStyle = "#ffd166"; ctx.fillStyle = "#ffd166"; ctx.beginPath();
  observations.forEach((row, index) => { const x = xFor(row.radius_m), y = yFor(row.source_height_m); index ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }); ctx.stroke();
  observations.forEach((row) => { ctx.beginPath(); ctx.arc(xFor(row.radius_m), yFor(row.source_height_m), 3, 0, Math.PI * 2); ctx.fill(); });
  ctx.fillStyle = "#9caf9f"; ctx.font = "10px system-ui"; ctx.fillText(`${minH.toFixed(1)} m`, 2, pad + height); ctx.fillText(`${maxH.toFixed(1)} m`, 2, pad + 8); ctx.fillText(`${maxR.toFixed(2)} m radius`, pad + 4, 12);
}

function dot(a, b) { return a.reduce((sum, value, index) => sum + value * b[index], 0); }
function drawCross() {
  const canvas = $("crossCanvas"), ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
  const plane = chosenPlane(), window = chosenWindow(), points = state.crop?.sampled_points_xyz || [];
  if (!plane || !window) { ctx.fillStyle = "#9caf9f"; ctx.fillText("ไม่มี POM candidate plane สำหรับหน้าตัด", 16, 28); $("crossNote").textContent = ""; return; }
  const projected = points.map((point) => {
    const relative = point.map((value, index) => value - plane.center_xyz[index]);
    return { distance: dot(relative, plane.axis_direction), u: dot(relative, plane.basis_u), v: dot(relative, plane.basis_v) };
  }).filter((point) => Math.abs(point.distance) <= .06);
  const pad = 25, width = canvas.clientWidth - 2 * pad, height = canvas.clientHeight - 2 * pad;
  const extent = Math.max(.18, Number(window.radius_m || 0) * 2.8, ...projected.flatMap((point) => [Math.abs(point.u), Math.abs(point.v)]).filter((value) => value < 1.0));
  const scale = Math.min(width, height) / (2 * extent), xFor = (u) => canvas.clientWidth / 2 + u * scale, yFor = (v) => canvas.clientHeight / 2 - v * scale;
  ctx.strokeStyle = "#355746"; ctx.beginPath(); ctx.moveTo(xFor(-extent), yFor(0)); ctx.lineTo(xFor(extent), yFor(0)); ctx.moveTo(xFor(0), yFor(-extent)); ctx.lineTo(xFor(0), yFor(extent)); ctx.stroke();
  ctx.fillStyle = "#8fa298"; ctx.globalAlpha = .72; projected.forEach((point) => ctx.fillRect(xFor(point.u) - 1.2, yFor(point.v) - 1.2, 2.4, 2.4)); ctx.globalAlpha = 1;
  ctx.strokeStyle = record()?.status === "MANUAL_REVIEW" ? "#ff915f" : "#ffd166"; ctx.lineWidth = 2; ctx.setLineDash(record()?.status === "MANUAL_REVIEW" ? [6, 4] : []); ctx.beginPath(); ctx.arc(xFor(0), yFor(0), window.radius_m * scale, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([]);
  ctx.fillStyle = "#9caf9f"; ctx.font = "10px system-ui"; ctx.fillText(`±${extent.toFixed(2)} m`, 6, 13);
  $("crossNote").textContent = `${projected.length} sampled crop points ภายใน slab ตั้งฉาก ±0.06 ม.; วงคือรัศมี screening จาก published horizontal stable-window ${format(window.radius_m, 3)} ม.${record()?.status === "MANUAL_REVIEW" ? " (candidate เท่านั้น)" : ""}`;
}

function pointerDown(event) { const canvas = $("cloudCanvas"); canvas.setPointerCapture(event.pointerId); state.drag = { x: event.clientX, y: event.clientY }; }
function pointerMove(event) { if (!state.drag) return; const dx = event.clientX - state.drag.x, dy = event.clientY - state.drag.y; state.view.yaw += dx * .008; state.view.pitch = Math.max(-1.45, Math.min(1.57, state.view.pitch + dy * .006)); state.drag = { x: event.clientX, y: event.clientY }; drawCloud(); }
function pointerUp() { state.drag = null; }

init().catch((error) => { $("headline").textContent = `โหลด V3 ไม่สำเร็จ: ${error.message}`; });
