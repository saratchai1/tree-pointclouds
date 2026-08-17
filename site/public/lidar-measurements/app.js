"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  records: [], filtered: [], current: null, marking: null,
  yaw: -0.65, pitch: 0.26, zoom: 1, dragging: false, lastX: 0, lastY: 0,
};

const fmt = (value, digits = 2) => value == null ? "—" : Number(value).toFixed(digits);
const effectiveCircumference = (row) => row.field_aid_circumference_cm;
const effectiveDiameter = (row) => row.field_aid_diameter_cm;

function fieldAidSourceText(row) {
  if (row.field_aid_source === "HUMAN_OPERATOR_EXCLUSION") return "ตัดออกตามคำยืนยันของผู้ใช้";
  if (row.field_aid_source === "CURRENT_PROTOCOL_FINAL") return "Full‑LAS · ยืนยัน protocol ล่าสุด";
  if (row.field_aid_source === "LEGACY_FULL_RESOLUTION_ACCEPTED") return "Full‑LAS รุ่นแรก · ผลที่รับไว้เดิม";
  if (row.field_aid_source === "FULL_LAS_GEOMETRY_ESTIMATE") return "Full‑LAS · วง fit เรขาคณิตสะอาด";
  if (row.field_aid_source === "FULL_LAS_REVIEW_CANDIDATE") return "Full‑LAS · ค่าประมาณสำหรับเช็กหน้างาน";
  return "ยังไม่มีวง fit ที่ใช้ได้";
}

function statusText(row) {
  if (row.field_aid_status === "EXCLUDED_CONFIRMED_WRONG") return "ตัดออก · ผู้ใช้ยืนยันว่าผิด";
  if (row.field_aid_source === "CURRENT_PROTOCOL_FINAL") return "พร้อมใช้ · ยืนยัน protocol ล่าสุด";
  if (row.field_aid_source === "LEGACY_FULL_RESOLUTION_ACCEPTED") return "พร้อมใช้ · Full‑LAS รุ่นแรก";
  if (row.field_aid_status === "READY_FOR_FIELD_USE") return "พร้อมใช้ช่วยภาคสนาม";
  if (row.field_aid_status === "CHECK_ON_SITE") return "มีค่าประมาณ · ควรเช็กหน้างาน";
  return "ข้อมูลไม่พอสำหรับวง fit";
}

function protocolText(row) {
  if (row.measurement_kind === "STANDARD_DBH_1_30") return "Standard · DBH ที่ 1.30 ม.";
  if (row.measurement_kind === "PROP_ROOT_PLUS_030") return "Prop-root · จุดเกาะราก +0.30 ม.";
  if (row.measurement_kind === "LEGACY_STANDARD_DBH_1_30") return "Full‑LAS รุ่นแรก · Standard 1.30 ม.";
  if (row.measurement_kind === "LEGACY_ADAPTIVE_IRREGULAR_ZONE_PLUS_030") return "Full‑LAS รุ่นแรก · ยอดโซนผิดปกติ +0.30 ม.";
  return "Screening ที่ 1.30 ม. · ยังไม่ยืนยันโปรโตคอล";
}

function matchesFilter(row, filter) {
  if (filter === "ALL") return true;
  if (filter === "FIELD_READY") return row.field_aid_status === "READY_FOR_FIELD_USE";
  if (filter === "PROP_ROOT") return row.measurement_kind === "PROP_ROOT_PLUS_030" && !row.operationally_excluded;
  if (filter === "LEGACY") return row.legacy_full_resolution_status === "ACCEPTED" && !row.operationally_excluded;
  if (filter === "LEGACY_STANDARD") return row.legacy_measurement_rule === "STANDARD_1_30" && !row.operationally_excluded;
  if (filter === "LEGACY_ADAPTIVE") return row.legacy_measurement_rule === "ADAPTIVE_IRREGULAR_ZONE_PLUS_030" && !row.operationally_excluded;
  if (filter === "FINAL") return row.acceptance_status === "FINAL_LIDAR_ESTIMATE";
  if (filter === "CHECK") return row.field_aid_status === "CHECK_ON_SITE";
  if (filter === "HAS_ESTIMATE") return row.field_aid_circumference_cm != null && !row.operationally_excluded;
  if (filter === "NO_ESTIMATE") return row.field_aid_status === "NO_ESTIMATE";
  if (filter === "EXCLUDED") return row.field_aid_status === "EXCLUDED_CONFIRMED_WRONG";
  return true;
}

function updateList() {
  const query = $("searchInput").value.trim().toUpperCase();
  const filter = $("filterSelect").value;
  state.filtered = state.records.filter((row) => matchesFilter(row, filter) && row.tree_id.includes(query));
  $("listCount").textContent = `${state.filtered.length} / ${state.records.length}`;
  const list = $("treeList");
  list.replaceChildren();
  for (const row of state.filtered) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `tree-item${state.current?.tree_id === row.tree_id ? " active" : ""}`;
    button.setAttribute("role", "option");
    button.setAttribute("aria-selected", state.current?.tree_id === row.tree_id ? "true" : "false");
    const dotClass = row.field_aid_status === "EXCLUDED_CONFIRMED_WRONG"
      ? "fail"
      : row.field_aid_source === "LEGACY_FULL_RESOLUTION_ACCEPTED"
      ? "legacy"
      : row.field_aid_status === "READY_FOR_FIELD_USE"
        ? "final"
        : row.field_aid_status === "NO_ESTIMATE" ? "fail" : "";
    button.innerHTML = `<strong>${row.tree_id}</strong><span class="mini-status ${dotClass}"></span><small>${statusText(row)}</small>`;
    button.addEventListener("click", () => selectTree(row));
    list.append(button);
  }
}

async function selectTree(row) {
  state.current = row;
  state.marking = null;
  updateList();
  renderRecord();
  $("loading").classList.remove("hidden");
  try {
    const response = await fetch(`/${row.marking_url}`, {cache: "no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.marking = await response.json();
    drawAll();
    history.replaceState(null, "", `?tree=${row.tree_id}`);
  } catch (error) {
    $("qaReasons").insertAdjacentHTML("beforeend", `<span class="reason">โหลด marking ไม่สำเร็จ: ${error.message}</span>`);
  } finally {
    $("loading").classList.add("hidden");
  }
}

function renderRecord() {
  const row = state.current;
  if (!row) return;
  $("treeTitle").textContent = row.tree_id;
  $("treeSubtitle").textContent = `${protocolText(row)} · ${row.detection_status} · ${row.identity_review_status}`;
  $("statusBadge").textContent = statusText(row);
  $("statusBadge").className = `status-badge ${row.field_aid_status === "READY_FOR_FIELD_USE" ? "" : row.field_aid_status === "CHECK_ON_SITE" ? "provisional" : "review"}`;
  const circ = effectiveCircumference(row);
  const diameter = effectiveDiameter(row);
  $("circumferenceValue").textContent = circ == null ? "—" : `${fmt(circ)} cm`;
  $("circumferenceNote").textContent = fieldAidSourceText(row);
  const isAdaptive = row.measurement_kind === "PROP_ROOT_PLUS_030" || row.legacy_measurement_rule === "ADAPTIVE_IRREGULAR_ZONE_PLUS_030";
  $("diameterLabel").textContent = row.field_aid_dbh_cm != null ? "DBH estimate" : isAdaptive ? "เส้นผ่านศูนย์กลาง ณ +0.30" : "เส้นผ่านศูนย์กลาง ณ ระนาบ";
  $("diameterValue").textContent = diameter == null ? "—" : `${fmt(diameter)} cm`;
  $("diameterNote").textContent = row.field_aid_dbh_cm != null ? "วัดที่ 1.30 ม." : isAdaptive ? "ไม่ใช่ DBH ที่ 1.30 ม." : "เส้นผ่านศูนย์กลางที่ระนาบ marking";
  $("heightValue").textContent = fmt(row.field_aid_measurement_height_agl_m, 3);
  $("coverageValue").textContent = row.angular_coverage_deg == null ? "—" : fmt(row.angular_coverage_deg, 0);
  const details = [
    ["โปรโตคอล", protocolText(row)],
    ["สถานะช่วยภาคสนาม", row.field_aid_status],
    ["คำตัดออก", row.operational_exclusion_decision || "ไม่มี"],
    ["แหล่งค่าที่แสดง", fieldAidSourceText(row)],
    ["ผล Full‑LAS รุ่นแรก", row.legacy_circumference_cm == null ? "ไม่มี" : `${fmt(row.legacy_circumference_cm)} cm @ ${fmt(row.legacy_measurement_height_agl_m, 3)} m`],
    ["วง fit รอบล่าสุด", row.candidate_circumference_cm == null ? "ไม่มี" : `${fmt(row.candidate_circumference_cm)} cm`],
    ["Marking ที่แสดง", row.field_aid_marking_source],
    ["สถานะเรขาคณิต", row.geometric_status],
    ["โมเดลที่เลือก", row.fit_model || "ไม่มี fit"],
    ["จุดในหน้าตัด", row.point_count.toLocaleString()],
    ["จุดที่รับเข้า fit", row.inlier_count.toLocaleString()],
    ["แกนลำต้น", row.axis_source],
    ["slice รองรับแกน", String(row.axis_supporting_slice_count)],
    ["ย้ายระนาบหรือไม่", row.protocol_plane_moved_to_cleaner_height ? "ย้าย" : "ไม่ย้าย"],
    ["ตรวจภาคสนาม", row.field_verified ? "ตรวจแล้ว" : "ยังไม่ได้ตรวจ"],
  ];
  $("measurementDetails").innerHTML = details.map(([key, value]) => `<dt>${key}</dt><dd>${value}</dd>`).join("");
  const legacyTag = row.legacy_full_resolution_status === "ACCEPTED"
    ? `<span class="reason pass">LEGACY_FULL_RESOLUTION_ACCEPTED</span>` : "";
  $("qaReasons").innerHTML = legacyTag + (row.qa_reason_codes.length
    ? row.qa_reason_codes.map((reason) => `<span class="reason">${reason}</span>`).join("")
    : `<span class="reason pass">วง fit เรขาคณิตสะอาด</span>`);
  drawAll();
}

function sizeCanvas(canvas) {
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(1, Math.round(rect.width * ratio));
  const height = Math.max(1, Math.round(rect.height * ratio));
  if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  return {ctx, width: rect.width, height: rect.height};
}

function rotatePoint(point, origin) {
  const x = point[0] - origin[0], y = point[1] - origin[1], z = point[2] - origin[2];
  const cy = Math.cos(state.yaw), sy = Math.sin(state.yaw);
  const cp = Math.cos(state.pitch), sp = Math.sin(state.pitch);
  const x1 = cy * x - sy * y;
  const y1 = sy * x + cy * y;
  return [x1, cp * z - sp * y1, sp * z + cp * y1];
}

function fitOutline3d(marking, samples = 96) {
  const fit = marking.field_aid_fit || marking.fit;
  if (!fit) return [];
  const plane = marking.measurement_plane;
  const model = marking.field_aid_fit_model || state.current.field_aid_fit_model || state.current.fit_model;
  const ellipse = fit.ellipse || {};
  const points = [];
  for (let i = 0; i <= samples; i++) {
    const angle = Math.PI * 2 * i / samples;
    let u, v;
    if (model === "ELLIPSE" && ellipse.valid) {
      const cx = ellipse.center[0], cy = ellipse.center[1], rot = ellipse.rotation_rad;
      const a = ellipse.semi_major_axis_m, b = ellipse.semi_minor_axis_m;
      u = cx + a * Math.cos(angle) * Math.cos(rot) - b * Math.sin(angle) * Math.sin(rot);
      v = cy + a * Math.cos(angle) * Math.sin(rot) + b * Math.sin(angle) * Math.cos(rot);
    } else {
      u = fit.center[0] + fit.radius_m * Math.cos(angle);
      v = fit.center[1] + fit.radius_m * Math.sin(angle);
    }
    points.push([0,1,2].map((k) => plane.center_xyz[k] + plane.basis_u[k] * u + plane.basis_v[k] * v));
  }
  return points;
}

function drawCloud() {
  const {ctx, width, height} = sizeCanvas($("cloudCanvas"));
  ctx.clearRect(0, 0, width, height);
  if (!state.marking) return;
  const marking = state.marking;
  const origin = marking.measurement_plane.center_xyz;
  const all = marking.display_points_xyz.map((p) => rotatePoint(p, origin));
  const maxRange = Math.max(.35, ...all.map((p) => Math.max(Math.abs(p[0]), Math.abs(p[1]))));
  const scale = Math.min(width, height) * .43 / maxRange * state.zoom;
  const project = (point) => {
    const p = rotatePoint(point, origin);
    return [width / 2 + p[0] * scale, height / 2 - p[1] * scale, p[2]];
  };
  const sorted = marking.display_points_xyz.map((p) => [...project(p), p]).sort((a,b) => a[2]-b[2]);
  ctx.fillStyle = "rgba(132,160,145,.32)";
  for (const p of sorted) ctx.fillRect(p[0], p[1], 1.25, 1.25);
  ctx.fillStyle = "rgba(111,224,167,.78)";
  for (const point of marking.accepted_slice_points_xyz) {
    const p = project(point); ctx.fillRect(p[0]-1, p[1]-1, 2.2, 2.2);
  }
  const plane = marking.measurement_plane;
  const planeRadius = .34;
  const planePoints = [];
  for (let i=0;i<=80;i++) {
    const a = Math.PI*2*i/80;
    planePoints.push([0,1,2].map((k) => plane.center_xyz[k] + plane.basis_u[k]*planeRadius*Math.cos(a) + plane.basis_v[k]*planeRadius*Math.sin(a)));
  }
  drawPath(ctx, planePoints.map(project), "rgba(255,189,102,.85)", 1.4, [5,4]);
  drawPath(ctx, fitOutline3d(marking).map(project), "#72d8ff", 2.3, []);
  const axisA = [0,1,2].map((k) => origin[k] - plane.axis_direction[k]*.45);
  const axisB = [0,1,2].map((k) => origin[k] + plane.axis_direction[k]*.45);
  drawPath(ctx, [project(axisA), project(axisB)], "rgba(111,224,167,.75)", 1.5, [7,4]);
  ctx.fillStyle = "rgba(233,244,237,.7)"; ctx.font = "12px system-ui";
  ctx.fillText(`${state.current.tree_id} · plane ${fmt(state.current.measurement_height_agl_m,3)} m`, 14, 22);
}

function drawPath(ctx, points, color, width, dash) {
  if (!points.length) return;
  ctx.save(); ctx.strokeStyle = color; ctx.lineWidth = width; ctx.setLineDash(dash); ctx.beginPath();
  points.forEach((p,i) => i ? ctx.lineTo(p[0],p[1]) : ctx.moveTo(p[0],p[1]));
  ctx.stroke(); ctx.restore();
}

function outline2d(fit, model, samples=120) {
  if (!fit) return [];
  const ellipse = fit.ellipse || {};
  return Array.from({length:samples+1},(_,i) => {
    const t=Math.PI*2*i/samples;
    if (model === "ELLIPSE" && ellipse.valid) {
      const c=Math.cos(ellipse.rotation_rad),s=Math.sin(ellipse.rotation_rad);
      return [ellipse.center[0]+ellipse.semi_major_axis_m*Math.cos(t)*c-ellipse.semi_minor_axis_m*Math.sin(t)*s,
        ellipse.center[1]+ellipse.semi_major_axis_m*Math.cos(t)*s+ellipse.semi_minor_axis_m*Math.sin(t)*c];
    }
    return [fit.center[0]+fit.radius_m*Math.cos(t),fit.center[1]+fit.radius_m*Math.sin(t)];
  });
}

function drawCross() {
  const {ctx,width,height}=sizeCanvas($("crossCanvas")); ctx.clearRect(0,0,width,height);
  if (!state.marking) return;
  const accepted=state.marking.accepted_projected_points_xy, rejected=state.marking.rejected_projected_points_xy;
  const activeFit=state.marking.field_aid_fit||state.marking.fit;
  const activeModel=state.marking.field_aid_fit_model||state.current.field_aid_fit_model||state.current.fit_model;
  const outline=outline2d(activeFit,activeModel);
  const all=[...accepted,...rejected,...outline];
  const range=Math.max(.12,...all.flatMap((p)=>[Math.abs(p[0]),Math.abs(p[1])]))*1.12;
  const scale=Math.min(width,height)*.45/range;
  const project=(p)=>[width/2+p[0]*scale,height/2-p[1]*scale];
  ctx.strokeStyle="rgba(105,139,122,.25)";ctx.lineWidth=1;ctx.setLineDash([3,4]);
  ctx.beginPath();ctx.moveTo(width/2,18);ctx.lineTo(width/2,height-18);ctx.moveTo(18,height/2);ctx.lineTo(width-18,height/2);ctx.stroke();ctx.setLineDash([]);
  ctx.fillStyle="rgba(169,119,83,.36)"; for(const p of rejected){const q=project(p);ctx.fillRect(q[0],q[1],1.4,1.4);}
  ctx.fillStyle="rgba(111,224,167,.8)"; for(const p of accepted){const q=project(p);ctx.fillRect(q[0]-1,q[1]-1,2,2);}
  drawPath(ctx,outline.map(project),"#72d8ff",2.5,[]);
  ctx.strokeStyle="#ffbd66";ctx.lineWidth=1.5;ctx.beginPath();ctx.moveTo(width/2-5,height/2);ctx.lineTo(width/2+5,height/2);ctx.moveTo(width/2,height/2-5);ctx.lineTo(width/2,height/2+5);ctx.stroke();
  ctx.fillStyle="rgba(233,244,237,.7)";ctx.font="12px system-ui";ctx.fillText(`${activeModel || "NO FIT"} · ${fieldAidSourceText(state.current)}`,14,22);
}

function drawAll(){ drawCloud(); drawCross(); }

function bindCanvas() {
  const canvas=$("cloudCanvas");
  canvas.addEventListener("pointerdown",(e)=>{state.dragging=true;state.lastX=e.clientX;state.lastY=e.clientY;canvas.setPointerCapture(e.pointerId);});
  canvas.addEventListener("pointermove",(e)=>{if(!state.dragging)return;state.yaw+=(e.clientX-state.lastX)*.008;state.pitch=Math.max(-1.2,Math.min(1.2,state.pitch+(e.clientY-state.lastY)*.008));state.lastX=e.clientX;state.lastY=e.clientY;drawCloud();});
  canvas.addEventListener("pointerup",()=>{state.dragging=false;});
  canvas.addEventListener("wheel",(e)=>{e.preventDefault();state.zoom=Math.max(.5,Math.min(3,state.zoom*Math.exp(-e.deltaY*.001)));drawCloud();},{passive:false});
  $("resetView").addEventListener("click",()=>{state.yaw=-.65;state.pitch=.26;state.zoom=1;drawCloud();});
}

async function init() {
  const [summaryResponse, measurementResponse] = await Promise.all([
    fetch("/data/lidar-measurements/summary.json",{cache:"no-store"}),
    fetch("/data/lidar-measurements/measurements.json",{cache:"no-store"}),
  ]);
  if (!summaryResponse.ok || !measurementResponse.ok) throw new Error("โหลดผลการวัดไม่สำเร็จ");
  const summary=await summaryResponse.json(), payload=await measurementResponse.json();
  state.records=payload.records;
  $("fieldReadyCount").textContent=summary.field_aid_ready_count;
  $("legacyCount").textContent=summary.legacy_operational_count;
  $("legacyNote").textContent=`จากเดิม ${summary.legacy_full_resolution_accepted_count} ต้น · ตัดออก ${summary.legacy_full_resolution_accepted_count-summary.legacy_operational_count}`;
  $("propRootCount").textContent=summary.prop_root_plus_030_count;
  $("propRootNote").textContent=`พร้อมใช้ ${summary.prop_root_plus_030_ready_count} · เช็กหน้างาน ${summary.prop_root_plus_030_check_on_site_count}`;
  $("checkCount").textContent=summary.field_aid_check_on_site_count;
  $("noEstimateNote").textContent="มีค่าแต่ QA ยังไม่นิ่ง";
  $("excludedCount").textContent=summary.operational_excluded_count;
  $("noEstimateCount").textContent=summary.field_aid_no_estimate_count;
  $("searchInput").addEventListener("input",updateList);
  $("filterSelect").addEventListener("change",()=>{updateList();if(!state.filtered.includes(state.current)&&state.filtered[0])selectTree(state.filtered[0]);});
  bindCanvas(); window.addEventListener("resize",drawAll);
  updateList();
  const requested=new URLSearchParams(location.search).get("tree");
  const first=state.records.find((row)=>row.tree_id===requested)||state.filtered[0]||state.records[0];
  await selectTree(first);
}

init().catch((error)=>{$("loading").textContent=error.message;console.error(error);});
