\# HireFlow AI



A multi-agent recruitment pipeline. Upload a job description and a stack of resumes. HireFlow screens each candidate, emails the shortlist, books interviews on your calendar, and gives a hire or reject recommendation after the interview.



\## Stack



\- Agents: CrewAI (sequential handoff) with Groq `llama-3.3-70b-versatile`

\- Parsing and search: PyMuPDF, Qdrant + FastEmbed (hybrid dense + BM25)

\- State: SQLite

\- Integrations: Gmail and Google Calendar (mock mode is on by default)

\- Backend: FastAPI

\- Frontend: Next.js, Tailwind, shadcn/ui, Aceternity, Framer Motion



\## Agents



1\. Ingestion: parses and indexes the job description and resumes

2\. Screening Agent: scores each candidate against the job

3\. Outreach Agent: emails shortlisted candidates

4\. Scheduling Agent: books interviews on the calendar

5\. Evaluator Agent: gives a HIRE or REJECT recommendation from interview notes



\## Project structure



```

hireflow-ai/

&#x20; backend/    FastAPI app, agents, tasks, RAG engine, tools

&#x20; frontend/   Next.js app

```



\## Setup



You need Python 3.11+ and Node.js 18+.



\### 1. Backend



```

cd backend

python -m venv venv

venv\\Scripts\\Activate.ps1

pip install -r requirements.txt

copy .env.example .env

```



Open `backend/.env` and set `GROQ\_API\_KEY`. Keep `GOOGLE\_MOCK\_MODE=true` unless you have real Google credentials.



Run the server:



```

uvicorn main:app --reload --port 8000

```



Check it works by opening http://localhost:8000/health. You should see `{"status":"ok", ...}`.



\### 2. Frontend



```

cd frontend

npm install

copy .env.local.example .env.local

npm run dev

```



Open http://localhost:3000.



\## Using the app



1\. Upload one job description and any number of resumes (PDF or DOCX), then click "Upload and ingest".

2\. Click "Screen and email candidates" and watch the agent handoff.

3\. Click "Check replies and schedule" after candidates have answered.

4\. Pick a candidate, add interview notes, and click "Get recommendation".



\## API routes



\- `GET /health`

\- `POST /jobs`, `POST /candidates`, `GET /candidates`, `GET /candidates/{email}`

\- `POST /candidates/{email}/run`, `POST /pipeline/run`, `POST /poll`

\- `POST /candidates/{email}/evaluate`

\- `GET /runs/{run\_id}`, `GET /runs/{run\_id}/events` (SSE)

\- `GET /candidates/{email}/evaluation/stream` (SSE)



\## Security



Never commit `.env`, `credentials.json` or `token.json`. They are listed in `.gitignore`.

