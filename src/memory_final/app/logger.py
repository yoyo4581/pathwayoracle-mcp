import logging
import sys


# Setup logging to stderr (CRITICAL for MCP)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr  # ← This is the key addition!
)
logger = logging.getLogger(__name__)