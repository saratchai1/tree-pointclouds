import * as THREE from 'three';
import { OrbitControls } from './vendor/OrbitControls.js';

const DATA = '../pointcloud-data/';
const statusEl = document.querySelector('#status');
const budgetEl = document.querySelector('#budget');
const pointSizeEl = document.querySelector('#pointSize');
const pointSizeLabelEl = document.querySelector('#pointSizeLabel');

const renderer = new THREE.WebGLRenderer({ antialias: true, logarithmicDepthBuffer: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight);
renderer.setClearColor(0x07110d, 1);
document.body.prepend(renderer.domElement);

const scene = new THREE.Scene();
scene.fog = new THREE.FogExp2(0x07110d, 0.016);

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

let sourcePositions;
let sourceColors;
let cloud;
let bounds;
let center = new THREE.Vector3();
let radius = 6;

const treeMeasurement = {
  trunkCenter: [-1.008, 0.304],
  groundZ: -1.404,
  breastHeightZ: -0.104,
  trunkRadius: 0.1117,
  crownCenter: [-0.928, 0.584],
  crownSpread: [2.3, 1.8],
  crownBaseZ: 1.0,
  treeTopZ: 3.42
};

const measurementOverlay = new THREE.Group();
measurementOverlay.name = 'tree-measurement-overlay';
scene.add(measurementOverlay);

function fmt(n, digits = 1) { return Number(n).toLocaleString('th-TH', { maximumFractionDigits: digits }); }

function setStatus(text) { statusEl.textContent = text; }

function frame(direction = new THREE.Vector3(1.2, .85, 1.2)) {
  camera.position.copy(center).add(direction.clone().normalize().multiplyScalar(radius * 2.25));
  controls.target.copy(center);
  controls.update();
}

function focusMeasurement(target, distance, direction = new THREE.Vector3(1, .55, 1)) {
  camera.position.copy(target).add(direction.normalize().multiplyScalar(distance));
  controls.target.copy(target);
  controls.update();
}

function makeLabel(text, color) {
  const canvas = document.createElement('canvas');
  canvas.width = 1024;
  canvas.height = 220;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = 'rgba(3, 10, 7, .9)';
  ctx.fillRect(8, 8, canvas.width - 16, canvas.height - 16);
  ctx.strokeStyle = color;
  ctx.lineWidth = 10;
  ctx.strokeRect(8, 8, canvas.width - 16, canvas.height - 16);
  ctx.fillStyle = '#ffffff';
  ctx.font = '700 62px system-ui, sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, canvas.width / 2, canvas.height / 2 + 2);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, depthTest: false, transparent: true }));
  sprite.scale.set(1.0, .215, 1);
  sprite.renderOrder = 20;
  return sprite;
}

function rebuildCloud() {
  const requested = Number(budgetEl.value);
  const total = sourcePositions.length / 3;
  const stride = Math.max(1, Math.ceil(total / requested));
  const count = Math.ceil(total / stride);
  const positions = new Float32Array(count * 3);
  const colors = new Uint8Array(count * 3);

  let out = 0;
  for (let i = 0; i < total; i += stride) {
    const p = i * 3;
    const c = i * 4;
    // Pix4D Z-up -> Three.js Y-up. Coordinates are already local in metres.
    positions[out * 3] = sourcePositions[p];
    positions[out * 3 + 1] = sourcePositions[p + 2];
    positions[out * 3 + 2] = -sourcePositions[p + 1];
    colors[out * 3] = sourceColors[c];
    colors[out * 3 + 1] = sourceColors[c + 1];
    colors[out * 3 + 2] = sourceColors[c + 2];
    out++;
  }

  cloud?.geometry.dispose();
  cloud?.material.dispose();
  if (cloud) scene.remove(cloud);

  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute('color', new THREE.Uint8BufferAttribute(colors, 3, true));
  geometry.computeBoundingBox();
  geometry.computeBoundingSphere();
  bounds = geometry.boundingBox;
  center.copy(bounds.getCenter(new THREE.Vector3()));
  radius = Math.max(geometry.boundingSphere.radius, 1);

  const material = new THREE.PointsMaterial({
    size: Number(pointSizeEl.value) * 0.008,
    sizeAttenuation: true,
    vertexColors: true,
    transparent: true,
    opacity: 0.98
  });
  cloud = new THREE.Points(geometry, material);
  scene.add(cloud);
  grid.position.y = bounds.min.y - 0.03;
  setStatus(`พร้อม · แสดง ${fmt(count, 0)} จาก ${fmt(total, 0)} จุด`);
}

function addTreeMeasurementOverlay() {
  const m = treeMeasurement;
  const toScene = (x, y, z) => new THREE.Vector3(x, z, -y);

  const dbhRing = new THREE.Mesh(
    new THREE.TorusGeometry(m.trunkRadius, 0.018, 10, 72),
    new THREE.MeshBasicMaterial({ color: 0xffc84f, depthTest: false })
  );
  dbhRing.rotation.x = Math.PI / 2;
  dbhRing.position.copy(toScene(m.trunkCenter[0], m.trunkCenter[1], m.breastHeightZ));
  dbhRing.renderOrder = 5;
  measurementOverlay.add(dbhRing);

  const sectionBand = new THREE.Mesh(
    new THREE.CylinderGeometry(m.trunkRadius + .035, m.trunkRadius + .035, .07, 64, 1, true),
    new THREE.MeshBasicMaterial({ color: 0xffc84f, transparent: true, opacity: .24, side: THREE.DoubleSide, depthTest: false, depthWrite: false })
  );
  sectionBand.position.copy(toScene(m.trunkCenter[0], m.trunkCenter[1], m.breastHeightZ));
  sectionBand.renderOrder = 4;
  measurementOverlay.add(sectionBand);

  const heightLine = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints([
      toScene(m.trunkCenter[0], m.trunkCenter[1], m.groundZ),
      toScene(m.trunkCenter[0], m.trunkCenter[1], m.breastHeightZ)
    ]),
    new THREE.LineBasicMaterial({ color: 0xffc84f, depthTest: false })
  );
  heightLine.renderOrder = 5;
  measurementOverlay.add(heightLine);

  const tape = new THREE.Mesh(
    new THREE.CylinderGeometry(.012, .012, 1.3, 12),
    new THREE.MeshBasicMaterial({ color: 0xffc84f, depthTest: false })
  );
  tape.position.copy(toScene(m.trunkCenter[0], m.trunkCenter[1], (m.groundZ + m.breastHeightZ) / 2));
  tape.renderOrder = 5;
  measurementOverlay.add(tape);

  const dbhLabel = makeLabel('สูง 1.30 ม. · รอบวง 0.70 ม.', '#ffc84f');
  dbhLabel.position.copy(toScene(m.trunkCenter[0], m.trunkCenter[1], m.breastHeightZ + .72));
  measurementOverlay.add(dbhLabel);

  const crownRing = new THREE.Mesh(
    new THREE.TorusGeometry(1, 0.015, 8, 96),
    new THREE.MeshBasicMaterial({ color: 0x75e69a, transparent: true, opacity: 0.95, depthTest: false })
  );
  const crownMidZ = (m.crownBaseZ + m.treeTopZ) / 2;
  const crownHalfHeight = (m.treeTopZ - m.crownBaseZ) / 2;
  crownRing.rotation.x = Math.PI / 2;
  crownRing.scale.set(m.crownSpread[0] / 2, m.crownSpread[1] / 2, 1);
  crownRing.position.copy(toScene(m.crownCenter[0], m.crownCenter[1], crownMidZ));
  crownRing.renderOrder = 4;
  measurementOverlay.add(crownRing);

  const crownVolume = new THREE.Mesh(
    new THREE.SphereGeometry(1, 28, 18),
    new THREE.MeshBasicMaterial({ color: 0x75e69a, wireframe: true, transparent: true, opacity: .32, depthTest: false, depthWrite: false })
  );
  crownVolume.scale.set(m.crownSpread[0] / 2, crownHalfHeight, m.crownSpread[1] / 2);
  crownVolume.position.copy(toScene(m.crownCenter[0], m.crownCenter[1], crownMidZ));
  crownVolume.renderOrder = 3;
  measurementOverlay.add(crownVolume);

  const crownAxes = [
    [toScene(m.crownCenter[0] - m.crownSpread[0] / 2, m.crownCenter[1], crownMidZ), toScene(m.crownCenter[0] + m.crownSpread[0] / 2, m.crownCenter[1], crownMidZ)],
    [toScene(m.crownCenter[0], m.crownCenter[1] - m.crownSpread[1] / 2, crownMidZ), toScene(m.crownCenter[0], m.crownCenter[1] + m.crownSpread[1] / 2, crownMidZ)]
  ];
  for (const axis of crownAxes) {
    const line = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(axis),
      new THREE.LineBasicMaterial({ color: 0x75e69a, depthTest: false })
    );
    line.renderOrder = 6;
    measurementOverlay.add(line);
    for (const point of axis) {
      const end = new THREE.Mesh(new THREE.SphereGeometry(.045, 12, 8), new THREE.MeshBasicMaterial({ color: 0x75e69a, depthTest: false }));
      end.position.copy(point);
      end.renderOrder = 6;
      measurementOverlay.add(end);
    }
  }

  const crownLabel = makeLabel('เรือนยอด 2.3 × 1.8 ม.', '#75e69a');
  crownLabel.position.copy(toScene(m.crownCenter[0], m.crownCenter[1], m.treeTopZ + .22));
  measurementOverlay.add(crownLabel);
}

function addMeasuredPointHighlights() {
  const m = treeMeasurement;
  const trunk = [];
  const crown = [];
  const rx = m.crownSpread[0] / 2;
  const ry = m.crownSpread[1] / 2;
  for (let i = 0; i < sourcePositions.length; i += 3) {
    const x = sourcePositions[i];
    const y = sourcePositions[i + 1];
    const z = sourcePositions[i + 2];
    const trunkR = Math.hypot(x - m.trunkCenter[0], y - m.trunkCenter[1]);
    if (Math.abs(z - m.breastHeightZ) <= .035 && trunkR <= m.trunkRadius + .06) {
      trunk.push(x, z, -y);
    }
    const ex = (x - m.crownCenter[0]) / rx;
    const ey = (y - m.crownCenter[1]) / ry;
    if (z >= m.crownBaseZ && z <= m.treeTopZ && ex * ex + ey * ey <= 1.08) {
      crown.push(x, z, -y);
    }
  }

  const addPoints = (positions, color, size, opacity, renderOrder) => {
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(positions), 3));
    const points = new THREE.Points(
      geometry,
      new THREE.PointsMaterial({ color, size, sizeAttenuation: true, transparent: true, opacity, depthTest: false, depthWrite: false })
    );
    points.renderOrder = renderOrder;
    measurementOverlay.add(points);
  };
  addPoints(crown, 0x63ff95, .017, .6, 7);
  addPoints(trunk, 0xffcf59, .024, .95, 8);
}

async function load() {
  const metadata = await fetch(`${DATA}metadata.json`).then(r => {
    if (!r.ok) throw new Error(`metadata HTTP ${r.status}`);
    return r.json();
  });
  document.querySelector('#points').textContent = fmt(metadata.points, 0);

  setStatus('กำลังโหลดตำแหน่งและสีประมาณ 60 MB…');
  const [positionsBuffer, colorsBuffer] = await Promise.all([
    fetch(`${DATA}positions.glbin`).then(r => {
      if (!r.ok) throw new Error(`positions HTTP ${r.status}`);
      return r.arrayBuffer();
    }),
    fetch(`${DATA}colors.glbin`).then(r => {
      if (!r.ok) throw new Error(`colors HTTP ${r.status}`);
      return r.arrayBuffer();
    })
  ]);
  sourcePositions = new Float32Array(positionsBuffer);
  sourceColors = new Uint8Array(colorsBuffer);
  addMeasuredPointHighlights();

  const posAttr = metadata.attributes.find(a => a.name === 'position');
  const dx = posAttr.max[0] - posAttr.min[0];
  const dy = posAttr.max[1] - posAttr.min[1];
  const dz = posAttr.max[2] - posAttr.min[2];
  document.querySelector('#footprint').textContent = `${fmt(dx)} × ${fmt(dy)} ม.`;
  document.querySelector('#height').textContent = `${fmt(dz)} ม.`;

  rebuildCloud();
  frame();
}

budgetEl.addEventListener('change', rebuildCloud);
pointSizeEl.addEventListener('input', () => {
  pointSizeLabelEl.textContent = Number(pointSizeEl.value).toFixed(1);
  if (cloud) cloud.material.size = Number(pointSizeEl.value) * 0.008;
});
document.querySelector('#reset').addEventListener('click', () => frame());
document.querySelector('#top').addEventListener('click', () => frame(new THREE.Vector3(0, 1, .001)));
document.querySelector('#front').addEventListener('click', () => frame(new THREE.Vector3(0, .08, 1)));
document.querySelector('#focusTrunk').addEventListener('click', () => {
  focusMeasurement(new THREE.Vector3(treeMeasurement.trunkCenter[0], treeMeasurement.breastHeightZ, -treeMeasurement.trunkCenter[1]), 2.2, new THREE.Vector3(1, .35, 1));
});
document.querySelector('#focusCrown').addEventListener('click', () => {
  const crownMidZ = (treeMeasurement.crownBaseZ + treeMeasurement.treeTopZ) / 2;
  focusMeasurement(new THREE.Vector3(treeMeasurement.crownCenter[0], crownMidZ, -treeMeasurement.crownCenter[1]), 4.2, new THREE.Vector3(1, .45, 1));
});
document.querySelector('#toggleMeasure').addEventListener('click', event => {
  measurementOverlay.visible = !measurementOverlay.visible;
  event.currentTarget.setAttribute('aria-pressed', String(measurementOverlay.visible));
  event.currentTarget.textContent = measurementOverlay.visible ? 'ซ่อนไฮไลท์' : 'แสดงไฮไลท์';
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

addTreeMeasurementOverlay();
load().catch(error => {
  console.error(error);
  setStatus(`เปิดข้อมูลไม่สำเร็จ: ${error.message}`);
});
