#!/usr/bin/env bash
# =============================================================================
# repo_cleanup.sh — InsightForge AI 仓库清理脚本
# -----------------------------------------------------------------------------
# 设计原则 (遵循清理方案核心要求):
#   1. 安全第一: 默认 DRY-RUN (仅预检查输出清单); 实际删除需显式 --apply
#   2. 备份非删除: 所有被清理文件/目录先 mv 到 /tmp/project_backup_<时间戳>
#      而非 rm -rf, 可随时回滚
#   3. 以 Git 状态为准: 仅清理 untracked / 已确认无用文件; 已提交文件用 git rm
#   4. 保护基础设施: .env / config/*.yaml / requirements.txt 等绝不触碰
#   5. 危险项 opt-in: 运行时 DB (含用户账号)、.idea、train.csv 默认排除
#
# 用法:
#   bash scripts/repo_cleanup.sh                 # 预检查 (dry-run, 不动任何文件)
#   bash scripts/repo_cleanup.sh --apply          # 实际执行 (备份后清理)
#   bash scripts/repo_cleanup.sh --apply --include-dbs --include-ide --untrack-train-csv
#   bash scripts/repo_cleanup.sh --apply --commit-staged   # 顺带提交已 git rm 的删除
# =============================================================================
set -uo pipefail

# ------------------------------- 配置 ----------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TS="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="/tmp/project_backup_${TS}"

DRY_RUN=1            # 默认 dry-run
INCLUDE_DBS=0        # 运行时 SQLite (危险: users.db 含账号, chart_knowledge.db 策展)
INCLUDE_IDE=0        # .idea/ IDE 配置
UNTRACK_TRAIN=0      # train.csv 停止 git 跟踪 (保留磁盘文件)
COMMIT_STAGED=0      # 提交本次 git rm 的删除 (仅限清理相关路径)
KEEP_ONE_CSV=1       # 重复 CSV 保留 1 份示例

# ------------------------------- 参数解析 ------------------------------------
usage() {
  sed -n '3,20p' "${BASH_SOURCE[0]}"
}
for arg in "$@"; do
  case "$arg" in
    --apply)             DRY_RUN=0 ;;
    --include-dbs)       INCLUDE_DBS=1 ;;
    --include-ide)       INCLUDE_IDE=1 ;;
    --untrack-train-csv) UNTRACK_TRAIN=1 ;;
    --commit-staged)     COMMIT_STAGED=1 ;;
    --remove-all-csv)    KEEP_ONE_CSV=0 ;;
    -h|--help)           usage; exit 0 ;;
    *) echo "未知参数: $arg"; usage; exit 1 ;;
  esac
done

# ------------------------------- 工具函数 ------------------------------------
C_BLUE='\033[1;34m'; C_YEL='\033[1;33m'; C_GRN='\033[1;32m'; C_RST='\033[0m'
log()  { echo -e "${C_BLUE}[cleanup]${C_RST} $*"; }
warn() { echo -e "${C_YEL}[warn]${C_RST} $*"; }
ok()   { echo -e "${C_GRN}[ok]${C_RST} $*"; }

# 备份并移除 目录 (mv, 非 rm -rf)
purge_dir() {
  local p="$1"
  [ -d "$p" ] || { log "跳过(非目录): $p"; return; }
  local sz; sz=$(du -sh "$p" 2>/dev/null | cut -f1)
  if [ "$DRY_RUN" = 1 ]; then
    log "DRY-RUN  将备份+移除目录: $p ($sz)"
  else
    mkdir -p "$BACKUP_DIR"
    local rel="${p#"$REPO_ROOT"/}"
    mkdir -p "$BACKUP_DIR/$(dirname "$rel")"
    mv "$p" "$BACKUP_DIR/$rel" && ok "已备份+移除: $p ($sz) -> $BACKUP_DIR/$rel"
  fi
}

# 备份并移除 文件
purge_file() {
  local p="$1"
  [ -f "$p" ] || { log "跳过(非文件): $p"; return; }
  if [ "$DRY_RUN" = 1 ]; then
    log "DRY-RUN  将备份+移除文件: $p"
  else
    mkdir -p "$BACKUP_DIR"
    local rel="${p#"$REPO_ROOT"/}"
    mkdir -p "$BACKUP_DIR/$(dirname "$rel")"
    mv "$p" "$BACKUP_DIR/$rel" && ok "已备份+移除: $p -> $BACKUP_DIR/$rel"
  fi
}

# git rm 已提交文件 (附说明)
git_rm() {
  local p="$1" reason="$2"
  if git -C "$REPO_ROOT" ls-files --error-unmatch "$p" >/dev/null 2>&1; then
    if [ "$DRY_RUN" = 1 ]; then
      log "DRY-RUN  将 git rm: $p  ($reason)"
    else
      git -C "$REPO_ROOT" rm --quiet "$p" && ok "git rm: $p  ($reason)"
    fi
  else
    log "跳过(未跟踪/已移除): $p"
  fi
}

# =========================== 类别 A: 未被引用的源码 ===========================
category_A() {
  log "========== A. 未被引用的源码 =========="
  log "审计结论: 全部 tracked Python 模块均被引用, 无死代码。"
  log "  - agent/analysis/{anomaly_detection,product_analysis,trend_analysis} -> 被 trend/product/risk_agent 导入"
  log "  - agent/database/schema_loader -> 被 duckdb_manager 导入"
  log "  - agent/agents/document_report_agent -> 被 agent_tools._new_document_report_agent 实例化"
  log "  - agent/utils/progress_emitter, agent/api/static/js/icons.js (untracked 新文件) -> 均已接入"
  log "  无需任何删除操作。"
}

# =========================== 类别 B: 过时的测试用例 ===========================
category_B() {
  log "========== B. 过时的测试用例 =========="
  log "审计结论: 测试套件无 Streamlit / 已删功能残留引用, 无过时用例。"
  log "  - test_*.py 中 'st.'/'app.py' 命中均为 false positive (test./pytest./unittest.)"
  log "  - 静态资源 8 个 JS/CSS 全部被 app.html / index.html 引用, 无孤儿资产"
  log "  无需任何删除操作。"
}

# ===================== 类别 C: 运行时/插件系统产物 ============================
category_C() {
  log "========== C. 运行时/插件系统产物 (临时日志/中间缓存/自动提交文件) =========="
  # C1 — Agent 运行时报告与图表 (gitignored)
  purge_dir "$REPO_ROOT/agent/reports"     # ~9.4M 运行时生成的 .md 报告 + .html 图表
  purge_dir "$REPO_ROOT/agent/logs"        # 运行日志
  # C2 — 向量库持久化 (清理后需 /api/knowledge/reindex 重建)
  warn "agent/chroma_db 清理后需执行 /api/knowledge/reindex 重建向量索引"
  purge_dir "$REPO_ROOT/agent/chroma_db"   # ~1.1M ChromaDB 向量库
  # C3 — Agent/SDD 工作流产物 (briefs/reports/review diff, gitignored)
  purge_dir "$REPO_ROOT/.superpowers"      # ~1.1M SDD 工作流中间产物
  # C4 — Python 字节码 / pytest 缓存
  while IFS= read -r d; do purge_dir "$d"; done < <(
    find "$REPO_ROOT" -type d \( -name "__pycache__" -o -name ".pytest_cache" \) \
      -not -path "*/.git/*" 2>/dev/null
  )
  # C5 — 运行时 SQLite (默认排除: users.db 含真实账号, chart_knowledge.db 为策展知识库)
  if [ "$INCLUDE_DBS" = 1 ]; then
    warn "!! 清理运行时 DB: users.db(账号) / chart_knowledge.db(策展) / datasources.db / memory.db / customers.db / user_settings.db !!"
    for db in "$REPO_ROOT"/agent/database/*.db; do purge_file "$db"; done
  else
    log "跳过运行时 DB (6 个 .db) — 危险, 需 --include-dbs 显式开启"
  fi
  # C6 — IDE 配置 (默认排除)
  if [ "$INCLUDE_IDE" = 1 ]; then
    purge_dir "$REPO_ROOT/.idea"
  else
    log "跳过 .idea/ — 需 --include-ide 开启"
  fi
}

# ================ 类别 D: 冗余/无关数据 + 已提交无用文件 ======================
category_D() {
  log "========== D. 冗余/无关数据 + 已提交无用文件 =========="
  # D1 — 3 份 md5 完全相同的重复 CSV (573fb97420e981964bc9a1d7961a7b99), 代码未引用
  log "重复 CSV (md5 完全相同, 代码零引用):"
  if [ "$KEEP_ONE_CSV" = 1 ]; then
    purge_file "$REPO_ROOT/agent/data/datasets/wdi2.csv"
    purge_file "$REPO_ROOT/agent/data/datasets/API_SE_PRM_CMPT_FE_ZS_DS2_en_csv_v2_34687.csv"
    log "(保留 wdi.csv 作为示例; --remove-all-csv 可全删)"
  else
    purge_file "$REPO_ROOT/agent/data/datasets/wdi.csv"
    purge_file "$REPO_ROOT/agent/data/datasets/wdi2.csv"
    purge_file "$REPO_ROOT/agent/data/datasets/API_SE_PRM_CMPT_FE_ZS_DS2_en_csv_v2_34687.csv"
  fi
  # D2 — 无关 PDF (青岛科技大学资助办法, 与项目无关)
  purge_file "$REPO_ROOT/agent/data/08《青岛科技大学研究生参加国际学术会议资助办法》.pdf"
  # D3 — 已提交但无用 (git rm)
  git_rm "agent/app.py"                                                "Streamlit 入口已移除, 改用 FastAPI"
  git_rm "docs/HALLMARK_INSTALLED.md"                                 "Hallmark 标记文件, 已废弃"
  git_rm "docs/superpowers/plans/2026-07-20-datasource-management.md" "SDD 计划文档 (已落地)"
  git_rm "docs/superpowers/plans/2026-07-21-config-file-report.md"   "SDD 计划文档 (已落地)"
  git_rm "docs/superpowers/plans/2026-07-22-welcome-page-ui.md"       "SDD 计划文档 (已落地)"
  git_rm "docs/superpowers/specs/2026-07-20-datasource-management-design.md" "SDD 设计文档"
  git_rm "docs/superpowers/specs/2026-07-21-config-file-report-design.md"   "SDD 设计文档"
  git_rm "docs/superpowers/specs/2026-07-22-welcome-page-ui-design.md"      "SDD 设计文档"
  git_rm "images/show_image1.png" "README 展示图, 已不再引用"
  git_rm "images/show_image2.png" "README 展示图, 已不再引用"
  git_rm "images/show_image3.png" "README 展示图, 已不再引用"
  # D4 — train.csv (2.1M, tracked, 是 DEFAULT_DATASET; 默认保留)
  if [ "$UNTRACK_TRAIN" = 1 ]; then
    warn "train.csv 为 DEFAULT_DATASET; --cached 仅停止跟踪, 磁盘文件保留 (已加 .gitignore? 需手动确认)"
    if [ "$DRY_RUN" = 1 ]; then
      log "DRY-RUN  将 git rm --cached agent/data/train.csv (保留磁盘文件)"
    else
      git -C "$REPO_ROOT" rm --quiet --cached "agent/data/train.csv" \
        && ok "untracked (磁盘保留): agent/data/train.csv"
      # 写入 .gitignore (幂等), 防止后续 git add 重新跟踪
      if ! grep -qxF "agent/data/train.csv" "$REPO_ROOT/.gitignore"; then
        printf '\n# 默认数据集 (2.1M, 停止版本跟踪, 磁盘保留; 全新 clone 需手动放回)\nagent/data/train.csv\n' \
          >> "$REPO_ROOT/.gitignore" && ok "已写入 .gitignore: agent/data/train.csv"
      fi
      git -C "$REPO_ROOT" add .gitignore
    fi
  else
    log "保留 train.csv (2.1M, tracked, DEFAULT_DATASET) — --untrack-train-csv 可停止版本跟踪"
  fi
  # D5 — 提交本次 git rm 的删除 (仅限清理路径, 不触碰其他未提交改动)
  if [ "$COMMIT_STAGED" = 1 ] && [ "$DRY_RUN" = 0 ]; then
    git -C "$REPO_ROOT" commit -q -m "chore(cleanup): remove deprecated app.py, SDD docs, show images" -- \
      agent/app.py docs/HALLMARK_INSTALLED.md \
      "docs/superpowers/plans/2026-07-20-datasource-management.md" \
      "docs/superpowers/plans/2026-07-21-config-file-report.md" \
      "docs/superpowers/plans/2026-07-22-welcome-page-ui.md" \
      "docs/superpowers/specs/2026-07-20-datasource-management-design.md" \
      "docs/superpowers/specs/2026-07-21-config-file-report-design.md" \
      "docs/superpowers/specs/2026-07-22-welcome-page-ui-design.md" \
      images/show_image1.png images/show_image2.png images/show_image3.png \
      .gitignore agent/data/train.csv \
      && ok "已提交清理相关删除 (pathspec 限定, 不含其他改动)"
  elif [ "$COMMIT_STAGED" = 1 ] && [ "$DRY_RUN" = 1 ]; then
    log "DRY-RUN  --commit-staged 将仅提交上述清理路径 (pathspec 限定)"
  fi
}

# --------------------------------- 主流程 ------------------------------------
main() {
  cd "$REPO_ROOT" || exit 1
  log "仓库根目录: $REPO_ROOT"
  if [ "$DRY_RUN" = 1 ]; then
    log "模式: DRY-RUN 预检查 (不执行任何删除) — 确认无误后加 --apply 实际执行"
  else
    log "模式: APPLY (备份目录: $BACKUP_DIR, 可回滚)"
  fi
  echo ""
  category_A; echo ""
  category_B; echo ""
  category_C; echo ""
  category_D; echo ""
  if [ "$DRY_RUN" = 1 ]; then
    log "=== 预检查完成 ==="
    log "确认清单无误后执行: bash scripts/repo_cleanup.sh --apply"
    log "(可选) --commit-staged 提交已 git rm 的删除; --include-dbs/--include-ide/--untrack-train-csv 按需开启"
  else
    ok "=== 清理完成 ==="
    ok "备份位于: $BACKUP_DIR  (回滚: 将各文件 mv 回原位即可)"
    [ "$INCLUDE_DBS" = 0 ] && warn "未清理运行时 DB; 若清理了 chroma_db 请执行 /api/knowledge/reindex"
  fi
}
main "$@"
