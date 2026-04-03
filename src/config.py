# JIRA/Scrum Project Configuration
import os

# API Keys and Configuration
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY")
MASTER_MODEL = 'qwen-3-235b-a22b-instruct-2507'
PLAYER_MODEL = 'llama3.1-8b'
API_BASE_URL = "https://api.cerebras.ai/v1"

# Project state template
PROJECT_STATE = {
    "active_sprints": {},
    "backlog": [],
    "in_progress": [],
    "completed": []
}
