#!/usr/bin/env bash
# Phase 6A demo journey — walks the full frontend flow using ONLY curl:
#   create session -> ask (streamed) -> follow-up (streamed) -> open para
#   -> fetch PDF -> reset
#
# Prereqs: the API is running (docker compose up, or uvicorn app.main:app
# --port 8080), the corpus is ingested, and the LLM endpoint is reachable.
#
# Usage:  BASE=http://localhost:8080 KEY=dev-key-123 bash scripts/demo_journey.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
KEY="${KEY:-dev-key-123}"
PARA="Corporate Office|2019-20|IIB|2"          # Tapovan HRT
# URL-encode the para key (spaces -> %20, | -> %7C)
PARA_ENC=$(printf '%s' "$PARA" | sed 's/ /%20/g; s/|/%7C/g')

hr() { printf '\n========== %s ==========\n' "$1"; }

# extract a JSON string field without requiring jq
json_field() { sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p"; }

hr "1. health"
curl -s "$BASE/api/v1/health"; echo

hr "2. create session"
SID=$(curl -s -X POST "$BASE/api/v1/sessions" -H "X-API-Key: $KEY" | json_field session_id)
echo "session_id = $SID"

hr "3. ask (SSE stream)"
curl -sN -X POST "$BASE/api/v1/sessions/$SID/ask" \
     -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"message":"What did audit observe about the Tapovan HRT package?"}'

hr "4. follow-up in the same session (context kept)"
curl -sN -X POST "$BASE/api/v1/sessions/$SID/ask" \
     -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"message":"Who was the contractor and what advances were given to it?"}'

hr "5. open the para (full structured record)"
curl -s "$BASE/api/v1/paras/$PARA_ENC" -H "X-API-Key: $KEY"; echo

hr "6. fetch the para's source PDF pages -> demo_para.pdf"
curl -s "$BASE/api/v1/paras/$PARA_ENC/pdf?pages=true" -H "X-API-Key: $KEY" -o demo_para.pdf
echo "saved $(wc -c < demo_para.pdf) bytes to demo_para.pdf"

hr "7. reset context"
curl -s -X POST "$BASE/api/v1/sessions/$SID/reset" -H "X-API-Key: $KEY"; echo

hr "history after reset"
curl -s "$BASE/api/v1/sessions/$SID/history" -H "X-API-Key: $KEY"; echo

echo; echo "Journey complete."
