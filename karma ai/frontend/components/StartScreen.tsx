"use client";

export function StartScreen({
  onBegin,
  submitting,
}: {
  onBegin: () => void;
  submitting: boolean;
}) {
  return (
    <div className="flex-1 flex justify-center items-center px-6 py-24">
      <div className="w-full max-w-[560px] text-center animate-fade-in">
        <div className="flex items-center justify-center gap-2.5 mb-8">
          <span className="w-[9px] h-[9px] rounded-full bg-accent" />
          <span className="font-serif italic font-medium text-[19px] text-foreground">
            Karma Advisor
          </span>
        </div>
        <h1 className="font-serif italic font-medium text-[40px] leading-tight text-foreground mb-5">
          Your build is ready — after 13 quick questions
        </h1>
        <p className="text-[15px] leading-relaxed text-muted mb-12">
          Tell us your budget, what you’ll use the PC for, and what you already
          own. We’ll check feasibility against a live parts catalog and hand
          you a fully-priced, in-stock build.
        </p>
        <button
          onClick={onBegin}
          disabled={submitting}
          className="px-8 py-4 rounded-lg bg-accent text-ink font-semibold text-[15px] tracking-wide cursor-pointer hover:bg-accent-hover transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {submitting ? "Starting…" : "Begin"}
        </button>
      </div>
    </div>
  );
}
