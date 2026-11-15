# docs/

界面示意图（如 `gui-screenshot.png`、`gui-idle-state.png`）是**本机截图产物**，
画面里带有本机玩家昵称与对局信息，因此由 `.gitignore` 的 `docs/*.png` 排除，
不进入公开仓库。

想看界面，本地跑一次即可：

```bash
hs-copilot doctor     # 检查日志与卡表
hs-copilot gui        # 置顶悬浮窗，可拖动；左上角三色圆点关闭/折叠/精简
```

界面结构（400×620 深色浮窗）：状态栏 → 敌我局面条 → 战术建议 → 实时战况。
