"""The LocalPlane backend.

The backend owns durable truth. It asks the agent what the host says, decides what that
means — identity, health, management state — and records it. The agent reports facts; the
judgements live here, so that they can change without redeploying anything privileged.
"""
