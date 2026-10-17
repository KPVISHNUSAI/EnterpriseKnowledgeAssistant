"""Write the API's OpenAPI document to a file (the frontend generates its types from it).

uv run python scripts/export_openapi.py ../frontend/src/lib/api/openapi.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app

if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "openapi.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(create_app().openapi(), indent=2) + "\n")
    print(f"wrote {target}")
