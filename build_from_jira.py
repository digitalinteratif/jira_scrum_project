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
# This will automatically load OPENAI_API_KEY from your .env file
load_dotenv()

# 🛑 CHANGE THIS TO THE TICKET ID YOU WANT TO BUILD 🛑
TARGET_JIRA_TICKET = os.environ.get("TARGET_JIRA_TICKET", "YOUR-TICKET-ID") # e.g., "APP-4" 

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
        print(f"📦 Detected Epic! Fetching all child stories linked to {ticket_id}...")
        jql_query = f'parent = "{ticket_id}" OR "Epic Link" = "{ticket_id}"'
        
        try:
            child_issues = jira_client.search_issues(jql_query)
            if child_issues:
                print(f"   ✅ Found {len(child_issues)} child stories.")
                requirements_text += "\n" + "="*40 + "\n"
                requirements_text += f"CHILD STORIES TO IMPLEMENT FOR THIS EPIC:\n"
                requirements_text += "="*40 + "\n\n"
                
                for i, child in enumerate(child_issues, 1):
                    requirements_text += f"--- STORY {i}: {child.fields.summary} ({child.key}) ---\n"
                    requirements_text += f"{child.fields.description}\n\n"
            else:
                print("   ⚠️ No child stories found for this Epic.")
        except JIRAError as e:
            print(f"   ⚠️ Could not fetch child stories: {e.text}")
            
    return requirements_text

jira_requirements = get_ticket_details(TARGET_JIRA_TICKET)

# --- 3. AUTONOMOUS TESTING TOOL ---
class PythonTesterSchema(BaseModel):
    code_string: str = Field(..., description="The complete, raw Python script to execute. Must be a plain string.")

class PythonTesterTool(BaseTool):
    name: str = "Python Code Execution and Syntax Tester"
    description: str = (
        "Saves the provided Python code to a temporary file and executes it. "
        "CRITICAL FORMATTING: Your Action Input MUST be a valid JSON object strictly matching this format: "
        "{\"code_string\": \"<your python code here>\"}. DO NOT wrap the input inside a 'properties' dictionary."
    )
    args_schema: type[BaseModel] = PythonTesterSchema

    def _run(self, code_string: str) -> str:
        # Clean up markdown formatting if the LLM accidentally passes it into the tool
        code = code_string.replace('```python', '').replace('```', '').strip()
        
        test_filename = "ai_test_run.py"
        with open(test_filename, "w", encoding="utf-8") as f:
            f.write(code)
            
        try:
            print("\n⏳ [AI Tool] Booting up Flask server for 4 seconds to test for crashes...")
            # Run the script. A successful Flask app will block forever, triggering the timeout.
            result = subprocess.run(
                ["python", test_filename], 
                capture_output=True, 
                text=True, 
                timeout=4
            )
            # If it exits before 4 seconds, it either finished instantly or crashed.
            if result.returncode != 0:
                print(f"💥 [AI Tool] Crash detected! Sending traceback back to AI to fix...")
                # TRUNCATE ERROR: Reduced to 800 chars to heavily protect the context limit
                error_out = result.stderr[-800:] if len(result.stderr) > 800 else result.stderr
                return f"CRASH DETECTED:\n...{error_out}\nAnalyze this traceback, fix the bugs in the code, and test it again."
            else:
                out = result.stdout[-300:] if len(result.stdout) > 300 else result.stdout
                return "Script exited cleanly (0) quickly. Output:\n" + out
                
        except subprocess.TimeoutExpired as e:
            print("✅ [AI Tool] Server booted successfully and stayed alive! Code is clean.")
            # The holy grail: the server booted and stayed alive for 4 seconds!
            return "SUCCESS: The script ran for 4 seconds without crashing. The Flask server successfully started, meaning no Syntax, Import, or Context errors occurred. You may now output the final code."
        except Exception as e:
            return f"Unexpected error running script: {e}"

python_tester_tool = PythonTesterTool()

# --- 4. THE MODELS & AGENTS ---
# Using OpenAI's GPT-4o for maximum reasoning quality and code accuracy
openai_llm = LLM(model='gpt-4o', temperature=0.1)

senior_developer = Agent(
    role='Senior Python Developer',
    goal='Write flawless, self-contained Python code based on Jira requirements.',
    backstory="You are a 10x Python developer. You read Jira tickets and write perfect, fully functional code. You use standard libraries or lightweight frameworks like Flask/SQLite to ensure the code can run immediately.",
    llm=openai_llm,
    verbose=True,
    allow_delegation=False
)

code_reviewer = Agent(
    role='Lead Code Reviewer and QA Tester',
    goal='Review the generated code, test it by executing it, fix any errors, and output the final code block.',
    backstory="You are a strict code reviewer and tester. You ensure the code perfectly matches the Jira requirements. YOU MUST TEST THE CODE using the 'Python Code Execution and Syntax Tester' tool. If the tool returns a traceback, you must figure out why it crashed, rewrite the code to fix the bug, and use the tool to test it again. Do NOT output the final code until the tool returns 'SUCCESS'.",
    llm=openai_llm,
    verbose=True,
    allow_delegation=False,
    max_iter=3,
    tools=[python_tester_tool]
)

# --- 5. THE TASKS ---
write_code_task = Task(
    description=(
        f"Read the following Jira ticket requirements carefully:\n"
        f"=========================================\n"
        f"{jira_requirements}\n"
        f"=========================================\n\n"
        "Write a fully functional, self-contained Python application that fulfills ALL of these requirements. "
        "If multiple stories are provided, combine them into a single cohesive application. "
        "If it is a web feature, use Python's 'Flask' framework.\n"
        "CRITICAL SINGLE-FILE RULE: Your entire application MUST be contained within this single Python script. Do NOT use `from forms import ...`. All HTML templates MUST be defined directly inline within this main script as strings. You MUST use Flask's `render_template_string` to render these inline string templates. NEVER use `render_template`.\n"
        "CRITICAL TEMPLATE RULE: When writing inline HTML strings for `render_template_string`, DO NOT use Python f-strings (e.g., `f'''...'''`). Jinja template syntax strictly conflicts with Python f-strings and causes a SyntaxError. Use standard multi-line strings (`'''...'''`) and pass variables as keyword arguments.\n"
        "CRITICAL DATABASE RULE: If using SQLAlchemy, you MUST use a safe local relative path (e.g., 'sqlite:///local_app.db').\n"
        "CRITICAL FLASK VERSION RULE: Do NOT use `@app.before_first_request`. It has been removed in modern Flask 2.3+. Perform all setup and database initialization directly inside a `with app.app_context():` block at the bottom of the script before calling `app.run()`.\n"
        "CRITICAL FLASK CONTEXT RULE: When initializing the database (e.g., calling `db.create_all()`), you MUST wrap it inside an application context using `with app.app_context():` to prevent 'Working outside of application context' RuntimeErrors.\n"
        "CRITICAL MOCKING RULE: Do NOT attempt to connect to a real SMTP server. Mock all emails using `print()`.\n"
        "CRITICAL PYTHON RULE: Do NOT write custom Python decorators (e.g., @requires_auth) for business logic. Handle session validation directly INSIDE the standard Flask route functions.\n"
        "CRITICAL FEATURE RULE: You MUST implement the ACTUAL business logic (e.g., URL Shortening). Do NOT just build a login screen and stop. The user MUST be able to log in, see a dashboard, submit a URL, and get a short link.\n"
        "CRITICAL ROUTING RULE: You MUST include a root route ('/') that returns a fully functional HTML UI so the user can immediately test the main feature!"
    ),
    expected_output="A complete, functional Python script containing the entire requested system (Auth + Core Feature) with a working HTML frontend, entirely self-contained.",
    agent=senior_developer
)

review_and_format_task = Task(
    description=(
        "Review the Python code written by the Senior Developer.\n"
        "1. You MUST use the 'Python Code Execution and Syntax Tester' tool to run the code. Pass the entire Python script string into the tool.\n"
        "2. CRITICAL FORMATTING: You must pass the parameter exactly as a JSON object: {\"code_string\": \"YOUR CODE HERE\"}. DO NOT wrap the input inside a 'properties' dictionary.\n"
        "3. If the tool returns a 'CRASH DETECTED' traceback, you must read the error, rewrite the code to fix the bug, and use the tool to test it again.\n"
        "4. Repeat this process until the tool returns 'SUCCESS'.\n"
        "5. Once the code passes the test, OUTPUT THE FINAL, PERFECT CODE INSIDE A ```python ... ``` BLOCK. Do not include any other text outside the block."
    ),
    expected_output="The final Python code, tested and wrapped securely in triple backticks.",
    agent=code_reviewer
)

# --- 6. EXECUTION ---
dev_crew = Crew(
    agents=[senior_developer, code_reviewer],
    tasks=[write_code_task, review_and_format_task],
    process=Process.sequential, 
    verbose=True
)

print(f"🚀 Starting AI Development Team (GPT-4o with Autonomous Testing & Safety Guardrails)...\n")
raw_result = dev_crew.kickoff()

print("\n✅ Coding complete. Extracting and saving to VS Code...\n")

# --- 7. EXTRACT AND SAVE THE CODE ---
output_string = raw_result.raw if hasattr(raw_result, 'raw') else str(raw_result)
code_match = re.search(r'```python\n(.*?)\n```', output_string, re.DOTALL)

if code_match:
    final_code = code_match.group(1)
    
    output_filename = "generated_app.py"
    with open(output_filename, "w", encoding="utf-8") as file:
        file.write(final_code)
    
    print(f"🎉 SUCCESS! The functional, self-tested code has been saved to '{output_filename}' in your VS Code workspace.")
    print("To run it, you may need to install flask and sqlalchemy: pip install flask flask-sqlalchemy flask-wtf")
    print(f"Then run: python {output_filename}")
else:
    print("⛔️ Could not isolate the Python code block from the LLM output. Here is the raw output:")
    print(output_string)