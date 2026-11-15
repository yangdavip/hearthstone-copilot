"""
炉石助手 · macOS 可视化悬浮界面

在屏幕最上层显示置顶浮窗，实时展示：
- 对局状态（回合、法力、双方血量与场面）
- 大模型战术建议（含斩杀高亮）
- 实时战况日志（伤害、死亡、触发、区域变更）

依赖（未安装时自动降级为终端输出）::

    .venv/bin/python -m pip install pyobjc-core pyobjc-framework-Cocoa

实现要点：
- 用 NSPanel + 浮动层级实现「最上层」，但不抢焦点、不遮挡游戏操作
- 所有控件在主线程更新：watcher 在后台线程跑日志，通过 performSelectorOnMainThread 派发
- 可折叠：平时只显示建议条，展开后看完整日志
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

from .advisor import Advice

# ─────────────────────────────────────────────────────────────
# 配色（暗色系，与炉石 UI 观感一致）
# ─────────────────────────────────────────────────────────────

C_BG = (0.09, 0.09, 0.11, 0.93)
C_CARD = (0.15, 0.15, 0.18, 1.0)
C_BORDER = (0.32, 0.32, 0.38, 1.0)
C_TITLE = (0.95, 0.85, 0.55, 1.0)
#: 对局已结束的判定依据（覆盖炉石原始字符串）
#: 实测：对局结束后 step=FINAL_GAMEOVER、state=COMPLETE，两者都会出现
_FINISHED_STEPS = frozenset(
    {"FINAL_GAMEOVER", "FINAL_WRAPUP", "GAME_OVER", "COMPLETE", "WAITING", "LOADING"}
)
_FINISHED_STATES = frozenset(
    {"COMPLETE", "FINAL_GAMEOVER", "FINAL_WRAPUP", "WAITING", "LOADING", "INVALID"}
)

#: 日志静默超过该秒数即认为「炉石已离开战斗」。
#: 炉石只在战斗进行中持续写 Power.log，套牌/选人/主界面完全不写。
#: 90 秒足够宽松（不会在思考长回合时误判），也足够灵敏。
_IDLE_FATAL_SECONDS = 90.0

#: macOS 三键窗口控制按钮配色
C_DOT_CLOSE = (1.00, 0.37, 0.34, 1.0)
C_DOT_MIN = (1.00, 0.74, 0.18, 1.0)
C_DOT_MAX = (0.16, 0.79, 0.35, 1.0)

C_ME = (0.45, 0.85, 0.55, 1.0)
C_FOE = (0.95, 0.45, 0.45, 1.0)
C_TEXT = (0.88, 0.88, 0.90, 1.0)
C_DIM = (0.58, 0.58, 0.63, 1.0)
C_ACCENT = (0.45, 0.70, 0.98, 1.0)
C_LETHAL = (1.0, 0.30, 0.30, 1.0)
C_WARN = (0.98, 0.72, 0.30, 1.0)


def _window_style_mask() -> int:
    """Titled | FullSizeContentView。

    PyObjC 的 styleMask 参数必须传整数常量，传bool 会 struct 解包失败。
    """
    try:
        import AppKit

        return int(
            AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskFullSizeContentView
        )
    except Exception:
        return (1 << 0) | (1 << 15)


@dataclass
class BoardSnapshot:
    """送进 GUI 的局面快照（只含展示需要的字段）。"""

    turn: int = 0
    step: str = ""
    state: str = "等待对局"
    my_pid: int = 1
    #: 两个玩家的显示名（键为 PlayerID），用于界面上区分敌我
    names: dict = field(default_factory=dict)
    #: 日志已静默多久（秒）。> 0 表示炉石当前不在对局中（套牌/主界面不写 Power.log）
    idle_seconds: float = 0.0
    my_hero: str = ""
    my_hp: int = 0
    my_armor: int = 0
    my_mana: str = "0/0"
    my_hand_count: int = 0
    my_board: list[tuple[str, str, str]] = field(default_factory=list)
    #: 场上地标（不是随从，单列显示）
    my_locations: list[str] = field(default_factory=list)
    foe_locations: list[str] = field(default_factory=list)
    my_hand: list[tuple[str, str]] = field(default_factory=list)
    foe_hero: str = ""
    foe_hp: int = 0
    foe_armor: int = 0
    foe_mana: str = "0/0"
    foe_hand_count: int = 0
    foe_board: list[tuple[str, str, str]] = field(default_factory=list)
    foe_deck: int | str = 0
    my_deck: int | str = 0
    battle_log: list[str] = field(default_factory=list)

    @property
    def in_progress(self) -> bool:
        """对局是否正在进行中（用于决定标题栏与建议区的显示）。

        必须覆盖炉石的**原始状态字符串**，实测有：
        - ``FINAL_GAMEOVER``（出现在 ``step``）
        - ``COMPLETE``（出现在 ``state``，对局结束后的终态）
        - ``WAITING`` / ``RUNNING`` 等

        漏掉任何一个都会导致「已结束的对局仍显示在浮窗上」。
        """
        if self.turn <= 0:
            return False
        # 日志静默**过久** → 炉石已离开战斗。
        # 阈值不能设成 0：正常对局中两次 poll 之间也有 1~2 秒间隔，
        # 用「>0」会把进行中的对局误判成已离局。
        if self.idle_seconds > _IDLE_FATAL_SECONDS:
            return False
        if _FINISHED_STEPS.intersection({self.step or "", self.state or ""}):
            return False
        return not _FINISHED_STATES.intersection({self.step or "", self.state or ""})

    @classmethod
    def from_recon(cls, recon, my_pid: int = 1) -> "BoardSnapshot":
        me = recon.player_view(my_pid)
        foe = recon.player_view(2 if my_pid == 1 else 1)

        def mech_with_status(e, mine: bool) -> str:
            """随从标签里补上攻击资格，避免「刚上场却看起来能打」。"""
            parts = list(e.mechanics())
            st = e.attack_status()
            if mine and st["short"]:
                parts.append(st["short"])
            elif not mine and st["just_summoned"]:
                parts.append("刚召唤·下回合可打")
            elif not mine and st["short"] in ("冻结", "无法攻击"):
                parts.append(st["short"])
            return " ".join(parts)

        step = recon.step or ""
        # 炉石终态是 ``state=COMPLETE``；step 侧还有 FINAL_GAMEOVER。
        # 两者任一命中都视为「已结束」，标题栏据此显示而不是继续显示旧局面。
        finished = bool(
            _FINISHED_STEPS.intersection({step, recon.state or ""})
        )
        idle = float(getattr(recon, "idle_seconds", 0.0) or 0.0)
        if finished and idle > 60:
            # 终局 + 日志停更= 玩家已回到套牌/主界面
            state = f"上一局已结束（{int(idle // 60)} 分钟前）"
        elif finished:
            state = "对局已结束"
        elif recon.state:
            state = recon.state
        else:
            state = "对局中"
        return cls(
            turn=recon.turn,
            step=recon.step,
            state=state,
            my_pid=my_pid,
            names=dict(recon.player_names),
            idle_seconds=float(getattr(recon, "idle_seconds", 0.0) or 0.0),
            my_hero=me.hero.display_name if me.hero else "-",
            my_hp=me.hero_hp,
            my_armor=me.armor,
            my_mana=f"{me.mana}/{me.mana_max}",
            my_hand_count=len(me.hand),
            my_board=[
                (e.display_name, f"{e.stats[0]}/{e.stats[1]}", mech_with_status(e, True))
                for e in me.board
            ],
            my_locations=[
                f"{e.display_name} {e.stats[1]}耐久"
                + ("（本回合已用）" if e.location_used else "")
                for e in me.locations
            ],
            foe_locations=[
                f"{e.display_name} {e.stats[1]}耐久"
                + ("（本回合已用）" if e.location_used else "")
                for e in foe.locations
            ],
            my_hand=[(f"#{i}", e.display_name) for i, e in enumerate(me.hand, 1)],
            foe_hero=foe.hero.display_name if foe.hero else "-",
            foe_hp=foe.hero_hp,
            foe_armor=foe.armor,
            foe_mana=f"{foe.mana}/{foe.mana_max}",
            foe_hand_count=len(foe.hand),
            foe_board=[
                (e.display_name, f"{e.stats[0]}/{e.stats[1]}", mech_with_status(e, False))
                for e in foe.board
            ],
            foe_deck=foe.deck_count if foe.deck_count_known else "?",
            my_deck=me.deck_count if me.deck_count_known else "?",
            battle_log=[l for l in recon.battle_log[-60:]],
        )


def _say(text: str) -> None:
    """系统 TTS 播报结论首句。"""
    try:
        first = text.splitlines()[0].replace("【结论】", "").strip()
        if not first:
            return
        for voice in ("Tingting", "Meijia", ""):
            cmd = ["say"] + ([f"-v", voice] if voice else []) + [first]
            r = subprocess.run(cmd, check=False, capture_output=True)
            if r.returncode == 0:
                return
    except Exception:
        pass


def _nscolor(rgba):
    """(r,g,b,a) → NSColor。"""
    import AppKit

    return AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(*rgba)


def _font(size: float, bold: bool = False, mono: bool = False):
    """获取字体。PyObjC 的字体 API 是 ``NSFont.fontWithName_size_``，没有 NSBoldFontOfSize_。"""
    import AppKit

    if mono:
        return AppKit.NSFont.fontWithName_size_("Menlo", size) or AppKit.NSFont.userFixedPitchFontOfSize_(size)
    if bold:
        return AppKit.NSFont.fontWithName_size_("Helvetica-Bold", size) or AppKit.NSFont.boldSystemFontOfSize_(size)
    return AppKit.NSFont.systemFontOfSize_(size)


def _style_for(line: str) -> tuple[float, tuple]:
    """按建议行的前缀决定字号与颜色（斩杀标红加粗）。"""
    if line.startswith("【斩杀】"):
        return 14.0, C_LETHAL
    if line.startswith("【结论】"):
        return 13.0, C_TITLE
    if line.startswith("【"):
        return 11.0, C_DIM
    if line.startswith("  -"):
        return 11.0, C_WARN
    return 12.0, C_TEXT


def _build_rich(lines: list[str]):
    """直接拼装 NSAttributedString。

    不走 HTML 解析：PyObjC 的 ``initWithHTML:...`` 在本机版本上签名不匹配，
    且 HTML 解析会带来额外开销与转义风险。
    """
    import AppKit

    storage = AppKit.NSMutableAttributedString.alloc().init()
    for i, line in enumerate(lines):
        size, color = _style_for(line)
        font = _font(size, bold=line.startswith(("【结论】", "【斩杀】")))
        attrs = {
            AppKit.NSFontAttributeName: font,
            AppKit.NSForegroundColorAttributeName: (
                AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(*color)
            ),
        }
        storage.appendAttributedString_(
            AppKit.NSAttributedString.alloc().initWithString_attributes_(line, attrs)
        )
        if i < len(lines) - 1:
            storage.appendAttributedString_(
                AppKit.NSAttributedString.alloc().initWithString_attributes_("\n", attrs)
            )
    return storage


def resolve_position(AppKit, W: int, H: int) -> tuple[float, float]:
    """决定浮窗坐标。

    实测本机双屏：主屏 1728x1117（炉石全屏在此）+ 副屏 1440x2560 竖屏（工作窗口在此）。
    炉石全屏时，浮窗放在炉石所在屏会被盖住/看不见；放另一屏又可能落在视线之外。
    因此提供多级策略，默认放「炉石之外的屏」，并支持参数强制指定屏与位置。

    环境变量：
      HS_OVERLAY_SCREEN = auto（默认）| main | game | 0 | 1
      HS_OVERLAY_POS    = left（默认） | center | right
    """
    env_screen = os.environ.get("HS_OVERLAY_SCREEN", "auto").lower()
    env_pos = os.environ.get("HS_OVERLAY_POS", "left").lower()
    screens = list(AppKit.NSScreen.screens())

    def place(scr, mode):
        vis = scr.visibleFrame()
        if mode == "right":
            x = vis.origin.x + vis.size.width - W - 20
        elif mode == "center":
            x = vis.origin.x + (vis.size.width - W) / 2
        else:
            x = vis.origin.x + 20
        y = vis.origin.y + vis.size.height - H - 30
        return x, y

    # 指定屏序号
    if env_screen.isdigit():
        idx = int(env_screen)
        if 0 <= idx < len(screens):
            return place(screens[idx], env_pos)
    if env_screen == "main":
        return place(AppKit.NSScreen.mainScreen(), env_pos)
    if env_screen == "game":
        return place(find_target_screen(AppKit), env_pos)

    # auto：优先放非炉石屏
    game_screen = find_target_screen(AppKit)
    others = [s for s in screens if s != game_screen]
    if others:
        return place(others[0], env_pos)
    return place(game_screen, env_pos)


def find_target_screen(AppKit):
    """定位浮窗应放到的屏幕：优先炉石窗口所在屏，其次主屏。

    实测本机是双屏（主屏 1728x1008横向 + 副屏 1440x2560 竖屏），炉石与主力工作
    窗口常分处两块屏。若浮窗固定放主屏，可能被炉石窗口盖住或落在注意力之外，
    让人误以为「没启动」。
    """
    try:
        import Quartz

        wins = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
        )
        for w in wins or []:
            owner = (w.get("kCGWindowOwnerName") or "")
            if "hearthstone" not in owner.lower() and "炉石" not in owner:
                continue
            b = w.get("kCGWindowBounds") or {}
            if b.get("Width", 0) < 200:
                continue
            cx = b.get("X", 0) + b.get("Width", 0) / 2
            cy = b.get("Y", 0) + b.get("Height", 0) / 2
            for sc in AppKit.NSScreen.screens():
                f = sc.frame()
                if (
                    f.origin.x <= cx <= f.origin.x + f.size.width
                    and f.origin.y <= cy <= f.origin.y + f.size.height
                ):
                    return sc
    except Exception:
        pass
    return AppKit.NSScreen.mainScreen()


class Overlay:
    """置顶悬浮窗。未安装 PyObjC 时降级为终端输出。"""

    def __init__(self, voice: bool = False, expanded: bool = True):
        self.voice = voice
        self.expanded = expanded
        self.available = self._check()
        self._panels: list = []
        self._adv_text = ""
        self._snap: BoardSnapshot | None = None
        self._building = False

    @staticmethod
    def _check() -> bool:
        try:
            import AppKit  # noqa: F401

            return True
        except ImportError:
            return False

    # ── 外部接口 ──────────────────────────────────────────
    def update_board(self, snap: BoardSnapshot) -> None:
        """更新局面（高频，不必重绘建议区）。"""
        self._snap = snap
        if self.available:
            self._on_main(self._render_board)
        else:
            self._print_board(snap)

    def update_advice(self, adv: Advice, title: str = "战术建议") -> None:
        """更新建议区。"""
        self._adv_text = adv.to_text()
        self._adv_title = title
        if self.voice:
            _say(self._adv_text)
        if self.available:
            self._on_main(self._render_advice)
        else:
            print(f"\n[{title}]\n{self._adv_text}\n")

    def close(self) -> None:
        if getattr(self, "available", False):
            for p in self._panels:
                try:
                    p.close()
                except Exception:
                    pass
            self._panels.clear()

    def run(self) -> None:
        """进入事件循环（仅 GUI 模式）。"""
        if not self.available:
            return
        import AppKit

        self.build()
        AppKit.NSApplication.sharedApplication()
        AppKit.NSApp().activateIgnoringOtherApps_(False)
        AppKit.NSApp().run()

    # ── 构建界面 ──────────────────────────────────────────
    def build(self) -> None:
        import AppKit

        W, H = 400, 620
        x, y = resolve_position(AppKit, W, H)

        panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            # PyObjC 严格要求 NSRect 结构类型与整数 backingStore，
            # 传普通 tuple / dict 会 struct 解包失败
            AppKit.NSMakeRect(x, y, W, H),
            _window_style_mask(),
            AppKit.NSBackingStoreBuffered,
            True,
        )
        panel.setFloatingPanel_(True)
        # 关键：置于最上层但不抢焦点，游戏操作不受影响
        panel.setLevel_(AppKit.NSFloatingWindowLevel)
        panel.setHidesOnDeactivate_(False)
        panel.setOpaque_(False)
        panel.setHasShadow_(True)
        panel.setTitleVisibility_(AppKit.NSWindowTitleHidden)
        panel.setMovableByWindowBackground_(True)

        # 半透明毛玻璃底 + 细边框（style dict 只能给 NSVisualEffectView用，
        # NSPanel 用它会因 struct不匹配抛 ValueError）
        vev = AppKit.NSVisualEffectView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, W, H))
        vev.setMaterial_(AppKit.NSVisualEffectMaterialHUDWindow)
        vev.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
        vev.setState_(AppKit.NSVisualEffectStateActive)
        vev.setWantsLayer_(True)
        vev.layer().setCornerRadius_(14.0)
        vev.layer().setMasksToBounds_(True)
        vev.layer().setBorderWidth_(1.0)
        vev.layer().setBorderColor_(
            AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(*C_BORDER).CGColor()
        )
        vev.layer().setBackgroundColor_(
            AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(*C_BG).CGColor()
        )
        panel.contentView().addSubview_(vev)

        container = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, W, H))
        panel.contentView().addSubview_(container)
        self._container = container
        self._panel = panel

        self._W, self._full_h = W, H
        self._collapsed = False
        # 标题栏左侧的控制按钮（浮窗无系统标题栏，得自己画）
        self._make_window_buttons()

        pad = 14
        y_cursor = H - pad

        # ── 标题栏 ──
        # 左侧留出三个控制按钮的空间（约 56pt），避免文字被压住
        title_x = pad + 56
        self._status_label = self._label(
            "等待对局…", size=13, color=C_TITLE, bold=True,
            frame=(title_x, y_cursor - 20, 250 - 56, 20),
        )
        # 右侧固定显示本机玩家名 —— 用于随时确认「工具认的是不是我」
        me_name = (self._snap.names.get(self._snap.my_pid, "") if self._snap else "")
        self._me_label = self._label(
            f"本机：{me_name}" if me_name else "本机：?",
            size=11,
            color=C_ME,
            frame=(W - pad - 130, y_cursor - 20, 130, 20),
        )
        self._me_label.setAlignment_(2)  # 右对齐
        y_cursor -= 32

        # ── 双方状态条 ──
        self._foe_bar = self._label(
            "", size=12, color=C_FOE, frame=(pad, y_cursor - 42, W - pad * 2, 42)
        )
        y_cursor -= 50
        self._me_bar = self._label(
            "", size=12, color=C_ME, frame=(pad, y_cursor - 42, W - pad * 2, 42)
        )
        y_cursor -= 52

        # ── 建议区 ──
        self._adv_title_label = self._label(
            "战术建议", size=12, color=C_ACCENT, bold=True,
            frame=(pad, y_cursor - 16, W - pad * 2, 16),
        )
        y_cursor -= 20
        # 建议区高度：占上半部，给日志留至少 140pt
        log_min = 140
        adv_h = max(180, (y_cursor - pad) - log_min - 26)
        self._adv_view, _ = self._scroll_text(
            frame=(pad, y_cursor - adv_h, W - pad * 2, adv_h)
        )
        self._adv_area_h = adv_h
        y_cursor -= adv_h + 6

        # ── 战况日志 ──
        self._log_title = self._label(
            "实时战况", size=12, color=C_ACCENT, bold=True,
            frame=(pad, y_cursor - 15, W - pad * 2, 15),
        )
        y_cursor -= 18
        self._log_view, log_h = self._scroll_text(
            frame=(pad, pad, W - pad * 2, max(110, y_cursor - pad))
        )

        panel.setContentSize_(AppKit.NSMakeSize(W, H))
        self._refresh_me_label()
        self._panels.append(panel)
        panel.orderFrontRegardless()
        self._render_board()
        self._render_advice()

    # ── UI 辅助 ───────────────────────────────────────────
    # ── 窗口控制按钮 ──────────────────────────────────────
    # 浮窗是无边框 NSPanel，没有系统标题栏，也没有系统关闭按钮，
    # 用户无法点掉它。这里自绘 macOS 三键风格（关闭/最小化/收起建议），
    # 让浮窗至少可以被用户主动关闭或临时收起，不至于只能杀进程。
    _BTN_R = 6.0      # 半径
    _BTN_GAP = 17.0   # 按钮间距
    _BTN_TOP = 11.0   # 距顶距离

    def _dot_button(self, cx: float, cy: float, color, action, tip: str):
        """画一个圆形控制按钮（NSButton + layer 圆形背景）。"""
        import AppKit

        d = self._BTN_R * 2
        btn = AppKit.NSButton.alloc().initWithFrame_(
            AppKit.NSMakeRect(cx - self._BTN_R, cy - self._BTN_R, d, d)
        )
        btn.setTitle_("")
        btn.setBezelStyle_(AppKit.NSBezelStyleShadowlessSquare)
        btn.setBordered_(False)
        btn.setWantsLayer_(True)
        btn.layer().setCornerRadius_(self._BTN_R)
        btn.layer().setMasksToBounds_(True)
        btn.layer().setBackgroundColor_(
            AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(*color).CGColor()
        )
        btn.setToolTip_(tip)
        btn.setTarget_(self)
        btn.setAction_(action)
        self._container.addSubview_(btn)
        return btn

    def _make_window_buttons(self) -> None:
        """标题栏左侧三个圆点：关闭 / 收起建议 / 折叠面板。"""
        import AppKit

        cy = self._container.frame().size.height - self._BTN_TOP
        cx = self._BTN_TOP
        self._btn_close = self._dot_button(
            cx, cy, C_DOT_CLOSE, "on_close_", "关闭军师（退出进程）"
        )
        cx += self._BTN_GAP
        self._btn_collapse = self._dot_button(
            cx, cy, C_DOT_MIN, "on_collapse_", "折叠到标题栏 / 展开"
        )
        cx += self._BTN_GAP
        self._btn_hide = self._dot_button(
            cx, cy, C_DOT_MAX, "on_hide_", "精简/完整切换（只留局面条）"
        )

    # ── 按钮回调 ──────────────────────────────────────────
    def on_close_(self, sender) -> None:
        """关闭按钮：关窗并退出进程。"""
        import AppKit

        self.close()
        AppKit.NSApp().terminate_(None)

    def on_collapse_(self, sender) -> None:
        """折叠/展开面板（不退出，方便长时间挂着）。"""
        import AppKit

        self._collapsed = not getattr(self, "_collapsed", False)
        h = 46 if self._collapsed else self._full_h
        for p in self._panels:
            # PyObjC 的 NSWindow 只有 setFrame_display_，没有 setFrame_
            f = p.frame()
            p.setFrame_display_(
                AppKit.NSMakeRect(f.origin.x, f.origin.y, f.size.width, h), True
            )
        self._panel.setContentSize_(AppKit.NSMakeSize(self._W, h))

    def on_hide_(self, sender) -> None:
        """绿色：切换「精简模式」—— 只保留双方状态条，隐藏建议与日志。

        精简模式下浮窗高度收窄到 140pt，便于在游戏全屏时完全不影响视野；
        再点一次恢复完整面板。比 ``miniaturize`` 可靠——
        后者对 ``NSFloatingWindowLevel`` 的面板不生效。
        """
        import AppKit

        self._compact = not getattr(self, "_compact", False)
        h = 140 if self._compact else self._full_h
        for p in self._panels:
            # PyObjC 的 NSWindow 只有 setFrame_display_（无 setFrame_）
            f = p.frame()
            p.setFrame_display_(
                AppKit.NSMakeRect(f.origin.x, f.origin.y, f.size.width, h), True
            )
        self._panel.setContentSize_(AppKit.NSMakeSize(self._W, h))

        # 隐藏下方面板
        for name in ("_adv_title_label", "_adv_view", "_log_title", "_log_view"):
            obj = getattr(self, name, None)
            if obj is not None:
                obj.setHidden_(self._compact)
        # 重新布局上半部分（状态条要顶上来）
        self._layout_top()

    def _layout_top(self) -> None:
        """把标题栏与双方状态条重新贴到面板顶部。

        面板高度在「完整 /精简」两种模式间切换后，子视图坐标不会自动跟随，
        需要按**当前**面板高度自上而下重排一次。

        坐标系说明：PyObjC 的 NSView 原点在**左下角**，所以 y 越小越靠上。
        """
        import AppKit

        if not hasattr(self, "_foe_bar"):
            return
        W = self._W
        pad = 14
        top = self._panel.contentView().frame().size.height

        # 标题行（高 20）
        y = top - pad - 20
        self._status_label.setFrame_(
            AppKit.NSMakeRect(pad + 56, y, W - pad * 2 - 56 - 130, 20)
        )
        self._me_label.setFrame_(AppKit.NSMakeRect(W - pad - 130, y, 130, 20))
        y -= 8

        # 两条状态条（各高 42）
        self._foe_bar.setFrame_(AppKit.NSMakeRect(pad, y - 42, W - pad * 2, 42))
        y -= 50
        self._me_bar.setFrame_(AppKit.NSMakeRect(pad, y - 42, W - pad * 2, 42))

    def _label(self, text: str, size=12, color=C_TEXT, bold=False, frame=(0, 0, 100, 20)):
        import AppKit

        f = frame
        lbl = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(*f))
        lbl.setStringValue_(text)
        lbl.setEditable_(False)
        lbl.setBordered_(False)
        lbl.setDrawsBackground_(False)
        lbl.setSelectable_(False)
        lbl.setFont_(_font(size, bold=bold))
        lbl.setTextColor_(AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(*color))
        self._container.addSubview_(lbl)
        return lbl

    def _scroll_text(self, frame):
        """创建一个带富文本的可滚动文本区，返回 (scrollview, 建议高度)。"""
        import AppKit

        sv = AppKit.NSScrollView.alloc().initWithFrame_(AppKit.NSMakeRect(*frame))
        sv.setHasVerticalScroller_(True)
        sv.setDrawsBackground_(False)
        sv.setBorderType_(0)  # 0 == 无边框（PyObjC 无 NSBorderStyleNone 常量）

        # 文档视图必须比 scroll view 略高，否则首行会被裁掉
        doc_h = frame[3] + 40
        tv = AppKit.NSTextView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, frame[2] - 12, doc_h)
        )
        tv.setEditable_(False)
        tv.setDrawsBackground_(False)
        tv.setRichText_(True)  # 接受富文本
        tv.setImportsGraphics_(False)
        tv.setVerticallyResizable_(True)
        tv.setHorizontallyResizable_(False)
        tv.setAutoresizingMask_(0)
        # 宽度跟随 scroll view，高度随内容增长（1e7 = 不限高）
        tv.textContainer().setContainerSize_(AppKit.NSMakeSize(frame[2] - 16, 1e7))
        tv.textContainer().setWidthTracksTextView_(True)
        tv.setVerticallyResizable_(True)
        sv.setDocumentView_(tv)
        self._container.addSubview_(sv)
        return sv, frame[3]

    def _set_rich(self, scrollview, text: str, size=12, color=C_TEXT, mono=False):
        """给 scroll view 里的 NSTextView 填充纯文本并滚到底。"""
        import AppKit

        tv = scrollview.documentView()
        font = _font(size - 1 if mono else size, mono=mono)
        attrs = {
            AppKit.NSFontAttributeName: font,
            AppKit.NSForegroundColorAttributeName: AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(*color),
            # 关闭连字，中文/数字混排更整齐
            AppKit.NSLigatureAttributeName: 0,
        }
        astr = AppKit.NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        tv.textStorage().setAttributedString_(astr)
        # 文档视图高度跟随内容，滚动到底部
        w = scrollview.frame().size.width - 16
        h = max(scrollview.frame().size.height, astr.size().height + 12)
        tv.setFrameSize_(AppKit.NSMakeSize(w, h))
        tv.scrollRangeToVisible_((len(text), 0))

    # ── 渲染 ──────────────────────────────────────────────
    def _refresh_me_label(self) -> None:
        """刷新「本机玩家」标识——用于随时确认工具认的是不是自己。"""
        lbl = getattr(self, "_me_label", None)
        if lbl is None or self._snap is None:
            return
        name = self._snap.names.get(self._snap.my_pid, "")
        lbl.setStringValue_(f"本机：{name}" if name else "本机：未识别")

    def _render_board(self) -> None:
        if not self.available or self._snap is None:
            return
        s = self._snap
        if not s.in_progress:
            # 终局 / 未开局：清空双方状态条与日志，只在标题栏说明原因。
            # 不清的话会把上一局的血量与随从一直挂着——实测套牌界面下就是这样，
            # 让人以为「工具还没更新」。
            self._status_label.setStringValue_(s.state or "等待对局")
            self._me_bar.setStringValue_(
                f"▼我方 {s.names.get(s.my_pid, '')}  —"
            )
            self._foe_bar.setStringValue_(
                f"▲对手 {s.names.get(2 if s.my_pid == 1 else 1, '')}  —"
            )
            self._me_bar.setTextColor_(_nscolor(C_DIM))
            self._foe_bar.setTextColor_(_nscolor(C_DIM))
            self._refresh_me_label()
            self._set_rich(
                self._log_view,
                s.battle_log[-1] if s.battle_log else "（等待对局）",
                size=10,
                color=C_DIM,
                mono=True,
            )
            return

        # 对局进行中：恢复各自的标识色
        self._me_bar.setTextColor_(_nscolor(C_ME))
        self._foe_bar.setTextColor_(_nscolor(C_FOE))

        status = f"第 {s.turn} 回合 · {s.step or '—'}"
        self._status_label.setStringValue_(status)
        self._refresh_me_label()

        my_name = s.names.get(s.my_pid, "")
        foe_pid = 2 if s.my_pid == 1 else 1
        foe_name = s.names.get(foe_pid, "")
        # 标识带玩家名并用「▼」标出我方 —— 炉石界面里对手在上、我方在下，
        # 浮窗若把「我方」放上方会持续造成敌我误读。
        my_tag = f"▼我方 {my_name}" if my_name else "▼我方"
        foe_tag = f"▲对手 {foe_name}" if foe_name else "▲对手"

        def fmt_board(items) -> str:
            if not items:
                return "（空）"
            return "  ".join(
                f"{n}{st}{('[' + m + ']' if m else '')}" for n, st, m in items
            )

        def fmt_locations(items) -> str:
            # 地标与随从分开显示，避免被误读成「一个 0 攻随从」
            return "\n地标：" + "  ".join(items) if items else ""

        my_line = (
            f"{my_tag}  {s.my_hero}  {s.my_hp}血" + (f"+{s.my_armor}甲" if s.my_armor else "")
            + f"   法力 {s.my_mana}   手牌 {s.my_hand_count}   牌库 {s.my_deck}\n"
            + fmt_board(s.my_board) + fmt_locations(s.my_locations)
        )
        foe_line = (
            f"{foe_tag}  {s.foe_hero}  {s.foe_hp}血" + (f"+{s.foe_armor}甲" if s.foe_armor else "")
            + f"   法力 {s.foe_mana}   手牌 {s.foe_hand_count}   牌库 {s.foe_deck}\n"
            + fmt_board(s.foe_board) + fmt_locations(s.foe_locations)
        )
        self._me_bar.setStringValue_(my_line)
        self._foe_bar.setStringValue_(foe_line)

        lines = [l for l in s.battle_log if "受到 0 点伤害" not in l][-40:]
        self._set_rich(self._log_view, "\n".join(lines) or "（等待对局）",
                       size=10, color=C_DIM, mono=True)

    def _render_advice(self) -> None:
        if not self.available:
            return
        import AppKit

        self._adv_title_label.setStringValue_(getattr(self, "_adv_title", "战术建议"))
        text = self._adv_text or "等待我方回合…"
        storage = _build_rich(text.splitlines())

        tv = self._adv_view.documentView()
        w = self._adv_view.frame().size.width - 16
        view_h = self._adv_view.frame().size.height
        tv.textStorage().setAttributedString_(storage)
        tv.textContainer().setContainerSize_(AppKit.NSMakeSize(w, 1e7))
        # 文档视图高度固定为可视区高度，内容超出则由 scroll view 滚动。
        # 不做「按内容收缩 scroll view」的动态调整——实测会导致顶部留白、
        # 滚动位置跳动，反而更难读。
        tv.setFrameSize_(AppKit.NSMakeSize(w, view_h))
        tv.setFrameOrigin_(AppKit.NSMakePoint(0, 0))
        tv.scrollRangeToVisible_((len(storage.string()), 0))

        # ── 线程调度 ──────────────────────────────────────────
    def _on_main(self, fn) -> None:
        """在主线程执行（watcher 在后台线程读日志时使用）。"""
        import AppKit

        def trampoline():
            try:
                fn()
            except Exception:
                pass

        if AppKit.NSThread.isMainThread():
            trampoline()
        else:
            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(trampoline)

    # ── 降级输出 ──────────────────────────────────────────
    @staticmethod
    def _print_board(s: BoardSnapshot) -> None:
        if not s.in_progress:
            print(f"[{s.state}]")
            return
        print(
            f"第{s.turn}回合 {s.step} | 我方 {s.my_hero} {s.my_hp}血 法力{s.my_mana} "
            f"手牌{s.my_hand_count} | 对手 {s.foe_hero} {s.foe_hp}血 "
            f"法力{s.foe_mana} 手牌{s.foe_hand_count}"
        )