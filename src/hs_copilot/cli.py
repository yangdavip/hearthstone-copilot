"""
炉石助手 · CLI 入口

子命令：
  board      解析当前/指定日志，打印局面（不上LLM）
  advise     解析局面并请求大模型建议
  watch      实时监听，换牌与回合开始时给出建议
  replay     离线回放日志
  refresh-cards  刷新卡牌数据
  doctor     环境自检
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.json import JSON
from rich.panel import Panel
from rich.table import Table

from .advisor import Advice, LLMClient, detect_lethal
from .cards import CardDB
from .config import Config, find_latest_power_log
from .deck import find_active_deck, resolve_active_deck, resolve_deck
from .log_parser import GameStateReconstructor, parse_log_file
from .planner import DeckProfile, PlanEngine
from .watcher import LogWatcher

app = typer.Typer(add_completion=False, help="炉石传说 AI 助手（macOS）")
console = Console()


def _resolve_log(log: str | None, cfg: Config) -> Path:
    if log:
        return Path(log)
    if cfg.log_path:
        return Path(cfg.log_path)
    p = find_latest_power_log()
    if not p:
        console.print(
            "[red]未找到 Power.log。[/red]\n"
            "请检查：~/Library/Preferences/Blizzard/Hearthstone/log.config 是否开启 [Power]；"
            "或用 --log 指定路径。",
        )
        raise typer.Exit(1)
    return p


def _render_board(recon: GameStateReconstructor, my_pid: int, show_json: bool) -> None:
    if show_json:
        # rich.print_json 只接受 str 或 data=，直接传 dict 会抛 TypeError；
        # 同时按本机 PlayerID 导出，避免与 GUI 的敌我方向相反。
        console.print_json(data=recon.board_state(my_pid))
        return

    me = recon.player_view(my_pid)
    foe = recon.player_view(2 if my_pid == 1 else 1)
    my_name = recon.player_names.get(my_pid, "")
    foe_name = recon.player_names.get(2 if my_pid == 1 else 1, "")
    me_tag = f"我方 {my_name}" if my_name else "我方"
    foe_tag = f"对手 {foe_name}" if foe_name else "对手"

    def hero_text(v) -> str:
        """对局刚开始时英雄实体尚未建立，需容错。"""
        if not v.hero:
            return "未建立"
        return f"{v.hero.display_name} {v.hero_atk}/{v.hero_hp}+{v.armor}甲"

    table = Table(title=f"炉石局面 · 第 {recon.turn} 回合 · {recon.step or '未知步骤'}", show_header=True)
    table.add_column("对象", style="cyan")
    table.add_column("我方", style="green")
    table.add_column("对手", style="red")
    table.add_row("英雄", hero_text(me), hero_text(foe))
    table.add_row("法力", f"{me.mana}/{me.mana_max}", f"{foe.mana}/{foe.mana_max}")
    table.add_row("手牌", f"{len(me.hand)} 张", f"{len(foe.hand)} 张")
    table.add_row(
        "场面",
        f"{len(me.board)} 随从（{sum(1 for e in me.board if e.attack_status()['can_attack'])} 本回合可攻击）",
        f"{len(foe.board)} 随从",
    )
    table.add_row("地标", f"{len(me.locations)}", f"{len(foe.locations)}")
    table.add_row("牌库",
                  str(me.deck_count) if me.deck_count_known else "?",
                  str(foe.deck_count) if foe.deck_count_known else "?")
    table.add_row("奥秘", f"{len(me.secrets)}", f"{len(foe.secrets)}")
    table.add_row("疲劳", f"{me.fatigue}", f"{foe.fatigue}")
    console.print(table)
    if my_name or foe_name:
        console.print(f"[dim]{me_tag} ｜ {foe_tag}[/dim]")

    for title, view, mine in (("我方场面", me, True), ("对手场面", foe, False)):
        if not view.board:
            continue
        rows = []
        for e in view.board:
            atk, hp = e.stats
            mech = " ".join(e.mechanics())
            st = e.attack_status()
            if mine and st["short"]:
                mech = (mech + " " + st["short"]).strip()
            elif not mine and st["just_summoned"]:
                mech = (mech + " 刚召唤·下回合可打").strip()
            rows.append(f"[{e.zone_pos}] {e.display_name} {atk}/{hp} {mech}")
        console.print(Panel("\n".join(rows), title=title, expand=False))

    for title, view in (("我方地标", me), ("对手地标", foe)):
        if not view.locations:
            continue
        rows = [
            f"[位置{e.zone_pos}] {e.display_name} 剩余{e.stats[1]}耐久"
            + ("（本回合已用）" if e.location_used else "")
            for e in view.locations
        ]
        console.print(Panel("\n".join(rows), title=title, expand=False))

    if me.hand:
        rows = []
        for i, e in enumerate(me.hand, 1):
            atk, hp = e.stats
            cost = e.cost
            rows.append(f"#{i} {e.display_name} ({cost}费 {atk}/{hp})")
        console.print(Panel("\n".join(rows), title="我方手牌", expand=False))


@app.command()
def board(
    log: str = typer.Option(None, "--log", "-l", help="Power.log 路径"),
    json_out: bool = typer.Option(False, "--json", help="以 JSON 输出"),
    my_pid: int = typer.Option(1, "--me", help="自己对应的玩家编号"),
    config: str = typer.Option(None, "--config", "-c", help="配置文件路径"),
) -> None:
    """解析日志并打印当前局面。"""
    cfg = Config.load(config)
    p = _resolve_log(log, cfg)
    # --json 必须输出可被直接解析的纯 JSON，不能混入提示行
    if not json_out:
        console.print(f"[dim]日志：{p}[/dim]")
    recon = parse_log_file(p, player_name=cfg.player_name, account_id=cfg.account_id)
    # account_id 优先于 --me
    resolved = recon.resolve_my_pid()
    if resolved:
        my_pid = resolved
    _render_board(recon, my_pid, json_out)


@app.command()
def advise(
    log: str = typer.Option(None, "--log", "-l", help="Power.log 路径"),
    my_pid: int = typer.Option(1, "--me", help="自己对应的玩家编号"),
    config: str = typer.Option(None, "--config", "-c", help="配置文件路径"),
    deckstring: str = typer.Option(None, "--deck", help="套牌码，覆盖配置"),
) -> None:
    """解析局面并请求大模型战术建议。"""
    cfg = Config.load(config)
    if deckstring:
        cfg.deckstring = deckstring
    p = _resolve_log(log, cfg)

    console.print(f"[dim]日志：{p}[/dim]")
    console.print(f"[dim]模型：{cfg.llm.model} @ {cfg.llm.base_url}（key {cfg.llm.masked_key()}）[/dim]")
    if not cfg.llm.api_key:
        console.print("[yellow]未配置 API Key，将使用本地启发式建议。[/yellow]")

    recon = parse_log_file(p, player_name=cfg.player_name, account_id=cfg.account_id)
    resolved = recon.resolve_my_pid()
    if resolved:
        my_pid = resolved
    _render_board(recon, my_pid, show_json=False)
    console.rule("[bold green]军师建议")
    try:
        db = CardDB.load(cfg.cards_path)
    except FileNotFoundError:
        db = None
    deck, active = resolve_active_deck(db, cfg.deckstring, p) if db else (None, None)
    if active:
        console.print(
            f"[dim]套牌：{active.name}（Decks.log 自动识别 · {deck.size} 张 · ID {active.deck_id}）[/dim]"
        )
    client = LLMClient(cfg)
    if db:
        engine = PlanEngine(client, db, enabled=cfg.watch.plan_enabled)
        game_plan, profile = engine.update(recon, deck, my_pid)
        progress = engine.progress(recon, my_pid)
        result = client.advise(
            recon, db, deck, my_pid,
            plan=game_plan, profile=profile, plan_progress=progress,
        )
    else:
        result = Advice(summary="卡牌数据缺失，无法生成建议")
    console.print(Panel(result.to_text(), border_style="green", expand=False))


@app.command()
def plan(
    log: str = typer.Option(None, "--log", "-l", help="Power.log 路径"),
    my_pid: int = typer.Option(1, "--me", help="自己对应的玩家编号"),
    config: str = typer.Option(None, "--config", "-c", help="配置文件路径"),
    json_out: bool = typer.Option(False, "--json", help="以 JSON 输出完整计划"),
    refresh: bool = typer.Option(False, "--refresh", help="强制重新规划（忽略缓存）"),
) -> None:
    """查看本局套牌的整局计划：套牌定位、致胜条件、阶段目标、资源纪律。"""
    cfg = Config.load(config)
    p = _resolve_log(log, cfg)
    try:
        db = CardDB.load(cfg.cards_path)
    except FileNotFoundError:
        console.print("[red]卡牌数据缺失，请先运行 refresh-cards。[/red]")
        raise typer.Exit(1)
    recon = parse_log_file(p, player_name=cfg.player_name, account_id=cfg.account_id)
    resolved = recon.resolve_my_pid()
    if resolved:
        my_pid = resolved
    deck, active = resolve_active_deck(db, cfg.deckstring, p)
    if not deck.size:
        console.print(
            "[yellow]未识别到套牌：检查同目录 Decks.log 是否有「Finding Game With Deck:」，"
            "或在 config.yaml 填 deckstring。[/yellow]"
        )
        raise typer.Exit(1)
    client = LLMClient(cfg)
    engine = PlanEngine(client, db, enabled=True)
    game_plan, _profile = engine.update(recon, deck, my_pid, force=refresh)
    if game_plan is None:
        console.print("[red]整局计划生成失败。[/red]")
        raise typer.Exit(1)
    if json_out:
        console.print_json(data={
            "deck": {"name": deck.deck_name, "id": deck.deck_id,
                     "size": deck.size, "unidentified": len(deck.unknown_dbfids),
                     "source": active.source if active else "config"},
            "plan": game_plan.to_dict(),
            "progress": engine.progress(recon, my_pid),
        })
        return
    lines = [
        f"套牌：{deck.deck_name}（{deck.format_name} · {deck.size} 张 · {game_plan.source}）",
        f"定位：{game_plan.archetype}",
        f"致胜：{game_plan.win_condition}",
        "",
    ]
    for s in game_plan.stages:
        lines.append(
            f"  我方 {s.get('my_turn_from')}-{s.get('my_turn_to')} 回合｜{s.get('goal', '')}"
        )
        for a in s.get("actions", []):
            lines.append(f"      · {a}")
    lines += ["", "核心卡："]
    for c in game_plan.core_cards[:12]:
        lines.append(
            f"  {'[留]' if c.get('keep') else '[用]'} {c.get('name')}：{c.get('role')}"
        )
    lines += ["", "资源纪律："]
    lines += [f"  · {r}" for r in game_plan.resource_rules]
    if game_plan.lethal_setup:
        lethal = game_plan.lethal_setup
        comps = lethal.get("components", []) if isinstance(lethal, dict) else []
        lines += ["", "终结组件：" + ("、".join(str(x) for x in comps) or "靠场面持续输出")]
        if isinstance(lethal, dict) and lethal.get("note"):
            lines.append(f"  成型条件：{lethal['note']}")
    lines += ["", f"下一里程碑：{game_plan.next_milestone}",
              f"对局应对：{game_plan.matchup_note}"]
    console.print(Panel("\n".join(lines), title="整局计划", expand=False))


@app.command()
def replay(
    log: str = typer.Option(None, "--log", "-l"),
    my_pid: int = typer.Option(1, "--me"),
    config: str = typer.Option(None, "--config", "-c"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """离线回放日志并输出终局局面。"""
    cfg = Config.load(config)
    p = _resolve_log(log, cfg)
    recon = parse_log_file(p, player_name=cfg.player_name, account_id=cfg.account_id)
    if not json_out:
        console.print(f"[dim]日志：{p}  实体数：{len(recon.entities)}[/dim]")
    _render_board(recon, my_pid, json_out)


@app.command()
def watch(
    log: str = typer.Option(None, "--log", "-l"),
    my_pid: int = typer.Option(1, "--me"),
    config: str = typer.Option(None, "--config", "-c"),
    from_start: bool = typer.Option(False, "--from-start", help="从文件开头开始（默认只跟新增）"),
    once: bool = typer.Option(False, "--once", help="只处理一次后退出"),
) -> None:
    """实时监听对局，在换牌与回合开始时给出建议。"""
    cfg = Config.load(config)
    p = _resolve_log(log, cfg)
    try:
        db = CardDB.load(cfg.cards_path)
    except FileNotFoundError:
        console.print("[red]卡牌数据缺失，请先运行 refresh-cards。[/red]")
        raise typer.Exit(1)
    deck, active_deck = resolve_active_deck(db, cfg.deckstring, p)
    client = LLMClient(cfg)
    plan_engine = PlanEngine(client, db, enabled=cfg.watch.plan_enabled)

    console.print(
        f"[bold green]炉石军师已启动[/]\n"
        f"日志：{p}\n模型：{cfg.llm.model} @ {cfg.llm.base_url}\n"
        f"套牌：{active_deck.name if active_deck else deck.deck_name or '未识别'}"
        f"（{deck.size} 张 · 整局规划 {'开' if cfg.watch.plan_enabled else '关'}）\n"
        f"[dim]等待对局开始…（Ctrl+C 退出）[/dim]"
    )
    watcher = LogWatcher(p, start_from_end=not from_start, player_name=cfg.player_name,
                             account_id=cfg.account_id)
    watcher.follow_latest = True
    last_advice = 0.0
    last_path = watcher.path
    try:
        while True:
            triggers = watcher.poll(timeout=1.0)
            if watcher.path != last_path:
                last_path = watcher.path
                deck, active_deck = resolve_active_deck(db, cfg.deckstring, watcher.path)
                plan_engine.reset()
            # 与 gui 一致：自动判定本机 PlayerID（先后手会换边）
            try:
                resolved = watcher.recon.resolve_my_pid()
            except Exception:
                resolved = 0
            if resolved:
                my_pid = resolved
            watcher.set_my_pid(my_pid)
            for t in triggers:
                if t.kind == "game_start":
                    console.print("[bold blue]── 检测到新对局 ──[/]")
                    continue
                want = (
                    (t.kind == "mulligan" and cfg.watch.trigger_on_mulligan)
                    or (t.kind == "my_turn" and cfg.watch.trigger_on_my_turn)
                )
                if not want:
                    continue
                # 只在我方回合开始给建议：用触发瞬间的快照判断。
                # 不能再拿 t.current_player 与 my_pid 硬比 —— watcher 已用
                # 「行动方名字」放行，多局日志里编号槽位可能残留旧配对，
                # 硬比会把合法建议拦掉（实测浮窗卡在「正在分析你的回合…」）。
                if t.kind == "my_turn":
                    if t.step and t.step != "MAIN_ACTION":
                        continue
                now = time.time()
                if now - last_advice < cfg.watch.cooldown:
                    continue
                last_advice = now
                phase = "mulligan" if t.kind == "mulligan" else "play"
                console.rule(f"[bold green]{'留牌建议' if phase == 'mulligan' else '回合建议'}")
                # 与 gui 一致：同目录内换卡组也要重新识牌
                latest = find_active_deck(watcher.path)
                if latest and (
                    active_deck is None or latest.key != active_deck.key
                ):
                    deck, active_deck = resolve_active_deck(db, cfg.deckstring, watcher.path)
                    plan_engine.reset()
                game_plan, profile = plan_engine.update(t.recon, deck, my_pid)
                progress = plan_engine.progress(t.recon, my_pid)
                adv = client.advise(
                    t.recon, db, deck, my_pid, phase=phase,
                    plan=game_plan, profile=profile, plan_progress=progress,
                )
                console.print(Panel(adv.to_text(), border_style="green", expand=False))
                if once:
                    return
    except KeyboardInterrupt:
        console.print("\n[dim]已退出。[/dim]")


@app.command()
def gui(
    log: str = typer.Option(None, "--log", "-l"),
    my_pid: int = typer.Option(1, "--me"),
    config: str = typer.Option(None, "--config", "-c"),
    from_start: bool = typer.Option(False, "--from-start", help="从文件开头开始监听"),
    no_llm: bool = typer.Option(False, "--no-llm", help="只看局面与日志，不请求大模型"),
    screen: str = typer.Option(
        "auto",
        "--screen",
        help="浮窗所在屏：auto（默认，放炉石之外的屏）/ main / game / 0 / 1",
    ),
    pos: str = typer.Option(None, "--pos", help="屏内位置：left / center / right"),
) -> None:
    """启动可视化悬浮军师：实时局面 + 战况日志 + 战术建议。

    日志监听在后台线程进行，界面在主线程更新，全程只读日志不做任何输入模拟。
    """
    import threading

    import AppKit

    from .overlay import BoardSnapshot, Overlay

    cfg = Config.load(config)
    p = _resolve_log(log, cfg)
    try:
        db = CardDB.load(cfg.cards_path)
    except FileNotFoundError:
        console.print("[red]卡牌数据缺失，请先运行 refresh-cards。[/red]")
        raise typer.Exit(1)

    # 把 --screen / --pos 写入环境变量，供 Overlay.build() 读取
    if screen and screen != "auto":
        os.environ["HS_OVERLAY_SCREEN"] = screen
    if pos:
        os.environ["HS_OVERLAY_POS"] = pos

    # 套牌优先用 Decks.log 自动识别的「本局牌」；放进可变容器，
    # 因为 loop 是闭包，切日志时要重新赋值（直接赋值会 UnboundLocalError）。
    deck, active_deck = resolve_active_deck(db, cfg.deckstring, p)
    session = {"deck": deck, "active": active_deck}
    client = LLMClient(cfg)
    plan_engine = PlanEngine(
        client, db, enabled=cfg.watch.plan_enabled and not no_llm
    )
    ov = Overlay(voice=cfg.watch.voice)
    if not ov.available:
        console.print(
            "[yellow]未安装 PyObjC，可视化界面不可用，将退化为终端输出。[/yellow]\n"
            "[dim]安装：.venv/bin/python -m pip install pyobjc-core pyobjc-framework-Cocoa[/dim]"
        )

    console.print(f"[bold green]可视化军师已启动[/]  日志 {p}")
    console.print(f"[dim]模型 {cfg.llm.model} · Ctrl+C 退出[/dim]")
    console.print(
        f"[dim]套牌 {active_deck.name if active_deck else deck.deck_name or '未识别'}"
        f"（{deck.size} 张）· 整局规划 {'开' if cfg.watch.plan_enabled and not no_llm else '关'}[/dim]"
    )
    # 打印身份判定结果——敌我颠倒时第一眼就能看出问题
    console.print(
        f"[dim]身份判据：player_name={cfg.player_name!r} "
        f"account_id={cfg.account_id!r} my_player_id={my_pid}（后两者会被自动判定覆盖）[/dim]"
    )

    # ★ 启动前检查是否已有实例在跑。
    # 浮窗是独立进程，重复启动会留下多个实例各自跟读同一份日志——
    # 它们加载的代码版本可能不同（改完代码不重启旧实例就会出现「敌我反」）。
    try:
        import Quartz as _Q

        _alive = sorted({
            w.get("kCGWindowOwnerPID")
            for w in _Q.CGWindowListCopyWindowInfo(
                _Q.kCGWindowListOptionAll, _Q.kCGNullWindowID
            ) or []
            if "python" in (w.get("kCGWindowOwnerName") or "").lower()
            and (w.get("kCGWindowBounds") or {}).get("Width", 0) > 300
            and _pid_alive(w.get("kCGWindowOwnerPID"))
        })
        if _alive:
            console.print(
                f"[yellow]⚠️  已有 {len(_alive)} 个军师实例在运行"
                f"（PID {', '.join(str(x) for x in _alive)}）。[/yellow]\n"
                "[dim]   多个实例会各自跟读同一日志、且可能代码版本不同，"
                "显示会互相冲突。[/dim]\n"
                "[cyan]   先关闭旧实例：hs-copilot stop --all[/cyan]"
            )
            if not typer.confirm("仍要启动新实例吗？", default=False):
                raise typer.Exit(0)
    except ImportError:
        pass
    except typer.Exit:
        raise
    except Exception:
        pass

    stop = threading.Event()

    # 本机PlayerID 的共享容器：闭包内需要更新它，用可变对象避免
    # 「赋值需 nonlocal、漏写就UnboundLocalError」这个坑（已踩过一次）。
    me = {"pid": my_pid}

    def resolve_pid(recon) -> int:
        """判定本机 PlayerID：player_name / account_id 优先，--me 仅兜底。

        结果写入共享容器 :data:`me`，供 :func:`loop` 后续使用。
        """
        try:
            resolved = recon.resolve_my_pid()
        except Exception:
            resolved = 0
        if resolved and resolved != me["pid"]:
            console.print(
                f"[dim]身份判定：本机为 PlayerID {resolved}"
                f"（配置 my_player_id={me['pid']}，已自动修正）[/dim]"
            )
        if resolved:
            me["pid"] = resolved
        return me["pid"]

    def loop() -> None:
        """后台线程：读日志 → 更新局面 → 触发建议。"""
        watcher = LogWatcher(p, start_from_end=not from_start, player_name=cfg.player_name,
                             account_id=cfg.account_id)
        # 炉石开局后才会写新目录里的 Power.log，允许自动切换过去；
        # 否则主界面期间启动的军师会一直挂在上一份日志上，界面毫无反应。
        watcher.follow_latest = True
        # 关键：先回填当前对局的历史（CREATE_GAME 与 FULL_ENTITY 声明已在日志里），
        # 否则实体表为空 → 血量/场面/手牌全显示 0
        if not from_start:
            watcher.backfill_current_game()
            resolve_pid(watcher.recon)
            # ★ 把身份告知 watcher：只有「轮到我方」才弹建议，
            # 否则后手时对方的回合也会触发。
            watcher.set_my_pid(me["pid"])
            snap0 = BoardSnapshot.from_recon(watcher.recon, me["pid"])
            # 占位文字是否还挂着（尚未被真实建议替换）
            advice_pending = False
            if snap0.in_progress:
                my_turn_now = watcher.recon.is_my_action_phase(me["pid"])
                advice_pending = my_turn_now
                ov.update_advice(
                    Advice(summary=(
                        "正在分析你的回合…" if my_turn_now
                        else "等待你的回合开始…（对手回合不生成建议）"
                    )), "就绪" if my_turn_now else "等待中"
                )
            else:
                ov.update_advice(Advice(summary="等待新对局开始…"), "等待中")

            # 只在「我方回合开始」生成建议。旧版在这里无条件做一次
            # 「启动即分析」，于是在对手回合也会请求模型（用户看到
            # 「第 2 回合建议」却写着「非我方行动回合」）。
            # 现在先 poll 建立增量偏移；若启动时正处于我方 MAIN_ACTION，
            # watcher 的首轮补触发会立刻给出这一回合的建议。
            pending = watcher.poll(timeout=0) if snap0.in_progress else []
        else:
            pending = []
            advice_pending = False
        last_advice = 0.0
        last_board_turn = -1
        last_path = watcher.path

        def wait_for_hand_reveal(max_wait: float = 1.2) -> None:
            """回合刚开始时补读几批日志，等客户端写出手牌卡号。

            Power.log 里起手/新抽的手牌先是 ``UNKNOWN ENTITY ... CardID=``，
            真正的卡号由随后几百毫秒内的 ``SHOW_ENTITY`` 写入。若此刻就把
            payload 发给模型，它会拿到「实体14 / 卡面未知」然后编造建议。
            这里做一次极短等待（上限 1.2 秒），超时仍走 advisor 的未知牌兜底。
            补读期间产生的触发事件追加到 pending，绝不丢弃。
            """
            nonlocal pending
            deadline = time.monotonic() + max_wait
            while time.monotonic() < deadline:
                hand = watcher.recon.player_view(me["pid"]).hand
                if not hand or all(bool(e.card_id) for e in hand):
                    return
                extra = watcher.poll(timeout=0.12)
                if extra:
                    pending.extend(extra)

        try:
            while not stop.is_set():
                triggers = pending + watcher.poll(timeout=0.6)
                pending = []
                recon = watcher.recon

                # 每轮重新确认身份：切到新日志后本机 PlayerID 可能随先后手变化，
                # 且开局早期名字/账号信息尚未写全，等信息到齐要能自动纠正。
                pid = resolve_pid(recon)
                watcher.set_my_pid(pid)
                if watcher.path != last_path:
                    last_path = watcher.path
                    last_board_turn = -1
                    # 新日志目录可能换了卡组：重新读 Decks.log 并让整局计划重建。
                    # 回合号回退（同一日志里开新局）由 PlanEngine.update 自己识别。
                    session["deck"], session["active"] = resolve_active_deck(
                        db, cfg.deckstring, watcher.path
                    )
                    plan_engine.reset()
                    ov.update_advice(
                        Advice(summary="已接入新对局日志，正在跟踪…"), "已连接"
                    )

                # 局面快照：每轮刷新，保证血量/手牌实时
                ov.update_board(BoardSnapshot.from_recon(recon, me["pid"]))

                # 占位文字兜底：启动瞬间可能是我方回合，日志随即切到对手
                # 回合（或合法触发被 cooldown 拦下），不能让「正在分析…」
                # 一直挂在浮窗上。已有的真实建议不会被覆盖。
                if advice_pending and not recon.is_my_action_phase(me["pid"]):
                    ov.update_advice(
                        Advice(summary="等待你的回合开始…（对手回合不生成建议）"),
                        "等待中",
                    )
                    advice_pending = False

                for t in triggers:
                    if t.kind == "game_start":
                        ov.update_advice(
                            Advice(summary="检测到新对局，正在跟踪…"), "对局开始"
                        )
                        last_board_turn = -1
                        continue

                    # 回合数变化时也刷新一次建议（同一回合内不重复请求）
                    if t.kind == "my_turn":
                        # watcher 已完成「行动方名字 == 本机 + 回合间隔 +
                        # 回合去重」的全部判定；这里只确认触发瞬间可行动。
                        # 不要再用编号硬比，否则会拦掉名字判据放行的建议。
                        if t.step and t.step != "MAIN_ACTION":
                            continue
                        if recon.turn != last_board_turn:
                            last_board_turn = recon.turn

                    want = (
                        (t.kind == "mulligan" and cfg.watch.trigger_on_mulligan)
                        or (t.kind == "my_turn" and cfg.watch.trigger_on_my_turn)
                    )
                    if not want:
                        continue
                    if (time.time() - last_advice) < cfg.watch.cooldown:
                        continue
                    last_advice = time.time()
                    phase = "mulligan" if t.kind == "mulligan" else "play"
                    # 同一客户端连续多局不会换日志目录，但 Decks.log 会追加新的
                    # 「Finding Game With Deck:」块。每次触发时比对 Deck ID，
                    # 换了卡组就重新识牌并重建整局计划（读 14KB 文本，开销可忽略）。
                    latest = find_active_deck(watcher.path)
                    if latest and (
                        session["active"] is None
                        or latest.key != session["active"].key
                    ):
                        session["deck"], session["active"] = resolve_active_deck(
                            db, cfg.deckstring, watcher.path
                        )
                        plan_engine.reset()
                    cur_deck = session["deck"]
                    title = "起手留牌建议" if phase == "mulligan" else f"第 {t.turn} 回合建议"
                    if no_llm:
                        adv = client._fallback(t.recon, me["pid"], phase)
                    else:
                        # 收到回合触发立刻给出反馈，不等模型返回；
                        # 否则用户会以为工具没反应。首次还要读完整套牌制定
                        # 整局计划（每局只此一次，之后复用缓存，回合建议不额外变慢）。
                        first_plan = (
                            plan_engine.enabled and plan_engine.plan is None
                            and cur_deck is not None and cur_deck.size > 0
                        )
                        ov.update_advice(
                            Advice(summary=(
                                "正在读取套牌并制定整局计划…"
                                if first_plan else "正在分析局面，马上给出建议…"
                            )), "分析中"
                        )
                        advice_pending = True
                        game_plan, profile = plan_engine.update(
                            t.recon, cur_deck, me["pid"]
                        )
                        progress = plan_engine.progress(t.recon, me["pid"])
                        # 先补读日志等卡号到齐，再构造 payload；超时由
                        # _validate_unknown_hand 兜底，不会卡住建议。
                        wait_for_hand_reveal()
                        adv = client.advise(
                            t.recon, db, cur_deck, me["pid"], phase=phase,
                            plan=game_plan, profile=profile, plan_progress=progress,
                        )
                        if game_plan and game_plan.archetype and phase == "play":
                            title += f" · {game_plan.archetype}"
                    ov.update_advice(adv, title)
                    advice_pending = False
        except Exception as e:
            ov.update_advice(Advice(summary=f"监听出错：{type(e).__name__} {e}"), "错误")

    # 先建界面再起线程，避免 UI 尚未就绪就被后台更新
    def start_ui() -> None:
        ov.build()
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        app = AppKit.NSApplication.sharedApplication()
        # Accessory 策略：只显示浮窗、不出现在 Dock、不抢游戏焦点
        try:
            app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        except Exception:
            pass
        app.activateIgnoringOtherApps_(False)
        app.run()
        # run() 返回后仍需阻塞：Python 主线程退出会连带终止 Cocoa 事件循环
        while not stop.is_set():
            time.sleep(3600)

    try:
        # GUI 模式阻塞在事件循环；不可用时后台跑终端模式
        if ov.available:
            start_ui()
        else:
            loop()
    except KeyboardInterrupt:
        stop.set()
        ov.close()
        console.print("\n[dim]已退出。[/dim]")


@app.command("whoami")
def whoami(
    log: str = typer.Option(None, "--log", "-l"),
    config: str = typer.Option(None, "--config", "-c"),
    detect: bool = typer.Option(False, "--detect", help="自动探测本机战网账号 lo 值"),
) -> None:
    """身份自检：显示日志里两名玩家分别是谁，以及本机是哪一个。

    敌我颠倒时先跑这个命令。``--detect`` 会统计各账号在日志里出现的 PlayerID 分布，
    横跨两个位置的即为本机（自己的账号会随先后手换边，而对手账号位置固定）。
    """
    cfg = Config.load(config)
    p = _resolve_log(log, cfg)
    recon = parse_log_file(p, player_name=cfg.player_name, account_id=cfg.account_id)

    console.print(f"[dim]日志：{p}[/dim]\n")

    table = Table(title="玩家身份", show_header=True)
    table.add_column("PlayerID", style="cyan")
    table.add_column("玩家名", style="bold")
    table.add_column("战网lo", style="magenta")
    table.add_column("英雄", style="green")
    table.add_column("血量", style="yellow")
    table.add_column("判断", style="bold")

    resolved = recon.resolve_my_pid()
    for pid in (1, 2):
        v = recon.player_view(pid)
        name = recon.player_names.get(pid, "(未识别)")
        acc = recon.pid_account.get(pid, "?")
        if cfg.account_id:
            if acc == cfg.account_id:
                mark = "[green]◀ 本机[/]"
            else:
                mark = ""
        else:
            mark = "[yellow]（未配置 account_id）[/]" if pid == 1 else ""
        table.add_row(
            str(pid), name, acc,
            v.hero.card_id if v.hero else "-", str(v.hero_hp), mark,
        )
    console.print(table)

    # ── 自动探测本机账号 ──
    if detect or not cfg.account_id:
        from collections import Counter, defaultdict

        import re

        raw = Path(p).read_text(encoding="utf-8", errors="ignore")
        pos = defaultdict(set)
        cnt = Counter()
        for m in re.finditer(
            r"Player EntityID=\d+ PlayerID=(\d+) GameAccountId=\[hi=\d+ lo=(\d+)\]", raw
        ):
            pid, lo = str(m.group(1)), str(m.group(2))
            pos[lo].add(pid)
            cnt[lo] += 1
        # ── 自动探测本机 ──
        # ★ 唯一可靠判据：**参与对局数最多**。每个玩家在本机日志里都是
        #   「一次性的对手」，唯独本机账号贯穿所有对局。
        #⚠️ 「lo / 名字横跨 PlayerID 1/2」**不能**区分敌我——
        #   本机换边时对手也跟着换边，两人都横跨（我曾据此误判）。
        # lo 的作用只是交叉验证：它能确认「某个账号贯穿始终」，
        # 但无法单独证明那个账号是本机。
        from hs_copilot.log_parser import scan_player_identity

        ident = scan_player_identity(p)
        lo_map = ident["lo_map"]
        gc = ident["game_count"]

        console.print(
            "\n[bold]身份证据[/]"
            "[dim]（lo 横跨 PlayerID 1/2 = 本机随先后手换边；"
            "名字参与对局数最多 = 本机的常驻对手）[/dim]"
        )
        t3 = Table(show_header=True)
        t3.add_column("战网 lo", style="magenta")
        t3.add_column("出现过的 PlayerID", justify="right")
        t3.add_column("配对名字", justify="right")
        t3.add_column("")
        for lo in sorted(lo_map, key=lambda x: -len(lo_map[x])):
            pids = lo_map[lo]
            mark = "[cyan]◀ 贯穿始终[/]" if len(pids) > 1 else "[dim]（换边过）[/]"
            t3.add_row(lo, ",".join(str(x) for x in sorted(pids)),
                       ident["name_of_lo"].get(lo, "-"), mark)
        console.print(t3)

        if gc:
            t4 = Table(title="玩家名 · 参与对局数", show_header=True)
            t4.add_column("玩家名", style="bold")
            t4.add_column("对局数", justify="right")
            t4.add_column("出现过的 PlayerID", justify="right")
            t4.add_column("")
            top = max(gc.values())
            for nm in sorted(gc, key=lambda x: (-gc[x], x)):
                mark = "[green]◀ 本机[/]" if gc[nm] == top and top > 1 else ""
                t4.add_row(nm, str(gc[nm]),
                           ",".join(str(x) for x in sorted(ident["names"].get(nm, ()))),
                           mark)
            console.print(t4)

        top = max(gc.values(), default=0)
        top_names = [nm for nm, n in gc.items() if n == top and top > 1]
        my_los = [lo for lo, pids in lo_map.items() if len(pids) > 1]
        if len(top_names) == 1:
            console.print(
                f"\n[green]✓[/] 本机玩家名： [bold]{top_names[0]}[/]"
                f"（参与 {gc[top_names[0]]} 局，其余名字各 1 局）"
            )
        else:
            console.print(
                f"\n[yellow]![/] 对局数判据不唯一（候选 {top_names}），"
                "请对照上表人工确认"
            )
        if my_los:
            console.print(
                f"[dim]参考：{len(my_los)} 个 lo 横跨 PlayerID 1/2"
                f"（{', '.join(my_los)}）—— 该指标无法单独区分敌我[/dim]"
            )
        if len(top_names) == 1:
            console.print(
                f"\n[bold green]→ 请确认配置：[/bold green]\n"
                f'  [cyan]player_name: "{top_names[0]}"[/cyan]\n'
                "[dim]  account_id 可留空，或填 whoami 表格里贯穿始终的那个 lo[/dim]"
            )

    if cfg.account_id:
        if resolved:
            console.print(
                f"\n[green]✓[/] account_id={cfg.account_id} → PlayerID {resolved}"
                f"（我方 my_player_id 应为 {resolved}）"
            )
            if resolved != cfg.my_player_id:
                console.print(
                    f"[yellow]注意[/] my_player_id={cfg.my_player_id} 与自动判定不符，"
                    "GUI 启动时会以 account_id 判定为准"
                )
        else:
            console.print(
                f"\n[yellow]![/] 本局日志里没找到 account_id={cfg.account_id}"
                "（对局尚未开始？）"
            )


def _pid_alive(pid: int) -> bool:
    """探测进程是否存活。沙箱里 ps 可能不可用，用 kill -0 更可靠。"""
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但无权发信号
    except OSError:
        return False
    return True


@app.command("stop")
def stop(
    all: bool = typer.Option(False, "--all", help="关闭全部实例（含残留）"),
) -> None:
    """关闭军师浮窗并退出进程。

    浮窗是独立进程，反复启动会留下多个实例（窗口重叠、CPU 占用翻倍）。
    本命令按**窗口所属 PID** 精确关闭，比 pkill 可靠——pkill 依赖进程名匹配，
    而 GUI 实例的进程名在 ps 里常被截断或包在 shell 包装层内。

    默认只关闭最新的一个实例；``--all`` 关闭全部。
    """
    import os
    import signal
    import time

    try:
        import Quartz
    except ImportError:
        console.print(
            "[yellow]未安装 pyobjc-framework-Quartz，无法按窗口定位。[/yellow]\n"
            "[dim]安装：.venv/bin/python -m pip install pyobjc-framework-Quartz[/dim]"
        )
        raise typer.Exit(1)

    wins = []
    for w in Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID
    ) or []:
        owner = (w.get("kCGWindowOwnerName") or "").lower()
        b = w.get("kCGWindowBounds") or {}
        if "python" in owner and b.get("Width", 0) > 300:
            wins.append(
                (w.get("kCGWindowOwnerPID"), int(b.get("X", 0)), int(b.get("Y", 0)))
            )

    if not wins:
        console.print("[green]✓[/] 没有运行中的军师实例")
        return

    # 同一进程可能登记多个窗口，需去重
    pids = sorted({p for p, _, _ in wins if p})
    console.print(f"发现 {len(pids)} 个军师实例：")
    for pid in pids:
        pos = next((f"X={x} Y={y}" for p, x, y in wins if p == pid), "")
        state = "存活" if _pid_alive(pid) else "僵尸窗口（WindowServer 未回收）"
        console.print(f"  PID {pid}{f'  {pos}' if pos else ''}  [{state}]")

    targets = pids if all else pids[-1:]
    console.print(f"\n[bold]→ 关闭：{', '.join(str(t) for t in targets)}[/]")
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            console.print(f"  [yellow]PID {pid} 无法发送信号：{exc}[/]")

    time.sleep(1.5)
    for pid in targets:
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
                console.print(f"  [dim]PID {pid} 未响应 TERM，已用 KILL[/dim]")
            except OSError:
                pass
    time.sleep(0.5)

    left = [p for p in targets if _pid_alive(p)]
    if left:
        console.print(f"[red]✗[/] 仍有残留：{left}（可能是系统权限限制）")
    else:
        console.print("[green]✓[/] 已全部关闭")


@app.command()
def overlay_info() -> None:
    """查看浮窗将被放在哪块屏的哪个位置（多屏时便于确认）。"""
    import AppKit

    from .overlay import find_target_screen, resolve_position

    screens = list(AppKit.NSScreen.screens())
    game = find_target_screen(AppKit)
    W, H = 400, 620
    x, y = resolve_position(AppKit, W, H)

    table = Table(title="屏幕布局与浮窗位置", show_header=True)
    table.add_column("屏", style="cyan")
    table.add_column("范围(x,y w×h)")
    table.add_column("说明")
    for i, s in enumerate(screens):
        f = s.frame()
        note = "← 炉石在此屏" if s == game else ""
        table.add_row(str(i), f"{f.origin.x:.0f},{f.origin.y:.0f}  {f.size.width:.0f}×{f.size.height:.0f}", note)
    console.print(table)
    console.print(
        f"[green]浮窗将位于[/] x={x:.0f} y={y:.0f}（{W}×{H}）\n"
        f"[dim]可用 --screen 0/1/main/game 指定屏，--pos left/center/right 指定屏内位置[/]"
    )


@app.command("refresh-cards")
def refresh_cards(
    config: str = typer.Option(None, "--config", "-c"),
    locale: str = typer.Option("zhCN", "--locale", help="语言（zhCN / enUS / zhTW...）"),
) -> None:
    """从 HearthstoneJSON 刷新卡牌数据。"""
    import urllib.request

    cfg = Config.load(config)
    url = f"https://api.hearthstonejson.com/v1/latest/{locale}/cards.json"
    out = Path(cfg.cards_path)
    console.print(f"下载 {url} …")
    try:
        # HearthstoneJSON 对默认 Python-urllib UA 返回 403，必须带浏览器 UA。
        # 实测不带 UA 会一直刷新失败，卡表停留在旧版本导致新卡查不到。
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
    except Exception as e:
        console.print(f"[red]下载失败：{e}[/red]")
        raise typer.Exit(1)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    db = CardDB.load(out)
    console.print(f"[green]已保存 {out}（{len(db)} 张卡）[/green]")


@app.command()
def doctor(config: str = typer.Option(None, "--config", "-c")) -> None:
    """环境自检：日志、卡牌数据、LLM 配置。"""
    cfg = Config.load(config)
    ok = True

    p = None
    try:
        p = find_latest_power_log()
    except Exception:
        pass
    if p:
        console.print(f"[green]✓[/] Power.log：{p}（{p.stat().st_size/1024/1024:.1f} MB）")
    else:
        console.print("[red]✗[/] 未找到 Power.log")
        ok = False

    cp = Path(cfg.cards_path)
    if cp.exists():
        try:
            db = CardDB.load(cp)
            console.print(f"[green]✓[/] 卡牌数据：{len(db)} 张（{cp}）")
        except Exception as e:
            console.print(f"[red]✗[/] 卡牌数据加载失败：{e}")
            ok = False
    else:
        console.print(f"[red]✗[/] 卡牌数据缺失：{cp}（运行 refresh-cards）")
        ok = False

    if cfg.llm.api_key:
        console.print(f"[green]✓[/] LLM：{cfg.llm.model} @ {cfg.llm.base_url}（key {cfg.llm.masked_key()}）")
    else:
        console.print("[yellow]![/] 未配置 LLM API Key（将退化为本地启发式）")

    # 日志截断检查
    client_cfg = Path("/Applications/Hearthstone/client.config")
    if client_cfg.exists() and "FileSizeLimit.Int=-1" in client_cfg.read_text():
        console.print("[green]✓[/] 已解除日志 10MB 截断")
    else:
        console.print("[yellow]![/] 建议在 /Applications/Hearthstone/client.config 写入 FileSizeLimit.Int=-1[/]")

    console.print("\n" + ("[green]环境就绪[/]" if ok else "[red]存在问题，请先修复[/]"))


if __name__ == "__main__":
    app()