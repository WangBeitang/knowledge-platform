#!/usr/bin/env bash
# Stage 6 冒烟验收（在目标云服务器 deploy/ 目录执行；任一步失败返回非 0）。
#
# 用法：
#   BASE_URL=https://<域名> \
#   SERVICE_API_KEY=<外部密钥实际值> \
#   SMOKE_ADMIN_USERNAME=admin SMOKE_ADMIN_PASSWORD=<初始管理员实际密码> \
#   bash deploy/scripts/smoke.sh
#
# 可选开关：
#   SMOKE_DOC_EXT=md   # 原 RAG 未配置 MinerU 时，显式切换到 .md 验证文档导入链路（默认 pdf）
#   SMOKE_SKIP_CLEAN=1 # 保留测试数据（默认清理 smoke 前缀的文档/FAQ/会话）
#
# 覆盖（冻结 §五）：live/ready/登录/bootstrap verify/文档导入轮询终态/文档查询/
# Chunk 读取/内部问答 SSE 终态唯一/FAQ 发布+精确命中/FAQ 同步状态/Dashboard/
# Audit/外部 Key 拒绝/外部合法 SSE/外部 dataset_ids 422/外部不可命中 internal/admin 范围。
set -euo pipefail

BASE="${BASE_URL:-http://127.0.0.1:8000}"
SERVICE_API_KEY="${SERVICE_API_KEY:-}"
ADMIN_USER="${SMOKE_ADMIN_USERNAME:-admin}"
ADMIN_PASS="${SMOKE_ADMIN_PASSWORD:-}"
DOC_EXT="${SMOKE_DOC_EXT:-pdf}"
SKIP_CLEAN="${SMOKE_SKIP_CLEAN:-0}"
PREFIX="smoke_stage6_$(date +%s)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

[ -n "$SERVICE_API_KEY" ] || { echo "!! 缺少 SERVICE_API_KEY 环境变量" >&2; exit 1; }
[ -n "$ADMIN_PASS" ] || { echo "!! 缺少 SMOKE_ADMIN_PASSWORD 环境变量" >&2; exit 1; }
case "$DOC_EXT" in pdf|md) ;; *) echo "!! SMOKE_DOC_EXT 仅支持 pdf|md" >&2; exit 1;; esac

echo "==> 冒烟开始 BASE=$BASE PREFIX=$PREFIX DOC_EXT=$DOC_EXT"

fail() { echo "!! FAIL: $*" >&2; exit 1; }
pass() { echo "    ok: $*"; }

# http <METHOD> <path> [curl args...] → 设置 HTTP_CODE / HTTP_BODY
http() {
  local method=$1 path=$2; shift 2
  local tmp="$TMP_DIR/http_body"
  HTTP_CODE=$(curl -sS -o "$tmp" -w '%{http_code}' -X "$method" "$BASE$path" "$@")
  HTTP_BODY=$(cat "$tmp")
}

# sse_events <sse_text> → 每行 "event<TAB>data"，供后续断言
sse_events() {
  python3 - "$1" <<'PY'
import sys
text = sys.argv[1]
for block in text.split("\n\n"):
    block = block.strip()
    if not block:
        continue
    event = "message"
    data_lines = []
    for line in block.splitlines():
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
    if data_lines:
        print(event + "\t" + "".join(data_lines))
PY
}

json_get() { # json_get <json> <python-expr>
  python3 - "$1" "$2" <<'PY'
import json, sys
data = json.loads(sys.argv[1])
print(eval(sys.argv[2], {"data": data}))
PY
}

# sse_error_code <sse_text>：取最后一个 error 事件的稳定错误码（安全展示，不泄漏内部异常）
sse_error_code() {
  echo "$1" | python3 -c '
import json, sys
last_code = "UNKNOWN"
for line in sys.stdin.read().splitlines():
    parts = line.split("\t", 1)
    if len(parts) == 2 and parts[0] == "error":
        try:
            last_code = str(json.loads(parts[1]).get("code") or "UNKNOWN")
        except Exception:
            last_code = "UNKNOWN"
print(last_code)
'
}

# assert_final <sse_text> <场景说明>：正常 RAG 问答必须唯一 final 终态；
# error → 打印安全错误码并使脚本返回非 0（同时保持 final/error 互斥检查）
assert_final() {
  local text=$1 desc=$2
  local ev last fin err
  ev=$(sse_events "$text")
  last=$(echo "$ev" | tail -1 | cut -f1)
  fin=$(echo "$ev" | cut -f1 | grep -c '^final$' || true)
  err=$(echo "$ev" | cut -f1 | grep -c '^error$' || true)
  [ $((fin + err)) -eq 1 ] || fail "${desc} final/error 未互斥（final=$fin error=$err）"
  [ "$last" = "final" ] || fail "${desc} 未得到 final（终态=${last}，安全错误码=$(sse_error_code "$text")）"
}

gen_test_pdf() { # gen_test_pdf <path>
  python3 - "$1" <<'PY'
import sys
path = sys.argv[1]
text = "Smoke Stage6 PDF Document"
objects = [
    b"<< /Type /Catalog /Pages 2 0 R >>",
    b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
    b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
]
stream = f"BT /F1 20 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
out = b"%PDF-1.4\n"
offsets = [0]
for i, obj in enumerate(objects, 1):
    offsets.append(len(out))
    out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
xref_pos = len(out)
out += f"xref\n0 {len(objects)+1}\n".encode()
out += b"0000000000 65535 f \n"
for off in offsets[1:]:
    out += f"{off:010d} 00000 n \n".encode()
out += f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
with open(path, "wb") as fh:
    fh.write(out)
PY
}

# ---------- 1/2. 健康检查 ----------
echo "==> [1/16] health/live"
http GET /api/v1/health/live
[ "$HTTP_CODE" = "200" ] || fail "health/live HTTP=$HTTP_CODE body=$HTTP_BODY"
pass "health/live 200"

echo "==> [2/16] health/ready（MySQL/Redis 必须 ok；RAG 状态仅记录）"
http GET /api/v1/health/ready
[ "$HTTP_CODE" = "200" ] || fail "health/ready HTTP=$HTTP_CODE body=$HTTP_BODY"
ready_status=$(json_get "$HTTP_BODY" 'data["data"]["status"]')
ready_components=$(json_get "$HTTP_BODY" '{k: v["status"] for k, v in data["data"]["components"].items()}')
echo "    ready=$ready_status components=$ready_components"
echo "$HTTP_BODY" | python3 -c '
import json, sys
d = json.load(sys.stdin)["data"]["components"]
if d.get("mysql", {}).get("status") != "ok": sys.exit("mysql not ok")
if d.get("redis", {}).get("status") != "ok": sys.exit("redis not ok")
' || fail "MySQL/Redis 未就绪"
pass "health/ready（mysql/redis ok）"

# ---------- 3. 管理员登录 ----------
echo "==> [3/16] 管理员登录"
http POST /api/v1/auth/login -H 'Content-Type: application/json' \
  -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}"
[ "$HTTP_CODE" = "200" ] || fail "登录失败 HTTP=$HTTP_CODE body=$HTTP_BODY"
TOKEN=$(json_get "$HTTP_BODY" 'data["data"]["access_token"]')
[ -n "$TOKEN" ] || fail "登录响应缺少 access_token"
AUTH="Authorization: Bearer $TOKEN"
pass "登录成功（admin=${ADMIN_USER}）"

# ---------- 4. Dataset bootstrap verify ----------
echo "==> [4/16] Dataset bootstrap / verify"
http POST /api/v1/admin/integration/rag/bootstrap -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"verify_only": true}'
[ "$HTTP_CODE" = "200" ] || fail "bootstrap verify HTTP=$HTTP_CODE body=$HTTP_BODY"
bs_overall=$(json_get "$HTTP_BODY" 'data["data"]["overall"]')
echo "    bootstrap overall=$bs_overall"
[ "$bs_overall" = "succeeded" ] || fail "bootstrap verify overall=${bs_overall}（三档 Dataset 应全部存在且成员校验通过）"
pass "bootstrap verify overall=$bs_overall"

# ---------- 5. 文档导入（PDF/MD）并轮询真实终态 ----------
echo "==> [5/16] 文档导入（${DOC_EXT}）并轮询终态"
DOC_FILE="$TMP_DIR/doc.$DOC_EXT"
if [ "$DOC_EXT" = "pdf" ]; then
  gen_test_pdf "$DOC_FILE"
else
  printf '# Smoke Stage6 Document\n\nsmoke_stage6 文档导入验证内容。\n' > "$DOC_FILE"
fi
http POST /api/v1/admin/documents/import -H "$AUTH" \
  -F "knowledge_scope=external_public" -F "files=@$DOC_FILE"
[ "$HTTP_CODE" = "200" ] || fail "文档导入 HTTP=$HTTP_CODE body=$HTTP_BODY"
item_status=$(json_get "$HTTP_BODY" 'data["data"]["items"][0]["status"]')
TASK_ID=$(json_get "$HTTP_BODY" 'data["data"]["items"][0]["task_id"]')
DOC_ID=$(json_get "$HTTP_BODY" 'data["data"]["items"][0]["document_id"]')
echo "    item_status=$item_status task_id=$TASK_ID doc_id=$DOC_ID"
[ "$item_status" = "pending" ] || fail "导入未进入 pending（可能被拒绝）：$HTTP_BODY"
[ -n "$TASK_ID" ] || fail "导入缺少 task_id"
# 轮询到真实终态（succeeded / failed），最多 30 次 × 5s
task_done=0
for i in $(seq 1 30); do
  http GET "/api/v1/admin/integration/tasks/$TASK_ID" -H "$AUTH"
  [ "$HTTP_CODE" = "200" ] || { echo "    ... 查询任务失败($i) HTTP=$HTTP_CODE"; sleep 5; continue; }
  tstatus=$(json_get "$HTTP_BODY" 'data["data"]["status"]')
  echo "    ... task=$tstatus ($i/30)"
  case "$tstatus" in
    succeeded) task_done=1; break;;
    failed) fail "文档导入任务 failed: $HTTP_BODY";;
  esac
  sleep 5
done
[ "$task_done" = "1" ] || fail "文档导入任务 150 秒内未达终态（最后 status=${tstatus}）"
pass "文档导入任务 succeeded"

# ---------- 6. 文档可查询 ----------
echo "==> [6/16] 文档列表可查询"
http GET "/api/v1/admin/documents?page=1&page_size=20" -H "$AUTH"
[ "$HTTP_CODE" = "200" ] || fail "文档列表 HTTP=$HTTP_CODE body=$HTTP_BODY"
echo "$HTTP_BODY" | python3 -c '
import json, sys
data = json.load(sys.stdin)["data"]
items = data.get("items") or []
assert any(i.get("id") == sys.argv[1] for i in items), "导入文档未出现在列表"
' "$DOC_ID" || fail "导入文档未出现在列表"
pass "文档列表包含导入文档"

# ---------- 7. Chunk 可读取 ----------
echo "==> [7/16] Chunk 可读取"
http GET "/api/v1/admin/documents/$DOC_ID/chunks" -H "$AUTH"
[ "$HTTP_CODE" = "200" ] || fail "Chunk 列表 HTTP=$HTTP_CODE body=$HTTP_BODY"
chunk_count=$(json_get "$HTTP_BODY" 'len(data["data"]["items"])')
echo "    chunk_count=$chunk_count"
pass "Chunk 列表可读取（$chunk_count 条）"

# ---------- 8. 内部问答 SSE 终态唯一 ----------
echo "==> [8/16] 内部问答 SSE"
http POST /api/v1/chat/sessions -H "$AUTH" -H 'Content-Type: application/json' -d '{}'
[ "$HTTP_CODE" = "201" ] || fail "创建会话 HTTP=$HTTP_CODE body=$HTTP_BODY"
SID=$(json_get "$HTTP_BODY" 'data["data"]["id"]')
Q8="$PREFIX 内部问答如何查看风险等级？"
http POST "/api/v1/chat/sessions/$SID/messages:stream" -H "$AUTH" \
  -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  -d "{\"question\":\"$Q8\"}"
[ "$HTTP_CODE" = "200" ] || fail "内部问答 SSE HTTP=$HTTP_CODE body=$HTTP_BODY"
# 正常 RAG 问答必须得到唯一 final；error → 打印安全错误码并失败
assert_final "$HTTP_BODY" "内部 RAG 问答"
pass "内部问答 SSE final 唯一"

# ---------- 9. FAQ 发布 + 精确命中（外部 API，同时覆盖外部合法 SSE） ----------
echo "==> [9/16] FAQ 发布与精确命中"
FAQ_Q="$PREFIX 外部公开FAQ如何办理双录？"
FAQ_A="smoke 外部公开答案：办理双录请携带身份证至柜台。"
http POST /api/v1/admin/faqs -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"knowledge_scope\":\"external_public\",\"question\":\"$FAQ_Q\",\"answer\":\"$FAQ_A\"}"
[ "$HTTP_CODE" = "200" ] || fail "创建 FAQ HTTP=$HTTP_CODE body=$HTTP_BODY"
FAQ_EXT_ID=$(json_get "$HTTP_BODY" 'data["data"]["id"]')
echo "    faq_id=$FAQ_EXT_ID"
# 外部 API 精确命中（不调用 RAG）
http POST /api/v1/external/knowledge/messages:stream \
  -H "X-Service-Key: $SERVICE_API_KEY" -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -d "{\"external_session_id\":\"${PREFIX}_sess\",\"external_user_id\":\"smoke_user\",\"question\":\"$FAQ_Q\"}"
[ "$HTTP_CODE" = "200" ] || fail "外部 FAQ 命中 HTTP=$HTTP_CODE body=$HTTP_BODY"
ev9=$(sse_events "$HTTP_BODY")
last9=$(echo "$ev9" | tail -1)
echo "$last9" | python3 -c '
import json, sys
line = sys.stdin.read().split("\t", 1)
event = line[0]
data = json.loads(line[1])
assert event == "final", f"外部 FAQ 命中未到 final: {event}"
assert data.get("answer_source") == "faq_cache", f"answer_source={data.get('answer_source')}"
assert data.get("trace_id") is None and data.get("citations") == []
assert "smoke 外部公开答案" in data.get("answer", "")
' || fail "外部未精确命中 external_public FAQ"
pass "FAQ 发布后外部精确命中（answer_source=faq_cache）"

# ---------- 10. FAQ 同步状态可查询 ----------
echo "==> [10/16] FAQ 同步状态可查询"
http GET "/api/v1/admin/faqs?knowledge_scope=external_public&status=published&page=1&page_size=20" -H "$AUTH"
[ "$HTTP_CODE" = "200" ] || fail "FAQ 列表 HTTP=$HTTP_CODE body=$HTTP_BODY"
sync_status=$(echo "$HTTP_BODY" | python3 -c '
import json, sys
data = json.load(sys.stdin)["data"]
for item in data.get("items") or []:
    if item.get("id") == sys.argv[1]:
        print(item.get("rag_sync_status") or "none")
        break
else:
    sys.exit("FAQ 不在列表")
' "$FAQ_EXT_ID") || fail "$sync_status"
echo "    rag_sync_status=$sync_status"
http GET "/api/v1/admin/faq-sync-runs?page=1&page_size=5" -H "$AUTH"
[ "$HTTP_CODE" = "200" ] || fail "FAQ 同步记录 HTTP=$HTTP_CODE"
pass "FAQ 同步状态可查询（rag_sync_status=${sync_status}）"

# ---------- 11. Dashboard ----------
echo "==> [11/16] Dashboard 可查询"
http GET "/api/v1/admin/dashboard/summary?page=1" -H "$AUTH"
[ "$HTTP_CODE" = "200" ] || fail "Dashboard summary HTTP=$HTTP_CODE body=$HTTP_BODY"
pv=$(json_get "$HTTP_BODY" 'data["data"].get("pv_count")')
echo "    pv_count=$pv"
pass "Dashboard summary 可查询"

# ---------- 12. Audit Logs ----------
echo "==> [12/16] Audit Logs 可查询"
http GET "/api/v1/admin/audit-logs?page=1&page_size=5" -H "$AUTH"
[ "$HTTP_CODE" = "200" ] || fail "Audit HTTP=$HTTP_CODE body=$HTTP_BODY"
pass "Audit Logs 可查询"

# ---------- 13. 外部缺/错 Key 拒绝 ----------
echo "==> [13/16] 外部缺/错 X-Service-Key 拒绝"
http POST /api/v1/external/knowledge/messages:stream \
  -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  -d '{"external_session_id":"s","external_user_id":"u","question":"测试问题"}'
[ "$HTTP_CODE" = "401" ] || fail "缺 Key 应 401，实际 HTTP=$HTTP_CODE body=$HTTP_BODY"
[ "$(json_get "$HTTP_BODY" 'data["error"]["code"]')" = "SERVICE_AUTH_FAILED" ] || fail "缺 Key 错误码不符"
http POST /api/v1/external/knowledge/messages:stream \
  -H "X-Service-Key: wrong-key-value" -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  -d '{"external_session_id":"s","external_user_id":"u","question":"测试问题"}'
[ "$HTTP_CODE" = "401" ] || fail "错 Key 应 401，实际 HTTP=$HTTP_CODE"
pass "缺/错 Key 均 401 SERVICE_AUTH_FAILED"

# ---------- 14. 外部合法请求可完成 SSE（RAG 路径，faq 未命中） ----------
echo "==> [14/16] 外部合法请求完成 SSE"
Q14="$PREFIX 外部普通问题如何查询佣金？"
http POST /api/v1/external/knowledge/messages:stream \
  -H "X-Service-Key: $SERVICE_API_KEY" -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -d "{\"external_session_id\":\"${PREFIX}_sess2\",\"external_user_id\":\"smoke_user\",\"question\":\"$Q14\"}"
[ "$HTTP_CODE" = "200" ] || fail "外部合法请求 HTTP=$HTTP_CODE body=$HTTP_BODY"
# 外部合法、FAQ 未命中的 RAG 问答必须得到唯一 final；error → 打印安全错误码并失败
assert_final "$HTTP_BODY" "外部 RAG 问答"
pass "外部合法请求 SSE final"

# ---------- 15. 外部 dataset_ids → 422 ----------
echo "==> [15/16] 外部 dataset_ids 越权 422"
http POST /api/v1/external/knowledge/messages:stream \
  -H "X-Service-Key: $SERVICE_API_KEY" -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  -d '{"external_session_id":"s","external_user_id":"u","question":"q","dataset_ids":["securities_internal_shared"]}'
[ "$HTTP_CODE" = "422" ] || fail "dataset_ids 应 422，实际 HTTP=$HTTP_CODE body=$HTTP_BODY"
[ "$(json_get "$HTTP_BODY" 'data["error"]["code"]')" = "VALIDATION_ERROR" ] || fail "422 错误码不符"
pass "外部 dataset_ids → 422 VALIDATION_ERROR"

# ---------- 16. 外部不可命中 internal/admin 范围 ----------
echo "==> [16/16] 外部不可命中 internal/admin 范围"
INT_Q="$PREFIX 内部FAQ费率如何查询？"
http POST /api/v1/admin/faqs -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"knowledge_scope\":\"internal_shared\",\"question\":\"$INT_Q\",\"answer\":\"内部专属答案：仅内部可见。\"}"
[ "$HTTP_CODE" = "200" ] || fail "创建 internal FAQ HTTP=$HTTP_CODE body=$HTTP_BODY"
FAQ_INT_ID=$(json_get "$HTTP_BODY" 'data["data"]["id"]')
# 外部问同一问题：不得命中 internal_shared（answer_source 不得为 faq_cache）
http POST /api/v1/external/knowledge/messages:stream \
  -H "X-Service-Key: $SERVICE_API_KEY" -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -d "{\"external_session_id\":\"${PREFIX}_sess3\",\"external_user_id\":\"smoke_user\",\"question\":\"$INT_Q\"}"
[ "$HTTP_CODE" = "200" ] || fail "外部 internal 探测 HTTP=$HTTP_CODE body=$HTTP_BODY"
# 该请求是外部 FAQ 未命中的 RAG 问答：必须 final（error → 打印安全错误码并失败），
# 同时校验未命中 internal（answer_source 不得为 faq_cache，越权防护）
assert_final "$HTTP_BODY" "外部 internal 范围探测"
ev16=$(sse_events "$HTTP_BODY")
echo "$ev16" | tail -1 | python3 -c '
import json, sys
line = sys.stdin.read().split("\t", 1)
event = line[0]
data = json.loads(line[1])
assert event == "final", f"终态异常: {event}"
assert data.get("answer_source") != "faq_cache", "外部命中了内部 FAQ（越权）！"
' || fail "外部疑似命中 internal 范围"
pass "外部未命中 internal/admin 范围"

# ---------- 清理 ----------
if [ "$SKIP_CLEAN" != "1" ]; then
  echo "==> 清理 smoke 测试数据"
  # 下线 smoke FAQ（external_public + internal_shared）
  for fid in "$FAQ_EXT_ID" "$FAQ_INT_ID"; do
    http POST "/api/v1/admin/faqs/$fid/unpublish" -H "$AUTH" -H 'Content-Type: application/json' -d '{}' || true
  done
  # 删除 smoke 文档（软删）
  http DELETE "/api/v1/admin/documents/$DOC_ID" -H "$AUTH" || true
  # 软删内部测试会话
  http DELETE "/api/v1/chat/sessions/$SID" -H "$AUTH" || true
  pass "清理完成（FAQ 下线 / 文档软删 / 会话软删）"
else
  echo "==> SMOKE_SKIP_CLEAN=1，保留测试数据"
fi

echo "==> 冒烟通过：16/16 项全部完成（PREFIX=${PREFIX}）"
