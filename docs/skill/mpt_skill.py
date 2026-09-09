#!/usr/bin/env python3
"""MoneyPrinterTurbo Skill runner with native, API and Docker transports."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SKILL_DIR.parents[1]
DEFAULT_API_URL = os.environ.get("MPT_API_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
COMPLETE = 1
FAILED = -1


class SkillRunnerError(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"[MoneyPrinterTurbo Skill v2] {message}", flush=True)


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", required=True)
    parser.add_argument(
        "--transport",
        choices=("auto", "native", "api", "docker"),
        default=os.environ.get("MPT_SKILL_TRANSPORT", "auto"),
    )
    parser.add_argument("--api-base-url", default=DEFAULT_API_URL)
    parser.add_argument("--api-key", default=os.environ.get("MPT_API_KEY", ""))
    parser.add_argument("--script-file", type=Path)
    parser.add_argument("--video-source", default="pexels")
    parser.add_argument("--image-provider", default="")
    parser.add_argument("--aspect", choices=("16:9", "9:16", "1:1"), default="9:16")
    parser.add_argument("--language", default="")
    parser.add_argument("--voice-name", default="")
    parser.add_argument("--clip-duration", type=int, default=5)
    parser.add_argument("--video-count", type=int, default=1)
    parser.add_argument("--no-subtitles", action="store_true")
    parser.add_argument("--bgm-type", default="random")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("native_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    args.subject = args.subject.strip()
    args.image_provider = args.image_provider.strip()
    if not args.subject:
        parser.error("--subject cannot be empty")
    if args.native_args and args.native_args[0] == "--":
        args.native_args = args.native_args[1:]
    return args


def _headers(api_key: str, json_body: bool = False):
    headers = {"Accept": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _urlopen(request: urllib.request.Request, timeout: int):
    """Avoid sending loopback API calls through a host-configured proxy."""
    host = (urllib.parse.urlsplit(request.full_url).hostname or "").lower()
    if host in {"127.0.0.1", "localhost", "::1"}:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


def request_json(base_url: str, path: str, *, api_key="", payload=None, timeout=120):
    url = base_url.rstrip("/") + "/" + path.lstrip("/")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        headers=_headers(api_key, payload is not None),
        method="POST" if payload is not None else "GET",
    )
    try:
        with _urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SkillRunnerError(f"HTTP {exc.code} {url}: {detail[:1200]}") from exc
    except urllib.error.URLError as exc:
        raise SkillRunnerError(f"cannot reach {url}: {exc.reason}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def api_is_ready(base_url: str) -> bool:
    try:
        return request_json(base_url, "/ping", timeout=3) == "pong"
    except Exception:
        return False


def wait_for_api(base_url: str, timeout=120):
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            if request_json(base_url, "/ping", timeout=10) == "pong":
                return
        except Exception as exc:
            last_error = str(exc)
        time.sleep(2)
    raise SkillRunnerError(f"MoneyPrinterTurbo API did not become ready: {last_error}")


def _replace_toml_value(text: str, key: str, value: Any) -> str:
    pattern = re.compile(rf"(?m)^({re.escape(key)}\s*=\s*).*$")
    if not pattern.search(text):
        raise SkillRunnerError(f"config field not found: {key}")
    encoded = json.dumps(value, ensure_ascii=False)
    return pattern.sub(lambda m: f"{m.group(1)}{encoded}", text, count=1)


def prepare_docker_bridge(project_root: Path, api_key: str, image_provider: str):
    provider_config = project_root / "providers.docker.json"
    provider_example = project_root / "providers.docker.example.json"
    if not provider_config.exists():
        if not provider_example.is_file():
            raise SkillRunnerError("providers.docker.example.json is missing")
        shutil.copy2(provider_example, provider_config)
        log(f"created provider config: {provider_config}")

    config_path = project_root / "config.toml"
    if not config_path.exists():
        shutil.copy2(project_root / "config.example.toml", config_path)
        log(f"created application config: {config_path}")

    text = config_path.read_text(encoding="utf-8")
    bridge = f"http://127.0.0.1:8080/api/v1/providers/{image_provider}"
    text = _replace_toml_value(text, "openai_image_base_url", bridge)
    text = _replace_toml_value(text, "openai_image_model", "provider-default")
    text = _replace_toml_value(text, "openai_image_api_keys", [api_key] if api_key else [])
    config_path.write_text(text, encoding="utf-8")
    log(f"configured openai_image bridge -> {image_provider}")


def docker_compose_files(project_root: Path) -> list[str]:
    """Return the compose files for the current host OS.

    Native Linux uses host networking so a provider bound only to
    127.0.0.1 on the host remains reachable from the MoneyPrinterTurbo API.
    Docker Desktop keeps the regular bridge setup.
    """
    base_name = "docker-compose.linux.yml" if is_linux() else "docker-compose.yml"
    base_path = project_root / base_name
    providers_path = project_root / "docker-compose.providers.yml"
    if not base_path.is_file():
        raise SkillRunnerError(f"required compose file is missing: {base_path}")
    if not providers_path.is_file():
        raise SkillRunnerError(f"required compose file is missing: {providers_path}")
    return [base_name, "docker-compose.providers.yml"]


def start_docker_api(project_root: Path):
    if shutil.which("docker") is None:
        raise SkillRunnerError("docker is not installed")

    try:
        subprocess.run(
            ["docker", "compose", "version"],
            cwd=project_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise SkillRunnerError(
            "docker compose is unavailable"
            + (f": {detail[:500]}" if detail else "")
        ) from exc

    compose_files = docker_compose_files(project_root)
    command = ["docker", "compose"]
    for filename in compose_files:
        command += ["-f", filename]
    command += ["up", "-d", "--build", "api"]

    network_label = "host network (Linux)" if is_linux() else "bridge network"
    log(f"starting MoneyPrinterTurbo API with Docker provider support using {network_label}")
    try:
        subprocess.run(command, cwd=project_root, check=True)
    except subprocess.CalledProcessError as exc:
        raise SkillRunnerError(f"docker compose failed: {exc.returncode}") from exc


def run_native(args):
    helper = SKILL_DIR / "mpt_agent.py"
    if not helper.is_file():
        raise SkillRunnerError(f"original helper is missing: {helper}")
    command = [
        "uv", "run", "--no-project", "--python", "3.11", "python",
        str(helper), "--subject", args.subject,
    ]
    if args.native_args:
        command += ["--", *args.native_args]
    log("using original/native MoneyPrinterTurbo Skill workflow")
    try:
        return subprocess.run(command, cwd=SKILL_DIR).returncode
    except FileNotFoundError as exc:
        raise SkillRunnerError("uv is not installed for native mode") from exc


def provider_health(base_url, api_key, provider_id):
    result = request_json(
        base_url,
        f"/api/v1/providers/{urllib.parse.quote(provider_id, safe='')}/health",
        api_key=api_key,
        timeout=45,
    )
    data = result.get("data", result) if isinstance(result, dict) else {}
    if not isinstance(data, dict) or not data.get("ok"):
        raise SkillRunnerError(f"provider is not healthy: {provider_id}: {data}")
    return data


def build_payload(args):
    payload = {
        "video_subject": args.subject,
        "video_aspect": args.aspect,
        "video_source": "openai_image" if args.image_provider else args.video_source,
        "video_clip_duration": args.clip_duration,
        "video_count": args.video_count,
        "subtitle_enabled": not args.no_subtitles,
        "bgm_type": args.bgm_type,
    }
    if args.language:
        payload["video_language"] = args.language
    if args.voice_name:
        payload["voice_name"] = args.voice_name
    if args.script_file:
        script_path = args.script_file.expanduser().resolve()
        if not script_path.is_file():
            raise SkillRunnerError(f"script file not found: {script_path}")
        payload["video_script"] = script_path.read_text(encoding="utf-8").strip()
    return payload


def submit_video(base_url, api_key, payload):
    result = request_json(base_url, "/api/v1/videos", api_key=api_key, payload=payload)
    data = result.get("data") or {} if isinstance(result, dict) else {}
    task_id = str(data.get("task_id", "")).strip() if isinstance(data, dict) else ""
    if not task_id:
        raise SkillRunnerError(f"video submission did not return task_id: {result}")
    log(f"task submitted: {task_id}")
    return task_id


def wait_task(base_url, api_key, task_id, timeout):
    deadline = time.monotonic() + timeout
    last_progress = None
    while time.monotonic() < deadline:
        result = request_json(
            base_url,
            f"/api/v1/tasks/{urllib.parse.quote(task_id, safe='')}",
            api_key=api_key,
            timeout=60,
        )
        data = result.get("data") or {} if isinstance(result, dict) else {}
        if not isinstance(data, dict):
            raise SkillRunnerError(f"invalid task response: {result}")
        progress = data.get("progress")
        if progress != last_progress:
            log(f"task progress: {progress}%")
            last_progress = progress
        if data.get("state") == COMPLETE:
            return data
        if data.get("state") == FAILED:
            raise SkillRunnerError(
                f"task failed at {data.get('failed_stage')}: {data.get('error')}"
            )
        time.sleep(3)
    raise SkillRunnerError(f"task did not finish within {timeout}s; task_id={task_id}")


def download_video(base_url, api_key, value, output):
    url = str(value or "").strip()
    if not url.startswith(("http://", "https://")):
        url = base_url.rstrip("/") + "/" + url.lstrip("/")
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers=_headers(api_key))
    try:
        with _urlopen(request, timeout=600) as response, output.open("wb") as f:
            shutil.copyfileobj(response, f)
    except Exception as exc:
        raise SkillRunnerError(f"failed to download final video: {type(exc).__name__}") from exc
    if output.stat().st_size <= 0:
        raise SkillRunnerError("downloaded final video is empty")
    return output


def run_api(args):
    base_url = args.api_base_url.rstrip("/")
    wait_for_api(base_url)
    log(f"using MoneyPrinterTurbo API: {base_url}")
    if args.image_provider:
        status = provider_health(base_url, args.api_key, args.image_provider)
        log(
            f"image provider healthy: {args.image_provider} "
            f"({status.get('elapsed_ms', 'n/a')} ms)"
        )
    task_id = submit_video(base_url, args.api_key, build_payload(args))
    task = wait_task(base_url, args.api_key, task_id, args.timeout)
    videos = task.get("videos") or []
    if not videos:
        raise SkillRunnerError("completed task has no final videos")
    output = args.output or (Path.cwd() / f"moneyprinterturbo-{task_id}.mp4")
    output = download_video(base_url, args.api_key, videos[0], output)
    print("MPT_RESULT")
    print(f"VIDEO_FILE={output}")
    print(f"TASK_ID={task_id}")
    print(f"TRANSPORT={args.transport}")
    if args.image_provider:
        print(f"IMAGE_PROVIDER={args.image_provider}")
    return 0


def resolve_transport(args) -> str:
    if args.transport != "auto":
        return args.transport
    if api_is_ready(args.api_base_url):
        return "api"
    if is_linux():
        return "docker"
    return "native"


def main(argv=None):
    args = parse_args(argv)
    args.transport = resolve_transport(args)
    log(f"selected transport: {args.transport}")

    if args.transport == "native":
        return run_native(args)
    if args.transport == "docker":
        if args.image_provider:
            prepare_docker_bridge(PROJECT_ROOT, args.api_key, args.image_provider)
        start_docker_api(PROJECT_ROOT)
        wait_for_api(args.api_base_url, timeout=180)
    return run_api(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SkillRunnerError, OSError, ValueError) as exc:
        print("MPT_ERROR", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
