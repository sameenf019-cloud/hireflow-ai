"use client";

import { motion, useMotionTemplate, useMotionValue } from "framer-motion";

// Aceternity-style spotlight: a soft light that follows the cursor across the hero.
export function SpotlightHero() {
  const x = useMotionValue(-500);
  const y = useMotionValue(-500);
  const spotlight = useMotionTemplate`radial-gradient(420px circle at ${x}px ${y}px, rgba(245,181,68,0.16), transparent 70%)`;

  return (
    <section
      onMouseMove={(e) => {
        const rect = e.currentTarget.getBoundingClientRect();
        x.set(e.clientX - rect.left);
        y.set(e.clientY - rect.top);
      }}
      className="relative overflow-hidden rounded-3xl border border-white/10 bg-white/[0.03] px-6 py-12 backdrop-blur-xl sm:px-12 sm:py-16"
    >
      <div aria-hidden className="dot-grid pointer-events-none absolute inset-0" />
      <motion.div aria-hidden className="pointer-events-none absolute inset-0" style={{ background: spotlight }} />
      <div className="relative max-w-2xl">
        <h1 className="font-display text-4xl font-semibold leading-[1.1] tracking-tight text-white sm:text-5xl">
          Four agents run your hiring pipeline.
        </h1>
        <p className="mt-5 max-w-xl text-base leading-relaxed text-mute">
          Upload a job description and a stack of resumes. HireFlow screens each candidate, emails the shortlist,
          books interviews on your calendar, and gives you a hire or reject recommendation after the interview.
        </p>
      </div>
    </section>
  );
}
