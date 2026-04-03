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
# Automatically loads OPENAI_API_KEY and JIRA credentials from .env
load_dotenv()

# Change this if your ticket ID is different
TARGET_JIRA_TICKET = os.environ.get("TARGET_JIRA_TICKET", "KAN-19") 

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

    summary = issue.fields.summary
    description = issue.fields.description
    issue_type = issue.fields.issuetype.name
    
    requirements_text = f"TITLE ({issue_type}): {summary}\n\nDESCRIPTION:\n{description}\n"
    
    if issue_type == 'Epic':
        print(f"📦 Epic detected. Extracting child stories...")
        jql_query = f'parent = "{ticket_id}" OR "Epic Link" = "{ticket_id}"'
        try:
            child_issues = jira_client.search_issues(jql_query)
            requirements_text += "\nCHILD STORIES:\n" + "="*20 + "\n"
            for child in child_issues:
                requirements_text += f"- {child.fields.summary} ({child.key}): {child.fields.description}\n"
        except JIRAError:
            pass
            
    return requirements_text

jira_requirements = get_ticket_details(TARGET_JIRA_TICKET)

# --- 3. THE QA TESTING TOOL ---
class PythonTesterSchema(BaseModel):
    code_string: str = Field(..., description="Raw Python script string.")

class PythonTesterTool(BaseTool):
    name: str = "Python Code Execution and Syntax Tester"
    description: str = (
        "Executes the provided Python code in a subprocess. "
        "Returns SUCCESS if the server boots and stays stable for 4 seconds. "
        "Returns the specific TRACEBACK error if it crashes."
    )
    args_schema: type[BaseModel] = PythonTesterSchema

    def _run(self, code_string: str) -> str:
        # Strip potential markdown formatting from the LLM input
        code = code_string.replace('```python', '').replace('```', '').strip()
        test_filename = "ai_test_run.py"
        
        with open(test_filename, "w", encoding="utf-8") as f:
            f.write(code)
            
        try:
            print("\n⏳ [QA Tool] Booting up Flask server to check for crashes...")
            # We wait 4 seconds. A successful Flask server blocks the process.
            result = subprocess.run(
                ["python", test_filename], 
                capture_output=True, 
                text=True, 
                timeout=4
            )
            # If it exits before 4 seconds, it either crashed or finished immediately.
            if result.returncode != 0:
                return f"CRASH DETECTED:\n{result.stderr[-1000:]}"
            return "SUCCESS: Script executed without error."
            
        except subprocess.TimeoutExpired:
            # For a Flask app, timing out is actually a sign of success (the server stayed up)
            return "SUCCESS: Server is stable and running."
        except Exception as e:
            return f"Unexpected Tool Error: {e}"

python_tester_tool = PythonTesterTool()

# --- 4. THE MODELS ---
# Using high-reasoning for architecture and coding-specific models for the build
architect_llm = LLM(model='gpt-5-mini-2025-08-07') 
coder_llm = LLM(model='gpt-5-mini-2025-08-07')

# --- 5. THE AGENTS (Modular "Artifact" Assembly Line) ---

architect = Agent(
    role='System Architect',
    goal='Create a high-level technical blueprint that prevents security and logic flaws.',
    backstory="""You are a security-first System Architect. You translate Jira requirements into 
    Technical Specifications. You define the exact SQLAlchemy models, the route map, and 
    the mandatory security logic (e.g. strict filtering). You DO NOT write the full code, 
    only the technical blueprint (The Artifact).""",
    llm=architect_llm,
    verbose=True,
    allow_delegation=False
)

coder = Agent(
    role='Senior Python Developer',
    goal='Implement the Technical Blueprint into a single-file Flask application.',
    backstory="""You are a master of Python and Flask. You take an Architect's blueprint 
    and turn it into perfect, clean, single-file code. You handle templates as inline strings 
    and ensure the logic is fully functional. You never use placeholders for core features.""",
    llm=coder_llm,
    verbose=True,
    allow_delegation=False
)

qa_auditor = Agent(
    role='Security Auditor & QA Engineer',
    goal='Audit the code for logic flaws and test for runtime errors.',
    backstory="""You are a pedantic QA Engineer. You check the code for lazy logic shortcuts 
    like User.query.first() without ID filters. You ensure password reset flows are secure 
    and all external integrations are mocked correctly.""",
    llm=coder_llm,
    verbose=True,
    allow_delegation=False,
    tools=[python_tester_tool]
)

# --- 6. THE TASKS ---

# Task 1: Generate the Specification (The "Artifact")
blueprint_task = Task(
    description=(
        f"Analyze these Jira requirements:\n{jira_requirements}\n\n"
        "Create a Technical Blueprint. You MUST include:\n"
        "1. Database Schema with relationships.\n"
        "2. Mandatory Security Rules: (e.g. Sensitive routes must filter by ID/Email, never using query.first() placeholders).\n"
        "3. Explicit Route logic definitions for Registration, Login, Shortening, and Verification."
    ),
    expected_output="A structured Technical Specification document.",
    agent=architect
)

# Task 2: Implementation (Contextual Build)
coding_task = Task(
    description=(
        "Using the Architect's Blueprint, write a complete, single-file Flask application. "
        "CRITICAL RULES:\n"
        "- Use render_template_string (absolutely NO f-strings for HTML).\n"
        "- Mock email sending by printing to console.\n"
        "- Ensure db.create_all() is wrapped in app.app_context().\n"
        "- Implement ACTUAL logic for URL shortening (hashing/unique ID generation)."
    ),
    expected_output="A self-contained Python script with a logic plan in the comments.",
    agent=coder,
    context=[blueprint_task]
)

# Task 3: Security Audit and Stability Test
audit_task = Task(
    description=(
        "Perform a final audit on the generated code.\n"
        "1. Check for logic vulnerabilities: Does password reset filter by the specific requester?\n"
        "2. Validate inputs: Are URLs checked for valid formatting before saving?\n"
        "3. Execution Test: Run the Python Tester Tool. If it fails, fix the code and repeat until it passes."
    ),
    expected_output="The finalized, audited, and stable Python code in a markdown block.",
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

print(f"🚀 Starting Multi-Agent Assembly Line (o3-mini + gpt-5-codex)...\n")
result = crew.kickoff()

# --- 8. SAVE THE HARDENED RESULT ---
output_string = result.raw if hasattr(result, 'raw') else str(result)
code_match = re.search(r'```python\n(.*?)\n```', output_string, re.DOTALL)

if code_match:
    final_code = code_match.group(1)
    with open("generated_app.py", "w", encoding="utf-8") as f:
        f.write(final_code)
    print("\n🎉 SUCCESS! Secure, audited code saved to 'generated_app.py'.")
else:
    print("\n⛔ Failed to isolate code block. Full raw output follows:\n")
    print(output_string)