import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# "{{.project_name}}" is a literal directory name: `databricks bundle init`
# renders it to the chosen project name; tests import the template source.
PAYLOAD = ROOT / "template" / "{{.project_name}}"
sys.path.insert(0, str(PAYLOAD / "src" / "job"))
sys.path.insert(0, str(PAYLOAD / "src" / "app"))
sys.path.insert(0, str(ROOT / "actions" / "gate-check"))
