"""
Compatibilité ascendante — les outils passent maintenant par le système de plugins.
Ce module reste pour les appels directs internes (video, project API).
"""
from plugins import get_loader


def run_tool(tool_name: str, params: dict) -> str:
    return get_loader().run(tool_name, params)


def exec_python(code: str) -> str:
    return get_loader().run("exec_python", {"code": code})


def write_file(path: str, content: str) -> str:
    return get_loader().run("write_file", {"path": path, "content": content})


def read_file(path: str) -> str:
    return get_loader().run("read_file", {"path": path})


def list_files(path: str = ".") -> str:
    return get_loader().run("list_files", {"path": path})


def create_project(name: str, type: str = "python", description: str = "") -> str:
    return get_loader().run("create_project", {"name": name, "type": type, "description": description})


AVAILABLE_TOOLS = {}  # populated at runtime via get_loader().list_all()
