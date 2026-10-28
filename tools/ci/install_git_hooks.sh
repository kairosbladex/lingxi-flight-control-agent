#!/usr/bin/env bash
# 安装本仓库的 git hooks。仅在用户显式运行后生效（不自动改 .git/hooks）。
#
# 用法: bash tools/ci/install_git_hooks.sh
#
# pre-commit 会做两件事：
#   1. 跑全量离线测试；
#   2. 跑合流门禁（完整签署模式，要求 docs/evidence/redteam_review.json 的
#      reviewed_tree 等于当前暂存树）。
# 也就是说：没有通过第二方评审，提交会被挡住。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOOK="$REPO_ROOT/.git/hooks/pre-commit"

if [[ ! -d "$REPO_ROOT/.git" ]]; then
  echo "错误：$REPO_ROOT 不是 git 仓库" >&2
  exit 1
fi

if [[ -f "$HOOK" ]] && ! grep -q "lingxi-flight-control-agent" "$HOOK" 2>/dev/null; then
  echo "错误：已存在非本仓库的 pre-commit hook，未覆盖：$HOOK" >&2
  exit 1
fi

cat > "$HOOK" <<'HOOK_EOF'
#!/usr/bin/env bash
# 由 tools/ci/install_git_hooks.sh 生成（lingxi-flight-control-agent）。
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

echo "[pre-commit] 全量离线测试..."
bash tools/ci/offline_tests.sh

echo "[pre-commit] 合流门禁（需要第二方评审证据）..."
python3 tools/ci/adversarial_jury_evaluator.py --no-offline
HOOK_EOF

chmod +x "$HOOK"
echo "已安装 pre-commit hook: $HOOK"
echo "卸载: rm $HOOK"
