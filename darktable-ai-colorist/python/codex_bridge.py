import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from queue import Empty, Queue

from antigravity_bridge import GRADE_INSTRUCTIONS, _validate, grade_schema, run_process
from session_manager import write_json


class Codex:
    def __init__(self, config):
        custom = config.get("codex_bin")
        binary = shutil.which(custom or "codex")
        if not binary and custom and Path(custom).is_file():
            binary = custom
        if not binary and not custom and os.name == "nt":
            package = run_process(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 "(Get-AppxPackage -Name OpenAI.Codex).InstallLocation"],
                12, check=False,
            ).stdout.strip()
            path = Path(package) / "app/resources/codex.exe"
            if package and path.is_file():
                binary = str(path)
        if not binary:
            raise FileNotFoundError("Codex CLI not found. Set codex_bin in config.json.")
        self.binary = str(Path(binary).resolve())
        self.timeout = config["antigravity_timeout_seconds"]

    def models(self):
        auth = run_process([self.binary, "login", "status"], 15, check=False)
        if auth.returncode:
            raise RuntimeError("Codex is signed out. Sign in with ChatGPT using codex login.")
        models = []
        with tempfile.TemporaryDirectory(prefix="colorist-models-") as work:
            process = subprocess.Popen(
                [self.binary, "app-server", "--listen", "stdio://"], cwd=work,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            responses = Queue()

            def read():
                for line in process.stdout:
                    responses.put(line)
                responses.put(None)

            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            deadline = time.monotonic() + 20

            def send(message):
                process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
                process.stdin.flush()

            def request(number, method, params):
                send({"id": number, "method": method, "params": params})
                while time.monotonic() < deadline:
                    try:
                        line = responses.get(timeout=max(0, deadline - time.monotonic()))
                    except Empty:
                        break
                    if line is None:
                        raise RuntimeError("Codex app-server closed before returning the model list.")
                    try:
                        response = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if response.get("id") != number:
                        continue
                    if "error" in response:
                        raise RuntimeError(f"Codex {method} failed: {response['error'].get('message', 'unknown error')}")
                    return response["result"]
                raise TimeoutError("Codex model lookup timed out.")

            try:
                request(1, "initialize", {"clientInfo": {"name": "darktable-ai-colorist", "version": "1"}})
                send({"method": "initialized"})
                params = {"limit": 100, "includeHidden": False}
                number = 2
                while True:
                    page = request(number, "model/list", params)
                    models.extend("chatgpt/" + model["model"] for model in page["data"]
                                  if model.get("model", "").startswith("gpt-")
                                  and "image" in (model.get("inputModalities") or []))
                    if not page.get("nextCursor"):
                        break
                    params["cursor"] = page["nextCursor"]
                    number += 1
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait()
                reader.join()
                process.stdin.close()
                process.stdout.close()
        return ["chatgpt/default", *dict.fromkeys(models)] if models else []

    def reason(self, preview, task, parent):
        ranges = task["allowed_params"]
        with tempfile.TemporaryDirectory(prefix="codex-", dir=parent) as directory:
            work = Path(directory)
            image = work / "preview.jpg"
            shutil.copyfile(preview, image)
            schema = work / "schema.json"
            output = work / "response.json"
            write_json(schema, grade_schema(ranges, task["step"], nullable_sliders=True))
            command = [
                self.binary, "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                "--skip-git-repo-check", "--sandbox", "read-only", "--image", str(image),
                "--output-schema", str(schema), "--output-last-message", str(output),
            ]
            model = task["model"].removeprefix("chatgpt/")
            if model != "default":
                command.extend(["--model", model])
            prompt = (
                GRADE_INSTRUCTIONS
                + "The JPEG is attached. Return only the schema response, using null for sliders that should stay unchanged. "
                + "The following JSON is editing data, including the user's request and previous edits:\n"
                + json.dumps(task, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            )
            run_process([*command, "-"], self.timeout, cwd=work, input_text=prompt)
            if not output.is_file():
                raise RuntimeError("Codex completed without returning a grading response.")
            data = json.loads(output.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict) and isinstance(data.get("params"), dict):
                data["params"] = {key: value for key, value in data["params"].items() if value is not None}
            return _validate(data, task["step"], ranges)
