"use client";

import { useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { evaluateCandidate, evaluationStreamUrl, runEventsUrl } from "@/lib/api";
import type { Candidate, EvaluationResult } from "@/lib/types";

export function EvaluatorPanel({
  candidate,
  onEvaluating,
}: {
  candidate: Candidate | null;
  onEvaluating: (active: boolean) => void;
}) {
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [recommendation, setRecommendation] = useState<EvaluationResult["recommendation"] | null>(null);
  const [summary, setSummary] = useState("");

  const runEsRef = useRef<EventSource | null>(null);
  const evalEsRef = useRef<EventSource | null>(null);

  const email = candidate?.candidate_email ?? null;

  useEffect(() => {
    setNotes("");
    setRecommendation(null);
    setSummary("");
    setError(null);
  }, [email]);

  useEffect(() => {
    return () => {
      runEsRef.current?.close();
      evalEsRef.current?.close();
    };
  }, []);

  function watchEvaluationStream(candidateEmail: string) {
    evalEsRef.current?.close();
    const es = new EventSource(evaluationStreamUrl(candidateEmail));
    evalEsRef.current = es;

    es.addEventListener("meta", (e) => {
      try {
        const data = JSON.parse((e as MessageEvent).data);
        setRecommendation(String(data.recommendation ?? "").toUpperCase() === "HIRE" ? "HIRE" : "REJECT");
      } catch {
        // ignore malformed events
      }
    });

    es.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data) as { word?: string };
        if (data.word) setSummary((prev) => (prev ? `${prev} ${data.word}` : data.word!));
      } catch {
        // ignore malformed events
      }
    };

    es.addEventListener("end", () => {
      es.close();
      evalEsRef.current = null;
      setBusy(false);
      onEvaluating(false);
    });

    es.onerror = () => {
      es.close();
      evalEsRef.current = null;
      setBusy(false);
      onEvaluating(false);
      setError((prev) => prev ?? "Lost connection while streaming the evaluation.");
    };
  }

  function watchRun(runId: string, candidateEmail: string) {
    runEsRef.current?.close();
    const es = new EventSource(runEventsUrl(runId));
    runEsRef.current = es;

    es.addEventListener("end", () => {
      es.close();
      runEsRef.current = null;
      watchEvaluationStream(candidateEmail);
    });

    es.onerror = () => {
      es.close();
      runEsRef.current = null;
      setBusy(false);
      onEvaluating(false);
      setError("The evaluation run failed to complete.");
    };
  }

  async function submit() {
    if (!candidate || !notes.trim()) return;
    setBusy(true);
    setError(null);
    setRecommendation(null);
    setSummary("");
    onEvaluating(true);
    try {
      const { run_id } = await evaluateCandidate(candidate.candidate_email, notes.trim());
      watchRun(run_id, candidate.candidate_email);
    } catch (e) {
      setBusy(false);
      onEvaluating(false);
      setError(e instanceof Error ? e.message : "Evaluation failed.");
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Final evaluation</CardTitle>
        <CardDescription>
          {candidate
            ? `Add your interview notes for ${candidate.name || candidate.candidate_email}.`
            : "Select a candidate with Evaluate to add interview notes."}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <Textarea
          aria-label="Interview notes"
          placeholder="What stood out in the interview? Strengths, concerns, anything the resume did not show."
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          disabled={!candidate || busy}
        />
        <Button onClick={submit} disabled={!candidate || busy || !notes.trim()}>
          {busy && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
          {busy ? "Evaluating" : "Get recommendation"}
        </Button>

        {error && (
          <p role="alert" className="break-words text-sm text-bad">
            {error}
          </p>
        )}

        {(recommendation || summary) && (
          <div className="rounded-xl border border-white/10 bg-black/25 p-4">
            {recommendation && (
              <Badge variant={recommendation === "HIRE" ? "success" : "danger"}>
                {recommendation === "HIRE" ? "Recommend hire" : "Recommend reject"}
              </Badge>
            )}
            <p className="mt-3 max-w-prose text-sm leading-relaxed text-white/90">{summary}</p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}