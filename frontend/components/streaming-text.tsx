"use client";

import { useEffect, useMemo, useState } from "react";
import { useReducedMotion } from "framer-motion";

// Reveals text one word at a time. The full text stays available to screen readers.
export function StreamingText({
  text,
  wordsPerSecond = 14,
  className,
}: {
  text: string;
  wordsPerSecond?: number;
  className?: string;
}) {
  const reduce = useReducedMotion();
  const words = useMemo(() => text.split(/(\s+)/).filter((w) => w.length > 0), [text]);
  const tokenCount = words.length;
  const [shown, setShown] = useState(0);

  useEffect(() => {
    if (reduce) {
      setShown(tokenCount);
      return;
    }
    setShown(0);
    const id = setInterval(() => {
      setShown((n) => {
        if (n >= tokenCount) {
          clearInterval(id);
          return n;
        }
        return n + 1;
      });
    }, 1000 / (wordsPerSecond * 2)); // tokens include whitespace, so double the tick rate
    return () => clearInterval(id);
  }, [text, tokenCount, wordsPerSecond, reduce]);

  return (
    <p className={className}>
      <span className="sr-only">{text}</span>
      <span aria-hidden>
        {words.slice(0, shown).join("")}
        {shown < tokenCount && <span className="ml-0.5 inline-block h-4 w-[2px] translate-y-0.5 bg-signal" />}
      </span>
    </p>
  );
}
