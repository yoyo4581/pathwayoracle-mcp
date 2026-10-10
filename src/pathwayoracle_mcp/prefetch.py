"""
Download the encoder and the BM25 model into the cache, verify, and exit.

Run by the `model-fetch` compose service before `mcp` starts. Without it the
server downloads ~420 MB on its first tool call: `up` reports a healthy stack,
the first query hangs for a minute or two (long enough for a client to time
out), and a bad pin surfaces as a tool error mid-conversation instead of a
failed startup. This moves all of that to `up`, where it can fail loudly.

Only the DOWNLOAD moves. Loading the model into memory stays lazy, on the first
tool call, so the server itself still starts without paying for torch.

Idempotent: on a warm cache the downloads are no-ops and only the hash runs.

    BIOBERT_MODEL_PATH   Hub repo id (or a local directory, which is skipped)
    BIOBERT_REVISION     Hub commit to fetch
    BIOBERT_SHA256       expected sha256 of model.safetensors; the backend
                         publishes it as weights_sha256 in provenance.json
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

from .core.config import BM25_MODEL, DEFAULT_MODEL


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch_encoder(model: str, revision: str | None, expected: str | None) -> None:
    if Path(model).is_dir():
        print(f"encoder: local directory {model}, nothing to fetch")
        return

    from huggingface_hub import snapshot_download

    path = Path(snapshot_download(model, revision=revision))
    print(f"encoder: {model}@{revision or 'main'} -> {path}")

    if not expected:
        print("encoder: BIOBERT_SHA256 not set, weights NOT verified")
        return
    got = sha256(path / "model.safetensors")
    if got != expected:
        # The failure this exists for: the collections were embedded by the
        # weights hashing to `expected`, and any other weights put queries in a
        # different vector space -- retrieval degrades and nothing errors.
        sys.exit(f"encoder: model.safetensors sha256 is {got}, expected {expected}. "
                 f"BIOBERT_REVISION does not point at the weights this release was "
                 f"built with; fix the pin rather than overriding the hash.")
    print(f"encoder: weights sha256 OK ({got[:12]})")


def fetch_sparse() -> None:
    from fastembed import SparseTextEmbedding

    SparseTextEmbedding(model_name=BM25_MODEL)
    print(f"bm25: {BM25_MODEL} ready")


def main() -> None:
    fetch_encoder(os.environ.get("BIOBERT_MODEL_PATH", DEFAULT_MODEL),
                  os.environ.get("BIOBERT_REVISION") or None,
                  os.environ.get("BIOBERT_SHA256") or None)
    fetch_sparse()


if __name__ == "__main__":
    main()
