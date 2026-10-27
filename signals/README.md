# 多 Agent 协同信号量目录 (Signals Directory)

各 Agent 在本机会话或外部 CLI 工具完成其任务后，可通过生成标志文件通知总控大脑：
- `signals/codex.done`: Codex CLI 完成后端适配器与单测
- `signals/deepseek.done`: DeepSeek Harness 完成几何解算与对抗单测
- `signals/claude.done`: Claude CLI 完成前端组件与 13 场景话术走查
- `signals/grok.done`: Grok Build 完成仿真桩与一键冒烟脚本
- `signals/agy.done`: agy 完成跨仓信封与复合意图评测

总控大脑亦会自主扫描 Git commit 历史与文件变更时间戳，双重保障自动验收。
