# Phase 5 — Two-step write 協議

> 📌 Phase 10b 開啟基礎工具集後,`write_file` / `edit_file` 也照同樣的 two-step 規範跑(`confirm=False` → preview;`confirm=True` → 真寫)。`update_settings` 跟 `git_commit_and_push` 也是。**所有「會改變外部世界」的工具一律 two-step、不只是專案 wrap 那批**。

## 為什麼這個 phase 不能跳過

LLM 工具呼叫的「寫」操作(寫檔、發訊息、改遠端狀態、刪除東西)如果一次就執行、使用者沒辦法在動作生效前看到 preview。實作上 LLM 會在很多場景下不該寫卻寫了:

- 使用者問「**如果**我這樣改設定會怎樣」 → LLM 直接改了
- 使用者打「OK」想看下一步建議 → LLM 解讀成確認寫入
- LLM 自己幻覺出一份內容、沒問就提交

**沒這層,寫工具就是定時炸彈。**

## 規範

**任何「寫」工具必須有 `confirm: bool = False` 參數**。

### LLM 流程(寫進 system prompt)

```
規則:任何會「寫到外部世界」的 tool call,第一輪只能用 confirm=False。

confirm=False → 拿到 preview + 副作用摘要 + diff(如有)
→ 用文字告訴使用者「我打算做 X、預覽是 Y、Z 個檔案會變、確認嗎?」
→ 等使用者回 yes/好/確認/套用
→ 重新呼叫 SAME tool 配 confirm=True 真正寫入
→ 回報結果

不准的事:
- 沒先 confirm=False 就直接 confirm=True
- 使用者只說「OK 你解釋的對」就當成寫入確認
- 一輪內同時 confirm=False + confirm=True 兩次呼叫
- 講「我已套用」但這 turn 沒任何 confirm=True tool call(這是 Phase 6 偵測的違規)
```

### 工具實作範本

```python
@register
def save_user_config(
    key: str,
    value: str,
    confirm: bool = False,
) -> dict:
    """寫使用者偏好到 ~/.myapp/config.json。

    Args:
        key: 設定 key
        value: 新值
        confirm: False 預覽, True 寫入

    Returns:
        confirm=False: {"preview": {old, new, file}, "needs_confirmation": True}
        confirm=True:  {"ok": True, "wrote": file}
    """
    permissions.check(CONFIG_FILE, "write")
    config = load_config()
    old = config.get(key)

    if not confirm:
        return {
            "preview": {"file": str(CONFIG_FILE), "key": key, "old": old, "new": value},
            "needs_confirmation": True,
            "diff": f"{key}: {old!r} → {value!r}",
        }

    config[key] = value
    save_config(config)
    return {"ok": True, "wrote": str(CONFIG_FILE), "key": key, "value": value}
```

### Orchestrator 端的補強

不必硬擋第二輪 confirm=True(LLM 自己會走兩步),但 system prompt 要說「TG 通道沒有按鈕、確認時 LLM 自己 emit 確認文字、等使用者再說 yes 才呼第二次」。

### 哪些 tool 算「寫」

| 算寫 | 不算寫 |
|---|---|
| `save_config()` | `read_config()` |
| `send_message(to, text)` | `list_chats()` |
| `delete_file(path)` | `stat_file(path)` |
| `start_job(query)` | `list_jobs()` |
| `update_record(id, data)` | `get_record(id)` |
| `create_file(path, content)` | `read_file(path)` |
| `run_shell(cmd)` *(永遠走 Phase 10 inline button、不靠這個 confirm)* | — |

**判準**:`tool(confirm=True)` 跑完世界有沒有不同?有 → 算寫。

## 系統 prompt 規範片段(複製進去)

```
## 寫操作協議(必讀)

任何會改外部世界的 tool 都有 `confirm: bool` 參數。

預設 confirm=False、做這四步:
1. 呼叫 tool 拿 preview
2. 用文字告訴使用者「我打算 X、影響 Y、確認?」
3. 等使用者明確說 yes / 好 / 確認 / 套用 / OK 寫
4. 收到確認 → 呼叫 SAME tool 配 confirm=True

最高優先級違規:沒第一輪 confirm=False、直接 confirm=True 寫了。
也是違規:口頭講「已套用」但這 turn 沒任何 confirm=True call(Phase 6 會自動偵測 + 警告使用者)。
```

## 跟其他 phase 的關係

- Phase 6 是這個協議的**驗證層** — LLM 違規時 orchestrator 自動偵測 + reply 前綴警告
- Phase 10(Shell)寫 code / 跑 command 走自己的 inline button approval、**不**走 confirm 機制
- Phase 12(Self-evolution)merge 新工具走 TG inline button、也**不**走 confirm

## 檢查清單

- [ ] 每個寫工具都有 `confirm: bool = False` 參數
- [ ] confirm=False 路徑回傳 `needs_confirmation: True` + preview / diff
- [ ] System prompt 加上「寫操作協議」段落
- [ ] 跟使用者一起跑一次 dry run:讓 LLM 用 confirm=False 預覽 → 確認 → confirm=True 寫
