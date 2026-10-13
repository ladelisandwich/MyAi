"""Built-in tools. There is deliberately no shell/command tool: arbitrary command execution
is not available by default (see SECURITY.md)."""

from homemesh.tools.builtin.files import LIST_DIRECTORY, READ_FILE, WRITE_FILE
from homemesh.tools.builtin.general import CALCULATOR, CURRENT_TIME
from homemesh.tools.builtin.memory_knowledge import SEARCH_KNOWLEDGE, SEARCH_MEMORY
from homemesh.tools.builtin.system import SYSTEM_INFO
from homemesh.tools.builtin.web import FETCH_URL, OPEN_URL, WEB_SEARCH

BUILTIN_TOOLS = [CALCULATOR, CURRENT_TIME, SEARCH_MEMORY, SEARCH_KNOWLEDGE, SYSTEM_INFO, LIST_DIRECTORY, READ_FILE,
                 WRITE_FILE, WEB_SEARCH, FETCH_URL, OPEN_URL]

__all__ = ["BUILTIN_TOOLS"]
