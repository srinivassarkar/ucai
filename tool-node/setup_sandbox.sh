#!/usr/bin/env bash
set -euo pipefail

# Initialize sandboxed tool runtime environment
SANDBOX_DIR="${1:-$HOME/agent_sandbox}"

echo "[+] Initializing ucai tool sandbox in: $SANDBOX_DIR"
mkdir -p "$SANDBOX_DIR/files"
mkdir -p "$SANDBOX_DIR/memory"
mkdir -p "$SANDBOX_DIR/logs"

# Ensure proper permissions
chmod 700 "$SANDBOX_DIR"
chmod 700 "$SANDBOX_DIR/memory"
chmod 700 "$SANDBOX_DIR/logs"

echo "[✓] Sandbox directory initialized successfully."
