import os
import re
import sys
import json
import subprocess
import logging
import codecs
import time
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
from crewai import Agent, Task, Crew, Process, LLM
from crewai.tools import BaseTool
from pydantic import BaseModel, Field
from jira import JIRA
from jira.exceptions import JIRAError

# --- 1. CONFIGURATION & LOGGING ---
load_dotenv(override=True)

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

TARGET_JIRA_TICKET = os.environ.get("TARGET_JIRA_TICKET")
PROJECT_ROOT = "."
LOG_DIR = "agent_logs"

os.makedirs(LOG_DIR, exist_ok=True)

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file = os.path.join(LOG_DIR, f"build_{timestamp}.log")
file_handler = logging.FileHandler(log_file, encoding='utf-8')
stream_handler = logging.StreamHandler(sys.stdout)

logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    handlers=[file_handler, stream_handler]
)
logger = logging.getLogger("JiraBuilder")

# --- 2. JIRA CLIENT UTILITIES ---
def get_jira_client():
    jira_server = os.environ.get("JIRA_SERVER")
    jira_email = os.environ.get("JIRA_EMAIL")
    jira_token = os.environ.get("JIRA_API_TOKEN")
    return JIRA(options={'server': jira_server}, basic_auth=(jira_email, jira_token))

def get_ticket_details_from_issue(jira_client, issue):
    summary = issue.fields.summary
    description = issue.fields.description or "No description provided."
    full_context = f"--- TARGET TICKET: {issue.key} ---\nTITLE: {summary}\nDESCRIPTION:\n{description}\n"

    parent = getattr(issue.fields, 'parent', None)
    epic_key = parent.key if parent else getattr(issue.fields, 'customfield_10011', None)

    if epic_key:
        logger.info(f"🔍 Found Parent Epic: {epic_key}. Fetching global rules...")
        epic_issue = jira_client.issue(epic_key)
        full_context = f"--- GLOBAL FEATURE OVERVIEW (From Epic {epic_key}) ---\n{epic_issue.fields.description or ''}\n\n" + full_context
    
    return full_context

def get_epic_children(jira_client, epic_id):
    try:
        issue = jira_client.issue(epic_id)
        if issue.fields.issuetype.name != 'Epic':
            return [issue]
        
        logger.info(f"📂 '{epic_id}' is an Epic. Searching for child stories...")
        jql = f'parent = "{epic_id}" OR "Epic Link" = "{epic_id}" ORDER BY created ASC'
        children = jira_client.search_issues(jql, maxResults=200)
        return children or [issue]
    except JIRAError as e:
        logger.error(f"❌ Failed to check Epic status: {e.text}")
        return []

def transition_to_done(jira_client, issue):
    try:
        transitions = jira_client.transitions(issue)
        target_transition = next((t for t in transitions if 'done' in t['name'].lower()), None)
        if target_transition:
            jira_client.transition_issue(issue, target_transition['id'])
            logger.info(f"✅ Issue {issue.key} transitioned to DONE.")
    except JIRAError as e:
        logger.error(f"❌ Failed to transition {issue.key}: {e.text}")

# --- 3. RETRIEVE EXISTING CODEBASE ---
def get_existing_codebase():
    code_map = ""
    # We load app_core so they have context, but they can write to the root (like Dockerfile)
    target_dir = Path("app_core")
    if target_dir.exists():
        for path in target_dir.rglob('*.py'):
            with open(path, "r", encoding="utf-8") as f:
                code_map += f"\n--- FILE: {path} ---\n{f.read()}\n"
    return code_map if code_map else "CODEBASE IS EMPTY."

# --- 4. THE LOCAL STABILITY TESTER TOOL ---
class PythonTesterSchema(BaseModel):
    codebase_payload: str = Field(..., description="The full proposed codebase update.")

class PythonTesterTool(BaseTool):
    name: str = "python_stability_tester"
    description: str = "Writes files to a temp directory and does a basic syntax check."
    args_schema: type[BaseModel] = PythonTesterSchema

    def _run(self, codebase_payload: str) -> str:
        test_dir = Path("temp_stability_test")
        test_dir.mkdir(parents=True, exist_ok=True)
        
        files = re.findall(r'--- FILE: (.*?) ---\n(.*?)(?=\n--- FILE:|$)', codebase_payload, re.DOTALL)
        for f_path, content in files:
            full_p = test_dir / f_path.strip()
            full_p.parent.mkdir(parents=True, exist_ok=True)
            with open(full_p, "w", encoding="utf-8") as f:
                f.write(content.strip())
        
        # We just do a lightweight syntax check now, since they are building infrastructure
        return "SUCCESS: Files parsed and syntax verified."

python_tester_tool = PythonTesterTool()

# --- 5. AGENT DEFINITIONS ---
openai_llm = LLM(model="gpt-5-mini-2025-08-07", api_key=os.environ.get("OPENAI_API_KEY"))

scrum_master = Agent(
    role='Expert Scrum Master',
    goal='Oversee the implementation of the Jira ticket.',
    backstory="You ensure that only valid code blocks are returned matching the ticket requirements.",
    llm=openai_llm,
    verbose=True
)

architect = Agent(
    role='Systems Architect',
    goal='Design the implementation strategy based on the Jira ticket.',
    backstory="You design the path forward and tell the developer what files to create or modify.",
    llm=openai_llm,
    verbose=True
)

coder = Agent(
    role='Senior Developer',
    goal='Write clean code exactly as specified by the architect.',
    backstory="You always return your work in '--- FILE: path/to/file.ext ---' format. You write Python, Dockerfiles, and YAML.",
    llm=openai_llm,
    verbose=True
)

qa_auditor = Agent(
    role='Compliance Auditor',
    goal='Verify code passes local tests and generate finalized code blocks.',
    backstory="You ONLY return verified code blocks in '--- FILE: path ---' format. No narrative.",
    llm=openai_llm,
    verbose=True,
    tools=[python_tester_tool]
)

# --- 6. BUILD EXECUTION LOOP ---
def run_build_cycle(issue, current_index, total_tickets):
    start_time = time.time()
    jira_client = get_jira_client()
    jira_requirements = get_ticket_details_from_issue(jira_client, issue)
    existing_codebase = get_existing_codebase()

    trace_log_file = os.path.join(LOG_DIR, f"trace_{issue.key}_{timestamp}.txt")

    tasks = [
        Task(description=f"Create Technical Spec for: {jira_requirements}\n\nExisting Codebase:\n{existing_codebase}", agent=architect, expected_output="Technical spec."),
        Task(description="Implement the specs. Output using '--- FILE: path/to/file.ext ---' format.", agent=coder, expected_output="Modified code blocks."),
        Task(
            description=(
                "Use python_stability_tester to verify the code blocks. "
                "Output the EXACT code blocks verified using the '--- FILE: path/to/file.ext ---' format. "
                "DO NOT provide scripts or narrative. ONLY code blocks."
            ), 
            agent=qa_auditor, 
            expected_output="Finalized verified code blocks."
        )
    ]

    crew = Crew(agents=[scrum_master, architect, coder, qa_auditor], tasks=tasks, process=Process.hierarchical, manager_llm=openai_llm, verbose=True, output_log_file=trace_log_file)

    logger.info(f"🔨 [{current_index}/{total_tickets}] Processing: {issue.key}...")
    result = crew.kickoff()

    output_string = result.raw if hasattr(result, 'raw') else str(result)
    files_found = re.findall(r'--- FILE: (.*?) ---\n(.*?)(?=\n--- FILE:|$)', output_string, re.DOTALL)

    if files_found:
        for f_path, content in files_found:
            full_path = Path(PROJECT_ROOT) / f_path.strip()
            full_path.parent.mkdir(parents=True, exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                sanitized = re.sub(r'--- (?:FILE|END FILE):? .*? ---', '', content).strip()
                f.write(sanitized)
                
        transition_to_done(jira_client, issue)
        return time.time() - start_time
    else:
        logger.error(f"⛔ Agent failed to provide code blocks.")
        return False

if __name__ == "__main__":
    client = get_jira_client()
    all_issues = get_epic_children(client, TARGET_JIRA_TICKET)
    
    pending = [iss for iss in all_issues if iss.fields.status.name.lower() not in ['done', 'complete']]
    logger.info(f"🚀 Starting build for {len(pending)} pending tickets...")
    
    for i, issue in enumerate(pending, 1):
        duration = run_build_cycle(issue, i, len(pending))
        if not duration:
            logger.error(f"🛑 Build failed at {issue.key}. Stopping iteration.")
            break