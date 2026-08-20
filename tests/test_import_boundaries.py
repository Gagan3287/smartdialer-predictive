import ast
import os
import glob
import pytest

def get_imports_from_file(filepath: str):
    with open(filepath, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=filepath)
    
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imports.append(module)
    return imports

def test_pacing_engine_import_boundaries():
    pacing_files = glob.glob("app/pacing/*.py")
    assert len(pacing_files) > 0, "No files found in app/pacing/"

    forbidden_patterns = [
        "app.allocator",
        "allocator",
        "app.providers",
        "providers",
        "telecom",
        "provider_a",
        "provider_b"
    ]

    for filepath in pacing_files:
        imported_modules = get_imports_from_file(filepath)
        for imp in imported_modules:
            for forbidden in forbidden_patterns:
                assert not imp.startswith(forbidden), (
                    f"Architectural Boundary Violation! Pacing module '{filepath}' "
                    f"imports forbidden module '{imp}'."
                )
