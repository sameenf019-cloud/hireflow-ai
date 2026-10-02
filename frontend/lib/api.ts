import type { Candidate } from "@/lib/types";

// Every browser call goes through the Next.js proxy (see next.config.mjs).
const BASE = "/backend";

// Matches backend/main.py exactly. If FastAPI routes change, change them here.
export const ENDPOINTS = {
  jobs: "/jobs", // POST multipart field "file" (1 JD file) -> { job_id }
  candidates: "/candidates", // GET -> { candidates }  |  POST multipart field "files" (many)
  candidate: (email: string) => `/candidates/${encodeURIComponent(email)}`,
  runCandidate: (email: string) => `/candidates/${encodeURIComponent(email)}/run`,
  runPipeline: "/pipeline/run",
  poll: "/poll",
  evaluate: (email: string) => `/candidates/${encodeURIComponent(email)}/evaluate`,
  evaluationStream: (email: string) => `/candidates/${encodeURIComponent(email)}/evaluation/stream`,
  run: (runId: string) => `/runs/${runId}`,
  runEvents: (runId: string) => `/runs/${runId}/events`,
} as const;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { cache: "no-store", ...init });
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      detail = typeof body?.detail === "string" ? body.detail : JSON.stringify(body);
    } catch {
      detail = await res.text().catch(() => "");
    }
    throw new Error(`${res.status} ${res.statusText}${detail ? ` - ${detail}` : ""}`);
  }
  return (await res.json()) as T;
}

// Stage: upload the job description
export async function uploadJob(file: File): Promise<{ job_id: string; filename: string; chars: number }> {
  const form = new FormData();
  form.append("file", file);
  return request(ENDPOINTS.jobs, { method: "POST", body: form });
}

// Stage: upload one or more resumes
export async function uploadResumes(files: File[], email?: string): Promise<{ results: unknown[] }> {
  const form = new FormData();
  files.forEach((file) => form.append("files", file));
  if (email && files.length === 1) form.append("email", email);
  return request(ENDPOINTS.candidates, { method: "POST", body: form });
}

export async function getCandidates(): Promise<Candidate[]> {
  const data = await request<{ candidates: Candidate[] }>(ENDPOINTS.candidates);
  return data.candidates ?? [];
}

export async function getCandidate(email: string): Promise<Candidate> {
  return request(ENDPOINTS.candidate(email));
}

// Route one candidate (background run) -> returns a run_id to track
export async function runCandidate(email: string, jobId?: string): Promise<{ run_id: string; events_url: string }> {
  return request(ENDPOINTS.runCandidate(email), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ job_id: jobId ?? null }),
  });
}

// Screen every PENDING_SCREENING candidate -> returns a run_id to track
export async function runPipeline(jobId?: string): Promise<{ run_id: string; candidates: string[]; events_url: string }> {
  return request(ENDPOINTS.runPipeline, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ job_id: jobId ?? null }),
  });
}

// Gmail poll + reschedule routing -> returns a run_id to track
export async function pollGmail(): Promise<{ run_id: string; events_url: string }> {
  return request(ENDPOINTS.poll, { method: "POST" });
}

// Stage 5 evaluation with manager's notes -> returns a run_id to track
export async function evaluateCandidate(
  email: string,
  interviewNotes: string,
  jobId?: string
): Promise<{ run_id: string; events_url: string }> {
  return request(ENDPOINTS.evaluate(email), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ interview_notes: interviewNotes, job_id: jobId ?? null }),
  });
}

// Poll a run's status/results directly (non-streaming)
export async function getRun(runId: string): Promise<{ run_id: string; kind: string; done: boolean; results: unknown; error: string | null; event_count: number }> {
  return request(ENDPOINTS.run(runId));
}

// For `new EventSource(...)` in a component — live agent-handoff feed
export function runEventsUrl(runId: string): string {
  return `${BASE}${ENDPOINTS.runEvents(runId)}`;
}

// For `new EventSource(...)` — word-by-word evaluation summary stream
export function evaluationStreamUrl(email: string): string {
  return `${BASE}${ENDPOINTS.evaluationStream(email)}`;
}