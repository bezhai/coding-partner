#!/usr/bin/env bash
# Coding Partner — one-step setup & deploy
# Usage: ./scripts/setup.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_NAME="coding-partner"

# ── Colors ──────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }

# ── 1. Banner ───────────────────────────────────────────
echo ""
echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║   Coding Partner — Setup & Deploy        ║${NC}"
echo -e "${CYAN}║   飞书 Vibe Coding 机器人                ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
echo ""

# ── 2. .env 配置 ────────────────────────────────────────
ENV_FILE="$PROJECT_DIR/.env"
ENV_EXAMPLE="$PROJECT_DIR/.env.example"

if [ ! -f "$ENV_FILE" ]; then
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    info "已从 .env.example 创建 .env"
fi

# 检查必填项
source "$ENV_FILE"
NEED_CONFIG=false

for var in FEISHU_APP_ID FEISHU_APP_SECRET REPO_BASE_PATH; do
    if [ -z "${!var:-}" ]; then
        NEED_CONFIG=true
    fi
done

read_required() {
    local prompt="$1" current="$2" result=""
    while [ -z "$result" ]; do
        if [ -n "$current" ]; then
            read -rp "  $prompt [$current]: " result
            result="${result:-$current}"
        else
            read -rp "  $prompt: " result
        fi
        if [ -z "$result" ]; then
            warn "  ↑ 此项为必填"
        fi
    done
    echo "$result"
}

source "$ENV_FILE" 2>/dev/null || true

if [ -z "${FEISHU_APP_ID:-}" ] || [ -z "${FEISHU_APP_SECRET:-}" ] || [ -z "${REPO_BASE_PATH:-}" ]; then
    info "请配置以下必填项（已有值可回车跳过）:"
    echo ""
    FEISHU_APP_ID="$(read_required "FEISHU_APP_ID" "${FEISHU_APP_ID:-}")"
    FEISHU_APP_SECRET="$(read_required "FEISHU_APP_SECRET" "${FEISHU_APP_SECRET:-}")"
    REPO_BASE_PATH="$(read_required "REPO_BASE_PATH (git 仓库所在目录)" "${REPO_BASE_PATH:-}")"

    sed -i "s|^FEISHU_APP_ID=.*|FEISHU_APP_ID=$FEISHU_APP_ID|" "$ENV_FILE"
    sed -i "s|^FEISHU_APP_SECRET=.*|FEISHU_APP_SECRET=$FEISHU_APP_SECRET|" "$ENV_FILE"
    sed -i "s|^REPO_BASE_PATH=.*|REPO_BASE_PATH=$REPO_BASE_PATH|" "$ENV_FILE"
    ok "配置已写入 .env"
    echo ""
fi

# ── 3. 选择部署方式 ─────────────────────────────────────
echo "请选择部署方式:"
echo ""
echo "  1) systemd  — 原生运行，适合 Linux 服务器 (推荐)"
echo "  2) docker   — 容器化运行，适合多环境分发"
echo ""
read -rp "请输入 [1/2]: " DEPLOY_MODE

case "$DEPLOY_MODE" in
    2|docker)
        DEPLOY_MODE="docker"
        ;;
    *)
        DEPLOY_MODE="systemd"
        ;;
esac

echo ""
info "已选择: $DEPLOY_MODE"

# ── 4. 前置依赖检查 ─────────────────────────────────────
check_cmd() {
    if command -v "$1" &>/dev/null; then
        ok "$1 ✓"
        return 0
    else
        err "$1 ✗ — $2"
        return 1
    fi
}

echo ""
info "检查依赖..."
MISSING=0

if [ "$DEPLOY_MODE" = "systemd" ]; then
    check_cmd "uv"     "安装: https://docs.astral.sh/uv/"               || MISSING=1
    check_cmd "git"    "安装: apt install git / yum install git"         || MISSING=1
    check_cmd "claude" "安装: https://docs.anthropic.com/en/docs/claude-code" || MISSING=1
    check_cmd "script" "安装: apt install bsdutils"                      || MISSING=1
else
    check_cmd "docker"          "安装: https://docs.docker.com/get-docker/" || MISSING=1
    # docker compose 可以是插件或独立命令
    if docker compose version &>/dev/null 2>&1; then
        ok "docker compose ✓"
    elif command -v docker-compose &>/dev/null; then
        ok "docker-compose ✓"
    else
        err "docker compose ✗ — 安装: https://docs.docker.com/compose/install/"
        MISSING=1
    fi
fi

if [ "$MISSING" -eq 1 ]; then
    echo ""
    err "有缺失的依赖，请先安装后重新运行此脚本"
    exit 1
fi

echo ""

# ── 5. 部署 ─────────────────────────────────────────────
if [ "$DEPLOY_MODE" = "systemd" ]; then
    # ── 5a. Systemd 部署 ────────────────────────────────
    TEMPLATE="$PROJECT_DIR/systemd/$SERVICE_NAME.service.template"
    CURRENT_USER="${SUDO_USER:-$(whoami)}"
    UV_BIN="$(command -v uv)"

    info "生成 systemd 服务文件..."
    sed \
        -e "s|{{USER}}|$CURRENT_USER|g" \
        -e "s|{{WORK_DIR}}|$PROJECT_DIR|g" \
        -e "s|{{UV_BIN}}|$UV_BIN|g" \
        "$TEMPLATE" > "$PROJECT_DIR/systemd/$SERVICE_NAME.service"

    ok "已生成 systemd/$SERVICE_NAME.service"

    info "安装 Python 依赖..."
    (cd "$PROJECT_DIR" && uv sync --frozen --no-dev)
    ok "依赖安装完成"

    echo ""
    read -rp "是否安装为 systemd 服务并启动? [y/N] " INSTALL_ANSWER
    if [[ "$INSTALL_ANSWER" =~ ^[Yy]$ ]]; then
        sudo cp "$PROJECT_DIR/systemd/$SERVICE_NAME.service" "/etc/systemd/system/$SERVICE_NAME.service"
        sudo systemctl daemon-reload
        sudo systemctl enable "$SERVICE_NAME"
        sudo systemctl start "$SERVICE_NAME"
        echo ""
        ok "服务已启动!"
        echo ""
        echo "  查看状态: sudo systemctl status $SERVICE_NAME"
        echo "  查看日志: sudo journalctl -u $SERVICE_NAME -f"
        echo "  停止服务: sudo systemctl stop $SERVICE_NAME"
    else
        echo ""
        info "跳过安装。手动操作:"
        echo "  sudo cp systemd/$SERVICE_NAME.service /etc/systemd/system/"
        echo "  sudo systemctl daemon-reload"
        echo "  sudo systemctl enable --now $SERVICE_NAME"
        echo ""
        info "或直接运行:"
        echo "  uv run python -m coding_partner.main"
    fi

else
    # ── 5b. Docker 部署 ─────────────────────────────────
    info "构建并启动 Docker 容器..."

    # 检查 claude 是否在宿主机上可用，提示挂载
    if command -v claude &>/dev/null; then
        CLAUDE_BIN="$(command -v claude)"
        CLAUDE_REAL="$(readlink -f "$CLAUDE_BIN" 2>/dev/null || echo "$CLAUDE_BIN")"
        ok "检测到宿主机 Claude CLI: $CLAUDE_REAL"
        info "将通过 volume 挂载到容器中"

        # 确保 docker-compose 中挂载了 claude binary
        # 如果用户的 claude 不在标准路径，追加挂载
        if ! grep -q "claude" "$PROJECT_DIR/docker-compose.yml" 2>/dev/null; then
            warn "请在 docker-compose.yml volumes 中添加:"
            echo "      - $CLAUDE_REAL:/usr/local/bin/claude:ro"
        fi
    else
        warn "宿主机未找到 claude CLI"
        warn "请确保 Dockerfile 中已取消注释 Claude 安装行，或手动挂载"
    fi

    echo ""
    cd "$PROJECT_DIR"

    if docker compose version &>/dev/null 2>&1; then
        docker compose up -d --build
    else
        docker-compose up -d --build
    fi

    echo ""
    ok "容器已启动!"
    echo ""
    echo "  查看日志: docker compose logs -f"
    echo "  停止服务: docker compose down"
fi

# ── 6. 完成 ─────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   Setup 完成!                            ║${NC}"
echo -e "${GREEN}║   在飞书中找到机器人，发送 /repo 开始    ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
echo ""
echo -e "${CYAN}💡 免克隆安装方式:${NC}"
echo "  CLI:    uv tool install git+https://github.com/<org>/coding-partner.git"
echo "          coding-partner setup"
echo "  Docker: curl -fsSL <raw-url>/scripts/install-docker.sh | bash"
echo ""
