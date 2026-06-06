#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
PYTHON="${PROJECT_DIR}/.venv/bin/python"
SCRIPT="${SCRIPT_DIR}/sector_analysis.py"

KEYWORD="${1:-商业航天}"
MODE="${2:-local}"

if [[ "$MODE" == "--deep" ]]; then
    echo "============================================"
    echo "=== 多智能体板块深度分析: ${KEYWORD} ==="
    echo "============================================"
    echo ""
    echo "6名AI分析师协作流程:"
    echo "  1. 数据采集 → 板块排名/成分股/资金流"
    echo "  2. 板块分析师 → 多维度分析+趋势预测"
    echo "  3. 多头研究员 → 看多论证"
    echo "  4. 空头研究员 → 看空论证"
    echo "  5. 裁判裁决 → 最终判断"
    echo ""
    "$PYTHON" "$SCRIPT" deep "$KEYWORD"
elif [[ "$MODE" == "--api" ]]; then
    TOKEN="${TRADINGAGENTS_TOKEN:-}"
    API_URL="${TRADINGAGENTS_API_URL:-https://app.510168.xyz}"

    if [[ -z "$TOKEN" ]]; then
        echo "Error: TRADINGAGENTS_TOKEN is required for API mode"
        exit 1
    fi
    echo "=== API 模式: 板块分析 ==="
    echo "Keyword: $KEYWORD"
    RESPONSE=$(curl -s -X POST \
        "${API_URL}/api/v1/sector/analysis" \
        -H "Authorization: Bearer ${TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{\"keyword\": \"${KEYWORD}\"}" \
        --max-time 120)
    if [[ -z "$RESPONSE" ]]; then
        echo "Error: API request failed"
        exit 1
    fi
    echo "$RESPONSE"
else
    echo "=== 本地模式: 板块综合分析 ==="
    echo "Keyword: $KEYWORD"
    echo ""
    "$PYTHON" "$SCRIPT" analysis "$KEYWORD"
fi