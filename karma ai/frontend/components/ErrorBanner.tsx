"use client";

export function ErrorBanner({
  message,
  retryable,
  onRetry,
  onDismiss,
  onStartOver,
}: {
  message: string;
  retryable: boolean;
  onRetry?: () => void;
  onDismiss?: () => void;
  onStartOver?: () => void;
}) {
  return (
    <div className="w-full rounded-lg border border-line-strong bg-raised px-5 py-4 flex items-start justify-between gap-4 animate-fade-in">
      <p className="text-[14px] leading-relaxed text-foreground/90">{message}</p>
      <div className="flex items-center gap-3 shrink-0">
        {retryable && onRetry ? (
          <button
            onClick={onRetry}
            className="text-[13px] font-medium text-accent hover:text-accent-hover cursor-pointer"
          >
            Try again
          </button>
        ) : null}
        {!retryable && onStartOver ? (
          <button
            onClick={onStartOver}
            className="text-[13px] font-medium text-accent hover:text-accent-hover cursor-pointer"
          >
            Start over
          </button>
        ) : null}
        {onDismiss ? (
          <button
            onClick={onDismiss}
            className="text-[13px] text-faint hover:text-muted cursor-pointer"
          >
            Dismiss
          </button>
        ) : null}
      </div>
    </div>
  );
}
