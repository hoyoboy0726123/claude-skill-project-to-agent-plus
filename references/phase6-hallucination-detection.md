# Phase 6 — Server-side hallucination 偵測

## 真實案例(這是 LLM agent 常踩的坑)

典型對話:

```
使用者: 把我使用者偏好的 theme 改成 dark
LLM:    [tool] get_config(...)
LLM:    我看了一下、預計把 theme 從 "light" 改成 "dark"、確認嗎?
使用者: yes
LLM:    ✅ 已套用,重新開啟 app 應該就會看到 dark 主題

← 但這個 turn 內 LLM 沒呼叫 save_config(confirm=True)、什麼都沒寫
← 使用者重啟還是 light,不知道哪裡出錯
```

```
使用者: 幫我把這個 task 排程明早 9 點跑
LLM:    沒問題、我建好排程 my-task-9am、cron 0 9 * * *、明天就開始

← LLM 連 schedule_task tool 都沒呼、純編造、實際排程不存在
```

兩個都是 LLM 的「**Tool 用 confirm=False 預覽完 → 跳過 confirm=True 直接宣稱已做**」。**這個錯不能靠 prompt 規範擋死、必須 server-side 程式驗證**。

## 偵測規則

每輪 reply(orchestrator 拿到 LLM 最終 text 後、回給使用者前)跑兩個檢查:

1. **claim word match**:reply 內含「已套用 / 已寫入 / 已改好 / 已建立 / 已排程 / 已啟動 / 套用完成 / 改好了 / 完成 / 已執行」之一
2. **tool_calls 掃描**:這 turn 所有 LLM 訊息的 tool_calls 中、**沒有一個** `confirm=True` 的

兩個全中 → 偽造。

> 若你的設計另外有「READY marker」機制(LLM emit 一個結構化區塊讓前端 / 系統處理寫入、而不靠 confirm=True),可加第三個檢查:reply 沒含預定 marker 才算違規。

## 實作範本

```python
# orchestrator.py(或 main agent loop 內)

_CLAIM_PATTERNS = (
    "已套用", "已寫入", "已改好", "已建立", "已排程",
    "已啟動", "已執行", "已刪除", "套用完成", "改好了", "完成寫入",
    "我已套用", "我已建立", "我已加",
)

def detect_hallucinated_write(reply: str, lc_messages: list) -> bool:
    """LLM reply 宣稱已寫、但實際沒呼 confirm=True 任何 tool → True"""
    claimed = any(p in reply for p in _CLAIM_PATTERNS)
    if not claimed:
        return False
    # scan this turn's tool calls
    for msg in lc_messages:
        tcs = getattr(msg, "tool_calls", None) or []
        for tc in tcs:
            args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
            if (args or {}).get("confirm") is True:
                return False
    return True


# 在 agent loop 拿到最終 reply 之後:
if detect_hallucinated_write(reply, lc_messages):
    logger.warning("[agent] LLM claimed write but no confirm=True tool call")
    reply = (
        "⚠️ 我剛剛口頭說已執行、但**實際上沒真的呼叫工具**(系統自動偵測)。\n"
        "請再跟我說一次「請執行」、我會重新跑工具真正執行。\n\n"
        "(原回覆:)\n" + reply
    )
```

## 文案分通道(如果未來擴展)

```python
if channel == "telegram":
    hint = "請再說一次「請執行」、我會重新跑工具真正執行。"
elif channel == "desktop_web":
    hint = "請重新請我處理、會出現確認按鈕、點下去才會真寫入。"
else:
    hint = "請重新請我執行、確保看到工具實際呼叫的進度。"

reply = f"⚠️ 我剛剛口頭說已執行、但實際上沒真的呼叫工具(系統自動偵測)。{hint}\n\n(原回覆:)\n{reply}"
```

## 跟 Phase 5 的關係

- Phase 5 是**規範**(LLM 要走兩步)
- Phase 6 是**驗證**(LLM 違規時 server 偵測 + 反饋)

兩個一起才有效。只有 Phase 5、LLM 違規時使用者只看到「已套用」字樣、會以為真寫了。只有 Phase 6 但 prompt 沒規範,LLM 一輪內 confirm=True 直接寫也是違規,但 Phase 6 不會偵測到(那場景沒幻覺、是不該寫但 user 想要才會寫)。

## 系統 prompt 規範片段(複製進去)

```
## Hallucination 防線(看完之後不要碰運氣)

口頭講「已套用 / 已寫入 / 已建立 / 已排程」、但這個 turn 內你沒呼叫過 confirm=True
版本的工具、也沒 emit ready marker → 系統會自動偵測並在我的 reply 前綴一段
警告告訴使用者「LLM 騙人、實際沒寫」、你會被記一筆違規。

正確流程:寫了就確實呼工具、沒寫就**不要**講「已套用」。
不確定的話講「我剛剛嘗試,但工具沒回成功、可能還沒寫成功、再試一次?」
這比假裝成功好。
```

## 檢查清單

- [ ] orchestrator 內加 `detect_hallucinated_write()` 函式
- [ ] agent loop return reply 前跑這個檢查、偽造就 prefix 警告
- [ ] system prompt 加 「Hallucination 防線」段落
- [ ] log 一行 warning 方便事後 trace
- [ ] 跟使用者一起測:故意叫 LLM「假裝已套用」、看防線會不會出來
