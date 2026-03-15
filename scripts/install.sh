#!/usr/bin/env bash
# Deprecated: use setup.sh instead
echo "请使用 ./scripts/setup.sh 统一安装脚本"
exec "$(dirname "$0")/setup.sh" "$@"
