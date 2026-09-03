(() => {
  "use strict";

  const ASSUMPTION_VERSION = "indicative-carbon-v0.1.0";
  const SETTINGS_KEY = "tree-pointclouds-carbon-settings-v1";
  const DEFAULTS = Object.freeze({
    woodDensityLow: 0.40,
    woodDensityMid: 0.60,
    woodDensityHigh: 0.80,
    carbonFraction: 0.47,
    annualDiameterGrowth: 0.50,
    includeRoots: true,
    allowReviewCandidate: false,
  });

  const byId = (id) => document.getElementById(id);
  let records = [];
  let recordsById = new Map();
  let currentTreeId = null;

  function finiteNumber(value, fallback = null) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, value));
  }

  function formatNumber(value, digits = 2) {
    if (!Number.isFinite(value)) return "—";
    return value.toLocaleString("th-TH", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }

  function formatMassKg(value) {
    if (!Number.isFinite(value)) return "—";
    if (Math.abs(value) >= 1000) return `${formatNumber(value / 1000, 3)} ตัน`;
    return `${formatNumber(value, 1)} กก.`;
  }

  function formatTco2(value, digits = 3) {
    return Number.isFinite(value) ? `${formatNumber(value, digits)} tCO₂e` : "—";
  }

  function csvCell(value) {
    if (value == null) return "";
    const text = String(value);
    return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
  }

  function readSettings() {
    const low = clamp(finiteNumber(byId("woodDensityLow")?.value, DEFAULTS.woodDensityLow), 0.10, 1.50);
    const mid = clamp(finiteNumber(byId("woodDensityMid")?.value, DEFAULTS.woodDensityMid), 0.10, 1.50);
    const high = clamp(finiteNumber(byId("woodDensityHigh")?.value, DEFAULTS.woodDensityHigh), 0.10, 1.50);
    const orderedDensity = [low, mid, high].sort((left, right) => left - right);
    return {
      woodDensityLow: orderedDensity[0],
      woodDensityMid: orderedDensity[1],
      woodDensityHigh: orderedDensity[2],
      carbonFraction: clamp(finiteNumber(byId("carbonFraction")?.value, DEFAULTS.carbonFraction), 0.10, 0.70),
      annualDiameterGrowth: clamp(finiteNumber(byId("annualDiameterGrowth")?.value, DEFAULTS.annualDiameterGrowth), 0, 10),
      includeRoots: Boolean(byId("includeRoots")?.checked),
      allowReviewCandidate: Boolean(byId("allowReviewCandidate")?.checked),
    };
  }

  function writeSettings(settings) {
    byId("woodDensityLow").value = settings.woodDensityLow.toFixed(2);
    byId("woodDensityMid").value = settings.woodDensityMid.toFixed(2);
    byId("woodDensityHigh").value = settings.woodDensityHigh.toFixed(2);
    byId("carbonFraction").value = settings.carbonFraction.toFixed(2);
    byId("annualDiameterGrowth").value = settings.annualDiameterGrowth.toFixed(2);
    byId("includeRoots").checked = settings.includeRoots;
    byId("allowReviewCandidate").checked = settings.allowReviewCandidate;
  }

  function restoreSettings() {
    let settings = { ...DEFAULTS };
    try {
      const stored = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "null");
      if (stored && typeof stored === "object") settings = { ...settings, ...stored };
    } catch (error) {
      console.warn("Carbon settings could not be restored", error);
    }
    writeSettings(settings);
  }

  function persistSettings() {
    try {
      localStorage.setItem(SETTINGS_KEY, JSON.stringify(readSettings()));
    } catch (error) {
      console.warn("Carbon settings could not be saved", error);
    }
  }

  function getDiameterSource(record, settings) {
    const automaticDiameter = finiteNumber(record?.diameter_cm);
    if (automaticDiameter != null && automaticDiameter > 0) {
      return {
        diameterCm: automaticDiameter,
        heightAglM: finiteNumber(record.measurement_height_agl_m),
        source: record.measurement_kind || record.status || "AUTOMATIC",
        exploratory: false,
      };
    }

    const reviewDiameter = finiteNumber(record?.best_review_candidate?.diameter_cm);
    if (settings.allowReviewCandidate && reviewDiameter != null && reviewDiameter > 0) {
      return {
        diameterCm: reviewDiameter,
        heightAglM: finiteNumber(record.best_review_candidate?.height_agl_m ?? record.candidate_height_agl_m),
        source: "BEST_REVIEW_CANDIDATE",
        exploratory: true,
      };
    }

    return null;
  }

  function diameterScenarioMarginCm(record, source) {
    const fitDiameterCm = Math.abs(finiteNumber(record?.fit_rmse_m, 0)) * 200;
    const stabilityDiameterCm = Math.abs(finiteNumber(record?.radius_stability_mad_m, 0)) * 200;
    return Math.max(0.50, fitDiameterCm, stabilityDiameterCm);
  }

  function calculateStock(diameterCm, woodDensity, carbonFraction, includeRoots) {
    const diameter = Math.max(0.01, diameterCm);
    const abovegroundBiomassKg = 0.251 * woodDensity * (diameter ** 2.46);
    const rootBiomassKg = includeRoots
      ? 0.199 * (woodDensity ** 0.899) * (diameter ** 2.22)
      : 0;
    const totalBiomassKg = abovegroundBiomassKg + rootBiomassKg;
    const carbonKg = totalBiomassKg * carbonFraction;
    const co2eTonnes = carbonKg * (44 / 12) / 1000;
    return {
      abovegroundBiomassKg,
      rootBiomassKg,
      totalBiomassKg,
      carbonKg,
      co2eTonnes,
    };
  }

  function estimateRecord(record, settings) {
    const source = getDiameterSource(record, settings);
    if (!source) return null;

    const marginCm = diameterScenarioMarginCm(record, source);
    const lowerDiameterCm = Math.max(0.10, source.diameterCm - marginCm);
    const upperDiameterCm = source.diameterCm + marginCm;
    const central = calculateStock(
      source.diameterCm,
      settings.woodDensityMid,
      settings.carbonFraction,
      settings.includeRoots,
    );
    const low = calculateStock(
      lowerDiameterCm,
      settings.woodDensityLow,
      settings.carbonFraction,
      settings.includeRoots,
    );
    const high = calculateStock(
      upperDiameterCm,
      settings.woodDensityHigh,
      settings.carbonFraction,
      settings.includeRoots,
    );
    const future = calculateStock(
      source.diameterCm + settings.annualDiameterGrowth,
      settings.woodDensityMid,
      settings.carbonFraction,
      settings.includeRoots,
    );

    return {
      record,
      source,
      marginCm,
      lowerDiameterCm,
      upperDiameterCm,
      central,
      low,
      high,
      annualGrossIncrementTco2e: Math.max(0, future.co2eTonnes - central.co2eTonnes),
    };
  }

  function aggregateEstimates(settings) {
    const aggregate = {
      count: 0,
      automaticCount: 0,
      reviewCandidateCount: 0,
      centralTco2e: 0,
      lowTco2e: 0,
      highTco2e: 0,
      annualGrossIncrementTco2e: 0,
    };

    records.forEach((record) => {
      const estimate = estimateRecord(record, settings);
      if (!estimate) return;
      aggregate.count += 1;
      if (estimate.source.exploratory) aggregate.reviewCandidateCount += 1;
      else aggregate.automaticCount += 1;
      aggregate.centralTco2e += estimate.central.co2eTonnes;
      aggregate.lowTco2e += estimate.low.co2eTonnes;
      aggregate.highTco2e += estimate.high.co2eTonnes;
      aggregate.annualGrossIncrementTco2e += estimate.annualGrossIncrementTco2e;
    });

    return aggregate;
  }

  function qualityLabel(record, estimate) {
    if (!estimate) return "ไม่มีเส้นผ่านศูนย์กลางสำหรับคำนวณ";
    if (estimate.source.exploratory) return "Exploratory · ใช้ best review candidate";
    if (record.field_verified) return "Indicative · field verified";
    if (record.status === "STANDARD_DBH") return "Indicative · DBH geometry accepted, not field verified";
    return "Exploratory · alternative POM, not field verified";
  }

  function buildWarnings(record, estimate, settings) {
    const warnings = [];
    if (!estimate) {
      warnings.push("ต้นนี้ไม่มีค่า diameter ที่ระบบอนุมัติให้ปล่อยอัตโนมัติ จึงยังไม่แสดงค่าคาร์บอน");
      if (!settings.allowReviewCandidate && record?.best_review_candidate?.diameter_cm) {
        warnings.push("เปิดตัวเลือก best review candidate ได้เพื่อทดลอง แต่ค่าดังกล่าวยังไม่ผ่านการตรวจด้วยคน");
      }
      return warnings;
    }

    if (!record.field_verified) warnings.push("ผล geometry และชนิดต้นไม้ยังไม่ได้ยืนยันภาคสนาม");
    if (estimate.source.exploratory) warnings.push("กำลังใช้หน้าตัด best review candidate ซึ่งไม่ใช่ผลวัดที่ระบบอนุมัติ");
    if (record.status === "ALTERNATIVE_POM") {
      warnings.push(`เส้นผ่านศูนย์กลางวัดที่ ${formatNumber(estimate.source.heightAglM, 2)} ม. AGL ไม่ใช่ DBH 1.30 ม. หรือ D₍R0.3₎ มาตรฐาน`);
    }
    warnings.push("ยังไม่ทราบชนิดไม้ จึงใช้ช่วง wood density ที่ผู้ใช้กำหนดแทนค่ารายชนิด");
    warnings.push("ช่วงต่ำ–สูงเป็น scenario range ไม่ใช่ช่วงความเชื่อมั่นทางสถิติ");
    warnings.push("ไม่รวมคาร์บอนในดิน ไม้ตาย เศษซาก baseline, leakage, project emissions และ buffer");
    return warnings;
  }

  function renderSelected(record, settings) {
    const estimate = estimateRecord(record, settings);
    const statusNode = byId("carbonEstimateStatus");
    const warningsNode = byId("carbonWarnings");

    byId("carbonTreeId").textContent = record?.tree_id || "—";
    statusNode.textContent = qualityLabel(record, estimate);
    statusNode.dataset.state = estimate
      ? estimate.source.exploratory || record.status === "ALTERNATIVE_POM" ? "caution" : "ready"
      : "blocked";

    if (!record || !estimate) {
      byId("carbonStock").textContent = "—";
      byId("carbonRange").textContent = "—";
      byId("carbonAnnual").textContent = "—";
      byId("carbonMeasurement").textContent = record ? `${record.status} · ${record.confidence_label || "—"}` : "ยังไม่ได้เลือกต้นไม้";
      byId("carbonAboveground").textContent = "—";
      byId("carbonBelowground").textContent = "—";
      byId("carbonStored").textContent = "—";
      byId("carbonDiameterMargin").textContent = "—";
      byId("carbonCreditState").textContent = "ยังคำนวณไม่ได้";
      warningsNode.innerHTML = buildWarnings(record, estimate, settings).map((warning) => `<li>${warning}</li>`).join("");
      return;
    }

    const heightText = estimate.source.heightAglM == null ? "ไม่ทราบระดับวัด" : `${formatNumber(estimate.source.heightAglM, 2)} ม. AGL`;
    byId("carbonStock").textContent = formatTco2(estimate.central.co2eTonnes, 3);
    byId("carbonRange").textContent = `${formatTco2(estimate.low.co2eTonnes, 3)} – ${formatTco2(estimate.high.co2eTonnes, 3)}`;
    byId("carbonAnnual").textContent = settings.annualDiameterGrowth > 0
      ? `${formatTco2(estimate.annualGrossIncrementTco2e, 3)} / ปี`
      : "ไม่ตั้ง growth scenario";
    byId("carbonMeasurement").textContent = `D ${formatNumber(estimate.source.diameterCm, 2)} ซม. @ ${heightText} · ${estimate.source.source}`;
    byId("carbonAboveground").textContent = formatMassKg(estimate.central.abovegroundBiomassKg);
    byId("carbonBelowground").textContent = settings.includeRoots ? formatMassKg(estimate.central.rootBiomassKg) : "ไม่รวม";
    byId("carbonStored").textContent = `${formatMassKg(estimate.central.carbonKg)} C`;
    byId("carbonDiameterMargin").textContent = `±${formatNumber(estimate.marginCm, 2)} ซม. (scenario)`;
    byId("carbonCreditState").textContent = "ยังไม่ใช่เครดิตที่ออกได้";
    warningsNode.innerHTML = buildWarnings(record, estimate, settings).map((warning) => `<li>${warning}</li>`).join("");
  }

  function renderAggregate(settings) {
    const aggregate = aggregateEstimates(settings);
    byId("carbonSiteStock").textContent = formatTco2(aggregate.centralTco2e, 2);
    byId("carbonSiteRange").textContent = `${formatTco2(aggregate.lowTco2e, 2)} – ${formatTco2(aggregate.highTco2e, 2)}`;
    byId("carbonSiteAnnual").textContent = settings.annualDiameterGrowth > 0
      ? `${formatTco2(aggregate.annualGrossIncrementTco2e, 2)} / ปี`
      : "—";
    byId("carbonSiteCoverage").textContent = `${aggregate.count} / ${records.length} Tree IDs · อัตโนมัติ ${aggregate.automaticCount}`
      + (aggregate.reviewCandidateCount ? ` · review candidate ${aggregate.reviewCandidateCount}` : "");
  }

  function renderAssumptions(settings) {
    byId("carbonAssumptionSummary").textContent = `ρ ${settings.woodDensityLow.toFixed(2)} / ${settings.woodDensityMid.toFixed(2)} / ${settings.woodDensityHigh.toFixed(2)} g/cm³ · CF ${settings.carbonFraction.toFixed(2)} · growth ${settings.annualDiameterGrowth.toFixed(2)} cm/yr · roots ${settings.includeRoots ? "included" : "excluded"}`;
  }

  function render() {
    if (!records.length) return;
    const settings = readSettings();
    const record = recordsById.get(currentTreeId) || null;
    renderSelected(record, settings);
    renderAggregate(settings);
    renderAssumptions(settings);
  }

  function setCurrentTree(treeId) {
    const normalized = String(treeId || "").trim().toUpperCase();
    if (!recordsById.has(normalized)) return;
    currentTreeId = normalized;
    render();
  }

  function bindControls() {
    [
      "woodDensityLow",
      "woodDensityMid",
      "woodDensityHigh",
      "carbonFraction",
      "annualDiameterGrowth",
      "includeRoots",
      "allowReviewCandidate",
    ].forEach((id) => {
      const node = byId(id);
      node.addEventListener("input", () => {
        persistSettings();
        render();
      });
      node.addEventListener("change", () => {
        persistSettings();
        render();
      });
    });

    byId("carbonReset").addEventListener("click", () => {
      writeSettings(DEFAULTS);
      persistSettings();
      render();
    });
    byId("carbonExportCsv").addEventListener("click", exportCsv);

    const treeSelect = byId("treeSelect");
    treeSelect?.addEventListener("change", () => setCurrentTree(treeSelect.value));

    const treeTitle = byId("treeTitle");
    if (treeTitle) {
      new MutationObserver(() => setCurrentTree(treeTitle.textContent)).observe(treeTitle, {
        childList: true,
        characterData: true,
        subtree: true,
      });
    }
    window.addEventListener("popstate", () => setCurrentTree(new URLSearchParams(location.search).get("tree")));
  }

  function exportCsv() {
    const settings = readSettings();
    const headers = [
      "tree_id",
      "measurement_status",
      "measurement_source",
      "measurement_height_agl_m",
      "diameter_cm",
      "diameter_scenario_margin_cm",
      "wood_density_low_g_cm3",
      "wood_density_mid_g_cm3",
      "wood_density_high_g_cm3",
      "carbon_fraction",
      "include_roots",
      "aboveground_biomass_kg",
      "root_biomass_kg",
      "total_biomass_kg",
      "stored_carbon_kg",
      "central_tco2e",
      "scenario_low_tco2e",
      "scenario_high_tco2e",
      "annual_diameter_growth_cm",
      "gross_annual_increment_tco2e",
      "field_verified",
      "estimate_status",
      "assumption_version",
    ];

    const rows = records.map((record) => {
      const estimate = estimateRecord(record, settings);
      if (!estimate) {
        return [
          record.tree_id,
          record.status,
          "",
          "",
          "",
          "",
          settings.woodDensityLow,
          settings.woodDensityMid,
          settings.woodDensityHigh,
          settings.carbonFraction,
          settings.includeRoots,
          "",
          "",
          "",
          "",
          "",
          "",
          "",
          settings.annualDiameterGrowth,
          "",
          Boolean(record.field_verified),
          "NO_RELEASED_DIAMETER",
          ASSUMPTION_VERSION,
        ];
      }
      return [
        record.tree_id,
        record.status,
        estimate.source.source,
        estimate.source.heightAglM ?? "",
        estimate.source.diameterCm,
        estimate.marginCm,
        settings.woodDensityLow,
        settings.woodDensityMid,
        settings.woodDensityHigh,
        settings.carbonFraction,
        settings.includeRoots,
        estimate.central.abovegroundBiomassKg,
        estimate.central.rootBiomassKg,
        estimate.central.totalBiomassKg,
        estimate.central.carbonKg,
        estimate.central.co2eTonnes,
        estimate.low.co2eTonnes,
        estimate.high.co2eTonnes,
        settings.annualDiameterGrowth,
        estimate.annualGrossIncrementTco2e,
        Boolean(record.field_verified),
        estimate.source.exploratory ? "EXPLORATORY_REVIEW_CANDIDATE" : "INDICATIVE_GEOMETRY",
        ASSUMPTION_VERSION,
      ];
    });

    const content = `\ufeff${[headers, ...rows].map((row) => row.map(csvCell).join(",")).join("\n")}`;
    const blob = new Blob([content], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `indicative-carbon-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  async function initCarbonEstimator() {
    const panel = byId("carbonPanel");
    if (!panel) return;

    restoreSettings();
    bindControls();

    try {
      const response = await fetch("data/measurements.json", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      records = Array.isArray(payload.records) ? payload.records : [];
      recordsById = new Map(records.map((record) => [record.tree_id, record]));
      byId("carbonModuleState").textContent = `${records.length} Tree IDs loaded · ${ASSUMPTION_VERSION}`;
      const queryTree = new URLSearchParams(location.search).get("tree");
      const visibleTree = byId("treeTitle")?.textContent;
      const selectTree = byId("treeSelect")?.value;
      setCurrentTree(queryTree || visibleTree || selectTree || records[0]?.tree_id);
      render();
    } catch (error) {
      byId("carbonModuleState").textContent = `โหลดข้อมูลคาร์บอนไม่สำเร็จ: ${error.message}`;
      byId("carbonEstimateStatus").textContent = "Carbon module unavailable";
      byId("carbonEstimateStatus").dataset.state = "blocked";
      console.error(error);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initCarbonEstimator, { once: true });
  } else {
    initCarbonEstimator();
  }
})();
