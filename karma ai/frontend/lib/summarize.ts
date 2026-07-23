import { formatINR, titleCase } from "./format";
import type { BriefSummaryDTO } from "./types";

// BriefSummaryDTO's per-section fields (budget, purpose, performance, …) are
// untyped dicts on the wire (api/dtos.py) — the OpenAPI schema and freeze
// test do not cover their inner shape, so this file treats the committed
// example fixtures (api/contract/fixtures/review/*.json) as the authoritative
// description of the keys used below (docs/frontend_contract_plan.md
// section 3). Unknown/extra keys are simply ignored, not an error.

export interface ReviewField {
  label: string;
  value: string;
}

function get(section: Record<string, unknown> | undefined, key: string): unknown {
  return section ? section[key] : undefined;
}

function str(section: Record<string, unknown> | undefined, key: string): string | null {
  const v = get(section, key);
  return v === null || v === undefined || v === "" ? null : String(v);
}

export function buildReviewFields(brief: BriefSummaryDTO): ReviewField[] {
  const fields: ReviewField[] = [];

  const min = get(brief.budget, "comfortable_min");
  const max = get(brief.budget, "comfortable_max");
  const ceiling = get(brief.budget, "ceiling");
  if (typeof min === "number" && typeof max === "number") {
    let value = `${formatINR(min)} – ${formatINR(max)}`;
    if (typeof ceiling === "number") value += ` (ceiling ${formatINR(ceiling)})`;
    fields.push({ label: "Budget", value });
  }

  const primaryUse = str(brief.purpose, "primary_use_case");
  const subCase = str(brief.purpose, "sub_case");
  if (primaryUse) {
    fields.push({
      label: "Primary use",
      value: subCase ? `${titleCase(primaryUse)} — ${titleCase(subCase)}` : titleCase(primaryUse),
    });
  }

  if (brief.software?.length) {
    fields.push({
      label: "Software",
      value: brief.software.map((s) => s.name).join(", "),
    });
  }

  const resolution = str(brief.performance, "target_resolution");
  const framerate = get(brief.performance, "target_framerate");
  if (resolution) {
    let value = resolution;
    if (typeof framerate === "number") value += ` @ ${framerate}+ fps`;
    if (get(brief.performance, "hdr_wanted") === true) value += ", HDR";
    fields.push({ label: "Performance target", value });
  }

  const monitorSpecs = str(brief.monitor, "specs");
  const monitorOwned = str(brief.monitor, "owned");
  if (monitorSpecs || monitorOwned) {
    fields.push({
      label: "Monitor setup",
      value:
        monitorOwned === "no"
          ? "New monitor needed"
          : monitorSpecs
          ? `Keeping existing ${monitorSpecs}`
          : "Already owns a monitor",
    });
  }

  if (brief.peripherals?.length) {
    fields.push({
      label: "Peripherals",
      value: brief.peripherals
        .map((p) => `${titleCase(p.type)}${p.priority === "must_have" ? " (must-have)" : ""}`)
        .join(", "),
    });
  }

  const capacity = get(brief.storage, "capacity_gb");
  const speedTier = str(brief.storage, "speed_tier");
  if (typeof capacity === "number") {
    fields.push({
      label: "Storage",
      value: `${capacity}GB${speedTier ? ` ${speedTier.toUpperCase()}` : ""}`,
    });
  }

  const os = str(brief.operating_system, "os");
  const license = str(brief.operating_system, "license");
  if (os) {
    fields.push({
      label: "Operating system",
      value: license ? `${titleCase(os)} (${license})` : titleCase(os),
    });
  }

  if (brief.reuse_parts?.length) {
    fields.push({
      label: "Existing parts",
      value: brief.reuse_parts.map((p) => `${titleCase(p.slot)}: ${p.identifier}`).join(", "),
    });
  }

  const brandEntries = Object.entries(brief.brand_prefs ?? {}).filter(
    ([, v]) => v !== null && v !== undefined && v !== ""
  );
  if (brandEntries.length) {
    fields.push({
      label: "Brand preferences",
      value: brandEntries.map(([slot, brand]) => `${titleCase(slot)}: ${brand}`).join(", "),
    });
  }

  const formFactor = str(brief.physical, "form_factor_pref");
  const noise = str(brief.physical, "noise_tolerance");
  if (formFactor || noise) {
    fields.push({
      label: "Physical constraints",
      value: [formFactor && titleCase(formFactor), noise && `${titleCase(noise)} noise tolerance`]
        .filter(Boolean)
        .join(", "),
    });
  }

  const reliability = str(brief.longevity, "reliability_priority");
  const timeline = str(brief.longevity, "timeline");
  if (reliability || timeline) {
    fields.push({
      label: "Longevity",
      value: [reliability && titleCase(reliability), timeline && titleCase(timeline)]
        .filter(Boolean)
        .join(", "),
    });
  }

  const rgb = str(brief.extras, "rgb_pref");
  const connectivity = get(brief.extras, "connectivity_needs");
  if (rgb || (Array.isArray(connectivity) && connectivity.length)) {
    fields.push({
      label: "Extras",
      value: [
        rgb && `${titleCase(rgb)} RGB`,
        Array.isArray(connectivity) && connectivity.length
          ? connectivity.map((c) => String(c).toUpperCase()).join(", ")
          : null,
      ]
        .filter(Boolean)
        .join(" · "),
    });
  }

  const mustHave = get(brief.hard_constraints, "must_have");
  const mustNot = get(brief.hard_constraints, "must_not");
  const mustHaveList = Array.isArray(mustHave) ? mustHave : [];
  const mustNotList = Array.isArray(mustNot) ? mustNot : [];
  if (mustHaveList.length || mustNotList.length) {
    fields.push({
      label: "Must-haves / must-nots",
      value: [
        ...mustHaveList.map((m) => `Must have ${m}`),
        ...mustNotList.map((m) => `Must not have ${m}`),
      ].join("; "),
    });
  }

  return fields;
}
