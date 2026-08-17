import * as THREE from 'three';
import { OrbitControls } from './vendor/OrbitControls.js';

const DATA = './data/';
const POSITION_CHUNKS = ['positions-00.glbin', 'positions-01.glbin', 'positions-02.glbin'];
const statusEl = document.querySelector('#status');
const budgetEl = document.querySelector('#budget');
const pointSizeEl = document.querySelector('#pointSize');
const pointSizeLabelEl = document.querySelector('#pointSizeLabel');
const measurementCardEl = document.querySelector('#measurementCard');
const measurementTreeEl = document.querySelector('#measurementTree');
const measurementBadgeEl = document.querySelector('#measurementBadge');
const measurementDbhEl = document.querySelector('#measurementDbh');
const measurementCircumferenceEl = document.querySelector('#measurementCircumference');
const measurementWarningEl = document.querySelector('#measurementWarning');
const focusMeasurementEl = document.querySelector('#focusMeasurement');
const toggleMeasurementsEl = document.querySelector('#toggleMeasurements');

const renderer = new THREE.WebGLRenderer({ antialias: true, logarithmicDepthBuffer: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight);
renderer.setClearColor(0x07110d, 1);
document.body.prepend(renderer.domElement);

const scene = new THREE.Scene();
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

const measurementGroup = new THREE.Group();
measurementGroup.name = 'Rayong DBH screening overlays';
scene.add(measurementGroup);

let sourcePositions;
let sourceColors;
let cloud;
let center = new THREE.Vector3();
let radius = 6;
let displayedPointCount = 0;
let measurementRecords = [];
let activeMeasurement = null;

const fmt = (value, digits = 1) => Number(value).toLocaleString('th-TH', { maximumFractionDigits: digits });
const setStatus = (text) => { statusEl.textContent = text; };
const pointWorldSize = () => Number(pointSizeEl.value) * 0.008;

const QA_LABELS = {
  FITTED_RADIUS_NEAR_0_30_M_BOUND: 'รัศมีชนใกล้ขอบเขตตัวตรวจจับ',
  LIMITED_POINT_SUPPORT: 'จำนวนจุดรองรับน้อย',
  PARTIAL_ANGULAR_COVERAGE: 'ผิวรอบวงเห็นไม่ครบ',
  LIMITED_VERTICAL_SLICE_SUPPORT: 'ผ่าน slice แนวดิ่งน้อย',
  CENTERLINE_SPREAD_HIGH: 'แนวศูนย์กลางแกว่ง',
  RADIUS_VARIATION_HIGH: 'รัศมีต่างกันมากระหว่างระดับ',
};

function updateReadyStatus() {
  if (!sourcePositions) return;
  const candidateText = measurementRecords.length
    ? ` · candidate วัดได้ ${fmt(measurementRecords.length, 0)} จุด`
    : '';
  setStatus(`พร้อม · แสดง ${fmt(displayedPointCount, 0)} จุด จาก sample Rayong${candidateText}`);
}

function frame(direction = new THREE.Vector3(1.2, 0.85, 1.2)) {
  camera.position.copy(center).add(direction.clone().normalize().multiplyScalar(radius * 2.25));
  controls.target.copy(center);
  controls.update();
}

function focusMeasurement(record) {
  if (!record) return;
  const target = new THREE.Vector3(Number(record.x), Number(record.measurementZ), -Number(record.y));
  const viewDistance = Math.max(2.4, Number(record.radiusM || 0.2) * 9);
  camera.position.copy(target).add(new THREE.Vector3(1.2, 0.8, 1.2).normalize().multiplyScalar(viewDistance));
  controls.target.copy(target);
  controls.update();
}

function rebuildCloud() {
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
  cloud = new THREE.Points(geometry, new THREE.PointsMaterial({
    size: pointWorldSize(), sizeAttenuation: true, vertexColors: true, transparent: true, opacity: 0.98,
  }));
  scene.add(cloud);
  grid.scale.setScalar(Math.max(radius * 2, 20) / 20);
  grid.position.y = geometry.boundingBox.min.y - 0.03;
  displayedPointCount = count;
  updateReadyStatus();
}

function makeLabelSprite(record) {
  const canvas = document.createElement('canvas');
  canvas.width = 1024;
  canvas.height = 300;
  const context = canvas.getContext('2d');
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.fillStyle = 'rgba(17, 15, 10, 0.92)';
  context.strokeStyle = 'rgba(255, 196, 93, 0.95)';
  context.lineWidth = 8;
  context.beginPath();
  context.roundRect(12, 12, canvas.width - 24, canvas.height - 24, 42);
  context.fill();
  context.stroke();

  context.textBaseline = 'middle';
  context.fillStyle = '#ffd77d';
  context.font = '700 58px system-ui, sans-serif';
  context.fillText(record.treeId, 54, 82);
  context.fillStyle = '#fff8e7';
  context.font = '700 48px system-ui, sans-serif';
  context.fillText(`DBH ${fmt(record.dbhCm, 1)} ซม.`, 54, 158);
  context.fillStyle = '#e8d8ba';
  context.font = '500 40px system-ui, sans-serif';
  context.fillText(`เส้นรอบวง ${fmt(record.circumferenceCm, 1)} ซม.`, 54, 228);

  const texture = new THREE.CanvasTexture(canvas);
  texture.minFilter = THREE.LinearFilter;
  const material = new THREE.SpriteMaterial({
    map: texture,
    transparent: true,
    depthTest: false,
    depthWrite: false,
  });
  const sprite = new THREE.Sprite(material);
  sprite.scale.set(7.6, 2.23, 1);
  sprite.renderOrder = 100;
  return sprite;
}

function disposeObject(object) {
  object.traverse((child) => {
    child.geometry?.dispose?.();
    if (Array.isArray(child.material)) {
      child.material.forEach((material) => {
        material.map?.dispose?.();
        material.dispose?.();
      });
    } else {
      child.material?.map?.dispose?.();
      child.material?.dispose?.();
    }
  });
}

function clearMeasurements() {
  while (measurementGroup.children.length) {
    const child = measurementGroup.children.pop();
    disposeObject(child);
  }
}

function addMeasurementMarker(record) {
  const x = Number(record.x);
  const sourceY = Number(record.y);
  const groundZ = Number(record.groundZ);
  const measurementZ = Number(record.measurementZ);
  const fittedRadius = Number(record.radiusM || Number(record.dbhCm) / 200);
  if (![x, sourceY, groundZ, measurementZ, fittedRadius].every(Number.isFinite)) return;

  const marker = new THREE.Group();
  marker.name = record.treeId;
  marker.userData.record = record;
  const sceneZ = -sourceY;
  const tubeRadius = Math.max(0.018, Math.min(0.035, fittedRadius * 0.075));

  // The torus centreline uses the fitted radius. Its plane is perpendicular to
  // the source Z axis, which is vertical in this preview coordinate system.
  const ring = new THREE.Mesh(
    new THREE.TorusGeometry(fittedRadius, tubeRadius, 12, 72),
    new THREE.MeshBasicMaterial({
      color: 0xffbd59,
      transparent: true,
      opacity: 0.98,
      depthTest: false,
      depthWrite: false,
    }),
  );
  ring.rotation.x = Math.PI / 2;
  ring.position.set(x, measurementZ, sceneZ);
  ring.renderOrder = 80;
  marker.add(ring);

  const centerDot = new THREE.Mesh(
    new THREE.SphereGeometry(Math.max(0.035, fittedRadius * 0.12), 16, 12),
    new THREE.MeshBasicMaterial({ color: 0xff5c35, depthTest: false, depthWrite: false }),
  );
  centerDot.position.set(x, measurementZ, sceneZ);
  centerDot.renderOrder = 82;
  marker.add(centerDot);

  const leaderHeight = 1.25;
  const leader = new THREE.Mesh(
    new THREE.CylinderGeometry(0.018, 0.018, leaderHeight, 10),
    new THREE.MeshBasicMaterial({ color: 0xffd98f, depthTest: false, depthWrite: false }),
  );
  leader.position.set(x, measurementZ + leaderHeight / 2, sceneZ);
  leader.renderOrder = 84;
  marker.add(leader);

  const label = makeLabelSprite(record);
  label.position.set(x, measurementZ + leaderHeight + 0.42, sceneZ);
  marker.add(label);

  measurementGroup.add(marker);
}

function setMeasurementCard(record, total) {
  activeMeasurement = record;
  if (!record) {
    measurementCardEl.hidden = true;
    return;
  }
  measurementCardEl.hidden = false;
  measurementTreeEl.textContent = total > 1 ? `${record.treeId} · 1/${total}` : record.treeId;
  measurementBadgeEl.textContent = record.status === 'CHECK_ON_SITE' ? 'ต้องตรวจซ้ำ' : 'ค่าคัดกรอง';
  measurementDbhEl.textContent = `${fmt(record.dbhCm, 1)} ซม.`;
  measurementCircumferenceEl.textContent = `${fmt(record.circumferenceCm, 1)} ซม.`;
  const flags = (record.qaFlags || []).map((flag) => QA_LABELS[flag] || flag);
  const diagnostics = [
    `ระดับวัด ${fmt(record.measurementHeightM, 2)} ม.`,
    `fit ${fmt(record.fitPoints, 0)} จุด`,
    `${fmt(record.validatedSlices, 0)} slice`,
  ];
  if (flags.length) diagnostics.push(`เหตุผลตรวจซ้ำ: ${flags.join(', ')}`);
  measurementWarningEl.textContent = diagnostics.join(' · ');
}

function addMeasurementOverlays(payload) {
  clearMeasurements();
  measurementRecords = Array.isArray(payload?.trees) ? payload.trees : [];
  measurementRecords.forEach(addMeasurementMarker);
  measurementGroup.visible = true;
  toggleMeasurementsEl.textContent = 'ซ่อนวงวัด';
  setMeasurementCard(measurementRecords[0] || null, measurementRecords.length);
  updateReadyStatus();
}

async function load() {
  const [metadata, measurements] = await Promise.all([
    fetch(`${DATA}metadata.json`).then((response) => {
      if (!response.ok) throw new Error(`metadata HTTP ${response.status}`);
      return response.json();
    }),
    fetch(`${DATA}tree-measurements.json`).then((response) => {
      if (!response.ok) return null;
      return response.json();
    }).catch(() => null),
  ]);
  document.querySelector('#points').textContent = fmt(metadata.sourcePointCount ?? metadata.points, 0);
  const positionAttribute = metadata.attributes.find((attribute) => attribute.name === 'position');
  const min = positionAttribute.min;
  const max = positionAttribute.max;
  document.querySelector('#footprint').textContent = `${fmt(max[0] - min[0])} × ${fmt(max[1] - min[1])} ม.`;
  document.querySelector('#height').textContent = `${fmt(max[2] - min[2])} ม.`;
  setStatus(`กำลังโหลด sample ${fmt(metadata.points, 0)} จุด…`);

  const [positionBuffers, colorsBuffer] = await Promise.all([
    Promise.all(POSITION_CHUNKS.map((name) => fetch(`${DATA}${name}`).then((response) => {
      if (!response.ok) throw new Error(`${name} HTTP ${response.status}`);
      return response.arrayBuffer();
    }))),
    fetch(`${DATA}colors.glbin`).then((response) => {
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
  addMeasurementOverlays(measurements);
  frame();
}

budgetEl.addEventListener('change', rebuildCloud);
pointSizeEl.addEventListener('input', () => {
  pointSizeLabelEl.textContent = Number(pointSizeEl.value).toFixed(1);
  if (cloud) cloud.material.size = pointWorldSize();
});
document.querySelector('#reset').addEventListener('click', () => frame());
document.querySelector('#top').addEventListener('click', () => frame(new THREE.Vector3(0, 1, 0.001)));
document.querySelector('#front').addEventListener('click', () => frame(new THREE.Vector3(0, 0.08, 1)));
focusMeasurementEl.addEventListener('click', () => focusMeasurement(activeMeasurement));
toggleMeasurementsEl.addEventListener('click', () => {
  measurementGroup.visible = !measurementGroup.visible;
  toggleMeasurementsEl.textContent = measurementGroup.visible ? 'ซ่อนวงวัด' : 'แสดงวงวัด';
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
load().catch((error) => {
  console.error(error);
  setStatus(`เปิดข้อมูลไม่สำเร็จ: ${error.message}`);
});
