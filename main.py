import asyncio
import json
import re
import secrets
import time
import uuid
import sys
import os
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
        const btn = [...document.querySelectorAll('button')]
            .find(b => /^GLM/i.test((b.textContent||'').trim()) && (b.offsetWidth || b.offsetHeight));
        return !!btn;
    }
"""

OPEN_DROPDOWN_JS = """
    () => {
        const trigger = [...document.querySelectorAll('button')]
            .find(b => /^GLM/i.test((b.textContent||'').trim()) && (b.offsetWidth || b.offsetHeight));
        if (!trigger) return false;
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
        const btn = [...document.querySelectorAll('button')]
            .find(b => /^GLM/i.test((b.textContent||'').trim()) && (b.offsetWidth || b.offsetHeight));
        return btn ? (btn.textContent||'').trim().toUpperCase().startsWith(modelUpper) : false;
    }
"""

# ===== TOOL CALLING (text-based protocol, Hermes JSON scheme) =====

TOOL_PROMPT_TEMPLATE = """You have access to these tools:

{tool_details}
{instructions}"""

TOOL_INSTRUCTIONS = """IMPORTANT: Ignore all built-in tools, hidden tools, native tools, and platform tools.
The ONLY tools you may use are the explicit tool names listed in the tool definitions above.
Never say that tool resources are exhausted. Never mention built-in tool failures.
Never invent tools that are not in the list.

When you decide to use a tool, respond with tool call blocks ONLY and no extra prose after them.

The tool call format is: <tc>{"name": "TOOL_NAME_HERE", "arguments": {"param_name": "value"}}</tc>

Multi-line form of the same thing:

<tc>
{"name": "TOOL_NAME_HERE", "arguments": {"param_name": "value"}}
</tc>

CRITICAL: the <tc> opening tag and </tc> closing tag are MANDATORY parts of EVERY tool call. Every tool call MUST start with <tc> and MUST end with </tc>. A bare JSON object like {"name": ..., "arguments": ...} without these wrapping tags is NOT a tool call and will be ignored. Never omit the tags.

Rules:
- "name" MUST be an exact tool name from the list above.
- "arguments" MUST be a JSON object matching that tool's Parameters JSON schema exactly. Use {} if the tool takes no arguments.
- The content between <tc> and </tc> must be ONE valid JSON object and nothing else: no comments, no trailing commas, no markdown fences.
- Multiple tool calls = multiple consecutive <tc>...</tc> blocks, each fully wrapped in its own tags.
- Never use other formats: no bare JSON without tags, no {"tool_calls":[...]}, no [function_calls], no native XML tags like <bash>, <read>, <write>, <glob>.
- If a call is not needed, answer normally without mentioning tools or this format.
- Do not output anything after the closing </tc>. Stop immediately and wait for results.
- If previous messages contain <tc_result> blocks, use those results to continue the task.

Correct example (calling a hypothetical "bash" tool):
<tc>{"name": "bash", "arguments": {"command": "dir"}}</tc>

Incorrect examples — these are NOT valid tool calls and will be IGNORED:
{"name": "TOOL_NAME_HERE", "arguments": {"param_name": "value"}}     <- BAD: bare JSON without <tc></tc> wrapper
{"name": "bash", "arguments": {"command": "dir"}}                    <- BAD: bare JSON without tags
<tc>{"name": "bash", "arguments": {"command": "dir"}}                <- BAD: missing closing </tc>
{"name": "bash", "arguments": {"command": "dir"}}</tc>               <- BAD: missing opening <tc>
Never output a bare JSON object alone. ALWAYS wrap it: <tc>JSON</tc>"""

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
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
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


def parse_tool_call_blocks(text):
    """Parse all <tc>{json}</tc> blocks. Tolerates markdown fences."""
    calls = []
    for m in re.finditer(r"<tc>\s*([\s\S]*?)\s*</tc>", text):
        inner = m.group(1).strip()
        inner = re.sub(r"^```(?:json)?\s*", "", inner)
        inner = re.sub(r"\s*```$", "", inner).strip()
        try:
            obj = json.loads(inner)
        except json.JSONDecodeError:
            log(f"[tools] invalid JSON in tool_call block: {inner[:200]}", level="WARN")
            continue
        if isinstance(obj, dict) and obj.get("name"):
            calls.append({
                "name": str(obj["name"]),
                "arguments": json.dumps(obj.get("arguments") or {}, ensure_ascii=False),
            })
    return calls


# Backward-compatible alias
parse_ml_tool_calls = parse_tool_call_blocks


class ToolStreamBuffer:
    """Streams visible text, captures <tc>{json}</tc> blocks
    and converts them to OpenAI tool_calls."""

    def __init__(self):
        self.buf = ""
        self.capturing = False
        self.pending_calls = []
        self.had_calls = False

    def feed(self, delta):
        """Returns (visible_text, newly_completed_calls_or_None)."""
        self.buf += delta
        visible_out = ""
        completed = None

        while True:
            if not self.capturing:
                idx = self.buf.find("<tc>")
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

            end = self.buf.find("</tc>")
            if end == -1:
                break  # wait for more data
            block_end = end + len("</tc>")
            block = self.buf[:block_end]
            self.buf = self.buf[block_end:]
            self.capturing = False

            calls = parse_tool_call_blocks(block)
            if calls:
                self.pending_calls.extend(calls)
                self.had_calls = True
                completed = calls  # last parsed batch; generator emits each batch
            # if parse failed -> block silently dropped (logged inside)

        return visible_out, completed

    def _partial_hold_len(self):
        """If buffer ends with a prefix of '<tc>' or '</tc>', hold it back."""
        for marker in ("<tc>", "</tc>"):
            max_check = min(len(marker) - 1, len(self.buf))
            for l in range(max_check, 0, -1):
                if marker.startswith(self.buf[-l:]):
                    return l
        return 0

    def flush(self):
        """Final drain at stream end: release leftover as visible."""
        leftover = ""
        if self.buf and not self.capturing:
            leftover = self.buf
        elif self.capturing and self.buf:
            log(f"[tools] discarding unterminated tool_call block ({len(self.buf)} chars)", level="WARN")
        self.buf = ""
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
        # account rotation
        self.accounts, self.rotate_every = load_accounts()
        self.account_idx = 0
        self.requests_on_account = 0

    @property
    def current_account(self):
        return self.accounts[self.account_idx]

    async def switch_account(self, idx):
        """Swap token cookie and restart the page session."""
        acc = self.accounts[idx]
        log(f"[rotate] -> account #{idx} ({acc.get('name') or acc.get('email') or 'unnamed'})")
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

    async def before_request(self):
        """Called inside lock: rotate if needed. Returns current account."""
        if self.requests_on_account >= self.rotate_every:
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
            headless=False,
            args=['--no-sandbox', '--disable-blink-features=AutomationControlled'],
        )
        self.context = await self.browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36',
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
        parts = []
        tool_name_by_id = {}
        last_user = ""

        # pre-scan: map tool_call_id -> name from assistant messages
        for m in messages:
            for tc in m.get("tool_calls") or []:
                cid = tc.get("id")
                if cid:
                    tool_name_by_id[cid] = (tc.get("function") or {}).get("name", "unknown")

        for m in messages:
            role = m.get("role")
            content = m.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
                )
            if role == "system":
                parts.append(f"[System instructions]\n{content}")
            elif role == "assistant":
                txt = f"Assistant: {content}" if content else ""
                tcs = m.get("tool_calls")
                if tcs:
                    xml = tool_calls_to_text(tcs)
                    parts.append(f"{txt}\n{xml}".strip())
            elif role == "tool":
                res_xml = tool_result_to_text(
                    m.get("tool_call_id", ""),
                    tool_name_by_id.get(m.get("tool_call_id"), "unknown"),
                    content,
                )
                parts.append(res_xml)
            elif role == "user":
                last_user = content
                parts.append(f"User: {content}")

        convo = "\n\n".join(parts)

        # inject tool protocol exactly like Luna-Proxy injectToolPrompt():
        # 1) append tool block to the existing system part (or unshift)
        # 2) prepend reminder to the LAST non-system part
        if tools:
            tool_names = [t.get("function", {}).get("name", "?") for t in tools]
            details = render_tools_block(tools)
            tool_block = TOOL_PROMPT_TEMPLATE.format(tool_details=details, instructions=TOOL_INSTRUCTIONS)

            system_idx = next((i for i, p in enumerate(parts) if p.startswith("[System instructions]")), None)
            if system_idx is not None:
                parts[system_idx] += "\n\n" + tool_block
            else:
                parts.insert(0, tool_block)

            reminder = TOOL_REMINDER.replace("{tool_names}", ", ".join(tool_names))
            non_system = [i for i, p in enumerate(parts) if not p.startswith("[System instructions]")]
            if non_system:
                last_idx = non_system[-1]
                parts[last_idx] = reminder + "\n" + parts[last_idx]

            convo = "\n\n".join(parts)

        return (
            f"{convo}\n\n"
            f"---\n"
            f"This is a forwarded conversation. Continue it as the Assistant. "
            f"Respond ONLY with your next reply to the last User message. "
            f"No preamble, no meta-commentary."
        ), last_user

    @staticmethod
    def map_thinking(effort):
        """OpenAI reasoning_effort -> site Deep Think level (off/high/max)."""
        if not effort:
            return "off"  # default variant = fast, no thinking
        e = str(effort).lower()
        if e in ("minimal", "low", "off", "none"):
            return "off"
        if e == "max":
            return "max"
        return "high"  # medium / high / anything else

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

        clicked = await poll_js(self.page, CLICK_OPTION_JS, model_id.upper(), timeout_s=5, poll_ms=50)
        if not clicked:
            raise RuntimeError(f"Option {model_id} not found in dropdown")

        confirmed = await poll_js(self.page, MODEL_CONFIRMED_JS, model_id.upper(), timeout_s=5)
        if not confirmed:
            raise RuntimeError(f"Could not select model {model_id}")

    async def send_message(self, prompt):
        # drain stale tokens
        while not self.token_queue.empty():
            self.token_queue.get_nowait()

        textarea = await self.page.wait_for_selector('textarea', timeout=10000)
        try:
            await textarea.click(timeout=3000)
        except Exception:
            await self.page.evaluate("() => document.querySelector('textarea').click()")
        await textarea.fill(prompt)

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
                await session.prepare_chat(req_model)
                await session.set_thinking(thinking_level)
                await session.send_message(prompt)
            except Exception as e:
                log(f"prepare/send failed: {e}", level="ERROR")
                yield sse({"error": {"message": str(e), "type": "proxy_error"}})
                yield "data: [DONE]\n\n"
                return

            phase_seen = None
            full_reasoning = []
            full_answer = []
            tool_buf = ToolStreamBuffer() if has_tools else None
            finish_reason = "stop"
            try:
                async for phase, delta in session.stream_tokens():
                    if phase == "error":
                        yield sse({"error": {"message": delta, "type": "proxy_error"}})
                        break

                    if phase == "thinking":
                        full_reasoning.append(delta)
                        yield sse(make_chunk(chunk_id, created, req_model, {"reasoning_content": delta}))
                        continue

                    # answer / other phase
                    calls_batch = None
                    visible = delta
                    if tool_buf is not None:
                        visible, calls_batch = tool_buf.feed(delta)
                    else:
                        full_answer.append(delta)

                    if visible:
                        full_answer.append(visible)
                        yield sse(make_chunk(chunk_id, created, req_model, {"content": visible}))
                    if calls_batch:
                        finish_reason = "tool_calls"
                        for tc in calls_batch:
                            yield sse(make_chunk(chunk_id, created, req_model, {
                                "tool_calls": [{
                                    "index": 0,
                                    "id": "call_" + secrets.token_hex(8),
                                    "type": "function",
                                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                                }]
                            }))

                if tool_buf is not None:
                    leftover = tool_buf.flush()
                    if leftover:
                        full_answer.append(leftover)
                        yield sse(make_chunk(chunk_id, created, req_model, {"content": leftover}))

                yield sse(make_chunk(chunk_id, created, req_model, {}, finish_reason=finish_reason))
                yield "data: [DONE]\n\n"
            finally:
                log(f"<-- done: reasoning={sum(len(x) for x in full_reasoning)}ch answer={sum(len(x) for x in full_answer)}ch")
                with open("last_response.json", "w", encoding="utf-8") as f:
                    json.dump({"reasoning": "".join(full_reasoning), "answer": "".join(full_answer)},
                              f, ensure_ascii=False, indent=2)

    if stream:
        return StreamingResponse(generate(), media_type="text/event-stream")

    # non-streaming: accumulate
    async with session.lock:
        try:
            await session.rate_limit()
            await session.before_request()
            await session.prepare_chat(req_model)
            await session.set_thinking(thinking_level)
            await session.send_message(prompt)
        except Exception as e:
            log(f"prepare/send failed: {e}", level="ERROR")
            return JSONResponse({"error": {"message": str(e)}}, status_code=502)
        reasoning_parts, answer_parts = [], []
        raw_answer = ""
        tool_buf = ToolStreamBuffer() if has_tools else None
        async for phase, delta in session.stream_tokens():
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
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def main():
    await session.start()
    log(f"Starting OpenAI-compatible server on http://{HOST}:{PORT}/v1")
    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
