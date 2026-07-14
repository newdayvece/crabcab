#!/usr/bin/env python3
"""Offline self-test. Exercises the translation logic with fake data.
Touches no network, no token, no openai. Just proves the machinery maps correctly."""

import os
import json
import proxy as proxy_module
from proxy import anthropic_to_responses, responses_to_anthropic_events, _collapse, _jwt_client_id, _merge_refreshed, _secure_read, _validate_request, _iter_sse, _PrefixedReader, MAX_SSE_LINE_BYTES

fails = []
def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        fails.append(name)

anthropic_req = {
    "model": "gpt-5.6-sol",
    "max_tokens": 1024,
    "system": "You are the crab.",
    "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "NYC"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": "72F"},
        ]},
    ],
    "tools": [{"name": "get_weather", "description": "weather",
               "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}],
    "tool_choice": {"type": "auto"},
}
r = anthropic_to_responses(anthropic_req)
print("\n[request translation]")
check("system -> instructions", r["instructions"] == "You are the crab.")
check("tools flattened w/ parameters", r["tools"][0]["parameters"]["properties"]["city"]["type"] == "string")
check("tool_choice auto", r["tool_choice"] == "auto")
check("reasoning.effort set", r["reasoning"]["effort"] in ("low", "medium", "high", "xhigh", "max"))
check("max_output_tokens dropped (Codex backend rejects it)", "max_output_tokens" not in r)
check("user text -> input_text", r["input"][0]["content"][0]["type"] == "input_text")
check("tool_use -> function_call", r["input"][1]["type"] == "function_call" and r["input"][1]["call_id"] == "call_1")
check("tool_result -> function_call_output", r["input"][2]["type"] == "function_call_output" and r["input"][2]["output"] == "72F")

sysmsg = anthropic_to_responses({
    "model": "gpt-5.6-sol",
    "system": "top-level system",
    "messages": [
        {"role": "user", "content": "hi"},
        {"role": "system", "content": [{"type": "text", "text": "be brief"}]},
    ],
})
roles = [it.get("role") for it in sysmsg["input"] if it.get("type") == "message"]
print("\n[system-message handling]  input roles:", roles)
check("top-level system -> instructions", sysmsg.get("instructions") == "top-level system")
check("role:system message -> developer, never 'system'", "developer" in roles and "system" not in roles)

fake_stream = [
    {"type": "response.created", "response": {"id": "resp_1"}},
    {"type": "response.output_item.added", "output_index": 0, "item": {"type": "message", "role": "assistant"}},
    {"type": "response.output_text.delta", "delta": "Hello"},
    {"type": "response.output_text.delta", "delta": " world"},
    {"type": "response.output_item.done", "item": {"type": "message"}},
    {"type": "response.output_item.added", "output_index": 1, "item": {"type": "function_call", "call_id": "call_x", "name": "get_weather"}},
    {"type": "response.function_call_arguments.delta", "delta": "{\"city\":"},
    {"type": "response.function_call_arguments.delta", "delta": "\"NYC\"}"},
    {"type": "response.output_item.done", "item": {"type": "function_call"}},
    {"type": "response.completed", "response": {"usage": {"output_tokens": 12}}},
]
events = [json.loads(c.split("data: ", 1)[1]) for c in responses_to_anthropic_events(iter(fake_stream), "gpt-5.6-sol")]
types = [e["type"] for e in events]
print("\n[response translation]  sequence:", " -> ".join(types))
check("starts with message_start", types[0] == "message_start")
check("has a text block", any(e["type"] == "content_block_start" and e["content_block"]["type"] == "text" for e in events))
text = "".join(e["delta"]["text"] for e in events if e["type"] == "content_block_delta" and e["delta"]["type"] == "text_delta")
check("text reassembles to 'Hello world'", text == "Hello world")
check("has a tool_use block named get_weather", any(e["type"] == "content_block_start" and e["content_block"].get("name") == "get_weather" for e in events))
args = "".join(e["delta"]["partial_json"] for e in events if e["type"] == "content_block_delta" and e["delta"]["type"] == "input_json_delta")
check("tool args reassemble to valid json {city:NYC}", json.loads(args) == {"city": "NYC"})
check("stop_reason is tool_use", any(e["type"] == "message_delta" and e["delta"]["stop_reason"] == "tool_use" for e in events))
check("ends with message_stop", types[-1] == "message_stop")

msg = _collapse(responses_to_anthropic_events(iter(fake_stream), "gpt-5.6-sol"), "gpt-5.6-sol")
print("\n[non-streaming collapse]")
check("collapsed message has role assistant", msg["role"] == "assistant")
check("collapsed text block present", any(b["type"] == "text" and b["text"] == "Hello world" for b in msg["content"]))
check("collapsed tool_use input parsed", any(b["type"] == "tool_use" and b["input"] == {"city": "NYC"} for b in msg["content"]))

print("\n[parallel + hostile tool-call streams]")
multi = [
    {"type": "response.output_item.added", "output_index": 0, "item": {"type": "function_call", "call_id": "c0", "name": "alpha"}},
    {"type": "response.output_item.added", "output_index": 1, "item": {"type": "function_call", "call_id": "c1", "name": "beta"}},
    {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": "{\"a\":"},
    {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": "{\"b\":"},
    {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": "1}"},
    {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": "2}"},
    {"type": "response.output_item.done", "output_index": 0, "item": {"type": "function_call"}},
    {"type": "response.output_item.done", "output_index": 1, "item": {"type": "function_call"}},
    {"type": "response.completed", "response": {"usage": {"output_tokens": 5}}},
]
mm = _collapse(responses_to_anthropic_events(iter(multi), "m"), "m")
tools = [b for b in mm["content"] if b["type"] == "tool_use"]
check("two parallel tool calls kept as separate blocks", len(tools) == 2)
check("interleaved args routed to the right call (alpha -> {a:1})", any(b["name"] == "alpha" and b["input"] == {"a": 1} for b in tools))
check("interleaved args routed to the right call (beta -> {b:2})", any(b["name"] == "beta" and b["input"] == {"b": 2} for b in tools))

bad = [
    {"type": "response.output_item.added", "output_index": 0, "item": {"type": "function_call", "call_id": "c", "name": "x"}},
    {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": "{not valid json"},
    {"type": "response.output_item.done", "output_index": 0, "item": {"type": "function_call"}},
    {"type": "response.completed", "response": {"usage": {"output_tokens": 1}}},
]
mb = _collapse(responses_to_anthropic_events(iter(bad), "m"), "m")
check("malformed tool-call json degrades to {} (no crash)", any(b["type"] == "tool_use" and b["input"] == {} for b in mb["content"]))

inc = [
    {"type": "response.output_item.added", "output_index": 0, "item": {"type": "function_call", "call_id": "c", "name": "x"}},
    {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": "{\"k\":1}"},
]
itypes = [json.loads(c.split("data: ", 1)[1])["type"] for c in responses_to_anthropic_events(iter(inc), "m")]
check("incomplete/aborted stream still terminates with message_stop", itypes[-1] == "message_stop")
check("incomplete stream closes its open block", itypes.count("content_block_stop") >= 1)

print("\n[model routing]")
def _route(m): return anthropic_to_responses({"model": m, "messages": []})["model"]
check("bare 'sol' -> gpt-5.6-sol", _route("sol") == "gpt-5.6-sol")
check("bare 'terra' -> gpt-5.6-terra (regression: it used to collapse to sol)", _route("terra") == "gpt-5.6-terra")
check("bare 'luna' -> gpt-5.6-luna (regression)", _route("luna") == "gpt-5.6-luna")
check("full 'gpt-5.6-terra' preserved", _route("gpt-5.6-terra") == "gpt-5.6-terra")
check("'claude-gpt-5.6-luna' prefix stripped", _route("claude-gpt-5.6-luna") == "gpt-5.6-luna")
check("real claude id 'sonnet' -> default gpt, never sent raw upstream", _route("sonnet") == "gpt-5.6-sol")
check("empty model -> default gpt", _route("") == "gpt-5.6-sol")

print("\n[token refresh mechanics]  (pure, no network)")
import base64 as _b64
def _mkjwt(claims):
    body = _b64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return "h." + body + ".sig"
check("client_id read from client_id claim", _jwt_client_id(_mkjwt({"client_id": "app_CID", "aud": "x"})) == "app_CID")
check("client_id falls back to aud claim", _jwt_client_id(_mkjwt({"aud": "app_AUD"})) == "app_AUD")
_m = _merge_refreshed({"tokens": {"access_token": "old", "refresh_token": "r0", "account_id": "acc"}, "OPENAI_API_KEY": "keep"},
                      {"access_token": "new", "refresh_token": "r1"})
check("refresh updates access_token", _m["tokens"]["access_token"] == "new")
check("refresh rotates refresh_token", _m["tokens"]["refresh_token"] == "r1")
check("refresh preserves account_id", _m["tokens"]["account_id"] == "acc")
check("refresh preserves sibling fields (OPENAI_API_KEY)", _m.get("OPENAI_API_KEY") == "keep")
check("refresh keeps old refresh_token when none returned",
      _merge_refreshed({"tokens": {"access_token": "o", "refresh_token": "keepme"}}, {"access_token": "n"})["tokens"]["refresh_token"] == "keepme")
check("refresh returns None on empty response, so auth.json is never clobbered", _merge_refreshed({"tokens": {"access_token": "x"}}, {}) is None)

print("\n[secure file read]")
import tempfile as _tf
_d = _tf.mkdtemp()
def _raises(fn):
    try:
        fn(); return False
    except Exception:
        return True
_good = os.path.join(_d, "good"); open(_good, "w").write("secret123"); os.chmod(_good, 0o600)
check("0600 regular file reads ok", _secure_read(_good, require_private=True).strip() == "secret123")
_grp = os.path.join(_d, "grp"); open(_grp, "w").write("x"); os.chmod(_grp, 0o644)
check("group/other-readable rejected under require_private", _raises(lambda: _secure_read(_grp, require_private=True)))
check("group/other-readable ok without require_private", _secure_read(_grp).strip() == "x")
_sym = os.path.join(_d, "sym"); os.symlink(_good, _sym)
check("symlink rejected (O_NOFOLLOW)", _raises(lambda: _secure_read(_sym)))
check("directory rejected", _raises(lambda: _secure_read(_d)))
_big = os.path.join(_d, "big"); open(_big, "w").write("A" * 2_000_000); os.chmod(_big, 0o600)
check("oversized file rejected", _raises(lambda: _secure_read(_big)))

print("\n[challenge nonce bound]")
def _validn(n):
    return 16 <= len(n) <= 256 and n.isascii() and n.isprintable()
check("15 chars rejected", not _validn("a" * 15))
check("16 chars accepted", _validn("a" * 16))
check("256 chars accepted", _validn("a" * 256))
check("257 chars rejected", not _validn("a" * 257))
check("embedded control char rejected", not _validn("a" * 16 + "\n"))

# Hardening regression tests
print("\n[hardening validation]")
check("valid minimal request accepted", _validate_request({"messages": [{"role": "user", "content": "hi"}]}) is None)
check("unknown well-formed block tolerated (thinking replay / forward-compat)", _validate_request({"messages": [{"role": "assistant", "content": [{"type": "thinking", "thinking": "...", "signature": "s"}]}]}) is None)
check("malformed non-object content block still rejected", "content blocks must be objects" in (_validate_request({"messages": [{"role": "user", "content": [42]}]}) or ""))
check("duplicate tool_use ids rejected", "unique" in (_validate_request({"messages": [{"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": "a"}, {"type": "tool_use", "id": "x", "name": "b"}]}]}) or ""))
check("duplicate tool names rejected", "unique" in (_validate_request({"messages": [], "tools": [{"name": "x", "input_schema": {}}, {"name": "x", "input_schema": {}}]}) or ""))

print("\n[bounded SSE parser]")
check("valid SSE data object parsed", list(_iter_sse(__import__('io').BytesIO(b'data: {"type":"ok"}\n'))) == [{"type": "ok"}])
check("non-object SSE data rejected", _raises(lambda: list(_iter_sse(__import__('io').BytesIO(b'data: [1,2]\n')))))
check("oversized SSE line rejected", _raises(lambda: list(_iter_sse(__import__('io').BytesIO(b'data: ' + b'x' * (MAX_SSE_LINE_BYTES + 1))))))

print("\n[maximum hardening regressions]")
check("SSE parser handles chunk boundaries", list(_iter_sse(__import__('io').BytesIO(b'data: {"type":"o' + b'k"}\n'))) == [{"type": "ok"}])
_old_out = proxy_module.MAX_OUTPUT_CHARS
try:
    proxy_module.MAX_OUTPUT_CHARS = 3
    check("translated output cap is enforced", _raises(lambda: list(responses_to_anthropic_events(iter([{"type":"response.output_text.delta","delta":"four"}]), "m"))))
finally:
    proxy_module.MAX_OUTPUT_CHARS = _old_out
_old_arg = proxy_module.MAX_TOOL_ARG_CHARS
try:
    proxy_module.MAX_TOOL_ARG_CHARS = 3
    check("tool argument cap is enforced", _raises(lambda: list(responses_to_anthropic_events(iter([
        {"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","call_id":"c","name":"x"}},
        {"type":"response.function_call_arguments.delta","output_index":0,"delta":"four"}
    ]), "m"))))
finally:
    proxy_module.MAX_TOOL_ARG_CHARS = _old_arg

print("\n[live streaming wiring (the exact do_POST path)]")
_io = __import__("io")
_wire = _io.BytesIO(
    b'event: response.created\n'
    b'data: {"type":"response.created","response":{"id":"r"}}\n\n'
    b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
    b'data: {"type":"response.completed","response":{"usage":{"output_tokens":1}}}\n\n'
)
_first = _wire.readline(MAX_SSE_LINE_BYTES + 1)
check("first-line sniff matches do_POST's branch test", bool(_first) and _first[:1] in (b"e", b"d", b":", b"\n"))
_stream = _PrefixedReader(_first, _wire)
check("stream reader exposes .read (chain-bug regression, v1.2.0)", hasattr(_stream, "read"))
_wev = [json.loads(c.split("data: ", 1)[1]) for c in responses_to_anthropic_events(_iter_sse(_stream), "m")]
_wtypes = [e["type"] for e in _wev]
check("wired stream starts/ends correctly", _wtypes[0] == "message_start" and _wtypes[-1] == "message_stop")
_wtext = "".join(e["delta"]["text"] for e in _wev
                 if e["type"] == "content_block_delta" and e["delta"]["type"] == "text_delta")
check("text survives the full wire path", _wtext == "hi")
check("prefixed reader drains the sniffed line before the pipe",
      _PrefixedReader(b"ab", _io.BytesIO(b"cd")).read(65536) == b"ab")

print("\n[request validation additions]")
check("non-string model rejected",
      "model must be a string" in (_validate_request({"model": {"x": 1}, "messages": []}) or ""))
_old_text = proxy_module.MAX_TEXT_CHARS
try:
    proxy_module.MAX_TEXT_CHARS = 5
    _tr = {"messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "c", "content": "sixchars"}]}]}
    check("tool_result content counts toward the text cap",
          "text content too large" in (proxy_module._validate_request(_tr) or ""))
finally:
    proxy_module.MAX_TEXT_CHARS = _old_text

print("\n" + ("all green" if not fails else f"{len(fails)} failed: " + ", ".join(fails)))
exit(1 if fails else 0)
