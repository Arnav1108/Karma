"use client";

export type Step = "intake" | "review" | "generating" | "result";

const STEPS: { key: Step; label: string }[] = [
  { key: "intake", label: "Intake" },
  { key: "review", label: "Review" },
  { key: "generating", label: "Generating" },
  { key: "result", label: "Result" },
];

export function AppHeader({
  currentStep,
  progressPercent,
}: {
  currentStep: Step | null;
  progressPercent: number | null;
}) {
  const currentIndex = currentStep ? STEPS.findIndex((s) => s.key === currentStep) : -1;

  return (
    <div className="sticky top-0 z-10 bg-background/90 backdrop-blur-sm border-b border-line">
      <div className="max-w-5xl mx-auto px-6 sm:px-10 h-[76px] flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <span className="w-[9px] h-[9px] rounded-full bg-accent" />
          <span className="font-serif italic font-medium text-[19px] tracking-wide text-foreground">
            Karma Advisor
          </span>
        </div>
        <div className="flex gap-6 sm:gap-7">
          {STEPS.map((s, i) => {
            const isCurrent = s.key === currentStep;
            const isDone = currentIndex >= 0 && i < currentIndex;
            return (
              <div
                key={s.key}
                className="pb-1 text-[11px] font-medium tracking-[.09em] uppercase border-b-2"
                style={{
                  color: isCurrent
                    ? "var(--color-accent)"
                    : isDone
                    ? "var(--color-text)"
                    : "var(--color-faint)",
                  borderColor: isCurrent ? "var(--color-accent)" : "transparent",
                }}
              >
                {s.label}
              </div>
            );
          })}
        </div>
      </div>
      {progressPercent !== null ? (
        <div className="h-[2px] bg-line relative">
          <div
            className="absolute left-0 top-0 h-[2px] bg-accent transition-all duration-400 ease-out"
            style={{ width: `${progressPercent}%` }}
          />
        </div>
      ) : null}
    </div>
  );
}
