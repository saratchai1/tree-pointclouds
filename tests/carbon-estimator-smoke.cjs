const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class FakeNode {
  constructor(id) {
    this.id = id;
    this.value = "";
    this.checked = false;
    this.textContent = "";
    this.innerHTML = "";
    this.dataset = {};
    this.listeners = new Map();
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }
}

const ids = [
  "carbonPanel",
  "woodDensityLow",
  "woodDensityMid",
  "woodDensityHigh",
  "carbonFraction",
  "annualDiameterGrowth",
  "includeRoots",
  "allowReviewCandidate",
  "carbonReset",
  "carbonExportCsv",
  "treeSelect",
  "treeTitle",
  "carbonModuleState",
  "carbonEstimateStatus",
  "carbonTreeId",
  "carbonStock",
  "carbonRange",
  "carbonAnnual",
  "carbonMeasurement",
  "carbonAboveground",
  "carbonBelowground",
  "carbonStored",
  "carbonDiameterMargin",
  "carbonCreditState",
  "carbonWarnings",
  "carbonSiteStock",
  "carbonSiteRange",
  "carbonSiteAnnual",
  "carbonSiteCoverage",
  "carbonAssumptionSummary",
];

const nodes = Object.fromEntries(ids.map((id) => [id, new FakeNode(id)]));
nodes.treeTitle.textContent = "TREE_0066";
nodes.treeSelect.value = "TREE_0066";

const tree0066 = {
  tree_id: "TREE_0066",
  status: "ALTERNATIVE_POM",
  measurement_kind: "ALTERNATIVE_POM",
  measurement_height_agl_m: 2.9,
  diameter_cm: 15.69,
  circumference_cm: 49.29,
  fit_rmse_m: 0.003838,
  radius_stability_mad_m: 0.000844,
  quality_score: 90.93,
  confidence_label: "HIGH",
  detection_status: "PROBABLE",
  field_verified: false,
};

const manualReview = {
  tree_id: "TREE_0065",
  status: "MANUAL_REVIEW",
  measurement_kind: "MANUAL_REVIEW",
  diameter_cm: null,
  fit_rmse_m: 0.002862,
  radius_stability_mad_m: 0.000111,
  confidence_label: "MANUAL_REVIEW",
  field_verified: false,
  best_review_candidate: {
    diameter_cm: 4.07,
    height_agl_m: 2.1,
  },
};

global.document = {
  readyState: "complete",
  getElementById(id) {
    return nodes[id] || null;
  },
  addEventListener() {},
};
global.window = { addEventListener() {} };
global.location = { search: "?tree=TREE_0066" };
global.localStorage = {
  getItem() { return null; },
  setItem() {},
};
global.MutationObserver = class {
  constructor(callback) { this.callback = callback; }
  observe() {}
};
global.fetch = async (requestPath) => {
  assert.equal(requestPath, "data/measurements.json");
  return {
    ok: true,
    async json() {
      return { records: [tree0066, manualReview] };
    },
  };
};

const scriptPath = path.resolve(__dirname, "../site/public/viewer-v3-full-las/carbon.js");
const source = fs.readFileSync(scriptPath, "utf8");
vm.runInThisContext(source, { filename: scriptPath });

setImmediate(() => {
  assert.match(nodes.carbonModuleState.textContent, /2 Tree IDs loaded/);
  assert.equal(nodes.carbonTreeId.textContent, "TREE_0066");
  assert.match(nodes.carbonStock.textContent, /0\.324 tCO₂e/);
  assert.match(nodes.carbonRange.textContent, /0\.194 tCO₂e.*0\.481 tCO₂e/);
  assert.match(nodes.carbonAnnual.textContent, /0\.025 tCO₂e/);
  assert.match(nodes.carbonMeasurement.textContent, /D 15\.69 ซม\./);
  assert.match(nodes.carbonMeasurement.textContent, /2\.90 ม\. AGL/);
  assert.match(nodes.carbonAboveground.textContent, /131\.5 กก\./);
  assert.match(nodes.carbonBelowground.textContent, /56\.7 กก\./);
  assert.match(nodes.carbonStored.textContent, /88\.5 กก\. C/);
  assert.match(nodes.carbonDiameterMargin.textContent, /±0\.77 ซม\./);
  assert.match(nodes.carbonCreditState.textContent, /ยังไม่ใช่เครดิต/);
  assert.match(nodes.carbonWarnings.innerHTML, /ไม่ใช่ DBH 1\.30 ม\./);

  // MANUAL_REVIEW is excluded from the aggregate unless explicitly enabled.
  assert.match(nodes.carbonSiteCoverage.textContent, /1 \/ 2 Tree IDs/);
  assert.match(nodes.carbonSiteCoverage.textContent, /อัตโนมัติ 1/);
  assert.doesNotMatch(nodes.carbonSiteCoverage.textContent, /review candidate/);
  assert.match(nodes.carbonAssumptionSummary.textContent, /ρ 0\.40 \/ 0\.60 \/ 0\.80/);

  console.log("carbon estimator smoke: passed");
});
