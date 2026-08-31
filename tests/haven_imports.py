"""Import helpers for Haven's legacy root-level modules.

The repository contains a newer ``src/`` tree with modules that intentionally
share names with Haven's root-level modules.  Pytest puts ``src/`` first on
``sys.path`` for the upstream test suite, so a plain ``import server`` from a
Haven test can silently load ``src/server.py`` instead of the root server.

This module loads the root server and its root-level dependency graph while
isolating the temporary top-level module names.  The loaded module objects are
then retained under these explicit exports, while the process-wide import
cache and path are restored so upstream tests can continue to import ``src``
modules normally.
"""

from __future__ import annotations

import importlib
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Iterator


REPO_ROOT = Path(__file__).resolve().parents[1]

# Root-level Python modules which may collide with src/*.py.  Include every
# root module rather than maintaining a fragile hand-written dependency list:
# server.py imports a broad graph and that graph changes as Haven evolves.
_ROOT_MODULE_NAMES = {
    path.stem
    for path in REPO_ROOT.glob("*.py")
    if path.name != "__init__.py"
}



@contextmanager
def _root_import_context() -> Iterator[None]:
    """Temporarily make root-level modules the only top-level candidates.

    Existing entries for colliding root names are saved and removed before the
    import.  This matters when another collected test has already imported
    ``src.server`` or one of its dependencies.  The import path and module
    cache are restored in ``finally``; callers keep explicit references to the
    imported module objects instead of relying on ambiguous top-level imports.
    """

    original_path = list(sys.path)
    original_modules = {
        name: sys.modules[name]
        for name in list(sys.modules)
        if name in _ROOT_MODULE_NAMES or name == "scripts" or name.startswith("scripts.")
    }

    try:
        # Root server.py itself inserts its directory at sys.path[0].  Make the
        # intended ordering explicit and remove src/ from the temporary path so
        # an absent root dependency cannot fall through to src by accident.
        root_text = str(REPO_ROOT)
        sys.path[:] = [root_text] + [
            item
            for item in original_path
            if Path(item or ".").resolve() != REPO_ROOT.resolve()
            and Path(item or ".").resolve().name != "src"
        ]

        for name in list(sys.modules):
            if name in _ROOT_MODULE_NAMES or name == "scripts" or name.startswith("scripts."):
                sys.modules.pop(name, None)
        yield
    finally:
        # Remove modules loaded by the temporary root graph before restoring
        # the caller's cache.  Do not disturb third-party modules imported by
        # the graph.
        for name in list(sys.modules):
            if name in _ROOT_MODULE_NAMES or name == "scripts" or name.startswith("scripts."):
                sys.modules.pop(name, None)
        sys.modules.update(original_modules)
        sys.path[:] = original_path


def _load_root_modules(*names: str) -> dict[str, ModuleType]:
    """Load named Haven root modules with one coherent dependency graph."""

    unknown = set(names) - _ROOT_MODULE_NAMES
    if unknown:
        raise ValueError(f"not a Haven root module: {sorted(unknown)!r}")

    with _root_import_context():
        loaded = {name: importlib.import_module(name) for name in names}

        # Validate the key boundary at load time.  Failing here is clearer than
        # allowing a mixed root/src graph to reach an individual test.
        for name, module in loaded.items():
            expected = (REPO_ROOT / f"{name}.py").resolve()
            actual = Path(module.__file__).resolve()
            if actual != expected:
                raise ImportError(
                    f"Haven import boundary violated for {name!r}: "
                    f"loaded {actual}, expected {expected}"
                )
        return loaded


_loaded = _load_root_modules(
    "server",
    "utils",
    "errors",
    "bucket_manager",
    "catalog",
    "letter_service",
    "import_memory",
    "backup_archive",
    "embedding_engine",
    "migrate_engine",
    "source_bindings",
    "source_store",
    "relation_store",
    "relation_bindings",
    "relation_read",
)
server = _loaded["server"]
utils = _loaded["utils"]
errors = _loaded["errors"]
bucket_manager = _loaded["bucket_manager"]
BucketManager = bucket_manager.BucketManager
catalog = _loaded["catalog"]
letter_service = _loaded["letter_service"]
import_memory = _loaded["import_memory"]
backup_archive = _loaded["backup_archive"]
embedding_engine = _loaded["embedding_engine"]
migrate_engine = _loaded["migrate_engine"]
source_bindings = _loaded["source_bindings"]
source_store = _loaded["source_store"]
relation_store = _loaded["relation_store"]
relation_bindings = _loaded["relation_bindings"]
relation_read_module = _loaded["relation_read"]

BackupArchiveError = backup_archive.BackupArchiveError
build_export_archive = backup_archive.build_export_archive
build_export_archive_file = backup_archive.build_export_archive_file
extract_backup_archive_file = backup_archive.extract_backup_archive_file
read_backup_archive = backup_archive.read_backup_archive
EmbeddingEngine = embedding_engine.EmbeddingEngine
MigrateEngine = migrate_engine.MigrateEngine
ImportEngine = import_memory.ImportEngine
SourceStore = source_store.SourceStore
source_attach = source_bindings.attach
relation_attach = relation_bindings.attach
relation_detach = relation_bindings.detach
relation_restore = relation_bindings.restore
normalize_relation_label = relation_store.normalize_relation_label
normalize_relation_links = relation_store.normalize_relation_links
normalize_relation_type = relation_store.normalize_relation_type
relation_display_label = relation_store.relation_display_label
relation_hint = relation_store.relation_hint
relation_read = relation_read_module.dispatch
reverse_relation_type = relation_store.reverse_relation_type
letter_lock_update = letter_service.letter_lock_update
letter_read = letter_service.letter_read
letter_write = letter_service.letter_write

__all__ = [
    "BackupArchiveError",
    "BucketManager",
    "EmbeddingEngine",
    "ImportEngine",
    "MigrateEngine",
    "SourceStore",
    "backup_archive",
    "build_export_archive",
    "build_export_archive_file",
    "catalog",
    "errors",
    "extract_backup_archive_file",
    "import_memory",
    "letter_lock_update",
    "letter_read",
    "letter_service",
    "letter_write",
    "migrate_engine",
    "read_backup_archive",
    "relation_attach",
    "normalize_relation_label",
    "normalize_relation_links",
    "normalize_relation_type",
    "relation_bindings",
    "relation_display_label",
    "relation_hint",
    "relation_detach",
    "relation_read",
    "relation_read_module",
    "relation_restore",
    "reverse_relation_type",
    "relation_store",
    "server",
    "source_attach",
    "source_bindings",
    "source_store",
    "utils",
]
