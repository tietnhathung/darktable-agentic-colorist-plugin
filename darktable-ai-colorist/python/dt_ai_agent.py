import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from antigravity_bridge import Antigravity, run_process
from codex_bridge import Codex
from session_manager import Session

ROOT = Path(__file__).resolve().parent.parent
VERSION = "5.6.2"


def load_config(path):
    config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    if path.is_file():
        user_config = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(user_config, dict):
            raise ValueError("config.json must contain an object.")
        config.update(user_config)
    for name, maximum in (("max_inner_steps", 20), ("antigravity_timeout_seconds", 3600),
                          ("darktable_timeout_seconds", 3600), ("preview_size", 8192)):
        if type(config[name]) is not int or not 1 <= config[name] <= maximum:
            raise ValueError(f"{name} must be an integer between 1 and {maximum}.")
    if not isinstance(config.get("models"), dict) or not all(
        re.fullmatch(r"[\w.-]+", key) and isinstance(value, str) and re.fullmatch(r"[\w.-]+", value)
        for key, value in config["models"].items()
    ):
        raise ValueError("models must map aliases to Antigravity model slugs.")
    for key in ("antigravity_bin", "codex_bin", "darktable_cli_path", "rawspeed_cameras"):
        value = config.get(key)
        if value:
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a path or executable name.")
            value = os.path.expandvars(os.path.expanduser(value))
            if Path(value).parent != Path(".") and not Path(value).is_absolute():
                value = str(path.parent / value)
            config[key] = value
    return config


def list_models(config):
    models, warnings = [], []
    for provider in (Antigravity, Codex):
        try:
            models.extend(provider(config).models())
        except (OSError, RuntimeError, ValueError) as exc:
            warnings.append(str(exc))
    if not models:
        raise RuntimeError("No grading models available. " + " ".join(warnings))
    return {"models": models, "warnings": warnings}


class Renderer:
    def __init__(self, config, work, user_config=None):
        name = config.get("darktable_cli_path")
        system_path = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "darktable/bin/darktable-cli.exe"
        mac_path = Path("/Applications/darktable.app/Contents/MacOS/darktable-cli")
        binary = shutil.which(name) if name else shutil.which("darktable-cli")
        if name and Path(name).is_file():
            binary = name
        if not binary and not name:
            binary = next((str(path) for path in (system_path, mac_path) if path.is_file()), None)
        if not binary:
            raise FileNotFoundError("darktable-cli not found. Add it to PATH or set darktable_cli_path.")
        self.binary = Path(binary).resolve()
        version = run_process([str(self.binary), "--version"], 20).stdout
        if not re.search(rf"\bdarktable\s+{re.escape(VERSION)}(?:\s|$)", version):
            raise ValueError(f"This XMP writer targets Darktable {VERSION}. Found: {version.splitlines()[:1]}")
        self.config_dir = work / "darktable-config"
        self.config_dir.mkdir()
        if user_config:
            profiles = Path(user_config) / "color"
            if profiles.is_dir():
                shutil.copytree(profiles, self.config_dir / "color")
        self.size = str(config["preview_size"])
        self.timeout = config["darktable_timeout_seconds"]
        self.cameras = config.get("rawspeed_cameras")
        candidates = (
            self.binary.parent.parent / "share/darktable/rawspeed/cameras.xml",
            self.binary.parent.parent / "Resources/share/darktable/rawspeed/cameras.xml",
            Path("/usr/share/darktable/rawspeed/cameras.xml"),
            Path("/usr/local/share/darktable/rawspeed/cameras.xml"),
        )
        self.cameras = Path(self.cameras) if self.cameras else next((path for path in candidates if path.is_file()), None)

    def render(self, raw, sidecar, output):
        # The Windows CLI treats backslashes in its output filename as template escapes.
        command = [self.binary.as_posix(), raw.as_posix()]
        if sidecar is not None:
            command.append(sidecar.as_posix())
        command += [
            output.as_posix(), "--width", self.size, "--height", self.size,
            "--icc-type", "SRGB", "--apply-custom-presets", "false",
            "--core", "--configdir", self.config_dir.as_posix(), "--disable-opencl",
        ]
        result = run_process(command, self.timeout)
        diagnostics = result.stdout + "\n" + result.stderr
        if (not output.is_file() or output.stat().st_size == 0
                or re.search(r"can't open XMP|error reading xmp|wrong params size|"
                             r"failed to (?:read|decode).*xmp|corrupt history|masks? version mismatch",
                             diagnostics, re.I)):
            raise RuntimeError(f"Darktable did not render the requested history. {diagnostics.strip()[-3000:]}")


def process(args, config, session, mode):
    from image_analyzer import analyze_image
    from xmp_modifier import FIELDS, History, camera_profile

    raw = Path(args.raw).resolve(strict=True)
    if not raw.is_file():
        raise ValueError("--raw must point to an image file.")
    preview = Path(args.preview).resolve(strict=True) if args.preview else None
    state = session.load() if mode == "refine" else {
        "raw": str(raw), "initial_prompt": args.prompt, "current_params": {}, "conversation_history": [],
    }
    if state["raw"] != str(raw):
        raise ValueError("This session belongs to another image.")
    feedback = args.feedback if mode == "refine" else ""
    if not (feedback if mode == "refine" else args.prompt).strip():
        raise ValueError("Enter a style prompt or refinement feedback.")
    steps = args.max_steps or config["max_inner_steps"]
    if not 1 <= steps <= 20:
        raise ValueError("--max-steps must be between 1 and 20.")
    bridge = Codex(config) if args.model.startswith("chatgpt/") else Antigravity(config)
    with tempfile.TemporaryDirectory(prefix="darktable-colorist-") as directory:
        work = Path(directory)
        renderer = Renderer(config, work, args.darktable_config)
        is_raw, matrix = camera_profile(raw, renderer.cameras, args.is_raw, args.maker, args.camera)
        source = Path(args.xmp).resolve(strict=True) if args.xmp else None
        if source is None and mode == "refine":
            source = work / "previous.xmp"
            source.write_text(state["current_xmp"], encoding="utf-8")
        elif source is None and Path(f"{raw}.xmp").is_file():
            source = Path(f"{raw}.xmp")
        baseline = work / "before.xmp"
        History(source, is_raw).write(baseline, {}, matrix, {})
        source = baseline if source is not None else None
        history = History(source, is_raw)
        current = history.current(matrix)
        if mode == "refine" and not args.xmp:
            current.update(state["current_params"])
        if args.temperature is not None and args.tint is not None and matrix is not None:
            current.update(temperature_k=args.temperature, tint=args.tint)
        if preview is None:
            preview = work / "baseline.jpg"
            renderer.render(raw, source, preview)
        final_xmp = baseline
        conversation = state["conversation_history"]
        if feedback:
            conversation.append({"role": "user", "refinement_feedback": feedback})
        last_metrics = analyze_image(preview)
        ranges = {key: [low, high] for key, (_, _, _, low, high) in FIELDS.items()
                  if matrix is not None or key not in ("temperature_k", "tint")}
        for step in range(1, steps + 1):
            task = {
                "step": step, "max_steps": steps, "model": args.model,
                "initial_prompt": state["initial_prompt"], "refinement_feedback": feedback or None,
                "current_params": current, "histogram_metrics": last_metrics,
                "darktable_context": history.grading_context(),
                "conversation_history": conversation, "allowed_params": ranges,
            }
            proposal = bridge.reason(preview, task, work)
            changes = {key: value for key, value in proposal["params"].items()
                       if key not in current or abs(value - current[key]) > 1e-7
                       or not history.enabled(FIELDS[key][0])}
            if changes:
                next_xmp = work / f"edit-{step}.xmp"
                history.write(next_xmp, changes, matrix, current)
                next_preview = work / f"preview-{step}.jpg"
                renderer.render(raw, next_xmp, next_preview)
                current.update(changes)
                preview, final_xmp = next_preview, next_xmp
                last_metrics = analyze_image(preview)
            conversation.append({"role": "assistant", **proposal, "rendered_histogram": last_metrics})
            if proposal["is_satisfied"] and not changes:
                break
        encoded = final_xmp.read_text(encoding="utf-8")
        if args.output_xmp:
            output = Path(args.output_xmp).resolve()
            if output == raw or (source is not None and args.xmp and output == Path(args.xmp).resolve()) \
                    or output == Path(f"{raw}.xmp"):
                raise ValueError("--output-xmp must point to a separate output file.")
            output.write_text(encoded, encoding="utf-8")
        state.update(current_params=current, current_xmp=encoded)
        session.save(state)
        return {
            "session_id": args.session_id, "critique": proposal["critique"], "step": step,
            "is_satisfied": proposal["is_satisfied"] and not changes, "params": current,
            "histogram_metrics": last_metrics, "changed": final_xmp != baseline,
        }


def parser():
    result = argparse.ArgumentParser(description=__doc__ or "Darktable 5.6.2 color grading agent")
    actions = result.add_mutually_exclusive_group(required=True)
    for name in ("list-models", "run", "refine", "approve"):
        actions.add_argument(f"--{name}", dest="mode", action="store_const", const=name)
    actions.add_argument("--ui-request", type=Path, metavar="PATH")
    result.add_argument("--config", type=Path, default=ROOT / "config.json")
    result.add_argument("--session-dir", type=Path, default=Path(tempfile.gettempdir()) / "darktable-ai-colorist")
    result.add_argument("--session-id")
    result.add_argument("--raw")
    result.add_argument("--preview")
    result.add_argument("--xmp")
    result.add_argument("--output-xmp")
    result.add_argument("--model", default="antigravity/default")
    result.add_argument("--prompt", default="")
    result.add_argument("--feedback", default="")
    result.add_argument("--max-steps", type=int)
    return result


def from_ui(args):
    request = {}
    for line in args.ui_request.read_text(encoding="ascii").splitlines():
        name, value = line.split("=", 1)
        request[name] = bytes.fromhex(value).decode("utf-8")
    for name in ("mode", "session_id", "raw", "xmp", "output_xmp", "model", "prompt", "feedback",
                 "maker", "camera", "darktable_config"):
        if name in request:
            setattr(args, name, request[name])
    args.max_steps = int(request["max_steps"]) if "max_steps" in request else None
    args.is_raw = request.get("is_raw") == "true" if "is_raw" in request else None
    args.temperature = float(request["temperature"]) if "temperature" in request else None
    args.tint = float(request["tint"]) if "tint" in request else None


def main():
    args = parser().parse_args()
    args.maker = args.camera = ""
    args.is_raw = args.temperature = args.tint = args.darktable_config = None
    result, error = None, None
    try:
        if args.ui_request:
            from_ui(args)
        if args.mode == "list-models":
            result = list_models(load_config(args.config))
        else:
            if not args.session_id:
                raise ValueError("--session-id is required.")
            session = Session(args.session_dir, args.session_id)
            with session.locked():
                if args.mode == "approve":
                    session.approve()
                    result = {"approved": True, "session_id": args.session_id}
                elif args.mode in {"run", "refine"} and args.raw:
                    result = process(args, load_config(args.config), session, args.mode)
                else:
                    raise ValueError("--raw is required for run/refine.")
    except Exception as exc:
        error = str(exc)
        print(json.dumps({"error": error}, ensure_ascii=True), file=sys.stderr)
    if args.ui_request:
        lines = {"ok": str(error is None), "message": error or (result or {}).get("critique", "")}
        if result:
            lines["models"] = "\n".join(result.get("models", []))
            lines["warnings"] = "\n".join(result.get("warnings", []))
            lines["changed"] = str(result.get("changed", False))
            lines["verified"] = str(result.get("is_satisfied", False))
            lines.update({f"param_{name}": str(value) for name, value in result.get("params", {}).items()})
        args.ui_request.with_suffix(".reply").write_text(
            "".join(f"{key}={value.encode('utf-8').hex()}\n" for key, value in lines.items()), encoding="ascii"
        )
    if result:
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return int(error is not None)


if __name__ == "__main__":
    sys.exit(main())
