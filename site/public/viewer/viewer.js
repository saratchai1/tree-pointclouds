import * as THREE from 'three';
import { OrbitControls } from './vendor/OrbitControls.js';

const DATA = '../data/';
const DATA_VERSION = '?v=20260817-lidar-field-aid-v2';
const POSITION_CHUNKS = ['positions-00.glbin', 'positions-01.glbin', 'positions-02.glbin'];
const MEASUREMENT_INDEX = `${DATA}lidar-measurements/viewer-index.json`;
const MEASUREMENT_CSV = `${DATA}lidar-measurements/measurements.csv`;
const MEASUREMENT_DETAIL_PAGE = '../lidar-measurements/';

const statusEl = document.querySelector('#status');
const budgetEl = document.querySelector('#budget');
const pointSizeEl = document.querySelector('#pointSize');
const pointSizeLabelEl = document.querySelector('#pointSizeLabel');
const treeListEl = document.querySelector('#treeList');
const treeListCountEl = document.querySelector('#treeListCount');
const treeSearchEl = document.querySelector('#treeSearch');
const measurementFilterEl = document.querySelector('#measurementFilter');
const showAllTreesEl = document.querySelector('#showAllTrees');
const toggleMeasurementsEl = document.querySelector('#toggleMeasurements');
const exportMeasurementsEl = document.querySelector('#exportMeasurements');
const selectedTreeEl = document.querySelector('#selectedTree');
const selectedStatusEl = document.querySelector('#selectedStatus');
const selectedCircumferenceEl = document.querySelector('#selectedCircumference');
const selectedDiameterEl = document.querySelector('#selectedDiameter');
const selectedProtocolEl = document.querySelector('#selectedProtocol');
const selectedQaEl = document.querySelector('#selectedQa');
const openMeasurementDetailEl = document.querySelector('#openMeasurementDetail');

const renderer = new THREE.WebGLRenderer({ antialias: true, logarithmicDepthBuffer: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight);
renderer.setClearColor(0x07110d, 1);
document.body.prepend(renderer.domElement);

const scene = new THREE.Scene();
scene.fog = null;

const camera = new THREE.PerspectiveCamera(52, innerWidth / innerHeight, 0.01, 500);
camera.up.set(0, 1, 0);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.07;
controls.screenSpacePanning = true;

scene.add(new THREE.HemisphereLight(0xd8f5e2, 0x13251a, 1.2));
const grid = new THREE.GridHelper(20, 20, 0x3d7050, 0x193726);
grid.material.opacity = 0.48;
grid.material.transparent = true;
scene.add(grid);

const measurementLayer = new THREE.Group();
measurementLayer.name = 'lidar-field-aid-measurements';
scene.add(measurementLayer);
const selectionLayer = new THREE.Group();
selectionLayer.name = 'selected-lidar-measurement';
scene.add(selectionLayer);

const markerGeometry = new THREE.SphereGeometry(0.045, 12, 8);
const markerMaterials = new Map();
const measurementGroups = new Map();
const measurementHitTargets = [];
const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();

let sourcePositions;
let sourceColors;
let cloud;
let measurementIndex;
let measurementRecords = [];
let visibleMeasurementRecords = [];
let displayedPointCount = 0;
let selectedTreeId = null;
let pointerStart = null;
let center = new THREE.Vector3();
let radius = 6;

function fmt(number, digits = 1) {
  if (number == null || Number.isNaN(Number(number))) return '–';
  return Number(number).toLocaleString('th-TH', {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  });
}

function setStatus(text) {
  statusEl.textContent = text;
}

function pointWorldSize() {
  return Number(pointSizeEl.value) * 0.008;
}

function toScene(x, y, z) {
  return new THREE.Vector3(x, z, -y);
}

function frame(direction = new THREE.Vector3(1.2, 0.85, 1.2)) {
  camera.position.copy(center).add(direction.clone().normalize().multiplyScalar(radius * 2.25));
  controls.target.copy(center);
  controls.update();
}

function fieldAidStatusLabel(record) {
  if (record.operationally_excluded || record.field_aid_status === 'EXCLUDED_CONFIRMED_WRONG') {
    return 'ตัดออกตามการตรวจยืนยัน';
  }
  if (record.field_aid_status === 'READY_FOR_FIELD_USE') {
    return record.field_aid_is_current_protocol_final ? 'พร้อมใช้ · protocol ล่าสุด' : 'พร้อมใช้ช่วยภาคสนาม';
  }
  if (record.field_aid_status === 'CHECK_ON_SITE') return 'มีค่าประมาณ · เช็กหน้างาน';
  if (record.field_aid_status === 'NO_ESTIMATE') return 'ข้อมูลไม่พอสำหรับวง fit';
  return record.field_aid_status || 'ยังไม่ระบุสถานะ';
}

function protocolLabel(record) {
  if (record.measurement_kind === 'STANDARD_DBH_1_30') return 'Standard DBH ที่ 1.30 ม.';
  if (record.measurement_kind === 'PROP_ROOT_PLUS_030') return 'Prop-root · จุดเกาะรากสูงสุด +0.30 ม.';
  if (record.measurement_kind === 'LEGACY_STANDARD_DBH_1_30') return 'Full-LAS รุ่นแรก · Standard 1.30 ม.';
  if (record.measurement_kind === 'LEGACY_ADAPTIVE_IRREGULAR_ZONE_PLUS_030') {
    return 'Full-LAS รุ่นแรก · ยอดโซนผิดปกติ +0.30 ม.';
  }
  return 'Screening ที่ 1.30 ม. · ยังไม่ยืนยัน protocol';
}

function statusClass(record) {
  if (record.operationally_excluded || record.field_aid_status === 'EXCLUDED_CONFIRMED_WRONG') return 'excluded';
  if (record.field_aid_status === 'READY_FOR_FIELD_USE') return 'ready';
  if (record.field_aid_status === 'CHECK_ON_SITE') return 'check';
  return 'none';
}

function measurementColor(record) {
  if (record.operationally_excluded || record.field_aid_status === 'EXCLUDED_CONFIRMED_WRONG') return 0xf06c5b;
  if (record.field_aid_status === 'NO_ESTIMATE') return 0x82958b;
  if (record.field_aid_is_current_protocol_final) return 0x72d8ff;
  if (record.measurement_kind === 'PROP_ROOT_PLUS_030') return 0xd6a4ff;
  if (record.field_aid_source === 'LEGACY_FULL_RESOLUTION_ACCEPTED') return 0x9fb4ff;
  if (record.field_aid_status === 'READY_FOR_FIELD_USE') return 0x6fe0a7;
  return 0xffbd66;
}

function markerMaterial(record) {
  const key = `${statusClass(record)}-${measurementColor(record)}`;
  if (!markerMaterials.has(key)) {
    markerMaterials.set(key, new THREE.MeshBasicMaterial({
      color: measurementColor(record),
      transparent: true,
      opacity: record.renderable ? 0.7 : 0.92,
      depthTest: false,
      depthWrite: false,
    }));
  }
  return markerMaterials.get(key);
}

function sourcePointOnPlane(plane, u, v) {
  return [0, 1, 2].map((index) => (
    plane.center_xyz[index]
    + plane.basis_u[index] * u
    + plane.basis_v[index] * v
  ));
}

function fitCenterSource(record) {
  if (!record.plane) return null;
  const ellipse = record.fit?.ellipse;
  const centerUv = record.fit_model === 'ELLIPSE' && ellipse?.valid
    ? ellipse.center
    : record.fit?.center;
  if (!centerUv) return [...record.plane.center_xyz];
  return sourcePointOnPlane(record.plane, centerUv[0], centerUv[1]);
}

function outlineSourcePoints(record, samples = 72) {
  if (!record.plane || !record.fit) return [];
  const fit = record.fit;
  const ellipse = fit.ellipse;
  const points = [];
  for (let index = 0; index < samples; index += 1) {
    const angle = Math.PI * 2 * index / samples;
    let u;
    let v;
    if (record.fit_model === 'ELLIPSE' && ellipse?.valid) {
      const cosine = Math.cos(ellipse.rotation_rad);
      const sine = Math.sin(ellipse.rotation_rad);
      u = ellipse.center[0]
        + ellipse.semi_major_axis_m * Math.cos(angle) * cosine
        - ellipse.semi_minor_axis_m * Math.sin(angle) * sine;
      v = ellipse.center[1]
        + ellipse.semi_major_axis_m * Math.cos(angle) * sine
        + ellipse.semi_minor_axis_m * Math.sin(angle) * cosine;
    } else if (fit.center && fit.radius_m != null) {
      u = fit.center[0] + fit.radius_m * Math.cos(angle);
      v = fit.center[1] + fit.radius_m * Math.sin(angle);
    } else {
      return [];
    }
    points.push(sourcePointOnPlane(record.plane, u, v));
  }
  return points;
}

function effectiveRadiusM(record) {
  if (record.fit_model === 'ELLIPSE' && record.fit?.ellipse?.valid) {
    return (record.fit.ellipse.semi_major_axis_m + record.fit.ellipse.semi_minor_axis_m) / 2;
  }
  return record.fit?.radius_m ?? Math.max((record.field_aid_diameter_cm ?? 8) / 200, 0.04);
}

function makeRingGeometry(record, selected = false) {
  const points = outlineSourcePoints(record).map((point) => toScene(point[0], point[1], point[2]));
  if (points.length < 4) return null;
  const curve = new THREE.CatmullRomCurve3(points, true, 'centripetal', 0.5);
  const baseTubeRadius = Math.max(0.008, Math.min(0.028, effectiveRadiusM(record) * 0.04));
  return new THREE.TubeGeometry(curve, 72, selected ? baseTubeRadius * 1.9 : baseTubeRadius, 6, true);
}

function recordSceneCenter(record) {
  const source = fitCenterSource(record) ?? record.plane?.center_xyz;
  return source ? toScene(source[0], source[1], source[2]) : null;
}

function addMeasurementOverlay(record) {
  if (!record.plane) return;
  const group = new THREE.Group();
  group.name = `measurement-${record.tree_id}`;
  group.userData.treeId = record.tree_id;

  const sceneCenter = recordSceneCenter(record);
  const marker = new THREE.Mesh(markerGeometry, markerMaterial(record));
  marker.position.copy(sceneCenter);
  marker.scale.setScalar(record.renderable ? 0.82 : 1.25);
  marker.renderOrder = 14;
  marker.userData.treeId = record.tree_id;
  marker.userData.kind = 'marker';
  group.add(marker);
  measurementHitTargets.push(marker);

  if (record.renderable) {
    const geometry = makeRingGeometry(record);
    if (geometry) {
      const ring = new THREE.Mesh(
        geometry,
        new THREE.MeshBasicMaterial({
          color: measurementColor(record),
          transparent: true,
          opacity: record.field_aid_status === 'READY_FOR_FIELD_USE' ? 0.98 : 0.82,
          depthTest: false,
          depthWrite: false,
        })
      );
      ring.renderOrder = 13;
      ring.userData.treeId = record.tree_id;
      ring.userData.kind = 'ring';
      group.add(ring);
      measurementHitTargets.push(ring);
    }
  }

  measurementGroups.set(record.tree_id, group);
  measurementLayer.add(group);
}

function disposeLayer(layer) {
  for (const object of [...layer.children]) {
    object.traverse((child) => {
      child.geometry?.dispose();
      child.material?.map?.dispose();
      child.material?.dispose();
    });
    layer.remove(object);
  }
}

function makeMeasurementLabel(record) {
  const canvas = document.createElement('canvas');
  canvas.width = 1400;
  canvas.height = 300;
  const context = canvas.getContext('2d');
  const color = `#${measurementColor(record).toString(16).padStart(6, '0')}`;
  context.fillStyle = 'rgba(3, 10, 7, .94)';
  context.fillRect(8, 8, canvas.width - 16, canvas.height - 16);
  context.strokeStyle = color;
  context.lineWidth = 10;
  context.strokeRect(8, 8, canvas.width - 16, canvas.height - 16);
  context.textAlign = 'center';
  context.textBaseline = 'middle';
  context.fillStyle = '#fff9e8';
  context.font = '800 70px system-ui, sans-serif';
  context.fillText(record.tree_id, canvas.width / 2, 92);
  context.fillStyle = '#d9e9df';
  context.font = '600 44px system-ui, sans-serif';
  const circumferenceText = record.field_aid_circumference_cm == null
    ? fieldAidStatusLabel(record)
    : `รอบวง ${fmt(record.field_aid_circumference_cm, 2)} ซม. · ${protocolLabel(record)}`;
  context.fillText(circumferenceText, canvas.width / 2, 202);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
    map: texture,
    depthTest: false,
    depthWrite: false,
    transparent: true,
  }));
  sprite.scale.set(3.7, 0.79, 1);
  sprite.renderOrder = 30;
  return sprite;
}

function updateSelectedDetail(record) {
  selectedTreeEl.textContent = record.tree_id;
  selectedStatusEl.textContent = fieldAidStatusLabel(record);
  selectedStatusEl.className = `selected-status status-${statusClass(record)}`;
  selectedCircumferenceEl.textContent = record.field_aid_circumference_cm == null
    ? '–'
    : `${fmt(record.field_aid_circumference_cm, 2)} ซม.`;
  const diameter = record.field_aid_dbh_cm ?? record.field_aid_diameter_cm;
  selectedDiameterEl.textContent = diameter == null ? '–' : `${fmt(diameter, 2)} ซม.`;
  document.querySelector('#selectedDiameterLabel').textContent = record.field_aid_dbh_cm != null
    ? 'DBH estimate'
    : 'เส้นผ่านศูนย์กลาง ณ ระนาบ';
  selectedProtocolEl.textContent = `${protocolLabel(record)} · สูง ${fmt(record.field_aid_measurement_height_agl_m, 3)} ม.`;
  selectedQaEl.textContent = record.qa_reason_codes?.length
    ? record.qa_reason_codes.slice(0, 3).join(' · ')
    : 'ไม่พบ reason code เพิ่มเติม';
  openMeasurementDetailEl.href = `${MEASUREMENT_DETAIL_PAGE}?tree=${encodeURIComponent(record.tree_id)}`;
  openMeasurementDetailEl.removeAttribute('aria-disabled');
}

function clearSelection() {
  selectedTreeId = null;
  disposeLayer(selectionLayer);
  document.querySelectorAll('.tree-row').forEach((row) => row.classList.remove('selected'));
  selectedTreeEl.textContent = 'ยังไม่ได้เลือกต้นไม้';
  selectedStatusEl.textContent = 'คลิกวงหรือเลือกรายการด้านล่าง';
  selectedStatusEl.className = 'selected-status';
  selectedCircumferenceEl.textContent = '–';
  selectedDiameterEl.textContent = '–';
  selectedProtocolEl.textContent = '–';
  selectedQaEl.textContent = '–';
  openMeasurementDetailEl.href = '#';
  openMeasurementDetailEl.setAttribute('aria-disabled', 'true');
}

function focusTree(record, { updateUrl = true } = {}) {
  if (!record?.plane) return;
  selectedTreeId = record.tree_id;
  disposeLayer(selectionLayer);

  const target = recordSceneCenter(record);
  const haloGeometry = record.renderable ? makeRingGeometry(record, true) : null;
  if (haloGeometry) {
    const halo = new THREE.Mesh(
      haloGeometry,
      new THREE.MeshBasicMaterial({
        color: 0xffffff,
        transparent: true,
        opacity: 0.95,
        depthTest: false,
        depthWrite: false,
      })
    );
    halo.renderOrder = 25;
    selectionLayer.add(halo);
  } else {
    const halo = new THREE.Mesh(
      new THREE.SphereGeometry(0.095, 16, 10),
      new THREE.MeshBasicMaterial({ color: 0xffffff, depthTest: false, depthWrite: false })
    );
    halo.position.copy(target);
    halo.renderOrder = 25;
    selectionLayer.add(halo);
  }

  const axis = new THREE.Vector3(...(record.plane.axis_direction ?? [0, 0, 1])).normalize();
  if (axis.z < 0) axis.multiplyScalar(-1);
  const sourceCenter = fitCenterSource(record) ?? record.plane.center_xyz;
  const height = record.field_aid_measurement_height_agl_m ?? record.measurement_height_agl_m ?? 1.3;
  const groundSource = [
    sourceCenter[0] - axis.x * height,
    sourceCenter[1] - axis.y * height,
    sourceCenter[2] - axis.z * height,
  ];
  const axisLine = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints([
      toScene(groundSource[0], groundSource[1], groundSource[2]),
      target,
    ]),
    new THREE.LineDashedMaterial({ color: measurementColor(record), dashSize: 0.08, gapSize: 0.05, depthTest: false })
  );
  axisLine.computeLineDistances();
  axisLine.renderOrder = 24;
  selectionLayer.add(axisLine);

  const label = makeMeasurementLabel(record);
  label.position.copy(target).add(new THREE.Vector3(0, Math.max(0.72, effectiveRadiusM(record) * 2.2), 0));
  selectionLayer.add(label);

  const distance = Math.max(3.2, effectiveRadiusM(record) * 9);
  const cameraDirection = new THREE.Vector3(1, 0.48, 1).normalize();
  const screenRight = new THREE.Vector3().crossVectors(cameraDirection.clone().negate(), camera.up).normalize();
  const lookTarget = target.clone().addScaledVector(screenRight, -0.85);
  camera.position.copy(lookTarget).add(cameraDirection.multiplyScalar(distance));
  controls.target.copy(lookTarget);
  controls.update();

  document.querySelectorAll('.tree-row').forEach((row) => {
    row.classList.toggle('selected', row.dataset.treeId === record.tree_id);
  });
  document.querySelector(`.tree-row[data-tree-id="${record.tree_id}"]`)?.scrollIntoView({ block: 'nearest' });
  updateSelectedDetail(record);
  setStatus(`${record.tree_id} · ${fieldAidStatusLabel(record)} · รอบวง ${fmt(record.field_aid_circumference_cm, 2)} ซม.`);

  if (updateUrl) {
    const url = new URL(location.href);
    url.searchParams.set('tree', record.tree_id);
    history.replaceState(null, '', url);
  }
}

function recordMatchesFilter(record, filter) {
  if (filter === 'ALL') return true;
  if (filter === 'MEASURED') return record.renderable;
  if (filter === 'READY') return record.field_aid_status === 'READY_FOR_FIELD_USE' && !record.operationally_excluded;
  if (filter === 'CHECK') return record.field_aid_status === 'CHECK_ON_SITE' && !record.operationally_excluded;
  if (filter === 'FINAL') return record.acceptance_status === 'FINAL_LIDAR_ESTIMATE' && !record.operationally_excluded;
  if (filter === 'PROP_ROOT') return record.measurement_kind === 'PROP_ROOT_PLUS_030' && !record.operationally_excluded;
  if (filter === 'LEGACY') return record.legacy_full_resolution_status === 'ACCEPTED' && !record.operationally_excluded;
  if (filter === 'NO_ESTIMATE') return record.field_aid_status === 'NO_ESTIMATE';
  if (filter === 'EXCLUDED') return record.operationally_excluded || record.field_aid_status === 'EXCLUDED_CONFIRMED_WRONG';
  return true;
}

function renderTreeList(records) {
  treeListEl.replaceChildren();
  treeListCountEl.textContent = `${records.length} / ${measurementRecords.length}`;
  if (!records.length) {
    const empty = document.createElement('div');
    empty.className = 'measurement-empty';
    empty.textContent = 'ไม่พบ Tree ID ตามตัวกรอง';
    treeListEl.append(empty);
    return;
  }

  for (const record of records) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = `tree-row status-${statusClass(record)}`;
    row.dataset.treeId = record.tree_id;
    row.classList.toggle('selected', record.tree_id === selectedTreeId);

    const id = document.createElement('span');
    id.className = 'tree-id';
    id.textContent = record.tree_id.replace('TREE_', '');

    const metrics = document.createElement('span');
    const title = document.createElement('b');
    title.textContent = record.field_aid_circumference_cm == null
      ? fieldAidStatusLabel(record)
      : `รอบวง ${fmt(record.field_aid_circumference_cm, 2)} ซม.`;
    const detail = document.createElement('small');
    detail.textContent = `${protocolLabel(record)} · ${fmt(record.inlier_count, 0)} จุด fit`;
    metrics.append(title, detail);

    const status = document.createElement('span');
    status.className = `tree-confidence confidence-${statusClass(record)}`;
    status.textContent = record.field_aid_status === 'READY_FOR_FIELD_USE'
      ? 'พร้อมใช้'
      : record.field_aid_status === 'CHECK_ON_SITE'
        ? 'เช็กหน้างาน'
        : record.operationally_excluded
          ? 'ตัดออก'
          : 'ไม่มีค่า';

    row.append(id, metrics, status);
    row.addEventListener('click', () => focusTree(record));
    treeListEl.append(row);
  }
}

function applyMeasurementFilter({ frameAfter = false } = {}) {
  const filter = measurementFilterEl.value;
  const query = treeSearchEl.value.trim().toUpperCase();
  visibleMeasurementRecords = measurementRecords.filter((record) => (
    recordMatchesFilter(record, filter)
    && (!query || record.tree_id.includes(query))
  ));
  const visibleIds = new Set(visibleMeasurementRecords.map((record) => record.tree_id));
  for (const record of measurementRecords) {
    const group = measurementGroups.get(record.tree_id);
    if (group) group.visible = visibleIds.has(record.tree_id);
  }
  renderTreeList(visibleMeasurementRecords);
  if (selectedTreeId && !visibleIds.has(selectedTreeId)) clearSelection();
  if (frameAfter) frameMeasurements();
}

function frameMeasurements(direction = new THREE.Vector3(1.15, 0.95, 1.15)) {
  const records = visibleMeasurementRecords.filter((record) => record.plane);
  if (!records.length) return frame(direction);
  const box = new THREE.Box3();
  for (const record of records) {
    const recordCenter = recordSceneCenter(record);
    if (recordCenter) box.expandByPoint(recordCenter);
  }
  if (box.isEmpty()) return frame(direction);
  const target = box.getCenter(new THREE.Vector3());
  const measurementRadius = Math.max(box.getBoundingSphere(new THREE.Sphere()).radius, 1.5);
  camera.position.copy(target).add(direction.clone().normalize().multiplyScalar(measurementRadius * 2.45));
  controls.target.copy(target);
  controls.update();
}

function updateSummary() {
  const summary = measurementIndex.summary ?? {};
  document.querySelector('#treeCount').textContent = fmt(summary.tree_count ?? measurementRecords.length, 0);
  document.querySelector('#readyCount').textContent = fmt(summary.field_aid_ready_count, 0);
  document.querySelector('#checkCount').textContent = fmt(summary.field_aid_check_on_site_count, 0);
  document.querySelector('#otherCount').textContent = fmt(
    (summary.field_aid_no_estimate_count ?? 0) + (summary.operational_excluded_count ?? 0),
    0
  );
}

function exportMeasurements() {
  const link = document.createElement('a');
  link.href = `${MEASUREMENT_CSV}${DATA_VERSION}`;
  link.download = 'samutsongkhram-lidar-field-aid-measurements.csv';
  document.body.append(link);
  link.click();
  link.remove();
}

function rebuildCloud() {
  if (!sourcePositions || !sourceColors) return;
  const requested = Number(budgetEl.value);
  const total = sourcePositions.length / 3;
  const stride = Math.max(1, Math.ceil(total / requested));
  const count = Math.ceil(total / stride);
  const positions = new Float32Array(count * 3);
  const colors = new Uint8Array(count * 3);
  let output = 0;
  for (let index = 0; index < total; index += stride) {
    const positionIndex = index * 3;
    const colorIndex = index * 4;
    positions[output * 3] = sourcePositions[positionIndex];
    positions[output * 3 + 1] = sourcePositions[positionIndex + 2];
    positions[output * 3 + 2] = -sourcePositions[positionIndex + 1];
    colors[output * 3] = sourceColors[colorIndex];
    colors[output * 3 + 1] = sourceColors[colorIndex + 1];
    colors[output * 3 + 2] = sourceColors[colorIndex + 2];
    output += 1;
  }

  cloud?.geometry.dispose();
  cloud?.material.dispose();
  if (cloud) scene.remove(cloud);
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute('color', new THREE.Uint8BufferAttribute(colors, 3, true));
  geometry.computeBoundingBox();
  geometry.computeBoundingSphere();
  center.copy(geometry.boundingBox.getCenter(new THREE.Vector3()));
  radius = Math.max(geometry.boundingSphere.radius, 1);
  cloud = new THREE.Points(
    geometry,
    new THREE.PointsMaterial({
      size: pointWorldSize(),
      sizeAttenuation: true,
      vertexColors: true,
      transparent: true,
      opacity: 0.98,
    })
  );
  scene.add(cloud);
  grid.scale.setScalar(Math.max(radius * 2, 20) / 20);
  grid.position.y = geometry.boundingBox.min.y - 0.03;
  displayedPointCount = count;
  const ringCount = measurementRecords.filter((record) => record.renderable).length;
  setStatus(`พร้อม · แสดง ${fmt(count, 0)} จุด · มีวง field-aid ${fmt(ringCount, 0)} ต้น`);
}

async function loadJson(url, label) {
  const response = await fetch(url, { cache: 'no-store' });
  if (!response.ok) throw new Error(`${label} HTTP ${response.status}`);
  return response.json();
}

async function load() {
  const [metadata, index] = await Promise.all([
    loadJson(`${DATA}metadata.json${DATA_VERSION}`, 'metadata'),
    loadJson(`${MEASUREMENT_INDEX}${DATA_VERSION}`, 'LiDAR viewer index'),
  ]);
  measurementIndex = index;
  measurementRecords = Array.isArray(index.records) ? index.records : [];
  if (!measurementRecords.length) throw new Error('LiDAR viewer index ไม่มี records');

  document.querySelector('#points').textContent = fmt(metadata.sourcePointCount ?? metadata.points, 0);
  const positionAttribute = metadata.attributes.find((attribute) => attribute.name === 'position');
  const min = positionAttribute.min;
  const max = positionAttribute.max;
  document.querySelector('#footprint').textContent = `${fmt(max[0] - min[0])} × ${fmt(max[1] - min[1])} ม.`;
  document.querySelector('#height').textContent = `${fmt(max[2] - min[2])} ม.`;
  updateSummary();

  for (const record of measurementRecords) addMeasurementOverlay(record);
  applyMeasurementFilter();
  showAllTreesEl.disabled = false;
  toggleMeasurementsEl.disabled = false;
  exportMeasurementsEl.disabled = false;

  setStatus(`กำลังโหลด ${fmt(metadata.points, 0)} จุด และตำแหน่ง Tree ID ${fmt(measurementRecords.length, 0)} ต้น…`);
  const [positionBuffers, colorsBuffer] = await Promise.all([
    Promise.all(POSITION_CHUNKS.map((name) => fetch(`${DATA}${name}${DATA_VERSION}`).then((response) => {
      if (!response.ok) throw new Error(`${name} HTTP ${response.status}`);
      return response.arrayBuffer();
    }))),
    fetch(`${DATA}colors.glbin${DATA_VERSION}`).then((response) => {
      if (!response.ok) throw new Error(`colors HTTP ${response.status}`);
      return response.arrayBuffer();
    }),
  ]);
  const positionsByteLength = positionBuffers.reduce((sum, buffer) => sum + buffer.byteLength, 0);
  const mergedPositions = new Uint8Array(positionsByteLength);
  let positionOffset = 0;
  for (const buffer of positionBuffers) {
    mergedPositions.set(new Uint8Array(buffer), positionOffset);
    positionOffset += buffer.byteLength;
  }
  sourcePositions = new Float32Array(mergedPositions.buffer);
  sourceColors = new Uint8Array(colorsBuffer);
  rebuildCloud();

  const requestedTreeId = new URLSearchParams(location.search).get('tree')?.toUpperCase();
  const requestedRecord = measurementRecords.find((record) => record.tree_id === requestedTreeId);
  if (requestedRecord) {
    measurementFilterEl.value = 'ALL';
    treeSearchEl.value = requestedRecord.tree_id;
    applyMeasurementFilter();
    focusTree(requestedRecord, { updateUrl: false });
  } else {
    frameMeasurements();
  }
}

function objectAndParentsVisible(object) {
  let current = object;
  while (current) {
    if (!current.visible) return false;
    current = current.parent;
  }
  return true;
}

function selectMeasurementAtPointer(event) {
  const rect = renderer.domElement.getBoundingClientRect();
  pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const hit = raycaster.intersectObjects(measurementHitTargets, false)
    .find((intersection) => objectAndParentsVisible(intersection.object));
  if (!hit) return;
  const record = measurementRecords.find((item) => item.tree_id === hit.object.userData.treeId);
  if (record) focusTree(record);
}

budgetEl.addEventListener('change', rebuildCloud);
pointSizeEl.addEventListener('input', () => {
  pointSizeLabelEl.textContent = Number(pointSizeEl.value).toFixed(1);
  if (cloud) cloud.material.size = pointWorldSize();
});
document.querySelector('#reset').addEventListener('click', () => frame());
document.querySelector('#top').addEventListener('click', () => frameMeasurements(new THREE.Vector3(0, 1, 0.001)));
document.querySelector('#front').addEventListener('click', () => frameMeasurements(new THREE.Vector3(0, 0.08, 1)));
showAllTreesEl.addEventListener('click', () => {
  clearSelection();
  frameMeasurements();
  setStatus(`พร้อม · แสดง ${fmt(displayedPointCount, 0)} จุด · ตัวกรองปัจจุบัน ${fmt(visibleMeasurementRecords.length, 0)} Tree IDs`);
});
toggleMeasurementsEl.addEventListener('click', () => {
  measurementLayer.visible = !measurementLayer.visible;
  selectionLayer.visible = measurementLayer.visible;
  toggleMeasurementsEl.textContent = measurementLayer.visible ? 'ซ่อนวงวัด' : 'แสดงวงวัด';
});
exportMeasurementsEl.addEventListener('click', exportMeasurements);
measurementFilterEl.addEventListener('change', () => applyMeasurementFilter({ frameAfter: true }));
treeSearchEl.addEventListener('input', () => applyMeasurementFilter());
openMeasurementDetailEl.addEventListener('click', (event) => {
  if (openMeasurementDetailEl.getAttribute('aria-disabled') === 'true') event.preventDefault();
});

renderer.domElement.addEventListener('pointerdown', (event) => {
  pointerStart = { x: event.clientX, y: event.clientY };
});
renderer.domElement.addEventListener('pointerup', (event) => {
  if (!pointerStart) return;
  const movement = Math.hypot(event.clientX - pointerStart.x, event.clientY - pointerStart.y);
  pointerStart = null;
  if (movement <= 5 && measurementLayer.visible) selectMeasurementAtPointer(event);
});

addEventListener('resize', () => {
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
});

renderer.setAnimationLoop(() => {
  controls.update();
  renderer.render(scene, camera);
});

clearSelection();
load().catch((error) => {
  console.error(error);
  setStatus(`เปิดข้อมูลไม่สำเร็จ: ${error.message}`);
});
