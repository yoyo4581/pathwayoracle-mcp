"""
Vendored from POKnowledgeBase_ETL, src/builders/Qdrant/core/, at commit
a1d45dd (origin/main, 2026-10-07).

Vendored rather than installed because the backend is not a package: it has no
pyproject.toml, and its import name is the bare top-level `core`. The intended
end state is the backend publishing these two modules as an installable package
and this directory being deleted in favour of a git-tag dependency.

Until then this is a COPY, and copies drift silently. The retriever is what the
backend's promotion gate measures, so a change made in only one repo means the
thing benchmarked is no longer the thing served. Change these files upstream
first, then re-copy, then update the hashes below.

Local modifications (the only ones permitted):
  retriever.py   `from core.config import` -> `from .config import`

Upstream sha256 (before the modification above):
  config.py      43d1b5fe5bcbaf5be87ddb5ed9bb49d385df03658c59a2858e40ff1043b90e8e
  retriever.py   e07b839fb150d57baebf5ad057dc4c7a992a00455beea45d75923658e739da4a
"""
