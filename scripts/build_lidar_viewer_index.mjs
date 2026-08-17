import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const repositoryRoot = path.resolve(scriptDirectory, '..');
const publicRoot = path.join(repositoryRoot, 'site', 'public');
const dataDirectory = path.join(publicRoot, 'data', 'lidar-measurements');
const measurementsPath = path.join(dataDirectory, 'measurements.json');
const summaryPath = path.join(dataDirectory, 'summary.json');
const outputPath = path.join(dataDirectory, 'viewer-index.json');

const round = (value, digits = 6) => {
  if (value == null || !Number.isFinite(Number(value))) return null;
  const factor = 10 ** digits;
  return Math.round(Number(value) * factor) / factor;
};

const compactVector = (value, digits = 6) => (
  Array.isArray(value) ? value.map((item) => round(item, digits)) : null
);

function compactEllipse(ellipse) {
  if (!ellipse || ellipse.valid !== true) return null;
  return {
    valid: true,
    center: compactVector(ellipse.center),
    semi_major_axis_m: round(ellipse.semi_major_axis_m),
    semi_minor_axis_m: round(ellipse.semi_minor_axis_m),
    rotation_rad: round(ellipse.rotation_rad),
  };
}

function compactFit(fit) {
  if (!fit) return null;
  const compact = {
    center: compactVector(fit.center),
    radius_m: round(fit.radius_m),
    ellipse: compactEllipse(fit.ellipse),
  };
  if (!compact.center && compact.radius_m == null && !compact.ellipse) return null;
  return compact;
}

function compactPlane(plane) {
  if (!plane?.center_xyz || !plane?.basis_u || !plane?.basis_v) return null;
  return {
    center_xyz: compactVector(plane.center_xyz),
    basis_u: compactVector(plane.basis_u),
    basis_v: compactVector(plane.basis_v),
    axis_direction: compactVector(plane.axis_direction ?? [0, 0, 1]),
  };
}

function markingPathFromUrl(markingUrl) {
  if (!markingUrl || typeof markingUrl !== 'string') {
    throw new Error('Measurement record has no marking_url');
  }
  const relativePath = markingUrl.replace(/^\/+/, '');
  const resolvedPath = path.resolve(publicRoot, relativePath);
  const publicPrefix = `${publicRoot}${path.sep}`;
  if (!resolvedPath.startsWith(publicPrefix)) {
    throw new Error(`Unsafe marking path: ${markingUrl}`);
  }
  return resolvedPath;
}

function compactRecord(record, marking) {
  const fit = compactFit(marking.field_aid_fit ?? marking.fit ?? null);
  const plane = compactPlane(marking.measurement_plane);
  const fitModel = marking.field_aid_fit_model ?? record.field_aid_fit_model ?? record.fit_model ?? null;
  const renderable = Boolean(
    plane
    && fit
    && record.field_aid_circumference_cm != null
    && !record.operationally_excluded
    && ['READY_FOR_FIELD_USE', 'CHECK_ON_SITE'].includes(record.field_aid_status)
  );

  return {
    tree_id: record.tree_id,
    marking_url: record.marking_url,
    detection_status: record.detection_status,
    identity_review_status: record.identity_review_status,
    protocol_applicability: record.protocol_applicability,
    protocol_resolved: Boolean(record.protocol_resolved),
    measurement_kind: record.measurement_kind,
    measurement_height_agl_m: round(record.measurement_height_agl_m, 3),
    field_aid_measurement_height_agl_m: round(record.field_aid_measurement_height_agl_m, 3),
    field_aid_status: record.field_aid_status,
    field_aid_source: record.field_aid_source,
    field_aid_is_current_protocol_final: Boolean(record.field_aid_is_current_protocol_final),
    field_aid_circumference_cm: round(record.field_aid_circumference_cm, 2),
    field_aid_diameter_cm: round(record.field_aid_diameter_cm, 2),
    field_aid_dbh_cm: round(record.field_aid_dbh_cm, 2),
    legacy_full_resolution_status: record.legacy_full_resolution_status,
    legacy_measurement_rule: record.legacy_measurement_rule,
    legacy_circumference_cm: round(record.legacy_circumference_cm, 2),
    acceptance_status: record.acceptance_status,
    geometric_status: record.geometric_status,
    fit_model: fitModel,
    point_count: Number(record.point_count ?? 0),
    inlier_count: Number(record.inlier_count ?? 0),
    angular_coverage_deg: round(record.angular_coverage_deg, 1),
    axis_source: record.axis_source,
    axis_status: record.axis_status,
    axis_uncertainty_m: round(record.axis_uncertainty_m),
    operationally_excluded: Boolean(record.operationally_excluded),
    operational_exclusion_decision: record.operational_exclusion_decision,
    field_verified: Boolean(record.field_verified),
    qa_reason_codes: Array.isArray(record.qa_reason_codes) ? record.qa_reason_codes : [],
    renderable,
    plane,
    fit,
  };
}

async function main() {
  const [measurementText, summaryText] = await Promise.all([
    readFile(measurementsPath, 'utf8'),
    readFile(summaryPath, 'utf8'),
  ]);
  const measurementPayload = JSON.parse(measurementText.replace(/^\uFEFF/, ''));
  const summary = JSON.parse(summaryText.replace(/^\uFEFF/, ''));
  if (!Array.isArray(measurementPayload.records)) {
    throw new Error('measurements.json does not contain a records array');
  }

  const records = [];
  const failures = [];
  for (const record of measurementPayload.records) {
    try {
      const markingText = await readFile(markingPathFromUrl(record.marking_url), 'utf8');
      const marking = JSON.parse(markingText.replace(/^\uFEFF/, ''));
      records.push(compactRecord(record, marking));
    } catch (error) {
      failures.push({ tree_id: record.tree_id, error: error.message });
    }
  }

  if (failures.length) {
    const detail = failures.map((failure) => `${failure.tree_id}: ${failure.error}`).join('\n');
    throw new Error(`Could not build ${failures.length} measurement records:\n${detail}`);
  }

  const renderableCount = records.filter((record) => record.renderable).length;
  const payload = {
    schema_version: 1,
    algorithm_version: measurementPayload.algorithm_version,
    field_verified: Boolean(measurementPayload.field_verified),
    generated_at_utc: measurementPayload.generated_at_utc ?? summary.generated_at_utc ?? null,
    viewer_summary: {
      record_count: records.length,
      renderable_ring_count: renderableCount,
      marker_only_count: records.length - renderableCount,
    },
    source_files: {
      measurements: 'data/lidar-measurements/measurements.json',
      summary: 'data/lidar-measurements/summary.json',
      markings: 'data/lidar-measurements/markings/*.json',
    },
    summary,
    records,
  };

  await writeFile(outputPath, `${JSON.stringify(payload, null, 2)}\n`, 'utf8');
  console.log(`Wrote ${path.relative(repositoryRoot, outputPath)} with ${records.length} records (${renderableCount} renderable rings).`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
