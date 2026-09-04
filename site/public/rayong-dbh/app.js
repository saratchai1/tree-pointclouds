import * as THREE from 'three';
import { OrbitControls } from '../viewer/vendor/OrbitControls.js';

const DATA_VERSION = '?v=20260904-rayong-small-stem-v1';
const POSITION_CHUNKS = ['positions-00.glbin', 'positions-01.glbin', 'positions-02.glbin'];
const DATASETS = {
  'site-001': { label: 'Rayong site-001', data: './data/site-001/', measurements: './data/site-001/measurements.json' },
  'site-002': { label: 'Rayong site-002', data: './data/site-002/', measurements: './data/site-002/measurements.json' },
};
const ids = ['status','budget','pointSize','pointSizeLabel','medianDbh','iqrDbh','strongDbh','sensitivityDbh','rawPoints','samplePoints','stride','footprint','visibleCount','treeSearch','candidateFilter','selectedTree','selectedStatus','selectedBadge','selectedDbh','selectedCirc','selectedBreast','selectedShell','selectedBands','selectedVerticality','selectedQa','histogram','distributionLabel','listCount','treeList','toggleMarkers','toggleRings','toggleCloud','reset','top','side','showAll','focusStrong','site001','site002'];
const els = Object.fromEntries(ids.map((id) => [id, document.getElementById(id)]));

const renderer = new THREE.WebGLRenderer({ antialias: true, logarithmicDepthBuffer: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight);
renderer.setClearColor(0x06100c, 1);
document.body.prepend(renderer.domElement);
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(52, innerWidth / innerHeight, 0.01, 800);
camera.up.set(0, 1, 0);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.07;
controls.screenSpacePanning = true;
scene.add(new THREE.HemisphereLight(0xdaf6e3, 0x13251a, 1.15));
const grid = new THREE.GridHelper(20, 20, 0x3d7050, 0x193726);
grid.material.opacity = 0.42;
grid.material.transparent = true;
scene.add(grid);
const markerLayer = new THREE.Group();
const ringLayer = new THREE.Group();
const selectionLayer = new THREE.Group();
scene.add(markerLayer, ringLayer, selectionLayer);

const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
const hitTargets = [];
const cache = new Map();
const recordGroups = new Map();
const strongIds = new Set();
const robustIds = new Set();
let loadToken = 0;
let currentSite = 'site-001';
let metadata = null;
let measurements = null;
let robustSummary = null;
let sensitivitySummary = null;
let sourcePositions = null;
let sourceColors = null;
let cloud = null;
let center = new THREE.Vector3();
let radius = 5;
let records = [];
let visibleRecords = [];
let selectedTreeId = null;
let pointerStart = null;

const fmt = (v, d = 1) => v == null || Number.isNaN(Number(v)) ? '–' : Number(v).toLocaleString('th-TH', { maximumFractionDigits: d });
const setStatus = (text) => { els.status.textContent = text; };
const toScene = (x, y, z) => new THREE.Vector3(x, z, -y);
const pointWorldSize = () => Number(els.pointSize.value) * 0.008;
const circumferenceCm = (r) => Number(r.diameterCm) * Math.PI;
function disposeObject(object) {
  object.traverse((child) => {
    child.geometry?.dispose?.();
    const mats = child.material ? (Array.isArray(child.material) ? child.material : [child.material]) : [];
    mats.forEach((mat) => { mat.map?.dispose?.(); mat.dispose?.(); });
  });
}
function clearLayer(layer) { for (const child of [...layer.children]) { layer.remove(child); disposeObject(child); } }
function evidenceClass(r) {
  if (strongIds.has(r.treeId)) return 'strong';
  if (robustIds.has(r.treeId)) return 'robust';
  if (r.status === 'A_SMALL_STEM_INDICATIVE') return 'a';
  if (r.status === 'B_SMALL_STEM_LOW_CONFIDENCE') return 'b';
  return 'c';
}
function evidenceLabel(r) {
  const c = evidenceClass(r);
  return c === 'strong' ? 'Strong evidence' : c === 'robust' ? 'Robust evidence' : c === 'a' ? 'A · Indicative' : c === 'b' ? 'B · Low confidence' : 'Reject / not usable';
}
function evidenceColor(r) {
  const c = evidenceClass(r);
  return c === 'strong' ? 0x6edcff : c === 'robust' ? 0x6fe0a7 : c === 'a' ? 0xffd166 : c === 'b' ? 0xff9e64 : 0xf27a6a;
}
function makeLocatorSprite(record) {
  const canvas = document.createElement('canvas'); canvas.width = 128; canvas.height = 128;
  const ctx = canvas.getContext('2d');
  ctx.beginPath(); ctx.arc(64, 64, 36, 0, Math.PI * 2); ctx.strokeStyle = 'rgba(0,0,0,.9)'; ctx.lineWidth = 25; ctx.stroke();
  ctx.beginPath(); ctx.arc(64, 64, 36, 0, Math.PI * 2); ctx.strokeStyle = `#${evidenceColor(record).toString(16).padStart(6, '0')}`; ctx.lineWidth = 14; ctx.stroke();
  const texture = new THREE.CanvasTexture(canvas); texture.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, transparent: true, depthTest: false, depthWrite: false }));
  sprite.scale.set(0.55, 0.55, 1); sprite.renderOrder = 12; sprite.userData.treeId = record.treeId; return sprite;
}
function addRecordOverlay(record) {
  const pom = toScene(record.x, record.y, record.groundZ + 1.30);
  const group = new THREE.Group(); const sprite = makeLocatorSprite(record); sprite.position.copy(pom); group.add(sprite); markerLayer.add(group); hitTargets.push(sprite);
  const r = Math.max(Number(record.radiusM) || Number(record.diameterCm) / 200, 0.009);
  const torus = new THREE.Mesh(new THREE.TorusGeometry(r, Math.max(0.006, Math.min(0.012, r * 0.17)), 8, 32), new THREE.MeshBasicMaterial({ color: evidenceColor(record), transparent: true, opacity: 0.84, depthTest: false, depthWrite: false }));
  torus.rotation.x = Math.PI / 2; torus.position.copy(pom); torus.renderOrder = 10;
  const ringGroup = new THREE.Group(); ringGroup.add(torus); ringLayer.add(ringGroup); recordGroups.set(record.treeId, { marker: group, ring: ringGroup });
}
function selectedCylinder(record) {
  clearLayer(selectionLayer);
  const color = evidenceColor(record); const radiusM = Math.max(Number(record.radiusM) || Number(record.diameterCm) / 200, 0.009);
  const ground = toScene(record.x, record.y, record.groundZ); const pom = toScene(record.x, record.y, record.groundZ + 1.30);
  const axis = new THREE.Line(new THREE.BufferGeometry().setFromPoints([ground, pom]), new THREE.LineDashedMaterial({ color, dashSize: 0.08, gapSize: 0.05, depthTest: false }));
  axis.computeLineDistances(); axis.renderOrder = 20; selectionLayer.add(axis);
  const ring = new THREE.Mesh(new THREE.TorusGeometry(radiusM, Math.max(0.012, Math.min(0.02, radiusM * 0.28)), 10, 48), new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: .96, depthTest: false, depthWrite: false }));
  ring.rotation.x = Math.PI / 2; ring.position.copy(pom); ring.renderOrder = 22; selectionLayer.add(ring);
  const cylinder = new THREE.Mesh(new THREE.CylinderGeometry(radiusM, radiusM, 1.25, 24, 1, true), new THREE.MeshBasicMaterial({ color, transparent: true, opacity: .12, wireframe: true, depthTest: false }));
  cylinder.position.copy(toScene(record.x, record.y, record.groundZ + 1.125)); cylinder.renderOrder = 18; selectionLayer.add(cylinder);
  if (sourcePositions) {
    const local = [];
    for (let i = 0; i < sourcePositions.length; i += 3) {
      const dx = sourcePositions[i] - record.x, dy = sourcePositions[i + 1] - record.y, dz = sourcePositions[i + 2] - record.groundZ;
      if (dx * dx + dy * dy <= 0.0784 && dz >= 0.25 && dz <= 2.0) local.push(sourcePositions[i], sourcePositions[i + 2], -sourcePositions[i + 1]);
    }
    if (local.length) {
      const geo = new THREE.BufferGeometry(); geo.setAttribute('position', new THREE.Float32BufferAttribute(local, 3));
      const pts = new THREE.Points(geo, new THREE.PointsMaterial({ color: 0xffffff, size: 0.022, transparent: true, opacity: .82, depthTest: false })); pts.renderOrder = 21; selectionLayer.add(pts);
    }
  }
}
function frame(direction = new THREE.Vector3(1.1, .8, 1.2)) {
  camera.position.copy(center).add(direction.clone().normalize().multiplyScalar(radius * 2.2)); controls.target.copy(center); controls.update();
}
function frameRecords(targetRecords) {
  const box = new THREE.Box3(); targetRecords.forEach((r) => box.expandByPoint(toScene(r.x, r.y, r.groundZ + 1.3)));
  if (box.isEmpty()) return frame();
  const target = box.getCenter(new THREE.Vector3()); const r = Math.max(box.getBoundingSphere(new THREE.Sphere()).radius, 1.2);
  camera.position.copy(target).add(new THREE.Vector3(1.15, .9, 1.15).normalize().multiplyScalar(r * 2.6)); controls.target.copy(target); controls.update();
}
function updateSelected(record) {
  const cls = evidenceClass(record);
  els.selectedTree.textContent = record.treeId; els.selectedStatus.textContent = evidenceLabel(record); els.selectedBadge.textContent = cls.toUpperCase(); els.selectedBadge.className = `selected-badge ${cls}`;
  els.selectedDbh.textContent = `${fmt(record.diameterCm, 1)} cm`; els.selectedCirc.textContent = `${fmt(circumferenceCm(record), 1)} cm`; els.selectedBreast.textContent = fmt(record.breastSupportPoints, 0); els.selectedShell.textContent = fmt(record.shellPoints, 0); els.selectedBands.textContent = `${fmt(record.persistentBands, 0)} / 5`; els.selectedVerticality.textContent = fmt(record.verticality, 3);
  els.selectedQa.textContent = `axis residual ${fmt(Number(record.axisCenterResidualM) * 100, 1)} cm · ground Z ${fmt(record.groundZ, 2)} · วงใน 3D เป็น locator ที่ 1.30 ม. และทรงกระบอกเป็นภาพช่วยอ่าน geometry จาก sample ไม่ใช่วง full-LAS.`;
}
function focusTree(record, { updateUrl = true } = {}) {
  selectedTreeId = record.treeId; selectedCylinder(record); const target = toScene(record.x, record.y, record.groundZ + 1.3);
  camera.position.copy(target).add(new THREE.Vector3(1, .55, 1).normalize().multiplyScalar(2.4)); controls.target.copy(target); controls.update(); updateSelected(record);
  document.querySelectorAll('.tree-row').forEach((row) => row.classList.toggle('selected', row.dataset.treeId === record.treeId));
  document.querySelector(`.tree-row[data-tree-id="${CSS.escape(record.treeId)}"]`)?.scrollIntoView({ block: 'nearest' }); setStatus(`${record.treeId} · DBH ${fmt(record.diameterCm, 1)} ซม. · ${evidenceLabel(record)}`);
  if (updateUrl) { const url = new URL(location.href); url.searchParams.set('site', currentSite); url.searchParams.set('tree', record.treeId); history.replaceState(null, '', url); }
}
function filterMatch(r, filter) {
  const cls = evidenceClass(r); if (filter === 'ALL') return true; if (filter === 'STRONG') return cls === 'strong'; if (filter === 'ROBUST') return cls === 'strong' || cls === 'robust'; if (filter === 'A') return r.status === 'A_SMALL_STEM_INDICATIVE'; if (filter === 'B') return r.status === 'B_SMALL_STEM_LOW_CONFIDENCE'; if (filter === 'C') return r.status === 'C_REJECT'; return true;
}
function renderTreeList() {
  els.treeList.replaceChildren(); els.listCount.textContent = `${visibleRecords.length} รายการ`;
  visibleRecords.forEach((record) => {
    const cls = evidenceClass(record); const row = document.createElement('button'); row.type = 'button'; row.className = `tree-row status-${cls}`; row.dataset.treeId = record.treeId; row.classList.toggle('selected', record.treeId === selectedTreeId);
    const id = document.createElement('span'); id.className = 'tree-id'; id.textContent = record.treeId.split('-').at(-1);
    const middle = document.createElement('span'); const b = document.createElement('b'); b.textContent = `DBH ${fmt(record.diameterCm, 1)} cm · รอบวง ${fmt(circumferenceCm(record), 1)} cm`; const small = document.createElement('small'); small.textContent = `${fmt(record.breastSupportPoints, 0)} จุด @1.30m · ${fmt(record.shellPoints, 0)} shell · ${record.persistentBands}/5 bands`; middle.append(b, small);
    const tag = document.createElement('span'); tag.className = 'tree-confidence'; tag.textContent = cls === 'strong' ? 'STRONG' : cls === 'robust' ? 'ROBUST' : cls.toUpperCase(); row.append(id, middle, tag); row.addEventListener('click', () => focusTree(record)); els.treeList.append(row);
  });
}
function renderHistogram() {
  const bins = [0, 4, 6, 8, 10, Infinity], labels = ['<4', '4–6', '6–8', '8–10', '>10'], counts = Array(5).fill(0);
  visibleRecords.forEach((r) => { const d = Number(r.diameterCm); for (let i = 0; i < 5; i += 1) if (d >= bins[i] && d < bins[i + 1]) { counts[i] += 1; break; } });
  const max = Math.max(...counts, 1); els.histogram.replaceChildren();
  counts.forEach((count, i) => { const wrap = document.createElement('div'); wrap.className = 'bar-wrap'; const area = document.createElement('div'); area.className = 'bar-area'; const bar = document.createElement('div'); bar.className = 'bar'; bar.style.height = `${Math.max(3, count / max * 43)}px`; bar.title = `${labels[i]} cm: ${count}`; area.append(bar); const label = document.createElement('small'); label.textContent = `${labels[i]} (${count})`; wrap.append(area, label); els.histogram.append(wrap); });
  els.distributionLabel.textContent = els.candidateFilter.options[els.candidateFilter.selectedIndex].text;
}
function applyFilter({ frameAfter = false } = {}) {
  const filter = els.candidateFilter.value, q = els.treeSearch.value.trim().toUpperCase(); visibleRecords = records.filter((r) => filterMatch(r, filter) && (!q || r.treeId.includes(q))); const ids = new Set(visibleRecords.map((r) => r.treeId));
  records.forEach((r) => { const g = recordGroups.get(r.treeId), visible = ids.has(r.treeId); if (g?.marker) g.marker.visible = visible; if (g?.ring) g.ring.visible = visible; });
  els.visibleCount.textContent = `${visibleRecords.length} / ${records.length}`; renderTreeList(); renderHistogram();
  if (selectedTreeId && !ids.has(selectedTreeId)) { clearLayer(selectionLayer); selectedTreeId = null; }
  if (frameAfter) frameRecords(visibleRecords);
}
function rebuildCloud() {
  if (!sourcePositions || !sourceColors) return;
  const requested = Number(els.budget.value), total = sourcePositions.length / 3, stride = Math.max(1, Math.ceil(total / requested)), count = Math.ceil(total / stride);
  const positions = new Float32Array(count * 3), colors = new Uint8Array(count * 3); let out = 0;
  for (let i = 0; i < total; i += stride) { const p = i * 3, c = i * 4; positions[out * 3] = sourcePositions[p]; positions[out * 3 + 1] = sourcePositions[p + 2]; positions[out * 3 + 2] = -sourcePositions[p + 1]; colors[out * 3] = sourceColors[c]; colors[out * 3 + 1] = sourceColors[c + 1]; colors[out * 3 + 2] = sourceColors[c + 2]; out += 1; }
  if (cloud) { scene.remove(cloud); cloud.geometry.dispose(); cloud.material.dispose(); }
  const geo = new THREE.BufferGeometry(); geo.setAttribute('position', new THREE.BufferAttribute(positions, 3)); geo.setAttribute('color', new THREE.Uint8BufferAttribute(colors, 3, true)); geo.computeBoundingBox(); geo.computeBoundingSphere(); center.copy(geo.boundingBox.getCenter(new THREE.Vector3())); radius = Math.max(geo.boundingSphere.radius, 1);
  cloud = new THREE.Points(geo, new THREE.PointsMaterial({ size: pointWorldSize(), sizeAttenuation: true, vertexColors: true, transparent: true, opacity: .97 })); cloud.visible = els.toggleCloud.getAttribute('aria-pressed') === 'true'; scene.add(cloud); grid.scale.setScalar(Math.max(radius * 2, 20) / 20); grid.position.y = geo.boundingBox.min.y - .03;
  setStatus(`${DATASETS[currentSite].label} · แสดง ${fmt(count, 0)} จุด · ${visibleRecords.length} candidates ตาม filter`);
}
async function loadJson(url, label) { const res = await fetch(`${url}${DATA_VERSION}`, { cache: 'no-store' }); if (!res.ok) throw new Error(`${label} HTTP ${res.status}`); return res.json(); }
async function loadBinary(url, label) { const res = await fetch(`${url}${DATA_VERSION}`); if (!res.ok) throw new Error(`${label} HTTP ${res.status}`); return res.arrayBuffer(); }
async function datasetPayload(site) {
  if (cache.has(site)) return cache.get(site); const cfg = DATASETS[site];
  const [meta, measure, posBuffers, colorsBuffer] = await Promise.all([loadJson(`${cfg.data}metadata.json`, 'metadata'), loadJson(cfg.measurements, 'measurements'), Promise.all(POSITION_CHUNKS.map((name) => loadBinary(`${cfg.data}${name}`, name))), loadBinary(`${cfg.data}colors.glbin`, 'colors')]);
  const length = posBuffers.reduce((sum, b) => sum + b.byteLength, 0), merged = new Uint8Array(length); let offset = 0; posBuffers.forEach((b) => { merged.set(new Uint8Array(b), offset); offset += b.byteLength; });
  const payload = { meta, measure, positions: new Float32Array(merged.buffer), colors: new Uint8Array(colorsBuffer) }; cache.set(site, payload); return payload;
}
function updateSummaryCards() {
  const siteRobust = robustSummary.sites[currentSite], siteSensitivity = sensitivitySummary.sites[currentSite], a = measurements.aStats;
  els.medianDbh.textContent = `${fmt(a.medianCm, 1)} cm`; els.iqrDbh.textContent = `${fmt(a.p25Cm, 1)}–${fmt(a.p75Cm, 1)} cm`; els.strongDbh.textContent = siteRobust?.strong?.medianCm == null ? '–' : `${fmt(siteRobust.strong.medianCm, 1)} cm (n=${siteRobust.strong.n})`; els.sensitivityDbh.textContent = `${fmt(siteSensitivity.minVariantMedianCm, 2)}–${fmt(siteSensitivity.maxVariantMedianCm, 2)} cm`;
  els.rawPoints.textContent = fmt(metadata.sourcePointCount, 0); els.samplePoints.textContent = fmt(metadata.points, 0); els.stride.textContent = `1 / ${fmt(metadata.samplingStride, 0)}`; const b = metadata.boundingBox; els.footprint.textContent = `${fmt(b.max[0] - b.min[0], 0)}×${fmt(b.max[1] - b.min[1], 0)} m`;
}
async function switchSite(site, requestedTree = null) {
  if (!DATASETS[site]) site = 'site-001'; currentSite = site; const token = ++loadToken; document.querySelectorAll('.site-button').forEach((b) => b.classList.toggle('active', b.dataset.site === site)); setStatus(`กำลังโหลด ${DATASETS[site].label}…`);
  selectedTreeId = null; clearLayer(selectionLayer); clearLayer(markerLayer); clearLayer(ringLayer); hitTargets.splice(0); recordGroups.clear(); strongIds.clear(); robustIds.clear(); if (cloud) { scene.remove(cloud); cloud.geometry.dispose(); cloud.material.dispose(); cloud = null; } sourcePositions = null; sourceColors = null;
  try {
    if (!robustSummary || !sensitivitySummary) [robustSummary, sensitivitySummary] = await Promise.all([loadJson('./data/robust-summary.json', 'robust summary'), loadJson('./data/sensitivity-summary.json', 'sensitivity summary')]);
    const payload = await datasetPayload(site); if (token !== loadToken) return; metadata = payload.meta; measurements = payload.measure; sourcePositions = payload.positions; sourceColors = payload.colors; records = Array.isArray(measurements.trees) ? measurements.trees : [];
    const siteRobust = robustSummary.sites[site] ?? {}; (siteRobust.robustTreeIds ?? []).forEach((id) => robustIds.add(id)); (siteRobust.strongTreeIds ?? []).forEach((id) => strongIds.add(id)); records.forEach(addRecordOverlay); updateSummaryCards(); els.candidateFilter.value = 'STRONG'; els.treeSearch.value = ''; applyFilter(); rebuildCloud();
    const wanted = requestedTree && records.find((r) => r.treeId === requestedTree); if (wanted) { els.candidateFilter.value = 'ALL'; applyFilter(); focusTree(wanted, { updateUrl: false }); } else frameRecords(visibleRecords.length ? visibleRecords : records);
    const url = new URL(location.href); url.searchParams.set('site', site); if (!wanted) url.searchParams.delete('tree'); history.replaceState(null, '', url);
  } catch (error) { console.error(error); setStatus(`โหลดไม่สำเร็จ: ${error.message}`); }
}
function setPressed(button, value) { button.setAttribute('aria-pressed', String(value)); }
els.toggleMarkers.addEventListener('click', () => { const next = els.toggleMarkers.getAttribute('aria-pressed') !== 'true'; setPressed(els.toggleMarkers, next); markerLayer.visible = next; });
els.toggleRings.addEventListener('click', () => { const next = els.toggleRings.getAttribute('aria-pressed') !== 'true'; setPressed(els.toggleRings, next); ringLayer.visible = next; });
els.toggleCloud.addEventListener('click', () => { const next = els.toggleCloud.getAttribute('aria-pressed') !== 'true'; setPressed(els.toggleCloud, next); if (cloud) cloud.visible = next; });
els.budget.addEventListener('change', rebuildCloud);
els.pointSize.addEventListener('input', () => { els.pointSizeLabel.textContent = Number(els.pointSize.value).toFixed(1); if (cloud) cloud.material.size = pointWorldSize(); });
els.candidateFilter.addEventListener('change', () => applyFilter({ frameAfter: true })); els.treeSearch.addEventListener('input', () => applyFilter());
els.reset.addEventListener('click', () => frame()); els.top.addEventListener('click', () => frame(new THREE.Vector3(0, 1, .01))); els.side.addEventListener('click', () => frame(new THREE.Vector3(1, .15, 0)));
els.showAll.addEventListener('click', () => { els.candidateFilter.value = 'ALL'; els.treeSearch.value = ''; applyFilter({ frameAfter: true }); }); els.focusStrong.addEventListener('click', () => { els.candidateFilter.value = 'STRONG'; els.treeSearch.value = ''; applyFilter({ frameAfter: true }); });
els.site001.addEventListener('click', () => switchSite('site-001')); els.site002.addEventListener('click', () => switchSite('site-002'));
renderer.domElement.addEventListener('pointerdown', (e) => { pointerStart = { x: e.clientX, y: e.clientY }; });
renderer.domElement.addEventListener('pointerup', (e) => {
  if (!pointerStart || Math.hypot(e.clientX - pointerStart.x, e.clientY - pointerStart.y) > 5) return; const rect = renderer.domElement.getBoundingClientRect(); pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1; pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1; raycaster.setFromCamera(pointer, camera);
  const hit = raycaster.intersectObjects(hitTargets, false).find((h) => h.object.visible && h.object.parent?.visible && markerLayer.visible); if (hit) { const record = records.find((r) => r.treeId === hit.object.userData.treeId); if (record) focusTree(record); }
});
window.addEventListener('resize', () => { camera.aspect = innerWidth / innerHeight; camera.updateProjectionMatrix(); renderer.setSize(innerWidth, innerHeight); });
function animate() { requestAnimationFrame(animate); controls.update(); renderer.render(scene, camera); } animate();
const params = new URLSearchParams(location.search), initialSite = DATASETS[params.get('site')] ? params.get('site') : 'site-001', initialTree = params.get('tree')?.toUpperCase() ?? null;
switchSite(initialSite, initialTree);
