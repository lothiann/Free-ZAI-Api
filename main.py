import asyncio
import json
import re
import secrets
import subprocess
import time
import uuid
import sys
import os
import webbrowser
from datetime import datetime

from playwright.async_api import async_playwright
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

# ===== CONFIG =====
TOKEN = ""
HOST = "127.0.0.1"
PORT = 8492
FALLBACK_MODEL = "glm-5.2"

# ===== STARTUP MENU STATE (toggled from the console before launch) =====
CAPTCHA_BYPASS = True    # [2] reload same account & retry when Aliyun captcha appears
ACCOUNT_ROTATE = True    # [3] rotate between accounts after rotate_every requests
HEADLESS = False         # [6] run the Playwright browser in headless mode
REQUEST_COOLDOWN = 5.0  # seconds between requests, avoids captcha on rapid fire
ACCOUNTS_FILE = "accounts.json"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, f"server_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] [{level}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", errors="replace").decode(), flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ===== JS SNIPPETS =====

INIT_HOOK_JS = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    const __origFetch = window.fetch;
    window.fetch = async function (...args) {
        const resp = await __origFetch.apply(this, args);
        try {
            const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
            if (!url.includes('/api/v2/chat/completions') || !resp.body) return resp;

            const [appStream, hookStream] = resp.body.tee();
            (async () => {
                const reader = hookStream.getReader();
                const decoder = new TextDecoder();
                let buf = '';
                while (true) {
                    const { value, done } = await reader.read();
                    if (done) break;
                    buf += decoder.decode(value, { stream: true });
                    const lines = buf.split('\\n');
                    buf = lines.pop();
                    for (const line of lines) {
                        const l = line.trim();
                        if (!l.startsWith('data:')) continue;
                        const payload = l.slice(5).trim();
                        if (payload === '[DONE]') {
                            window.__pyStream(JSON.stringify({ done: true }));
                            continue;
                        }
                        try {
                            let obj = JSON.parse(payload);
                            let d = obj.data;
                            if (typeof d === 'string') d = JSON.parse(d);
                            const phase = d.phase;
                            const delta = d.delta_content ?? d.content ?? '';
                            if (delta) window.__pyStream(JSON.stringify({ phase, delta }));
                            if (d.usage) window.__pyStream(JSON.stringify({ usage: d.usage }));
                            if (d.error) window.__pyStream(JSON.stringify({ error: d.error }));
                        } catch (e) {}
                    }
                }
                window.__pyStream(JSON.stringify({ done: true }));
            })();

            return new Response(appStream, { status: resp.status, statusText: resp.statusText, headers: resp.headers });
        } catch (e) {
            return resp;
        }
    };
"""

POPUP_KILLER_JS = """
    () => {
        let acted = false;
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        let node;
        while ((node = walker.nextNode())) {
            const t = node.textContent || '';
            if (t.includes('Now Available')) {
                let el = node.parentElement;
                let dialog = null;
                while (el && el !== document.body) {
                    if (el.hasAttribute('data-dialog-overlay')
                        || el.getAttribute('data-state') === 'open'
                        || el.getAttribute('role') === 'dialog'
                        || el.classList.contains('modal')) dialog = el;
                    el = el.parentElement;
                }
                if (!dialog) continue;
                for (const b of dialog.querySelectorAll('button')) {
                    const label = (b.getAttribute('aria-label') || '') + ' ' + (b.textContent || '');
                    const hasX = !!b.querySelector('svg.lucide-x, svg[class*="x"], svg[data-icon="x"], svg[data-icon="close"]');
                    const looksClose = /close|закрыть|×|✕/i.test(label);
                    const tiny = (b.textContent || '').trim().length <= 1;
                    if (hasX || looksClose || tiny) { b.click(); acted = true; break; }
                }
            }
        }
        const overlays = document.querySelectorAll(
            '[data-dialog-overlay], [role="dialog"][data-state="open"], div._modal-overlay'
        );
        for (const o of overlays) o.remove(), acted = true;
        document.body.style.pointerEvents = '';
        document.documentElement.style.pointerEvents = '';
        document.body.style.overflow = '';
        return acted;
    }
"""

MODEL_READY_JS = """
    () => {
        const btn = document.querySelector('button.modelSelectorButton, button[aria-haspopup="menu"]');
        return !!btn && !!(btn.offsetWidth || btn.offsetHeight);
    }
"""

OPEN_DROPDOWN_JS = """
    () => {
        const trigger = document.querySelector('button.modelSelectorButton, button[aria-haspopup="menu"]');
        if (!trigger || !(trigger.offsetWidth || trigger.offsetHeight)) return false;
        trigger.click();
        return true;
    }
"""

CLICK_OPTION_JS = """
    (modelUpper) => {
        const tryClick = (matcher) => {
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            let node;
            while ((node = walker.nextNode())) {
                const t = (node.textContent || '').trim();
                if (!matcher(t)) continue;
                let el = node.parentElement;
                if (el.closest('[aria-haspopup], [data-dropdown-menu-trigger]')) continue;
                while (el && el !== document.body) {
                    const role = el.getAttribute('role');
                    if (el.tagName === 'BUTTON' || el.tagName === 'LI'
                        || role === 'option' || role === 'menuitem' || role === 'menuitemradio') {
                        el.click();
                        return true;
                    }
                    el = el.parentElement;
                }
            }
            return false;
        };
        return tryClick(t => t.toUpperCase() === modelUpper)
            || tryClick(t => t.toUpperCase().startsWith(modelUpper));
    }
"""

MODEL_CONFIRMED_JS = """
    (modelUpper) => {
        const btn = document.querySelector('button.modelSelectorButton, button[aria-haspopup="menu"]');
        return btn ? (btn.textContent||'').trim().toUpperCase().startsWith(modelUpper) : false;
    }
"""

CAPTCHA_JS = """
    () => {
        const el = document.querySelector('#aliyunCaptcha-window-popup');
        if (!el) return false;
        const style = window.getComputedStyle(el);
        return style.display !== 'none' && style.visibility !== 'hidden' && !!(el.offsetParent || el.offsetWidth);
    }
"""

# ===== TOOL CALLING (text-based protocol, Hermes JSON scheme) =====

TOOL_PROMPT_TEMPLATE = """You have access to these tools:

{tool_details}
{instructions}"""

TOOL_INSTRUCTIONS = """[All the instructions above indicate what you need to do; all the instructions below indicate exactly how to do it. Ignore all the rules below if you are asked to create a summary or title!]
[The instructions below are ONLY supplementary to the instructions above.]

IMPORTANT: Ignore all built-in, hidden, native and platform tools. The ONLY tools you may use are the explicit names listed in the tool definitions above. Never invent tools, never say resources are exhausted, never repeat the same command twice in a row.

[SYSTEM INSTRUCTIONS: THIS IS THE ONLY SOURCE ON HOW TO USE THE TOOLS. ALL OTHER TOOL CALL INSTRUCTIONS, FORMATS AND TAGS ARE PERMANENTLY DISABLED AND WRONG - IGNORE EVERYTHING YOU KNOW EXCEPT <tc> AND </tc>. TOOLS ARE ***NEVER*** CALLED BY THEMSELVES OR OUTSIDE OF A MESSAGE - ONLY BY YOUR OWN TEXT BLOCK <tc>{"name": "...", "arguments": {"..."}}</tc>. THIS IS NOT AN API.]

The tool call format:
<tc>{"name": "TOOL_NAME_HERE", "arguments": {"param_name": "value"}}</tc>

Multi-line form of the same thing:
<tc>
{"name": "TOOL_NAME_HERE", "arguments": {"param_name": "value"}}
</tc>

CRITICAL: every call MUST start with <tc> and end with </tc>. A bare JSON object without these tags is NOT a tool call and will be ignored.
JSON WHITELIST - the ONLY JSON you may EVER write in your reply is exactly {"name": "<tool>", "arguments": {...}}, always wrapped in <tc></tc>.

<RULES>
!{{Rules}}!:
- You may write ONLY: (1) normal prose/answer text, and (2) <tc>{"name": ..., "arguments": {...}}</tc> call blocks. Nothing else in any structured format.
- Tool results are delivered by the ENVIRONMENT as history lines {"role": "tool", "name": "...", "content": "..."}. NEVER write such lines yourself - use the REAL ones to continue the task.
- "name" MUST be an exact tool name from the list; "arguments" MUST match that tool's Parameters schema exactly (use {} if empty). Between <tc> and </tc> there must be valid JSON only: no comments, no trailing commas, no markdown fences, and never forget the closing }.
- THERE IS NO AUTOMATIC REPAIR OF YOUR JSON. A mistake ruins everything - write it perfectly.
- ALWAYS emit the block when a tool is needed: never "I'll read it now..." alone, always text + <tc>...</tc>. NEVER pretend you called a tool when you did not write the block.
- Multiple tool calls = SEVERAL separate <tc> blocks, one JSON object each, so a broken block never kills the rest. Never put several JSON objects inside a single <tc> block:

<tc>
{"name": "TOOL_NAME_HERE1", "arguments": {"param_name": "value"}}
</tc>
<tc>
{"name": "TOOL_NAME_HERE2", "arguments": {"param_name": "value"}}
</tc>

- After the last </tc> output nothing more and stop immediately, waiting for results.
- If no suitable tool exists, pick an alternative from the EXISTING list; do not even mention other tools.
- Paths: use forward slashes / (recommended). If you must use backslashes, double them (\\\\) - single raw backslashes are invalid JSON escapes.
- It is recommended to use a colon to indicate that you are calling the tool:

Now I will read:
<tc> ... </tc>
</RULES>

Incorrect:
<BAD_EXAMPLES>
{"name": "bash", "arguments": {"command": "..."}}                                      <- bare JSON without <tc></tc> wrapper
<tc>{"name": "bash", "arguments": {"command": "..."}}                                  <- missing closing </tc>
{"name": "bash", "arguments": {"command": "dir"}}</tc>                                 <- missing opening <tc>
<tc>{"name": "bash", "arguments": {"command": "..."}</tc>                              <- missing closing }
<tc>{"name": "bash", "arguments": {"command": "..."}}]</tc>                            <- an unnecessary square bracket
<tc>{"name": "...", "arguments": {"...": 123"}}</tc>                                   <- unnecessary quotation mark
I'll read it now... (nothing)                                                          <- narrated instead of calling
I'll read it now... <tc>{"name": "read", "arguments": {"filePath": "/f"}}</tc>         <- call not moved to its own line
Let me search for that. {"name": "grep", "arguments": {"pattern": "x"}}                <- bare JSON next to text is NOT a call
<tc>{"name": "a", "arguments": {}}</tc> <tc>{"name": "b", "arguments": {}}</tc>        <- parallel calls on the SAME line; put each block on its OWN line
<tc> {"name": "a", "arguments": {}} {"name": "b", "arguments": {}} </tc>               <- never bundle several JSON objects into ONE block
<tool_call>...</tool_call>; <arg_value>...</arg_value>; search.todowrite, readfilePath <- non-existent blocks/tools
<tc>{"name": "bash", "arguments": {"command": "rg -n "p" src/"}}</tc>                  <- raw inner quotes break JSON; escape them as \\"
<tc>{"name": "...", 'arguments': {"..."}}</tc>                                         <- single quotes are invalid JSON
<tc>{"name": "...", "arguments": {"filePath": "\\Project\\file.h"}}</tc>               <- raw backslashes are invalid JSON escapes
<tc>{"name": "...", "arguments": {}} // fetch it</tc>                                  <- no comments inside the block
<tc>{"name": "...", "arguments": {},}</tc>                                             <- no trailing comma
<tc>{"name": "TOOL_NAME_HERE", "arguments": {"param_name": "value"}}</tc>              <- replace placeholders with real values
{"tool_calls": [{"name": "a"}, {"name": "b"}]}                                         <- array-wrapper format does not exist here
</BAD_EXAMPLES>

Correct:
<GOOD_EXAMPLES>
single call - brief prose if needed, then ONE block on its own line, then STOP completely:
Let me read that file.
<tc>{"name": "read", "arguments": {"filePath": "/project/file.txt"}}</tc>

parallel calls - SEVERAL separate blocks, one JSON object per block, stop right after:
<tc>
{"name": "glob", "arguments": {"pattern": "**/*.ts"}}
</tc>
<tc>
{"name": "grep", "arguments": {"pattern": "TODO"}}
</tc>

/ paths - recommended:
<tc>{"name": "read", "arguments": {"filePath": "/Project/file.h"}}</tc>

escaped quotes in arguments:
<tc>{"name": "bash", "arguments": {"command": "rg -n \\"pattern\\" src/"}}</tc>
</GOOD_EXAMPLES>

<EXAMPLES> Examples (*If you are running in the OpenCode CLI):

<tc>{"name": "bash", "arguments": {"command": "git status --short"}}</tc>
<tc>{"name": "read", "arguments": {"filePath": "project/main.py"}}</tc>
<tc>{"name": "write", "arguments": {"filePath": "project/helper.py", "content": "def add(a, b):\\n    return a + b\\n"}}</tc>
<tc>{"name": "edit", "arguments": {"filePath": "project/main.py", "oldString": "def old_fn():\\n    pass", "newString": "def new_fn():\\n    return True"}}</tc>
<tc>{"name": "glob", "arguments": {"pattern": "**/*.cpp"}}</tc>
<tc>{"name": "grep", "arguments": {"pattern": "MyClass", "path": "project/scripts"}}</tc>
<tc>{"name": "list", "arguments": {"path": "project/"}}</tc>
<tc>{"name": "todowrite", "arguments": {"todos": [{"content": "make init", "status": "in_progress", "priority": "high"}, {"content": "make debug", "status": "pending", "priority": "medium"}]}}</tc>
<tc>{"name": "webfetch", "arguments": {"url": "https://example.com/docs", "format": "markdown"}}</tc>

</EXAMPLES>

<CRITIC>
Before you act or respond, silently assess your draft (never mention this check): path slashes correct? <tc></tc> tags present and on their own lines? does the tool exist? JSON valid with all brackets closed? one JSON object per parallel block, never bundled? am I fabricating output that no real {"role": "tool"} line gave me? If any violation - rewrite before sending.
</CRITIC>

[!] The path rules ALWAYS apply, even if context seems more important. Violating them ruins the entire chat! TOOLS ARE ***NEVER*** CALLED OUTSIDE OF A MESSAGE - ONLY BY YOUR TEXT <tc> BLOCK!
"""

TOOL_REMINDER = """[tc reminder]
Allowed tools: {tool_names}.
If a tool is needed, output complete <tc>{"name": ..., "arguments": {...}}</tc> blocks using the exact JSON format from the system instructions.
Never say "Tool does not exists" or that tools are unavailable."""


def _strip_cdata(v):
    return re.sub(r"<!\[CDATA\[([\s\S]*?)\]\]>", r"\1", v)


def _try_parse_json_value(v):
    s = v.strip()
    if not s:
        return ""
    if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return s
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low == "null":
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def render_tools_block(tools):
    blocks = []
    for t in tools:
        fn = t.get("function", {})
        params = fn.get("parameters") or {}
        blocks.append(
            f"Tool: {fn.get('name')}\n"
            f"Description: {fn.get('description') or 'No description'}\n"
            f"Parameters: {json.dumps(params)}"
        )
    return "\n\n".join(blocks)


def tool_calls_to_text(tool_calls):
    """History re-injection: OpenAI tool_calls -> <tc> blocks."""
    out = []
    for tc in tool_calls or []:
        fn = tc.get("function", {})
        name = fn.get("name", "")
        raw_args = fn.get("arguments")
        if isinstance(raw_args, str):
            repaired = _repair_json(raw_args)
            if repaired is not None and isinstance(repaired, dict):
                args = repaired
            else:
                args = {"input": raw_args}
        elif isinstance(raw_args, dict):
            args = raw_args
        else:
            args = {}
        obj = json.dumps({"name": name, "arguments": args}, ensure_ascii=False)
        out.append(f"<tc>\n{obj}\n</tc>")
    return "\n".join(out)


def tool_result_to_text(call_id, name, content):
    payload = json.dumps(
        {"tool_call_id": call_id or "", "name": name or "", "content": str(content)},
        ensure_ascii=False,
    )
    return f"<tc_result>\n{payload}\n</tc_result>"


def _extract_json_objects(s):
    """Extract top-level balanced {...} objects from a string."""
    objs, depth, start = [], 0, None
    in_str = esc = False
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    objs.append(s[start:i + 1])
                    start = None
    return objs


def _repair_json(text):
    """Automatically salvage a broken JSON object/array using the failure
    patterns enumerated in <BAD_EXAMPLES>:
      - stray leading/trailing square brackets   ({...}]  /  [{...})
      - trailing commas                           {"a":1,}  ...
      - //-style comments inside the block        {...} // note
      - missing closing brace                     {"a": {"b": 1}
    Escaped-quote, raw-backslash and single-quote mistakes are intentionally
    NOT repaired: any such "fix" can corrupt string CONTENT (e.g. an
    apostrophe in "it's a test" or a slash in "https://..."), so those cases
    are left untouched. Returns the parsed Python value, or None if nothing
    could be salvaged."""
    text = text.strip()
    if not text:
        return None
    # Try as-is first.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    variants = [text]
    t = text
    # 1) Strip stray surrounding square brackets (allow repeats).
    for _ in range(4):
        nt = re.sub(r"^\s*\[\s*", "", t)
        nt = re.sub(r"\s*\]\s*$", "", nt)
        if nt == t:
            break
        t = nt
        variants.append(t)
    # 2) Remove top-level `//`-style comments, but ONLY outside string literals
    #    (a `//` inside a value like "https://x" must be kept intact).
    variants.append(_strip_line_comments(text))
    # 3) Remove trailing commas before a closing brace/bracket or at the end.
    t_tc = re.sub(r",\s*([}\]])", r"\1", text)
    t_tc = re.sub(r",\s*$", "", t_tc)
    variants.append(t_tc)
    # 4) Close an unclosed trailing brace by appending the right number of }.
    variants.append(_close_unclosed(text))
    # 5) Combined: several fixes applied together (e.g. trailing comma AND an
    #    unclosed brace in the same block).
    variants.append(_close_unclosed(t_tc))

    for v in variants:
        if not v or not v.strip():
            continue
        try:
            obj = json.loads(v)
        except (json.JSONDecodeError, TypeError):
            continue
        # Only accept a top-level object or array.
        if isinstance(obj, (dict, list)):
            return obj
    # 6) Last resort: extract any individually-balanced object and parse it
    #    directly (no recursion - a recursive call can never shrink the input).
    for raw in _extract_json_objects(text):
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, (dict, list)):
            return obj
    return None


def _strip_line_comments(s):
    """Remove `// ...` to end-of-line, but never inside a double-quoted JSON
    string (so 'https://x' or 'rg -n //foo src/' keep their content)."""
    out = []
    i, n = 0, len(s)
    in_str = esc = False
    while i < n:
        ch = s[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
        elif ch == "/" and i + 1 < n and s[i + 1] == "/":
            # skip to end of line, but keep the newline itself
            while i < n and s[i] != "\n":
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _close_unclosed(s):
    """Append enough closing braces to balance an object/array that the model
    forgot to close (e.g. {'a': {'b': 1}  ->  {'a': {'b': 1}})."""
    out = []
    depth = 0
    in_str = esc = False
    for ch in s:
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
        elif ch in "{[":
            depth += 1
            out.append(ch)
        elif ch in "}]":
            if depth > 0:
                depth -= 1
            out.append(ch)
        else:
            out.append(ch)
    return "".join(out) + ("}" * depth)


def parse_tool_call_blocks(text):
    """Parse tool calls from ALL <tc>...</tc> blocks, recognising BOTH the
    primary <tc> wrapper and the legacy <tool_call>/<tool_call> pairs.
    Each block may hold ONE or SEVERAL consecutive JSON objects. Parallel
    calls can therefore be written either as several objects inside ONE block
    OR as several separate blocks - both are collected.This way a broken object
    in one block doesn't take down the other calls (streaming-friendly)."""
    calls = []
    blocks = list(re.finditer(r"<(?:tc|tool_call)>\s*([\s\S]*?)\s*</(?:tc|tool_call)>", text))
    for m in blocks:
        inner = m.group(1).strip()
        inner = re.sub(r"^```(?:json)?\s*", "", inner)
        inner = re.sub(r"\s*```$", "", inner).strip()
        candidates = []
        try:
            obj = json.loads(inner)
            candidates = [obj]
        except json.JSONDecodeError:
            # Multiple balanced top-level objects inside THIS block = parallel
            # calls -> handle each separately (a whole-block repair would only
            # recover the first one).
            objs = _extract_json_objects(inner)
            used_objs = False
            if len(objs) > 1:
                for raw in objs:
                    fixed = _repair_json(raw)
                    if fixed is not None and isinstance(fixed, dict):
                        candidates.append(fixed)
                        used_objs = True
                    else:
                        try:
                            candidates.append(json.loads(raw))
                            used_objs = True
                        except json.JSONDecodeError:
                            log(f"[tools] invalid JSON fragment skipped: {raw[:120]}", level="WARN")
            if not used_objs:
                # Single (possibly broken) object/array -> auto-repair the block.
                repaired = _repair_json(inner)
                if repaired is not None:
                    if isinstance(repaired, list):
                        candidates = repaired
                    else:
                        candidates = [repaired]
        for obj in candidates:
            if isinstance(obj, dict) and obj.get("name"):
                calls.append({
                    "name": str(obj["name"]),
                    "arguments": json.dumps(obj.get("arguments") or {}, ensure_ascii=False),
                })
    return calls


# Backward-compatible alias
parse_ml_tool_calls = parse_tool_call_blocks


class ToolStreamBuffer:
    """Streams visible text, captures <tc>{json}</tc> blocks (and the legacy
    <tool_call>{json}</tool_call> form) and converts them to OpenAI tool_calls.
    If a captured block turns out not to be a valid tool call
    (e.g. '<tc>' mentioned in prose/code), its text is released back
    to the output so nothing is lost."""

    OPEN_TAGS = ("<tc>", "<tool_call>")
    CLOSE_TAGS = ("</tc>", "</tool_call>")

    def __init__(self):
        self.buf = ""
        self.capturing = False

    def feed(self, delta):
        """Returns (visible_text, newly_completed_calls_or_None)."""
        self.buf += delta
        visible_out = ""
        completed = None

        while True:
            if not self.capturing:
                # locate the nearest opening tag of either kind
                starts = [self.buf.find(t) for t in self.OPEN_TAGS if self.buf.find(t) != -1]
                idx = min(starts) if starts else -1
                if idx == -1:
                    # emit everything except a potentially partial trailing tag
                    hold = self._partial_hold_len()
                    if hold:
                        visible_out += self.buf[: len(self.buf) - hold]
                        self.buf = self.buf[len(self.buf) - hold:]
                    else:
                        visible_out += self.buf
                        self.buf = ""
                    break
                visible_out += self.buf[:idx]
                self.buf = self.buf[idx:]
                self.capturing = True

            ends = [self.buf.find(t) for t in self.CLOSE_TAGS if self.buf.find(t) != -1]
            if not ends:
                break  # wait for more data inside the block
            end = min(ends)
            # use the length of whichever closing tag actually matched
            matched_close = [t for t in self.CLOSE_TAGS if self.buf.find(t) == end]
            close_len = len(matched_close[0]) if matched_close else len(self.CLOSE_TAGS[0])
            block_end = end + close_len
            block = self.buf[:block_end]
            self.buf = self.buf[block_end:]
            self.capturing = False

            calls = parse_tool_call_blocks(block)
            if calls:
                completed = calls  # real tool call -> consumed, not visible
            else:
                visible_out += block  # false positive -> release as plain text

        return visible_out, completed

    def _partial_hold_len(self):
        """If buffer ends with a prefix of any opening/closing tag, hold it back."""
        for marker in self.OPEN_TAGS + self.CLOSE_TAGS:
            max_check = min(len(marker) - 1, len(self.buf))
            for l in range(max_check, 0, -1):
                if marker.startswith(self.buf[-l:]):
                    return l
        return 0

    def flush(self):
        """Final drain at stream end: release everything still buffered."""
        leftover = self.buf
        self.buf = ""
        self.capturing = False
        return leftover

THINKING_TRIGGER_JS = """
    () => {
        // find the "Deep Think" dropdown trigger near the input
        const candidates = [...document.querySelectorAll('button, [aria-haspopup]')];
        const trig = candidates.find(b => {
            const t = (b.textContent || '').trim();
            return /deep think/i.test(t) && (b.offsetWidth || b.offsetHeight);
        });
        if (!trig) return {found: false};
        trig.click();
        return {found: true};
    }
"""

# Simple think toggle for models WITHOUT the Deep Think dropdown
# (button carries data-autothink="true"/"false")
THINKING_SIMPLE_TOGGLE_JS = """
    (wantOn) => {
        const btns = [...document.querySelectorAll('button[data-autothink]')]
            .filter(b => b.offsetWidth || b.offsetHeight);
        if (!btns.length) return {found: false};
        const btn = btns[0];
        const isOn = btn.getAttribute('data-autothink') === 'true';
        if (isOn !== wantOn) {
            btn.click();
            return {found: true, changed: true};
        }
        return {found: true, changed: false};
    }
"""

THINKING_SWITCH_STATE_JS = """
    () => {
        const sws = [...document.querySelectorAll('[role="switch"]')]
            .filter(s => s.offsetWidth || s.offsetHeight);
        if (!sws.length) return null;
        const sw = sws[0];
        return sw.getAttribute('data-state')
            || (sw.getAttribute('aria-checked') === 'true' ? 'checked' : 'unchecked');
    }
"""

THINKING_SWITCH_CLICK_JS = """
    () => {
        const sws = [...document.querySelectorAll('[role="switch"]')]
            .filter(s => s.offsetWidth || s.offsetHeight);
        if (!sws.length) return false;
        sws[0].click();
        return true;
    }
"""

THINKING_CLICK_JS = """
    (levelUpper) => {
        const tryClick = (matcher) => {
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            let node;
            while ((node = walker.nextNode())) {
                const t = (node.textContent || '').trim();
                if (!matcher(t)) continue;
                let el = node.parentElement;
                if (el.closest('[aria-haspopup]')) continue;
                while (el && el !== document.body) {
                    const role = el.getAttribute('role');
                    if (el.tagName === 'BUTTON' || el.tagName === 'LI'
                        || role === 'option' || role === 'menuitem' || role === 'menuitemradio') {
                        el.click();
                        return true;
                    }
                    el = el.parentElement;
                }
            }
            return false;
        };
        // exact first, then prefix match
        return tryClick(t => t.toUpperCase() === levelUpper)
            || tryClick(t => t.toUpperCase().startsWith(levelUpper));
    }
"""


async def poll_js(page, expr, arg=None, timeout_s=10, poll_ms=100):
    deadline = time.time() + timeout_s
    result = None
    while time.time() < deadline:
        try:
            result = await page.evaluate(expr, arg) if arg is not None else await page.evaluate(expr)
            if result:
                return result
        except Exception:
            pass
        await asyncio.sleep(poll_ms / 1000)
    return None


def load_accounts():
    """accounts.json format:
    {"rotate_every": 10, "accounts": [{"name": "...", "email": "...", "token": "..."}]}
    or just [{"name": "...", "token": "..."}]
    """
    try:
        with open(ACCOUNTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        accounts = data["accounts"] if isinstance(data, dict) else data
        rotate_every = data.get("rotate_every", 10) if isinstance(data, dict) else 10
    except FileNotFoundError:
        # fallback: hardcoded TOKEN as single account
        log(f"{ACCOUNTS_FILE} not found, using fallback token", level="WARN")
        accounts = [{"name": "default", "token": TOKEN}]
        rotate_every = 10
    valid = [a for a in accounts if a.get("token")]
    log(f"Loaded {len(valid)} account(s): {[a.get('name', a.get('email', '?')) for a in valid]}")
    return valid, int(rotate_every)


# ===== BROWSER SESSION =====

class ZaiSession:
    def __init__(self):
        self.page = None
        self.browser = None
        self.context = None
        self.lock = asyncio.Lock()
        self.token_queue = asyncio.Queue()
        self._models_cache = None
        self._models_cache_ts = 0
        self.last_activity = 0.0
        self.last_usage = None
        # account rotation
        self.accounts, self.rotate_every = load_accounts()
        self.account_idx = 0
        self.requests_on_account = 0

    @property
    def current_account(self):
        return self.accounts[self.account_idx]

    async def switch_account(self, idx):
        """Swap token cookie, wipe cached auth state and restart the page."""
        acc = self.accounts[idx]
        log(f"[rotate] -> account #{idx} ({acc.get('name') or acc.get('email') or 'unnamed'})")

        # wipe localStorage/sessionStorage of the OLD origin first:
        # z.ai caches the user there and restores the previous session on load,
        # which overwrites our freshly-set cookie back to the old account
        try:
            await self.page.evaluate("try{localStorage.clear();sessionStorage.clear();}catch(e){}")
        except Exception:
            pass

        await self.context.clear_cookies()
        await self.context.add_cookies([
            {'name': 'token', 'value': acc["token"], 'domain': '.z.ai', 'path': '/'},
        ])
        self.account_idx = idx
        self.requests_on_account = 0
        self._models_cache = None  # models may differ per account tier
        await self.page.goto('https://chat.z.ai/', wait_until='domcontentloaded', timeout=60000)
        ready = await poll_js(self.page, MODEL_READY_JS, timeout_s=30)
        if not ready:
            raise RuntimeError(f"Account #{idx}: page never became ready")

    async def is_captcha(self):
        """True if the Aliyun slider captcha is currently visible."""
        try:
            return bool(await self.page.evaluate(CAPTCHA_JS))
        except Exception:
            return False

    async def reload_current(self):
        """Wipe all browser state (cookies/localStorage) and reload the SAME
        account from scratch to clear a captcha/fingerprint. Does NOT touch
        account_idx or requests_on_account, so it never counts as a rotation.
        """
        acc = self.accounts[self.account_idx]
        try:
            await self.page.evaluate("try{localStorage.clear();sessionStorage.clear();}catch(e){}")
        except Exception:
            pass
        await self.context.clear_cookies()
        await self.context.add_cookies([
            {'name': 'token', 'value': acc["token"], 'domain': '.z.ai', 'path': '/'},
        ])
        # Force a real reload: page.reload() re-navigates the current document
        # from scratch (goto() to the same SPA URL can be served from the bfcache
        # and miss the storage wipe, so the captcha survives).
        try:
            await self.page.reload(wait_until='domcontentloaded', timeout=60000)
        except Exception:
            await self.page.goto('https://chat.z.ai/', wait_until='domcontentloaded', timeout=60000)
        # double-check we are actually on the chat origin
        if self.page.url and "chat.z.ai" not in self.page.url:
            await self.page.goto('https://chat.z.ai/', wait_until='domcontentloaded', timeout=60000)
        ready = await poll_js(self.page, MODEL_READY_JS, timeout_s=30)
        if not ready:
            raise RuntimeError(f"Account #{self.account_idx}: page never became ready after captcha reload")

    async def _monitor_captcha(self, captcha_event, stop_event):
        """Background task: poll for the Aliyun captcha while we consume the
        stream. Sets captcha_event the instant the captcha appears and pushes a
        'done' sentinel so stream_tokens() wakes up instead of blocking forever
        (the server cuts the stream when the captcha pops up)."""
        try:
            if not CAPTCHA_BYPASS:
                return False
            while not stop_event.is_set():
                if await self.is_captcha():
                    captcha_event.set()
                    # wake up the blocked stream_tokens() consumer so the caller
                    # can reach reload_current()/retry even if no more tokens come
                    try:
                        self.token_queue.put_nowait({"done": True})
                    except Exception:
                        pass
                    return True
                await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log(f"[captcha] monitor error: {e}", level="ERROR")
        return False

    async def before_request(self):
        """Called inside lock: rotate if needed. Returns current account."""
        if ACCOUNT_ROTATE and self.requests_on_account >= self.rotate_every:
            next_idx = (self.account_idx + 1) % len(self.accounts)
            if len(self.accounts) > 1:
                await self.switch_account(next_idx)
            else:
                self.requests_on_account = 0  # single account, just reset counter
        self.requests_on_account += 1
        return self.current_account

    async def rate_limit(self):
        """Ensure REQUEST_COOLDOWN passed since last activity. Call inside lock."""
        wait = REQUEST_COOLDOWN - (time.time() - self.last_activity)
        if wait > 0:
            log(f"[rate-limit] cooldown {wait:.1f}s before next request")
            await asyncio.sleep(wait)
        self.last_activity = time.time()

    async def start(self):
        p = await async_playwright().start()
        self.browser = await p.chromium.launch(
            headless=HEADLESS,
            args=['--no-sandbox', '--disable-blink-features=AutomationControlled'],
        )
        self.context = await self.browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36',
            viewport={'width': 1536, 'height': 735},
            screen={'width': 1680, 'height': 1050},
            device_scale_factor=1.25,
            color_scheme='light',
            locale='en-US',
            timezone_id='Europe/Moscow',
        )
        await self.context.add_cookies([
            {'name': 'token', 'value': self.current_account["token"], 'domain': '.z.ai', 'path': '/'},
        ])
        await self.context.add_init_script(INIT_HOOK_JS)

        def handle_stream_chunk(data_json):
            try:
                obj = json.loads(data_json)
            except (json.JSONDecodeError, ValueError):
                return
            self.token_queue.put_nowait(obj)

        await self.context.expose_function("__pyStream", handle_stream_chunk)

        self.page = await self.context.new_page()

        async def on_pageerror(err):
            log(f"[PAGE ERROR] {err}", level="ERROR")

        self.page.on('pageerror', on_pageerror)

        # background popup watcher
        async def popup_watcher():
            while True:
                try:
                    acted = await self.page.evaluate(POPUP_KILLER_JS)
                    if acted:
                        log("[watcher] popup killed")
                except Exception:
                    pass
                await asyncio.sleep(0.3)

        asyncio.create_task(popup_watcher())

        # initial navigation
        await self.page.goto('https://chat.z.ai/', wait_until='domcontentloaded', timeout=60000)
        await poll_js(self.page, MODEL_READY_JS, timeout_s=30)
        log("Browser session ready")

    async def get_models(self):
        # cache for 60s
        if self._models_cache and time.time() - self._models_cache_ts < 60:
            return self._models_cache
        data = await self.page.evaluate("""
            async () => {
                const r = await fetch('/api/models', {headers: {'content-type': 'application/json'}});
                return await r.json();
            }
        """)
        models = []
        for m in data.get("data", []):
            models.append({
                "id": m.get("id"),
                "name": m.get("name"),
                "description": (m.get("info") or {}).get("meta", {}).get("description", ""),
            })
        self._models_cache = models
        self._models_cache_ts = time.time()
        return models

    def build_prompt(self, messages, tools=None):
        """History is rendered as OpenAI-style JSONL with FLAT tool calls
        ({"name": ..., "arguments": {...}} — same shape the model must emit
        inside <tc>), results keyed by tool name, no ids anywhere."""
        last_user = ""
        call_label_by_id = {}

        # pass 1: id -> display label ("name", repeats get "name #2")
        for m in messages:
            tcs = m.get("tool_calls") or []
            if not tcs:
                continue
            counts = {}
            for tc in tcs:
                name = (tc.get("function") or {}).get("name", "unknown")
                counts[name] = counts.get(name, 0) + 1
                label = name if counts[name] == 1 else f"{name} #{counts[name]}"
                cid = tc.get("id")
                if cid:
                    call_label_by_id[cid] = label

        def _content_str(c):
            if isinstance(c, list):
                return "\n".join(
                    x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text"
                )
            if isinstance(c, str):
                return c
            return json.dumps(c, ensure_ascii=False)

        def _reasoning_str(m):
            """opencode sends assistant reasoning back as content parts
            {"type": "reasoning", "text": ...} (or a reasoning_content field)."""
            c = m.get("content")
            if isinstance(c, list):
                parts = [x.get("text", "") for x in c
                         if isinstance(x, dict) and x.get("type") == "reasoning"]
                return "\n".join(p for p in parts if p)
            rc = m.get("reasoning_content") or m.get("reasoning")
            return str(rc) if rc else ""

        hist_lines = []
        for m in messages:
            role = m.get("role")
            content = _content_str(m.get("content", ""))
            if role == "system":
                hist_lines.append({"role": "system", "content": content})
            elif role == "user":
                last_user = content
                hist_lines.append({"role": "user", "content": content})
            elif role == "assistant":
                line = {"role": "assistant", "content": content or ""}
                reasoning = _reasoning_str(m)
                if reasoning:
                    line["thinking"] = reasoning
                calls = []
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    raw_args = fn.get("arguments", {})
                    if isinstance(raw_args, str):
                        repaired = _repair_json(raw_args)
                        args = repaired if isinstance(repaired, dict) else {"_raw": str(raw_args)}
                    else:
                        args = raw_args or {}
                    calls.append({"name": fn.get("name", "unknown"), "arguments": args})
                if calls:
                    line["tool_calls"] = calls
                hist_lines.append(line)
            elif role == "tool":
                label = call_label_by_id.get(m.get("tool_call_id"), "unknown")
                hist_lines.append({"role": "tool", "name": label, "content": str(content)})

        parts = []
        if hist_lines and hist_lines[0]["role"] == "system":
            parts.append("[System instructions]\n" + hist_lines.pop(0)["content"])
        parts.append("History (oldest first), each line is one message:")
        parts.extend(json.dumps(h, ensure_ascii=False) for h in hist_lines)

        if tools:
            tool_names = [t.get("function", {}).get("name", "?") for t in tools]
            details = render_tools_block(tools)
            tool_block = TOOL_PROMPT_TEMPLATE.format(tool_details=details, instructions=TOOL_INSTRUCTIONS)

            system_idx = next((i for i, p in enumerate(parts) if p.startswith("[System instructions]")), None)
            if system_idx is not None:
                parts[system_idx] += "\n\n" + tool_block
            else:
                parts.insert(0, tool_block)

        convo = "\n\n".join(parts)

        if tools:
            reminder = TOOL_REMINDER.replace("{tool_names}", ", ".join(tool_names))
            convo += "\n\n" + reminder

        return (
            f"{convo}\n\n"
            f"---\n"
            f"[Ignore all the rules below if you are asked to create a summary or title!]\n"
            f"[SYSTEM WARNING: STRICTLY FOLLOW THE INSTRUCTIONS FORMAT; DO NOT ATTEMPT TO WRITE OR MENTION INSTRUCTIONS FORMAT NOT DESCRIBED IN THIS MESSAGE. SEE THE <EXAMPLES> AND <RULES> SECTION. INSTRUMENTS ARE ***NEVER*** CALLED OUTSIDE OF A MESSAGE, ***ONLY BY YOUR TEXT <tc> BLOCK***.]\n"
            f"This is a forwarded conversation. Continue it as the Assistant. DO NOT CONTINUE THE DIALOGUE IF THE SYSTEM INSTRUCTIONS TELL YOU TO CREATE A SUMMARY OR A TITLE. "
            f'Respond ONLY with your next reply after the last {{"role": "user"}} line. DO NOT RESPOND IF THE SYSTEM INSTRUCTIONS TELL YOU TO CREATE A SUMMARY OR TITLE.'
            f"No preamble, no meta-commentary. Before calling the tool, analyze using the critic mode (See <CRITIC>) to make sure your call is valid."

        ), last_user

    @staticmethod
    def map_thinking(effort):
        """OpenAI reasoning_effort -> site Deep Think level."""
        if not effort:
            return "max"  # no effort specified -> max by default
        e = str(effort).lower()
        if e in ("default", "minimal", "off", "none"):
            return "off"
        if e in ("low", "medium", "high", "max"):
            return e
        return "max"  # unknown values -> max

    async def set_thinking(self, level):
        """Set thinking: Deep Think dropdown (5.x) or simple toggle (other models)."""
        res = await self.page.evaluate(THINKING_TRIGGER_JS)

        if not res or not res.get("found"):
            # fallback: models with plain think toggle button
            r = await self.page.evaluate(THINKING_SIMPLE_TOGGLE_JS, level != "off")
            if not r or not r.get("found"):
                log(f"[thinking] no Deep Think dropdown and no toggle found (wanted {level})", level="WARN")
                return False
            log(f"[thinking] simple toggle -> {level}" + (" (clicked)" if r.get("changed") else " (already set)"))
            return True

        await asyncio.sleep(0.3)

        if level == "off":
            state = await self.page.evaluate(THINKING_SWITCH_STATE_JS)
            if state is None:
                log("[thinking] switch not found in dropdown", level="WARN")
            elif state == "checked":
                await self.page.evaluate(THINKING_SWITCH_CLICK_JS)
                log("[thinking] toggled OFF")
            else:
                log("[thinking] already off")
            await self.page.keyboard.press("Escape")
            return True

        # high / max: ensure switch is ON first
        state = await self.page.evaluate(THINKING_SWITCH_STATE_JS)
        if state is None:
            log("[thinking] switch not found, trying option directly", level="WARN")
        elif state != "checked":
            await self.page.evaluate(THINKING_SWITCH_CLICK_JS)
            await asyncio.sleep(0.4)  # wait for level options to render
            log("[thinking] toggled ON")

        clicked = await poll_js(self.page, THINKING_CLICK_JS, level.upper(), timeout_s=3, poll_ms=100)
        if clicked:
            log(f"[thinking] set to {level}")
        else:
            log(f"[thinking] option '{level}' not found in dropdown", level="WARN")
        await self.page.keyboard.press("Escape")
        return bool(clicked)

    async def prepare_chat(self, model_id):
        """Fresh chat page with the given model selected."""
        await self.page.goto('https://chat.z.ai/', wait_until='domcontentloaded', timeout=60000)
        ready = await poll_js(self.page, MODEL_READY_JS, timeout_s=30)
        if not ready:
            raise RuntimeError("Model selector never appeared")
        # make sure no popup blocks us
        await poll_js(self.page,
                      "() => !document.querySelector('[data-dialog-overlay], div._modal-overlay')",
                      timeout_s=3)

        opened = await self.page.evaluate(OPEN_DROPDOWN_JS)
        if not opened:
            raise RuntimeError("Could not open model dropdown")

        # Try matching by display name first (from /api/models), then by model_id
        models = await self.get_models()
        display = model_id
        for m in models:
            if m.get("id") == model_id and m.get("name"):
                display = m["name"]
                break

        clicked = await poll_js(self.page, CLICK_OPTION_JS, display.upper(), timeout_s=5, poll_ms=50)
        if not clicked and display != model_id:
            clicked = await poll_js(self.page, CLICK_OPTION_JS, model_id.upper(), timeout_s=2, poll_ms=50)
        if not clicked:
            raise RuntimeError(f"Option {model_id} not found in dropdown")

        confirmed = await poll_js(self.page, MODEL_CONFIRMED_JS, display.upper(), timeout_s=5)
        if not confirmed and display != model_id:
            confirmed = await poll_js(self.page, MODEL_CONFIRMED_JS, model_id.upper(), timeout_s=2)
        if not confirmed:
            raise RuntimeError(f"Could not select model {model_id}")

    async def send_message(self, prompt):
        # drain stale tokens
        self.last_usage = None
        while not self.token_queue.empty():
            self.token_queue.get_nowait()

        await self._fill_and_send(prompt)

    async def _fill_and_send(self, text):
        textarea = await self.page.wait_for_selector('textarea', timeout=10000)
        try:
            await textarea.click(timeout=3000)
        except Exception:
            await self.page.evaluate("() => document.querySelector('textarea').click()")
        await textarea.fill(text)

        send_ready = await poll_js(self.page, """
            () => {
                const b = document.querySelector('#send-message-button')
                       || document.querySelector('button[type="submit"]');
                return !!(b && !b.disabled && (b.offsetWidth || b.offsetHeight));
            }
        """, timeout_s=5, poll_ms=50)
        if not send_ready:
            raise RuntimeError("Send button never became enabled")

        await self.page.evaluate("""
            () => {
                const b = document.querySelector('#send-message-button')
                       || document.querySelector('button[type="submit"]');
                if (b) b.click();
            }
        """)

    async def stop_generation(self):
        """Click the site's Stop button (aria-label='Stop') once."""
        js = """
            () => {
                const wrap = document.querySelector('div[aria-label="Stop"]');
                const b = wrap && wrap.querySelector('button');
                if (!b) return false;
                b.click();
                return true;
            }
        """
        try:
            return bool(await self.page.evaluate(js))
        except Exception:
            return False

    async def stream_tokens(self, timeout_s=300):
        """Yield (phase, delta) tuples until done."""
        while True:
            try:
                item = await asyncio.wait_for(self.token_queue.get(), timeout=timeout_s)
            except asyncio.TimeoutError:
                log("[stream] timeout waiting for tokens", level="ERROR")
                self.last_activity = time.time()
                return
            if item.get("done"):
                self.last_activity = time.time()
                return
            if "error" in item:
                log(f"[stream error] {item['error']}", level="ERROR")
                yield ("error", json.dumps(item["error"]))
                return
            if "usage" in item:
                self.last_usage = item.get("usage")
                continue
            delta = item.get("delta") or ""
            if delta:
                yield (item.get("phase"), delta)


session = ZaiSession()

# ===== FASTAPI APP =====

app = FastAPI(title="z.ai -> OpenAI compatible proxy")


@app.get("/")
async def root():
    return {"status": "ok", "endpoints": ["/v1/models", "/v1/chat/completions", "/accounts"]}


@app.get("/accounts")
async def accounts_status():
    return {
        "current_index": session.account_idx,
        "requests_on_account": session.requests_on_account,
        "rotate_every": session.rotate_every,
        "accounts": [
            {
                "index": i,
                "name": a.get("name") or a.get("email") or "unnamed",
                "active": i == session.account_idx,
                "token_preview": a["token"][:20] + "...",
            }
            for i, a in enumerate(session.accounts)
        ],
    }


@app.get("/v1/models")
async def list_models():
    try:
        models = await session.get_models()
    except Exception as e:
        log(f"/v1/models error: {e}", level="ERROR")
        return JSONResponse({"error": {"message": str(e)}}, status_code=500)
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": m["id"], "object": "model", "created": now, "owned_by": "z.ai",
             "permission": [], "root": m["id"], "parent": None}
            for m in models
        ],
    }


def make_chunk(chunk_id, created, model, delta, finish_reason=None):
    return {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def build_usage(session, full_reasoning, full_answer):
    """Real usage from the z.ai stream when available; rough char/4
    estimates otherwise. reasoning_tokens is always an estimate."""
    u = getattr(session, "last_usage", None) or {}
    pt = u.get("prompt_tokens") or 0
    ct = u.get("completion_tokens") or 0
    tt = u.get("total_tokens") or (pt + ct)
    if not (pt or ct or tt):
        prompt_len = getattr(session, "last_prompt_chars", 0)
        answer_len = sum(len(x) for x in full_answer)
        pt, ct, tt = prompt_len // 4, answer_len // 4, (prompt_len + answer_len) // 4
    reasoning_est = sum(len(x) for x in full_reasoning) // 4
    details = {"reasoning_tokens": reasoning_est} if reasoning_est else {}
    return {
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "total_tokens": tt,
        "completion_tokens_details": details,
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    req_model = body.get("model", FALLBACK_MODEL)
    messages = body.get("messages", [])
    stream = body.get("stream", False)
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    # validate model
    try:
        available = await session.get_models()
    except Exception as e:
        return JSONResponse({"error": {"message": f"failed to list models: {e}"}}, status_code=500)
    avail_ids = [m["id"] for m in available]
    # case-insensitive match -> canonical site ID
    avail_map = {mid.lower(): mid for mid in avail_ids}
    req_model_canonical = avail_map.get(req_model.lower())
    if req_model_canonical is None:
        return JSONResponse({
            "error": {
                "message": f"model '{req_model}' not found. Available: {avail_ids}",
                "type": "invalid_request_error",
            }
        }, status_code=400)
    req_model = req_model_canonical

    prompt, _last_user = session.build_prompt(messages, tools=body.get("tools"))
    thinking_level = session.map_thinking(body.get("reasoning_effort"))
    has_tools = bool(body.get("tools"))
    log(f"--> completion req: model={req_model} msgs={len(messages)} stream={stream} "
        f"prompt_len={len(prompt)} effort={body.get('reasoning_effort') or 'default'}->{thinking_level} "
        f"tools={len(body.get('tools') or [])}")

    def sse(obj):
        return f"data: {json.dumps(obj)}\n\n"

    async def generate():
        # Lock must cover the WHOLE flow (prepare + send + consume),
        # otherwise a parallel request navigates the page and kills this stream.
        async with session.lock:
            try:
                await session.rate_limit()
                acc = await session.before_request()
                log(f"[account] serving via '{acc.get('name') or acc.get('email')}' ({session.requests_on_account}/{session.rotate_every})")

                # Retry loop: if the Aliyun captcha pops up (usually right after
                # we START streaming, sometimes after a navigation), reload the
                # SAME account to clear fingerprint/cookies and retry WITHOUT
                # counting a rotation. A retry is only done when the captcha
                # appears BEFORE any thinking/answer output was delivered to the
                # client (otherwise a silent retry would duplicate sent tokens).
                while True:
                    # state is per-attempt (reset on every retry)
                    full_reasoning = []
                    full_answer = []
                    tool_buf = ToolStreamBuffer() if has_tools else None
                    finish_reason = "stop"
                    tool_call_index = 0
                    stream_error = None
                    captcha_abort = False

                    try:
                        await session.prepare_chat(req_model)
                        await session.set_thinking(thinking_level)
                        await session.send_message(prompt)
                    except Exception as e:
                        msg = str(e)
                        transient = (
                            await session.is_captcha()
                            or "never appeared" in msg
                            or "never became ready" in msg
                            or "not found in dropdown" in msg
                            or "enabled" in msg
                        )
                        if transient and CAPTCHA_BYPASS:
                            log("[captcha] Aliyun captcha detected during prepare -> retry", level="WARN")
                            await session.reload_current()
                            continue
                        yield sse({"error": {"message": str(e), "type": "proxy_error"}})
                        yield "data: [DONE]\n\n"
                        return

                    # ---- consume the stream, aborting on captcha before output ----
                    answer_started = False
                    stop_event = asyncio.Event()
                    captcha_event = asyncio.Event()
                    monitor = asyncio.create_task(session._monitor_captcha(captcha_event, stop_event))
                    try:
                        async for phase, delta in session.stream_tokens():
                            # captcha appeared before we emitted anything -> retry
                            if captcha_event.is_set() and not answer_started:
                                captcha_abort = True
                                break
                            if phase == "error":
                                stream_error = delta
                                break
                            if phase == "thinking":
                                answer_started = True
                                full_reasoning.append(delta)
                                yield sse(make_chunk(chunk_id, created, req_model, {"reasoning_content": delta}))
                                continue

                            calls_batch = None
                            visible = delta
                            if tool_buf is not None:
                                visible, calls_batch = tool_buf.feed(delta)
                            else:
                                full_answer.append(delta)
                            if visible:
                                answer_started = True
                                full_answer.append(visible)
                                yield sse(make_chunk(chunk_id, created, req_model, {"content": visible}))
                            if calls_batch:
                                finish_reason = "tool_calls"
                                for tc in calls_batch:
                                    yield sse(make_chunk(chunk_id, created, req_model, {
                                        "tool_calls": [{
                                            "index": tool_call_index,
                                            "id": "call_" + secrets.token_hex(8),
                                            "type": "function",
                                            "function": {"name": tc["name"], "arguments": tc["arguments"]},
                                        }]
                                    }))
                                    tool_call_index += 1
                                answer_started = True
                    finally:
                        stop_event.set()
                        monitor.cancel()

                    # Decisive captcha check AFTER stream_tokens returns, regardless
                    # of whether the async-for body ever ran (a captcha 'done'
                    # sentinel makes stream_tokens return before the first token,
                    # so the in-loop check above may never execute).
                    if captcha_event.is_set() and not answer_started:
                        captcha_abort = True

                    if stream_error:
                        yield sse({"error": {"message": stream_error, "type": "proxy_error"}})
                        yield "data: [DONE]\n\n"
                        return

                    if captcha_abort:
                        log("[captcha] Aliyun captcha detected during stream -> retry", level="WARN")
                        await session.reload_current()
                        continue

                    # no captcha, no stream error -> clean completion
                    break
            except Exception as e:
                log(f"prepare/send failed: {e}", level="ERROR")
                yield sse({"error": {"message": str(e), "type": "proxy_error"}})
                yield "data: [DONE]\n\n"
                return

            try:
                if tool_buf is not None:
                    leftover = tool_buf.flush()
                    if leftover:
                        full_answer.append(leftover)
                        yield sse(make_chunk(chunk_id, created, req_model, {"content": leftover}))

                yield sse(make_chunk(chunk_id, created, req_model, {}, finish_reason=finish_reason))
                usage_out = build_usage(session, full_reasoning, full_answer)
                yield sse({
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": req_model,
                    "choices": [],
                    "usage": usage_out,
                })
                yield "data: [DONE]\n\n"
            except (asyncio.CancelledError, GeneratorExit):
                # Client went away mid-stream -> stop generation on the site.
                # NOTE: awaiting anything here is pointless - the generator is
                # being finalized and won't resume. So the click AND its log
                # live in an independent task that survives the teardown.
                async def _stop_and_log():
                    stopped = await session.stop_generation()
                    log(f"[stream] client disconnected -> stop button "
                        f"{'clicked' if stopped else 'NOT found'}")
                asyncio.create_task(_stop_and_log())
                raise
            finally:
                log(f"<-- done: reasoning={sum(len(x) for x in full_reasoning)}ch answer={sum(len(x) for x in full_answer)}ch")
                with open("last_response.json", "w", encoding="utf-8") as f:
                    json.dump({"reasoning": "".join(full_reasoning), "answer": "".join(full_answer)},
                              f, ensure_ascii=False, indent=2)

    if stream:
        return StreamingResponse(generate(), media_type="text/event-stream")

    # non-streaming: accumulate
    while True:
        reasoning_parts, answer_parts = [], []
        raw_answer = ""
        tool_buf = ToolStreamBuffer() if has_tools else None
        captcha_abort = False
        stream_failed = None
        try:
            async with session.lock:
                await session.rate_limit()
                await session.before_request()
                await session.prepare_chat(req_model)
                await session.set_thinking(thinking_level)
                await session.send_message(prompt)

                captcha_event = asyncio.Event()
                stop_event = asyncio.Event()
                monitor = asyncio.create_task(session._monitor_captcha(captcha_event, stop_event))
                try:
                    async for phase, delta in session.stream_tokens():
                        if captcha_event.is_set() and not (reasoning_parts or answer_parts):
                            captcha_abort = True
                            break
                        if phase == "error":
                            stream_failed = delta
                            break
                        if phase == "thinking":
                            reasoning_parts.append(delta)
                        elif phase != "error":
                            if tool_buf is not None:
                                raw_answer += delta
                                visible, _ = tool_buf.feed(delta)
                                answer_parts.append(visible)
                                leftover = ""
                            else:
                                answer_parts.append(delta)
                                raw_answer += delta
                    if tool_buf is not None:
                        leftover = tool_buf.flush()
                        if leftover:
                            answer_parts.append(leftover)
                finally:
                    stop_event.set()
                    monitor.cancel()
        except Exception as e:
            log(f"prepare/send failed: {e}", level="ERROR")
            return JSONResponse({"error": {"message": str(e)}}, status_code=502)

        # Decisive captcha check AFTER stream_tokens returns, even if the
        # async-for body never ran (a captcha 'done' sentinel returns before the
        # first token, so the in-loop check above may not execute).
        if captcha_event.is_set() and not (reasoning_parts or answer_parts):
            captcha_abort = True

        if stream_failed:
            return JSONResponse({"error": {"message": stream_failed, "type": "proxy_error"}},
                                status_code=502)
        if not captcha_abort:
            break
        log("[captcha] Aliyun captcha detected -> retry", level="WARN")
        await session.reload_current()
        continue

    content = "".join(answer_parts)
    message = {"role": "assistant", "content": content,
               "reasoning_content": "".join(reasoning_parts)}
    finish = "stop"
    if has_tools:
        calls = parse_tool_call_blocks(raw_answer)
        if calls:
            message["tool_calls"] = [{
                "id": "call_" + secrets.token_hex(8),
                "type": "function",
                "function": {"name": tc["name"], "arguments": tc["arguments"]},
            } for tc in calls]
            message["content"] = None
            finish = "tool_calls"
    return {
        "id": chunk_id,
        "object": "chat.completion",
        "created": created,
        "model": req_model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish,
        }],
        "usage": build_usage(session, reasoning_parts, answer_parts),
    }


async def main():
    await run_menu()
    await session.start()
    log(f"Starting OpenAI-compatible server on http://{HOST}:{PORT}/v1")
    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


# ===== STARTUP MENU =====

LOGO = r"""███████╗██████╗ ███████╗███████╗   ███████╗ █████╗ ██╗       █████╗ ██████╗ ██╗
██╔════╝██╔══██╗██╔════╝██╔════╝   ╚══███╔╝██╔══██╗██║      ██╔══██╗██╔══██╗██║
█████╗  ██████╔╝█████╗  █████╗█████╗ ███╔╝ ███████║██║█████╗███████║██████╔╝██║
██╔══╝  ██╔══██╗██╔══╝  ██╔══╝╚════╝███╔╝  ██╔══██║██║╚════╝██╔══██║██╔═══╝ ██║
██║     ██║  ██║███████╗███████╗   ███████╗██║  ██║██║      ██║  ██║██║     ██║
╚═╝     ╚═╝  ╚═╝╚══════╝╚══════╝   ╚══════╝╚═╝  ╚═╝╚═╝      ╚═╝  ╚═╝╚═╝     ╚═╝"""


RESET = "\x1b[0m"


def _enable_ansi():
    """Enable ANSI color codes on Windows console (no-op elsewhere)."""
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            h = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            kernel32.GetConsoleMode(h, ctypes.byref(mode))
            kernel32.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:
            pass


def _clear():
    os.system("cls" if os.name == "nt" else "clear")


def _tick(on):
    if on:
        return "\x1b[32mON\x1b[0m"   # green
    return "\x1b[31mOFF\x1b[0m"      # red


def _visible_len(s):
    """Length of a string ignoring ANSI escape codes (for alignment)."""
    return len(re.sub(r"\x1b\[[0-9;]*m", "", s))


def _term_width():
    try:
        return os.get_terminal_size().columns
    except Exception:
        return 80


def _center(s, width=None):
    """Center a plain (non-ANSI) string on a terminal line."""
    if width is None:
        width = _term_width()
    left = max(0, (width - len(s)) // 2)
    return " " * left + s


def _ansi_color(t):
    """45° gradient: purple (top-left) -> cyan (bottom-right) via linear lerp."""
    r1, g1, b1 = 147, 112, 219  # purple
    r2, g2, b2 = 0, 200, 255     # cyan
    r = int(r1 + (r2 - r1) * t)
    g = int(g1 + (g2 - g1) * t)
    b = int(b1 + (b2 - b1) * t)
    return f"\x1b[38;2;{r};{g};{b}m"


def _gradient_lines(lines, width):
    h = len(lines)
    pad_w = max(len(l) for l in lines)
    left = max(0, (width - pad_w) // 2)
    out = []
    for y, line in enumerate(lines):
        painted = " " * left
        for x, ch in enumerate(line):
            t = (x + y) / max(1, (pad_w - 1) + (h - 1))
            painted += _ansi_color(t) + ch
        out.append(painted + RESET)
    return out


def _render_menu():
    _enable_ansi()
    _clear()
    w = _term_width()
    for line in _gradient_lines(LOGO.splitlines(), w):
        print(line)
    print()
    table = [
        ["[1] Start", f"[4] API Port: {PORT}", "[7] GitHub"],
        [f"[2] {_tick(CAPTCHA_BYPASS)} Captcha Bypass", "[5] Open accounts.json", "[8] Exit"],
        [f"[3] {_tick(ACCOUNT_ROTATE)} Account Rotate", f"[6] {_tick(HEADLESS)} Headless Browser", ""],
    ]
    # One fixed width per column = the widest cell in that column; each column
    # is then separated by exactly COL_GAP spaces, so all rows align perfectly.
    COL_GAP = 3
    ncols = max(len(r) for r in table)
    col_w = [max(_visible_len(r[i]) if i < len(r) else 0 for r in table) for i in range(ncols)]
    rows = []
    for r in table:
        line = ""
        for i in range(ncols):
            cell = r[i] if i < len(r) else ""
            line += cell + " " * (col_w[i] - _visible_len(cell))
            if i < ncols - 1:
                line += " " * COL_GAP
        rows.append(line)
    # Center the whole block as one unit so EVERY row shares the SAME left
    # offset (otherwise each row centers itself and the columns drift apart).
    block_w = max(_visible_len(r) for r in rows)
    left = max(0, (w - block_w) // 2)
    for r in rows:
        print(" " * left + r)
    print()


def open_accounts_file():
    """Open accounts.json in the default editor/app (works on win/mac/linux)."""
    path = os.path.abspath(ACCOUNTS_FILE)
    try:
        if os.name == "nt":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        log(f"[menu] opened {path}")
    except Exception as e:
        log(f"[menu] could not open accounts.json: {e}", level="ERROR")


GITHUB_URL = "https://github.com/lothiann/Free-ZAI-Api"


def open_github():
    """Open the GitHub repo in the default browser (works on win/mac/linux)."""
    try:
        webbrowser.open(GITHUB_URL)
        log(f"[menu] opened {GITHUB_URL}")
    except Exception as e:
        log(f"[menu] could not open GitHub: {e}", level="ERROR")


async def run_menu():
    """Interactive startup menu. Returns when the user picks [1] Start."""
    global PORT, HEADLESS, CAPTCHA_BYPASS, ACCOUNT_ROTATE
    while True:
        _render_menu()
        try:
            choice = input(" Choice: ").strip()
        except (EOFError, KeyboardInterrupt):
            # Ctrl+C at the main prompt exits the program
            _clear()
            log("[menu] exited (Ctrl+C)")
            raise SystemExit(0)
        if choice == "1":
            _clear()
            return
        elif choice == "2":
            CAPTCHA_BYPASS = not CAPTCHA_BYPASS
        elif choice == "3":
            ACCOUNT_ROTATE = not ACCOUNT_ROTATE
        elif choice == "4":
            try:
                new_port = input("\n API Port (ESC to cancel): ").strip()
            except (EOFError, KeyboardInterrupt):
                new_port = ""
            # ESC (\x1b) or 'esc' or empty cancels back to the menu
            if new_port in ("", "esc", "\x1b") or "\x1b" in new_port:
                continue
            try:
                PORT = int(new_port)
            except ValueError:
                log(f"[menu] invalid port: {new_port!r}", level="ERROR")
        elif choice == "5":
            open_accounts_file()
            try:
                input("\n Press Enter to continue...")
            except (EOFError, KeyboardInterrupt):
                pass
        elif choice == "6":
            HEADLESS = not HEADLESS
        elif choice == "7":
            open_github()
            try:
                input("\n Press Enter to continue...")
            except (EOFError, KeyboardInterrupt):
                pass
        elif choice == "8":
            _clear()
            log("[menu] exited")
            raise SystemExit(0)


if __name__ == "__main__":
    asyncio.run(main())
