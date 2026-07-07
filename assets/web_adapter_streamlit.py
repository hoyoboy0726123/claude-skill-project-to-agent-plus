"""Streamlit web adapter v2 — multi-conversation + sqlite persistence.

v2 changes:
  Q1 ✓ st.chat_input(accept_file="multiple") — native upload, no sidebar uploader
  Q2 ✓ tools shown in expander — click to reveal all 24 names+descriptions
  Q3 ✓ multi-conversation threads in sidebar (ChatGPT-style), sqlite-backed
  Q4 ✓ sqlite-backed history persists across restarts (Phase 14 will add
       facts/episodes tables to same db for true long-term memory)

Run:  streamlit run agent/web_adapter_streamlit.py
"""
from __future__ import annotations

import json
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()

from agent import orchestrator_factory                                   # noqa: E402
from agent.chat_store import ChatStore, auto_title_from_first_message    # noqa: E402


# ─── Page setup ──────────────────────────────────────────────
st.set_page_config(
    page_title="Agent · Local Console",
    page_icon="🤖",
    layout="wide",
)
st.markdown(
    """
    <style>
      #MainMenu, footer, header[data-testid="stHeader"] { visibility: hidden; }
      .stApp > header { display: none !important; }
      [data-testid="stChatMessage"] { padding: 0.5em 1em; }
      .tool-progress {
        background: linear-gradient(90deg, #f5f7ff 0%, #faf0ff 100%);
        border-left: 3px solid #5567d8;
        padding: 0.4em 0.8em; border-radius: 6px;
        font-size: 0.85em; color: #4a5568;
        margin: 0.3em 0;
      }
      [data-testid="stSidebar"] .stButton button {
        text-align: left; justify-content: flex-start;
      }
    </style>
    """,
    unsafe_allow_html=True,
)


# ─── Singletons ──────────────────────────────────────────────
@st.cache_resource
def get_store() -> ChatStore:
    return ChatStore()


_PROGRESS_ZH = {
    "fetch_url": "🌐 抓網頁", "list_subfolders": "📂 列子資料夾",
    "search_vault": "🔍 搜尋 vault", "read_note": "📄 讀筆記",
    "git_status": "🔧 查 git 狀態", "classify_content": "🤖 LLM 分類",
    "write_note": "✍️ 寫筆記", "update_settings": "⚙️ 更新設定",
    "git_commit_and_push": "🚀 git push", "current_state": "🧭 查狀態",
    "read_file": "📖 讀檔", "write_file": "✍️ 寫檔", "edit_file": "🔧 改檔",
    "glob_paths": "🗂 列檔", "grep_files": "🔎 搜尋全檔",
    "view_image": "👀 看圖", "ask_user": "❓ 問你", "done": "✅ 完成",
    "run_shell": "🐳 跑 shell", "run_python": "🐍 跑 Python",
    "web_search": "🌍 網路搜尋",
    "list_proposed_tools": "📜 列待審工具", "propose_tool": "🧬 草稿新工具",
    "merge_proposed_tool": "🔀 合併新工具", "reject_proposed_tool": "🚫 棄稿",
}


def _format_tool_progress(name: str, args: dict) -> str:
    zh = _PROGRESS_ZH.get(name, f"▸ {name}")
    if not args:
        return zh
    preview = {k: (str(v)[:40] + "..." if len(str(v)) > 40 else v)
                for k, v in args.items()}
    return f"{zh}({', '.join(f'{k}={v}' for k, v in preview.items())})"


def _extract_files(tool_result_str: Any) -> list[str]:
    try:
        d = (json.loads(tool_result_str)
             if isinstance(tool_result_str, str) else tool_result_str)
    except Exception:
        return []
    if not isinstance(d, dict):
        return []
    keys = ("output_file", "saved_path", "path", "file", "written_to")
    out = [d[k] for k in keys if isinstance(d.get(k), str) and d.get(k)]
    for lk in ("output_files", "files"):
        v = d.get(lk)
        if isinstance(v, list):
            out.extend(x for x in v if isinstance(x, str))
    return out


def _safe_file_render(fp: str, key_suffix: str = ""):
    p = Path(fp)
    if not p.exists() or not p.is_file():
        return
    suffix = p.suffix.lower()
    try:
        if suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            st.image(str(p), caption=p.name)
        elif suffix in (".md", ".txt"):
            with st.expander(f"📄 {p.name}", expanded=False):
                st.markdown(p.read_text(encoding="utf-8", errors="ignore"))
        else:
            st.download_button(
                label=f"⬇ {p.name}",
                data=p.read_bytes(),
                file_name=p.name,
                key=f"dl_{fp}_{p.stat().st_mtime}_{key_suffix}",
            )
    except Exception as e:
        st.caption(f"(couldn't render {p.name}: {e})")


# ─── Conversation state ──────────────────────────────────────
def _load_or_create_active_conv(store: ChatStore) -> str:
    convs = store.list_conversations(limit=1)
    if convs:
        return convs[0]["id"]
    conv_id = str(uuid.uuid4())[:8]
    store.create_conversation(conv_id, title="新對話")
    return conv_id


def _ensure_session():
    store = get_store()
    if "active_conv_id" not in st.session_state:
        st.session_state.active_conv_id = _load_or_create_active_conv(store)
    if "orchs" not in st.session_state:
        st.session_state.orchs = {}
    _ensure_orch_for(st.session_state.active_conv_id)


def _ensure_orch_for(conv_id: str):
    if conv_id in st.session_state.orchs:
        return
    orch = orchestrator_factory(channel="web")
    stored = get_store().get_orchestrator_messages(conv_id)
    if stored:
        orch.messages.extend(stored)
        orch._trim_history()
    st.session_state.orchs[conv_id] = orch


def _new_conversation():
    conv_id = str(uuid.uuid4())[:8]
    get_store().create_conversation(conv_id, title="新對話")
    st.session_state.active_conv_id = conv_id
    _ensure_orch_for(conv_id)


def _switch_conversation(conv_id: str):
    st.session_state.active_conv_id = conv_id
    _ensure_orch_for(conv_id)


def _delete_conversation(conv_id: str):
    get_store().delete_conversation(conv_id)
    st.session_state.orchs.pop(conv_id, None)
    if st.session_state.active_conv_id == conv_id:
        convs = get_store().list_conversations(limit=1)
        if convs:
            st.session_state.active_conv_id = convs[0]["id"]
        else:
            _new_conversation()
        _ensure_orch_for(st.session_state.active_conv_id)


_ensure_session()
store = get_store()
active_id = st.session_state.active_conv_id
active_orch = st.session_state.orchs[active_id]


def _friendly_error(err: str) -> tuple[str, str]:
    """Map raw exception text to (icon, friendly_zh_message).
    Designed to read naturally to non-technical users."""
    e = err.lower()
    if "500" in e or "internal error" in e or "internal." in e:
        return (
            "⏳",
            "Gemini 伺服器目前過載(內建 3 次重試仍失敗)。\n"
            "通常 30 秒到 2 分鐘會恢復、麻煩等一下再重新發送您的問題。",
        )
    if "503" in e or "unavailable" in e:
        return (
            "⏳",
            "Gemini 暫時無法服務、可能高峰時段。\n"
            "等 1-2 分鐘後請再重新發送您的問題。",
        )
    if "429" in e or "rate" in e or "quota" in e:
        return (
            "🚦",
            "Gemini 免費額度暫時用滿、預計 60 秒後恢復。\n"
            "等一下再重新發送您的問題;若常發生可考慮升級付費 tier。",
        )
    if "400" in e or "invalid_argument" in e:
        return (
            "⚠️",
            "對話內容格式有問題(常見:圖片跟前一輪工具呼叫不相容)。\n"
            "建議「➕ 新對話」開新一條重試,可避免歷史殘留干擾。",
        )
    if "401" in e or "403" in e or "permission" in e or "api_key" in e:
        return (
            "🔑",
            "API 金鑰問題(無效或被吊銷)。請檢查 .env 內的 GEMINI_API_KEY。",
        )
    if "timeout" in e or "timed out" in e:
        return (
            "🐢",
            "API 回應超時、可能網路慢或請求太大。\n"
            "建議:檢查網路、縮短訊息、不附過大檔案,再重新發送。",
        )
    # default
    return (
        "⚠️",
        f"Agent 遇到未預期錯誤,請等一下再重新發送您的問題。\n\n技術細節(供 debug):\n```\n{err[:400]}\n```",
    )


# ─── Sidebar ─────────────────────────────────────────────────
with st.sidebar:
    st.title("🤖 Agent Console")
    st.caption("本機運行 · 對話資料不出本機")

    if st.button("➕ 新對話", use_container_width=True, type="primary"):
        _new_conversation()
        st.rerun()

    st.divider()

    st.markdown("**💬 對話**")
    convs = store.list_conversations(limit=50)
    for c in convs:
        is_active = c["id"] == active_id
        prefix = "▶ " if is_active else "   "
        title = c["title"]
        col_btn, col_del = st.columns([5, 1])
        with col_btn:
            if st.button(
                f"{prefix}{title}",
                key=f"conv_{c['id']}",
                use_container_width=True,
                type="secondary" if not is_active else "primary",
            ):
                if not is_active:
                    _switch_conversation(c["id"])
                    st.rerun()
        with col_del:
            if st.button("✕", key=f"del_{c['id']}",
                          help="刪除這個對話"):
                _delete_conversation(c["id"])
                st.rerun()

    st.divider()

    try:
        registry = active_orch.registry
        names = registry.names()
        schemas = registry.schemas()
        with st.expander(f"🔧 已註冊工具:{len(names)}", expanded=False):
            for sc in schemas:
                desc = (sc.get("description") or "").split("\n")[0]
                st.markdown(f"**`{sc['name']}`** — {desc[:80]}")
    except Exception as e:
        st.caption(f"(無法取得工具清單: {e})")

    msg_count = store.message_count(active_id)
    st.caption(f"💬 當前對話訊息:{msg_count}")
    if store.db_path.exists():
        db_size = store.db_path.stat().st_size
        st.caption(f"💾 chats.db:{db_size // 1024} KB")

    st.divider()

    # ── Provider + Model picker ──
    st.markdown("**🧠 LLM Provider**")

    from agent import detect_available_providers, get_llm, rebuild_llm

    @st.cache_data(ttl=60)
    def _cached_providers():
        return detect_available_providers()

    providers = _cached_providers()
    current_llm = get_llm()
    current_provider_class = type(current_llm).__name__
    provider_class_map = {
        "GeminiClient": "gemini",
        "OpenAIClient": "openai",
        "AnthropicClient": "anthropic",
        "GroqClient": "groq",
        "OllamaClient": "ollama",
    }
    current_provider = provider_class_map.get(current_provider_class, "gemini")

    # Build radio options labelled with availability
    provider_order = ["gemini", "openai", "anthropic", "groq", "ollama"]
    labels = []
    enabled = []
    for p in provider_order:
        info = providers.get(p, {"available": False, "reason": "?"})
        emoji = "✓" if info["available"] else "✗"
        labels.append(f"{emoji} {p}")
        enabled.append(info["available"])

    try:
        cur_idx = provider_order.index(current_provider)
    except ValueError:
        cur_idx = 0

    picked_label = st.radio(
        "Provider",
        options=labels,
        index=cur_idx,
        label_visibility="collapsed",
        horizontal=False,
    )
    picked_provider = provider_order[labels.index(picked_label)]
    picked_info = providers[picked_provider]

    if not picked_info["available"]:
        st.caption(f"⚠ {picked_info['reason']}")
        if picked_provider == "ollama":
            with st.expander("📦 怎麼裝 Ollama", expanded=False):
                st.markdown(
                    "1. `winget install ollama.ollama`(Windows)\n"
                    "   或 `brew install ollama`(Mac)\n"
                    "2. 重開 terminal、跑 `ollama serve`(背景跑)\n"
                    "3. 抓 model:`ollama pull qwen2.5:7b`(或 `llama3.1:8b`)\n"
                    "4. 重新整理這頁,sidebar 會自動偵測到"
                )
        else:
            with st.expander("🔑 怎麼設 API key", expanded=False):
                st.markdown(
                    f"1. 申請 {picked_provider.upper()} API key\n"
                    f"2. 編輯 `.env` 加 `{picked_provider.upper()}_API_KEY=sk-...`\n"
                    f"3. 重啟 streamlit"
                )
    elif picked_provider != current_provider:
        if st.button(f"🔄 切到 {picked_provider}", type="primary",
                      use_container_width=True):
            try:
                rebuild_llm(picked_provider)
                # Also rebuild active orchestrator so it uses new LLM
                st.session_state.orchs.pop(active_id, None)
                _ensure_orch_for(active_id)
                # Clear cached model list (different provider, different models)
                st.cache_data.clear()
                st.success(f"✓ 切到 {picked_provider}")
                st.rerun()
            except Exception as e:
                st.error(f"切換失敗:{e}")
    else:
        st.caption(f"✓ {picked_info['reason']}")

    st.markdown("**🧠 模型**")

    @st.cache_data(ttl=300)
    def _cached_model_list(_provider_hint: str):
        """Cache model list for 5 min. _provider_hint forces re-fetch on switch."""
        llm = get_llm()
        if not hasattr(llm, "list_models"):
            return None
        try:
            return llm.list_models()
        except Exception as e:
            return [{"error": str(e)}]

    models = _cached_model_list(current_provider)
    if models is None:
        st.caption("(此 provider 不支援 model 切換)")
    elif models and "error" in models[0]:
        st.caption(f"⚠ {models[0]['error'][:120]}")
    else:
        current = current_llm.default_model

        def _rank(m):
            n = m["name"]
            return (not m.get("supports_caching", False),
                    "preview" in n, "lite" in n, n)

        sorted_models = sorted(models, key=_rank)
        names = [m["name"] for m in sorted_models]

        def _label(m):
            n = m["name"]
            tags = []
            if m.get("supports_caching"):
                tags.append("⚡")
            ps = m.get("parameter_size")
            if ps:
                tags.append(ps)
            return f"{n}  {' · '.join(tags)}" if tags else n

        labels = [_label(m) for m in sorted_models]

        try:
            idx = names.index(current)
        except ValueError:
            idx = 0
        picked_label = st.selectbox(
            "選擇模型",
            options=labels,
            index=idx,
            label_visibility="collapsed",
        )
        picked = names[labels.index(picked_label)]
        if picked != current:
            current_llm.switch_model(picked)
            st.success(f"✓ 切到 `{picked}`")
            st.rerun()

        st.caption(f"目前:`{current}`")


# ─── Replay history for active conv ──────────────────────────
def _render_history():
    rows = store.get_messages(active_id)
    for r in rows:
        role = r["role"]
        if role == "tool":
            continue
        content = r.get("content", "")
        extras = r.get("extras") or {}
        with st.chat_message("user" if role == "user" else "assistant"):
            if role == "user" and extras.get("attachments"):
                for att in extras["attachments"]:
                    p = Path(att["path"])
                    if att.get("mime", "").startswith("image/") and p.exists():
                        st.image(str(p), width=200, caption=att.get("name", p.name))
                    else:
                        st.caption(f"📎 {att.get('name', p.name)}")
            st.markdown(content)
            for prog in extras.get("tool_progress", []):
                st.markdown(f"<div class='tool-progress'>{prog}</div>",
                             unsafe_allow_html=True)
            for fp in extras.get("files", []):
                _safe_file_render(fp, key_suffix=f"hist_{r['id']}")


_render_history()


# ─── Streaming orchestrator runner ──────────────────────────
def _run_orchestrator(user_text: str, attachments: list[dict]):
    orch = active_orch
    orch.add_user(user_text, attachments=attachments)
    store.add_message(active_id, "user", user_text,
                       extras={"attachments": attachments} if attachments else None)

    conv = store.get_conversation(active_id)
    if conv and conv["title"] == "新對話":
        store.rename_conversation(active_id, auto_title_from_first_message(user_text))

    text_placeholder = st.empty()
    progress_placeholder = st.empty()
    files_placeholder = st.empty()

    final_text_parts: list[str] = []
    tool_progress_lines: list[str] = []
    produced_files: list[str] = []
    turn_tool_calls: list[dict] = []

    turn_failed = False
    error_text = ""
    accumulated_stream = ""   # live token-by-token buffer
    with st.status("agent 思考中…", expanded=True) as status:
        try:
            for msg in orch.step_stream():
                role = msg.get("role")
                if role == "assistant_chunk":
                    # ★ True token-by-token streaming
                    accumulated_stream += msg.get("content", "")
                    text_placeholder.markdown(accumulated_stream + "▌")
                elif role == "assistant":
                    tcs = msg.get("tool_calls") or []
                    if tcs:
                        # PERSIST THIS ITERATION'S assistant turn IMMEDIATELY
                        # so DB order is: assistant(tool_calls) → tool → ...
                        # (not: tool → tool → final_assistant which breaks Gemini)
                        iter_tool_calls = []
                        for tc in tcs:
                            name = tc.name if hasattr(tc, "name") else tc.get("name")
                            args = tc.args if hasattr(tc, "args") else tc.get("args", {})
                            args_d = dict(args or {})
                            iter_tool_calls.append({"name": name, "args": args_d})
                            line = _format_tool_progress(name, args_d)
                            tool_progress_lines.append(line)
                            turn_tool_calls.append({"name": name, "args": args_d})
                            progress_placeholder.markdown(
                                "\n".join(
                                    f"<div class='tool-progress'>{l}</div>"
                                    for l in tool_progress_lines
                                ),
                                unsafe_allow_html=True,
                            )
                            status.update(label=line)
                        # Persist NOW — assistant_with_tool_calls then tool
                        # results that follow will be in correct order
                        store.add_message(
                            active_id, "assistant",
                            msg.get("content") or "",
                            extras={"tool_calls": iter_tool_calls},
                        )
                        accumulated_stream = ""
                    elif msg.get("content"):
                        # Final text — also persist (separate row from tool-call rounds)
                        full = msg["content"]
                        final_text_parts.append(full)
                        text_placeholder.markdown("\n\n".join(final_text_parts))
                        accumulated_stream = ""
                        store.add_message(
                            active_id, "assistant", full,
                            extras={},
                        )
                elif role == "tool":
                    produced_files.extend(_extract_files(msg.get("content", "")))
                    store.add_message(
                        active_id, "tool",
                        msg.get("content", ""),
                        extras={"tool_name": msg.get("tool_name", "")},
                    )
            status.update(label="✅ 完成", state="complete", expanded=False)
        except Exception as e:
            turn_failed = True
            error_text = str(e)
            # Phase 4 rule: full traceback to host stderr so the operator can
            # debug. The friendly message goes to the user; the stack trace
            # goes to the terminal that launched streamlit.
            print(f"\n[web_adapter] turn failed: {type(e).__name__}: {e}",
                  file=sys.stderr)
            traceback.print_exc()
            icon, friendly = _friendly_error(error_text)
            # Collapse the status widget — don't shove raw error in user's face
            status.update(
                label=f"{icon} 暫時無法回應", state="error", expanded=False,
            )
            # Render friendly message in the chat bubble itself
            text_placeholder.markdown(f"{icon}  {friendly}")

    with files_placeholder.container():
        for fp in produced_files:
            _safe_file_render(fp, key_suffix="latest")

    final_text = "\n\n".join(final_text_parts) if final_text_parts else "(無回應)"
    if not turn_failed:
        # assistant messages were already persisted per-iteration above.
        # Just associate progress + files metadata with the LAST assistant
        # message (for display replay).
        if final_text_parts:
            try:
                last_id = store.db.execute(
                    "SELECT id FROM messages WHERE conv_id=? AND role='assistant' "
                    "ORDER BY id DESC LIMIT 1", (active_id,),
                ).fetchone()
                if last_id:
                    store.db.execute(
                        "UPDATE messages SET extras_json=? WHERE id=?",
                        (json.dumps({
                            "tool_progress": tool_progress_lines,
                            "files": produced_files,
                        }, ensure_ascii=False), last_id["id"]),
                    )
                    store.db.commit()
            except Exception:
                pass
    else:
        # Failed turn handling:
        # - User message: KEEP in sqlite + history (user can re-issue same text
        #   without re-typing; orchestrator next turn won't double-add it
        #   because we only pop the trailing user msg before next add_user).
        # - Assistant turn: persist friendly error so it shows on reload.
        # - Orchestrator in-memory: pop the trailing user message so the next
        #   user send isn't treated as a 2nd consecutive user turn.
        icon, friendly = _friendly_error(error_text)
        store.add_message(
            active_id, "assistant",
            f"{icon}  {friendly}",
            extras={"is_error": True},
        )
        if orch.messages and orch.messages[-1].get("role") == "user":
            orch.messages.pop()


# ─── Native chat_input with file integration (Q1) ───────────
user_input = st.chat_input(
    "輸入訊息(可點 📎 附檔)…",
    accept_file="multiple",
    file_type=["png", "jpg", "jpeg", "gif", "webp",
                "pdf", "txt", "md", "json", "csv"],
)

if user_input:
    text = (user_input.text or "").strip()
    raw_files = user_input.files or []

    attachments: list[dict] = []
    if raw_files:
        cache_dir = Path.home() / ".cache" / "agent-web" / "uploads"
        cache_dir.mkdir(parents=True, exist_ok=True)
        for f in raw_files:
            target = cache_dir / f.name
            target.write_bytes(f.getbuffer())
            attachments.append({
                "path": str(target),
                "mime": f.type or "application/octet-stream",
                "name": f.name,
            })

    with st.chat_message("user"):
        for att in attachments:
            p = Path(att["path"])
            if att["mime"].startswith("image/") and p.exists():
                st.image(str(p), width=200, caption=att["name"])
            else:
                st.caption(f"📎 {att['name']}")
        st.markdown(text or "(只附了檔案、沒打字)")

    with st.chat_message("assistant"):
        _run_orchestrator(text or "(使用者只附了檔、請看看)", attachments)
