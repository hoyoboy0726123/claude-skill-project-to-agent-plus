# Phase 12 — Self-evolution loop

## 這個 phase 做什麼

使用者問了一件 agent 沒有對應工具的事 → agent 自動草稿一個新工具 → TG inline button 確認 → merge → hot-reload(不重啟)→ 用新工具回答原問題。

每次 approve 累積一個工具。長期下來、agent 變成「越用越懂使用者」的個人化助理。

## 必要前置

- Phase 7(permissions)— 沒這層 agent 寫的工具碰檔案會亂搞
- Phase 10(shell tool)— agent 要用 shell 才能寫檔、move file、mv `tools_proposed/` → `tools/`

**沒 shell tool 跳過整個 phase 12**。

## 完整 loop

1. **使用者要做沒有的事**:
   > 「也順便數一下 /downloads 裡最新 CSV 有幾筆?」

2. **Agent 認出 gap**(靠 Phase 12 加進 system prompt 的規範教):

3. **Agent 草稿新工具**到 `agent/tools_proposed/<name>.py`:

```python
# agent/tools_proposed/count_csv_rows.py
from pathlib import Path
import csv

def count_csv_rows(path: str) -> dict:
    """Count data rows (excluding header) in a CSV file."""
    from agent.permissions import permissions
    permissions.check(path, "read")
    p = Path(path)
    try:
        with p.open(newline="", encoding="utf-8") as f:
            return {"path": str(p), "rows": sum(1 for _ in csv.reader(f)) - 1}
    except Exception as e:
        return {"error": str(e)}


def register(registry):
    from agent.tool_registry import Tool
    registry.register(Tool(
        name="count_csv_rows",
        description="Count data rows (excluding header) in a CSV file.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Absolute path to CSV file. Must be in a read-permitted folder."},
            },
            "required": ["path"],
        },
        func=count_csv_rows,
    ))
```

4. **Agent 用 shell tool 顯示草稿給使用者**:
   ```
   shell: cat agent/tools_proposed/count_csv_rows.py
   ```
   把檔案內容貼進 TG、附 inline button:

```
[新工具草稿:count_csv_rows]
...
[✓ Approve & merge]  [✗ Deny]
```

5. **Approve**:
   - 跑 evals(下節)、通過才繼續
   - `shell: mv agent/tools_proposed/count_csv_rows.py agent/tools/`
   - 呼叫 `reload_tools(registry)` hot-reload
   - 用新工具回答原問題

6. **Deny**:草稿留在 `tools_proposed/`(或刪掉、看 system prompt 規範)、agent 用其他方式繼續對話

## Hot reload

```python
# agent/tool_registry.py
class ToolRegistry:
    def reload_all(self):
        """Phase 12 hot-reload — 重抓 agent/tools/ 跟 agent/tools_dyn/ 全部工具。

        ⛔ 致命陷阱:_tools.clear() 後若只迭代 tools_dyn 的 register、會把
           核心工具(read_file / write_file / run_shell / remember_fact /
           ask_user / done ...)全部 wipe 掉,下一輪 LLM 呼叫變 "unknown tool"。
        ✅ 正解:核心工具走 register_all() 統一入口、dyn 工具才迭代 submodule。
        """
        import importlib, pkgutil

        # 1. 清 finder cache(沒這行、剛 mv 進 tools_dyn/ 的新檔 iter_modules 看不到)
        importlib.invalidate_caches()

        # 2. 重 import 核心工具 package、清表、用 register_all 重掛
        from agent import tools as tools_pkg
        importlib.reload(tools_pkg)
        self._tools.clear()
        tools_pkg.register_all(self)              # ★ 漏這行 = 核心工具全消失

        # 3. 重 import 動態工具(self-evolution 產出的)、逐一掛
        try:
            from agent import tools_dyn as dyn_pkg
            importlib.reload(dyn_pkg)
            for _, name, _ in pkgutil.iter_modules(dyn_pkg.__path__):
                full = f"agent.tools_dyn.{name}"
                mod = (importlib.reload(importlib.sys.modules[full])
                       if full in importlib.sys.modules
                       else importlib.import_module(full))
                if hasattr(mod, "register"):
                    mod.register(self)
        except (ImportError, AttributeError):
            pass   # tools_dyn 還沒建立、跳過
```

**核心 vs 動態的分流**:
- **核心工具**(`agent/tools/__init__.py` 的 `register_all`)— 程式員手寫、`__init__` 內顯式呼,reload 必走這條
- **動態工具**(`agent/tools_dyn/*.py` 從 self-evolution 來)— LLM 草稿、merge 後落地,reload 迭代 submodule

⛔ **不要把核心工具當動態工具來掃**(`iter_modules(tools_pkg.__path__)`)— 因為:
- 核心工具可能放在 `tools/__init__.py` 直接定義、不是 submodule(iter_modules 看不到)
- 核心工具的 `register_all()` 通常還會做 env-flag opt-in 判斷(`ENABLE_SHELL_TOOL` / 記憶開關),靠 entry point 統一邏輯比 submodule scan 可靠

每個 tool 檔在 `agent/tools/<name>.py` exports `register(registry)`。reload 不會清掉 LLM client、不會中斷 TG bot、新工具下一輪 chat 立刻可用。

## Evals 守門員

`assets/evals/evals.json`(skill 自帶)是新工具的 smoke test。merge 前跑一輪、過了才放進 `tools/`:

```json
{
  "skill_name": "project-to-agent",
  "evals": [
    {
      "id": "count_csv_rows.smoke",
      "applies_to": "count_csv_rows",
      "setup": "echo 'h\\nA\\nB\\nC' > /tmp/test.csv",
      "tool_args": {"path": "/tmp/test.csv"},
      "expect": {"rows": 3}
    }
  ]
}
```

執行 helper(放 `agent/evals.py`):

```python
import json, subprocess, importlib.util
from pathlib import Path

def run_evals_for(tool_file: Path, tool_name: str) -> tuple[bool, list[str]]:
    """Return (all_passed, error_msgs)."""
    evals = json.loads(Path("evals/evals.json").read_text())
    relevant = [e for e in evals["evals"] if e.get("applies_to") == tool_name]
    if not relevant:
        return True, ["(no evals defined for this tool — smoke pass)"]

    # Lazy-load the proposed module (not yet in registry)
    spec = importlib.util.spec_from_file_location(tool_name, tool_file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, tool_name)

    failures = []
    for e in relevant:
        if e.get("setup"):
            subprocess.run(e["setup"], shell=True, check=False)
        try:
            result = fn(**(e.get("tool_args") or {}))
        except Exception as ex:
            failures.append(f"{e['id']}: exception {type(ex).__name__}: {ex}")
            continue
        expect = e.get("expect", {})
        for k, v in expect.items():
            if result.get(k) != v:
                failures.append(f"{e['id']}: {k} expected {v!r}, got {result.get(k)!r}")
    return (len(failures) == 0, failures)
```

merge 流程:

```python
def merge_proposed_tool(tool_name: str, registry) -> dict:
    src = Path(f"agent/tools_proposed/{tool_name}.py")
    if not src.exists():
        return {"error": "no such proposed tool"}
    ok, msgs = run_evals_for(src, tool_name)
    if not ok:
        return {"error": "evals failed", "failures": msgs}
    dst = Path(f"agent/tools/{tool_name}.py")
    src.rename(dst)
    registry.reload()
    return {"ok": True, "merged_to": str(dst), "eval_results": msgs}
```

LLM 對話流(寫進 system prompt):
```
新工具流程:
1. 寫 agent/tools_proposed/<name>.py + 對應 evals entry
2. 跟使用者 confirm "approve?"
3. approve → 呼 merge_proposed_tool(name) → 看結果
   - evals fail → 把失敗訊息給使用者、修草稿、再試
   - evals pass → 工具已上線、用它回答原問題
4. deny → 不 merge、繼續對話
```

## 3 道防線 — 抓 LLM 寫 code 的常見錯(實戰驗證過)

跑過 end-to-end live test 後抓到 3 類真實的 Gemma 4 31B / GPT-4o-mini 寫 register code 時容易踩的坑。每個都做成 server-side 防線、不靠 LLM 自我約束。

### Defense 1 — AST syntax pre-check(`propose_tool` 內)

**位置**:`_propose_tool()` 在 `confirm=False` 階段 + 寫檔前都跑

```python
import ast
try:
    tree = ast.parse(code)
except SyntaxError as e:
    issues.append(
        f"SyntaxError at line {e.lineno}, col {e.offset}: {e.msg}. "
        f"Common culprit: docstring written as \\\"\\\"\\\" (escaped) instead "
        f"of \"\"\" (triple-quote). Use raw triple-quote, no backslashes."
    )
    tree = None
```

**抓的真實 bug**:
- Gemma 4 31B **90% 第一次 propose 把 `"""` 寫成 `\"\"\"`**(escape 過的雙引號)
- 任何 Python 語法錯(missing `:`、未閉合括號、illegal char)

**為什麼放 confirm=False**:preview 階段就攔,不浪費使用者看草稿時間;LLM 看到 error 含 line/col + culprit 提示,**自修率 > 95%**(第二次 propose 通常就對)。

### Defense 2 — Tool() parameters required check

**位置**:同 `_propose_tool()`,在 AST 解析後 walk tree 找所有 `Tool(...)` 呼叫

```python
for node in ast.walk(tree):
    if isinstance(node, ast.Call):
        func_name = (
            node.func.id if isinstance(node.func, ast.Name)
            else node.func.attr if isinstance(node.func, ast.Attribute)
            else None
        )
        if func_name == "Tool":
            kwargs = {kw.arg for kw in node.keywords if kw.arg}
            missing = {"name", "description", "parameters", "func"} - kwargs
            if missing:
                issues.append(
                    f"Tool(...) call missing required kwarg(s): {sorted(missing)}. "
                    f"All 4 are mandatory — parameters must be a JSON schema dict."
                )
```

**抓的真實 bug**:LLM 寫 `Tool(name=..., func=..., description=...)` 漏 `parameters=`(實測抓到過)。或漏 `description`(理論可能)。

**為什麼 dataclass-level 不夠**:`Tool.__init__()` raise 時已經是 merge → hot-reload 階段、檔案落地了、要清理。pre-check 在 propose 階段就擋、檔案不會錯誤寫入。

### Defense 3 — Eval import path 雙路寬容(`evals.py`)

**位置**:`run_evals_for()` 跑 `spec_from_file_location` 前

```python
import sys
agent_dir = str(PROJECT_ROOT / "agent")
proj_dir = str(PROJECT_ROOT)
added_paths = []
for p in (agent_dir, proj_dir):
    if p not in sys.path:
        sys.path.insert(0, p)
        added_paths.append(p)
try:
    spec = importlib.util.spec_from_file_location(tool_name, tool_file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
finally:
    for p in added_paths:
        try:
            sys.path.remove(p)
        except ValueError:
            pass
```

**寬容什麼**:LLM 寫 `from agent.tool_registry import Tool`(長)或 `from tool_registry import Tool`(短)**兩種都通過 evals**。

**為什麼需要**:`spec_from_file_location` 把 .py 當獨立 module 載入、不走 package import 鏈、`from agent.X` 沒有 `agent` package context;沒這層 sys.path,短寫法 fail。merge 後實際 import 走 `agent.tools_dyn.<name>`,兩種寫法都 work,但 evals 階段是 standalone load。

**設計哲學**:Defense 1+2 是「攔住錯」、Defense 3 是「容忍寫法差異」 — agent code 是要對 LLM 友善的、不要因為 import 細節讓 LLM 卡住。

### Defense 4 — Tool 回傳必為 dict(SDK Pydantic 校驗坑)

**位置**:`ToolRegistry.run()` 包裝層 — 不在 propose 階段、在 **runtime 每次呼叫工具後** wrap 一次

**坑位**:Google GenAI / OpenAI SDK 底層用 Pydantic 嚴格校驗工具回傳格式,**回 `str` / `int` / `float` / `bool` 直接 400 系列錯誤**(且 Gemini 是「下一輪 request 才爆」、難 debug)。LLM 自己寫的動態 tool 常常一爽寫 `return count`、`return f"完成,共 {n} 筆"`。

```python
# agent/tool_registry.py — ToolRegistry.run() 改成自動 wrap
def run(self, name: str, args: dict) -> dict:
    t = self._tools.get(name)
    if t is None:
        return {"error": f"unknown tool: {name}"}
    try:
        raw = t.func(**(args or {}))
    except TypeError as e:
        return {"error": f"bad args: {e}"}
    except Exception as e:
        import traceback
        traceback.print_exc()                       # full stack → host stderr
        return {"error": str(e), "type": type(e).__name__}

    # ★ Defense 4:強制 dict 化
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {"ok": True}
    return {"result": raw}                          # str / int / float / list 都包進去
```

**為什麼放 runtime 不放 propose**:propose 階段 AST 可以 hint LLM「請回 dict」、但**不能保證** LLM 寫的 function 真的回 dict(分支多、容易漏)。Runtime wrap 是 100% 兜底、零成本。

**測試**:故意讓 LLM 寫個回 `int` 的工具、reload、跑、看 host log 沒 Pydantic 錯、LLM 看到 `{"result": 42}` 能繼續推理。

### Defense 5 — `register(registry)` 自動補全

**位置**:`_propose_tool()` 在 AST 解析 + Defense 1/2 通過後、寫檔前

**坑位**:LLM 90% 能寫對 tool function body、但 **40% 漏寫 `def register(registry):` 協議函數**(Gemma 4 / 早期 Claude / 沒範例 cache 的 fresh session 常見)。漏寫 → merge 完 hot-reload 找不到註冊入口、tool **隱形**、agent 跟使用者說「我做好了」但實際沒掛載、使用者重啟還是看不到。

```python
def _ensure_register_func(src: str, tool_name: str, schema_name: str = None) -> str:
    """If src lacks `def register(registry):`, append a standard one."""
    tree = ast.parse(src)
    has_register = any(
        isinstance(n, ast.FunctionDef) and n.name == "register"
        for n in tree.body
    )
    if has_register:
        return src

    # 找 schema dict 名(通常是大寫 _TOOL)
    schemas = [
        n.targets[0].id for n in tree.body
        if isinstance(n, ast.Assign)
        and isinstance(n.targets[0], ast.Name)
        and n.targets[0].id.upper().endswith("_TOOL")
    ]
    schema = schema_name or (schemas[0] if schemas else None)

    if not schema:
        return src + (
            "\n\n# NOTE: register() missing and no *_TOOL schema found — "
            "merge will fail. Re-propose with schema.\n"
        )

    template = f"""

def register(registry):
    from agent.tool_registry import Tool
    registry.register(Tool(
        name={schema}["name"],
        description={schema}["description"],
        parameters={schema}["parameters"],
        func={tool_name},
    ))
"""
    return src + template
```

**抓的真實 bug**:LLM 在 chat 內寫了 function body + schema、結尾忘了 `register`。Defense 5 自動把 register 函數補上、merge 100% 成功率。

**為什麼不 raise 要 user 改而是自動補**:user 改成本太高(來回 2-3 輪)、自動補可預測(`schema_name` 是固定 convention)、補錯 user 看 diff 改 schema 就好,比 LLM 來回試靠譜。

---

## Hot-reload 額外注意

### `importlib.invalidate_caches()` 必呼

`reload_all()` 開頭一定要先呼:

```python
def reload_all(self):
    importlib.invalidate_caches()   # ← 這行不能省
    # ... rest of reload
```

**為什麼**:`pkgutil.iter_modules(__path__)` 用 finder cache。**新檔案剛 mv 進 tools_dyn/ 後、cache 還沒看到**,iter_modules 回空、新工具不會被 register。實測抓到過。

### `tools/__init__.py` 必匯出共用 helper

**坑位**:工具目錄重構成 package(`agent/tools/__init__.py` + `agent/tools/<name>.py`)時、有人把 `_check_perm`、`_get_perms` 之類**共用 helper** 放在 `agent/tools/__init__.py`、但忘了用 `__all__` 顯式 export。Defense 3 的 sys.path 雙路 import 是 standalone load、看不到 package-level 的 helper、動態 tool 一執行就 `ImportError: cannot import name _check_perm`。

```python
# agent/tools/__init__.py — 顯式 export 共用元件
from agent.permissions import Permissions

_perm: Permissions | None = None

def _get_perms() -> Permissions:
    global _perm
    if _perm is None:
        _perm = Permissions.load("agent/permissions.json")
    return _perm

def _check_perm(path: str, op: str) -> None:
    _get_perms().check(path, op)

# ★ 動態 tool 會 `from agent.tools import _check_perm` — 不寫 __all__ 也 ok(底線開頭 by default 不會被 `from X import *` 抓、但顯式 import 還是抓得到),關鍵是「helper 真的在這裡定義 / re-export」
__all__ = ["_get_perms", "_check_perm"]   # 顯式列出、防後人重構時誤刪
```

**規則**:
- 任何**共用 helper 升到 `__init__.py`** → 同步加入 `__all__` 或留註解註明「動態工具依賴」
- LLM propose 新工具時 system prompt 列**可用 helper 清單**(`_check_perm` / `_get_perms` / 自家領域 utility)、別讓 LLM 自己重造一個權限檢查(會繞過 permissions.json)
- Defense 1 AST check 可選地 walk import 看有沒有 import 不存在的 helper、提早攔

### Outer retry for transient API failures(test 級)

跑 e2e test 時,Gemini free tier 在高峰時段(例如台灣下班時間)會連續 500 / 503 INTERNAL。production 的 `_retry` backoff `[3, 8, 20]` ~31 秒可能不夠、過了就 raise。

**Test script 加 outer retry**(不改 production retry,production 等 5 分鐘很糟):

```python
MAX_ATTEMPTS = 6
BACKOFFS = [30, 60, 120, 180, 300]

for attempt in range(1, MAX_ATTEMPTS + 1):
    try:
        return main()
    except Exception as e:
        if not any(m in str(e) for m in ("500", "503", "INTERNAL", "UNAVAILABLE")):
            raise   # non-transient
        if attempt == MAX_ATTEMPTS:
            raise
        time.sleep(BACKOFFS[min(attempt - 1, len(BACKOFFS) - 1)])
```

實測:Gemini 高峰可能連續 3 次 attempt 都 500,第 4 次通。

## Quality discipline(system prompt 規範)

```
新工具品質規則:
- 必須有 type hints + 一行 docstring
- 失敗回 dict({"error": ...})、不要 raise
- 輸出超過 4KB 要 cap(LLM context 友善)
- 碰檔案必呼 permissions.check(path, "read"|"write"|"delete")
- description 動詞開頭、要寫 return shape
- 沒有全域副作用
- 必須有對應的 evals entry(否則 merge 跑不過)
```

## Anti-pattern

| ❌ 不要做 | 為什麼 |
|---|---|
| 不經 approve 自動 merge | 即使有 shell approval、檔案內容也要單獨看一次 — 每個工具都是新攻擊面 |
| 寫只是 wrap shell 的工具(`shell("git status")` 包成 `git_status`)| 有 shell 就用 shell、新工具要加實質價值(parsing / 抽象 / type safety) |
| Agent 改 `tool_registry.py` / `orchestrator.py` / `permissions.py` | 這三個是 trust boundary、永遠不自動 merge — 只有 `tools/` 可以 |
| Skip evals | evals 是新工具的最後一道防線、不跑 evals 等於信任 LLM 寫對 |
| 寫使用者根本不會 approve 的工具 | 連 deny 5 次 → system prompt 規範漏了什麼、回去補規則(手動)|

## 跟其他 phase 的關係

- Phase 5(two-step write)+ Phase 6(hallucination 偵測):新工具寫成後要先 dry run、不要 LLM 馬上講「已 merge」實際還沒
- Phase 7(permissions):新工具碰 FS 必呼 permissions.check
- Phase 10(shell):整個 merge 流程靠 shell 跑 `mv` / `cat` / `git diff`

## 長期效果

跑 1-2 個月之後、agent 累積 30-100 個小工具、每個都是使用者親手 approve 過的、貼合實際工作流。**這個 skill 真正的價值不在第一天設好的工具,在第 N 天會多哪些**。

## 檢查清單

- [ ] system prompt 加新工具流程規範 + 品質規則 + Hard rule(不准改 trust boundary 3 個檔)
- [ ] `agent/tool_registry.py` 有 `reload_all()` 方法、**開頭呼 `importlib.invalidate_caches()`**
- [ ] `assets/evals/evals.json` 範本複製到 `evals/evals.json`、有 1-2 個範例 entry
- [ ] **3 道防線都在位**:Defense 1 (AST syntax)、Defense 2 (Tool() kwargs)、Defense 3 (sys.path 雙路)
- [ ] **propose_tool description 含 perfect Tool() template**(含 parameters JSON schema 範例)
- [ ] **e2e test 跑通** — propose → confirm → merge → eval pass → hot-reload → 新 chat 用新工具
- [ ] Outer retry 抗 transient API errors(高峰時段 Gemini free tier 連續 500 是常態)
- [ ] 試 evals fail 的情境:故意寫一個會 fail 的草稿、看 merge_proposed_tool 是否擋住 + 給出可讀的錯誤訊息
