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
HEADLESS = True           # [6] hide the browser window (True = hidden, default on)
REQUEST_COOLDOWN = 5.0  # seconds between requests, avoids captcha on rapid fire
TOOL_CALL_DELAY = 0.5  # seconds between parallel tool-call chunks, avoids Busy errors in the client
CAPTCHA_RELOAD_ATTEMPTS = 3      # reload attempts inside reload_current() before failing
CAPTCHA_RELOAD_READY_TIMEOUT = 30  # seconds to wait for the page to become ready per attempt
CAPTCHA_RELOAD_BACKOFF = 5.0     # seconds between reload attempts
MAX_REQUEST_RETRIES = 4          # max captcha/fetch retries per request before giving up
ACCOUNTS_FILE = "accounts.json"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, f"server_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")


ANSI = {
    "INFO": "\x1b[36m",   # cyan
    "WARN": "\x1b[33m",   # yellow
    "ERROR": "\x1b[31m",  # red
    "OK": "\x1b[32m",     # green
    "BOLD": "\x1b[1m",
    "DIM": "\x1b[2m",
    "RESET": "\x1b[0m",
}


_ansi_enabled = False


def log(msg, level="INFO"):
    global _ansi_enabled
    if not _ansi_enabled:
        try:
            _enable_ansi()
        except Exception:
            pass
        _ansi_enabled = True
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] [{level}] {msg}"
    try:
        color = ANSI.get(level, "") + ANSI.get("BOLD", "")
        ts_col = f"{ANSI['DIM']}{ts}{ANSI['RESET']}"
        lvl_col = f"{color}[{level}]{ANSI['RESET']}"
        if level == "OK":
            print(f"{ts_col} {lvl_col} {ANSI['OK']}{msg}{ANSI['RESET']}", flush=True)
        else:
            print(f"{ts_col} {lvl_col} {msg}", flush=True)
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

# Injected right after "# History ..." header as a final system line when tools
# are used. Edit the text freely - the proxy injects it verbatim.
FINAL_SYSTEM_MESSAGE = """All other tool call instructions, formats and tags are PERMANENTLY disabled and WRONG - ignore everything you know except <tc> and </tc>. NEVER write {"role": "tool", "name": "...", "content": "..."} block. The path rules ALWAYS apply, even if context seems more important. NEVER write anything after <tc> block. <tc> block must ALWAYS be at the end of your response. See <rules> and <system> for rules. See <bad_examples> for bad examples. NEVER write \\n - this does NOT work. NEVER output JSON keys role, content, thinking, name, tool_call_id as your reply. Your reply is plain text, optionally with <tc>...</tc> blocks."""

SYSTEM_CONTINUE = 'This is a forwarded conversation.'

TOOL_PROMPT_TEMPLATE = """You have access to these tools:

{tool_details}
{instructions}"""

TOOL_INSTRUCTIONS = """STRICTLY follow the instructions; See the <examples>, <rules> and <critic> sections. All the instructions above indicate what you need to do; all the instructions below indicate exactly how to do it. Ignore all the rules below if you are asked to create a summary or title! The instructions below are ONLY supplementary to the instructions above.
IMPORTANT: Ignore all built-in, hidden, native and platform tools. The ONLY tools you may use are the explicit names listed in the tool definitions above. Never invent tools, never say resources are exhausted, never repeat the same command twice in a row.
This is the only source on how to use the tools. All other tool call instructions, formats and tags are PERMANENTLY disabled and WRONG - ignore everything you know except <tc> and </tc>. NEVER write {"role": "tool", "name": "...", "content": "..."} block.

 # Tool Call Format:
The tool call format:
<tc>{"name": "TOOL_NAME_HERE", "arguments": {"param_name": "value"}}</tc>

Multi-line form of the same thing:
<tc>
{"name": "TOOL_NAME_HERE", "arguments": {"param_name": "value"}}
</tc>

CRITICAL: every call MUST start with <tc> and end with </tc>. A bare JSON object without these tags is NOT a tool call and will be ignored.
JSON WHITELIST - the ONLY JSON you may EVER write in your reply is exactly {"name": "<tool>", "arguments": {...}}, always wrapped in <tc></tc>.

<rules>
 # Rules:
- You may write ONLY: (1) normal prose/answer text, and (2) <tc>{"name": ..., "arguments": {...}}</tc> call blocks. Nothing else in any structured format.
- Tool results are delivered by the ENVIRONMENT as history lines {"role": "tool", "name": "...", "content": "..."}. NEVER write such lines yourself - use the REAL ones to continue the task.
- "name" MUST be an exact tool name from the list; "arguments" MUST match that tool's Parameters schema exactly (use {} if empty). Between <tc> and </tc> there must be valid JSON only: no comments, no trailing commas, no markdown fences, and never forget the closing }.
- NEVER write {"role": "tool", "name": "...", "content": "..."} block. 
- NEVER output JSON keys role, content, thinking, name, tool_call_id as your reply. Your reply is plain text, optionally with <tc>...</tc> blocks.
- Use only THOSE tools that are listed in <allowed_tools>.
- If the previous tool didn't show result, it means you violated some rules of the tools from <bad_examples>.
- Multiple tool calls = SEVERAL separate <tc> blocks, one JSON object each, so a broken block never kills the rest. Never put several JSON objects inside a single <tc> block:

<tc>
{"name": "TOOL_NAME_HERE1", "arguments": {"param_name": "value"}}
</tc>
<tc>
{"name": "TOOL_NAME_HERE2", "arguments": {"param_name": "value"}}
</tc>

- If no suitable tool exists, pick an alternative from the EXISTING list; do not even mention other tools.
- Paths: use forward slashes / (recommended). If you must use backslashes, double them (\\\\) - single raw backslashes are invalid JSON escapes.
- Don't break anything, even if you've already broken it in the chat history.
- Don't write "The user reported ..." and similar phrases.
- NEVER write anything after <tc> block. <tc> block must ALWAYS be at the end of your response.
- NEVER write \\n - this does NOT work.
- It is recommended to use a colon to indicate that you are calling the tool:

Now I will read:
<tc> ... </tc>

</rules>

 # Incorrect:
<bad_examples>
{"role": "assistant", "content": ...                                                   <- "assistant" should NEVER be written
{"role": "assistant", "content": "..."}                                                <- "assistant" should NEVER be written
{"role": "tool", "name": "...", "content": "..."}                                      <- "tool" should NEVER be written
{"name": "bash", "arguments": {"command": "..."}}                                      <- bare JSON without <tc></tc> wrapper
<tc>{"name": "bash", "arguments": {"command": "..."}}                                  <- missing closing </tc>
{"name": "bash", "arguments": {"command": "dir"}}</tc>                                 <- missing opening <tc>
<tc>{"name": "bash", "arguments": {"command": "..."}</tc>                              <- missing closing }
<tc>{"name": "bash", "arguments": {"command": "..."}}]</tc>                            <- an unnecessary square bracket
<tc>{"name": "...", "arguments": {"...": 123"}}</tc>                                   <- unnecessary quotation mark
I'll read it now...: (nothing)                                                         <- narrated instead of calling
I'll read it now...: <tc>{"name": "read", "arguments": {"filePath": "/f"}}</tc>        <- call not moved to its own line
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
</bad_examples>

 # Correct:
<good_examples>
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
</good_examples>

<examples>
 # Examples (*If you are running in the OpenCode CLI):

<tc>{"name": "bash", "arguments": {"command": "git status --short"}}</tc>
<tc>{"name": "read", "arguments": {"filePath": "project/main.py"}}</tc>
<tc>{"name": "write", "arguments": {"filePath": "project/helper.py", "content": "def add(a, b):\\n    return a + b\\n"}}</tc>
<tc>{"name": "edit", "arguments": {"filePath": "project/main.py", "oldString": "def old_fn():\\n    pass", "newString": "def new_fn():\\n    return True"}}</tc>
<tc>{"name": "glob", "arguments": {"pattern": "**/*.cpp"}}</tc>
<tc>{"name": "grep", "arguments": {"pattern": "MyClass", "path": "project/scripts"}}</tc>
<tc>{"name": "list", "arguments": {"path": "project/"}}</tc>
<tc>{"name": "todowrite", "arguments": {"todos": [{"content": "make init", "status": "in_progress", "priority": "high"}, {"content": "make debug", "status": "pending", "priority": "medium"}]}}</tc>
<tc>{"name": "webfetch", "arguments": {"url": "https://example.com/docs", "format": "markdown"}}</tc>

</examples>

<critic>
Before you act or respond, silently assess your draft (never mention this check): path slashes correct? <tc></tc> tags present and on their own lines? does the tool exist? JSON valid with all brackets closed? one JSON object per parallel block, never bundled? am I fabricating output that no real {"role": "tool"} line gave me? If any violation - rewrite before sending.
</critic>

How your response chain works from the user’s perspective:

 +---- User message
 | (trigger)
 +---> Your previous text with <tc> block
 | (trigger)
 +---> Your previous text with <tc> block
 | (trigger)
 +---> A new request for you regarding the continuation
 |
 +---> If there's no <tc> block — that's it!

trigger - a new request for you to take the following action

<priorities>
 # Priorities:
1. The last role system message.
2. The <system> messages.
3. The <rules>
4. User message
</priorities>
"""


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


def _strip_stray_quotes(s):
    """Remove quote characters that cannot legally START a JSON string at
    their position. A quote is legal only right after a key/value context
    opener (`{`, `[`, `:`, `,`) or at the very start; anywhere else it is a
    stray that the model produced by accident (e.g. `"a": 123"}}` - a quote
    after the value has nothing to open). Only quotes OUTSIDE real strings
    are touched, so content inside strings (apostrophes, urls, escaped
    quotes) is always preserved."""
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
            j = len(out) - 1
            while j >= 0 and out[j] in " \t\r\n":
                j -= 1
            valid = j < 0 or out[j] in "{[:," 
            if valid:
                in_str = True
                out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


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
    # 4) Remove stray quote characters outside strings (e.g. `123"}}`).
    t_sq = _strip_stray_quotes(text)
    variants.append(t_sq)
    # 5) Close an unclosed trailing brace by appending the right number of }.
    variants.append(_close_unclosed(text))
    # 6) Combined: several fixes applied together (e.g. trailing comma AND an
    #    unclosed brace in the same block).
    variants.append(_close_unclosed(t_tc))
    variants.append(_close_unclosed(t_sq))

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
    # 7) Optional json_repair library (lazy import - never a hard dependency).
    #    Accept only if it did NOT invent new string content: every string we
    #    keep must already appear in the input, so fabrication like guessing
    #    "...a.ex" -> "...a.example." is rejected instead of corrupting a call.
    for v in _repair_via_lib(text):
        return v
    return None


def _repair_via_lib(s):
    """Try the `json_repair` package (pip install json-repair) as a final fallback.
    Guards against content fabrication by only returning parsed values whose
    string leaves are all present in the original input."""
    try:
        from json_repair import repair_json
    except Exception:
        return []
    try:
        value = repair_json(s, return_objects=True)
    except Exception:
        return []
    # avoid parsing a bare string/comment/number — we need an object or array
    if not isinstance(value, (dict, list)):
        return []
    if _json_strings_preserved(s, value):
        return [value]
    return []


def _json_strings_preserved(orig, obj):
    """True if every string leaf in `obj` also appears in `orig`. Detects when
    a repair library fabricated content that the model never emitted."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if not _json_strings_preserved(orig, k):
                    return False
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
        elif isinstance(cur, str):
            if cur and cur not in orig:
                return False
    return True


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

# ===== WINDOW HIDING (Windows only) =====
# The browser is a REAL GUI Chromium (true headless breaks Aliyun captcha), but
# when HEADLESS is on its window must not be visible. We hide it via Win32:
# SW_HIDE removes it from the screen, WS_EX_TOOLWINDOW removes its button from
# the taskbar / Alt+Tab list. HEADLESS = hide the window or not.
import psutil  # noqa: E402


def _ms_playwright_chrome_pids():
    """PIDs of running Chromium launched from a ms-playwright install dir."""
    pids = set()
    for proc in psutil.process_iter(["exe"]):
        try:
            exe = (proc.info.get("exe") or "").lower()
            if exe and "ms-playwright" in exe and exe.endswith("chrome.exe"):
                pids.add(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return pids


def _hide_windows_for_pids(pids):
    """Hide top-level windows owned by the given PIDs (no-op on non-Windows)."""
    if os.name != "nt" or not pids:
        return
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32

    def _pid_of_window(hwnd):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def _hide(hwnd):
        # SW_HIDE + remove from taskbar (tool window, not app window)
        GCL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x80
        WS_EX_APPWINDOW = 0x40000
        ex = user32.GetWindowLongW(hwnd, GCL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GCL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
        user32.ShowWindow(hwnd, 0)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum_cb(hwnd, lparam):
        if user32.IsWindowVisible(hwnd) and _pid_of_window(hwnd) in pids:
            try:
                _hide(hwnd)
            except Exception:
                pass
        return True

    user32.EnumWindows(_enum_cb, 0)


class ZaiSession:
    def __init__(self, worker_id=0, accounts=None, rotate_every=None):
        self.worker_id = worker_id
        self.busy = False
        self.page = None
        self.browser = None
        self.context = None
        self.lock = asyncio.Lock()
        self.token_queue = asyncio.Queue()
        self._models_cache = None
        self._models_cache_ts = 0
        self.last_activity = 0.0
        self.hide_pids = set()
        # account rotation
        if accounts is None:
            accounts, rotate_every = load_accounts()
        self.accounts = accounts
        self.rotate_every = rotate_every if rotate_every is not None else 10
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
        Retries internally so a slow page (or a re-appearing captcha) does not
        fail the request on the first attempt.
        """
        acc = self.accounts[self.account_idx]
        last_err = None
        for attempt in range(1, CAPTCHA_RELOAD_ATTEMPTS + 1):
            try:
                try:
                    await self.page.evaluate("try{localStorage.clear();sessionStorage.clear();}catch(e){}")
                except Exception:
                    pass
                await self.context.clear_cookies()
                await self.context.add_cookies([
                    {'name': 'token', 'value': acc["token"], 'domain': '.z.ai', 'path': '/'},
                ])
                # Force a real reload: page.reload() re-navigates the current
                # document from scratch (goto() to the same SPA URL can be
                # served from the bfcache and miss the storage wipe, so the
                # captcha survives).
                try:
                    await self.page.reload(wait_until='domcontentloaded', timeout=60000)
                except Exception:
                    await self.page.goto('https://chat.z.ai/', wait_until='domcontentloaded', timeout=60000)
                # double-check we are actually on the chat origin
                if self.page.url and "chat.z.ai" not in self.page.url:
                    await self.page.goto('https://chat.z.ai/', wait_until='domcontentloaded', timeout=60000)
                ready = await poll_js(self.page, MODEL_READY_JS, timeout_s=CAPTCHA_RELOAD_READY_TIMEOUT, poll_ms=250)
                if ready:
                    return True
                last_err = RuntimeError(f"Account #{self.account_idx}: page never became ready after captcha reload")
            except Exception as e:
                last_err = e
            if attempt < CAPTCHA_RELOAD_ATTEMPTS:
                log(f"[captcha] reload attempt {attempt}/{CAPTCHA_RELOAD_ATTEMPTS} not ready, retrying", level="WARN")
                await asyncio.sleep(CAPTCHA_RELOAD_BACKOFF)
        raise last_err or RuntimeError(f"Account #{self.account_idx}: page never became ready after captcha reload")

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
        _before_pids = _ms_playwright_chrome_pids()
        args = [
            '--no-sandbox',
            '--disable-blink-features=AutomationControlled',
            # strip non-essential components (safe on Windows:
            # NO --single-process / --no-zygote, they crash the browser)
            '--disable-gpu',
            '--disable-software-rasterizer',
            '--disable-extensions',
            '--disable-background-networking',
            '--disable-component-update',
            '--disable-default-apps',
            '--disable-sync',
            '--disable-translate',
            '--disable-breakpad',
            '--disable-crash-reporter',
            '--disable-dev-shm-usage',
            '--disable-renderer-backgrounding',
            '--disable-backgrounding-occluded-windows',
            '--disable-background-timer-throttling',
            '--disable-renderer-throttling',
            '--metrics-recording-only',
            '--no-first-run',
            '--no-default-browser-check',
            '--mute-audio',
            '--window-size=1366,768',
        ]
        # HEADLESS = hide the window. The browser itself stays a REAL (non-
        # headless) GUI build because true headless mode triggers Aliyun
        # captcha; with hiding on we just park the window far off-screen.
        if HEADLESS:
            args.append('--window-position=-32000,-32000')
        self.browser = await p.chromium.launch(
            headless=False,
            args=args,
        )
        if HEADLESS:
            # Hide this browser's window IMMEDIATELY (the window appears at
            # launch, so hiding it here beats any periodic hider loop).
            self.hide_pids = _ms_playwright_chrome_pids() - _before_pids
            _hide_windows_for_pids(self.hide_pids)
            # Chromium creates its top-level window a few ms AFTER launch()
            # returns, so keep re-hiding during the first ~2s while that
            # window materialises.
            async def _early_hide():
                for _ in range(50):
                    _hide_windows_for_pids(self.hide_pids)
                    await asyncio.sleep(0.04)
            asyncio.create_task(_early_hide())
        else:
            self.hide_pids = set()
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
                    await self.page.evaluate(POPUP_KILLER_JS)
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
                reasoning = _reasoning_str(m)
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
                # Tool calls are folded into the SAME content field as <tc>
                # blocks at the end (the shape the model itself must emit),
                # instead of a separate tool_calls key.
                if calls:
                    tc_text = "\n".join(
                        f"<tc>{json.dumps(c, ensure_ascii=False)}</tc>" for c in calls
                    )
                    if content:
                        content += "\n"
                    content += tc_text
                line = {"role": "assistant", "content": content or ""}
                if reasoning:
                    line["thinking"] = reasoning
                hist_lines.append(line)
            elif role == "tool":
                label = call_label_by_id.get(m.get("tool_call_id"), "unknown")
                hist_lines.append({"role": "tool", "name": label, "content": str(content)})

        # The first system message (if any) becomes the "[System instructions]"
        # block and is placed right after the History header below.
        system_instr = None
        if hist_lines and hist_lines[0]["role"] == "system":
            system_instr = hist_lines.pop(0)["content"]

        parts = []
        tool_names = []

        # 1. tool block first (if tools)
        if tools:
            tool_names = [t.get("function", {}).get("name", "?") for t in tools]
            details = render_tools_block(tools)
            parts.append(TOOL_PROMPT_TEMPLATE.format(tool_details=details, instructions=TOOL_INSTRUCTIONS))
        # 2. Allowed tools (only if tools)
        if tools:
            parts.append("<allowed_tools>\n Allowed tools: " + ", ".join(tool_names) + "\n</allowed_tools>")
        # 3. History header
        parts.append("# History (oldest first), each line is one message:")
        # [System instructions] directly after the history header, as a proper
        # role=system message line
        if system_instr:
            parts.append(json.dumps({"role": "system", "content": system_instr}, ensure_ascii=False))
        # 4. FINAL_SYSTEM_MESSAGE as a separate line (only if tools)
        if tools:
            parts.append(json.dumps({"role": "system", "content": FINAL_SYSTEM_MESSAGE}, ensure_ascii=False))
        # 5. Each history line as JSONL
        parts.extend(json.dumps(h, ensure_ascii=False) for h in hist_lines)

        convo = "\n\n".join(parts)

        return (
            f"{convo}\n\n"
            f"---\n"
            f"{SYSTEM_CONTINUE}\n"
            f"Assistant's reply (ONLY content):"
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
            return True

        await asyncio.sleep(0.3)

        if level == "off":
            state = await self.page.evaluate(THINKING_SWITCH_STATE_JS)
            if state is None:
                log("[thinking] switch not found in dropdown", level="WARN")
            elif state == "checked":
                await self.page.evaluate(THINKING_SWITCH_CLICK_JS)
            await self.page.keyboard.press("Escape")
            return True

        # high / max: ensure switch is ON first
        state = await self.page.evaluate(THINKING_SWITCH_STATE_JS)
        if state is None:
            log("[thinking] switch not found, trying option directly", level="WARN")
        elif state != "checked":
            await self.page.evaluate(THINKING_SWITCH_CLICK_JS)
            await asyncio.sleep(0.4)  # wait for level options to render

        clicked = await poll_js(self.page, THINKING_CLICK_JS, level.upper(), timeout_s=3, poll_ms=100)
        if not clicked:
            log(f"[thinking] option '{level}' not found in dropdown", level="WARN")
        await self.page.keyboard.press("Escape")
        return bool(clicked)

    async def prepare_chat(self, model_id):
        """Fresh chat page with the given model selected."""
        attempts = 3
        for attempt in range(1, attempts + 1):
            await self.page.goto('https://chat.z.ai/', wait_until='domcontentloaded', timeout=60000)
            ready = await poll_js(self.page, MODEL_READY_JS, timeout_s=30)
            if not ready:
                if attempt < attempts:
                    log(f"[prepare] model selector not ready (attempt {attempt}/{attempts}), "
                        f"reloading page ...", level="WARN")
                    await asyncio.sleep(5)
                    continue
                raise RuntimeError("Model selector never appeared")
            break
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
        while not self.token_queue.empty():
            self.token_queue.get_nowait()

        await self._fill_and_send(prompt)

    async def _fill_and_send(self, text):
        ok = await self.page.evaluate(
            """(text) => {
                const el = document.querySelector('textarea');
                if (!el) return false;
                const set = Object.getOwnPropertyDescriptor(
                    window.HTMLTextAreaElement.prototype, 'value').set;
                set.call(el, text);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            }""",
            text,
        )
        if not ok:
            raise RuntimeError("textarea not found")

        # The site disables Send when the text exceeds its client-side limit
        # (~900k chars) and shows "Text input is too long". The check is
        # cosmetic: the submission itself still works if disabled is removed,
        # so instead of waiting forever we force-enable and click.
        sent = False
        bypassed = False
        for _ in range(2):
            res = await self.page.evaluate("""
                () => {
                    const b = document.querySelector('#send-message-button')
                           || document.querySelector('button[type="submit"]');
                    if (!b) return {sent: false, bypassed: false};
                    let bypassed = false;
                    if (b.disabled) {
                        b.removeAttribute('disabled');
                        bypassed = true;
                    }
                    b.click();
                    return {sent: true, bypassed: bypassed};
                }
            """)
            sent = bool(res["sent"])
            if sent:
                bypassed = bool(res["bypassed"])
                break
            await asyncio.sleep(1)
        if not sent:
            raise RuntimeError("Send button never appeared")
        if bypassed:
            log("[limit] message over client limit -> forced Send", level="WARN")

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
                yield ("error", item["error"])
                return
            delta = item.get("delta") or ""
            if delta:
                yield (item.get("phase"), delta)


session = ZaiSession()

# ===== WORKER POOL =====
# Each concurrent /v1/chat/completions request gets its OWN worker (= its own
# browser/context/page with its own cookies), so N parallel requests use N
# browsers. Idle workers are reused; a new one is only spawned when all
# existing workers are busy - the browser count never exceeds the current
# number of active requests.

class WorkerPool:
    def __init__(self):
        accounts, rotate_every = load_accounts()
        self._accounts = accounts
        self._rotate_every = rotate_every
        self._workers = []            # all ZaiSession ever created
        self._idle = []               # free workers (queue discipline)
        self._counter = 0
        self._hider_task = None

    async def acquire(self):
        """Return a worker, spawning a fresh browser if none is idle."""
        while True:
            if self._idle:
                wk = self._idle.pop(0)
                wk.busy = True
                return wk
            wk = ZaiSession(
                worker_id=self._counter,
                accounts=self._accounts,
                rotate_every=self._rotate_every,
            )
            self._counter += 1
            self._workers.append(wk)
            wk.busy = True
            try:
                await wk.start()
            except Exception:
                self._workers.remove(wk)
                raise
            log(f"[pool] spawned worker #{wk.worker_id} ({len(self._workers)} browsers alive, {len(self._idle)} idle)")
            return wk

    def release(self, wk):
        """Return a busy worker to the idle pool."""
        if wk is None or wk not in self._workers:
            return
        wk.busy = False
        self._idle.append(wk)

    def start_hider(self):
        """Background loop: keep all worker browser windows hidden (HEADLESS only)."""
        if not HEADLESS or os.name != "nt" or self._hider_task is not None:
            return
        async def _hider():
            while True:
                pids = set()
                for wk in self._workers:
                    pids.update(getattr(wk, "hide_pids", set()))
                _hide_windows_for_pids(pids)
                await asyncio.sleep(0.1)
        self._hider_task = asyncio.create_task(_hider())

    @property
    def active_count(self):
        return sum(1 for w in self._workers if w.busy)

    async def models_any(self):
        """/v1/models helper: reuse the cache of any free worker, else grab one."""
        for wk in self._workers:
            if not wk.busy and wk._models_cache:
                return wk._models_cache
        wk = await self.acquire()
        try:
            return await wk.get_models()
        finally:
            self.release(wk)

    def usage_stats(self):
        """Aggregate counters for /accounts (worst-case: first busy worker)."""
        for wk in self._workers:
            if wk.busy:
                return wk
        return self._workers[0] if self._workers else None


pool = WorkerPool()

# ===== FASTAPI APP =====

app = FastAPI(title="z.ai -> OpenAI compatible proxy")


@app.get("/")
async def root():
    return {"status": "ok", "endpoints": ["/v1/models", "/v1/chat/completions", "/accounts"]}


@app.get("/accounts")
async def accounts_status():
    stats = pool.usage_stats()
    return {
        "active_requests": pool.active_count,
        "browsers": len(pool._workers),
        "current_index": stats.account_idx if stats else 0,
        "requests_on_account": stats.requests_on_account if stats else 0,
        "rotate_every": pool._rotate_every,
        "accounts": [
            {
                "index": i,
                "name": a.get("name") or a.get("email") or "unnamed",
                "active": i == pool.usage_stats().account_idx if pool.usage_stats() else False,
                "token_preview": a["token"][:20] + "...",
            }
            for i, a in enumerate(pool._accounts)
        ],
    }


@app.get("/v1/models")
async def list_models():
    try:
        models = await pool.models_any()
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


def _msg_chars(m):
    try:
        return len(json.dumps(m, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        return len(str(m))


def _non_text_part_chars(m):
    """Chars contributed by image / audio content parts inside a message."""
    image_chars = audio_chars = 0
    content = m.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            t = part.get("type")
            if t == "image_url":
                image_chars += _msg_chars(part.get("image_url") or {})
            elif t == "input_audio":
                audio_chars += _msg_chars(part.get("input_audio") or {})
    return image_chars, audio_chars


def build_usage(messages, full_reasoning, full_answer):
    """Char counts (1 token := 1 character), matching the site's
    ~2M real limit which is made of characters. Output follows the standard
    OpenAI usage shape (prompt_tokens_details / completion_tokens_details):
    - prompt_tokens   = every non-assistant message (user/system/developer/
        tool/function ...) serialized as JSON, incl. image/audio chars;
    - completion_tokens = every assistant message serialized as JSON (incl.
        tool_calls) + the newly generated reasoning/answer.
    """
    prompt_text = prompt_image = prompt_audio = completion_hist = 0
    for m in messages or []:
        n = _msg_chars(m)
        if (m.get("role") or "unknown") == "assistant":
            completion_hist += n
        else:
            prompt_text += n
        img, aud = _non_text_part_chars(m)
        prompt_image += img
        prompt_audio += aud
    prompt_image = min(prompt_image, prompt_text)
    prompt_audio = min(prompt_audio, prompt_text - prompt_image)

    reasoning_est = sum(len(x) for x in full_reasoning)
    answer_est = sum(len(x) for x in full_answer)
    completion_len = completion_hist + reasoning_est + answer_est

    return {
        "prompt_tokens": prompt_text,
        "prompt_tokens_details": {
            "cached_tokens": 0,
            "audio_tokens": prompt_audio,
            "image_tokens": prompt_image,
            "cached_tokens_details": {
                "text_tokens": prompt_text - prompt_image - prompt_audio,
                "audio_tokens": 0,
                "image_tokens": 0,
            },
        },
        "completion_tokens": completion_len,
        "completion_tokens_details": {
            "reasoning_tokens": reasoning_est,
            "accepted_prediction_tokens": 0,
            "rejected_prediction_tokens": 0,
            "audio_tokens": 0,
        },
        "total_tokens": prompt_text + completion_len,
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
        available = await pool.models_any()
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
    prompt_len = len(prompt)
    log(f"<-- request: history msgs={len(messages)} prompt_len={prompt_len} | model={req_model}")

    def sse(obj):
        return f"data: {json.dumps(obj)}\n\n"

    async def generate():
        # Each /v1/chat/completions stream runs on its OWN worker (its own
        # browser/context/page), so multiple requests can generate in parallel.
        wk = await pool.acquire()
        try:
            try:
                await wk.rate_limit()
                acc = await wk.before_request()
                log(f"[account] serving via '{acc.get('name') or acc.get('email')}' ({wk.requests_on_account}/{wk.rotate_every})")
            except Exception as e:
                log(f"prepare/send failed: {e}", level="ERROR")
                yield sse({"error": {"message": str(e), "type": "proxy_error"}})
                yield "data: [DONE]\n\n"
                return

            # Universal retry: ANY failure (Alipay captcha, page fetch error,
            # stream error, timeout, exception) while NOTHING has been sent to
            # the client yet -> reload the same account and retry. Once output
            # started a retry would duplicate tokens, so we stop retrying then.
            retries = 0
            while True:
                full_reasoning = []
                full_answer = []
                tool_buf = ToolStreamBuffer() if has_tools else None
                finish_reason = "stop"
                tool_call_index = 0
                answer_started = False
                fail_reason = None

                try:
                    await wk.prepare_chat(req_model)
                    await wk.set_thinking(thinking_level)
                    await wk.send_message(prompt)
                except Exception as e:
                    # a captcha/block during prepare also reloads forever
                    if await wk.is_captcha() and CAPTCHA_BYPASS:
                        fail_reason = "captcha appeared"
                    else:
                        fail_reason = str(e)

                captcha_event = asyncio.Event()
                stop_event = asyncio.Event()
                monitor = None
                try:
                    if fail_reason is None:
                        monitor = asyncio.create_task(wk._monitor_captcha(captcha_event, stop_event))
                        async for phase, delta in wk.stream_tokens():
                            # captcha appeared before we emitted anything -> retry
                            if captcha_event.is_set() and not answer_started:
                                fail_reason = "captcha appeared"
                                break
                            if phase == "error":
                                fail_reason = delta
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
                                last_sent = 0.0  # wall-clock throttle between calls
                                for tc in calls_batch:
                                    if last_sent:
                                        # send next call only if TOOL_CALL_DELAY has
                                        # passed since the previous one; otherwise wait
                                        remaining = TOOL_CALL_DELAY - (time.time() - last_sent)
                                        if remaining > 0:
                                            await asyncio.sleep(remaining)
                                    yield sse(make_chunk(chunk_id, created, req_model, {
                                        "tool_calls": [{
                                            "index": tool_call_index,
                                            "id": "call_" + secrets.token_hex(8),
                                            "type": "function",
                                            "function": {"name": tc["name"], "arguments": tc["arguments"]},
                                        }]
                                    }))
                                    tool_call_index += 1
                                    last_sent = time.time()
                                answer_started = True
                finally:
                    stop_event.set()
                    if monitor:
                        monitor.cancel()

                if fail_reason is None and captcha_event.is_set() and not answer_started:
                    fail_reason = "captcha appeared"

                if fail_reason:
                    if "upstream 413" in fail_reason or "Request Entity Too Large" in fail_reason:
                        # payload over the site limit -> never retry (same result)
                        log(f"[request] {fail_reason}", level="ERROR")
                        yield sse({"error": {"message": fail_reason, "type": "proxy_error"}})
                        yield "data: [DONE]\n\n"
                        return
                    if answer_started:
                        # output already delivered -> a retry would duplicate tokens
                        yield sse({"error": {"message": fail_reason, "type": "proxy_error"}})
                        yield "data: [DONE]\n\n"
                        return
                    if fail_reason == "captcha appeared":
                        # captcha retries forever (as before) - just reload the same
                        # account until the invisible check passes
                        log(f"[request] {fail_reason} -> retry", level="WARN")
                        await wk.reload_current()
                        continue
                    retries += 1
                    if retries >= MAX_REQUEST_RETRIES:
                        log(f"[request] giving up after {retries} retries: {fail_reason}", level="ERROR")
                        yield sse({"error": {"message": fail_reason, "type": "proxy_error"}})
                        yield "data: [DONE]\n\n"
                        return
                    log(f"[request] {fail_reason} -> retry {retries}/{MAX_REQUEST_RETRIES}", level="WARN")
                    await wk.reload_current()
                    continue

                # clean completion
                break

            try:
                if tool_buf is not None:
                    leftover = tool_buf.flush()
                    if leftover:
                        full_answer.append(leftover)
                        yield sse(make_chunk(chunk_id, created, req_model, {"content": leftover}))

                yield sse(make_chunk(chunk_id, created, req_model, {}, finish_reason=finish_reason))
                usage_out = build_usage(messages, full_reasoning, full_answer)
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
                    stopped = await wk.stop_generation()
                    log(f"[stream] client disconnected -> stop button "
                        f"{'clicked' if stopped else 'NOT found'}")
                asyncio.create_task(_stop_and_log())
                raise
            finally:
                log(f"--> done: reasoning={sum(len(x) for x in full_reasoning)}ch "
                    f"answer={sum(len(x) for x in full_answer)}ch "
f"prompt_len={prompt_len} | model={req_model}", level="OK")
                with open("last_response.json", "w", encoding="utf-8") as f:
                    json.dump({"reasoning": "".join(full_reasoning), "answer": "".join(full_answer)},
                              f, ensure_ascii=False, indent=2)
        finally:
            pool.release(wk)

    if stream:
        return StreamingResponse(generate(), media_type="text/event-stream")

    # non-streaming: accumulate (own worker per request, like the stream path)
    wk = await pool.acquire()
    try:
        try:
            await wk.rate_limit()
            await wk.before_request()
        except Exception as e:
            log(f"prepare/send failed: {e}", level="ERROR")
            return JSONResponse({"error": {"message": str(e)}}, status_code=502)

        # Universal retry: ANY failure while NOTHING has accumulated yet ->
        # reload the same account and retry. Once output started a retry would
        # duplicate tokens, so we stop retrying then.
        retries = 0
        while True:
            reasoning_parts, answer_parts = [], []
            raw_answer = ""
            tool_buf = ToolStreamBuffer() if has_tools else None
            fail_reason = None

            try:
                await wk.prepare_chat(req_model)
                await wk.set_thinking(thinking_level)
                await wk.send_message(prompt)
            except Exception as e:
                # a captcha/block during prepare also reloads forever
                if await wk.is_captcha() and CAPTCHA_BYPASS:
                    fail_reason = "captcha appeared"
                else:
                    fail_reason = str(e)

            captcha_event = asyncio.Event()
            stop_event = asyncio.Event()
            monitor = None
            try:
                if fail_reason is None:
                    monitor = asyncio.create_task(wk._monitor_captcha(captcha_event, stop_event))
                    async for phase, delta in wk.stream_tokens():
                        if captcha_event.is_set() and not (reasoning_parts or answer_parts):
                            fail_reason = "captcha appeared"
                            break
                        if phase == "error":
                            fail_reason = delta
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
                if monitor:
                    monitor.cancel()

            if fail_reason is None and captcha_event.is_set() and not (reasoning_parts or answer_parts):
                fail_reason = "captcha appeared"

            if fail_reason:
                if "upstream 413" in fail_reason or "Request Entity Too Large" in fail_reason:
                    # payload over the site limit -> never retry (same result)
                    log(f"[request] {fail_reason}", level="ERROR")
                    return JSONResponse({"error": {"message": fail_reason, "type": "proxy_error"}},
                                        status_code=502)
                if reasoning_parts or answer_parts:
                    # output already produced -> a retry would duplicate tokens
                    return JSONResponse({"error": {"message": fail_reason, "type": "proxy_error"}},
                                        status_code=502)
                if fail_reason == "captcha appeared":
                    # captcha retries forever (as before) - just reload the same
                    # account until the invisible check passes
                    log(f"[request] {fail_reason} -> retry", level="WARN")
                    await wk.reload_current()
                    continue
                retries += 1
                if retries >= MAX_REQUEST_RETRIES:
                    log(f"[request] giving up after {retries} retries: {fail_reason}", level="ERROR")
                    return JSONResponse({"error": {"message": fail_reason}}, status_code=502)
                log(f"[request] {fail_reason} -> retry {retries}/{MAX_REQUEST_RETRIES}", level="WARN")
                await wk.reload_current()
                continue

            break
    finally:
        pool.release(wk)

    content = "".join(answer_parts)
    message = {"role": "assistant", "content": content,
               "reasoning_content": "".join(reasoning_parts)}
    finish = "stop"
    log(f"--> done: reasoning={sum(len(x) for x in reasoning_parts)}ch "
        f"answer={sum(len(x) for x in answer_parts)}ch "
        f"prompt_len={prompt_len} | model={req_model}", level="OK")
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
        "usage": build_usage(messages, reasoning_parts, answer_parts),
    }


async def main():
    pool.start_hider()   # keep worker windows hidden (Windows only, HEADLESS on)
    await run_menu()
    # No global browser here: the pool spawns one browser per active
    # request on demand (see WorkerPool.acquire).
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
        [f"[3] {_tick(ACCOUNT_ROTATE)} Account Rotate", f"[6] {_tick(HEADLESS)} Hide Window", ""],
    ]
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
