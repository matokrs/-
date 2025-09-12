import os
import asyncio
import datetime
import itertools
import random
from dataclasses import dataclass, field
from typing import Dict, Set, List, Optional, Tuple

import discord
from discord.ext import commands, tasks

# -------------------- 설정 --------------------
DISCORD_TOKEN = "MTQxNTY3MDkwODIxOTI5Mzc2Nw.GRlAqW.LRci66Dq8noIwoMPKMBHnfFIidaVBgj8alku1k"
TARGET_CHANNEL = "마린크래프트"  # 채널 "이름" 기준

LANE_NAMES = ["탑", "정글", "미드", "원딜", "서폿"]
# 라인 배정 우선순위 (선호 라인 충족을 위한 탐색 순)
lane_priority = ["미드", "정글", "원딜", "탑", "서폿"]
# 라인 별칭(영문/변형) → 표준 라인명 매핑
LANE_ALIASES = {
    "top": "탑", "t": "ㅌ", "탑": "탑",
    "jungle": "정글", "jg": "ㅈㄱ", "정글": "정글",
    "mid": "미드", "m": "ㅁㄷ", "미드": "미드",
    "adc": "원딜", "bot": "ㅇㄷ", "원딜": "원딜",
    "support": "서폿", "sup": "ㅅㅍ", "서폿": "서폿", "서포트": "서폿", "서포터": "서폿",
}

# Windows에서 일부 환경 비동기 루프 이슈 방지
if os.name == "nt":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# -------------------- 데이터 구조 --------------------
@dataclass
class Player:
    uid: int
    name: str
    lanes: Set[str] = field(default_factory=set)  # 표준 라인명 집합
    tier: Optional[int] = None  # 1,2,3

# 전역 상태 (메모리)
players: Dict[int, Player] = {}
lines: Dict[str, Set[int]] = {ln: set() for ln in LANE_NAMES}
tiers: Dict[int, Set[int]] = {1: set(), 2: set(), 3: set()}
participants: List[int] = []  # uid 리스트
participants_start: Optional[datetime.datetime] = None
match_open: bool = False

# -------------------- 디스코드 기본 설정 --------------------
intents = discord.Intents.default()
intents.message_content = True  # 개발자 포털에서 해당 권한 활성화 필요
intents.guilds = True
intents.members = True  # 닉네임 갱신을 위해 권장 (필수는 아님)

bot = commands.Bot(command_prefix="!", intents=intents)

# -------------------- 유틸 --------------------
def in_target_channel(ctx: commands.Context) -> bool:
    """명령 사용 채널이 TARGET_CHANNEL 이름과 일치하는 텍스트채널인지 검사."""
    ch = ctx.channel
    return isinstance(ch, (discord.TextChannel, discord.Thread)) and getattr(ch, "name", None) == TARGET_CHANNEL

async def require_channel(ctx: commands.Context) -> bool:
    if not in_target_channel(ctx):
        # 조용히 무시 대신 안내
        await ctx.send(f"이 명령은 #{TARGET_CHANNEL} 채널에서만 사용할수 있어 이년아.")
        return False
    return True

def normalize_lane(token: str) -> Optional[str]:
    if not token:
        return None
    t = token.replace(" ", "").replace(",", "").replace("，", "").lower()
    return LANE_ALIASES.get(t) if t in LANE_ALIASES else (t if t in LANE_NAMES else None)

def ensure_player(ctx: commands.Context) -> Player:
    """플레이어 엔트리를 만들고 최신 닉네임으로 갱신."""
    uid = ctx.author.id
    name = ctx.author.display_name
    p = players.get(uid)
    if p is None:
        p = Player(uid=uid, name=name)
        players[uid] = p
    else:
        # 닉변 반영
        if p.name != name:
            p.name = name
    return p

# 가중치: 서폿 > 정글 > 미드 > 원딜 > 탑
def line_weight(lane: str, k: float = 0.3) -> float:
    if lane == "서폿": return 1 + 2*k
    if lane == "정글": return 1 + k
    if lane == "미드": return 1
    if lane == "원딜": return 1 - k
    if lane == "탑":   return 1 - 2*k
    return 1

def player_ppi(tier_int: int, lane: str, k: float = 0.3) -> float:
    # 1티어=3, 2티어=2, 3티어=1
    return (4 - tier_int) * line_weight(lane, k)

# 라인 배정 (백트래킹)
def assign_lines(team: List[Dict], k: float = 0.3) -> List[Tuple[List[Tuple[str, str]], float]]:
    # team: [{"name": str, "tier": int, "lanes": [lane,...]}] 길이 5
    lanes_required = set(LANE_NAMES)
    results: List[Tuple[List[Tuple[str, str]], float]] = []

    # 탐색량 줄이기: 선택지(지원 라인) 적은 플레이어부터 정렬
    # 또한 lane_priority 순으로 각 플레이어 선호 정렬
    def lane_prio_key(l: str) -> int:
        try:
            return lane_priority.index(l)
        except ValueError:
            return len(lane_priority)

    team_sorted = sorted(team, key=lambda p: (len(p["lanes"]), p["name"]))
    pref = [sorted([l for l in p["lanes"] if l in LANE_NAMES], key=lane_prio_key) for p in team_sorted]

    chosen: List[str] = []

    def backtrack(i: int, used: Set[str]):
        if i == 5:
            if used == lanes_required:
                power = sum(player_ppi(team_sorted[j]["tier"], chosen[j], k) for j in range(5))
                results.append((list(zip([p["name"] for p in team_sorted], chosen)), power))
            return
        # 가지치기: 남은 자리에 필요한 라인 수와 남은 플레이어 수가 맞지 않으면 중단
        remaining_slots = 5 - i
        remaining_lanes = len(lanes_required - used)
        if remaining_lanes > remaining_slots:
            return
        for lane in pref[i]:
            if lane not in used:
                used.add(lane); chosen.append(lane)
                backtrack(i+1, used)
                chosen.pop(); used.remove(lane)

    backtrack(0, set())
    return results

# 팀 분할 + 라인배정
def make_teams(players10: List[Dict], k: float = 0.3, tolerance: float = 4.0):
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

    if candidates:
        return random.choice(candidates)
    return best_split

# -------------------- 이벤트/태스크 --------------------
@bot.event
async def on_ready():
    print(f"로그인 성공: {bot.user}")
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
                # 공지
                ch = discord.utils.get(bot.get_all_channels(), name=TARGET_CHANNEL)
                if isinstance(ch, discord.TextChannel):
                    await ch.send("12시간 경과, 참가 명단 초기화")
        except Exception as e:
            # 조용히 로그만 출력
            print("reset_checker error:", repr(e))

@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    # 깔끔한 에러 안내
    if isinstance(error, commands.CommandNotFound):
        return
    msg = f"⚠️ 오류: {error.__class__.__name__} — {error}"
    try:
        await ctx.send(msg)
    except Exception:
        # 메시지 전송 자체가 실패하는 경우 콘솔 로깅
        print(msg)

# -------------------- 명령: 라인 --------------------
@bot.command()
@commands.guild_only()
async def 라인(ctx: commands.Context, *, lanes: str = None):
    if not await require_channel(ctx):
        return
    if not lanes:
        await ctx.send("사용법: `!라인 탑` 또는 `!라인 탑, 정글` 또는 !라인 ㅌ, ㅁㄷ, ㅈㄱ, ㅇㄷ, ㅅㅍ 가능")
        return

    p = ensure_player(ctx)

    # 입력 파싱
    raw_tokens = [x.strip() for x in lanes.replace("，", ",").split(",") if x.strip()]
    normalized: List[str] = []
    invalid: List[str] = []
    for tok in raw_tokens:
        n = normalize_lane(tok)
        if n in LANE_NAMES:
            if n not in normalized:
                normalized.append(n)
        else:
            invalid.append(tok)

    # 등록
    added: List[str] = []
    for ln in normalized:
        if ln not in p.lanes:
            p.lanes.add(ln)
            lines[ln].add(p.uid)
            added.append(ln)

    if added:
        await ctx.send(f"{p.name} → {', '.join(added)}")
    else:
        await ctx.send(f"{p.name} 이미 등록됐어 이년아")

    if invalid:
        await ctx.send(f"재대로 입력해봐: {', '.join(invalid)} (가능: {', '.join(LANE_NAMES)})")

@bot.command()
@commands.guild_only()
async def 라인현황(ctx: commands.Context):
    if not await require_channel(ctx):
        return
    def fmt(ln: str) -> str:
        uids = lines[ln]
        names = [players[uid].name for uid in uids if uid in players]
        return f"{ln}: {', '.join(sorted(names)) if names else '(없음)'}"
    msg = "라인 현황\n" + "\n".join(fmt(ln) for ln in LANE_NAMES)
    await ctx.send(msg)

@bot.command()
@commands.guild_only()
async def 라인초기화(ctx: commands.Context):
    if not await require_channel(ctx):
        return
    p = ensure_player(ctx)
    if not p.lanes:
        await ctx.send(f"{p.name} 등록된 라인 없음")
        return
    for ln in list(p.lanes):
        p.lanes.remove(ln)
        lines[ln].discard(p.uid)
    await ctx.send(f"{p.name} 라인 초기화 완료")

# -------------------- 명령: 티어 --------------------
@bot.command()
@commands.guild_only()
async def 티어(ctx: commands.Context, t: str = None):
    if not await require_channel(ctx):
        return
    if t not in {"1", "2", "3"}:
        await ctx.send("사용법: `!티어 1` / `!티어 2` / `!티어 3`")
        return
    tier_value = int(t)

    p = ensure_player(ctx)
    if p.tier == tier_value:
        await ctx.send(f"{p.name} 이미 티어 {t}")
        return

    if p.tier in (1, 2, 3):
        tiers[p.tier].discard(p.uid)
    p.tier = tier_value
    tiers[tier_value].add(p.uid)
    await ctx.send(f"{p.name} 티어 {t} 등록")

@bot.command()
@commands.guild_only()
async def 티어현황(ctx: commands.Context):
    if not await require_channel(ctx):
        return
    parts = []
    for tv in sorted(tiers.keys()):
        uids = tiers[tv]
        names = [players[uid].name for uid in uids if uid in players]
        parts.append(f"티어 {tv}: {', '.join(sorted(names)) if names else '(없음)'}")
    await ctx.send("티어 현황\n" + "\n".join(parts))

# -------------------- 명령: 내전 참가 관리 --------------------
@bot.command()
@commands.guild_only()
async def 내전시작(ctx: commands.Context):
    global match_open, participants_start
    if not await require_channel(ctx):
        return
    match_open = True
    participants.clear()
    participants_start = None
    await ctx.send("내전 시작! `!참가` / `!퇴장` 사용 가능")

@bot.command()
@commands.guild_only()
async def 참가(ctx: commands.Context):
    global participants_start
    if not await require_channel(ctx):
        return
    if not match_open:
        await ctx.send("내전 아직이야 이년아. `!내전시작`으로 열어.")
        return

    p = ensure_player(ctx)
    missing = []
    if not p.lanes:
        missing.append("라인")
    if p.tier not in (1, 2, 3):
        missing.append("티어")
    if missing:
        await ctx.send(f"{p.name} {', '.join(missing)} 등록 필요")
        return

    if p.uid in participants:
        await ctx.send(f"ℹ{p.name} 이미 참가 했어 이년아")
        return
    if len(participants) >= 10:
        await ctx.send("다 찼어!.")
        return

    participants.append(p.uid)
    if not participants_start:
        participants_start = datetime.datetime.now()
    await ctx.send(f"{p.name} 참가 ({len(participants)}/10)")

    if len(participants) == 10:
        await ctx.send("참가 10명 확정!")

@bot.command()
@commands.guild_only()
async def 퇴장(ctx: commands.Context):
    if not await require_channel(ctx):
        return
    if not match_open:
        await ctx.send("내전 아직이야 이년아")
        return
    p = ensure_player(ctx)
    if p.uid not in participants:
        await ctx.send(f"ℹ{p.name} 명단에 없음")
        return
    participants.remove(p.uid)
    await ctx.send(f"🚪 {p.name} 퇴장 ({len(participants)}/10)")

@bot.command()
@commands.guild_only()
async def 참가현황(ctx: commands.Context):
    if not await require_channel(ctx):
        return
    if not participants:
        await ctx.send("참가 인원\n(없음)")
        return
    msg = "👥 참가 인원\n"
    for uid in participants:
        p = players.get(uid)
        if not p:
            continue
        ln = sorted(list(p.lanes))
        tr = p.tier if p.tier in (1, 2, 3) else "미정"
        msg += f"{p.name} → 라인: {', '.join(ln) if ln else '미정'}, 티어: {tr}\n"
    await ctx.send(msg)

@bot.command()
@commands.guild_only()
async def 종료(ctx: commands.Context):
    global match_open, participants_start
    if not await require_channel(ctx):
        return
    participants.clear()
    participants_start = None
    match_open = False
    await ctx.send("강제 모집 종료 초기화")

# -------------------- 명령: 팀 짜기 --------------------
@bot.command()
@commands.guild_only()
async def 팀짜기(ctx: commands.Context):
    if not await require_channel(ctx):
        return
    if len(participants) != 10:
        await ctx.send("아직 10명 안찼어.")
        return

    players10 = []
    for uid in participants:
        p = players.get(uid)
        if not p:
            await ctx.send("내부 데이터 오류: 플레이어를 찾을 수 없습니다.")
            return
        if not p.lanes or p.tier not in (1, 2, 3):
            await ctx.send(f"{p.name} 라인/티어 정보가 부족합니다.")
            return
        lanes_clean = [ln for ln in p.lanes if ln in LANE_NAMES]
        if not lanes_clean:
            await ctx.send(f"{p.name} 유효한 라인이 없습니다.")
            return
        players10.append({"name": p.name, "tier": int(p.tier), "lanes": lanes_clean})

    result = make_teams(players10, k=0.3, tolerance=4.0)
    if not result:
        await ctx.send("라인 배정 가능한 조합을 찾지 못했습니다.")
        return

    teamA, teamB, pA, pB = result

    embed = discord.Embed(title="팀 배정 완료", color=0x00FFCC)
    embed.add_field(
        name=f"팀 A (전투력 {round(pA, 2)})",
        value="\n".join(f"- {n} ({l})" for n, l in teamA),
        inline=False,
    )
    embed.add_field(
        name=f"팀 B (전투력 {round(pB, 2)})",
        value="\n".join(f"- {n} ({l})" for n, l in teamB),
        inline=False,
    )
    embed.set_footer(text=f"전투력 차이: {round(abs(pA - pB), 2)}")
    await ctx.send(embed=embed)

# -------------------- 실행 --------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN or DISCORD_TOKEN == "MTQxNTY3MDkwODIxOTI5Mzc2Nw.G6rJVK.El_JxOvZNrf0BltGA0CxOdwOsQ4elo43zxq7Vg":
        print("[경고] DISCORD_TOKEN이 설정되지 않았습니다. 환경변수 DISCORD_TOKEN 또는 코드 상수를 설정하세요.")
    bot.run(DISCORD_TOKEN)
