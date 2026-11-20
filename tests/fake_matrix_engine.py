"""Scripted external CLI for the single-credential integration matrix."""

import json
import os
import subprocess
import sys


def main(engine):
    with open(os.environ["MATRIX_LOG"], "a") as stream:
        stream.write(json.dumps({"engine": engine, "cwd": os.getcwd()}) + "\n")
    # The author session: the TUI has no `-p`; the stream-json session has `-p` but also `--input-format`.
    if engine == "claude" and ("-p" not in sys.argv or "--input-format" in sys.argv):
        from fake_claude import main as claude  # type: ignore[import-not-found]  # script: sibling module

        if os.environ.get("MATRIX_BROWSER"):
            assert "--strict-mcp-config" in sys.argv
            browser(sys.argv[sys.argv.index("--mcp-config") + 1])
        limited = bool(os.environ.get("MATRIX_AUTHOR_LIMIT"))
        if limited and "--input-format" not in sys.argv:
            print("⎿ You've hit your usage limit. Try again later.", flush=True)
        return claude(limited=limited and "--input-format" in sys.argv)
    answer = os.environ.get("MATRIX_ANSWER", "finished")
    if os.environ.get("MATRIX_LIMIT") or os.environ.get("MATRIX_AUTHOR_LIMIT"):
        if engine == "claude":
            print(json.dumps({"result": "You've hit your usage limit. Try again later.", "is_error": True}))
            return 1
        os.environ["FAKE_CODEX_LIMIT"] = "1"
    if engine == "codex":
        from fake_codex import main as codex  # type: ignore[import-not-found]  # script: sibling module

        os.environ["FAKE_CODEX_ANSWER"] = answer
        status = codex(sys.argv[1:])
        # Edit markers must not appear before Codex has accepted its configuration and finished the turn.
        if status == 0:
            for command in json.loads(os.environ.get("MATRIX_COMMANDS", "[]")):
                subprocess.run(command, shell=True, check=True)
        return status
    for command in json.loads(os.environ.get("MATRIX_COMMANDS", "[]")):
        subprocess.run(command, shell=True, check=True)
    print(json.dumps({"result": answer, "is_error": False}))
    return 0


def browser(config_path):
    import urllib.request
    from pathlib import Path

    config = json.loads(Path(config_path).read_text())
    server = config["mcpServers"]["browser"]

    def send(method, params=None):
        request = urllib.request.Request(
            server["url"],
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode(),
            {"Content-Type": "application/json", **server.get("headers", {})},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)["result"]

    send("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}})
    tool = send("tools/list")["tools"][0]["name"]
    print(send("tools/call", {"name": tool, "arguments": {"url": "https://example.com"}}))
