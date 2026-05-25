import json
from pathlib import Path


def load_name_map(taxonomy_path: Path) -> dict[str, tuple[str, str]]:
    raw = json.loads(taxonomy_path.read_text())
    return {code: (entry[0] or code, entry[1] or "") for code, entry in raw.items()}


def name_for(code: str, name_map: dict[str, tuple[str, str]]) -> tuple[str, str]:
    if code == "bird1":
        return ("(any bird)", "")
    return name_map.get(code, (code, ""))
