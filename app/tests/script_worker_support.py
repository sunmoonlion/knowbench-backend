"""Run an actual script payload in the local isolated worker image."""

import asyncio
import json

from app.domain.workbench.script_contract_generated import (
    ScriptExecution,
    execution_frame,
)


async def run_script_image(body):
    frame, raw = execution_frame(ScriptExecution.model_validate(body))
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--memory=256m",
        "--pids-limit=64",
        "--tmpfs",
        "/tmp",
        "-i",
        "--entrypoint",
        "/usr/bin/python3",
        "luna-local/script-input:work8",
        "/opt/sandbox/skill_execute.py",
        str(frame.size),
        frame.sha256,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(raw), 60)
    assert proc.returncode == 0, err.decode()
    ack, result = out.decode().splitlines()
    assert ack == "SUNMOON_INPUT_ACCEPTED"
    return json.loads(result)
