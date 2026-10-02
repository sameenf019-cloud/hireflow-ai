"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Inbox, Loader2, Play } from "lucide-react";
import { AgentVisualizer } from "@/components/agent-visualizer";
import { CandidateTable } from "@/components/candidate-table";
import { EvaluatorPanel } from "@/components/evaluator-panel";
import { SpotlightHero } from "@/components/spotlight-hero";
import { UploadPanel } from "@/components/upload-panel";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { getCandidates, runPipeline, pollGmail, runEventsUrl } from "@/lib/api";
import type { Candidate, PipelineStatus } from "@/lib/types";

type RunEvent = {
  stage?: string;
  agent?: string;
  message?: string;
};

export default function Dashboard() {
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [status, setStatus] = useState<PipelineStatus | null>(null);
  const [polling, setPolling] = useState(false);
  const [finished, setFinished] = useState(false);
  const [evaluating, setEvaluating] = useState(false);
  const [selectedEmail, setSelectedEmail] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const eventSourceRef = useRef<EventSource | null>(null);

  const refresh = useCallback(async () => {
    try {
      setCandidates(await getCandidates());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load candidates.");
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    return () => {
      eventSourceRef.current?.close();
    };
  }, []);

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

  const busy = polling;
  const selected = useMemo(
    () => candidates.find((c) => c.candidate_email === selectedEmail) ?? null,
    [candidates, selectedEmail]
  );

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

      <div className="grid gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        <UploadPanel disabled={busy} onUploaded={refresh} />

        <div className="flex flex-col gap-6">
          <AgentVisualizer status={visualStatus} finished={finished} />
          <Card>
            <CardHeader>
              <CardTitle>Run the pipeline</CardTitle>
              <CardDescription>
                Screening and outreach run on every pending candidate. Check replies after candidates have answered.
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-wrap gap-3">
              <Button onClick={() => startRun(runPipeline)} disabled={busy || candidates.length === 0}>
                {busy ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : <Play className="h-4 w-4" aria-hidden />}
                Screen and email candidates
              </Button>
              <Button variant="outline" onClick={() => startRun(pollGmail)} disabled={busy}>
                <Inbox className="h-4 w-4" aria-hidden />
                Check replies and schedule
              </Button>
            </CardContent>
          </Card>
        </div>
      </div>

      <CandidateTable candidates={candidates} selectedEmail={selectedEmail} onSelect={(c) => setSelectedEmail(c.candidate_email)} />

      <EvaluatorPanel candidate={selected} onEvaluating={setEvaluating} />
    </main>
  );
}