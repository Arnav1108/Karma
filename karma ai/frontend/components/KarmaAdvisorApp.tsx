"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  abandonSession,
  createSession,
  getBuildStatus,
  lockSession,
  startBuild,
  submitAnswer,
} from "@/lib/api";
import type { BriefSummaryDTO, BuildStatusResponse, ProgressDTO, QuestionDTO } from "@/lib/types";
import { AppHeader, type Step } from "./AppHeader";
import { ErrorBanner } from "./ErrorBanner";
import { StartScreen } from "./StartScreen";
import { IntakeScreen, type TranscriptTurn } from "./IntakeScreen";
import { ReviewScreen } from "./ReviewScreen";
import { GeneratingScreen } from "./GeneratingScreen";
import { ResultScreen } from "./ResultScreen";

type Screen = "start" | "intake" | "review" | "generating" | "result";

const TERMINAL_BUILD_STATUSES = new Set(["succeeded", "infeasible", "cannot_proceed", "failed"]);

// Consecutive getBuildStatus failures tolerated before polling gives up and
// surfaces a retryable error, rather than failing (or spinning) silently.
const MAX_POLL_ERROR_ATTEMPTS = 3;
const POLL_BACKOFF_MS = [2000, 4000, 8000];

function friendlyError(err: unknown): { message: string; retryable: boolean } {
  if (err instanceof ApiError) {
    return { message: err.message, retryable: err.retryable };
  }
  return {
    message: "Couldn't reach the Karma Advisor service. Check your connection and try again.",
    retryable: true,
  };
}

export function KarmaAdvisorApp() {
  const [screen, setScreen] = useState<Screen>("start");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<{ message: string; retryable: boolean } | null>(null);

  const [sessionId, setSessionId] = useState<string | null>(null);
  const [question, setQuestion] = useState<QuestionDTO | null>(null);
  const [progress, setProgress] = useState<ProgressDTO | null>(null);
  const [transcript, setTranscript] = useState<TranscriptTurn[]>([]);
  const [briefSummary, setBriefSummary] = useState<BriefSummaryDTO | null>(null);

  const [buildStatus, setBuildStatus] = useState<BuildStatusResponse | null>(null);

  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollBuildRef = useRef<(id: string, delayMs: number) => void>(() => {});
  const pollErrorCountRef = useRef(0);
  const lastActionRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    pollBuildRef.current = (id: string, delayMs: number) => {
      if (pollTimer.current) clearTimeout(pollTimer.current);
      pollTimer.current = setTimeout(async () => {
        try {
          const status = await getBuildStatus(id);
          pollErrorCountRef.current = 0;
          if (TERMINAL_BUILD_STATUSES.has(status.status)) {
            setBuildStatus(status);
            setScreen("result");
          } else {
            pollBuildRef.current(id, status.poll_after_ms ?? 2000);
          }
        } catch (err) {
          pollErrorCountRef.current += 1;
          if (pollErrorCountRef.current < MAX_POLL_ERROR_ATTEMPTS) {
            const backoffMs =
              POLL_BACKOFF_MS[pollErrorCountRef.current - 1] ??
              POLL_BACKOFF_MS[POLL_BACKOFF_MS.length - 1];
            pollBuildRef.current(id, backoffMs);
          } else {
            lastActionRef.current = () => {
              pollErrorCountRef.current = 0;
              pollBuildRef.current(id, 2000);
            };
            setError(friendlyError(err));
          }
        }
      }, delayMs);
    };
  });

  useEffect(() => {
    return () => {
      if (pollTimer.current) clearTimeout(pollTimer.current);
    };
  }, []);

  const pollBuild = useCallback((id: string, delayMs: number) => {
    pollBuildRef.current(id, delayMs);
  }, []);

  async function handleBegin() {
    setSubmitting(true);
    setError(null);
    try {
      const res = await createSession();
      setSessionId(res.session_id);
      setQuestion(res.question);
      setProgress(res.progress);
      setTranscript([]);
      setScreen("intake");
    } catch (err) {
      lastActionRef.current = () => handleBegin();
      setError(friendlyError(err));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleAnswer(answer: string) {
    if (!sessionId || !question) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await submitAnswer(sessionId, answer);
      setTranscript((t) => [...t, { q: question.text, a: answer }]);
      if (res.status === "locked") {
        setBriefSummary(res.brief_summary);
        setScreen("review");
      } else {
        setQuestion(res.question);
        setProgress(res.progress);
      }
    } catch (err) {
      lastActionRef.current = () => handleAnswer(answer);
      setError(friendlyError(err));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleFinishEarly() {
    if (!sessionId) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await lockSession(sessionId);
      setBriefSummary(res.brief_summary);
      setScreen("review");
    } catch (err) {
      lastActionRef.current = () => handleFinishEarly();
      setError(friendlyError(err));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleGenerate() {
    if (!sessionId) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await startBuild(sessionId);
      setScreen("generating");
      pollBuild(res.build_id, res.poll_after_ms);
    } catch (err) {
      // A build already active for this session is not a dead end — resume
      // polling the existing build instead of surfacing an error.
      if (err instanceof ApiError && err.code === "BUILD_ALREADY_ACTIVE") {
        const existingId = err.details?.build_id;
        if (typeof existingId === "string") {
          setScreen("generating");
          pollBuild(existingId, 2000);
          setSubmitting(false);
          return;
        }
      }
      lastActionRef.current = () => handleGenerate();
      setError(friendlyError(err));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleRetryBuild() {
    if (!sessionId) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await startBuild(sessionId);
      setBuildStatus(null);
      setScreen("generating");
      pollBuild(res.build_id, res.poll_after_ms);
    } catch (err) {
      lastActionRef.current = () => handleRetryBuild();
      setError(friendlyError(err));
    } finally {
      setSubmitting(false);
    }
  }

  function handleStartOver() {
    if (sessionId) abandonSession(sessionId);
    if (pollTimer.current) clearTimeout(pollTimer.current);
    lastActionRef.current = null;
    setSessionId(null);
    setQuestion(null);
    setProgress(null);
    setTranscript([]);
    setBriefSummary(null);
    setBuildStatus(null);
    setError(null);
    setScreen("start");
  }

  function handleErrorRetry() {
    const retry = lastActionRef.current;
    lastActionRef.current = null;
    setError(null);
    retry?.();
  }

  function handleErrorDismiss() {
    lastActionRef.current = null;
    setError(null);
  }

  const step: Step | null =
    screen === "start" ? null : (screen as Step);
  const progressPercent =
    screen === "intake" && progress ? (progress.answered / progress.total) * 100 : null;

  return (
    <div className="min-h-screen w-full flex flex-col">
      <AppHeader currentStep={step} progressPercent={progressPercent} />

      {error ? (
        <div className="max-w-3xl w-full mx-auto px-6 pt-6">
          <ErrorBanner
            message={error.message}
            retryable={error.retryable}
            onRetry={lastActionRef.current ? handleErrorRetry : undefined}
            onDismiss={handleErrorDismiss}
            onStartOver={screen === "generating" ? handleStartOver : undefined}
          />
        </div>
      ) : null}

      {screen === "start" ? <StartScreen onBegin={handleBegin} submitting={submitting} /> : null}

      {screen === "intake" && question && progress ? (
        <IntakeScreen
          transcript={transcript}
          question={question}
          progress={progress}
          submitting={submitting}
          onAnswer={handleAnswer}
          onFinishEarly={handleFinishEarly}
        />
      ) : null}

      {screen === "review" && briefSummary ? (
        <ReviewScreen
          briefSummary={briefSummary}
          submitting={submitting}
          onGenerate={handleGenerate}
        />
      ) : null}

      {screen === "generating" ? <GeneratingScreen /> : null}

      {screen === "result" && buildStatus ? (
        <ResultScreen
          build={buildStatus}
          submitting={submitting}
          onRetry={handleRetryBuild}
          onStartOver={handleStartOver}
        />
      ) : null}
    </div>
  );
}
