"use client";

import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { CalendarClock, Check, FileText, Mail, Scale, ScanSearch, type LucideIcon } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { AgentName, PipelineStatus } from "@/lib/types";
import { cn } from "@/lib/utils";

const STAGES: { id: AgentName; label: string; Icon: LucideIcon }[] = [
  { id: "ingestion", label: "Ingestion", Icon: FileText },
  { id: "screening", label: "Screening Agent", Icon: ScanSearch },
  { id: "outreach", label: "Outreach Agent", Icon: Mail },
  { id: "scheduling", label: "Scheduling Agent", Icon: CalendarClock },
  { id: "evaluator", label: "Evaluator Agent", Icon: Scale },
];

// Pipeline stages are 0-3; the Evaluator is run separately after interviews.
const PIPELINE_LAST_INDEX = 3;

export function AgentVisualizer({
  status,
  finished,
}: {
  status: PipelineStatus | null;
  finished: boolean;
}) {
  const reduce = useReducedMotion();
  const activeIdx = status?.running && status.agent ? STAGES.findIndex((s) => s.id === status.agent) : -1;

  const isDone = (i: number) => (finished && i <= PIPELINE_LAST_INDEX) || (activeIdx >= 0 && i < activeIdx);

  const message = status?.running
    ? status.message || "Working..."
    : finished
      ? "Run complete. Candidate statuses are up to date."
      : "Idle. Upload files, then start a run.";

  return (
    <Card>
      <CardHeader>
        <CardTitle>Agent handoff</CardTitle>
        <CardDescription>Each agent passes its result to the next one.</CardDescription>
      </CardHeader>
      <CardContent>
       <ol className="flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-center sm:gap-y-4">
          {STAGES.map((stage, i) => {
            const active = i === activeIdx;
            const done = isDone(i);
            return (
              <li key={stage.id} className="flex items-center gap-3 sm:flex-1 sm:last:flex-none">
                <div className="flex items-center gap-2">
                  <span
                    className={cn(
                      "relative flex h-10 w-10 shrink-0 items-center justify-center rounded-full border transition-colors",
                      active && "border-signal bg-signal/15 text-signal",
                      done && "border-ok/40 bg-ok/10 text-ok",
                      !active && !done && "border-white/10 bg-white/5 text-mute"
                    )}
                  >
                    {done ? (
                      <Check className="h-4 w-4" aria-hidden />
                    ) : (
                      <stage.Icon className="h-4 w-4" aria-hidden />
                    )}
                    {active && !reduce && (
                      <motion.span
                        aria-hidden
                        className="absolute inset-0 rounded-full border border-signal"
                        initial={{ scale: 1, opacity: 0.7 }}
                        animate={{ scale: 1.5, opacity: 0 }}
                        transition={{ duration: 1.3, repeat: Infinity, ease: "easeOut" }}
                      />
                    )}
                  </span>
                  <span className={cn("text-sm", active ? "font-medium text-white" : "text-mute")}>
                    {stage.label}
                    {active && <span className="sr-only"> (working)</span>}
                    {done && <span className="sr-only"> (done)</span>}
                  </span>
                </div>
                {i < STAGES.length - 1 && (
                  <div aria-hidden className="relative mx-1 hidden h-px flex-1 overflow-hidden bg-white/10 sm:block">
                    {activeIdx === i + 1 && !reduce && (
                      <motion.span
                        className="absolute top-0 h-px w-1/3 bg-signal"
                        initial={{ left: "-33%" }}
                        animate={{ left: "100%" }}
                        transition={{ duration: 1.1, repeat: Infinity, ease: "linear" }}
                      />
                    )}
                    {isDone(i + 1) && <span className="absolute inset-0 bg-ok/40" />}
                  </div>
                )}
              </li>
            );
          })}
        </ol>

        <div className="mt-6 min-h-[2.5rem] rounded-xl border border-white/10 bg-black/25 px-4 py-3" role="status" aria-live="polite">
          <AnimatePresence mode="wait" initial={false}>
            <motion.p
              key={message}
              initial={reduce ? false : { opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={reduce ? { opacity: 0 } : { opacity: 0, y: -6 }}
              transition={{ duration: 0.18 }}
              className={cn("text-sm", status?.running ? "text-white" : "text-mute")}
            >
              {message}
            </motion.p>
          </AnimatePresence>
        </div>
      </CardContent>
    </Card>
  );
}
