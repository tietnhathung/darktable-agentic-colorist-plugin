import json
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from session_manager import write_json


GRADE_INSTRUCTIONS = (
    "You are a Darktable colorist. Visually inspect the preview and use the measured histogram. "
    "The preview reflects the current edit. Use current_params and darktable_context to understand "
    "which modules and settings already contribute to the image. Context values are read-only. "
    "Respect initial_prompt and refine relative to the current image using the newest refinement_feedback. "
    "Propose absolute Darktable slider settings only within allowed_params. Tint is Darktable's absolute "
    "multiplier (neutral 1.0). Vibrance and luminances are percentages. When current white balance is unknown, "
    "give both temperature_k and tint together or omit both. The available controls affect the full image "
    "or a tonal range. Explain any limits when the user asks for selective retouching. "
    "Judge highlight and shadow clipping in context. "
    "Write the critique in the language of the user. Set is_satisfied true only when the preview needs "
    "no further changes and return no changes then. Do not modify the image or use shell commands, "
    "browser tools, MCP, image generation, or other services. "
)


def grade_schema(ranges, step, *, nullable_sliders=False):
    params = {
        "type": "object", "additionalProperties": False,
        "properties": {key: {"type": ["number", "null"] if nullable_sliders else "number",
                             "minimum": low, "maximum": high}
                       for key, (low, high) in ranges.items()},
    }
    if nullable_sliders:
        params["required"] = list(ranges)
    return {
        "type": "object", "additionalProperties": False,
        "required": ["step", "critique", "is_satisfied", "params"],
        "properties": {
            "step": {"type": "integer", "enum": [step]},
            "critique": {"type": "string"},
            "is_satisfied": {"type": "boolean"},
            "params": params,
        },
    }


def run_process(command, timeout, cwd=None, check=True, input_text=None):
    try:
        result = subprocess.run(
            command, cwd=cwd, timeout=timeout, input=input_text,
            stdin=subprocess.DEVNULL if input_text is None else None,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{Path(command[0]).name} timed out after {timeout}s.") from exc
    if check and result.returncode:
        detail = (result.stderr.strip() or result.stdout.strip())[-3000:]
        raise RuntimeError(f"{Path(command[0]).name} exited with code {result.returncode}. {detail}")
    return result


class Antigravity:
    def __init__(self, config):
        custom = config.get("antigravity_bin")
        binary = (shutil.which(custom) or (custom if Path(custom).is_file() else None)) if custom else (
            shutil.which("antigravity") or shutil.which("agy")
        )
        if not binary:
            raise FileNotFoundError("Antigravity CLI not found. Install agy or set antigravity_bin in config.json.")
        self.binary = str(Path(binary).resolve())
        self.timeout = config["antigravity_timeout_seconds"]
        self.profiles = dict(config.get("models", {}))
        result = run_process([self.binary, "--help"], 20)
        self.help = result.stdout + "\n" + result.stderr
        self.prompt_flag = next((flag for flag in ("--print", "--prompt", "-p")
                                 if re.search(rf"(?<![\w-]){flag}(?![\w-])", self.help)), None)
        self.subcommand = []
        if self.prompt_flag is None and re.search(r"(?m)^\s+run\s+", self.help):
            self.subcommand = ["run"]
            result = run_process([self.binary, "run", "--help"], 20)
            self.help = result.stdout + "\n" + result.stderr
            self.prompt_flag = next((flag for flag in ("--prompt", "-p") if flag in self.help), None)
        if self.prompt_flag is None and not self.subcommand:
            raise RuntimeError("Antigravity CLI help does not advertise a supported non-interactive mode.")

    def models(self):
        models = ["antigravity/default"]
        if re.search(r"(?m)^\s+models\s+", self.help):
            result = run_process([self.binary, "models"], 30, check=False)
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or "Antigravity could not list the account's models.")
            ids = [line.split("\t", 1)[0] for line in result.stdout.splitlines()
                   if re.fullmatch(r"[a-zA-Z0-9_.-]+\t.+", line)]
            for alias, pattern in (("gemini-pro", r"^gemini.*-pro(?:-|$)"),
                                   ("gemini-flash", r"^gemini.*-flash(?:-|$)")):
                matching = [model for model in ids if re.search(pattern, model)]
                if matching:
                    selected = next((model for model in matching if model.endswith("-medium")), matching[0])
                    self.profiles.setdefault(alias, selected)
            models.extend("antigravity/" + model for model in ids)
        if "--model" in self.help:
            models.extend("antigravity/" + alias for alias in self.profiles)
        return list(dict.fromkeys(models))

    def reason(self, preview, task, parent):
        ranges = task["allowed_params"]
        structured = "--json-schema" in self.help and "--output-format" in self.help
        schema = grade_schema(ranges, task["step"])
        prompt = (
            "Call the built-in view_file tool directly on ./preview.jpg "
            "to inspect the JPEG and on ./task_input.json to read the histogram and editing instructions. "
            "These two local files are the only inputs you need. " + GRADE_INSTRUCTIONS
        )
        if structured:
            prompt += "Return only the JSON object matching the response schema supplied to the CLI."
        else:
            prompt += (
                "Write only output_params.json in this folder, or return only its JSON if file writes are unavailable. "
                "Use this output schema: " + json.dumps(schema, separators=(",", ":"))
            )
        with tempfile.TemporaryDirectory(prefix="reason-", dir=parent) as directory:
            work = Path(directory)
            shutil.copyfile(preview, work / "preview.jpg")
            write_json(work / "task_input.json", task)
            command = [self.binary, *self.subcommand]
            if self.prompt_flag:
                command.append(self.prompt_flag)
            command.append(prompt)
            model = task["model"].removeprefix("antigravity/")
            if model != "default":
                if "--model" not in self.help:
                    raise ValueError("This Antigravity CLI does not support model selection.")
                if model in {"gemini-pro", "gemini-flash"} and model not in self.profiles:
                    self.models()
                command += ["--model", self.profiles.get(model, model)]
            if "--settings" in self.help:
                settings = work / "agy-settings.json"
                write_json(settings, {"permissions": {"deny": ["mcp(*)"]}})
                command += ["--settings", str(settings)]
            if "--disable-slash-commands" in self.help:
                command.append("--disable-slash-commands")
            if structured:
                schema_path = work / "output_schema.json"
                write_json(schema_path, schema)
                command += ["--output-format", "json", "--json-schema", str(schema_path)]
            elif "--mode" in self.help and "accept-edits" in self.help:
                command += ["--mode", "accept-edits"]
            result = run_process(command, self.timeout, cwd=work, check=False)
            output = work / "output_params.json"
            data = None
            if result.returncode == 0 and not structured and output.is_file():
                try:
                    data = json.loads(output.read_text(encoding="utf-8-sig"))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise ValueError("Antigravity output_params.json is invalid JSON.") from exc
            elif result.returncode == 0:
                data = _stdout_output(result.stdout)
            if data is None:
                error = _output_error(result.stdout)
                if re.search(r'\bmcp\b.*\b(?:permission|denied)\b|\b(?:permission|denied)\b.*\bmcp\b',
                             error + "\n" + result.stderr, re.IGNORECASE):
                    raise RuntimeError("Antigravity tried to use MCP instead of reading the local preview. "
                                       "Retry with a vision-capable model from Refresh Models.")
                detail = error or result.stderr.strip()
                status = (f"exited with code {result.returncode}" if result.returncode
                          else "did not return a structured response")
                raise RuntimeError(f"Antigravity {status}. {detail[-1200:]}".rstrip())
            return _validate(data, task["step"], ranges)


def _output_error(stdout):
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return ""
    return str(envelope.get("error") or "") if isinstance(envelope, dict) else ""


def _stdout_output(text):
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and {"step", "critique", "is_satisfied", "params"} <= value.keys():
            return value
        if isinstance(value, dict):
            for key in ("structured_output", "response", "result", "output"):
                payload = value.get(key)
                if isinstance(payload, dict) and {"step", "critique", "is_satisfied", "params"} <= payload.keys():
                    return payload
                if isinstance(payload, str):
                    nested = _stdout_output(payload)
                    if nested is not None:
                        return nested
    return None


def _validate(data, step, ranges):
    if (not isinstance(data, dict) or data.keys() != {"step", "critique", "is_satisfied", "params"}
            or type(data["step"]) is not int or data["step"] != step
            or type(data["is_satisfied"]) is not bool or not isinstance(data["params"], dict)
            or not isinstance(data["critique"], str) or not data["critique"].strip()):
        raise ValueError("Antigravity response does not match the required output schema.")
    for name, value in data["params"].items():
        if (name not in ranges or type(value) not in (int, float)
                or not ranges[name][0] <= value <= ranges[name][1] or not math.isfinite(value)):
            raise ValueError(f"Invalid or unavailable Antigravity parameter: {name}.")
        # Match Bauhaus slider precision for Kelvin/tint.
        if name in ("temperature_k", "tint"):
            data["params"][name] = round(value, 0 if name == "temperature_k" else 3)
    return data
