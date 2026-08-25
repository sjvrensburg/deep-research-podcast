# Building JSON for Open Notebook API Calls

When building JSON bodies for `/api/sources/json` that contain markdown with backslashes, quotes, or newlines (e.g., converted LaTeX from PDFs), inline `jq -n --arg` can fail with syntax errors due to complex escaping.

## The Problem

```bash
# This FAILS when markdown contains backslashes/quotes:
curl -d "$(jq -n --arg c "$(cat doc.md)" '{type:"text", content:$c}')"
# Error: jq: error: syntax error, unexpected INVALID_CHARACTER
```

## The Fix: Write JSON Files First

1. Create the JSON body in a temporary file:
```bash
cat > /tmp/source.json << 'EOF'
{
  "type": "text",
  "content": "$(cat document.md)",
  "title": "Document Title",
  "notebooks": ["notebook:xxxxx"],
  "embed": true,
  "async_processing": true
}
EOF
```

Or use a heredoc with single-quoted delimiter to prevent shell expansion:
```bash
cat > /tmp/source.json << 'ENDJSON'
{
  "type": "text",
  "content": "# Markdown content\nwith $variables and \"quotes\"",
  ...
}
ENDJSON
```

2. Send via `@` syntax:
```bash
curl -s -X POST http://127.0.0.1:5055/api/sources/json \
  -H "Content-Type: application/json" \
  -d @/tmp/source.json
```

## Why This Works

- `@/path` tells curl to read the request body from a file
- Heredoc with `'EOF'` (quoted) prevents shell variable expansion and escaping issues
- The JSON file can contain any characters without shell interpretation
- Verified working 2026-08-17 with LaTeX-heavy markdown from PDF conversions

## Alternative: Python One-Liner

For complex cases, use Python to build valid JSON:
```bash
python3 -c "
import json, sys
with open('document.md') as f:
    content = f.read()
body = {'type': 'text', 'content': content, ...}
print(json.dumps(body))
" > /tmp/source.json
```
