"use client";

import { useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { CalendarCheck, ExternalLink } from "lucide-react";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { Candidate } from "@/lib/types";
import { cn } from "@/lib/utils";

const STATUS_LABEL: Record<string, string> = {
  PENDING_SCREENING: "Waiting for screening",
  SCREENED: "Screened",
  EMAILED_PENDING_REPLY: "Emailed, waiting for reply",
  INTERVIEW_SCHEDULED: "Interview booked",
  RESCHEDULE_REQUESTED: "Reschedule requested",
};

function statusVariant(status: string): NonNullable<BadgeProps["variant"]> {
  if (status === "INTERVIEW_SCHEDULED") return "success";
  if (status === "RESCHEDULE_REQUESTED") return "warn";
  if (status === "SCREENED" || status === "EMAILED_PENDING_REPLY") return "default";
  return "default";
}

function scoreTone(score: number) {
  if (score >= 75) return { text: "text-ok", bar: "bg-ok" };
  if (score >= 50) return { text: "text-signal", bar: "bg-signal" };
  return { text: "text-bad", bar: "bg-bad" };
}

function formatTime(iso?: string | null) {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function CandidateRow({
  candidate,
  selected,
  onSelect,
}: {
  candidate: Candidate;
  selected: boolean;
  onSelect: (c: Candidate) => void;
}) {
  const [open, setOpen] = useState(false);
  const hasScore = typeof candidate.match_score === "number";
  const score = hasScore ? Math.round(candidate.match_score as number) : 0;
  const tone = scoreTone(score);
  const matched = candidate.matched_skills ?? [];
  const missing = candidate.missing_skills ?? [];
  const when = formatTime(candidate.agreed_timestamp);

  return (
    <li
      className={cn(
        "rounded-xl border bg-black/20 p-4 transition-colors",
        selected ? "border-signal/60" : "border-white/10"
      )}
    >
      <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
        <div className="min-w-0 flex-1 basis-48">
          <p className="truncate text-sm font-medium text-white">{candidate.name || candidate.candidate_email}</p>
          {candidate.name && <p className="truncate text-xs text-mute">{candidate.candidate_email}</p>}
        </div>

        <Badge variant={statusVariant(candidate.status)}>{STATUS_LABEL[candidate.status] ?? candidate.status}</Badge>

        {hasScore ? (
          <div
            className="w-36"
            onPointerEnter={(e) => e.pointerType !== "touch" && setOpen(true)}
            onPointerLeave={(e) => e.pointerType !== "touch" && setOpen(false)}
          >
            <button
              type="button"
              aria-expanded={open}
              aria-label={`Fit score ${score} out of 100. Show matched and missing skills`}
              onClick={() => setOpen((o) => !o)}
              onFocus={(e) => e.currentTarget.matches(":focus-visible") && setOpen(true)}
              onBlur={() => setOpen(false)}
              className="w-full rounded-lg text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-signal"
            >
              <span className={cn("font-display text-lg font-semibold", tone.text)}>{score}</span>
              <span className="text-xs text-mute"> / 100 fit</span>
              <span className="mt-1 block h-1.5 overflow-hidden rounded-full bg-white/10">
                <span className={cn("block h-full rounded-full", tone.bar)} style={{ width: `${Math.min(score, 100)}%` }} />
              </span>
            </button>
          </div>
        ) : (
          <span className="w-36 text-xs text-mute">Not scored yet</span>
        )}

        <Button size="sm" variant={selected ? "default" : "outline"} onClick={() => onSelect(candidate)}>
          Evaluate
        </Button>
      </div>

      {(when || candidate.calendar_event_link) && (
        <div className="mt-3 flex flex-wrap items-center gap-3 text-xs text-mute">
          {when && (
            <span className="inline-flex items-center gap-1.5">
              <CalendarCheck className="h-3.5 w-3.5 text-ok" aria-hidden />
              {when}
            </span>
          )}
          {candidate.calendar_event_link && (
            <a
              href={candidate.calendar_event_link}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-signal underline-offset-2 hover:underline"
            >
              Open calendar event <ExternalLink className="h-3 w-3" aria-hidden />
            </a>
          )}
        </div>
      )}

      <AnimatePresence initial={false}>
        {open && hasScore && (
          <motion.div
            key="skills"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="mt-4 grid gap-4 border-t border-white/10 pt-4 sm:grid-cols-2">
              <div>
                <p className="mb-2 text-xs font-medium text-ok">Matched skills</p>
                <div className="flex flex-wrap gap-1.5">
                  {matched.length === 0 && <span className="text-xs text-mute">None found</span>}
                  {matched.map((s) => (
                    <Badge key={s} variant="success">
                      {s}
                    </Badge>
                  ))}
                </div>
              </div>
              <div>
                <p className="mb-2 text-xs font-medium text-bad">Missing skills</p>
                <div className="flex flex-wrap gap-1.5">
                  {missing.length === 0 && <span className="text-xs text-mute">No gaps found</span>}
                  {missing.map((s) => (
                    <Badge key={s} variant="danger">
                      {s}
                    </Badge>
                  ))}
                </div>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </li>
  );
}

export function CandidateTable({
  candidates,
  selectedEmail,
  onSelect,
}: {
  candidates: Candidate[];
  selectedEmail: string | null;
  onSelect: (c: Candidate) => void;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Candidates</CardTitle>
        <CardDescription>Hover or focus a fit score to see matched and missing skills.</CardDescription>
      </CardHeader>
      <CardContent>
        {candidates.length === 0 ? (
          <p className="rounded-xl border border-dashed border-white/10 p-6 text-center text-sm text-mute">
            No candidates yet. Upload a job description and resumes to begin.
          </p>
        ) : (
          <ul className="space-y-3">
            {candidates.map((c) => (
              <CandidateRow
                key={c.candidate_email}
                candidate={c}
                selected={selectedEmail === c.candidate_email}
                onSelect={onSelect}
              />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
