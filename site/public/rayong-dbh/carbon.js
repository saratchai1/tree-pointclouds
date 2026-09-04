(() => {
  'use strict';

  const DATA_VERSION = '?v=20260905-rayong-carbon-per-rai-v1';
  const SITE_CONFIG = {
    'site-001': { data: './data/site-001/', measurements: './data/site-001/measurements.json' },
    'site-002': { data: './data/site-002/', measurements: './data/site-002/measurements.json' },
  };
  const DEFAULTS = Object.freeze({
    woodDensity: 0.60,
    carbonFraction: 0.47,
    annualDiameterGrowthCm: 0.50,
    includeRoots: true,
    candidateSet: 'A',
  });

  const byId = (id) => document.getElementById(id);
  const fmt = (value, digits = 2) => Number.isFinite(Number(value))
    ? Number(value).toLocaleString('th-TH', { minimumFractionDigits: digits, maximumFractionDigits: digits })
    : '–';

  let currentSite = null;
  let metadata = null;
  let measurements = null;
  let robustSummary = null;
  let areaWasEdited = false;
  let loadToken = 0;

  function finite(value, fallback) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function readSettings() {
    return {
      areaRai: Math.max(0.0001, finite(byId('carbonAreaRai')?.value, 1)),
      woodDensity: Math.min(1.5, Math.max(0.1, finite(byId('carbonWoodDensity')?.value, DEFAULTS.woodDensity))),
      carbonFraction: Math.min(0.70, Math.max(0.10, finite(byId('carbonFraction')?.value, DEFAULTS.carbonFraction))),
      annualDiameterGrowthCm: Math.min(10, Math.max(0, finite(byId('carbonGrowth')?.value, DEFAULTS.annualDiameterGrowthCm))),
      includeRoots: Boolean(byId('carbonIncludeRoots')?.checked),
      candidateSet: byId('carbonCandidateSet')?.value || DEFAULTS.candidateSet,
    };
  }

  function bboxAreaRai(meta) {
    const box = meta?.boundingBox;
    if (!box?.min || !box?.max) return null;
    const width = Math.abs(finite(box.max[0], 0) - finite(box.min[0], 0));
    const height = Math.abs(finite(box.max[1], 0) - finite(box.min[1], 0));
    const areaM2 = width * height;
    return { width, height, areaM2, areaRai: areaM2 / 1600 };
  }

  function carbonStockTco2e(diameterCm, woodDensity, carbonFraction, includeRoots) {
    const diameter = Math.max(0.01, finite(diameterCm, 0.01));
    // Indicative mangrove screening equations only. Not yet registered/validated for T-VER ex-post use.
    const abovegroundBiomassKg = 0.251 * woodDensity * (diameter ** 2.46);
    const rootBiomassKg = includeRoots
      ? 0.199 * (woodDensity ** 0.899) * (diameter ** 2.22)
      : 0;
    const biomassKg = abovegroundBiomassKg + rootBiomassKg;
    return biomassKg * carbonFraction * (44 / 12) / 1000;
  }

  function selectedRecords(settings) {
    const records = Array.isArray(measurements?.trees) ? measurements.trees : [];
    const siteRobust = robustSummary?.sites?.[currentSite] || {};
    const strong = new Set(siteRobust.strongTreeIds || []);
    const robust = new Set(siteRobust.robustTreeIds || []);

    if (settings.candidateSet === 'STRONG') return records.filter((record) => strong.has(record.treeId));
    if (settings.candidateSet === 'ROBUST') return records.filter((record) => strong.has(record.treeId) || robust.has(record.treeId));
    if (settings.candidateSet === 'AB') return records.filter((record) => record.status === 'A_SMALL_STEM_INDICATIVE' || record.status === 'B_SMALL_STEM_LOW_CONFIDENCE');
    return records.filter((record) => record.status === 'A_SMALL_STEM_INDICATIVE');
  }

  function render() {
    if (!metadata || !measurements) return;
    const settings = readSettings();
    const chosen = selectedRecords(settings);
    let stockTco2e = 0;
    let annualGrossIncrementTco2e = 0;

    chosen.forEach((record) => {
      const diameter = finite(record.diameterCm, null);
      if (!(diameter > 0)) return;
      const current = carbonStockTco2e(diameter, settings.woodDensity, settings.carbonFraction, settings.includeRoots);
      const future = carbonStockTco2e(diameter + settings.annualDiameterGrowthCm, settings.woodDensity, settings.carbonFraction, settings.includeRoots);
      stockTco2e += current;
      annualGrossIncrementTco2e += Math.max(0, future - current);
    });

    const areaRai = settings.areaRai;
    byId('carbonPerRai').textContent = fmt(stockTco2e / areaRai, 3);
    byId('carbonAnnualPerRai').textContent = fmt(annualGrossIncrementTco2e / areaRai, 3);
    byId('carbonStemCount').textContent = `${chosen.length} / ${measurements.trees?.length || 0}`;
    byId('carbonTotalStock').textContent = `${fmt(stockTco2e, 3)} tCO₂e`;
    byId('carbonAreaUsed').textContent = `${fmt(areaRai, 3)} ไร่`;
    byId('carbonStatus').textContent = 'Stock / gross-increment proxy · ยังไม่ใช่เครดิตที่ออกได้';
  }

  function setAreaFromMetadata() {
    const area = bboxAreaRai(metadata);
    if (!area) return;
    if (!areaWasEdited) byId('carbonAreaRai').value = area.areaRai.toFixed(3);
    byId('carbonAreaNote').textContent = `ค่าเริ่มต้นใช้ bounding-box ${fmt(area.width, 1)} × ${fmt(area.height, 1)} ม. = ${fmt(area.areaRai, 3)} ไร่ เป็น area proxy เท่านั้น; ถ้ามีขอบเขตแปลงจริงให้ใส่พื้นที่จริงแทน.`;
  }

  async function loadJson(url, label) {
    const response = await fetch(`${url}${DATA_VERSION}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`${label} HTTP ${response.status}`);
    return response.json();
  }

  async function loadSite(site) {
    if (!SITE_CONFIG[site]) site = 'site-001';
    const token = ++loadToken;
    currentSite = site;
    areaWasEdited = false;
    byId('carbonStatus').textContent = `กำลังโหลด ${site}…`;
    try {
      const config = SITE_CONFIG[site];
      const [meta, measure, robust] = await Promise.all([
        loadJson(`${config.data}metadata.json`, 'metadata'),
        loadJson(config.measurements, 'measurements'),
        robustSummary ? Promise.resolve(robustSummary) : loadJson('./data/robust-summary.json', 'robust summary'),
      ]);
      if (token !== loadToken) return;
      metadata = meta;
      measurements = measure;
      robustSummary = robust;
      setAreaFromMetadata();
      render();
    } catch (error) {
      console.error(error);
      byId('carbonStatus').textContent = `คำนวณไม่ได้: ${error.message}`;
      byId('carbonPerRai').textContent = '–';
      byId('carbonAnnualPerRai').textContent = '–';
      byId('carbonStemCount').textContent = '–';
    }
  }

  function syncFromUrlOrActiveButton() {
    const active = document.querySelector('.site-button.active')?.dataset.site;
    const fromUrl = new URL(location.href).searchParams.get('site');
    const site = active || fromUrl || 'site-001';
    if (site !== currentSite) loadSite(site);
  }

  ['carbonWoodDensity', 'carbonFraction', 'carbonGrowth', 'carbonIncludeRoots', 'carbonCandidateSet'].forEach((id) => {
    byId(id)?.addEventListener('input', render);
    byId(id)?.addEventListener('change', render);
  });
  byId('carbonAreaRai')?.addEventListener('input', () => {
    areaWasEdited = true;
    render();
  });

  document.querySelectorAll('.site-button').forEach((button) => {
    button.addEventListener('click', () => queueMicrotask(() => loadSite(button.dataset.site)));
  });
  window.addEventListener('popstate', syncFromUrlOrActiveButton);

  const observer = new MutationObserver(syncFromUrlOrActiveButton);
  document.querySelectorAll('.site-button').forEach((button) => observer.observe(button, { attributes: true, attributeFilter: ['class'] }));

  syncFromUrlOrActiveButton();
})();
