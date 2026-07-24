"use client";

import { formatINR, titleCase } from "@/lib/format";
import type { BuildStatusResponse } from "@/lib/types";

const VERDICT_LABEL: Record<string, string> = {
  comfortable: "Comfortable fit",
  tight: "Tight fit",
  impossible: "Not feasible",
};

export function ResultScreen({
  build: status,
  submitting,
  onRetry,
  onStartOver,
}: {
  build: BuildStatusResponse;
  submitting: boolean;
  onRetry: () => void;
  onStartOver: () => void;
}) {
  return (
    <div className="flex-1 flex justify-center px-6 py-16 pb-24">
      <div className="w-full max-w-[720px] animate-fade-in">
        {status.status === "succeeded" && !status.build ? (
          <div className="text-center py-16 flex flex-col gap-5 items-center">
            <div className="font-serif italic text-[20px] text-foreground/90">
              Your build finished, but we couldn&apos;t load the part list. Try again or start a new build.
            </div>
            <div className="flex items-center gap-4">
              <button
                onClick={onRetry}
                disabled={submitting}
                className="px-8 py-3.5 rounded-lg bg-accent text-ink font-semibold text-[13px] cursor-pointer hover:bg-accent-hover transition-colors disabled:opacity-50"
              >
                {submitting ? "Retrying…" : "Retry"}
              </button>
              <button
                onClick={onStartOver}
                className="text-[13px] text-faint hover:text-muted cursor-pointer"
              >
                Start over
              </button>
            </div>
          </div>
        ) : null}

        {status.status === "succeeded" && status.build ? (
          <>
            <h2 className="font-serif italic font-medium text-[30px] text-foreground mb-8">
              Your build is ready
            </h2>
            <div className="border-t border-line">
              {status.build.parts.map((part) => (
                <div
                  key={part.slot}
                  className="flex justify-between items-baseline py-4 border-b border-line gap-6"
                >
                  <div className="text-[12px] font-medium tracking-[.05em] uppercase text-muted shrink-0">
                    {titleCase(part.slot)}
                  </div>
                  <div className="text-right">
                    <div className="text-[15px] text-foreground">{part.name}</div>
                    <div className="text-[12px] text-faint">{formatINR(part.price_inr)}</div>
                  </div>
                </div>
              ))}
            </div>
            <div className="flex justify-between items-baseline mt-7 mb-5">
              <div className="font-serif font-medium text-[40px] text-foreground">
                {formatINR(status.build.total_price_inr)}
              </div>
              {status.verdict ? (
                <div className="text-[13px] font-medium text-accent">
                  {VERDICT_LABEL[status.verdict.verdict] ?? status.verdict.verdict}
                </div>
              ) : null}
            </div>
            <p className="text-[14px] text-muted mb-5">{status.build.summary}</p>
            {status.build.warnings.length > 0 ? (
              <div className="px-[22px] py-5 bg-raised border-l-2 border-accent rounded-md text-[14px] leading-relaxed text-foreground/90 flex flex-col gap-2">
                {status.build.warnings.map((w, i) => (
                  <div key={i}>{w}</div>
                ))}
              </div>
            ) : null}
            <button
              onClick={onStartOver}
              className="mt-9 text-[13px] text-faint hover:text-muted cursor-pointer"
            >
              Start a new build
            </button>
          </>
        ) : null}

        {status.status === "infeasible" && !status.verdict ? (
          <div className="text-center py-16 flex flex-col gap-5 items-center">
            <div className="font-serif italic text-[20px] text-foreground/90">
              This build wasn&apos;t feasible within your budget, but we don&apos;t have details on why. Start over with a new brief.
            </div>
            <button
              onClick={onStartOver}
              className="px-6 py-3 rounded-lg bg-accent text-ink font-semibold text-[13px] cursor-pointer hover:bg-accent-hover transition-colors"
            >
              Start over with a new brief
            </button>
          </div>
        ) : null}

        {status.status === "infeasible" && status.verdict ? (
          <>
            <div className="font-serif italic font-medium text-[24px] sm:text-[28px] leading-snug text-foreground mb-7">
              {status.verdict.reason}
            </div>
            {status.verdict.suggested_adjustments.length > 0 ? (
              <>
                <div className="text-[12px] font-medium tracking-[.06em] uppercase text-muted mb-3.5">
                  Suggested adjustments
                </div>
                <div className="flex flex-col gap-2.5 mb-9">
                  {status.verdict.suggested_adjustments.map((adj, i) => (
                    <div
                      key={i}
                      className="px-[18px] py-3.5 border border-line-strong rounded-lg text-[14px] text-accent"
                    >
                      {adj}
                    </div>
                  ))}
                </div>
              </>
            ) : null}
            <button
              onClick={onStartOver}
              className="px-6 py-3 rounded-lg bg-accent text-ink font-semibold text-[13px] cursor-pointer hover:bg-accent-hover transition-colors"
            >
              Start over with a new brief
            </button>
          </>
        ) : null}

        {status.status === "cannot_proceed" ? (
          <div className="text-center py-16 flex flex-col gap-5 items-center">
            <div className="font-serif italic text-[20px] text-foreground/90">
              {status.reason ?? "The build couldn't be completed right now."}
            </div>
            <button
              onClick={onRetry}
              disabled={submitting}
              className="px-8 py-3.5 rounded-lg bg-accent text-ink font-semibold text-[13px] cursor-pointer hover:bg-accent-hover transition-colors disabled:opacity-50"
            >
              {submitting ? "Retrying…" : "Retry"}
            </button>
          </div>
        ) : null}

        {status.status === "failed" ? (
          <div className="text-center py-16 flex flex-col gap-5 items-center">
            <div className="font-serif italic text-[20px] text-foreground/90">
              {status.error?.message ?? "Something went wrong while assembling your build."}
            </div>
            {status.error?.retryable ? (
              <button
                onClick={onRetry}
                disabled={submitting}
                className="px-8 py-3.5 rounded-lg bg-accent text-ink font-semibold text-[13px] cursor-pointer hover:bg-accent-hover transition-colors disabled:opacity-50"
              >
                {submitting ? "Retrying…" : "Retry"}
              </button>
            ) : (
              <button
                onClick={onStartOver}
                className="px-8 py-3.5 rounded-lg bg-accent text-ink font-semibold text-[13px] cursor-pointer hover:bg-accent-hover transition-colors"
              >
                Start over
              </button>
            )}
          </div>
        ) : null}
      </div>
    </div>
  );
}
