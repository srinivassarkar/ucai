# ucai

**Host-Isolated Local LLM Agent with Decoupled Tool Sandboxing over MCP**

`ucai` is an architecture and toolkit for running a private, self-hosted autonomous agent. It physically separates model inference from tool execution across a secure network mesh (e.g., Tailscale), ensuring that external tool calls, filesystem operations, and internet browsing cannot compromise the primary compute host.

---

## Architecture Overview

Traditional local agent setups execute tools directly on the host running the model. In an era of web-browsing agents and indirect prompt injection, this exposes the primary workstation to severe operational risks.

`ucai` resolves this by decoupling the architecture into two dedicated roles:

```text
       Phone / Laptop Browser
                 │
                 │ HTTP (Port 8080)
                 │ Encrypted over Private Mesh (Tailscale)
                 ▼
 ┌────────────────────────────────────────────────────────┐
 │ INFERENCE NODE: COMPUTE PLANE (Apple Silicon / GPU)    │
 │                                                        │
 │  llama-server (Qwen 27B GGUF + Vision, 262k Context)   │
 │   • Serves Web UI & Manages Agent Inference Loop       │
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

1. **Inference Node (Compute Plane):** Hosts `llama-server` (serving high-parameter models such as Qwen 27B with native 262k context), hosts the interactive Web UI, and acts as the MCP client switchboard.
2. **Tool Node (Execution Sandbox):** An isolated secondary machine that executes file operations, runs read-only diagnostic commands, and manages durable search memory.
3. **Transport (MCP over SSH):** The Inference Node communicates with the Tool Node by spawning the tool daemon as a child process through a key-restricted SSH session. Stdio streams forward JSON-RPC payloads transparently without exposing listening ports.

---

## Key Features

* **Zero Cloud Dependency:** Runs 100% locally on your hardware. Zero API keys, zero token fees, zero data transmission outside your private mesh network.
* **Standardized Protocol (MCP):** Implements Anthropic's Model Context Protocol (JSON-RPC 2.0) across stdio pipes, making tool discovery native to the model runtime.
* **Zero-Dependency Tool Sandbox:** The `tool-node` runtime is implemented entirely in pure Python standard library (`sqlite3`, `pathlib`, `shlex`, `subprocess`). No external pip packages required.
* **Persistent Cross-Session Memory:** Backed by an embedded SQLite database using FTS5 (Full-Text Search). The agent records durable conclusions and retrieves historical context via BM25 lexical ranking.
* **Defense-in-Depth Security:**
  * Canonical path verification preventing directory traversal (`../`) and symlink escapes.
  * Shell execution restricted to an allowlist of 16 read-only diagnostic utilities. Metacharacters (`|`, `&`, `;`, `$`) are rejected.
  * Outbound web fetch tools enforce strict SSRF validation, blocking loopback, link-local, and private IP blocks.
  * Append-only JSONL audit logging for every tool invocation.

---

## Repository Structure

```text
ucai/
├── README.md                      # Project documentation and setup guide
├── LICENSE                        # Apache 2.0 License
├── .gitignore                     # Excludes models, databases, logs, and environments
├── .env.example                   # Configuration template for hosts and keys
│
├── inference-node/                # THE BRAIN: Model Host
│   ├── llama.sh                   # Inference daemon manager (Metal/CUDA, 262k context)
│   ├── mcp.template.json          # MCP configuration template for stdio tunneling
│   ├── webui_config.json          # System prompt and UI configuration
│   ├── web_mcp.py                 # SSRF-guarded web search & fetch tool
│   └── requirements.txt           # Inference-side dependencies (httpx)
│
├── tool-node/                     # THE HANDS: Isolated Execution Sandbox
│   ├── mcp_ucai.py                # Standalone tool server (Files, FTS5 Memory, Shell)
│   ├── setup_sandbox.sh           # Directory and permission initialization script
│   └── requirements.txt           # Zero dependencies (Standard library only)
│
├── cli/                           # THE SWITCHBOARD: Unified CLI
│   └── ucai                       # Orchestrator (up, down, status, doctor, logs)
│
└── docs/                          # Deep-Dive Documentation
    ├── ARCHITECTURE.md            # Detailed topology and interaction lifecycle
    └── SECURITY_MODEL.md          # Threat model and mitigation matrix
```

---

## Quickstart Guide

### Prerequisites
* Two networked machines connected over a secure private mesh (e.g., Tailscale).
* **Inference Node:** `llama.cpp` installed (`llama-server`), Python 3 with `httpx`.
* **Tool Node:** Python 3.10+ (standard library only), OpenSSH server.

### 1. Configure the Tool Node (Execution Sandbox)
On the secondary machine:
```bash
# Clone the repository
git clone https://github.com/srinivassarkar/ucai.git
cd ucai/tool-node

# Initialize the sandbox directory structure
chmod +x setup_sandbox.sh
./setup_sandbox.sh ~/agent_sandbox
```

### 2. Configure SSH Key Authentication
On the **Inference Node**, generate a dedicated, restricted SSH key:
```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/ucai_sandbox
```
Authorize the key on the **Tool Node** (`~/.ssh/authorized_keys`), prefixing it with security restrictions:
```text
from="<INFERENCE_NODE_IP>",restrict ~/.ssh/ucai_sandbox.pub
```

### 3. Configure the Inference Node
On the compute host:
```bash
cd ucai/inference-node
pip install -r requirements.txt

# Copy and configure the MCP wiring
cp mcp.template.json mcp.json
# Edit mcp.json with your sandbox user, IP, and key path
```

### 4. Launch with the Unified CLI
Copy `.env.example` to `.env` in the repository root and set your host details:
```bash
cp .env.example .env

# Verify end-to-end connectivity, SSH transport, and MCP tool discovery
./cli/ucai doctor

# Start the cluster and launch the Web UI
./cli/ucai up
```

---

## CLI Reference

The `cli/ucai` orchestrator manages the cross-node lifecycle:

```bash
./cli/ucai up        # Starts remote inference daemon, verifies health, launches UI
./cli/ucai status    # Displays compute health, sandbox transport, and tool count
./cli/ucai doctor    # Runs end-to-end network, SSH, and MCP diagnostic checks
./cli/ucai logs      # Streams live server logs over SSH
./cli/ucai down      # Gracefully shuts down inference engine and frees host memory
```

---

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for details.
