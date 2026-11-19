# Architecture Overview

`ucai` implements a decoupled, multi-node architecture designed to grant a fully local, self-hosted Large Language Model (LLM) agentic capabilities (filesystem manipulation, read-only shell inspection, persistent memory, and web browsing) without compromising the host machine running model inference.

---

## 1. Physical Topology

The system physically decouples the **Inference Node (Compute Plane)** from the **Tool Node (Execution Plane)** across a private encrypted network mesh (e.g., Tailscale):

```text
       Phone / Laptop Browser
                 │
                 │ HTTP (Port 8080)
                 │ Encrypted over Private Mesh
                 ▼
 ┌────────────────────────────────────────────────────────┐
 │ INFERENCE NODE: COMPUTE PLANE (Apple Silicon / GPU)    │
 │                                                        │
 │  llama-server (Qwen 27B GGUF + Vision, 262k Context)   │
 │   • Serves Web UI & Manages Agent Loop                 │
 │   • Acts as Model Context Protocol (MCP) Host          │
 │                                                        │
 │  Local MCP Server: "web"                               │
 │   • web_search, web_fetch                              │
 │   • SSRF Protection (blocks private / link-local IPs)  │
 └───────────────────────────┬────────────────────────────┘
                             │
                             │ Encrypted SSH Tunnel over Private Mesh
                             │ (Forwards MCP JSON-RPC Stdio Pipes)
                             ▼
 ┌────────────────────────────────────────────────────────┐
 │ TOOL NODE: EXECUTION SANDBOX (Isolated Secondary Host) │
 │                                                        │
 │  Remote MCP Server: "ucai"                             │
 │   • Sandboxed File I/O (directory traversal checks)    │
 │   • Read-only Shell Allowlist (ls, cat, grep, stat)    │
 │   • Persistent Long-Term Memory (SQLite FTS5)          │
 │   • Append-only Audit Log (JSONL)                      │
 │                                                        │
 │  Sandbox Root: ~/agent_sandbox/                        │
 └────────────────────────────────────────────────────────┘
```

---

## 2. Interaction Lifecycle

1. **User Interaction:** The client submits a prompt through the Web UI hosted on the Inference Node.
2. **Context Formulation:** The Inference Node combines conversation history, system prompt, and registered tool definitions, streaming tokens through the model.
3. **Tool Invocations:** When the model outputs a structured tool invocation:
   - Web requests (`web_*`) execute on the local Inference Node process.
   - Filesystem, shell, or memory calls (`ucai_*`) are forwarded as JSON-RPC payloads over standard I/O through the persistent SSH tunnel to the Tool Node.
4. **Execution & Auditing:** The Tool Node verifies path boundaries, executes allowlisted commands, records the call to an append-only audit log, and streams results back over stdio.
5. **Synthesis:** The Inference Node appends the tool response to context and generates the final user-facing response.
