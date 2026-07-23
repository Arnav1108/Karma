"use client";

import { useState } from "react";
import type { ProgressDTO, QuestionDTO } from "@/lib/types";

export interface TranscriptTurn {
  q: string;
  a: string;
}

export function IntakeScreen({
  transcript,
  question,
  progress,
  submitting,
  onAnswer,
  onFinishEarly,
}: {
  transcript: TranscriptTurn[];
  question: QuestionDTO;
  progress: ProgressDTO;
  submitting: boolean;
  onAnswer: (answer: string) => void;
  onFinishEarly: () => void;
}) {
  const [draft, setDraft] = useState("");

  function submitDraft() {
    const trimmed = draft.trim();
    if (!trimmed || submitting) return;
    setDraft("");
    onAnswer(trimmed);
  }

  return (
    <div className="flex-1 flex justify-center px-6 py-16 pb-24">
      <div className="w-full max-w-[680px] animate-fade-in">
        <div className="text-[11px] font-medium tracking-[.1em] uppercase text-accent mb-7">
          {progress.answered} of {progress.total} answered
        </div>

        {transcript.length > 0 ? (
          <div className="flex flex-col gap-6 mb-10">
            {transcript.slice(-4).map((turn, i) => (
              <div key={i} className="opacity-[0.48]">
                <div className="text-[12.5px] text-accent mb-1">{turn.q}</div>
                <div className="font-serif italic text-[16px] text-foreground">{turn.a}</div>
              </div>
            ))}
          </div>
        ) : null}

        <div className="pt-8 border-t border-line">
          <div className="text-[12px] font-medium tracking-[.06em] uppercase text-accent mb-3.5">
            Question {progress.answered + 1} of {progress.total}
          </div>
          <div className="font-serif italic font-medium text-[28px] sm:text-[34px] leading-snug text-foreground mb-8">
            {question.text}
          </div>

          {question.kind === "confirm_default" ? (
            <div className="flex gap-3.5 max-w-[440px]">
              <button
                onClick={() => onAnswer("yes")}
                disabled={submitting}
                className="flex-1 text-center py-4 rounded-lg border border-accent text-accent font-medium text-[14px] cursor-pointer hover:bg-accent/10 transition-colors disabled:opacity-50"
              >
                Yes
              </button>
              <button
                onClick={() => onAnswer("no")}
                disabled={submitting}
                className="flex-1 text-center py-4 rounded-lg border border-line-strong text-foreground/80 font-medium text-[14px] cursor-pointer hover:border-accent transition-colors disabled:opacity-50"
              >
                No
              </button>
            </div>
          ) : (
            <div className="flex flex-col gap-4 max-w-[560px]">
              <input
                autoFocus
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") submitDraft();
                }}
                disabled={submitting}
                placeholder="Type your answer…"
                className="w-full bg-transparent border-0 border-b border-line-strong focus:border-accent outline-none py-3 font-serif italic text-[18px] text-foreground placeholder:text-faint placeholder:not-italic transition-colors disabled:opacity-50"
              />
              <button
                onClick={submitDraft}
                disabled={submitting || !draft.trim()}
                className="self-start px-6 py-2.5 rounded-lg bg-accent text-ink font-semibold text-[13px] cursor-pointer hover:bg-accent-hover transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {submitting ? "Sending…" : "Continue"}
              </button>
            </div>
          )}

          {progress.floor_met ? (
            <button
              onClick={onFinishEarly}
              disabled={submitting}
              className="mt-6 text-[12px] text-faint hover:text-muted cursor-pointer disabled:opacity-50"
            >
              Budget and use case are set — finish early instead
            </button>
          ) : (
            <div className="mt-6 text-[12px] text-faint">
              A few more essentials first — budget and primary use case unlock early finish.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
