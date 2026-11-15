"""
炉石助手 · 实时日志监听

持续 tail Power.log，把增量动作喂给 :class:`GameStateReconstructor`，
并在关键时机（换牌阶段 / 轮到我方回合）触发回调。

实现要点：
- 只跟随文件新增内容，不重复解析；自动处理客户端重启导致的文件轮转
- 用「我的回合开始」作为触发点，避免同一回合内反复触发
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .log_parser import GameStateReconstructor, PowerLogParser

#: 我方**真正开始出牌**的步骤。
#:
#: 实测一个回合内的 step 序列（玩家 1 视角）：
#:   MAIN_READY → MAIN_START_TRIGGERS → MAIN_START → MAIN_ACTION → MAIN_END →…
#: 其中 ``MAIN_READY`` 只是「准备就绪」，玩家还不能操作，
#: 因此只有 ``MAIN_ACTION`` 才是真正的「我方回合开始」。
#: （旧版把 MAIN_READY 也算进来，导致每回合触发两次建议。）
ACTIONABLE_STEPS = {"MAIN_ACTION"}

#: 换牌相关步骤。注意 ``MAIN_START`` 只是「回合开始触发器」，
#: 每个回合都会经过它，**不能**当成换牌——否则每回合都弹「起手留牌」。
MULLIGAN_STEPS = {"BEGIN_MULLIGAN", "MULLIGAN"}


@dataclass
class Trigger:
    """一次触发事件。

    ``recon`` 是**同一个可变对象**，之后会继续被新日志更新——
    需要「触发瞬间」的回合号请用 :attr:`turn`，不要事后读``recon.turn``
    （那会拿到终局值，实测因此把 9 次触发误看成「都是第 9 回合」）。
    同理 :attr:`current_player` / :attr:`step` 也是触发瞬间的快照：
    一次 ``poll()`` 会读完当前所有新增日志，等主循环处理这些触发时
    ``recon`` 可能早已进入 ``MAIN_END`` 甚至下一个回合，
    用「当前 recon」判断是否在敌方回合会误杀建议。
    """

    kind: str  # "my_turn" | "mulligan" | "game_start"
    recon: GameStateReconstructor
    turn: int  # 触发瞬间的回合号
    my_pid: int = 0
    current_player: int = 0  # 触发瞬间日志里的行动方
    step: str = ""  # 触发瞬间的 step


class LogWatcher:
    """跟随 Power.log 的增量读取器。"""

    def __init__(
        self,
        log_path: str | Path,
        start_from_end: bool = True,
        player_name: str = "",
        account_id: str = "",
        stale_seconds: float = 90.0,
    ):
        self.path = Path(log_path)
        #: 日志静默多少秒后判定「不在对局中」。套牌/选人/主界面不写 Power.log，
        #: 只有进入战斗才会持续写入，因此「日志停滞」是可靠的离局信号。
        self.stale_seconds = stale_seconds
        #: 最近一次日志内容变化的时间戳（time.monotonic 基准）
        self._last_write = time.monotonic()
        self._last_size = -1
        self.start_from_end = start_from_end
        self.recon = GameStateReconstructor()
        self._parser = PowerLogParser()
        self._player_name = player_name
        self._parser.set_player_name(player_name)
        self.recon.my_account = (account_id or "").strip()
        self.recon.my_player_name = (player_name or "").strip()
        # recon 需要用 parser 的「名#号 → 玩家编号」换算来同步法力资源
        self.recon._slot_resolver = self._parser.player_slot_of
        # 以及「名#号 → 玩家显示名」，用于界面显示敌我姓名
        self.recon._pname_resolver = self._parser.player_name_of
        self._offset = 0
        self._inode: int | None = None
        self._game_no = 0
        #: 上次触发建议时的回合号（同一回合不重复触发）
        self._last_turn_fired = -1
        self._mulligan_fired = False
        #: 上次给出「回合建议」的全局回合号。炉石的 TURN 每个玩家回合都 +1，
        #: 两个我方回合之间必然相隔 2；若只差 1，说明把对手回合误判成
        #: 我方了（实测连续 13/14 回合各弹一次建议）。这是最后一道硬约束，
        #: 即使名字/编号判据都失效也绝不会「每个回合都给」。
        self._last_advice_turn = -1
        #: 本机 PlayerID，用于校验「轮到我方」。0 = 不校验。
        self._my_pid_hint = 0
        #: 本次启动以来已提示过的回合号。
        #: 用「回合号」而非「(对局, 回合)」——因为解析器重建对局会让对局号不断变化，
        #: 只有回合号在同一局内是稳定且单调的。
        self._advised_turns: set[int] = set()
        #: 上次给出「回合建议」的时刻，用于同回合内去重
        self._turn_advice_at = 0.0
        #: 身份不一致告警只提示一次
        self._identity_warned = False
        #: 上一次见到的回合号，用于识别「新对局」（回合号回退）
        self._prev_turn = 0
        #: 同回合建议的最小间隔（秒）
        self.turn_cooldown = 20.0
        #: 是否周期性重新定位「最新」的 Power.log。
        #: 炉石每次启动客户端会新建 Logs/Hearthstone_<时间戳>/ 目录，且
        #: Power.log 只在进入对局后才出现；主界面期间启动的军师若不主动
        #: 切换目录，开局后界面会一直停在旧日志上（实测用户遇到的正是这个）。
        #: 默认关闭，避免离线回放测试被切换干扰；gui / watch 会开启。
        self.follow_latest = False
        self.latest_check_interval = 3.0
        self._latest_check_at = 0.0
        #: 首轮是否检查过「启动时已处于我方回合」的补触发
        self._initial_trigger_done = False

    def calibrate_to_file_mtime(self) -> None:
        """按文件 mtime 校准停滞计时（仅在启动/回填后调用一次）。

        文件 mtime 就是炉石最后写入日志的真实时刻。若该时刻已过去很久，
        说明对局早已结束——不能再把它当成「刚刚还在更新」。
        """
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return
        idle = time.time() - mtime
        if idle <= 0:
            self._last_write = time.monotonic()
        else:
            # 折算成 monotonic 基准：让 is_stale 立即反映真实空闲时长
            self._last_write = time.monotonic() - idle

    @property
    def is_stale(self) -> bool:
        """日志是否已长时间无写入（=> 炉石不在对局中）。

        炉石只在**战斗进行中**持续写 Power.log；套牌选人、主界面、结算动画
        期间日志完全静止。若不判断这一点，浮窗会一直挂着上一局的终局画面，
        让人以为工具没在更新。
        """
        if self.stale_seconds <= 0:
            return False
        return (time.monotonic() - self._last_write) > self.stale_seconds

    def seconds_since_write(self) -> float:
        """距最近一次日志写入的秒数。"""
        return time.monotonic() - self._last_write

    def _touch(self) -> None:
        """记录日志确有变化。"""
        self._last_write = time.monotonic()

    def set_my_pid(self, pid: int) -> None:
        """告知本机PlayerID，用于校验「这一步是否轮到我方」。

        不设置（0）时只按 step 判定——会在对方回合也触发建议，
        因此应当在解析出身份后立即调用。
        """
        self._my_pid_hint = int(pid or 0)

    def backfill_current_game(self) -> None:
        """回填当前对局的历史数据，再开始增量跟读。

        **这是必须的**：炉石一个会话里会把多个对局写进同一个 Power.log。
        若直接从文件末尾跟读，``CREATE_GAME`` 与所有 ``FULL_ENTITY`` 声明
        都已在历史日志里，实体表会一直是空的 —— 表现为血量 0、场面 0、手牌 0，
        大模型据此回复「双方生命均为0，对局已结束」。

        实现：直接复用 :func:`parse_log_file` 全量回放。之所以不自己「反向找
        CREATE_GAME 再截段」，是因为实测炉石会在对局途中重发 CREATE_GAME，
        而英雄的 FULL_ENTITY 声明在更早的位置——分段回放很容易漏掉初始血量
        （试过按 CREATE_GAME 回溯、按 CARDTYPE/HEALTH 判据回溯，都不可靠）。
        全量回放与离线分析走同一套代码，不存在两套逻辑不一致的问题。

        8MB 日志全量回放约 0.3 秒，只在启动时执行一次，可接受。
        """
        from .log_parser import parse_log_file

        if not self.path.exists():
            return
        # 回填与后续增量跟读必须共用同一个 parser：虚拟实体 id、game_id、
        # FIRST_PLAYER 配对都由它分配，换实例会让 recon 的槽位映射与增量
        # 解析对不上（法力 / 玩家名 / 身份判定错乱）。
        self._parser = PowerLogParser()
        self._parser.set_player_name(self._player_name)
        try:
            self.recon = parse_log_file(
                self.path,
                player_name=self._player_name,
                account_id=self.recon.my_account,
                parser=self._parser,
            )
        except Exception:
            # 回放失败时退回「空状态 + 从头跟读」，保证至少能读到后续增量
            self.recon = GameStateReconstructor()
            self.recon._slot_resolver = self._parser.player_slot_of
            self.recon._pname_resolver = self._parser.player_name_of
            self.start_from_end = False
            self._open()
            self.poll(timeout=0)
            return

        self._backfilled = True
        self._last_size = self.path.stat().st_size if self.path.exists() else -1
        self.calibrate_to_file_mtime()
        self.recon.idle_seconds = self.seconds_since_write()
        # ★ 按文件真实 mtime 校准：回填发生在启动时，若一律用 monotonic 计时，
        # 会把一份「已经停更一小时」的旧日志判成新鲜的，浮窗继续显示陈旧终局。
        # 因此这里以文件 mtime 为准——炉石停止写入的那一刻就是真实的「离局时刻」。
        self._last_write = time.monotonic()

    def _open(self) -> None:
        """打开文件并定位初始偏移。"""
        self._inode = self.path.stat().st_ino
        size = self.path.stat().st_size
        if self.start_from_end:
            self._offset = size
        self._fh = self.path.open(encoding="utf-8", errors="ignore")
        self._fh.seek(self._offset)

    def _check_rotation(self) -> bool:
        """炉石重启会新建日志目录；检测到文件被替换则重新打开。"""
        try:
            cur_inode = self.path.stat().st_ino
        except FileNotFoundError:
            return False
        if self._inode is not None and cur_inode != self._inode:
            self._fh.close()
            # 炉石把上一场日志挪进子目录，新日志仍在原路径
            self._open()
            return True
        return False

    def _maybe_follow_latest(self) -> bool:
        """周期性重新定位最新的 Power.log，必要时整体切换。

        旧实现只在启动时调一次 ``find_latest_power_log()``，之后仅检查
        **同一路径**的 inode 轮转。但炉石每次启动客户端都会新建
        ``Logs/Hearthstone_<时间戳>/`` 目录，且 Power.log 要等进入对局才写。
        于是「主界面时启动军师 → 开局」的场景下，界面永远停在旧日志，
        表现就是「对局已开始，但浮窗上什么都没有」。
        """
        now = time.monotonic()
        if now - self._latest_check_at < self.latest_check_interval:
            return False
        self._latest_check_at = now
        try:
            from .config import find_latest_power_log

            latest = find_latest_power_log()
        except Exception:
            return False
        if not latest or latest == self.path:
            return False
        try:
            # 只往更新的日志切，避免扫描到历史目录后来回跳
            if latest.stat().st_mtime <= self.path.stat().st_mtime:
                return False
        except OSError:
            return False
        return self.switch_to(latest)

    def switch_to(self, new_path: str | Path) -> bool:
        """切到另一份 Power.log：重建局面、重置触发去重、继续跟读。"""
        new_path = Path(new_path)
        if not new_path.exists():
            return False
        if hasattr(self, "_fh"):
            try:
                self._fh.close()
            except OSError:
                pass
            del self._fh
        self.path = new_path
        self._offset = 0
        self._inode = None
        self.start_from_end = True
        self._game_no = 0
        self._mulligan_fired = False
        self._last_turn_fired = -1
        self._last_advice_turn = -1
        self._advised_turns.clear()
        self._prev_turn = 0
        self._turn_advice_at = 0.0
        self._identity_warned = False
        self._initial_trigger_done = False
        # 全量回填新日志（内部换代 parser / recon 并按文件 mtime 校准计时）
        self.backfill_current_game()
        self._open()
        self._touch()
        return True

    def poll(self, timeout: float = 1.0) -> list[Trigger]:
        """读取新增内容，返回本轮触发的所有事件。"""
        if self.follow_latest:
            self._maybe_follow_latest()
        if not hasattr(self, "_fh"):
            self._open()

        triggers: list[Trigger] = []
        if self._check_rotation():
            triggers.append(Trigger("game_start", self.recon, 1))

        # 增量读取
        got_any = False
        while True:
            line = self._fh.readline()
            if not line:
                break
            got_any = True
            action = self._parser.feed_line(line)
            if action:
                triggers.extend(self._apply(action))
        if got_any:
            # 确实读到了新内容 → 刷新停滞计时
            self._touch()
        else:
            # 没读到内容时以文件 mtime 为准，避免进程刚启动时的计时偏差
            self.calibrate_to_file_mtime()
        # 供界面判断「炉石是否已离开战斗」
        self.recon.idle_seconds = self.seconds_since_write()

        #身份一致性自检：配置的名字与解析出的绑定不一致时给出提示。
        # 这种情况通常意味着「跑的是旧代码」或「配置过期」。
        if not self._identity_warned:
            resolver = getattr(self.recon, "_slot_resolver", None)
            if resolver is not None and self._player_name:
                try:
                    resolver.resolve_first_player_pairs()
                    parser = getattr(resolver, "__self__", None)
                    if parser is not None:
                        slot = parser.slot_of_name(self._player_name)
                        if slot and self._my_pid_hint and slot != self._my_pid_hint:
                            self._identity_warned = True
                            print(
                                f"[warn] 身份修正：配置 player_name={self._player_name!r} "
                                f"实际对应 PlayerID {slot}（原按 {self._my_pid_hint} 显示）"
                            )
                except Exception:
                    pass

        last = self._parser.flush()
        if last:
            triggers.extend(self._apply(last))

        # 军师在我方回合中途才启动时，增量跟读看不到「从别的 step 切入
        # MAIN_ACTION」的边沿事件，会一直等不到建议。首轮主动补触发一次，
        # 保证「每次我的回合开始时给建议」不因启动时机而漏掉。
        if not self._initial_trigger_done:
            self._initial_trigger_done = True
            pid = self._my_pid_hint
            if (
                pid
                and self.recon.is_my_action_phase(pid)
                and not any(t.kind == "my_turn" for t in triggers)
            ):
                turn = self.recon.turn
                if turn not in self._advised_turns:
                    self._advised_turns.add(turn)
                    self._last_advice_turn = turn
                    triggers.append(Trigger(
                        "my_turn", self.recon, turn, pid,
                        self.recon.current_player, self.recon.step,
                    ))

        if timeout and not triggers:
            time.sleep(min(0.3, timeout))
        return triggers

    def _apply(self, action) -> list[Trigger]:
        """应用一个动作，判断是否产生触发事件。"""
        out: list[Trigger] = []
        prev_step = self.recon.step

        if action.action in ("CREATE_GAME", "RESET_GAME"):
            self._game_no += 1
            self._last_turn_fired = -1
            self._turn_advice_at = 0.0
            self._prev_turn = 0
            self._mulligan_fired = False
            # ★ 不清空去重表。
            # 实测**一场对局会发两次 CREATE_GAME**（本机一份日志 3 场对局
            # 对应 6 次 CREATE_GAME），清空后同一回合号会被重复提示。
            # 真正的「新对局」由回合号回退识别，见下方 starts_new_turn 判断。
            self._prev_turn = 0
            self.recon.apply(action)
            out.append(Trigger("game_start", self.recon, 1))
            return out

        # 先记住本条动作之前的状态——STEP 挂��� GameEntity(1)，
        # CURRENT_PLAYER 挂在玩家名实体上，二者**永不在同一动作块**，
        # 因此必须用「本动作的 STEP」与「全局 current_player」组合判断。
        act_step = ""
        for tag, value in action.tags:
            if tag == "STEP":
                act_step = value
                break

        self.recon.apply(action)
        step = act_step or self.recon.step

        # 换牌阶段：整局只触发一次
        if step in MULLIGAN_STEPS and not self._mulligan_fired:
            self._mulligan_fired = True
            out.append(Trigger("mulligan", self.recon, 1))

        # 我方回合开始：三个条件缺一不可——
        #   1) step == MAIN_ACTION（玩家真正能操作，MAIN_READY 只是「准备就绪」）
        #   2) 回合号比上次触发时新（同一回合内 MAIN_ACTION 会反复进出）
        #   3) CURRENT_PLAYER 是我方（后手时对方的回合也会经过 MAIN_ACTION）
        if step in ACTIONABLE_STEPS:
            turn = self.recon.turn
            cp_now = self.recon.current_player
            pid = self._my_pid_hint
            # 名字判据以「玩家级 TURN 那一侧的名字」为最优先（每回合必写），
            # 它明确写了「现在轮到谁」，比 PlayerID 槽位可靠得多。
            owner = (
                self.recon.turn_owner_name
                or self.recon.current_player_name
                or ""
            ).strip()
            mine = {
                n.strip().rsplit("#", 1)[0]
                for n in (self.recon.player_names.get(pid, ""), self.recon.my_player_name)
                if n and not n.startswith("UNKNOWN")
            }
            name_ok = (owner in mine) if owner else (pid <= 0 or cp_now == pid)
            # owner 在场时它有最终裁决权：编号可能残留旧局配对，
            # 但日志里 value=1 那一侧的名字是权威的「谁在行动」。
            is_mine = name_ok

            #★ 真新对局 = 回合号回退（新一轮从 1 开始）。
            # 不能用 CREATE_GAME 判定：一场对局会发两次。
            if turn < self._prev_turn:
                self._advised_turns.clear()
                self._last_advice_turn = -1
            self._prev_turn = max(self._prev_turn, turn)

            # 硬约束：炉石 TURN 每个玩家回合 +1，两个我方回合之间必差 2。
            # 差 1 只可能是把对手回合误判成我方（或极罕见的额外回合）。
            gap_ok = (
                self._last_advice_turn < 0
                or turn <= self._last_advice_turn
                or turn - self._last_advice_turn >= 2
            )
            starts_new_turn = prev_step not in ACTIONABLE_STEPS
            if is_mine and starts_new_turn and gap_ok and turn not in self._advised_turns:
                self._advised_turns.add(turn)
                self._last_turn_fired = turn
                self._last_advice_turn = turn
                self._turn_advice_at = time.monotonic()
                out.append(Trigger(
                    "my_turn", self.recon, turn, pid, cp_now, step,
                ))
        return out

    def close(self) -> None:
        if hasattr(self, "_fh"):
            self._fh.close()


def replay_once(
    log_path: str | Path, my_pid: int = 1
) -> tuple[GameStateReconstructor, list[Trigger]]:
    """离线全量回放，返回终局状态与过程中的触发点（用于测试与调试）。"""
    from .log_parser import parse_log_file

    recon = parse_log_file(log_path)
    return recon, []


def snapshot_at_turn(
    log_path: str | Path, target_turn: int = 1, my_pid: int = 1
) -> GameStateReconstructor:
    """回放到指定回合的某个时刻（用于离线验证与回放教学）。"""
    from .log_parser import parse_log_file

    recon = parse_log_file(log_path)
    return recon