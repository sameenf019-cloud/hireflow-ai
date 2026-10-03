"use client";

import { useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { evaluateCandidate, evaluationStreamUrl, runEventsUrl } from "@/lib/api";
import type { Candidate, EvaluationResult } from "@/lib/types";

// Ready-made interview notes so a tester can try the evaluation without writing any.
const SAMPLE_NOTES_STRONG =
  "Strong technical interview. Explained RAG design clearly, including hybrid search with a vector database, and described a multi-agent workflow built with CrewAI. Solid FastAPI and SQL knowledge and answered follow-up questions confidently. Communicated clearly with concrete examples from past projects. Minor gap in Docker deployment, but learns quickly. Would be a good addition to the team.";
const SAMPLE_NOTES_WEAK =
  "Struggled with basic Python questions. Could not explain how embeddings or vector search work. Answers about past projects were vague, with little hands-on experience of the tools in the job description. Communication was fine, but the technical depth is not there yet. Not ready for this role.";

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
  const displayName = candidate ? candidate.name || candidate.candidate_email : "";

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
            ? `Evaluating ${displayName}. Write your interview notes, or click one of the sample notes buttons below.`
            : "No candidate is ready yet. Finish steps 1 to 4 above (the candidate with a booked interview is selected automatically), or click Evaluate on any candidate in the table."}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {candidate && (
          <p className="text-sm text-white/80">
            Selected candidate: <span className="font-medium text-white">{displayName}</span>
          </p>
        )}
        <Textarea
          aria-label="Interview notes"
          placeholder="What stood out in the interview? Strengths, concerns, anything the resume did not show."
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          disabled={!candidate || busy}
        />
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm text-mute">No notes handy?</span>
          <Button
            type="button"
            variant="outline"
            onClick={() => setNotes(SAMPLE_NOTES_STRONG)}
            disabled={!candidate || busy}
          >
            Use sample notes (strong)
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={() => setNotes(SAMPLE_NOTES_WEAK)}
            disabled={!candidate || busy}
          >
            Use sample notes (weak)
          </Button>
        </div>
        <Button onClick={submit} disabled={!candidate || busy || !notes.trim()}>
          {busy && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
          {busy ? "Evaluating" : "Get recommendation"}
        </Button>
        {candidate && !notes.trim() && !busy && (
          <p className="text-sm text-mute">Click a sample notes button or type notes, then click Get recommendation.</p>
        )}

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