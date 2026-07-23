"use client";

import { useEffect, useState } from "react";

const GEN_LINES = [
  "Checking part compatibility…",
  "Balancing PSU headroom…",
  "Confirming in-stock availability…",
  "Optimizing price-to-performance…",
  "Finalizing component list…",
];

export function GeneratingScreen() {
  const [lineIndex, setLineIndex] = useState(0);

  useEffect(() => {
    const id = setInterval(() => {
      setLineIndex((i) => (i + 1) % GEN_LINES.length);
    }, 1800);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="flex-1 flex flex-col items-center justify-center gap-6 animate-fade-in">
      <div className="w-[60px] h-[60px] rounded-full border-[1.5px] border-line-strong border-t-accent animate-spin-slow" />
      <div className="font-serif italic text-[22px] text-foreground">{GEN_LINES[lineIndex]}</div>
      <div className="text-[13px] text-faint animate-pulse-soft">
        This can take up to a minute — assembling real, in-stock parts
      </div>
    </div>
  );
}
