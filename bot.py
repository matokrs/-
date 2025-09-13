# -*- coding: utf-8 -*-
import os
import asyncio
import datetime
import itertools
import random
import aiohttp
from discord.ext import commands
from dotenv import load_dotenv
from dataclasses import dataclass, field
from typing import Dict, Set, List, Optional, Tuple

import discord
from discord.ext import commands, tasks
from discord.ui import View, Button, Select

# ==================== 설정 ====================
DISCORD_TOKEN = "MTQxNTY3MDkwODIxOTI5Mzc2Nw.GRlAqW.LRci66Dq8noIwoMPKMBHnfFIidaVBgj8alku1k"  
GUILD_ID = int(os.getenv("GUILD_ID", "0"))        # 슬래시 커맨드 빠른 동기화용(선택)

TARGET_CHANNEL = "마린크래프트"

# 라인 및 우선순위
LANE_NAMES = ["탑", "정글", "미드", "원딜", "서폿"]
lane_priority = ["미드", "정글", "원딜", "탑", "서폿"]

# 라인 별칭(영문/축약)
LANE_ALIASES = {
    "top": "탑", "t": "탑", "탑": "탑",
    "jungle": "정글", "jg": "정글", "정글": "정글",
    "mid": "미드", "m": "미드", "미드": "미드",
    "adc": "원딜", "bot": "원딜", "원딜": "원딜",
    "support": "서폿", "sup": "서폿", "서폿": "서폿", "서포트": "서폿", "서포터": "서폿",
}

# 전투력 파라미터(쉽게 튜닝 가능)
K_LINE_WEIGHT = 0.3     # 라인 영향 가중치 강도
TEAM_TOLERANCE = 4.0    # 팀 전투력 허용 차이

# Windows 이벤트 루프 설정(호환성)
if os.name == "nt":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ==================== 데이터 구조 ====================
@dataclass
class Player:
    uid: int
    name: str
    lane_tiers: Dict[str, int] = field(default_factory=dict)  # 예: {"탑": 2, "미드": 1}

players: Dict[int, Player] = {}
lines: Dict[str, Set[int]] = {ln: set() for ln in LANE_NAMES}
participants: List[int] = []
participants_start: Optional[datetime.datetime] = None
match_open: bool = False

# ==================== 디스코드 기본 설정 ====================
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ==================== 유틸 / 헬퍼 ====================
def in_target_channel_name(ch: discord.abc.GuildChannel) -> bool:
    """지정된 텍스트채널/스레드에서만 사용하도록 체크"""
    if isinstance(ch, discord.TextChannel):
        return ch.name == TARGET_CHANNEL
    if isinstance(ch, discord.Thread) and isinstance(ch.parent, discord.TextChannel):
        return ch.parent.name == TARGET_CHANNEL
    return False

async def ensure_channel_or_hint(ctx_or_inter, ephemeral_ok: bool = False) -> bool:
    """채널이 맞는지 검사하고, 아니면 귀여운 안내 메시지 출력"""
    ch = ctx_or_inter.channel
    ok = in_target_channel_name(ch)
    if not ok:
        msg = f"여기는 `{TARGET_CHANNEL}` 채널이 아니에요… (•́︿•̀) `{TARGET_CHANNEL}`에서 다시 시도해 주세요!"
        if isinstance(ctx_or_inter, commands.Context):
            await ctx_or_inter.send(msg, delete_after=4)
        else:
            await ctx_or_inter.response.send_message(msg, ephemeral=True if ephemeral_ok else False)
    return ok

def normalize_lane(token: str) -> Optional[str]:
    t = token.replace(" ", "").replace(",", "").replace("，", "").lower()
    return LANE_ALIASES.get(t) or (t if t in LANE_NAMES else None)

def get_or_create_player(user: discord.abc.User, display_name: str) -> Player:
    p = players.get(user.id)
    if p is None:
        p = Player(uid=user.id, name=display_name)
        players[user.id] = p
    elif p.name != display_name:
        p.name = display_name
    return p

def cute(title: str) -> str:
    return f"{title} (๑˃̵ᴗ˂̵)و✧"

# 라인 가중치: 서폿 > 정글 > 미드 > 원딜 > 탑
def line_weight(lane: str, k: float = K_LINE_WEIGHT) -> float:
    if lane == "서폿": return 1 + 0.5 * k
    if lane == "정글": return 1 + 0.25 * k
    if lane == "미드": return 1
    if lane == "원딜": return 1 - 0.25 * k
    if lane == "탑":   return 1 - 0.5 * k
    return 1

def player_ppi(tier_int: int, lane: str, k: float = K_LINE_WEIGHT) -> float:
    # 1티어=3, 2티어=2, 3티어=1 → (5 - tier)
    return (5 - tier_int) * line_weight(lane, k)

# ==================== 라인 배정 / 팀 만들기 ====================
def assign_lines(team: List[Dict], k: float = K_LINE_WEIGHT) -> List[Tuple[List[Tuple[str, str, int]], float]]:
    """5명 팀에서 라인 배정 가능한 모든 케이스와 전투력 합을 반환"""
    lanes_required = set(LANE_NAMES)
    results: List[Tuple[List[Tuple[str, str, int]], float]] = []

    def lane_prio_key(l: str) -> int:
        try:
            return lane_priority.index(l)
        except ValueError:
            return len(lane_priority)

    team_sorted = sorted(team, key=lambda p: (len(p["lane_tiers"]), p["name"]))
    pref = [sorted([l for l in p["lane_tiers"] if l in LANE_NAMES], key=lane_prio_key) for p in team_sorted]

    chosen_lanes: List[str] = []

    def backtrack(i: int, used: Set[str]):
        if i == 5:
            if used == lanes_required:
                power = 0.0
                picks: List[Tuple[str, str, int]] = []
                for j in range(5):
                    lane = chosen_lanes[j]
                    tier_here = int(team_sorted[j]["lane_tiers"][lane])
                    power += player_ppi(tier_here, lane, k)
                    picks.append((team_sorted[j]["name"], lane, tier_here))
                results.append((picks, power))
            return
        remain_slots = 5 - i
        remain_lanes = len(lanes_required - used)
        if remain_lanes > remain_slots:
            return
        for lane in pref[i]:
            if lane not in used:
                used.add(lane); chosen_lanes.append(lane)
                backtrack(i+1, used)
                chosen_lanes.pop(); used.remove(lane)

    backtrack(0, set())
    return results

def make_teams(players10: List[Dict], k: float = K_LINE_WEIGHT, tolerance: float = TEAM_TOLERANCE):
    candidates = []
    best_split = None
    min_diff = float("inf")

    for combo in itertools.combinations(players10, 5):
        teamA = list(combo)
        teamB = [p for p in players10 if p not in combo]
        assignA = assign_lines(teamA, k)
        assignB = assign_lines(teamB, k)
        if not assignA or not assignB:
            continue
        for a_players, pA in assignA:
            for b_players, pB in assignB:
                diff = abs(pA - pB)
                if diff <= tolerance:
                    candidates.append((a_players, b_players, pA, pB))
                if diff < min_diff:
                    min_diff = diff
                    best_split = (a_players, b_players, pA, pB)

    return random.choice(candidates) if candidates else best_split

# ==================== UI (임베드/뷰) ====================
def make_lane_embed(p: Player, current_lane: Optional[str]) -> discord.Embed:
    def sort_key(kv):
        ln, _ = kv
        try:
            return lane_priority.index(ln)
        except ValueError:
            return 99
    pairs = sorted(p.lane_tiers.items(), key=sort_key)
    lines_str = "\n".join(f"- {ln} {tv}" for ln, tv in pairs) if pairs else "아직 등록된 라인이 없어요… (*´﹃｀*)"
    emb = discord.Embed(title=cute(f"{p.name} 님의 라인·티어 선택"), color=0xF8B4D9)
    emb.add_field(name="현재 등록", value=lines_str, inline=False)
    emb.add_field(name="현재 선택 라인", value=current_lane or "아직 선택 안 됐어요!", inline=False)
    emb.set_footer(text="순서: 라인 드롭다운 → 티어 버튼 / 여러 라인도 하나씩 천천히 등록해봐요 ✨")
    return emb

class LaneSelect(Select):
    def __init__(self, owner_id: int, player: Player):
        self.owner_id = owner_id
        self.player = player
        options = [discord.SelectOption(label=ln, value=ln) for ln in LANE_NAMES]
        super().__init__(placeholder="라인 고르기 ✨", options=options, min_values=1, max_values=len(LANE_NAMES))

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("이 버튼은 다른 친구 거예요… 미안해요 (๑•́ ₃ •̀๑)", ephemeral=True)
            return
        for lane in self.values:
            if lane not in self.player.lane_tiers:
                self.player.lane_tiers[lane] = 3  # 기본 티어는 3으로
                lines[lane].add(self.player.uid)
        if isinstance(self.view, LaneTierView):
            self.view.current_lane = self.values[-1]
        await interaction.response.edit_message(
            embed=make_lane_embed(self.player, self.view.current_lane), view=self.view
        )

class TierButton(Button):
    def __init__(self, owner_id: int, player: Player, tier: int):
        super().__init__(label=f"티어 {tier}", style=discord.ButtonStyle.primary)
        self.owner_id = owner_id
        self.player = player
        self.tier = tier

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("앗, 이건 주인 전용 버튼이에요! (⁎˃ᆺ˂)", ephemeral=True)
            return
        current_lane = self.view.current_lane if isinstance(self.view, LaneTierView) else None
        if not current_lane:
            await interaction.response.send_message("먼저 라인을 골라주세요! ꒰ᐢ⸝⸝• ̫ •⸝⸝ᐢ꒱", ephemeral=True)
            return
        self.player.lane_tiers[current_lane] = self.tier
        lines[current_lane].add(self.player.uid)
        await interaction.response.edit_message(
            embed=make_lane_embed(self.player, current_lane), view=self.view
        )

class ResetButton(Button):
    def __init__(self, owner_id: int, player: Player):
        super().__init__(label="내 라인 모두 지우기", style=discord.ButtonStyle.danger)
        self.owner_id = owner_id
        self.player = player

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("이건 주인 전용이에요…! (*•̀ᴗ•́*)و ̑̑", ephemeral=True)
            return
        for ln in list(self.player.lane_tiers.keys()):
            lines[ln].discard(self.player.uid)
        self.player.lane_tiers.clear()
        if isinstance(self.view, LaneTierView):
            self.view.current_lane = None
        await interaction.response.edit_message(
            embed=make_lane_embed(self.player, None), view=self.view
        )

class DoneButton(Button):
    def __init__(self, owner_id: int, player: Player):
        super().__init__(label="저장 완료하기", style=discord.ButtonStyle.success)
        self.owner_id = owner_id
        self.player = player

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("헷, 이 버튼은 해당 유저만 쓸 수 있어요! (๑˃̵ᴗ˂̵)و", ephemeral=True)
            return
        if not self.player.lane_tiers:
            await interaction.response.send_message("등록된 라인이 없어요! 하나만이라도 추가해볼까요? (ง •̀_•́)ง", ephemeral=True)
            return
        if self.view:
            for c in self.view.children:
                c.disabled = True
        await interaction.response.edit_message(
            embed=make_lane_embed(self.player, getattr(self.view, "current_lane", None)), view=self.view
        )

class LaneTierView(View):
    def __init__(self, owner_id: int, player: Player, timeout: int = 180):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.player = player
        self.message: Optional[discord.Message] = None
        self.current_lane: Optional[str] = None
        self.add_item(LaneSelect(owner_id, player))
        for t in [1, 2, 3]:
            self.add_item(TierButton(owner_id, player, t))
        self.add_item(ResetButton(owner_id, player))
        self.add_item(DoneButton(owner_id, player))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

# ==================== 이벤트/태스크 ====================
@bot.event
async def on_ready():
    print(f"로그인 성공: {bot.user}")
    # 슬래시 커맨드 동기화
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
        else:
            await bot.tree.sync()
    except Exception as e:
        print("Slash sync error:", repr(e))
    if not reset_checker.is_running():
        reset_checker.start()

@tasks.loop(minutes=1)
async def reset_checker():
    global participants_start, match_open
    if participants_start:
        try:
            if (datetime.datetime.now() - participants_start).total_seconds() > 43200:  # 12시간
                participants.clear()
                participants_start = None
                match_open = False
                ch = discord.utils.get(bot.get_all_channels(), name=TARGET_CHANNEL)
                if isinstance(ch, discord.TextChannel):
                    await ch.send("12시간이 지나서 참가 명단을 새로 고쳤어요! 다시 모여봐요~ (˶˙ᵕ˙˶)ﾉﾞ")
        except Exception as e:
            print("reset_checker error:", repr(e))

@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.CommandNotFound):
        return
    try:
        await ctx.send(f"앗… 문제가 생겼어요: `{error.__class__.__name__}` — {error} (ㅠ﹏ㅠ) 알려주시면 뚝딱 고쳐볼게요!")
    except Exception:
        print("Command error:", repr(error))

# ==================== UI 호출: /라인(에페메랄) & !라인(DM) ====================
def _make_view_for_user(user: discord.abc.User, display_name: str) -> LaneTierView:
    p = get_or_create_player(user, display_name)
    return LaneTierView(user.id, p, timeout=180)

@bot.tree.command(name="라인", description="라인·티어를 귀엽게 고를 수 있어요! (본인만 보임)")
async def slash_라인(interaction: discord.Interaction):
    if not await ensure_channel_or_hint(interaction, ephemeral_ok=True):
        return
    member_name = interaction.user.display_name if hasattr(interaction.user, "display_name") else interaction.user.name
    view = _make_view_for_user(interaction.user, member_name)
    embed = make_lane_embed(players[interaction.user.id], None)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    try:
        view.message = await interaction.original_response()
    except Exception:
        pass

@bot.command()
@commands.guild_only()
async def 라인(ctx: commands.Context):
    """DM으로 UI를 보내드려요! 채널은 깔끔하게~"""
    if not await ensure_channel_or_hint(ctx):
        return
    p = get_or_create_player(ctx.author, ctx.author.display_name)
    view = LaneTierView(ctx.author.id, p, timeout=180)
    embed = make_lane_embed(p, None)
    try:
        dm = await ctx.author.create_dm()
        msg = await dm.send(embed=embed, view=view)
        view.message = msg
        await ctx.send("깜짝! DM으로 라인 선택 창을 보냈어요. 확인해 주세요~ (˶˃ ᵕ ˂˶)✧", delete_after=4)
    except discord.Forbidden:
        await ctx.send("DM이 막혀 있어요… 대신 `/라인`을 사용해 주세요! (*ﾉωﾉ)", delete_after=5)

# ==================== 현황 / 관리 명령 ====================
@bot.command()
@commands.guild_only()
async def 라인삭제(ctx: commands.Context, *, lane: str = None):
    if not await ensure_channel_or_hint(ctx):
        return
    if not lane:
        await ctx.send("사용법: `!라인삭제 탑` 처럼 적어주세요! (งᐛ )ง", delete_after=6)
        return
    p = get_or_create_player(ctx.author, ctx.author.display_name)
    ln = normalize_lane(lane)
    if not ln or ln not in LANE_NAMES:
        await ctx.send(f"음… `{lane}` 는 잘 모르겠어요. 가능한 라인: {', '.join(LANE_NAMES)}", delete_after=6)
        return
    if ln not in p.lane_tiers:
        await ctx.send(f"{p.name} 님은 아직 `{ln}` 을 등록하지 않았어요~", delete_after=6)
        return
    del p.lane_tiers[ln]
    lines[ln].discard(p.uid)
    await ctx.send(f"`{ln}` 라인을 깔끔하게 지웠어요! (๑•̀ㅂ•́)و✧")

@bot.command()
@commands.guild_only()
async def 라인초기화(ctx: commands.Context):
    if not await ensure_channel_or_hint(ctx):
        return
    p = get_or_create_player(ctx.author, ctx.author.display_name)
    if not p.lane_tiers:
        await ctx.send(f"{p.name} 님은 아직 등록된 라인이 없어요~ (•ᵕ•)و̑̑", delete_after=5)
        return
    for ln in list(p.lane_tiers.keys()):
        lines[ln].discard(p.uid)
    p.lane_tiers.clear()
    await ctx.send("촤라락~ 모든 라인을 초기화했어요! 다시 예쁘게 등록해봐요 ✨")

@bot.command()
@commands.guild_only()
async def 라인현황(ctx: commands.Context):
    if not await ensure_channel_or_hint(ctx):
        return
    def fmt(ln: str) -> str:
        names = []
        for uid in lines[ln]:
            pl = players.get(uid)
            if pl and ln in pl.lane_tiers:
                names.append(f"{pl.name}({pl.lane_tiers[ln]})")
        return f"{ln}: {', '.join(sorted(names)) if names else '(없음)'}"
    await ctx.send("현재 라인 현황이에요~ (괄호는 해당 라인의 티어)\n" + "\n".join(fmt(ln) for ln in LANE_NAMES))

@bot.command()
@commands.guild_only()
async def 티어(ctx: commands.Context, t: str = None):
    if not await ensure_channel_or_hint(ctx):
        return
    if t not in {"1", "2", "3"}:
        await ctx.send("사용법: `!티어 1|2|3` (등록된 모든 라인의 티어를 한꺼번에 바꿔요!)", delete_after=6)
        return
    p = get_or_create_player(ctx.author, ctx.author.display_name)
    if not p.lane_tiers:
        await ctx.send("먼저 `/라인` 또는 `!라인`으로 라인을 등록해 주세요~ (๑˘︶˘๑)", delete_after=6)
        return
    tv = int(t)
    for ln in list(p.lane_tiers.keys()):
        p.lane_tiers[ln] = tv
        lines[ln].add(p.uid)
    await ctx.send(f"완료! 모든 등록 라인의 티어를 `{tv}` 로 바꿨어요 ✨")

@bot.command()
@commands.guild_only()
async def 티어현황(ctx: commands.Context):
    if not await ensure_channel_or_hint(ctx):
        return
    buckets: Dict[int, List[str]] = {1: [], 2: [], 3: []}
    for p in players.values():
        if p.lane_tiers:
            buckets[min(p.lane_tiers.values())].append(p.name)
    msg = "대표티어 현황이에요(등록 라인 중 최고 기준)\n" + "\n".join(
        f"티어 {k}: {', '.join(sorted(v)) if v else '(없음)'}" for k, v in buckets.items()
    )
    await ctx.send(msg)

# ==================== 내전 흐름 ====================
@bot.command()
@commands.guild_only()
async def 내전시작(ctx: commands.Context):
    global match_open, participants_start
    if not await ensure_channel_or_hint(ctx):
        return
    match_open = True
    participants.clear()
    participants_start = None
    await ctx.send("내전을 시작해볼까요? `!참가` / `!퇴장` 을 사용해 주세요! (*˙ᵕ˙*)ﾉﾞ")

@bot.command()
@commands.guild_only()
async def 참가(ctx: commands.Context):
    global participants_start
    if not await ensure_channel_or_hint(ctx):
        return
    if not match_open:
        await ctx.send("아직 내전이 열리지 않았어요! `!내전시작`으로 문을 두드려볼까요? ꒰⸝⸝• ̫ •⸝⸝꒱", delete_after=6)
        return
    p = get_or_create_player(ctx.author, ctx.author.display_name)
    if not p.lane_tiers:
        await ctx.send("먼저 `/라인`이나 `!라인`으로 라인·티어를 등록해 주세요~ (ง •̀_•́)ง", delete_after=6)
        return
    if p.uid in participants:
        await ctx.send(f"{p.name} 님은 이미 참가 중이에요! (๑•ㅂ•)و✧", delete_after=5)
        return
    if len(participants) >= 10:
        await ctx.send("지금은 10명이 꽉 찼어요! 조금만 기다려 주세요~ (•̥﹏•̥)", delete_after=6)
        return
    participants.append(p.uid)
    if not participants_start:
        participants_start = datetime.datetime.now()
    await ctx.send(f"{p.name} 님 참가 완료! 현재 인원 {len(participants)}/10 ✨")
    if len(participants) == 10:
        await ctx.send("와아! 참가 10명 확정이에요~ 준비되면 `!팀짜기` 해볼까요? (ﾉ◕ヮ◕)ﾉ*:･ﾟ✧")

@bot.command()
@commands.guild_only()
async def 퇴장(ctx: commands.Context):
    if not await ensure_channel_or_hint(ctx):
        return
    if not match_open:
        await ctx.send("아직 내전이 열리지 않았어요! (๑•́ ₃ •̀๑) ", delete_after=5)
        return
    p = get_or_create_player(ctx.author, ctx.author.display_name)
    if p.uid not in participants:
        await ctx.send(f"{p.name} 님은 참가 명단에 없어요~ (｡•́︿•̀｡)", delete_after=5)
        return
    participants.remove(p.uid)
    await ctx.send(f"{p.name} 님 퇴장! 현재 인원 {len(participants)}/10")

@bot.command()
@commands.guild_only()
async def 참가현황(ctx: commands.Context):
    if not await ensure_channel_or_hint(ctx):
        return
    if not participants:
        await ctx.send("아직 참가 인원이 없어요… 함께 놀 사람? (๑•́ ₃ •̀๑)ﾉ", delete_after=6)
        return
    msg = "👥 참가 인원이에요!\n"
    for uid in participants:
        p = players.get(uid)
        if p:
            ln_sorted = sorted(
                p.lane_tiers.items(),
                key=lambda kv: lane_priority.index(kv[0]) if kv[0] in lane_priority else 99
            )
            msg += f"{p.name} → {', '.join(f'{ln} {tv}' for ln, tv in ln_sorted)}\n"
    await ctx.send(msg)

@bot.command()
@commands.guild_only()
async def 종료(ctx: commands.Context):
    global match_open, participants_start
    if not await ensure_channel_or_hint(ctx):
        return
    participants.clear()
    participants_start = None
    match_open = False
    await ctx.send("모집을 정리했어요! 수고 많았어요~ 다음에 또 만나요 (´｡• ᵕ •｡`) ♡")

@bot.command()
@commands.guild_only()
async def 팀짜기(ctx: commands.Context):
    if not await ensure_channel_or_hint(ctx):
        return
    if len(participants) != 10:
        await ctx.send("아직 10명이 아니에요! 조금만 더 모아볼까요? (ง˙∇˙)ว", delete_after=6)
        return
    players10 = []
    for uid in participants:
        p = players.get(uid)
        if not p or not p.lane_tiers:
            await ctx.send("라인·티어가 비어있는 친구가 있어요! 먼저 등록 부탁해요~ ꒰•̥̥̥̥̥˘̩̩̩̩̩•̥̥̥̥̥꒱", delete_after=6)
            return
        lanes_clean = {ln: int(tv) for ln, tv in p.lane_tiers.items() if ln in LANE_NAMES and tv in (1, 2, 3)}
        if not lanes_clean:
            await ctx.send(f"{p.name} 님의 라인 정보가 이상해요! 다시 등록 부탁해요~", delete_after=6)
            return
        players10.append({"name": p.name, "lane_tiers": lanes_clean})

    result = make_teams(players10, k=K_LINE_WEIGHT, tolerance=TEAM_TOLERANCE)
    if not result:
        await ctx.send("가능한 라인 배정 조합을 찾지 못했어요… ㅠㅠ 라인 구성을 한 번만 조정해볼까요?", delete_after=7)
        return

    teamA, teamB, pA, pB = result
    embed = discord.Embed(title=cute("팀 배정 완료!"), color=0xB1E3AD)
    embed.add_field(
        name=f"팀 A (전투력 {round(pA, 2)})",
        value="\n".join(f"- {n} ({l} {t})" for n, l, t in teamA),
        inline=False,
    )
    embed.add_field(
        name=f"팀 B (전투력 {round(pB, 2)})",
        value="\n".join(f"- {n} ({l} {t})" for n, l, t in teamB),
        inline=False,
    )
    embed.set_footer(text=f"전투력 차이: {round(abs(pA - pB), 2)}  |  파라미터 k={K_LINE_WEIGHT}, tolerance={TEAM_TOLERANCE}")
    await ctx.send(embed=embed)

# ==================== 기타 ====================
@bot.command()
async def 가이드(ctx: commands.Context):
    if not await ensure_channel_or_hint(ctx):
        return
    await ctx.send(
        "사용법 안내에요~\n"
        "• `/라인` → 본인만 보이는 UI(에페메랄)로 라인·티어 등록\n"
        "• `!라인` → DM으로 UI 전송\n"
        "• `!라인삭제 탑` / `!라인초기화`\n"
        "• `!티어 2` → 등록된 모든 라인의 티어를 한 번에 변경\n"
        "• `!내전시작` → `!참가` → `!팀짜기` 흐름으로 진행해요\n"
        "궁금한 점은 언제든지 불러줘요! (❁´◡`❁)ﾉﾞ"
    )

@bot.command()
async def 섹스(ctx: commands.Context):
    if not await ensure_channel_or_hint(ctx):
        return
    await ctx.send("운지")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # (선택) 지정 채널에서만 반응하려면 유지, 제한 풀려면 다음 if 블록 삭제
    in_target = False
    if isinstance(message.channel, discord.TextChannel):
        in_target = (message.channel.name == TARGET_CHANNEL)
    elif isinstance(message.channel, discord.Thread) and isinstance(message.channel.parent, discord.TextChannel):
        in_target = (message.channel.parent.name == TARGET_CHANNEL)

    if bot.user and (bot.user in message.mentions) and in_target:
        try:
            await message.channel.send("부르셨냐요? (˶˃ ᵕ ˂˶)و ✨")
        except Exception as e:
            print("mention reply error:", repr(e))

    # 명령어도 처리되도록 반드시 호출
    await bot.process_commands(message)

load_dotenv()
OPENROUTER_API_KEY = "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"

# AI 호출 함수
async def ask_openrouter(prompt: str) -> str:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    data = {
        "model": "deepseek/deepseek-chat-v3.1:free",  # 원하는 모델 (예: gpt-4o, gpt-3.5 등)
        "messages": [{"role": "user", "content": prompt}],
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=data) as resp:
            if resp.status != 200:
                return f"AI 요청 실패.. 상태코드 {resp.status}"
            result = await resp.json()
            return result["choices"][0]["message"]["content"]

# 디스코드 커맨드 추가
bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())

@bot.command(name="ai")
async def ai_chat(ctx, *, prompt: str):
    """AI한테 물어보기"""
    await ctx.send("잠깐만요! AI가 생각중이에요... ")
    answer = await ask_openrouter(prompt)
    await ctx.send(answer)

# ==================== 실행 ====================
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("[경고] DISCORD_TOKEN 환경변수를 설정하세요.")
    bot.run(DISCORD_TOKEN if DISCORD_TOKEN else "invalid-token-will-fail")
