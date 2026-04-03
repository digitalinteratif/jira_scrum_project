import os
import re
import subprocess
from dotenv import load_dotenv
from crewai import Agent, Task, Crew, Process, LLM
from crewai.tools import BaseTool
from pydantic import BaseModel, Field
from jira import JIRA
from jira.exceptions import JIRAError

# --- 1. CONFIGURATION & SETUP ---
# override=True ensures the .env file values win over existing system environment variables
load_dotenv(override=True)

TARGET_JIRA_TICKET = os.environ.get("TARGET_JIRA_TICKET")

if not TARGET_JIRA_TICKET:
    print("❌ ERROR: TARGET_JIRA_TICKET not found in .env file.")
    exit(1)

# --- 2. FETCH THE STORY/EPIC FROM JIRA ---
def get_ticket_details(ticket_id):
    jira_server = os.environ.get("JIRA_SERVER")
    jira_email = os.environ.get("JIRA_EMAIL")
    jira_token = os.environ.get("JIRA_API_TOKEN")

    print(f"🔗 Pulling requirements for {ticket_id} from Jira...")
    jira_client = JIRA(options={'server': jira_server}, basic_auth=(jira_email, jira_token))
    
    try:
        issue = jira_client.issue(ticket_id)
    except JIRAError as e:
        return f"Error fetching ticket: {e.text}"

    requirements_text = f"TITLE: {issue.fields.summary}\n\nDESCRIPTION:\n{issue.fields.description}\n"
    
    if issue.fields.issuetype.name == 'Epic':
        jql_query = f'parent = "{ticket_id}" OR "Epic Link" = "{ticket_id}"'
        try:
            child_issues = jira_client.search_issues(jql_query)
            requirements_text += "\nCHILD STORIES:\n" + "="*20 + "\n"
            for child in child_issues:
                requirements_text += f"- {child.fields.summary}: {child.fields.description}\n"
        except JIRAError: pass
            
    return requirements_text

jira_requirements = get_ticket_details(TARGET_JIRA_TICKET)

# --- 3. THE QA TESTING TOOL ---
class PythonTesterSchema(BaseModel):
    code_string: str = Field(..., description="Raw Python script string.")

class PythonTesterTool(BaseTool):
    name: str = "Python Code Execution and Syntax Tester"
    description: str = "Executes code. Returns SUCCESS if stable for 4s, or the error traceback."
    args_schema: type[BaseModel] = PythonTesterSchema

    def _run(self, code_string: str) -> str:
        code = code_string.replace('```python', '').replace('```', '').strip()
        test_filename = "ai_test_run.py"
        with open(test_filename, "w", encoding="utf-8") as f:
            f.write(code)
        try:
            result = subprocess.run(["python", test_filename], capture_output=True, text=True, timeout=4)
            if result.returncode != 0: return f"CRASH:\n{result.stderr[-800:]}"
            return "SUCCESS"
        except subprocess.TimeoutExpired: return "SUCCESS"
        except Exception as e: return f"Error: {e}"

python_tester_tool = PythonTesterTool()

# --- 4. THE MODELS ---
architect_llm = LLM(model='gpt-5-mini-2025-08-07') 
coder_llm = LLM(model='gpt-5-mini-2025-08-07')

# --- 5. THE AGENTS ---

architect = Agent(
    role='Full-Stack Architect',
    goal='Design a secure URL shortener with a mandatory HTML5 User Interface.',
    backstory="""You are a veteran web architect. You hate 'headless' APIs for end-user tools. 
    You mandate that every user story must result in a visible HTML page. You explicitly 
    define the UI flow: Home -> Register -> Login -> Dashboard -> Shorten URL. 
    CRITICAL TECH NOTE: You are aware that Jinja2 'extends' does not work with 
    render_template_string in a single-file setup without a disk-based loader. 
    You mandate using a single 'LAYOUT' string and wrapping content inside it 
    manually or using a helper function to avoid TemplateNotFound errors.""",
    llm=architect_llm,
    verbose=True
)

coder = Agent(
    role='Senior Full-Stack Developer',
    goal='Build the Flask app with embedded HTML templates and logic.',
    backstory="""You write perfect Python and clean, modern HTML/CSS. You never return 
    JSON to a browser request. You use Flask's render_template_string for a single-file 
    experience. You implement secure logic (no query.first() hacks). 
    IMPORTANT: You avoid using '{% extends %}' because it crashes in single-file setups. 
    Instead, you use a helper function to wrap the page body in a common HTML layout string.""",
    llm=coder_llm,
    verbose=True
)

qa_auditor = Agent(
    role='Security & UI Auditor',
    goal='Ensure the app is not a blank page and is logically sound.',
    backstory="""You are a strict QA lead. You verify that the root page is an actual 
    webpage with buttons and forms. You audit the SQLAlchemy queries for security leaks. 
    You specifically test the index route to ensure it doesn't trigger TemplateNotFound errors.""",
    llm=coder_llm,
    verbose=True,
    tools=[python_tester_tool]
)

# --- 6. THE TASKS ---

blueprint_task = Task(
    description=(
        f"Analyze: {jira_requirements}\n\n"
        "Create a Technical Blueprint. CRITICAL REQUIREMENTS:\n"
        "1. Define HTML templates for: Index, Login, Register, Dashboard.\n"
        "2. Security: Mandatory filtering by User ID in all routes.\n"
        "3. Template Strategy: DO NOT use 'extends/block' inheritance. "
        "Use a single HTML string with a placeholder like {{ content|safe }} for the body."
    ),
    expected_output="A full-stack Technical Specification mandating a functional UI without Jinja inheritance bugs.",
    agent=architect
)

coding_task = Task(
    description=(
        "Implement the Architect's Blueprint in ONE Python file.\n"
        "- The Home page ('/') MUST be a beautiful HTML splash page with Login/Register links.\n"
        "- All forms must be functional (use POST methods).\n"
        "- Avoid TemplateNotFound: Do not use '{% extends %}'. Define a 'render_layout' function "
        "that takes a content string and wraps it in a boilerplate HTML layout string.\n"
        "- Use render_template_string."
    ),
    expected_output="A complete Flask app script serving a full web UI using string-wrapping for layout.",
    agent=coder,
    context=[blueprint_task]
)

audit_task = Task(
    description=(
        "1. Logic Check: Ensure password reset and shortening filter data strictly by User ID.\n"
        "2. UI Check: Verify that '/' serves a real webpage. Check for Jinja 'extends' tags—"
        "if found, REJECT and REWRITE to use a simple layout wrapper.\n"
        "3. Stability: Run the Python Tester tool."
    ),
    expected_output="The finalized, hardened, UI-driven Python code in a markdown block.",
    agent=qa_auditor,
    context=[coding_task]
)

# --- 7. EXECUTION ---
crew = Crew(
    agents=[architect, coder, qa_auditor],
    tasks=[blueprint_task, coding_task, audit_task],
    process=Process.sequential,
    verbose=True
)

print(f"🚀 Building URL Shortener with Full UI for {TARGET_JIRA_TICKET}...\n")
result = crew.kickoff()

# --- 8. SAVE ---
output_string = result.raw if hasattr(result, 'raw') else str(result)
code_match = re.search(r'```python\n(.*?)\n```', output_string, re.DOTALL)

if code_match:
    final_code = code_match.group(1)
    with open("generated_app.py", "w", encoding="utf-8") as f:
        f.write(final_code)
    print("\n🎉 SUCCESS! Secure UI-driven code saved to 'generated_app.py'.")
else:
    print("\n⛔ Failed to isolate code.")