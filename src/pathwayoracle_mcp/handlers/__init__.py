"""
Python implementations of registry tools, keyed by the registry's `handler`.

Each takes the Toolkit first and the validated arguments as keywords, and
returns one JSON object. They are plain functions, importable without the MCP
layer -- which is what lets DeepAgents call the same implementations.
"""
from . import evidence, graph, literature, memory, meta, retrieval

HANDLERS = {
    f"{module.__name__.rsplit('.', 1)[-1]}.{name}": fn
    for module in (retrieval, graph, literature, meta, evidence, memory)
    for name, fn in vars(module).items()
    if callable(fn) and not name.startswith("_") and getattr(fn, "__module__", "") == module.__name__
}
