# Phase 10 — Shell tool(opt-in)+ Host vs Sandbox 選擇

> 📌 **接下來請繼續看 [`phase10b-expand-tools.md`](phase10b-expand-tools.md)**:沙盒就緒後該 AskUserQuestion 問使用者要哪些基礎工具(read_file / write_file / edit_file / glob_paths / grep_files / view_image / ask_user / done / run_python)。**Phase 10 沙盒裝完不是 Phase 10 結束** — 真正的基礎工具集擴張是 Phase 10b。
>
> 命名:本 phase 講的 `shell` 工具,在 Phase 10b 改名 `run_shell`(配 `run_python`)。Sandbox 模式下,allowlist **不啟用**(容器是邊界);Host 模式下才強制 allowlist。

## 何時跳過整個 Phase

純 read-only agent(只 query 外部 API、看檔案、回答問題)**不需要 shell**。沒寫 + 沒 self-evolution = 沒理由開 shell。Skip 整個 phase、agent 還是好用。

加 shell 的合理理由:
- 想讓 agent 寫 code 進 `agent/tools_proposed/`(Phase 12 self-evolution 的前置)
- agent 要跑使用者既有的 CLI script(`python build_report.py`)
- agent 要做 `git status / diff / log` 等 inspection

如果上面這些都不需要 → 跳過 phase 10、直接 phase 11。

## Host vs Sandbox 二選一

如果使用者選了開 shell,**接著問**:

```
shell 跑在哪個環境?

A) 🏠 Host (直接在你電腦上)
   ✅ 設定 0 步、效能原生、Mac/Linux 開箱即用
   ⚠ agent 失控可能砍到家目錄(只靠 permissions.json + deny-list 擋)
   ⚠ Windows 跑 bash command 要透過 git-bash / WSL,有點繞

B) 🐳 Sandbox (WSL2 + Docker Engine)
   ✅ shell 失敗 / 寫操作鎖在容器、安全很多
   ✅ 沒裝過 WSL/Docker?skill 附 setup_sandbox.bat 全自動裝(用 docker.com 官方 script、
      不用 Docker Desktop、避開商業授權)
   ⚠ Windows 必裝 WSL2 (一次性、~5 分鐘)
   ⚠ 首次 build container image ~3-5 分鐘
   ⚠ docker 命令必須能免 sudo 跑(setup.sh 會處理 docker group,WSL 重啟一次即生效)

不確定 → A 起步,日後 phase 12 self-evolution 開啟前升到 B。
要讓 LLM 寫 + 跑陌生 code(phase 12)→ 強烈建議 B。
```

## 模式分流的設計

兩個模式套**同樣三道把關**(deny-list → permissions.check → 模式判斷),只在「模式判斷」這層分流:

| 模式 | 模式判斷邏輯 | 為何安全 |
|---|---|---|
| **host** | 嚴格 allowlist:`ls / cat / git status / git log / git diff / find / grep / pwd / which / python --version` 等只讀指令自動跑、其他直接 refuse | 沒邊界、只能限制能跑什麼 |
| **sandbox** | **allowlist bypass**:除了 hard deny-list,任何指令都跑 | 容器 IS the boundary;LLM 把容器爆了 host 不痛 |

production 版本見 `assets/shell_tool.py` — `HostShellTool` 跟 `SandboxShellTool` 共用 `_guard()`、實際分流在 `run()`。Sandbox 額外提供 `run_python()` method,LLM 用 `python -` heredoc 餵 stdin 跑任意 Python,**避開所有 nested shell quoting 噩夢**。

## 路徑 A:Host 模式 — 標準 subprocess shell tool

```python
# agent/shell_tool.py
import subprocess, re, os, uuid, asyncio
from pathlib import Path

DENY_PATTERNS = [
    r"\brm\s+-rf\s+/", r"\bsudo\b", r"\bchmod\s+\+s",
    r"\.ssh\b", r"\bcrontab\b\s+-",
]

class ShellTool:
    def __init__(self, permissions, approval_callback, mode="host"):
        self.perm = permissions
        self.approve = approval_callback
        self.mode = mode  # 'host' 或 'sandbox'
        # 唯讀 allowlist(自動跑、不問使用者)
        self.allowlist = (
            "ls ", "cat ", "head ", "tail ", "wc ", "grep ", "find ",
            "git status", "git log", "git diff", "git branch",
            "python --version", "pip list", "node --version",
        )

    def run(self, command: str, cwd: str = None) -> dict:
        # Hard deny-list(即使使用者 approve 也不准)
        for pat in DENY_PATTERNS:
            if re.search(pat, command):
                return {"error": f"denied by hard guardrail: {pat}"}

        cwd = cwd or os.getcwd()
        try:
            self.perm.check(cwd, "write")
        except Exception as e:
            return {"error": f"cwd not in write-allowed folders: {e}"}

        # 唯讀 allowlist 自動跑,其他需 inline button 確認
        if not any(command.strip().startswith(p) for p in self.allowlist):
            if not self.approve(command):
                return {"error": "user denied", "command": command}

        try:
            r = subprocess.run(
                command, shell=True, cwd=cwd,
                capture_output=True, text=True, timeout=120,
            )
            return {
                "ok": r.returncode == 0,
                "exit_code": r.returncode,
                "stdout": (r.stdout or "")[-4000:],
                "stderr": (r.stderr or "")[-2000:],
                "command": command,
                "cwd": cwd,
            }
        except subprocess.TimeoutExpired:
            return {"error": "timeout (>120s)", "command": command}
```

## 路徑 B:Sandbox 模式 — WSL2 + Docker Engine

### Step 1:跑 pre-flight check

進 sandbox 前**強制**驗:

```python
# agent/sandbox_preflight.py
import shutil, subprocess

def preflight_sandbox() -> tuple[bool, list[str]]:
    """Return (ok, list_of_failures)."""
    failures = []

    # WSL 存在?
    if not shutil.which("wsl"):
        failures.append("WSL 未安裝。Admin PowerShell:  wsl --install,然後重啟。")

    # docker 在 WSL 內可達?
    r = subprocess.run(
        ["wsl", "-e", "bash", "-c", "docker info >/dev/null 2>&1 && echo OK || echo FAIL"],
        capture_output=True, text=True, timeout=10,
    )
    if "OK" not in r.stdout:
        failures.append(
            "Docker Engine 在 WSL 內不可達或需要 sudo。\n"
            "  → 第一次:跑 sandbox/setup_sandbox.bat 自動裝。\n"
            "  → 裝完後:wsl --shutdown 一次,讓 docker group 生效。"
        )

    # 容器存在?
    r2 = subprocess.run(
        ["wsl", "-e", "bash", "-c", "docker ps --format '{{.Names}}' | grep -q '^agent-sandbox$' && echo YES || echo NO"],
        capture_output=True, text=True, timeout=10,
    )
    if "YES" not in r2.stdout:
        failures.append(
            "agent-sandbox 容器未跑。\n"
            "  → 跑 sandbox/setup_sandbox.bat 建立並啟動。"
        )

    return (len(failures) == 0, failures)
```

**Pre-flight fail → 強制報錯停下、不准 silent fallback 到 host**(見下方 Anti-pattern)。不可以「失敗也勉強試」、也**不可以**「使用者選 sandbox、實際偷偷跑 host」。

### ⛔ Anti-pattern:silent fallback to host(資安漏洞)

```python
# ❌ ❌ ❌ 真實踩坑回報:這段看起來「貼心」、實際是資安漏洞
if SANDBOX_MODE == "sandbox":
    ok, fails = preflight_sandbox()
    if not ok:
        print("⚠ Falling back to host mode.")
        SANDBOX_MODE = "host"      # ← 使用者以為自己沙盒了、實際 host
```

**為什麼是漏洞**:使用者顯式設 `AGENT_SANDBOX_MODE=sandbox` 是**安全意圖**(LLM 失控時把破壞鎖在容器)。pre-flight fail 偷偷退回 host = 安全意圖被擦除、LLM 仍能寫 host filesystem、使用者完全不知道防護消失。

**正確做法 — fail loud**:

```python
# ✅ 顯式設 sandbox 但 pre-flight 失敗 → raise、停下、要使用者修
def make_shell_tool(permissions, approval_cb):
    mode = os.getenv("AGENT_SANDBOX_MODE", "host").lower().strip()

    if mode == "sandbox":
        ok, fails = preflight_sandbox()
        if not ok:
            msg = (
                "⛔ Sandbox 模式 pre-flight 失敗、拒絕啟動。\n"
                "問題:\n  - " + "\n  - ".join(fails) + "\n"
                "若你接受 host 模式風險(LLM 可寫使用者 home),"
                "顯式設 AGENT_SANDBOX_MODE=host 重啟,而非依賴自動退回。"
            )
            raise RuntimeError(msg)  # 停下、不偷偷退
        return SandboxShellTool(permissions, approval_cb)

    return ShellTool(permissions, approval_cb, mode="host")
```

> 規則:**模式切換必須是使用者顯式操作、不能是 framework 暗中代決**。

> 還有另一個常見坑:`SANDBOX_MODE = os.getenv(...)` 寫在 module top-level、跑一次就快取。改 `.env` 重啟 dotenv 沒重 import module 就**讀不到新值**。一律**每次 `make_shell_tool()` 進來才 `os.getenv`**、不要 cache。

### Step 2:Shell tool 走 `docker exec`

```python
# agent/shell_tool.py(sandbox mode 分支)

class SandboxShellTool(ShellTool):
    def __init__(self, *args, container="agent-sandbox", **kwargs):
        super().__init__(*args, **kwargs)
        self.container = container
        self.mode = "sandbox"

    def _wrap(self, command: str, cwd: str) -> list[str]:
        # 在容器內跑 — bind-mount 讓容器 cwd 跟 host cwd 同路徑、不必翻譯
        return [
            "wsl", "-e", "bash", "-c",
            f"docker exec -w {cwd!r} {self.container} bash -c {command!r}",
        ]

    def run(self, command: str, cwd: str = None) -> dict:
        # 同樣 deny-list / permissions / approval 三道把關
        for pat in DENY_PATTERNS:
            if re.search(pat, command):
                return {"error": f"denied by hard guardrail: {pat}"}
        cwd = cwd or os.getcwd()
        try:
            self.perm.check(cwd, "write")
        except Exception as e:
            return {"error": f"cwd not in write-allowed folders: {e}"}
        if not any(command.strip().startswith(p) for p in self.allowlist):
            if not self.approve(command):
                return {"error": "user denied", "command": command}
        # 跑 docker exec
        try:
            args = self._wrap(command, cwd)
            r = subprocess.run(args, capture_output=True, text=True, timeout=120)
            return {
                "ok": r.returncode == 0,
                "exit_code": r.returncode,
                "stdout": (r.stdout or "")[-4000:],
                "stderr": (r.stderr or "")[-2000:],
                "command": command,
                "cwd": cwd,
                "mode": "sandbox",
            }
        except subprocess.TimeoutExpired:
            return {"error": "timeout (>120s)", "command": command}
```

### Step 3:讓 agent 知道自己在哪個模式 + 路徑映射

把 `mode` 跟**路徑映射規則**一起注入 system prompt(Phase 9 動態注入):

```
## 🖥 執行環境
shell tool 跑在 **{mode}** 模式。

{mode=host}:
- 直接在使用者 OS 跑、檔案路徑就是 host 原路徑(Windows `C:\...` / *nix `/...`)。

{mode=sandbox}(WSL + Docker bind-mount):
- 容器內看到的路徑 = host 路徑映射:
  - Windows `C:\Users\me\proj\foo.txt`  ↔ 容器內 `/mnt/c/Users/me/proj/foo.txt`
  - Windows `D:\data\bar.csv`            ↔ 容器內 `/mnt/d/data/bar.csv`
- **永遠用絕對路徑** — sandbox 的 `cwd` 不一定跟 host 同步,相對路徑會找錯檔
- 你呼叫 `run_shell` / `run_python` 用 sandbox 內路徑 (`/mnt/c/...`)
- 你呼叫 `write_file` / `read_file`(host tool)用 host 原路徑 (`C:\...`)
- **不確定就先 `pwd` 看自己在哪、用絕對路徑省事**
```

### Tool 回傳 path 一律用 host 原路徑

寫過檔的 tool(`write_file`、`save_image`、`export_report` 等)回傳 `path` 欄位**一律用 host 原路徑**、不要回 `/mnt/c/...`,否則 agent 下一輪以為要去 sandbox 找、走錯岔路。

```python
# ✅ 對:tool 回傳 host 原路徑
return {"ok": True, "path": "C:/Users/me/output/report.pdf"}

# ❌ 錯:tool 回傳 sandbox 內路徑
return {"ok": True, "path": "/mnt/c/Users/me/output/report.pdf"}
```

容器內路徑只在 LLM 要呼 `run_shell` 操作該檔案時、由 LLM 自己加 `/mnt/c/` 前綴。tool 之間互傳一律 host 原路徑。

## Approval callback(共用、TG inline button)

Phase 8(Telegram adapter)的 `_request_approval()` 給兩個模式共用 — shell tool 只用一個 `approve(command) → bool` callback,模式差異對 TG 透明。

## 設定切換

Agent 設定檔加一個欄位:

**用 Step 4 的 fail-loud 版本**(`make_shell_tool` 在 sandbox preflight 失敗時 `raise RuntimeError`,不 silent fallback)。**重要**:每次都用 `os.getenv` 讀(不要 module-load 時讀進 const)— 否則 `.env` 修改 / 重啟 dotenv 後讀不到新值,變成「使用者改了設定卻沒生效」的詭異情況。

```python
def make_shell_tool(permissions, approval_cb):
    mode = os.getenv("AGENT_SANDBOX_MODE", "host").lower().strip()
    # ... 見 Step 4 fail-loud 範例 ...
```

## Shell 能做的事(對 self-evolution 來說是基礎)

- agent `pip install requests` → 寫新 tool → asks for reload
- agent `git status / diff / log`(push/force-push 進 deny-list)
- agent `pytest -x -k test_foo` 驗證自己寫的東西
- agent `tail -f logs/app.log` 看 debug

## 硬性 guardrail

即使使用者按了 approve,**永遠**不准:
- 修 `permissions.json` 透過 shell(必須走 code review)
- `chmod +s`、改 `~/.ssh/`、改 login shell
- `rm -rf /`、`format`、cron 安裝、`sudo` 任何東西
- (sandbox mode 額外)逃出 `/workspace`、`docker stop/rm` 自己

Deny-list 在 `ShellTool.run()` 開頭、permissions 後面、approval 前面跑。

## Skill 提供的 assets

```
assets/sandbox/
├── setup_sandbox.bat     # Windows 入口(CMD)、調用 setup.sh
├── setup.sh              # WSL 內安裝邏輯(裝 Docker Engine、配 systemd、build image、啟容器)
└── Dockerfile            # 最小 Python 容器、CJK locale、核心套件
```

把整個 `assets/sandbox/` 複製到使用者專案的 `sandbox/` 目錄就好。第一次跑 `sandbox\setup_sandbox.bat`、之後改 Dockerfile 用 `--rebuild` 重 build。

### ⛔ 不要重寫 `setup_sandbox.bat` — 它已修好 3 個坑

SKILL 附的 `setup_sandbox.bat` 已內建 3 道防線、整支**全英文**:

| 防線 | 做什麼 | 不做會怎樣(真實踩坑) |
|---|---|---|
| `chcp 65001 >NUL` | 切 CMD 到 UTF-8 | echo 中文變亂碼 `µ▓Öτ¢Æτ╜▓σòƒσïò`、看不懂 |
| `wslpath -a "%~dp0.."` 翻譯路徑 | 把 Windows 路徑轉 `/mnt/c/...` | WSL 預設 cwd 是 `~`、`./sandbox/setup.sh: No such file or directory` |
| `sed -i 's/\r$//'` 自癒 CRLF | 強制把 setup.sh 轉成 LF | git autocrlf 把 .sh 轉 CRLF → bash 噴 `\r: command not found` 或 `set -euo pipefail\r: invalid option` |

**規則**:LLM / coding agent 跑這 phase 時,**不要「為了客製化而重寫」`setup_sandbox.bat`**。直接 `copy assets/sandbox/setup_sandbox.bat <project>/sandbox/`。要改變數就用 `SET "VAR=..."`、不要動骨幹。

如果使用者要中文輸出 → 用 SKILL asset 自帶的版本(已 `chcp 65001` 開頭);**如果偏好 100% 不踩編碼坑、就用全英文(本 SKILL 的預設)**。中英混寫但沒設 codepage、是踩坑的最快路徑。

### Dockerfile / Image 命名一致性

SKILL 預設容器名 `agent-sandbox`(通用)。若使用者改成專案名(例如 `myproj-sandbox`),**4 個地方必同步**:
- `setup.sh` 的 `docker run --name myproj-sandbox`
- `shell_tool.py` 的 `SandboxShellTool(container="myproj-sandbox")`
- `sandbox_preflight.py` 內 `grep -q '^myproj-sandbox$'`
- `shell_tool.py` 的 `DENY_PATTERNS` 自殺保護 `\bdocker\s+(stop|rm|kill)\s+myproj-sandbox\b`

漏一個 → preflight 永遠 fail / shell 找不到容器 / agent 可自殺。要改容器名建議用 `grep -rn 'agent-sandbox' assets/` 看到 4 處全改才行。

## 檢查清單

- [ ] 跟使用者確認:**真的需要 shell 嗎?**(read-only agent 不要開)
- [ ] 選了 shell → 問 host 還是 sandbox
- [ ] **WSL systemd 已開**(`wsl -e bash -c "systemctl --version"` 不 fail)— 否則 host 重開機後 dockerd 不會自動回
- [ ] 選 sandbox → 複製 `assets/sandbox/` 到專案、引導跑 `setup_sandbox.bat`
- [ ] pre-flight check 失敗就強制退回 host 或要使用者補完再來
- [ ] DENY_PATTERNS deny-list 跑在 permissions 跟 approval 之前
- [ ] 跟使用者測一次:讓 agent 跑 `ls /` 看 host 跟 sandbox 結果不同(確認分流真的有效)
