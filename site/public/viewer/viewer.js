import * as THREE from 'three';
import { OrbitControls } from './vendor/OrbitControls.js';

const DATA = '../data/';
const DATA_VERSION = '?v=20260807-samutsongkhram-full-las';
const POSITION_CHUNKS = ['positions-00.glbin', 'positions-01.glbin', 'positions-02.glbin'];
const statusEl = document.querySelector('#status');
const budgetEl = document.querySelector('#budget');
const pointSizeEl = document.querySelector('#pointSize');
const pointSizeLabelEl = document.querySelector('#pointSizeLabel');
const treeListEl = document.querySelector('#treeList');
const showAllTreesEl = document.querySelector('#showAllTrees');
const toggleMeasurementsEl = document.querySelector('#toggleMeasurements');
const exportMeasurementsEl = document.querySelector('#exportMeasurements');

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
measurementLayer.name = 'automatic-tree-measurements';
scene.add(measurementLayer);
const selectionLayer = new THREE.Group();
selectionLayer.name = 'selected-tree-measurement';
scene.add(selectionLayer);

let sourcePositions;
let sourceColors;
let cloud;
let treeAnalysis;
let displayedPointCount = 0;
let center = new THREE.Vector3();
let radius = 6;

function fmt(number, digits = 1) {
  return Number(number).toLocaleString('th-TH', { maximumFractionDigits: digits });
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

function frameTrees(direction = new THREE.Vector3(1.15, 0.95, 1.15)) {
  if (!treeAnalysis?.trees.length) return frame(direction);
  const box = new THREE.Box3();
  for (const tree of treeAnalysis.trees) {
    box.expandByPoint(toScene(tree.center[0], tree.center[1], tree.groundZ));
    box.expandByPoint(toScene(tree.center[0], tree.center[1], tree.measurementZ + 1.5));
  }
  const target = box.getCenter(new THREE.Vector3());
  const treeRadius = Math.max(box.getBoundingSphere(new THREE.Sphere()).radius, 2);
  camera.position.copy(target).add(direction.clone().normalize().multiplyScalar(treeRadius * 2.35));
  controls.target.copy(target);
  controls.update();
}

function confidenceColor(confidence) {
  if (confidence === 'high') return 0xffc84f;
  if (confidence === 'medium') return 0xff9652;
  return 0xf06c5b;
}

function confidenceLabel(confidence) {
  if (confidence === 'high') return 'สูง';
  if (confidence === 'medium') return 'ปานกลาง';
  return 'ควรตรวจซ้ำ';
}

function makeBadge(text, color) {
  const canvas = document.createElement('canvas');
  canvas.width = 256;
  canvas.height = 256;
  const context = canvas.getContext('2d');
  context.beginPath();
  context.arc(128, 128, 110, 0, Math.PI * 2);
  context.fillStyle = color;
  context.fill();
  context.lineWidth = 12;
  context.strokeStyle = '#fff3cf';
  context.stroke();
  context.fillStyle = '#142016';
  context.font = '800 104px system-ui, sans-serif';
  context.textAlign = 'center';
  context.textBaseline = 'middle';
  context.fillText(text, 128, 133);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, depthTest: false, transparent: true }));
  sprite.scale.set(1.05, 1.05, 1);
  sprite.renderOrder = 20;
  return sprite;
}

function makeMeasurementLabel(tree, color) {
  const canvas = document.createElement('canvas');
  canvas.width = 1200;
  canvas.height = 220;
  const context = canvas.getContext('2d');
  context.fillStyle = 'rgba(3, 10, 7, .92)';
  context.fillRect(8, 8, canvas.width - 16, canvas.height - 16);
  context.strokeStyle = color;
  context.lineWidth = 10;
  context.strokeRect(8, 8, canvas.width - 16, canvas.height - 16);
  context.fillStyle = '#fff9e8';
  context.font = '700 58px system-ui, sans-serif';
  context.textAlign = 'center';
  context.textBaseline = 'middle';
  context.fillText(`ต้น ${tree.id} · สูง 1.30 ม. · รอบวง ${fmt(tree.circumferenceM, 2)} ม.`, canvas.width / 2, canvas.height / 2 + 2);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, depthTest: false, transparent: true }));
  sprite.scale.set(2.5, 0.48, 1);
  sprite.renderOrder = 30;
  return sprite;
}

function addTreeOverlay(tree) {
  const color = confidenceColor(tree.confidence);
  const colorCss = `#${color.toString(16).padStart(6, '0')}`;
  const sourceAxis = tree.axis ?? [0, 0, 1];
  const sceneAxis = new THREE.Vector3(sourceAxis[0], sourceAxis[2], -sourceAxis[1]).normalize();
  const group = new THREE.Group();
  group.name = `tree-${tree.id}`;

  const ring = new THREE.Mesh(
    new THREE.TorusGeometry(tree.radiusM, Math.max(0.012, tree.radiusM * 0.045), 10, 72),
    new THREE.MeshBasicMaterial({ color, depthTest: false })
  );
  ring.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), sceneAxis);
  ring.position.copy(toScene(tree.center[0], tree.center[1], tree.measurementZ));
  ring.renderOrder = 12;
  group.add(ring);

  const band = new THREE.Mesh(
    new THREE.CylinderGeometry(tree.radiusM + 0.025, tree.radiusM + 0.025, 0.08, 48, 1, true),
    new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.28, side: THREE.DoubleSide, depthTest: false, depthWrite: false })
  );
  band.position.copy(toScene(tree.center[0], tree.center[1], tree.measurementZ));
  band.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), sceneAxis);
  band.renderOrder = 11;
  group.add(band);

  const heightLine = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints([
      toScene(tree.groundCenter?.[0] ?? tree.center[0], tree.groundCenter?.[1] ?? tree.center[1], tree.groundZ),
      toScene(tree.center[0], tree.center[1], tree.measurementZ),
    ]),
    new THREE.LineBasicMaterial({ color, depthTest: false })
  );
  heightLine.renderOrder = 13;
  group.add(heightLine);

  const badge = makeBadge(String(tree.id), colorCss);
  badge.name = 'tree-badge';
  badge.position.copy(toScene(tree.center[0], tree.center[1], tree.measurementZ + 0.72));
  group.add(badge);
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

function focusTree(tree) {
  disposeLayer(selectionLayer);
  measurementLayer.traverse((object) => {
    if (object.name === 'tree-badge') object.visible = true;
  });
  const selectedGroup = measurementLayer.getObjectByName(`tree-${tree.id}`);
  const selectedBadge = selectedGroup?.getObjectByName('tree-badge');
  if (selectedBadge) selectedBadge.visible = false;
  const color = confidenceColor(tree.confidence);
  const colorCss = `#${color.toString(16).padStart(6, '0')}`;
  const target = toScene(tree.center[0], tree.center[1], tree.measurementZ);
  const sourceAxis = tree.axis ?? [0, 0, 1];
  const sceneAxis = new THREE.Vector3(sourceAxis[0], sourceAxis[2], -sourceAxis[1]).normalize();

  const halo = new THREE.Mesh(
    new THREE.TorusGeometry(tree.radiusM + 0.045, 0.025, 10, 72),
    new THREE.MeshBasicMaterial({ color: 0xffffff, depthTest: false })
  );
  halo.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), sceneAxis);
  halo.position.copy(target);
  halo.renderOrder = 25;
  selectionLayer.add(halo);

  const label = makeMeasurementLabel(tree, colorCss);
  label.position.copy(toScene(tree.center[0], tree.center[1], tree.measurementZ + 1.05));
  selectionLayer.add(label);

  const distance = Math.max(6.5, tree.radiusM * 10);
  const cameraDirection = new THREE.Vector3(1, 0.42, 1).normalize();
  const screenRight = new THREE.Vector3().crossVectors(cameraDirection.clone().negate(), camera.up).normalize();
  const lookTarget = target.clone().addScaledVector(screenRight, -1.5);
  camera.position.copy(lookTarget).add(cameraDirection.multiplyScalar(distance));
  controls.target.copy(lookTarget);
  controls.update();
  document.querySelectorAll('.tree-row').forEach((row) => row.classList.toggle('selected', Number(row.dataset.treeId) === tree.id));
  setStatus(`ต้น ${tree.id} · รอบวง ${fmt(tree.circumferenceM, 2)} ม. · DBH ${fmt(tree.dbhCm, 1)} ซม. · เชื่อมั่น${confidenceLabel(tree.confidence)}`);
}

function renderTreeResults() {
  const counts = { high: 0, medium: 0, low: 0 };
  treeAnalysis.trees.forEach((tree) => counts[tree.confidence]++);
  document.querySelector('#treeCount').textContent = fmt(treeAnalysis.visibleMeasuredTrees, 0);
  document.querySelector('#highCount').textContent = fmt(counts.high, 0);
  document.querySelector('#mediumCount').textContent = fmt(counts.medium, 0);
  document.querySelector('#lowCount').textContent = fmt(counts.low, 0);

  treeListEl.replaceChildren();
  for (const tree of treeAnalysis.trees) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'tree-row';
    row.dataset.treeId = String(tree.id);
    const id = document.createElement('span');
    id.className = 'tree-id';
    id.textContent = String(tree.id);
    const metrics = document.createElement('span');
    const title = document.createElement('b');
    title.textContent = `รอบวง ${fmt(tree.circumferenceM, 2)} ม.`;
    const detail = document.createElement('small');
    detail.textContent = `DBH ${fmt(tree.dbhCm, 1)} ซม. · ${fmt(tree.fitPoints, 0)} จุด`;
    metrics.append(title, detail);
    const confidence = document.createElement('span');
    confidence.className = `tree-confidence confidence-${tree.confidence}`;
    confidence.textContent = confidenceLabel(tree.confidence);
    row.append(id, metrics, confidence);
    row.addEventListener('click', () => focusTree(tree));
    treeListEl.append(row);
  }
}

function exportMeasurements() {
  if (!treeAnalysis?.trees.length) return;
  const header = ['tree_id', 'circumference_m', 'dbh_cm', 'ground_z_m', 'measurement_z_m', 'fit_points', 'angular_coverage_pct', 'residual_cm', 'validated_slices', 'confidence'];
  const rows = treeAnalysis.trees.map((tree) => [
    tree.id,
    tree.circumferenceM.toFixed(3),
    tree.dbhCm.toFixed(1),
    tree.groundZ.toFixed(3),
    tree.measurementZ.toFixed(3),
    tree.fitPoints,
    (tree.angularCoverage * 100).toFixed(1),
    (tree.residualM * 100).toFixed(2),
    tree.validatedSlices,
    tree.confidence,
  ]);
  const csv = [header, ...rows].map((row) => row.join(',')).join('\n');
  const url = URL.createObjectURL(new Blob([`\ufeff${csv}`], { type: 'text/csv;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = 'samutsongkhram-visible-tree-circumference.csv';
  link.click();
  URL.revokeObjectURL(url);
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
    output++;
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
    new THREE.PointsMaterial({ size: pointWorldSize(), sizeAttenuation: true, vertexColors: true, transparent: true, opacity: 0.98 })
  );
  scene.add(cloud);
  grid.scale.setScalar(Math.max(radius * 2, 20) / 20);
  grid.position.y = geometry.boundingBox.min.y - 0.03;
  displayedPointCount = count;
  if (treeAnalysis) setStatus(`พร้อม · แสดง ${fmt(count, 0)} จุด · วัดลำต้นได้ ${fmt(treeAnalysis.visibleMeasuredTrees, 0)} ต้น`);
}

async function load() {
  const [metadata, analysis] = await Promise.all([
    fetch(`${DATA}metadata.json${DATA_VERSION}`).then((response) => {
      if (!response.ok) throw new Error(`metadata HTTP ${response.status}`);
      return response.json();
    }),
    fetch(`${DATA}tree-measurements.json${DATA_VERSION}`).then((response) => {
      if (!response.ok) throw new Error(`tree measurements HTTP ${response.status}`);
      return response.json();
    }),
  ]);
  treeAnalysis = analysis;
  document.querySelector('#points').textContent = fmt(metadata.sourcePointCount ?? metadata.points, 0);
  const positionAttribute = metadata.attributes.find((attribute) => attribute.name === 'position');
  const min = positionAttribute.min;
  const max = positionAttribute.max;
  document.querySelector('#footprint').textContent = `${fmt(max[0] - min[0])} × ${fmt(max[1] - min[1])} ม.`;
  document.querySelector('#height').textContent = `${fmt(max[2] - min[2])} ม.`;

  setStatus(`กำลังโหลด ${fmt(metadata.points, 0)} จุด และผลวัด ${fmt(analysis.visibleMeasuredTrees, 0)} ต้น…`);
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
  treeAnalysis.trees.forEach(addTreeOverlay);
  renderTreeResults();
  showAllTreesEl.disabled = false;
  toggleMeasurementsEl.disabled = false;
  exportMeasurementsEl.disabled = false;
  frameTrees();
}

budgetEl.addEventListener('change', rebuildCloud);
pointSizeEl.addEventListener('input', () => {
  pointSizeLabelEl.textContent = Number(pointSizeEl.value).toFixed(1);
  if (cloud) cloud.material.size = pointWorldSize();
});
document.querySelector('#reset').addEventListener('click', () => frame());
document.querySelector('#top').addEventListener('click', () => frameTrees(new THREE.Vector3(0, 1, 0.001)));
document.querySelector('#front').addEventListener('click', () => frameTrees(new THREE.Vector3(0, 0.08, 1)));
showAllTreesEl.addEventListener('click', () => {
  disposeLayer(selectionLayer);
  measurementLayer.traverse((object) => {
    if (object.name === 'tree-badge') object.visible = true;
  });
  document.querySelectorAll('.tree-row').forEach((row) => row.classList.remove('selected'));
  frameTrees();
  setStatus(`พร้อม · แสดง ${fmt(displayedPointCount, 0)} จุด · วัดลำต้นได้ ${fmt(treeAnalysis.visibleMeasuredTrees, 0)} ต้น`);
});
toggleMeasurementsEl.addEventListener('click', () => {
  measurementLayer.visible = !measurementLayer.visible;
  selectionLayer.visible = measurementLayer.visible;
  toggleMeasurementsEl.textContent = measurementLayer.visible ? 'ซ่อนวงวัด' : 'แสดงวงวัด';
});
exportMeasurementsEl.addEventListener('click', exportMeasurements);

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
