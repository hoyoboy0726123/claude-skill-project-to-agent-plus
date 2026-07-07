"""File-system tools — host-side, permission-checked, two-step on writes.

Skill template. Drop into your project as agent/file_tools.py. Pairs with
agent/tools.py via `register_all()` (Phase 10b).

Once Phase 10 sandbox is up, the LLM benefits enormously from generic file
ops on top of project-specific tools. This module covers read_file /
write_file / edit_file / glob_paths / grep_files / view_image (multi-modal,
works with Gemma 4 / GPT-4o / Claude 3+ vision).

All tools route through agent.permissions.Permissions.check() to enforce
the user-approved folder ACL (Phase 7). write_file and edit_file follow
the Phase 5 two-step protocol (confirm=False → preview, confirm=True → write).

All five tools go through `agent.permissions.Permissions.check()`. write_file
and edit_file follow the Phase 5 two-step protocol (confirm=False → preview,
confirm=True → actually write).
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def _truncate(s: str, cap: int) -> str:
    if len(s) <= cap:
        return s
    return s[:cap] + f"\n... [truncated, total {len(s)} chars]"


def _resolve(path: str) -> Path:
    return Path(path).expanduser().resolve()


# ─────────────────────────────────────────────────────────────
# read_file
# ─────────────────────────────────────────────────────────────
def read_file(path: str, max_bytes: int = 64000, encoding: str = "utf-8") -> dict:
    from agent.tools import _check  # avoid circular at import time
    p = _resolve(path)
    err = _check(p, "read")
    if err:
        return err
    if not p.exists():
        return {"error": f"file not found: {path}"}
    if not p.is_file():
        return {"error": f"not a regular file: {path}"}
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            raw = f.read(max_bytes)
        # Try text decode first; fall back to base64-ish summary
        try:
            content = raw.decode(encoding)
            is_binary = False
        except UnicodeDecodeError:
            content = f"(binary, {size} bytes, first 200 hex)\n" + raw[:200].hex()
            is_binary = True
        return {
            "path": str(p),
            "size_bytes": size,
            "is_binary": is_binary,
            "content": content,
            "truncated": size > max_bytes and not is_binary,
            "bytes_read": min(size, max_bytes),
        }
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────
# write_file (two-step)
# ─────────────────────────────────────────────────────────────
def write_file(path: str, content: str, confirm: bool = False,
               encoding: str = "utf-8") -> dict:
    from agent.tools import _check
    p = _resolve(path)
    err = _check(p, "write")
    if err:
        return err

    will_overwrite = p.exists()
    if not confirm:
        existing_head = ""
        if will_overwrite and p.is_file():
            try:
                existing_head = p.read_text(encoding=encoding)[:300]
            except Exception:
                existing_head = "(unable to read existing content for diff preview)"
        return {
            "confirm_required": True,
            "would_write_to": str(p),
            "will_overwrite": will_overwrite,
            "existing_first_300": existing_head if will_overwrite else None,
            "new_first_300": content[:300],
            "new_total_chars": len(content),
            "next_step": "User confirmation → call write_file(..., confirm=True).",
        }

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding=encoding)
        return {
            "written_to": str(p),
            "size_bytes": p.stat().st_size,
            "overwrote": will_overwrite,
        }
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────
# edit_file (two-step) — string replace
# ─────────────────────────────────────────────────────────────
def edit_file(path: str, old: str, new: str, confirm: bool = False,
              replace_all: bool = False, encoding: str = "utf-8") -> dict:
    from agent.tools import _check
    p = _resolve(path)
    err = _check(p, "write")
    if err:
        return err
    if not p.exists() or not p.is_file():
        return {"error": f"file not found: {path}"}
    try:
        original = p.read_text(encoding=encoding)
    except Exception as e:
        return {"error": f"cannot read for edit: {e}"}

    occurrences = original.count(old)
    if occurrences == 0:
        return {"error": "old string not found in file", "path": str(p)}
    if occurrences > 1 and not replace_all:
        return {
            "error": (
                f"old string occurs {occurrences} times; set replace_all=True "
                "or give a longer `old` that uniquely matches one location."
            ),
            "occurrences": occurrences,
        }

    new_content = original.replace(old, new) if replace_all else original.replace(old, new, 1)

    if not confirm:
        # show a small diff snippet around first occurrence
        idx = original.find(old)
        ctx_before = original[max(0, idx - 60):idx]
        ctx_after = original[idx + len(old):idx + len(old) + 60]
        return {
            "confirm_required": True,
            "path": str(p),
            "occurrences": occurrences,
            "replace_all": replace_all,
            "diff_preview": (
                f"...{ctx_before}\n"
                f"- {old}\n"
                f"+ {new}\n"
                f"{ctx_after}..."
            ),
            "size_delta_bytes": len(new_content) - len(original),
            "next_step": "User confirmation → call edit_file(..., confirm=True).",
        }

    try:
        p.write_text(new_content, encoding=encoding)
        return {
            "edited": str(p),
            "occurrences_replaced": occurrences if replace_all else 1,
            "new_size_bytes": p.stat().st_size,
        }
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────
# glob_paths
# ─────────────────────────────────────────────────────────────
def glob_paths(pattern: str, root: str = ".", max_results: int = 200) -> dict:
    """Glob like rg/find. Pattern is relative to `root`. Examples:
       glob_paths('**/*.md', root='/path/to/vault')
       glob_paths('modules/*.py')
    """
    from agent.tools import _check
    root_p = _resolve(root)
    err = _check(root_p, "read")
    if err:
        return err
    if not root_p.exists():
        return {"error": f"root not found: {root}"}

    try:
        matches = []
        for hit in root_p.rglob(pattern) if "**" in pattern else root_p.glob(pattern):
            if len(matches) >= max_results:
                break
            matches.append(str(hit))
        return {
            "pattern": pattern,
            "root": str(root_p),
            "count": len(matches),
            "matches": matches,
            "truncated": len(matches) == max_results,
        }
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────
# grep_files
# ─────────────────────────────────────────────────────────────
def grep_files(pattern: str, path: str = ".",
               file_glob: str = "**/*",
               max_results: int = 100,
               case_insensitive: bool = False) -> dict:
    """Plain text regex search across files under `path`. Returns hit list."""
    from agent.tools import _check
    root_p = _resolve(path)
    err = _check(root_p, "read")
    if err:
        return err
    try:
        flags = re.IGNORECASE if case_insensitive else 0
        regex = re.compile(pattern, flags)
    except re.error as e:
        return {"error": f"bad regex: {e}"}

    hits = []
    files_scanned = 0
    for fp in (root_p.rglob(file_glob) if "**" in file_glob else root_p.glob(file_glob)):
        if not fp.is_file():
            continue
        files_scanned += 1
        try:
            for lineno, line in enumerate(fp.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if regex.search(line):
                    hits.append({
                        "file": str(fp),
                        "line": lineno,
                        "text": line[:200],
                    })
                    if len(hits) >= max_results:
                        break
        except Exception:
            continue
        if len(hits) >= max_results:
            break

    return {
        "pattern": pattern,
        "path": str(root_p),
        "file_glob": file_glob,
        "files_scanned": files_scanned,
        "hit_count": len(hits),
        "hits": hits,
        "truncated": len(hits) == max_results,
    }


# ─────────────────────────────────────────────────────────────
# Tool schemas
# ─────────────────────────────────────────────────────────────
def read_file_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or ~-prefixed path."},
            "max_bytes": {"type": "integer", "description": "Cap (default 64000)."},
            "encoding": {"type": "string", "description": "Text encoding (default utf-8)."},
        },
        "required": ["path"],
    }


def write_file_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path. Parent created if needed."},
            "content": {"type": "string", "description": "Full file content (overwrites if exists)."},
            "confirm": {"type": "boolean", "description": "FALSE = preview (default). TRUE = write."},
            "encoding": {"type": "string"},
        },
        "required": ["path", "content"],
    }


def edit_file_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old": {"type": "string", "description": "Exact text to find (must occur exactly once unless replace_all)."},
            "new": {"type": "string", "description": "Replacement text."},
            "replace_all": {"type": "boolean", "description": "Allow multiple occurrences. Default false."},
            "confirm": {"type": "boolean", "description": "FALSE = preview. TRUE = edit."},
        },
        "required": ["path", "old", "new"],
    }


def glob_paths_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern e.g. '**/*.md' or '*.py'"},
            "root": {"type": "string", "description": "Root dir (default '.')."},
            "max_results": {"type": "integer", "description": "Cap (default 200)."},
        },
        "required": ["pattern"],
    }


_IMAGE_MIMES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}


def view_image(path: str) -> dict:
    """Attach an image at `path` to the next LLM call so the model can SEE it.

    Returns {"_image_attachment": {...}}. The orchestrator detects this key and
    buffers the image; the next chat() call injects it as a multi-modal Part
    (Gemma 4 / OpenAI / Anthropic all support image input).

    Use this when an image file exists on disk (e.g., a chart you just produced
    via run_python, or a screenshot a tool saved) and you need to look at it.
    For images the *user* sent via TG, you don't need this — the TG adapter
    auto-attaches them.
    """
    from agent.tools import _check
    p = _resolve(path)
    err = _check(p, "read")
    if err:
        return err
    if not p.exists() or not p.is_file():
        return {"error": f"image not found: {path}"}
    mime = _IMAGE_MIMES.get(p.suffix.lower())
    if not mime:
        return {"error": f"unsupported image extension: {p.suffix} (need .png/.jpg/.gif/.webp)"}
    size = p.stat().st_size
    if size > 10 * 1024 * 1024:
        return {"error": f"image too large: {size // (1024 * 1024)}MB (>10MB limit)"}
    return {
        "path": str(p),
        "mime": mime,
        "size_bytes": size,
        "_image_attachment": {"path": str(p), "mime": mime},
        "next_step": (
            "Image queued for the next chat call. Reply normally — you'll see "
            "the image content on your next turn."
        ),
    }


def view_image_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path to .png / .jpg / .gif / .webp."},
        },
        "required": ["path"],
    }


def grep_files_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python regex."},
            "path": {"type": "string", "description": "Root dir to search (default '.')."},
            "file_glob": {"type": "string", "description": "Filter (default '**/*')."},
            "max_results": {"type": "integer", "description": "Cap (default 100)."},
            "case_insensitive": {"type": "boolean", "description": "Default false."},
        },
        "required": ["pattern"],
    }
