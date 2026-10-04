# HireFlow AI

A multi-agent recruitment pipeline. Upload a job description and a stack of resumes. HireFlow screens each candidate, emails the shortlist, books interviews on your calendar, and gives a hire or reject recommendation after the interview.


## Live Demo

**[https://hireflow-ai-rho-tawny.vercel.app](https://hireflow-ai-rho-tawny.vercel.app)**

No setup needed — the app above runs in demo (mock) mode. Click **"Try with sample data"**, then follow the steps in **"For judges"** below.
## Source Code

**[https://github.com/sameenf019-cloud/hireflow-ai](https://github.com/sameenf019-cloud/hireflow-ai)**
## Agents

1. **Ingestion** — parses and indexes the job description and resumes
2. **Screening Agent** — scores each candidate against the job
3. **Outreach Agent** — emails shortlisted candidates
4. **Scheduling Agent** — books interviews on the calendar
5. **Evaluator Agent** — gives a HIRE or REJECT recommendation from interview notes

## Stack

- Agents: CrewAI (sequential handoff) with Groq `llama-3.3-70b-versatile`
- Parsing and search: PyMuPDF, Qdrant + FastEmbed (hybrid dense + BM25)
- State: SQLite
- Integrations: Gmail and Google Calendar (mock mode is on by default)
- Backend: FastAPI
- Frontend: Next.js, Tailwind, shadcn/ui, Aceternity, Framer Motion

## Requirements

- Python 3.11–3.13
- Node.js 18+
- A free [Groq API key](https://console.groq.com/)
- **Windows only:** the Microsoft Visual C++ 2015–2022 x64 Redistributable must be installed. Without it, `onnxruntime` fails to import with `DLL load failed`. [Download it here](https://aka.ms/vs/17/release/vc_redist.x64.exe) if you hit that error.

## Setup

### 1. Clone and set up the backend

```
git clone https://github.com/sameenf019-cloud/hireflow-ai.git
```
```
cd hireflow-ai\backend
```
```
python -m venv venv
```
```
venv\Scripts\Activate.ps1
```
```
pip install -r requirements.txt
```
