import { describe, expect, it } from "vitest";
import { buildReviewFields } from "./summarize";
import type { BriefSummaryDTO } from "./types";

// Fixtures are the authoritative description of BriefSummaryDTO's untyped-dict
// interiors (see the header comment in summarize.ts). Imported by relative
// path from api/contract/ so a backend fixture regen breaks this test.
import answerLocked from "../../api/contract/fixtures/review/answer_locked.response.json";
import lock from "../../api/contract/fixtures/review/lock.response.json";
import snapshotLocked from "../../api/contract/fixtures/review/snapshot_locked.response.json";

// snapshot_asking.response.json has brief_summary: null (pre-lock state) and
// is intentionally excluded — buildReviewFields takes a BriefSummaryDTO, not
// a nullable one.
const LOCKED_FIXTURES: Array<{ name: string; briefSummary: BriefSummaryDTO }> = [
  { name: "answer_locked", briefSummary: answerLocked.brief_summary as unknown as BriefSummaryDTO },
  { name: "lock", briefSummary: lock.brief_summary as unknown as BriefSummaryDTO },
  { name: "snapshot_locked", briefSummary: snapshotLocked.brief_summary as unknown as BriefSummaryDTO },
];

// All three fixtures currently ship identical brief data. brand_prefs (cpu:
// null, gpu: null) and hard_constraints (must_have: [], must_not: []) carry
// no real data in any of them, so those two sections are expected to be
// absent — asserting their presence would be a false requirement, not rigor.
const EXPECTED_FIELDS: Record<string, string> = {
  Budget: "₹60,000 – ₹65,000 (ceiling ₹70,000)",
  "Primary use": "Gaming — Competitive Fps",
  Software: "Valorant, CS2, GTA V",
  "Performance target": "1080p @ 144+ fps",
  "Monitor setup": "Keeping existing 1080p @ 144Hz",
  Peripherals: "Keyboard, Mouse",
  Storage: "512GB NVME",
  "Operating system": "Windows (oem)",
  "Physical constraints": "Atx Mid, Balanced noise tolerance",
  Longevity: "Consumer, Buy Now",
  Extras: "Minimal RGB · WIFI",
};

const SECTIONS_WITHOUT_DATA = ["Brand preferences", "Existing parts", "Must-haves / must-nots"];

describe("buildReviewFields", () => {
  for (const { name, briefSummary } of LOCKED_FIXTURES) {
    describe(`fixture: ${name}`, () => {
      const fields = buildReviewFields(briefSummary);
      const byLabel = new Map(fields.map((f) => [f.label, f.value]));

      it("produces every expected label with the expected value", () => {
        for (const [label, expectedValue] of Object.entries(EXPECTED_FIELDS)) {
          expect(byLabel.get(label), `missing or wrong label "${label}"`).toBe(expectedValue);
        }
      });

      it("does not silently drop a section that has real fixture data", () => {
        // Every key in EXPECTED_FIELDS corresponds to a section this fixture
        // populates with non-empty data; all must survive to the output.
        expect(byLabel.size).toBeGreaterThanOrEqual(Object.keys(EXPECTED_FIELDS).length);
      });

      it("omits sections with no real data rather than emitting empty labels", () => {
        for (const label of SECTIONS_WITHOUT_DATA) {
          expect(byLabel.has(label)).toBe(false);
        }
      });
    });
  }
});
