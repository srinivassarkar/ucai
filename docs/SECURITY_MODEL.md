# Security Model & Threat Mitigation

Connecting autonomous agent loops to tools and external web interfaces introduces specific attack surfaces. `ucai` employs a defense-in-depth posture:

---

## 1. Threat Matrix & Mitigations

| Attack Vector | Vulnerability | ucai Mitigation |
| :--- | :--- | :--- |
| **Indirect Prompt Injection** | Web content or file contains adversarial instructions attempting to execute system commands. | **Physical Decoupling & Read-Only Shell:** Tool execution is confined to an isolated secondary machine. Destructive commands, shell piping, redirection, and state alteration are rejected at the parser level. |
| **Filesystem Traversal** | Model attempts to access `/etc/passwd` or SSH keys using relative `../` escapes or symlinks. | **Path Canonicalization:** All filesystem paths are resolved to their absolute canonical form. Operations resolving outside the sandbox root throw permission faults. |
| **Server-Side Request Forgery (SSRF)** | Model is tricked into accessing cloud metadata endpoints (e.g., `169.254.169.254`) or local network services. | **IP Range Blacklisting:** `web_fetch` resolves target hostnames and drops requests directed at loopback (`127.0.0.0/8`), private (`10.0.0.0/8`, `192.168.0.0/16`, `172.16.0.0/12`), link-local, and Carrier-Grade NAT blocks before connection initiation. |
| **Unauthorized Host Access** | Compromise of tool runtime attempts lateral movement to the inference server. | **Restricted SSH Key Pairs:** The SSH key utilized for stdio transport is locked to the specific source IP with the `restrict` option in `authorized_keys`. |
| **Unbounded Output DoS** | A tool output returns gigabytes of text, blowing context limits or crashing memory. | **Strict Byte Ceilings:** File reads are capped at 200 KB, scans skip files over 1 MB, and shell outputs are truncated at 200 KB. |

---

## 2. Shell Command Allowlist

The tool execution node permits only the following read-only utilities:
* `ls`, `cat`, `head`, `tail`, `grep`, `wc`, `file`, `stat`, `du`, `df`, `date`, `pwd`, `which`, `uname`, `sw_vers`, `mdls`

Any command containing shell metacharacters (`|`, `&`, `;`, `<`, `>`, `(`, `)`, `` ` ``, `$`, `\n`, `\r`, `{`, `}`, `[`, `]`) is blocked immediately.
