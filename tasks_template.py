# Project tasks.py — copy this file to your project root as tasks.py.
#
# All invoke tasks from pcbtools/tasks.py are imported automatically.
# You do not need to edit this file when pcbtools is updated.
#
# To add project-specific tasks, define them below the import block
# as normal @task functions.

import os
import importlib.util

# Load pcbtools/tasks.py under a private name to avoid the circular-import
# that would occur if we used importlib.import_module('tasks') while this
# file is itself named tasks.py.
_here = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    '_pcbtools_tasks',
    os.path.join(_here, 'pcbtools', 'tasks.py'),
)
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)

# Re-export every public name (invoke Task objects, helpers, constants).
# Any task added to pcbtools/tasks.py is automatically available here.
globals().update({k: v for k, v in vars(_m).items() if not k.startswith('_')})

# ---------------------------------------------------------------------------
# Project-specific tasks (optional) — add them here.
# ---------------------------------------------------------------------------
