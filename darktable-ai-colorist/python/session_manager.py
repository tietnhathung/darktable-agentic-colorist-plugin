import json
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path


def safe_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise ValueError("Session ID must contain 1–100 letters, digits, underscores or hyphens.")
    return value


def write_json(path: Path, data: dict) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class Session:
    def __init__(self, root: Path, session_id: str):
        self.root = root
        stem = f"session_{safe_id(session_id)}"
        self.state = root / f"{stem}.json"
        self.lock = root / f"{stem}.lock"

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            handle = self.lock.open("x")
        except FileExistsError as exc:
            raise RuntimeError(f"Session is busy. If the previous process was killed, remove {self.lock}.") from exc
        try:
            with handle:
                yield
        finally:
            self.lock.unlink()

    def load(self) -> dict:
        if not self.state.is_file():
            raise ValueError("No session to refine. Run the agent first.")
        return json.loads(self.state.read_text(encoding="utf-8"))

    def save(self, state: dict) -> None:
        write_json(self.state, state)

    def approve(self) -> None:
        self.state.unlink(missing_ok=True)
