from crewai import Agent, Task, Crew, Process, LLM
import os

# --- MODELS ---
master_llm = LLM(
    model='qwen-3-235b-a22b-instruct-2507', 
    api_key=os.environ.get("CEREBRAS_API_KEY"),
    base_url="https://api.cerebras.ai/v1"
)

# --- TOOLS ---
from crewai.tools import tool

@tool("create_task")
def create_task(task_name: str, description: str) -> str:
    """Creates a new task in the project."""
    return f"Task '{task_name}' created: {description}"

# --- AGENTS ---
project_manager = Agent(
    role='Project Manager',
    goal="Manage JIRA tickets and sprints",
    backstory="You are an expert Scrum Master",
    tools=[create_task],
    llm=master_llm, 
    verbose=False
)

# --- TASKS ---
initial_task = Task(
    description="Review current sprint backlog",
    expected_output="Sprint status report",
    agent=project_manager
)

# --- CREW ---
crew = Crew(
    agents=[project_manager],
    tasks=[initial_task],
    process=Process.sequential,
    verbose=True
)
