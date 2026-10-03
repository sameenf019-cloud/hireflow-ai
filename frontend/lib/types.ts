export type CandidateStatus =
  | "PENDING_SCREENING"
  | "SCREENED"
  | "EMAILED_PENDING_REPLY"
  | "INTERVIEW_SCHEDULED"
  | "RESCHEDULE_REQUESTED"
  | (string & {});

export interface Candidate {
  candidate_email: string;
  name?: string | null;
  status: CandidateStatus;
  match_score?: number | null;
  matched_skills?: string[];
  missing_skills?: string[];
  screening_decision?: "SHORTLIST" | "REJECT" | null;
  agreed_timestamp?: string | null;
  calendar_event_link?: string | null;
}

export type AgentName = "ingestion" | "screening" | "outreach" | "scheduling" | "evaluator";

export interface PipelineStatus {
  running: boolean;
  agent: AgentName | null;
  message: string;
  candidate_email?: string | null;
}

export interface EvaluationResult {
  candidate_email: string;
  recommendation: "HIRE" | "REJECT";
  summary: string;
}

// Sample-data (demo) mode, reported by GET /demo/status
export interface DemoStatus {
  available: boolean; // true only when the backend runs in mock mode
  loaded: boolean; // true when all sample candidates are already loaded
  candidates: number;
}
