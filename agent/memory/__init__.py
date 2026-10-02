"""Memory: what earlier steps and runs settled, reused to skip the model.

- The playbook (Postgres, else a JSON file): which move followed which
  situation on a site, trusted once it led to finished steps.
- Recall (a JSON file): plans by goal, products by site and search term.
"""

from .playbook import (
    FilePlaybook,
    InMemoryPlaybook,
    PlaybookEntry,
    PlaybookStore,
    PostgresPlaybook,
    record_outcome,
    suggested_action,
)
from .recall import Recall, memory_dir
from .session import SessionMemory
from .shape import action_shape, path_pattern, situation

__all__ = [
    "FilePlaybook",
    "InMemoryPlaybook",
    "PlaybookEntry",
    "PlaybookStore",
    "PostgresPlaybook",
    "Recall",
    "SessionMemory",
    "action_shape",
    "memory_dir",
    "path_pattern",
    "record_outcome",
    "situation",
    "suggested_action",
]
