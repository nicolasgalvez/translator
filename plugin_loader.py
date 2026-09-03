from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_plugins(plugin_dir: Path = Path("plugins")) -> list[str]:
    """Import local Python plugins from a directory.

    A plugin can be either plugins/name.py or plugins/name/__init__.py. Importing
    the module is expected to register hooks via hooks.add_filter/add_action.
    """
    if not plugin_dir.exists():
        return []

    loaded: list[str] = []
    for path in sorted(plugin_dir.iterdir()):
        if path.name.startswith("_"):
            continue
        if path.is_file() and path.suffix == ".py":
            module_path = path
            module_name = path.stem
        elif path.is_dir() and (path / "__init__.py").exists():
            module_path = path / "__init__.py"
            module_name = path.name
        else:
            continue

        qualified_name = f"plugins.{module_name}"
        spec = importlib.util.spec_from_file_location(qualified_name, module_path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified_name] = module
        spec.loader.exec_module(module)
        loaded.append(qualified_name)

    return loaded
