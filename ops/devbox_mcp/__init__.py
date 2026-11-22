"""The devbox MCP harness: the factory MCP tools, in-process, against a live run on a fresh OSS devbox.

`python -m devbox_mcp.run` submits it (see run.py for the one command); `task.py` is the privileged child
action, `steps.py` the steps it runs, and `probe_run.py` and `mcp_calls.py` the scripts those steps run
inside the devbox's engine environment.
"""
