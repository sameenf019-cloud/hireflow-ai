"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Inbox, Loader2, MessageSquareReply, Play, RotateCcw } from "lucide-react";
import { AgentVisualizer } from "@/components/agent-visualizer";
import { CandidateTable } from "@/components/candidate-table";
import { EvaluatorPanel } from "@/components/evaluator-panel";
import { SpotlightHero } from "@/components/spotlight-hero";
import { UploadPanel } from "@/components/upload-panel";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { getCandidates, getDemoStatus, pollGmail, resetDemo, runEventsUrl, runPipeline, simulateDemoReply } from "@/lib/api";
import type { Candidate, DemoStatus, PipelineStatus } from "@/lib/types";

type RunEvent = {
  stage?: string;
  agent?: string;
  message?: string;
};

function statusOf(c: Candidate): string {
  return (c as unknown as { status?: string }).status ?? "";
}

// Candidate to evaluate when nobody has clicked Evaluate yet: the first sample
// candidate with a booked interview, else the first candidate with a booked interview.
function pickDefaultEmail(list: Candidate[]): string | null {
  const booked = list.filter((c) => statusOf(c) === "INTERVIEW_SCHEDULED");
  const sample = booked.find((c) => c.candidate_email.includes(".demo@"));
  return (sample ?? booked[0])?.candidate_email ?? null;
}

export default function Dashboard() {
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [status, setStatus] = useState<PipelineStatus | null>(null);
  const [polling, setPolling] = useState(false);
  const [finished, setFinished] = useState(false);
  const [evaluating, setEvaluating] = useState(false);
  const [selectedEmail, setSelectedEmail] = useState<string | null>(null);
  const [autoEmail, setAutoEmail] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [demo, setDemo] = useState<DemoStatus | null>(null);
  const [demoBusy, setDemoBusy] = useState(false);

  const eventSourceRef = useRef<EventSource | null>(null);

  const refresh = useCallback(async () => {
    try {
      setCandidates(await getCandidates());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load candidates.");
    }
  }, []);

  const refreshDemo = useCallback(async () => {
    try {
      setDemo(await getDemoStatus());
    } catch {
      setDemo(null); // backend without demo endpoints: simply hide the demo controls
    }
  }, []);

  const refreshAll = useCallback(async () => {
    await Promise.all([refresh(), refreshDemo()]);
  }, [refresh, refreshDemo]);

  useEffect(() => {
    refreshAll();
  }, [refreshAll]);

  useEffect(() => {
    return () => {
      eventSourceRef.current?.close();
    };
  }, []);

  // Keep a default candidate for the Final evaluation box, so a tester does not
  // have to click Evaluate first. Once chosen it stays put (even after the
  // candidate's status changes to EVALUATED) until the sample data is reset.
  useEffect(() => {
    if (selectedEmail) return;
    const stillThere = autoEmail !== null && candidates.some((c) => c.candidate_email === autoEmail);
    if (stillThere) return;
    setAutoEmail(pickDefaultEmail(candidates));
  }, [candidates, selectedEmail, autoEmail]);

  function watchRun(runId: string) {
    eventSourceRef.current?.close();
    const es = new EventSource(runEventsUrl(runId));
    eventSourceRef.current = es;

    es.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data) as RunEvent;
        setStatus({ running: true, agent: (data.agent ?? null) as PipelineStatus["agent"], message: data.message ?? "" });
      } catch {
        // ignore malformed events
      }
    };

    es.addEventListener("end", () => {
      es.close();
      eventSourceRef.current = null;
      setPolling(false);
      setFinished(true);
      setStatus(null);
      refresh();
    });

    es.onerror = () => {
      es.close();
      eventSourceRef.current = null;
      setPolling(false);
      setError("Lost connection to the live update stream.");
    };
  }

  async function startRun(action: () => Promise<{ run_id: string }>) {
    setError(null);
    setNote(null);
    setFinished(false);
    setStatus(null);
    setPolling(true);
    try {
      const { run_id } = await action();
      watchRun(run_id);
    } catch (e) {
      setPolling(false);
      setError(e instanceof Error ? e.message : "The run failed.");
    }
  }

  async function simulateReply() {
    setError(null);
    setNote(null);
    setDemoBusy(true);
    try {
      const result = await simulateDemoReply();
      setNote(
        `Simulated a reply from ${result.replied.length} shortlisted candidate${result.replied.length === 1 ? "" : "s"}. Now click "Check replies and schedule".`
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not simulate a reply.");
    } finally {
      setDemoBusy(false);
    }
  }

  async function resetSampleData() {
    setError(null);
    setNote(null);
    setDemoBusy(true);
    try {
      await resetDemo();
      setSelectedEmail(null);
      setAutoEmail(null);
      setFinished(false);
      setNote("Sample candidates removed. You can load the sample data again.");
      await refreshAll();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not reset the sample data.");
    } finally {
      setDemoBusy(false);
    }
  }

  const busy = polling;
  const selected = useMemo(() => {
    const email = selectedEmail ?? autoEmail;
    return candidates.find((c) => c.candidate_email === email) ?? null;
  }, [candidates, selectedEmail, autoEmail]);

  const visualStatus: PipelineStatus | null = evaluating
    ? { running: true, agent: "evaluator", message: "Evaluator Agent is weighing the interview notes against the job description..." }
    : status;

  return (
    <main className="mx-auto flex max-w-6xl flex-col gap-6 px-4 py-8 sm:px-6 sm:py-12">
      <SpotlightHero />

      {error && (
        <p role="alert" className="break-words rounded-xl border border-bad/30 bg-bad/10 px-4 py-3 text-sm text-bad">
          {error}
        </p>
      )}
      {note && (
        <p role="status" className="break-words rounded-xl border border-ok/30 bg-ok/10 px-4 py-3 text-sm text-ok">
          {note}
        </p>
      )}

      <div className="grid gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        <UploadPanel disabled={busy || demoBusy} demo={demo} onUploaded={refreshAll} />

        <div className="flex flex-col gap-6">
          <AgentVisualizer status={visualStatus} finished={finished} />
          <Card>
            <CardHeader>
              <CardTitle>Run the pipeline</CardTitle>
              <CardDescription>
                Screening and outreach run on every pending candidate. Check replies after candidates have answered.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="flex flex-wrap gap-3">
                <Button onClick={() => startRun(runPipeline)} disabled={busy || demoBusy || candidates.length === 0}>
                  {busy ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : <Play className="h-4 w-4" aria-hidden />}
                  Screen and email candidates
                </Button>
                <Button variant="outline" onClick={() => startRun(pollGmail)} disabled={busy || demoBusy}>
                  <Inbox className="h-4 w-4" aria-hidden />
                  Check replies and schedule
                </Button>
              </div>

              {demo?.available && (
                <div className="space-y-3 border-t border-white/10 pt-4">
                  <p className="text-sm text-mute">
                    Demo mode (no real emails are sent). Steps: 1) Try with sample data, 2) Screen and email candidates,
                    3) Simulate candidate reply, 4) Check replies and schedule, 5) scroll down to Final evaluation (the
                    candidate with a booked interview is selected for you), click a sample notes button, then click Get
                    recommendation.
                  </p>
                  <div className="flex flex-wrap gap-3">
                    <Button variant="outline" onClick={simulateReply} disabled={busy || demoBusy}>
                      {demoBusy ? (
                        <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                      ) : (
                        <MessageSquareReply className="h-4 w-4" aria-hidden />
                      )}
                      Simulate candidate reply
                    </Button>
                    <Button variant="outline" onClick={resetSampleData} disabled={busy || demoBusy}>
                      <RotateCcw className="h-4 w-4" aria-hidden />
                      Reset sample data
                    </Button>
                  </div>
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>

      <CandidateTable candidates={candidates} selectedEmail={selectedEmail} onSelect={(c) => setSelectedEmail(c.candidate_email)} />

      <EvaluatorPanel candidate={selected} onEvaluating={setEvaluating} />
    </main>
  );
}