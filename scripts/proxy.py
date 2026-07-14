#!/usr/bin/env python3
"""
crabcab - a tiny local bridge so claude code runs on gpt-5.6-sol
using YOUR chatgpt subscription, by borrowing the token codex already saved.

How it fits together:
  claude code  --(anthropic messages api)-->  crabcab  --(openai responses api)-->  chatgpt.com/backend-api/codex
                                                  ^
                                                  |
                            reads ~/.codex/auth.json at runtime (never prints it)

Safety properties (on purpose):
  * binds to 127.0.0.1 ONLY - nothing on any network can reach it.
  * the access token is read fresh from the file per-request and is NEVER logged.
  * zero third-party packages - pure Python standard library.

This is a personal-use interop tool for your own account. Driving a chatgpt
subscription through an unofficial client is tos-gray (account-flag risk) - that's
the tradeoff you chose knowingly.
"""

import os
import stat
import json
import time
import uuid
import base64
import pathlib
import tempfile
import subprocess
import shutil
import hmac
import hashlib
import secrets
import signal
import threading
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows is not a supported target today
    fcntl = None

AUTH_PATH  = pathlib.Path.home() / ".codex" / "auth.json"
BACKEND    = "https://chatgpt.com/backend-api/codex/responses"
TOKEN_URL  = "https://auth.openai.com/oauth/token"
MODEL      = os.environ.get("CRABCAB_MODEL", "gpt-5.6-sol")
EFFORT     = os.environ.get("CRABCAB_EFFORT", "medium")
HOST       = "127.0.0.1"
PORT       = int(os.environ.get("CRABCAB_PORT", "8317"))
ORIGINATOR = "codex_cli_rs"
ROUTING_LOG = pathlib.Path(os.environ.get("CRABCAB_ROUTING_LOG", str(pathlib.Path.home() / "crabcab" / "routing.log")))
MAX_BODY_BYTES = int(os.environ.get("CRABCAB_MAX_BODY_BYTES", str(16 * 1024 * 1024)))
MAX_SSE_LINE_BYTES = int(os.environ.get("CRABCAB_MAX_SSE_LINE_BYTES", str(1024 * 1024)))
MAX_MESSAGES = int(os.environ.get("CRABCAB_MAX_MESSAGES", "256"))
MAX_TOOLS = int(os.environ.get("CRABCAB_MAX_TOOLS", "128"))
MAX_TEXT_CHARS = int(os.environ.get("CRABCAB_MAX_TEXT_CHARS", str(4 * 1024 * 1024)))
MAX_CONCURRENT = int(os.environ.get("CRABCAB_MAX_CONCURRENT", "8"))
MAX_SSE_EVENTS = int(os.environ.get("CRABCAB_MAX_SSE_EVENTS", "200000"))
MAX_OUTPUT_CHARS = int(os.environ.get("CRABCAB_MAX_OUTPUT_CHARS", str(16 * 1024 * 1024)))
MAX_TOOL_ARG_CHARS = int(os.environ.get("CRABCAB_MAX_TOOL_ARG_CHARS", str(4 * 1024 * 1024)))
CURL = shutil.which("curl")
_REFRESH_LOCK = pathlib.Path.home() / ".codex" / ".crabcab-refresh.lock"
_CONCURRENCY = threading.BoundedSemaphore(MAX_CONCURRENT)
def _secure_read(path, require_private=False):
    """Read a small sensitive file, refusing symlinks, non-regular files, and files not owned by us -
    matching on the READ path the O_NOFOLLOW/0600 protections applied on the write path. With
    require_private, also refuse group/other-accessible modes (fail loud, tell the user to chmod)."""
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RuntimeError(f"{path}: not a regular file")
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            raise RuntimeError(f"{path}: not owned by you")
        if require_private and (st.st_mode & 0o077):
            raise RuntimeError(f"{path}: permissions allow group/other access - run: chmod 600 {path}")
        if st.st_size > 1_000_000:
            raise RuntimeError(f"{path}: unexpectedly large")
        return os.read(fd, st.st_size + 1).decode("utf-8", "replace")
    finally:
        os.close(fd)


def _fsync_parent(path):
    """Persist a rename on POSIX filesystems where directory fsync is supported."""
    try:
        fd = os.open(str(pathlib.Path(path).parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _atomic_private_json(path, obj):
    """Write JSON durably with mode 0600, then atomically replace the destination."""
    path = pathlib.Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        _fsync_parent(path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def _refresh_lock():
    """Serialize refreshes across handler threads and sibling crabcab processes."""
    _REFRESH_LOCK.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(_REFRESH_LOCK), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _validate_request(body):
    """Return a user-safe validation error string, or None for an accepted request."""
    if not isinstance(body, dict):
        return "request body must be a json object"
    model = body.get("model")
    if model is not None and not isinstance(model, str):
        return "model must be a string"
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        return "messages must be an array"
    if len(messages) > MAX_MESSAGES:
        return "too many messages"
    tools = body.get("tools")
    if tools is not None and not isinstance(tools, list):
        return "tools must be an array"
    if isinstance(tools, list) and len(tools) > MAX_TOOLS:
        return "too many tools"
    if body.get("stream") is not None and not isinstance(body.get("stream"), bool):
        return "stream must be a boolean"
    total_text = 0
    call_ids = set()
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant", "system", "developer", None):
            return "each message must be an object with a valid role"
        content = m.get("content", [])
        if isinstance(content, str):
            total_text += len(content)
            continue
        if not isinstance(content, list):
            return "message content must be text or an array"
        for block in content:
            if not isinstance(block, dict):
                return "content blocks must be objects"
            typ = block.get("type")
            if typ == "text":
                text = block.get("text", "")
                if not isinstance(text, str):
                    return "text blocks require string text"
                total_text += len(text)
            elif typ == "tool_use":
                cid = block.get("id")
                if not isinstance(cid, str) or not cid or cid in call_ids:
                    return "tool_use ids must be non-empty and unique"
                call_ids.add(cid)
                if not isinstance(block.get("name"), str) or not block.get("name"):
                    return "tool_use requires a name"
            elif typ == "tool_result":
                if not isinstance(block.get("tool_use_id"), str) or not block.get("tool_use_id"):
                    return "tool_result requires tool_use_id"
                c = block.get("content")
                if isinstance(c, str):
                    total_text += len(c)
                elif isinstance(c, list):
                    for x in c:
                        if isinstance(x, dict) and isinstance(x.get("text"), str):
                            total_text += len(x["text"])
            elif typ == "image":
                src = block.get("source")
                if not isinstance(src, dict) or src.get("type") not in ("base64", "url"):
                    return "image source must be base64 or url"
                if src.get("type") == "base64" and len(src.get("data", "")) > MAX_BODY_BYTES:
                    return "image payload too large"
            # any other well-formed block (e.g. thinking / redacted_thinking, which Claude Code MUST
            # replay before a tool_use in the same turn, or a future Anthropic block type) is tolerated
            # and skipped, exactly as v1.1.4 did: the translator ignores what it can't map, and the
            # MAX_BODY_BYTES ceiling still bounds total size. rejecting here 400s real streaming turns.
    if total_text > MAX_TEXT_CHARS:
        return "text content too large"
    if isinstance(tools, list):
        seen = set()
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not tool.get("name"):
                return "each tool requires a non-empty name"
            if tool["name"] in seen:
                return "tool names must be unique"
            seen.add(tool["name"])
            schema = tool.get("input_schema", {})
            if not isinstance(schema, dict):
                return "tool input_schema must be an object"
    return None


def _load_secret():
    """Per-machine local-auth secret so no other local user can ride your subscription. Uses
    CRABCAB_SECRET if set, else reads/creates a random 0600 file at ~/crabcab/.secret. Returns '' only
    if a secret truly cannot be established (unwritable dir, or an existing secret with unsafe perms/
    owner); the server then FAILS LOUD at startup rather than listening in an unusable state."""
    env = os.environ.get("CRABCAB_SECRET")
    if env and env.strip():
        return env.strip()
    p = pathlib.Path.home() / "crabcab" / ".secret"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(p.parent, 0o700)
        except OSError:
            pass
        if p.exists():
            cur = _secure_read(p, require_private=True).strip()
            if cur:
                return cur
        s = secrets.token_urlsafe(32)
        tmp = p.with_suffix(".secret.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(s)
        os.replace(tmp, p)
        return s
    except Exception:
        return ""


SECRET = _load_secret()


def _codex_version():
    """Report the installed codex version so the backend accepts newer models.
    Falls back to a recent known-good version if codex is not found on PATH."""
    try:
        out = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=5).stdout
        for tok in out.replace("codex-cli", "").split():
            if tok and all(c.isdigit() or c == "." for c in tok):
                return tok
    except Exception:
        pass
    env = os.environ.get("CRABCAB_CODEX_VERSION", "0.144.1")
    return env if env and all(c.isdigit() or c == "." for c in env) else "0.144.1"


USER_AGENT = "codex_cli_rs/" + _codex_version()


def _rotate(path, cap=1_000_000):
    """Keep a log file from growing forever: trim to its last half past a size cap."""
    try:
        p = pathlib.Path(path)
        if p.exists() and p.stat().st_size > cap:
            p.write_bytes(p.read_bytes()[-(cap // 2):])
    except Exception:
        pass


def _debug(obj):
    """Append a small structural note to debug.log when CRABCAB_DEBUG is set. No secrets."""
    if not os.environ.get("CRABCAB_DEBUG"):
        return
    try:
        log = pathlib.Path.home() / "crabcab" / "debug.log"
        _rotate(log)
        with log.open("a") as f:
            f.write(json.dumps(obj, default=str)[:4000] + "\n")
    except Exception:
        pass


def load_token():
    """Return (access_token, account_id). Reads fresh each call; never logs."""
    data = json.loads(_secure_read(AUTH_PATH, require_private=True))
    toks = data.get("tokens") or {}
    access = toks.get("access_token")
    account = toks.get("account_id")
    if not access:
        raise RuntimeError(
            "no access_token in ~/.codex/auth.json - run `codex` once to sign in / refresh"
        )
    if any((not c.isprintable()) or c in '"\\' for c in (access + str(account or ""))):
        raise RuntimeError("auth.json token/account has a control or quote char - refusing (possible tampering)")
    return access, account


def _jwt_client_id(token):
    """Pull the oauth client_id out of the access token's JWT claims, if present."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        aud = claims.get("aud")
        if isinstance(aud, list):
            aud = aud[0]
        return claims.get("client_id") or aud
    except Exception:
        return None


def _merge_refreshed(data, new):
    """Pure: fold a token-endpoint response into the existing auth dict, preserving every
    other field. Returns the updated dict, or None if the response carries no access_token."""
    def _clean_tok(v):
        return isinstance(v, str) and v != "" and not any((not c.isprintable()) or c in '"\\' for c in v)
    if not _clean_tok(new.get("access_token")):
        return None
    toks = dict(data.get("tokens") or {})
    toks["access_token"] = new["access_token"]
    if _clean_tok(new.get("refresh_token")):
        toks["refresh_token"] = new["refresh_token"]
    if _clean_tok(new.get("id_token")):
        toks["id_token"] = new["id_token"]
    out = dict(data)
    out["tokens"] = toks
    return out


def try_refresh():
    """Refresh OAuth credentials under an interprocess lock and durable atomic write."""
    if not CURL:
        return False
    try:
        with _refresh_lock():
            data = json.loads(_secure_read(AUTH_PATH, require_private=True))
            toks = data.get("tokens") or {}
            rt, access = toks.get("refresh_token"), toks.get("access_token")
            if not rt or not access:
                return False
            client_id = _jwt_client_id(access) or "app_EMoamEEZ73f0CkXaXp7hrann"
            payload = json.dumps({"client_id": client_id, "grant_type": "refresh_token",
                                  "refresh_token": rt, "scope": "openid profile email offline_access"}).encode()
            bf = tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False)
            try:
                os.fchmod(bf.fileno(), 0o600)
                bf.write(payload)
                bf.flush()
                os.fsync(bf.fileno())
                bf.close()
                pathfwd = bf.name.replace("\\", "/")
                cfg = "\n".join([f'url = "{TOKEN_URL}"', 'request = "POST"',
                                     'header = "Content-Type: application/json"',
                                     f'data-binary = "@{pathfwd}"', 'proto = "=https"',
                                     'tlsv1.2', 'max-redirs = "0"', 'fail-with-body',
                                     'silent', 'show-error', 'max-time = "15"']) + "\n"
                cp = subprocess.run([CURL, "--config", "-"], input=cfg.encode(), capture_output=True, timeout=20, env={"PATH": os.environ.get("PATH", "")})
                if cp.returncode != 0:
                    return False
                new = json.loads(cp.stdout.decode("utf-8", "strict"))
            finally:
                try:
                    bf.close()
                except Exception:
                    pass
                try:
                    os.unlink(bf.name)
                except OSError:
                    pass
            merged = _merge_refreshed(data, new)
            if merged is None:
                return False
            merged["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _atomic_private_json(AUTH_PATH, merged)
            return True
    except Exception:
        return False


def anthropic_to_responses(body):
    """Pure function. Turn a claude code request into a codex Responses request."""
    model = body.get("model") or MODEL
    if model.startswith("claude-gpt-"):
        model = model[len("claude-"):]
    if model in ("sol", "terra", "luna"):
        model = MODEL.rsplit("-", 1)[0] + "-" + model
    if not model.startswith("gpt"):
        model = MODEL
    out = {"model": model, "stream": True, "store": False}

    sys_val = body.get("system")
    if isinstance(sys_val, list):
        out["instructions"] = "\n\n".join(
            b.get("text", "") for b in sys_val if isinstance(b, dict)
        )
    elif isinstance(sys_val, str):
        out["instructions"] = sys_val

    inp = []
    for m in body.get("messages", []):
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for blk in content or []:
            bt = blk.get("type")
            if bt == "text":
                if role == "assistant":
                    out_role, part = "assistant", "output_text"
                elif role == "user":
                    out_role, part = "user", "input_text"
                else:
                    out_role, part = "developer", "input_text"
                inp.append({
                    "type": "message", "role": out_role,
                    "content": [{"type": part, "text": blk.get("text", "")}],
                })
            elif bt == "image":
                src = blk.get("source") or {}
                if src.get("type") == "base64":
                    url = f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
                else:
                    url = src.get("url", "")
                if url:
                    inp.append({"type": "message", "role": "user",
                                "content": [{"type": "input_image", "image_url": url}]})
            elif bt == "tool_use":
                inp.append({
                    "type": "function_call",
                    "call_id": blk.get("id"),
                    "name": blk.get("name"),
                    "arguments": json.dumps(blk.get("input", {})),
                })
            elif bt == "tool_result":
                c = blk.get("content")
                if isinstance(c, list):
                    c = "".join(x.get("text", "") for x in c if isinstance(x, dict))
                inp.append({
                    "type": "function_call_output",
                    "call_id": blk.get("tool_use_id"),
                    "output": c if isinstance(c, str) else json.dumps(c),
                })
    out["input"] = inp

    if body.get("tools"):
        out["tools"] = [{
            "type": "function",
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("input_schema", {}),
            "strict": False,
        } for t in body["tools"]]

    tc = body.get("tool_choice") or {}
    if tc.get("type") == "auto":
        out["tool_choice"] = "auto"
    elif tc.get("type") == "any":
        out["tool_choice"] = "required"
    elif tc.get("type") == "tool":
        out["tool_choice"] = {"type": "function", "name": tc.get("name")}

    out["reasoning"] = {"effort": body.get("effort") or EFFORT}
    return out


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def responses_to_anthropic_events(data_iter, model):
    """Generator. Consume parsed Responses events, yield anthropic SSE strings."""
    msg_id = "msg_" + uuid.uuid4().hex[:24]
    yield _sse("message_start", {"type": "message_start", "message": {
        "id": msg_id, "type": "message", "role": "assistant", "model": model,
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }})

    idx_of = {}          # upstream output_index -> our anthropic content-block index
    next_idx = 0
    stop_reason = "end_turn"
    out_tokens = 0
    output_chars = 0
    tool_arg_chars = {}
    completed = False

    for d in data_iter:
        t = d.get("type", "")
        oi = d.get("output_index", 0)

        if completed:
            raise ValueError("upstream emitted events after completion")

        if t == "response.output_item.added":
            item = d.get("item", {}) or {}
            if item.get("type") == "function_call" and oi not in idx_of:
                idx_of[oi] = next_idx
                next_idx += 1
                stop_reason = "tool_use"
                yield _sse("content_block_start", {
                    "type": "content_block_start", "index": idx_of[oi],
                    "content_block": {
                        "type": "tool_use",
                        "id": item.get("call_id") or item.get("id"),
                        "name": item.get("name"), "input": {},
                    },
                })

        elif t == "response.output_text.delta":
            delta = d.get("delta", "")
            if not isinstance(delta, str):
                raise ValueError("upstream text delta was not a string")
            output_chars += len(delta)
            if output_chars > MAX_OUTPUT_CHARS:
                raise ValueError("upstream output exceeded safety limit")
            if oi not in idx_of:
                idx_of[oi] = next_idx
                next_idx += 1
                yield _sse("content_block_start", {
                    "type": "content_block_start", "index": idx_of[oi],
                    "content_block": {"type": "text", "text": ""},
                })
            yield _sse("content_block_delta", {
                "type": "content_block_delta", "index": idx_of[oi],
                "delta": {"type": "text_delta", "text": delta},
            })

        elif t == "response.function_call_arguments.delta":
            delta = d.get("delta", "")
            if not isinstance(delta, str):
                raise ValueError("upstream tool delta was not a string")
            tool_arg_chars[oi] = tool_arg_chars.get(oi, 0) + len(delta)
            if tool_arg_chars[oi] > MAX_TOOL_ARG_CHARS:
                raise ValueError("upstream tool arguments exceeded safety limit")
            i = idx_of.get(oi)
            if i is None and len(idx_of) == 1:      # tolerate events that omit output_index
                i = next(iter(idx_of.values()))
            if i is not None:
                yield _sse("content_block_delta", {
                    "type": "content_block_delta", "index": i,
                    "delta": {"type": "input_json_delta", "partial_json": delta},
                })

        elif t == "response.output_item.done":
            i = idx_of.pop(oi, None)
            if i is None and len(idx_of) == 1:
                i = idx_of.pop(next(iter(idx_of)))
            if i is not None:
                yield _sse("content_block_stop", {"type": "content_block_stop", "index": i})

        elif t == "response.completed":
            completed = True
            usage = (d.get("response", {}) or {}).get("usage", {}) or {}
            out_tokens = usage.get("output_tokens", 0) or 0

    for oi in list(idx_of):     # close anything the upstream never finished (incomplete/aborted)
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx_of.pop(oi)})

    yield _sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": out_tokens},
    })
    yield _sse("message_stop", {"type": "message_stop"})


def _curl_backend(payload_bytes, access, account):
    """POST to the codex backend via curl, streaming SSE on stdout, retrying
    transient errors with backoff. Token goes via stdin config, never argv/disk.
    Returns (proc, body_temp_path)."""
    bf = tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False)
    bf.write(payload_bytes)
    bf.close()
    pathfwd = bf.name.replace("\\", "/")
    config = "\n".join([
        f'url = "{BACKEND}"',
        'request = "POST"',
        f'header = "Authorization: Bearer {access}"',
        f'header = "ChatGPT-Account-Id: {account or ""}"',
        'header = "Content-Type: application/json"',
        'header = "Accept: text/event-stream"',
        f'header = "originator: {ORIGINATOR}"',
        f'user-agent = "{USER_AGENT}"',
        f'data-binary = "@{pathfwd}"',
        'proto = "=https"',
        'tlsv1.2',
        'fail-with-body',
        'retry = "2"',
        'retry-delay = "1"',
        'retry-connrefused',
        'max-redirs = "0"',
        'connect-timeout = "30"',
        'max-time = "600"',
        'speed-limit = "1"',
        'speed-time = "180"',
        'silent',
        'show-error',
        'no-buffer',
    ]) + "\n"
    if not CURL:
        raise RuntimeError("curl executable not found")
    proc = subprocess.Popen(
        [CURL, "--config", "-"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, close_fds=True,
        env={"PATH": os.environ.get("PATH", "")},
    )
    proc.stdin.write(config.encode())
    proc.stdin.close()
    return proc, bf.name


def _iter_sse(resp):
    """Incrementally parse SSE without ever allowing an unbounded readline allocation."""
    buf = bytearray()
    events = 0
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > MAX_SSE_LINE_BYTES and b"\n" not in buf:
            raise ValueError("upstream SSE line exceeded safety limit")
        while True:
            pos = buf.find(b"\n")
            if pos < 0:
                break
            raw = bytes(buf[:pos + 1])
            del buf[:pos + 1]
            if len(raw) > MAX_SSE_LINE_BYTES:
                raise ValueError("upstream SSE line exceeded safety limit")
            line = raw.decode("utf-8", "strict").rstrip("\r\n")
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            event = json.loads(payload)
            if not isinstance(event, dict):
                raise ValueError("upstream SSE data must be an object")
            events += 1
            if events > MAX_SSE_EVENTS:
                raise ValueError("upstream SSE event limit exceeded")
            yield event
    if buf:
        if len(buf) > MAX_SSE_LINE_BYTES:
            raise ValueError("upstream SSE line exceeded safety limit")
        line = bytes(buf).decode("utf-8", "strict").rstrip("\r\n")
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                event = json.loads(payload)
                if not isinstance(event, dict):
                    raise ValueError("upstream SSE data must be an object")
                yield event


class _PrefixedReader:
    """Replay an already-sniffed first line, then hand off to the raw pipe.
    _iter_sse reads byte chunks; the old itertools.chain wrapper was a line
    iterator with no .read(), so every live stream died (v1.2.0 regression)."""
    def __init__(self, first, f):
        self._first = first
        self._f = f

    def read(self, n=-1):
        if self._first:
            out, self._first = self._first, b""
            return out
        return self._f.read(n)


class Handler(BaseHTTPRequestHandler):
    timeout = 20

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        self.close_connection = True
        b = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b)

    def _finish(self, proc, body_path):
        try:
            os.unlink(body_path)
        except OSError:
            pass
        try:
            proc.stdout.close()
        except Exception:
            pass
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                proc.wait(timeout=2)
            except Exception:
                pass

    def _not_browser(self):
        """Reject browser-driven requests (DNS-rebinding / CSRF). A rebind arrives with a non-loopback
        Host; a cross-origin request carries Origin or a cross-site Sec-Fetch-Site. The claude cli (and
        the launcher's challenge probe) send none of these."""
        raw_host = self._single_header("Host")
        if raw_host is None:
            return False
        host = raw_host.rsplit(":", 1)[0].strip("[]").lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            return False
        if self.headers.get("Origin") or self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            return False
        return True

    def _local_only(self):
        """Full gate: not browser-driven AND presents the per-machine secret (x-api-key / bearer),
        compared in constant time. Fail-closed: an empty SECRET denies everything."""
        if not self._not_browser():
            return False
        api_values = self.headers.get_all("x-api-key") or []
        auth_values = self.headers.get_all("Authorization") or []
        if len(api_values) > 1 or len(auth_values) > 1:
            return False
        got = api_values[0] if api_values else ""
        if not got:
            bearer = auth_values[0] if auth_values else ""
            got = bearer[7:] if bearer.startswith("Bearer ") else ""
        if not (SECRET and hmac.compare_digest(got.encode("utf-8", "ignore"), SECRET.encode("utf-8"))):
            return False
        return True

    def _deny(self):
        self._json(403, {"type": "error", "error": {"type": "forbidden", "message": "local requests only"}})

    def _single_header(self, name):
        values = self.headers.get_all(name) or []
        return values[0] if len(values) == 1 else None

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/v1/crabcab-id":
            if not self._not_browser():
                return self._deny()
            n = parse_qs(urlsplit(self.path).query).get("n", [""])[0]
            if not (16 <= len(n) <= 256 and n.isascii() and n.isprintable()):
                return self._json(400, {"type": "error", "error": {"type": "invalid_request", "message": "invalid challenge"}})
            mac = hmac.new(SECRET.encode(), n.encode(), hashlib.sha256).hexdigest() if SECRET else ""
            return self._json(200, {"id": mac})
        if not self._local_only():
            return self._deny()
        if path == "/v1/models":
            return self._json(200, {
                "data": [
                    {"type": "model", "id": "claude-gpt-5.6-sol", "display_name": "gpt-5.6-sol - best, most polished", "created_at": "2025-01-01T00:00:00Z"},
                    {"type": "model", "id": "claude-gpt-5.6-terra", "display_name": "gpt-5.6-terra - faster, cheaper", "created_at": "2025-01-01T00:00:00Z"},
                    {"type": "model", "id": "claude-gpt-5.6-luna", "display_name": "gpt-5.6-luna - fastest, lightest", "created_at": "2025-01-01T00:00:00Z"},
                ],
                "has_more": False,
                "first_id": "claude-gpt-5.6-sol",
                "last_id": "claude-gpt-5.6-luna",
            })
        return self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": "not found"}})

    def do_POST(self):
        if self.path.startswith(("http://", "https://")):
            return self._json(400, {"type": "error", "error": {"type": "invalid_request", "message": "absolute-form request target rejected"}})
        if not self._local_only():
            return self._deny()
        path = urlsplit(self.path).path
        if path not in ("/v1/messages", "/v1/messages/count_tokens"):
            return self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": "not found"}})

        ctype = self._single_header("Content-Type")
        if ctype is None or ctype.split(";", 1)[0].strip().lower() != "application/json":
            return self._json(415, {"type": "error", "error": {"type": "invalid_request", "message": "content-type must be application/json"}})
        if self.headers.get("Transfer-Encoding"):
            return self._json(400, {"type": "error", "error": {"type": "invalid_request", "message": "transfer-encoding not supported"}})
        cls = self.headers.get_all("Content-Length") or []
        if len(cls) != 1:
            return self._json(400, {"type": "error", "error": {"type": "invalid_request", "message": "exactly one content-length required"}})
        try:
            length = int(cls[0])
            if length < 0 or length > MAX_BODY_BYTES:
                return self._json(413, {"type": "error", "error": {"type": "too_large", "message": "request body too large"}})
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            return self._json(400, {"type": "error", "error": {"type": "invalid_request", "message": "malformed request body"}})

        validation_error = _validate_request(body)
        if validation_error:
            return self._json(400, {"type": "error", "error": {"type": "invalid_request", "message": validation_error}})

        if path == "/v1/messages/count_tokens":
            self._json(200, {"input_tokens": max(1, len(json.dumps(body)) // 4)})
            return

        try:
            req = anthropic_to_responses(body)
        except Exception:
            return self._json(400, {"type": "error", "error": {"type": "invalid_request", "message": "could not translate request"}})
        try:
            _rotate(ROUTING_LOG, 200_000)
            model_for_log = "".join(c for c in req.get("model", "?") if c.isprintable())[:120]
            with ROUTING_LOG.open("a") as f:
                f.write(time.strftime("%H:%M:%S") + " " + model_for_log + "\n")
        except Exception:
            pass
        _debug({
            "path": self.path,
            "incoming_system": type(body.get("system")).__name__,
            "incoming_msg_roles": [m.get("role") for m in body.get("messages", [])],
            "outgoing_input_roles": [(it.get("role") or it.get("type")) for it in req.get("input", [])],
        })
        payload = json.dumps(req).encode()

        for attempt in range(2):
            try:
                access, account = load_token()
            except Exception:
                self._json(500, {"type": "error", "error": {"type": "api_error", "message": "could not read Codex credentials; run crabcab-doctor"}})
                return
            try:
                proc, body_path = _curl_backend(payload, access, account)
            except Exception:
                self._json(502, {"type": "error", "error": {"type": "api_error", "message": "could not start the upstream transport"}})
                return

            first = proc.stdout.readline(MAX_SSE_LINE_BYTES + 1)
            if first and first[:1] in (b"e", b"d", b":", b"\n"):
                stream = _PrefixedReader(first, proc.stdout)
                events = responses_to_anthropic_events(_iter_sse(stream), req["model"])
                try:
                    if body.get("stream"):
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.end_headers()
                        for chunk in events:
                            self.wfile.write(chunk.encode())
                            self.wfile.flush()
                    else:
                        self._json(200, _collapse(events, req["model"]))
                finally:
                    self._finish(proc, body_path)
                return

            if first == b"":
                detail = "upstream sent nothing. curl: " + proc.stderr.read(65536).decode("utf-8", "replace")[:600]
            else:
                detail = (first + proc.stdout.read(65536)).decode("utf-8", "replace")[:800]
            self._finish(proc, body_path)
            low = detail.lower()

            if attempt == 0 and any(k in low for k in ("401", "unauthorized", "expired", "invalid_token", "invalid authentication")):
                if try_refresh():
                    continue
                self._json(401, {"type": "error", "error": {"type": "authentication_error",
                    "message": "your codex login expired and auto-refresh didn't take. run `codex` once to re-auth."}})
                return
            if any(k in low for k in ("rate limit", "429", "usage limit", "too many requests")):
                self._json(429, {"type": "error", "error": {"type": "rate_limit_error",
                    "message": "you've hit your chatgpt limit. run `crabcab-usage` for the reset time (and any banked resets)."}})
                return
            _debug({"upstream_error": detail[:2000]})
            self._json(502, {"type": "error", "error": {"type": "api_error", "message": "upstream returned an unexpected response"}})
            return

        self._json(502, {"type": "error", "error": {"type": "api_error",
            "message": "upstream still failing after a refresh - run `codex` to re-auth."}})


def _collapse(events, model):
    """Turn our anthropic SSE events back into one non-streaming Message object."""
    by_index, order, stop_reason, out_tokens = {}, [], "end_turn", 0
    for chunk in events:
        try:
            data = json.loads(chunk.split("data: ", 1)[1])
        except Exception:
            continue
        t = data.get("type")
        if t == "content_block_start":
            i = data.get("index")
            b = dict(data.get("content_block") or {})
            if b.get("type") == "tool_use":
                b["_json"] = ""
            if i not in by_index:
                order.append(i)
            by_index[i] = b
        elif t == "content_block_delta":
            b = by_index.get(data.get("index"))
            if b is None:
                continue
            d = data.get("delta") or {}
            if d.get("type") == "text_delta":
                b["text"] = b.get("text", "") + d.get("text", "")
            elif d.get("type") == "input_json_delta":
                b["_json"] = b.get("_json", "") + d.get("partial_json", "")
        elif t == "message_delta":
            stop_reason = (data.get("delta") or {}).get("stop_reason", stop_reason)
            out_tokens = (data.get("usage") or {}).get("output_tokens", out_tokens)
    blocks = []
    for i in order:
        b = by_index.get(i)
        if b is None:
            continue
        if "_json" in b:
            try:
                b["input"] = json.loads(b.pop("_json") or "{}")
            except Exception:
                b.pop("_json", None)
                b["input"] = {}
        blocks.append(b)
    return {
        "id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant",
        "model": model, "content": blocks, "stop_reason": stop_reason,
        "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": out_tokens},
    }


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def process_request(self, request, client_address):
        if not _CONCURRENCY.acquire(blocking=False):
            try:
                request.close()
            finally:
                return
        try:
            super().process_request(request, client_address)
        except Exception:
            _CONCURRENCY.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            _CONCURRENCY.release()

    def handle_error(self, request, client_address):
        # never stream a traceback into the (unrotated) log - a hostile local caller could otherwise
        # crash-loop the handler to fill the disk. each request is its own thread, so we just drop it.
        pass


if __name__ == "__main__":
    os.umask(0o077)
    if not CURL:
        raise SystemExit("crabcab refuses to start: curl was not found on PATH")
    if not (1024 <= PORT <= 65535):
        raise SystemExit("crabcab refuses to start: CRABCAB_PORT must be between 1024 and 65535")
    if min(MAX_BODY_BYTES, MAX_SSE_LINE_BYTES, MAX_MESSAGES, MAX_TOOLS, MAX_TEXT_CHARS, MAX_CONCURRENT) <= 0:
        raise SystemExit("crabcab refuses to start: safety limits must be positive")
    if HOST not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(f"crabcab refuses to start: HOST is {HOST!r}, not loopback. this must stay local.")
    if not SECRET:
        raise SystemExit("crabcab refuses to start: could not establish a local secret at ~/crabcab/.secret "
                         "(check that ~/crabcab is writable and owned by you). failing loud, not open.")
    print(f"crabcab listening on http://{HOST}:{PORT}  (model: {MODEL}, effort: {EFFORT})")
    print(f"    point claude code at it:  ANTHROPIC_BASE_URL=http://{HOST}:{PORT}")
    _Server((HOST, PORT), Handler).serve_forever()
