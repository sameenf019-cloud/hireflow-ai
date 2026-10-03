"""Sample data for the hackathon demo (judges can test without their own files).

Everything here is plain text, so no PDF is needed. The sample candidates use
@example.com addresses, which is why the demo endpoints only work in mock mode
(GOOGLE_MOCK_MODE=true): in real mode those emails would bounce.
"""
from datetime import date, timedelta

DEMO_JOB_TITLE = "AI Software Engineer (sample)"

DEMO_JOB_DESCRIPTION = """AI Software Engineer
HireFlow AI | Islamabad, Pakistan (Hybrid) | Full-time | Experience: 2-5 years

ABOUT THE ROLE
We are looking for a Python-based AI Software Engineer to build and maintain LLM-powered
applications and multi-agent automation workflows. You will work on backend APIs,
retrieval-augmented generation (RAG) pipelines and integrations with third-party services
such as Gmail and Google Calendar.

RESPONSIBILITIES
- Design, build and deploy backend services and REST APIs using Python and FastAPI.
- Develop LLM applications, including RAG pipelines with vector databases such as Qdrant or FAISS.
- Build multi-agent workflows using frameworks such as CrewAI or LangChain.
- Integrate external APIs (Gmail, Google Calendar, OAuth) to automate business workflows.
- Write clean, tested and documented code, and take part in code reviews.
- Work with the frontend team (React/Next.js) to deliver complete features.

REQUIRED SKILLS
- Strong Python programming (2+ years of professional experience).
- Experience with FastAPI or Flask and SQL databases (SQLite or PostgreSQL).
- Hands-on experience with LLM APIs (Groq, OpenAI or similar) and prompt engineering.
- Understanding of embeddings, vector search and RAG.
- Git and GitHub; basic knowledge of Docker.

PREFERRED SKILLS
- Experience with CrewAI, LangChain or other agent frameworks.
- Frontend experience with React, Next.js and Tailwind CSS.
- Experience with Google Cloud APIs and OAuth.
- Experience deploying applications to cloud platforms.

EDUCATION
Bachelor's degree in Computer Science, Software Engineering or a related field.
"""

DEMO_RESUMES = [
    {
        "name": "Sara Malik",
        "email": "sara.malik.demo@example.com",
        "filename": "Sara_Malik_Resume.pdf",
        "text": """Sara Malik
Software Developer (Python, AI/ML, Full-Stack) | Islamabad, Pakistan
Email: sara.malik.demo@example.com

PROFESSIONAL SUMMARY
Software developer with 4 years of experience building Python backends, REST APIs and
AI-powered applications. Strong hands-on experience with LLM applications, retrieval-augmented
generation (RAG), multi-agent workflows, FastAPI and React/Next.js.

TECHNICAL SKILLS
Languages: Python, JavaScript, TypeScript, SQL
Backend: FastAPI, Flask, REST APIs, SQLite, PostgreSQL, Docker
AI/ML: LLMs, CrewAI, LangChain, RAG, vector databases (Qdrant, FAISS), embeddings, prompt engineering, Groq, OpenAI APIs
Frontend: React, Next.js, Tailwind CSS
Tools: Git, GitHub, Google Cloud (Gmail and Calendar APIs), Linux

PROFESSIONAL EXPERIENCE
Software Engineer, TechNova Solutions, Islamabad (2023 - Present)
- Built and deployed FastAPI microservices serving 50,000+ requests per day.
- Developed an LLM-based document assistant using RAG with Qdrant, cutting support lookup time by 40%.
- Designed multi-agent automation workflows in Python using CrewAI and Groq-hosted open-source models.
- Integrated Gmail and Google Calendar APIs to automate email outreach and interview scheduling.
Junior Python Developer, CodeCraft Labs, Rawalpindi (2022 - 2023)
- Created REST APIs and data pipelines in Python and SQL for e-commerce clients.
- Wrote unit tests with pytest and raised coverage from 40% to 80%.

EDUCATION
BS Computer Science, COMSATS University Islamabad (2018 - 2022)
""",
    },
    {
        "name": "Usman Tariq",
        "email": "usman.tariq.demo@example.com",
        "filename": "Usman_Tariq_Resume.pdf",
        "text": """Usman Tariq
Full-Stack Web Developer | Lahore, Pakistan
Email: usman.tariq.demo@example.com

PROFESSIONAL SUMMARY
Web developer with 3 years of experience building websites and web apps with Python Flask
and React. Recently started experimenting with AI chatbots using the OpenAI API.

TECHNICAL SKILLS
Languages: Python, JavaScript, SQL
Backend: Flask, REST APIs, MySQL
Frontend: React, HTML, CSS, Bootstrap
Tools: Git, GitHub, Postman
AI: OpenAI API (basic chatbot prompts)

EXPERIENCE
Web Developer, BrightSoft, Lahore (2023 - Present)
- Built customer portals with Flask, MySQL and React for 6 clients.
- Added a simple support chatbot using the OpenAI API and prompt templates.
- Fixed bugs and shipped features in a small agile team.
Junior Developer, WebWorks, Lahore (2022 - 2023)
- Developed landing pages and admin dashboards with React and Bootstrap.

EDUCATION
BS Software Engineering, University of Central Punjab, Lahore (2018 - 2022)
""",
    },
    {
        "name": "Hina Qureshi",
        "email": "hina.qureshi.demo@example.com",
        "filename": "Hina_Qureshi_Resume.pdf",
        "text": """Hina Qureshi
Graphic Designer and Social Media Manager | Karachi, Pakistan
Email: hina.qureshi.demo@example.com

PROFESSIONAL SUMMARY
Creative graphic designer with 5 years of experience in branding, social media design and
video editing for retail and fashion brands.

SKILLS
Adobe Photoshop, Adobe Illustrator, Premiere Pro, Canva, Figma, typography, brand identity,
social media campaigns, photography

EXPERIENCE
Senior Graphic Designer, StyleHub, Karachi (2022 - Present)
- Designed brand identities and logos for 30+ clients.
- Grew client social media followers by 60% with campaign designs.
Graphic Designer, Pixel Studio, Karachi (2020 - 2022)
- Produced print materials including brochures, banners and packaging.

EDUCATION
BFA Visual Communication Design, Indus Valley School of Art, Karachi (2016 - 2020)
""",
    },
]

DEMO_EMAILS = [r["email"] for r in DEMO_RESUMES]


def demo_reply_text() -> str:
    """A candidate reply proposing a specific time a few working days from today."""
    day = date.today() + timedelta(days=3)
    while day.weekday() >= 5:  # skip Saturday/Sunday
        day += timedelta(days=1)
    return (
        f"Hi, thank you for the invitation. I am available on {day.strftime('%A')} "
        f"{day.day} {day.strftime('%B %Y')} at 2:00 PM Pakistan time (UTC+5)."
    )
