# GBrain MCP wire fixtures

Sanitized copies of the live gbrain 0.50 MCP wire shape (probed 2026-09-28): `text/event-stream`,
`event: message` / `data: {jsonrpc, id, result}`, payload is a JSON **string** in `result.content[0].text`,
errors are HTTP 200 with `result.isError: true` and `{error, message}` text. Page text is `compiled_truth`.
Keys and envelope match the live server; page text, slugs and hashes are synthetic (public repo).
`put_page_ok.sse` success payload is **assumed** (not probed; see spec wire contract "Unknown").
Serve through `httpx.MockTransport` with `content-type: text/event-stream` (except `plain_json_*.json`).
The JSON-RPC `id` in each file is 1. The client matches the response id to its request id,
so the MockTransport handler rewrites `"id":1` to the request id before replying.
