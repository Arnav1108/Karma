"use client";

import { buildReviewFields } from "@/lib/summarize";
import type { BriefSummaryDTO } from "@/lib/types";

export function ReviewScreen({
  briefSummary,
  submitting,
  onGenerate,
}: {
  briefSummary: BriefSummaryDTO;
  submitting: boolean;
  onGenerate: () => void;
}) {
  const fields = buildReviewFields(briefSummary);

  return (
    <div className="flex-1 flex justify-center px-6 py-16 pb-24">
      <div className="w-full max-w-[800px] animate-fade-in">
        <h2 className="font-serif italic font-medium text-[30px] text-foreground mb-2">
          Everything we understood
        </h2>
        <p className="text-[14px] text-muted mb-9">
          Review your brief before we assemble a build.
        </p>

        <div className="grid grid-cols-1 sm:grid-cols-2 border-t border-line">
          {fields.map((f) => (
            <div key={f.label} className="py-[18px] pr-5 border-b border-line">
              <div className="text-[11px] font-medium tracking-[.06em] uppercase text-muted mb-1.5">
                {f.label}
              </div>
              <div className="text-[15px] text-foreground">{f.value}</div>
            </div>
          ))}
        </div>

        <button
          onClick={onGenerate}
          disabled={submitting}
          className="mt-10 max-w-[320px] w-full py-[18px] rounded-lg bg-accent text-ink font-semibold text-[15px] tracking-wide cursor-pointer hover:bg-accent-hover transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {submitting ? "Starting…" : "Generate my build"}
        </button>
      </div>
    </div>
  );
}
