import * as THREE from 'three';
import { OrbitControls } from './vendor/OrbitControls.js';

const DATA = './data/';
const POSITION_CHUNKS = ['positions-00.glbin', 'positions-01.glbin', 'positions-02.glbin'];
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
let center = new THREE.Vector3();
let radius = 6;
let displayedPointCount = 0;

const fmt = (value, digits = 1) => Number(value).toLocaleString('th-TH', { maximumFractionDigits: digits });
const setStatus = (text) => { statusEl.textContent = text; };
const pointWorldSize = () => Number(pointSizeEl.value) * 0.008;

function frame(direction = new THREE.Vector3(1.2, 0.85, 1.2)) {
  camera.position.copy(center).add(direction.clone().normalize().multiplyScalar(radius * 2.25));
  controls.target.copy(center);
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
  setStatus(`พร้อม · แสดง ${fmt(displayedPointCount, 0)} จุด จาก sample Rayong`);
}

async function load() {
  const metadata = await fetch(`${DATA}metadata.json`).then((response) => {
    if (!response.ok) throw new Error(`metadata HTTP ${response.status}`);
    return response.json();
  });
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
