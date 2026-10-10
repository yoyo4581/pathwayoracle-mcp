# pathwayoracle-mcp: the MCP server, CPU-only.
#
# Weights are NOT baked in. They download from the Hugging Face Hub on first
# use into /cache, which compose mounts as a named volume -- so the image stays
# small, the first start pays the download once, and later starts reuse it.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# CPU torch FIRST, from the CPU index. A plain install resolves torch from PyPI,
# whose Linux wheel is the CUDA build: ~4.4 GB of nvidia/* for a container with
# no GPU. With this already present, the install below sees torch==2.14.0
# satisfied (+cpu is a local version label, which == ignores) and leaves it.
RUN pip install torch==2.14.0 --index-url https://download.pytorch.org/whl/cpu

WORKDIR /app

# Dependencies in their own layer, keyed on pyproject.toml alone, so editing
# source does not reinstall sentence-transformers and friends. The stub package
# exists only so the build backend has something to build.
COPY pyproject.toml README.md ./
RUN mkdir -p src/pathwayoracle_mcp && touch src/pathwayoracle_mcp/__init__.py \
 && pip install . && pip uninstall -y pathwayoracle-mcp

COPY src ./src
RUN pip install --no-deps .

# Non-root, and the owner of /cache. The ownership is what makes the named
# volume writable: Docker seeds a fresh named volume's ownership from the
# image's directory at the mount point. Without this directory in the image
# the volume would be created root-owned and this user could not write to it.
RUN useradd --uid 1000 --create-home app \
 && mkdir -p /cache/huggingface /cache/fastembed /cache/traces \
 && chown -R app:app /cache
USER app

ENV HF_HOME=/cache/huggingface \
    FASTEMBED_CACHE_PATH=/cache/fastembed \
    BIOBERT_MODEL_PATH=yoyo458/BioBERT_GOUniProt_FineTuned \
    MCP_TRANSPORT=stdio

ENTRYPOINT ["pathwayoracle-mcp"]
