import os
import random
import hashlib
import sqlite3
import json
import time
import re
import html
from flask import Flask, render_template, request, redirect, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash
import requests
from dotenv import load_dotenv
from collections import Counter
from champion_meta import (
    CHAMPION_TIERS, CURRENT_PATCH,
    ROLE_KR, TIER_COLOR, TREND_ICON, TREND_COLOR,
    DIFFICULTY_LABEL, DIFFICULTY_COLOR,
    NUMERIC_TIER_COLOR, CHAMPION_ROLE_MAP, META_STATS,
    DEFAULT_ROLE_STATS, calc_meta_tier, RANK_TIER_LABELS,
    get_champ_profile, COUNTER_ITEMS
)

load_dotenv()
app = Flask(__name__)
# 세션 암호화 키 (프로덕션은 환경변수 SECRET_KEY 설정 권장)
app.secret_key = os.getenv("SECRET_KEY", "sbgg-dev-secret-key-change-me")

API_KEY = os.getenv("RIOT_API_KEY")
HEADERS = {"X-Riot-Token": API_KEY}

# 💾 데이터베이스 — DATABASE_URL(외부 Postgres) 있으면 영구 DB, 없으면 로컬 SQLite
DB_FILE = "sbgg.db"
DATABASE_URL = os.getenv("DATABASE_URL")
IS_PG = bool(DATABASE_URL)
if IS_PG:
    import psycopg2
AUTO_PK = "SERIAL PRIMARY KEY" if IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
CACHE_EXPIRE_TIME = 300      # 캐시 만료 시간: 5분
LEADERBOARD_CACHE_TIME = 600 # 래더 캐시: 10분
SPECTATE_CACHE_TIME = 60     # 관전 캐시: 1분 (라이브 데이터)

# 🧠 AI 인게임 코치 (킬러 기능) — 안전 게이팅
#   AI_COACH_ENABLED : 기능 노출 여부 (기본 on)
#   AI_COACH_LIVE    : 실제 LLM 호출 여부 (기본 off → 데모 리포트, 키·비용·환각 0)
#   AI_COACH_MODEL   : 코치 모델 (품질 vs 비용). Anthropic 최신 권장.
AI_COACH_ENABLED = os.getenv("AI_COACH_ENABLED", "1") == "1"
AI_COACH_LIVE = os.getenv("AI_COACH_LIVE", "0") == "1"
AI_COACH_MODEL = os.getenv("AI_COACH_MODEL", "claude-sonnet-5")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
AI_COACH_CACHE_TTL = 60 * 60 * 24 * 14  # AI 코치 리포트 캐시: 14일

# ─── SQLite/Postgres 양립 DB 래퍼 ───
# 코드는 ? 플레이스홀더로 작성하고, Postgres에서는 자동으로 %s로 변환.
class _DBCursor:
    def __init__(self, cur): self._c = cur
    def execute(self, sql, params=()):
        self._c.execute(sql.replace("?", "%s") if IS_PG else sql, params); return self
    def fetchone(self): return self._c.fetchone()
    def fetchall(self): return self._c.fetchall()
    def __iter__(self): return iter(self._c.fetchall())  # for row in execute(...) 지원
    @property
    def lastrowid(self):
        try: return self._c.lastrowid
        except Exception: return None

class _DBConn:
    def __init__(self):
        self._c = psycopg2.connect(DATABASE_URL) if IS_PG else sqlite3.connect(DB_FILE)
    def cursor(self): return _DBCursor(self._c.cursor())
    def execute(self, sql, params=()):
        cur = self._c.cursor()
        cur.execute(sql.replace("?", "%s") if IS_PG else sql, params)
        return _DBCursor(cur)
    def commit(self): self._c.commit()
    def close(self): self._c.close()

def db_connect():
    return _DBConn()

def riot_get(url):
    return requests.get(url, headers=HEADERS, timeout=5)

def init_db():
    conn = db_connect()
    cursor = conn.cursor()
    # 소환사별 랭크 검색 데이터를 통째로 캐싱할 테이블 생성
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS search_cache (
            cache_key TEXT PRIMARY KEY,
            json_data TEXT,
            updated_at INTEGER
        )
    ''')
    # 고객의 소리(피드백) 테이블 — 추후 회원 기능 대비 user_ref 컬럼 포함
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS feedback (
            id {AUTO_PK},
            category TEXT,
            content TEXT NOT NULL,
            contact TEXT,
            user_ref TEXT,
            created_at INTEGER
        )
    ''')
    # ★ 실시간 통계 파이프라인 — 챔피언별 표본 누적 (피기백 수집)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS champion_stats (
            champ_en TEXT, role TEXT,
            games INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
            PRIMARY KEY (champ_en, role)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS champion_bans (
            champ_en TEXT PRIMARY KEY, bans INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS stats_meta (
            key TEXT PRIMARY KEY, value TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS processed_matches (
            match_id TEXT PRIMARY KEY, processed_at INTEGER
        )
    ''')
    # 듀오 찾기 게시판
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS duo_posts (
            id {AUTO_PK},
            name TEXT, tag TEXT, tier TEXT,
            my_role TEXT, find_role TEXT,
            queue_type TEXT, mic TEXT, message TEXT,
            created_at INTEGER
        )
    ''')
    # 회원 계정
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS users (
            id {AUTO_PK},
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            riot_name TEXT, riot_tag TEXT,
            created_at INTEGER
        )
    ''')
    # ★ 빌드 수집 (룬/스펠/아이템/스킬/아이템순서) — 실측 빌드 산출용
    cursor.execute('''CREATE TABLE IF NOT EXISTS build_runes (
        champ_en TEXT, role TEXT, keystone INTEGER, primary_style INTEGER, sub_style INTEGER,
        games INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
        PRIMARY KEY (champ_en, role, keystone, primary_style, sub_style))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS build_spells (
        champ_en TEXT, role TEXT, spells TEXT,
        games INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
        PRIMARY KEY (champ_en, role, spells))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS build_items (
        champ_en TEXT, role TEXT, item_id TEXT,
        games INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
        PRIMARY KEY (champ_en, role, item_id))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS build_skills (
        champ_en TEXT, role TEXT, skill_order TEXT,
        games INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
        PRIMARY KEY (champ_en, role, skill_order))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS build_item_order (
        champ_en TEXT, role TEXT, seq TEXT,
        games INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
        PRIMARY KEY (champ_en, role, seq))''')
    # 고티어(에메랄드+) 운영법 — 에디터 직접 작성 콘텐츠 (단계별)
    cursor.execute('''CREATE TABLE IF NOT EXISTS champion_guide (
        champ_en TEXT, bracket TEXT DEFAULT 'emeraldplus', phase TEXT,
        title TEXT, body TEXT, updated_at INTEGER,
        PRIMARY KEY (champ_en, bracket, phase))''')
    # 티어별 지표 벤치마크 — 수집/검색 게임에서 (티어×역할×지표) 평균 적립
    cursor.execute('''CREATE TABLE IF NOT EXISTS tier_benchmark (
        tier TEXT, role TEXT, metric TEXT,
        total REAL DEFAULT 0, cnt INTEGER DEFAULT 0,
        PRIMARY KEY (tier, role, metric))''')
    # 상세 룬 페이지 (키스톤+주룬3+보조룬2+샤드3 전체)
    cursor.execute('''CREATE TABLE IF NOT EXISTS build_runepages (
        champ_en TEXT, role TEXT, page TEXT, primary_style INTEGER, sub_style INTEGER,
        games INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
        PRIMARY KEY (champ_en, role, page))''')
    # 추천 신발
    cursor.execute('''CREATE TABLE IF NOT EXISTS build_boots (
        champ_en TEXT, role TEXT, item_id TEXT,
        games INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
        PRIMARY KEY (champ_en, role, item_id))''')
    # 시작 아이템 세트
    cursor.execute('''CREATE TABLE IF NOT EXISTS build_starts (
        champ_en TEXT, role TEXT, items TEXT,
        games INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
        PRIMARY KEY (champ_en, role, items))''')
    # 레벨별 스킬 선택 (lol.ps 스타일 18레벨 스킬트리)
    cursor.execute('''CREATE TABLE IF NOT EXISTS build_skill_levels (
        champ_en TEXT, role TEXT, lvl INTEGER, slot INTEGER,
        games INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
        PRIMARY KEY (champ_en, role, lvl, slot))''')
    # 카운터 라인 맞대결
    cursor.execute('''CREATE TABLE IF NOT EXISTS build_matchups (
        champ_en TEXT, role TEXT, opponent TEXT,
        games INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
        PRIMARY KEY (champ_en, role, opponent))''')
    # 타임라인 처리 완료 매치 (스킬/아이템순서 중복 방지)
    cursor.execute('''CREATE TABLE IF NOT EXISTS processed_timelines (
        match_id TEXT PRIMARY KEY, processed_at INTEGER)''')
    conn.commit()
    conn.close()

# ✅ MEDIUM 개선: SQLite 연결/해제 보일러플레이트를 헬퍼 함수로 추상화
def db_read(cache_key):
    """캐시 키로 데이터를 조회. (json_data, updated_at) 또는 (None, 0) 반환."""
    try:
        conn = db_connect()
        row = conn.execute("SELECT json_data, updated_at FROM search_cache WHERE cache_key=?", (cache_key,)).fetchone()
        conn.close()
        return (row[0], row[1]) if row else (None, 0)
    except Exception as e:
        print(f"DB 읽기 에러 [{cache_key}]: {e}")
        return (None, 0)

def db_write(cache_key, data, current_time):
    """데이터를 JSON으로 직렬화하여 캐시에 저장/갱신."""
    try:
        conn = db_connect()
        conn.execute("""INSERT INTO search_cache (cache_key, json_data, updated_at) VALUES (?, ?, ?)
                        ON CONFLICT(cache_key) DO UPDATE SET json_data=EXCLUDED.json_data, updated_at=EXCLUDED.updated_at""",
                     (cache_key, json.dumps(data), current_time))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"DB 쓰기 에러 [{cache_key}]: {e}")

# ═══════════════════════════════════════════════════════════════════════
#  ★ 실시간 통계 데이터 파이프라인
# ═══════════════════════════════════════════════════════════════════════
VALID_ROLES = {"TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"}
MIN_SAMPLE = 15  # 실측 데이터로 인정할 최소 표본(경기 수)
WR_PRIOR = 20    # 승률 베이지안 보정용 가상 표본(50% 기준으로 끌어당김)
_LAST_DB_ERROR = None  # 마지막 수집 에러 (진단용)

def smooth_wr(wins, games, prior=WR_PRIOR):
    """표본이 적을 때 승률 노이즈 제거 — 50% 기준 베이지안 보정.
    (승 + prior/2) / (판수 + prior). 판수가 커질수록 raw 승률에 수렴."""
    if not games:
        return 0.0
    return round((wins + prior * 0.5) / (games + prior) * 100, 1)

# Riot championName ≠ DDragon id 인 케이스 (이미지/한글명/빌드 매칭 깨짐 방지)
RIOT_NAME_ALIAS = {"Wukong": "MonkeyKing", "FiddleSticks": "Fiddlesticks"}
def champ_ddragon_id(name):
    """Riot championName → DDragon 정식 id. 통계·이미지·한글명을 일관되게."""
    if not name:
        return name
    if name in CHAMP_KR_MAP:
        return name
    if name in RIOT_NAME_ALIAS:
        return RIOT_NAME_ALIAS[name]
    return next((c for c in CHAMP_KR_MAP if c.lower() == name.lower()), name)

def match_champion_query(q):
    """검색어가 챔피언 이름(한글/영문)과 정확히 일치하면 DDragon id 반환, 아니면 None.
    검색창에 챔피언명을 치면 메타/상세 페이지로 보내기 위함."""
    q = (q or "").strip()
    if not q:
        return None
    # 한글 정식명 정확 일치 (르블랑, 오공 …)
    for cid, kr in CHAMP_KR_MAP.items():
        if kr == q:
            return cid
    # 영문 id/별칭/대소문자 보정
    cid = champ_ddragon_id(q)
    return cid if cid in CHAMP_KR_MAP else None

def record_match_stats(m_res, match_id):
    """매치 1건의 챔피언 픽/승패/밴을 통계 DB에 누적. 중복 매치는 건너뜀.
    피기백 수집: 전적 검색 시 이미 가져온 매치를 재활용 (추가 API 호출 0)."""
    try:
        conn = db_connect()
        cur = conn.cursor()
        # 중복 방지
        if cur.execute("SELECT 1 FROM processed_matches WHERE match_id=?", (match_id,)).fetchone():
            conn.close()
            return False

        info = m_res.get('info', {})
        # 비정상 게임 제외: 랭크(420/440)만 + 리메이크(5분 미만) 제외
        if info.get('queueId') not in (420, 440) or info.get('gameDuration', 0) < 300:
            conn.close()
            return False
        # 픽 / 승패 + 빌드(룬/스펠/아이템) 수집
        for p in info.get('participants', []):
            role = p.get('teamPosition', '')
            if role not in VALID_ROLES:
                continue
            champ = champ_ddragon_id(p.get('championName', ''))
            if not champ:
                continue
            win = 1 if p.get('win') else 0
            cur.execute("""INSERT INTO champion_stats (champ_en, role, games, wins) VALUES (?,?,1,?)
                           ON CONFLICT(champ_en, role) DO UPDATE SET games=champion_stats.games+1, wins=champion_stats.wins+?""",
                        (champ, role, win, win))

            # 상세 룬 페이지 (키스톤+주룬3 | 보조룬2 | 샤드3)
            try:
                perks = p.get('perks', {})
                styles = perks['styles']
                primary_sel = [s['perk'] for s in styles[0]['selections']]
                sub_sel = [s['perk'] for s in styles[1]['selections']]
                stat = perks.get('statPerks', {})
                shards = [stat.get('offense'), stat.get('flex'), stat.get('defense')]
                page = ",".join(str(x) for x in primary_sel) + "|" + \
                       ",".join(str(x) for x in sub_sel) + "|" + \
                       ",".join(str(x) for x in shards)
                cur.execute("""INSERT INTO build_runepages (champ_en, role, page, primary_style, sub_style, games, wins)
                               VALUES (?,?,?,?,?,1,?)
                               ON CONFLICT(champ_en, role, page) DO UPDATE SET games=build_runepages.games+1, wins=build_runepages.wins+?""",
                            (champ, role, page, styles[0]['style'], styles[1]['style'], win, win))
            except (KeyError, IndexError, TypeError):
                pass

            # 소환사 주문 (정렬된 조합)
            s1, s2 = p.get('summoner1Id'), p.get('summoner2Id')
            if s1 and s2:
                combo = "-".join(sorted([str(s1), str(s2)]))
                cur.execute("""INSERT INTO build_spells (champ_en, role, spells, games, wins) VALUES (?,?,?,1,?)
                               ON CONFLICT(champ_en, role, spells) DO UPDATE SET games=build_spells.games+1, wins=build_spells.wins+?""",
                            (champ, role, combo, win, win))

            # 최종 아이템 → 코어/신발 분리 빈도 집계
            for i in range(6):
                iid = str(p.get(f'item{i}', 0))
                if iid == '0':
                    continue
                if iid in BOOTS_ITEMS:
                    cur.execute("""INSERT INTO build_boots (champ_en, role, item_id, games, wins) VALUES (?,?,?,1,?)
                                   ON CONFLICT(champ_en, role, item_id) DO UPDATE SET games=build_boots.games+1, wins=build_boots.wins+?""",
                                (champ, role, iid, win, win))
                elif iid in CORE_ITEMS:
                    cur.execute("""INSERT INTO build_items (champ_en, role, item_id, games, wins) VALUES (?,?,?,1,?)
                                   ON CONFLICT(champ_en, role, item_id) DO UPDATE SET games=build_items.games+1, wins=build_items.wins+?""",
                                (champ, role, iid, win, win))

        # 카운터 라인 맞대결 (같은 라인 양 팀 챔피언 대결)
        by_role = {}
        for p in info.get('participants', []):
            r = p.get('teamPosition', '')
            if r in VALID_ROLES and p.get('championName'):
                by_role.setdefault(r, []).append((champ_ddragon_id(p['championName']), p.get('teamId'), 1 if p.get('win') else 0))
        for r, plist in by_role.items():
            if len(plist) == 2 and plist[0][1] != plist[1][1]:
                (ca, _, wa), (cb, _, wb) = plist
                cur.execute("""INSERT INTO build_matchups (champ_en, role, opponent, games, wins) VALUES (?,?,?,1,?)
                               ON CONFLICT(champ_en, role, opponent) DO UPDATE SET games=build_matchups.games+1, wins=build_matchups.wins+?""",
                            (ca, r, cb, wa, wa))
                cur.execute("""INSERT INTO build_matchups (champ_en, role, opponent, games, wins) VALUES (?,?,?,1,?)
                               ON CONFLICT(champ_en, role, opponent) DO UPDATE SET games=build_matchups.games+1, wins=build_matchups.wins+?""",
                            (cb, r, ca, wb, wb))
        # 밴
        for team in info.get('teams', []):
            for ban in team.get('bans', []):
                cid = str(ban.get('championId', -1))
                champ = CHAMP_KEYS.get(cid)
                if not champ:
                    continue
                cur.execute("""INSERT INTO champion_bans (champ_en, bans) VALUES (?,1)
                               ON CONFLICT(champ_en) DO UPDATE SET bans=champion_bans.bans+1""", (champ,))
        # 총 경기 수 증가 + 처리 완료 기록
        cur.execute("""INSERT INTO stats_meta (key, value) VALUES ('total_games', '1')
                       ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(stats_meta.value AS INTEGER)+1 AS TEXT)""")
        cur.execute("INSERT INTO processed_matches (match_id, processed_at) VALUES (?,?) ON CONFLICT(match_id) DO NOTHING",
                    (match_id, int(time.time())))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        global _LAST_DB_ERROR
        import traceback
        _LAST_DB_ERROR = traceback.format_exc()
        print(f"통계 기록 에러 [{match_id}]: {e}")
        try: conn.close()  # 에러 시 연결 누수 방지 (Postgres 연결 한도 보호)
        except Exception: pass
        return False

SKILL_LETTER = {1: "Q", 2: "W", 3: "E", 4: "R"}

def record_timeline_stats(timeline, m_res, match_id):
    """타임라인에서 스킬 마스터 순서 + 코어 아이템 구매 순서를 수집.
    (Riot Timeline API 추가 호출 필요 — /collect 시드 수집에서만 사용)"""
    try:
        conn = db_connect()
        cur = conn.cursor()
        if cur.execute("SELECT 1 FROM processed_timelines WHERE match_id=?", (match_id,)).fetchone():
            conn.close()
            return False

        # participantId(1~10) → (champ, role, win) 매핑
        pmeta = {}
        for p in m_res.get('info', {}).get('participants', []):
            pid = p.get('participantId')
            role = p.get('teamPosition', '')
            if pid and role in VALID_ROLES and p.get('championName'):
                pmeta[pid] = (champ_ddragon_id(p['championName']), role, 1 if p.get('win') else 0)

        skill_seq = {pid: [] for pid in pmeta}    # 슬롯(1/2/3/4) 레벨업 순서 (전체 18레벨)
        item_seq = {pid: [] for pid in pmeta}     # 코어 아이템 구매 순서
        start_items = {pid: set() for pid in pmeta}  # 시작 아이템 (초반 70초)

        for frame in timeline.get('info', {}).get('frames', []):
            for ev in frame.get('events', []):
                pid = ev.get('participantId')
                if pid not in pmeta:
                    continue
                et = ev.get('type')
                if et == 'SKILL_LEVEL_UP':
                    slot = ev.get('skillSlot')
                    if slot in (1, 2, 3, 4) and len(skill_seq[pid]) < 18:
                        skill_seq[pid].append(slot)
                elif et == 'ITEM_PURCHASED':
                    iid = str(ev.get('itemId', 0))
                    ts = ev.get('timestamp', 0)
                    if ts <= 70000 and iid in START_ITEMS:
                        start_items[pid].add(iid)
                    if iid in CORE_ITEMS and iid not in item_seq[pid]:
                        item_seq[pid].append(iid)

        for pid, (champ, role, win) in pmeta.items():
            full = skill_seq[pid]
            # 스킬 마스터 순서(우선순위): Q/W/E 초반 9포인트 빈도
            qwe = [s for s in full if s in (1, 2, 3)][:9]
            if qwe:
                counts = {}
                for idx, slot in enumerate(qwe):
                    if slot not in counts:
                        counts[slot] = [0, idx]
                    counts[slot][0] += 1
                ordered = sorted(counts.keys(), key=lambda s: (-counts[s][0], counts[s][1]))
                order_str = ">".join(SKILL_LETTER[s] for s in ordered)
                cur.execute("""INSERT INTO build_skills (champ_en, role, skill_order, games, wins) VALUES (?,?,?,1,?)
                               ON CONFLICT(champ_en, role, skill_order) DO UPDATE SET games=build_skills.games+1, wins=build_skills.wins+?""",
                            (champ, role, order_str, win, win))
            # 레벨별 스킬 (lol.ps 스타일 18레벨 트리)
            for lvl, slot in enumerate(full, start=1):
                cur.execute("""INSERT INTO build_skill_levels (champ_en, role, lvl, slot, games, wins) VALUES (?,?,?,?,1,?)
                               ON CONFLICT(champ_en, role, lvl, slot) DO UPDATE SET games=build_skill_levels.games+1, wins=build_skill_levels.wins+?""",
                            (champ, role, lvl, slot, win, win))
            # 코어 아이템 구매 순서: 앞 4개 (4코어 타임라인)
            if item_seq[pid]:
                seqstr = "-".join(item_seq[pid][:4])
                cur.execute("""INSERT INTO build_item_order (champ_en, role, seq, games, wins) VALUES (?,?,?,1,?)
                               ON CONFLICT(champ_en, role, seq) DO UPDATE SET games=build_item_order.games+1, wins=build_item_order.wins+?""",
                            (champ, role, seqstr, win, win))
            # 시작 아이템 세트
            if start_items[pid]:
                sset = "-".join(sorted(start_items[pid]))
                cur.execute("""INSERT INTO build_starts (champ_en, role, items, games, wins) VALUES (?,?,?,1,?)
                               ON CONFLICT(champ_en, role, items) DO UPDATE SET games=build_starts.games+1, wins=build_starts.wins+?""",
                            (champ, role, sset, win, win))

        cur.execute("INSERT INTO processed_timelines (match_id, processed_at) VALUES (?,?) ON CONFLICT(match_id) DO NOTHING",
                    (match_id, int(time.time())))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"타임라인 기록 에러 [{match_id}]: {e}")
        try: conn.close()  # 연결 누수 방지
        except Exception: pass
        return False

def get_stats_total_games():
    try:
        conn = db_connect()
        row = conn.execute("SELECT value FROM stats_meta WHERE key='total_games'").fetchone()
        conn.close()
        return int(row[0]) if row else 0
    except Exception:
        return 0

def get_real_stats_map():
    """수집된 실측 통계를 {champ_en: {role: {games, wins}}} + 밴 {champ_en: bans} 로 반환."""
    stats, bans = {}, {}
    try:
        conn = db_connect()
        for champ, role, games, wins in conn.execute("SELECT champ_en, role, games, wins FROM champion_stats"):
            stats.setdefault(champ, {})[role] = {"games": games, "wins": wins}
        for champ, b in conn.execute("SELECT champ_en, bans FROM champion_bans"):
            bans[champ] = b
        conn.close()
    except Exception as e:
        print(f"실측 통계 조회 에러: {e}")
    return stats, bans

BUILD_MIN_SAMPLE = 5  # 빌드를 표시할 최소 표본
# 레벨별 스킬트리는 표본 부족 + 규칙 미반영으로 신빙성 낮음 → 환경 갖춰질 때까지 표시 보류
SHOW_LEVEL_SKILL_TREE = False
COUNTER_MIN_GAMES = 10  # 카운터(맞대결)는 이 판수 이상일 때만 표시 (저표본 허수 방지)
# 실측 통계 표본 출처 표기 — /collect는 챌린저~플래티넘 수집 + 검색 유저(피기백) 합산
STATS_BASIS_LABEL = "플래티넘+ 솔로랭크 (KR)"

# ── 티어 지표 벤치마크 (개선 포인트 수치 비교용) ──
BENCH_MIN_SAMPLE = 30  # 벤치마크 평균을 신뢰할 최소 표본
TIER_KR = {"challenger": "챌린저", "grandmaster": "그랜드마스터", "master": "마스터",
           "diamond": "다이아", "emerald": "에메랄드", "platinum": "플래티넘",
           "gold": "골드", "silver": "실버", "bronze": "브론즈", "iron": "아이언"}
# 지표: 한글명, 단위, 높을수록 좋은지, 부족 시 조언(정형·정확한 가이드)
BENCHMARK_METRICS = {
    "cspm": {"kr": "분당 CS", "unit": "개", "higher": True,
             "advice": "라인 클리어·웨이브 관리를 신경 써 미니언을 놓치지 마세요. 분당 CS 격차가 골드 차이로 직결됩니다."},
    "ward": {"kr": "제어 와드 설치", "unit": "개", "higher": True,
             "advice": "첫 귀환 시 제어 와드를 구매하고, 주요 오브젝트 30초 전 길목 시야를 미리 장악하세요."},
    "vspm": {"kr": "분당 시야 점수", "unit": "", "higher": True,
             "advice": "와드를 아끼지 말고, 죽은 와드 정리(스윕)와 핵심 부쉬 시야 확보를 습관화하세요."},
    "kp":   {"kr": "킬 관여율", "unit": "%", "higher": True,
             "advice": "교전 신호를 미니맵으로 읽고 합류 타이밍을 앞당기세요. 한타 직전 포지션 선점이 관여율을 올립니다."},
    "dpm":  {"kr": "분당 챔피언 피해량", "unit": "", "higher": True,
             "advice": "교전에서 안전하게 더 오래 딜을 넣을 포지션을 잡고, 스킬 적중률을 높이세요."},
    "deaths": {"kr": "평균 데스", "unit": "회", "higher": False,
             "advice": "무리한 진입보다 포지셔닝을 우선하세요. 시야 없는 곳 진입과 1:1 과욕이 데스의 주원인입니다."},
}

def _extract_metrics(p, duration_min):
    """Match-v5 참가자 → 벤치마크 지표 딕셔너리."""
    ch = p.get('challenges', {}) or {}
    dm = max(1.0, duration_min)
    cs = p.get('totalMinionsKilled', 0) + p.get('neutralMinionsKilled', 0)
    k, d, a = p.get('kills', 0), p.get('deaths', 0), p.get('assists', 0)
    return {
        "cspm": round(cs / dm, 2),
        "ward": p.get('detectorWardsPlaced', ch.get('controlWardsPlaced', 0)),
        "vspm": round(p.get('visionScore', 0) / dm, 2),
        "kp":   round(ch.get('killParticipation', 0) * 100, 1),
        "dpm":  round(p.get('totalDamageDealtToChampions', 0) / dm, 1),
        "deaths": d,
    }

def record_tier_benchmark(metrics, tier, role):
    """티어×역할×지표 평균에 한 표본 누적. tier는 소문자 티어명."""
    if not tier or role not in VALID_ROLES:
        return
    try:
        conn = db_connect()
        for metric, val in metrics.items():
            conn.execute("""INSERT INTO tier_benchmark (tier, role, metric, total, cnt) VALUES (?,?,?,?,1)
                            ON CONFLICT(tier, role, metric) DO UPDATE SET
                              total=tier_benchmark.total+?, cnt=tier_benchmark.cnt+1""",
                         (tier, role, metric, val, val))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"벤치마크 적립 에러: {e}")
        try: conn.close()
        except Exception: pass

def get_tier_benchmarks(tier, role):
    """해당 티어·역할의 지표 평균 {metric: avg} (표본 충분한 것만)."""
    out = {}
    if not tier:
        return out
    try:
        conn = db_connect()
        for metric, total, cnt in conn.execute(
                "SELECT metric, total, cnt FROM tier_benchmark WHERE tier=? AND role=?", (tier, role)):
            if cnt >= BENCH_MIN_SAMPLE:
                out[metric] = round(total / cnt, 2)
        conn.close()
    except Exception as e:
        print(f"벤치마크 조회 에러: {e}")
    return out
# 아이템 패치 자동감지: 공식 패치노트와 일치 확인됨(예: 도란의 투구 방어/마저 10→8, 체력 140→150).
# 동일 이름 중복 id 중 '변경이 있는 = 현재 살아있는' 항목만 잡히므로 신뢰 가능.
RELIABLE_ITEM_DIFF = True

def get_champion_build(champ_en, preferred_role=None):
    """수집 데이터로 추천 빌드(룬/스펠/아이템/스킬/아이템순서)를 산출.
    표본이 부족하면 None 반환 → 프론트에서 '수집 중' 표시."""
    try:
        conn = db_connect()
        cur = conn.cursor()
        # 역할 결정: 표본이 가장 많은 역할 (선호 역할에 데이터 있으면 우선)
        roles = cur.execute("SELECT role, SUM(games) FROM champion_stats WHERE champ_en=? GROUP BY role ORDER BY SUM(games) DESC",
                            (champ_en,)).fetchall()
        if not roles:
            conn.close(); return None
        role = preferred_role if preferred_role and any(r == preferred_role for r, _ in roles) else roles[0][0]
        sample = next((g for r, g in roles if r == role), 0)
        if sample < BUILD_MIN_SAMPLE:
            conn.close(); return None

        build = {"role": role, "sample": sample}
        ALT_MIN = 5  # 고승률 변형 최소 표본 (허수 방지)
        # 고승률 정렬도 베이지안 보정 — 3판 100% 같은 허수 대신 안정적 상위 채택
        WR = f"((CAST(wins AS REAL)+{WR_PRIOR/2})/(games+{WR_PRIOR})) DESC, games DESC"

        # ── 인게임 풀 트리 룬 뷰 빌더 ──
        def rune_view(row):
            if not row:
                return None
            page, ps, ss, games, wins = row
            try:
                pp, sp, shp = page.split("|")
                pids = set(int(x) for x in pp.split(",") if x)
                sids = set(int(x) for x in sp.split(",") if x)
                shard_ids = [int(x) for x in shp.split(",") if x]
            except (ValueError, IndexError):
                return None
            def render(tree, sel, skip_key):
                if not tree:
                    return None
                rows = tree["slots"][1:] if skip_key else tree["slots"]
                return {"name": tree["name"], "icon": tree["icon"],
                        "slots": [[{**r, "selected": r["id"] in sel, "desc": RUNE_DESC.get(r["id"], "")} for r in row]
                                  for row in rows]}
            return {
                "primary": render(RUNE_TREES.get(ps), pids, False),
                "sub": render(RUNE_TREES.get(ss), sids, True),
                "shards": [{"icon": shard_icon(s), "name": SHARD_INFO.get(s, ("",))[0]} for s in shard_ids],
                "games": games, "wr": smooth_wr(wins, games),
            }
        def spell_view(row):
            if not row:
                return None
            spells = [{"icon": SPELL_MAP.get(s), "name": SPELL_NAME.get(SPELL_MAP.get(s), "")}
                      for s in row[0].split("-") if SPELL_MAP.get(s)]
            return {"list": spells, "games": row[1], "wr": smooth_wr(row[2], row[1])}
        def boots_view(row):
            if not row:
                return None
            return {"id": row[0], "name": ITEM_NAME.get(row[0], ""), "games": row[1],
                    "wr": smooth_wr(row[2], row[1])}
        def start_view(row):
            if not row:
                return None
            return {"list": [{"id": i, "name": ITEM_NAME.get(i, "")} for i in row[0].split("-")],
                    "games": row[1], "wr": smooth_wr(row[2], row[1])}

        def q(table, cols, order, min_g=0):
            return cur.execute(f"""SELECT {cols} FROM {table} WHERE champ_en=? AND role=? AND games>=?
                                   ORDER BY {order} LIMIT 1""", (champ_en, role, min_g)).fetchone()

        # 인기(판수) vs 고승률(표본 충족 중 최고 승률) — 2가지 셋업
        rune_pop = q("build_runepages", "page,primary_style,sub_style,games,wins", "games DESC")
        rune_hi  = q("build_runepages", "page,primary_style,sub_style,games,wins", WR, ALT_MIN)
        spell_pop = q("build_spells", "spells,games,wins", "games DESC")
        spell_hi  = q("build_spells", "spells,games,wins", WR, ALT_MIN)
        boots_pop = q("build_boots", "item_id,games,wins", "games DESC")
        boots_hi  = q("build_boots", "item_id,games,wins", WR, ALT_MIN)
        start_pop = q("build_starts", "items,games,wins", "games DESC")
        start_hi  = q("build_starts", "items,games,wins", WR, ALT_MIN)

        setup_pop = {"label": "인기 빌드", "sub": "가장 많이 채용",
                     "runes": rune_view(rune_pop), "spells": spell_view(spell_pop),
                     "boots": boots_view(boots_pop), "starts": start_view(start_pop)}
        setup_hi = {"label": "고승률 빌드", "sub": "승률 최상위",
                    "runes": rune_view(rune_hi), "spells": spell_view(spell_hi),
                    "boots": boots_view(boots_hi), "starts": start_view(start_hi)}
        # 2위 셋업이 1위와 의미있게 다른 경우에만 추가 노출
        has_alt = (rune_hi and rune_pop and rune_hi[0] != rune_pop[0]) or \
                  (spell_hi and spell_pop and spell_hi[0] != spell_pop[0]) or \
                  (boots_hi and boots_pop and boots_hi[0] != boots_pop[0]) or \
                  (start_hi and start_pop and start_hi[0] != start_pop[0])
        build["setups"] = [setup_pop, setup_hi] if has_alt else [setup_pop]

        # 코어 아이템 빈도 (상위 6개)
        items = cur.execute("""SELECT item_id, games, wins FROM build_items
                               WHERE champ_en=? AND role=? ORDER BY games DESC LIMIT 6""", (champ_en, role)).fetchall()
        build["items"] = [{"id": it[0], "name": ITEM_NAME.get(it[0], ""),
                           "wr": smooth_wr(it[2], it[1]), "games": it[1]} for it in items]
        # 상위 3개 코어 빌드 — 첫 코어별 그룹 → 위치별 최빈 아이템으로 4코어 경로 구성
        # (정확 시퀀스 집계는 표본 적을 때 짧은 빌드 편향 → 위치별 집계로 안정적 4코어 출력)
        all_seqs = cur.execute("""SELECT seq, games, wins FROM build_item_order
                                  WHERE champ_en=? AND role=?""", (champ_en, role)).fetchall()
        groups = {}  # 첫 코어 → {games, wins, pos:{i:{item:count}}}
        for seq, g, w in all_seqs:
            items = seq.split("-")[:4]
            if not items:
                continue
            grp = groups.setdefault(items[0], {"games": 0, "wins": 0, "pos": {}})
            grp["games"] += g; grp["wins"] += w
            for i, it in enumerate(items):
                grp["pos"].setdefault(i, {})
                grp["pos"][i][it] = grp["pos"][i].get(it, 0) + g
        top3 = sorted(groups.items(), key=lambda x: -x[1]["games"])[:3]
        build["top_builds"] = []
        for first, grp in top3:
            path = []
            for i in range(4):
                col = grp["pos"].get(i)
                if not col:
                    break
                best = max(col, key=col.get)
                path.append({"id": best, "name": ITEM_NAME.get(best, "")})
            build["top_builds"].append({
                "list": path, "games": grp["games"],
                "wr": smooth_wr(grp["wins"], grp["games"])})
        # 스킬 마스터 순서(우선순위)
        sk = cur.execute("""SELECT skill_order, games, wins FROM build_skills
                            WHERE champ_en=? AND role=? ORDER BY games DESC LIMIT 1""", (champ_en, role)).fetchone()
        if sk:
            build["skill_order"] = {"order": sk[0].split(">"), "games": sk[1], "wr": smooth_wr(sk[2], sk[1])}
        # 레벨별 스킬트리 (lol.ps 스타일) — 각 레벨에서 가장 많이 찍은 슬롯
        # ⏸️ 보류: 레벨별 표본이 적어 신빙성 부족 + 게임 규칙(R은 6/11/16만) 미반영으로
        #    불가능한 결과(R@18 등) 발생. Production Key로 표본 충분해지면 규칙 검증과 함께 재개.
        #    데이터 수집(build_skill_levels)은 계속 진행 → 재개 시 즉시 활용.
        if SHOW_LEVEL_SKILL_TREE:
            lvl_rows = cur.execute("""SELECT lvl, slot, games FROM build_skill_levels
                                      WHERE champ_en=? AND role=?""", (champ_en, role)).fetchall()
            if lvl_rows:
                best = {}  # lvl → (slot, games)
                for lvl, slot, games in lvl_rows:
                    if lvl not in best or games > best[lvl][1]:
                        best[lvl] = (slot, games)
                build["skill_levels"] = {lvl: SKILL_LETTER.get(best[lvl][0]) for lvl in best}
        conn.close()
        return build
    except Exception as e:
        print(f"빌드 산출 에러 [{champ_en}]: {e}")
        return None

def get_champion_counters(champ_en, role, min_games=COUNTER_MIN_GAMES):
    """라인 맞대결 승률 기반 카운터(취약)/유리 상대 산출. 저표본은 보류."""
    try:
        conn = db_connect()
        rows = conn.execute("""SELECT opponent, games, wins FROM build_matchups
                               WHERE champ_en=? AND role=? AND games>=? ORDER BY games DESC""",
                            (champ_en, role, min_games)).fetchall()
        conn.close()
        matchups = [{"id": r[0], "kr": CHAMP_KR_MAP.get(r[0], r[0]),
                     "games": r[1], "wr": smooth_wr(r[2], r[1])} for r in rows]
        if not matchups:
            return None
        weak = sorted(matchups, key=lambda x: x["wr"])[:3]            # 승률 낮은 = 취약
        strong = sorted(matchups, key=lambda x: -x["wr"])[:3]         # 승률 높은 = 유리
        return {"weak": [m for m in weak if m["wr"] < 50],
                "strong": [m for m in strong if m["wr"] >= 50]}
    except Exception as e:
        print(f"카운터 산출 에러 [{champ_en}]: {e}")
        return None

# 앱 시작 시 DB 초기화 실행
init_db()

# ═══════════════════════════════════════════════════════════════════════
#  ★ DDragon 데이터 + 자동 패치 업데이트 파이프라인 (요구사항 5)
# ═══════════════════════════════════════════════════════════════════════
# 전역 데이터 (load_ddragon으로 갱신) — 새 패치 시 자동 최신화
LATEST_VERSION = "14.12.1"; PREV_VERSION = None
CHAMP_KR_MAP, CHAMP_KEYS, CHAMP_TAGS = {}, {}, {}
SPELL_MAP, SPELL_NAME = {}, {}
RUNE_MAP, RUNE_NAME, RUNE_DESC, RUNE_TREES = {}, {}, {}, {}
ITEM_NAME, ITEM_DESC = {}, {}
CORE_ITEMS, BOOTS_ITEMS, START_ITEMS = set(), set(), set()
SHARD_INFO = {  # 스탯 샤드 (DDragon 미제공 → CommunityDragon 아이콘 + 한글명)
    5008: ("적응형 능력치", "statmodsadaptiveforceicon.png"),
    5005: ("공격 속도",     "statmodsattackspeedicon.png"),
    5007: ("스킬 가속",     "statmodscdrscalingicon.png"),
    5010: ("이동 속도",     "statmodsmovementspeedicon.png"),
    5011: ("체력",          "statmodshealthplusicon.png"),
    5013: ("강인함·둔화 저항","statmodstenacityicon.png"),
    5001: ("체력 비례 성장", "statmodshealthscalingicon.png"),
}
_ddragon_version_loaded = None
_last_patch_check = 0
PATCH_CHECK_INTERVAL = 3600  # 패치 확인 주기(초) — 요청 시 스로틀 체크

def _striptags(t):
    return re.sub(r'<[^>]+>', '', t or '').strip()

def official_patch(mm):
    """DDragon 데이터 버전(예: '16.13')을 Riot 공식 패치 표기('26.13')로 변환.
    2025년부터 공식 패치는 연도 기반(25.x/26.x)인데 DDragon CDN은 레거시(15.x/16.x)라 major +10.
    데이터/이미지 URL은 계속 LATEST_VERSION(16.x)을 쓰고, 사용자 표기에만 공식 번호를 사용."""
    try:
        major, minor = mm.split(".")[:2]
        return f"{int(major) + 10}.{minor}"
    except Exception:
        return mm

CURRENT_PATCH_DISPLAY = official_patch(CURRENT_PATCH)  # 사용자 표기용 공식 패치 번호

def load_ddragon():
    """DDragon 최신 데이터를 전부 (재)로드. 새 패치 감지 시 ensure_current_patch에서 재호출."""
    global LATEST_VERSION, PREV_VERSION, CURRENT_PATCH, CURRENT_PATCH_DISPLAY, CHAMP_KR_MAP, CHAMP_KEYS, CHAMP_TAGS
    global SPELL_MAP, SPELL_NAME, RUNE_MAP, RUNE_NAME, RUNE_DESC, RUNE_TREES
    global ITEM_NAME, ITEM_DESC, CORE_ITEMS, BOOTS_ITEMS, START_ITEMS, _ddragon_version_loaded
    try:
        ro = {"timeout": 5}
        versions = requests.get("https://ddragon.leagueoflegends.com/api/versions.json", **ro).json()
        latest = versions[0]
        cur_mm = ".".join(latest.split(".")[:2])
        LATEST_VERSION = latest
        PREV_VERSION = next((v for v in versions if ".".join(v.split(".")[:2]) != cur_mm), None)
        CURRENT_PATCH = cur_mm
        CURRENT_PATCH_DISPLAY = official_patch(cur_mm)
        base = f"https://ddragon.leagueoflegends.com/cdn/{latest}/data/ko_KR"

        champ_data = requests.get(f"{base}/champion.json", **ro).json()['data']
        CHAMP_KR_MAP = {v['id']: v['name'] for v in champ_data.values()}
        CHAMP_KEYS = {str(v['key']): v['id'] for v in champ_data.values()}
        CHAMP_TAGS = {v['id']: v.get('tags', []) for v in champ_data.values()}

        spell_data = requests.get(f"{base}/summoner.json", **ro).json()['data']
        SPELL_MAP = {str(v['key']): v['id'] for v in spell_data.values()}
        SPELL_NAME = {v['id']: v['name'] for v in spell_data.values()}

        # 룬 — 아이콘/이름/설명 + 전체 트리 구조(인게임 풀 트리용)
        rune_data = requests.get(f"{base}/runesReforged.json", **ro).json()
        rmap, rname, rdesc, rtrees = {}, {}, {}, {}
        for tree in rune_data:
            rmap[tree['id']] = tree['icon']; rname[tree['id']] = tree['name']
            slots = []
            for slot in tree['slots']:
                row = []
                for rune in slot['runes']:
                    rmap[rune['id']] = rune['icon']; rname[rune['id']] = rune['name']
                    rdesc[rune['id']] = _striptags(rune.get('shortDesc') or rune.get('longDesc') or '')
                    row.append({'id': rune['id'], 'icon': rune['icon'], 'name': rune['name']})
                slots.append(row)
            rtrees[tree['id']] = {'id': tree['id'], 'name': tree['name'], 'icon': tree['icon'], 'slots': slots}
        RUNE_MAP, RUNE_NAME, RUNE_DESC, RUNE_TREES = rmap, rname, rdesc, rtrees

        # 아이템 — 이름/설명 + 코어/신발/시작 판별
        item_data = requests.get(f"{base}/item.json", **ro).json()['data']
        iname, idesc, core, boots, starts = {}, {}, set(), set(), set()
        for iid, v in item_data.items():
            iname[iid] = v['name']
            idesc[iid] = _striptags(v.get('plaintext') or v.get('description') or '')
            gold = v.get('gold', {}); tags = v.get('tags', [])
            if not v.get('maps', {}).get('11'):
                continue
            if "Boots" in tags and gold.get('purchasable') and gold.get('total', 0) >= 600:
                boots.add(iid)
            elif (gold.get('purchasable') and gold.get('total', 0) >= 2000
                    and not v.get('into') and not v.get('requiredAlly')):
                core.add(iid)
            if gold.get('purchasable') and 0 < gold.get('total', 0) <= 500 and "Trinket" not in tags:
                starts.add(iid)
        ITEM_NAME, ITEM_DESC, CORE_ITEMS, BOOTS_ITEMS, START_ITEMS = iname, idesc, core, boots, starts

        _ddragon_version_loaded = latest
        print(f"DDragon 로드 완료: {latest} (패치 {cur_mm})")
        return True
    except Exception as e:
        print(f"DDragon 로드 실패: {e}")
        return False

load_ddragon()  # 앱 시작 시 초기 로드

def reset_stats_if_new_patch():
    """수집 통계가 이전 패치 것이면 초기화 (새 패치 메타 재수집)."""
    try:
        conn = db_connect()
        row = conn.execute("SELECT value FROM stats_meta WHERE key='collected_patch'").fetchone()
        if (row[0] if row else None) != CURRENT_PATCH:
            for t in ['champion_stats','champion_bans','processed_matches','build_runes','build_runepages',
                      'build_spells','build_items','build_boots','build_starts','build_skills',
                      'build_skill_levels','build_item_order','build_matchups','processed_timelines']:
                try: conn.execute(f"DELETE FROM {t}")
                except Exception: pass
            conn.execute("DELETE FROM stats_meta WHERE key='total_games'")
            conn.execute("""INSERT INTO stats_meta (key, value) VALUES ('collected_patch', ?)
                            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value""", (CURRENT_PATCH,))
            conn.commit()
            print(f"패치 변경 감지 → 빌드 통계 초기화 (현재 패치 {CURRENT_PATCH})")
        conn.close()
    except Exception as e:
        print(f"패치 통계 초기화 에러: {e}")

def ensure_current_patch():
    """요청 시 호출(스로틀). 새 패치 감지 시 DDragon 재로드 + 통계 초기화."""
    global _last_patch_check
    now = time.time()
    if now - _last_patch_check < PATCH_CHECK_INTERVAL:
        return
    _last_patch_check = now
    try:
        versions = requests.get("https://ddragon.leagueoflegends.com/api/versions.json", timeout=4).json()
        if versions and versions[0] != _ddragon_version_loaded:
            print(f"새 패치 감지: {_ddragon_version_loaded} → {versions[0]} · 데이터 자동 갱신")
            if load_ddragon():
                reset_stats_if_new_patch()
    except Exception as e:
        print(f"패치 확인 에러: {e}")

reset_stats_if_new_patch()  # 시작 시 패치 정합성 보장

def shard_icon(shard_id):
    info = SHARD_INFO.get(shard_id)
    return f"https://raw.communitydragon.org/latest/game/assets/perks/statmods/{info[1]}" if info else ""

ROLE_MAP = {"TOP": "탑", "JUNGLE": "정글", "MIDDLE": "미드", "BOTTOM": "원딜", "UTILITY": "서포터", "": "기타", "Invalid": "기타"}
ROLE_ICON = {"TOP": "🪓", "JUNGLE": "🌲", "MIDDLE": "🪄", "BOTTOM": "🏹", "UTILITY": "🛡️", "": "🎲", "Invalid": "🎲"}
ROLE_ORDER = {"TOP": 1, "JUNGLE": 2, "MIDDLE": 3, "BOTTOM": 4, "UTILITY": 5, "": 6, "Invalid": 6}
QUEUE_MAP = {420: "솔로랭크", 440: "자유랭크", 0: "기타 랭크"}

def get_champion_roster():
    roster = {
        "TOP": ["Aatrox", "Darius", "Camille", "Renekton", "Garen"],
        "JUNGLE": ["LeeSin", "Nidalee", "Viego", "XinZhao", "Khazix"],
        "MIDDLE": ["Ahri", "Sylas", "LeBlanc", "Yone", "Zed"],
        "BOTTOM": ["Jinx", "Ezreal", "Kaisa", "Lucian", "Ashe"],
        "UTILITY": ["Thresh", "Nautilus", "Lulu", "Karma", "Leona"]
    }
    clean_data = {}
    for lane, champs in roster.items():
        clean_data[lane] = [{'en': c, 'kr': CHAMP_KR_MAP.get(c, c)} for c in champs]
    return clean_data

GLOBAL_ROSTER_DATA = get_champion_roster()

# ✅ LOW 개선: 하드코딩 데이터이지만, 프로게이머 ID는 언제든 변경될 수 있음
# riot_id 변경 시 이 목록만 수정하면 전체 사이트에 적용됨
PRO_GAMERS = [
    # 탑
    {"team": "T1",  "name": "Zeus",     "riot_id": "우제는최고야", "tag": "KR1", "champ": "Jayce",    "role": "TOP"},
    {"team": "GEN", "name": "Kiin",     "riot_id": "Kiin",       "tag": "KR1", "champ": "Garen",    "role": "TOP"},
    # 정글
    {"team": "HLE", "name": "Peanut",   "riot_id": "Peanut",     "tag": "KR1", "champ": "Nidalee",  "role": "JUNGLE"},
    {"team": "GEN", "name": "Canyon",   "riot_id": "Canyon",     "tag": "KR1", "champ": "Viego",    "role": "JUNGLE"},
    # 미드
    {"team": "T1",  "name": "Faker",    "riot_id": "Hide on bush","tag": "KR1", "champ": "Azir",     "role": "MIDDLE"},
    {"team": "GEN", "name": "Chovy",    "riot_id": "지 수",       "tag": "KR1", "champ": "Yone",     "role": "MIDDLE"},
    {"team": "DK",  "name": "ShowMaker","riot_id": "MIDKING",    "tag": "KR1", "champ": "Syndra",   "role": "MIDDLE"},
    # 원딜
    {"team": "T1",  "name": "Gumayusi", "riot_id": "t1 gumayusi","tag": "KR1", "champ": "Jinx",     "role": "BOTTOM"},
    {"team": "GEN", "name": "Ruler",    "riot_id": "Ruler",      "tag": "KR1", "champ": "Kaisa",    "role": "BOTTOM"},
    # 서포터
    {"team": "T1",  "name": "Keria",    "riot_id": "역천괴",       "tag": "KR1", "champ": "Thresh",   "role": "UTILITY"},
    {"team": "GEN", "name": "Lehends",  "riot_id": "Lehends",    "tag": "KR1", "champ": "Nautilus", "role": "UTILITY"},
]

def calc_radar(stats, count):
    if count == 0: return [0,0,0,0,0,0]
    return [
        round(min(100, (stats['combat'] / count) * 6.5)),       
        round(min(100, (stats['growth'] / count) / 120)),       
        round(min(100, (stats['vision'] / count) * 2.5)),       
        round(min(100, (stats['survival'] / count) * 6)),       
        round(min(100, (stats['objectives'] / count) / 200)),   
        round(min(100, (stats['join'] / count) * 1.3))          
    ]

# ✅ LOW 개선: if/elif 체인 → dict lookup으로 리팩토링
# 새 태그 추가 시 해당 dict에만 항목 하나 추가하면 됨
TAG_TOP = {
    '전투':     {"icon": "⚔️",  "text": "피도 눈물도 없는 전투광",       "color": "#f43f5e", "bg": "rgba(244, 63, 94, 0.15)",   "border": "rgba(244, 63, 94, 0.4)"},
    '성장':     {"icon": "💰",  "text": "압도적 성장형 캐리",       "color": "#f59e0b", "bg": "rgba(245, 158, 11, 0.15)",  "border": "rgba(245, 158, 11, 0.4)"},
    '시야':     {"icon": "👁️", "text": "맵을 꿰뚫는 시야 장악",     "color": "#10b981", "bg": "rgba(16, 185, 129, 0.15)",  "border": "rgba(16, 185, 129, 0.4)"},
    '생존':     {"icon": "🛡️", "text": "강철의 불사신",           "color": "#3b82f6", "bg": "rgba(59, 130, 246, 0.15)",  "border": "rgba(59, 130, 246, 0.4)"},
    '오브젝트': {"icon": "🐉",  "text": "승리를 부르는 오브젝트 집착형", "color": "#8b5cf6", "bg": "rgba(139, 92, 246, 0.15)", "border": "rgba(139, 92, 246, 0.4)"},
    '합류':     {"icon": "🤝", "text": "홍길동급 특급 소방수",       "color": "#ec4899", "bg": "rgba(236, 72, 153, 0.15)",  "border": "rgba(236, 72, 153, 0.4)"},
}

TAG_WARN = {
    '시야':     {"icon": "🚨", "text": "갱킹 주의! 시야 점수 심각",  "color": "#ef4444", "bg": "rgba(239, 68, 68, 0.15)", "border": "rgba(239, 68, 68, 0.5)"},
    '생존':     {"icon": "🚨", "text": "데스가 너무 많습니다",      "color": "#ef4444", "bg": "rgba(239, 68, 68, 0.15)", "border": "rgba(239, 68, 68, 0.5)"},
    '합류':     {"icon": "🚨", "text": "팀원들이 고통받는 고립형",  "color": "#ef4444", "bg": "rgba(239, 68, 68, 0.15)", "border": "rgba(239, 68, 68, 0.5)"},
    '오브젝트': {"icon": "🚨", "text": "타워/용 딜량 부족",       "color": "#ef4444", "bg": "rgba(239, 68, 68, 0.15)", "border": "rgba(239, 68, 68, 0.5)"},
    '전투':     {"icon": "🚨", "text": "상대 딜량 교환이 불리함",    "color": "#ef4444", "bg": "rgba(239, 68, 68, 0.15)", "border": "rgba(239, 68, 68, 0.5)"},
    '성장':     {"icon": "🚨", "text": "CS 파밍이 부족합니다",       "color": "#ef4444", "bg": "rgba(239, 68, 68, 0.15)", "border": "rgba(239, 68, 68, 0.5)"},
}

TAG_BALANCE = {"icon": "⚖️", "text": "육각형 밸런스 플레이어", "color": "#94a3b8", "bg": "rgba(255, 255, 255, 0.1)", "border": "rgba(255, 255, 255, 0.2)"}

def generate_deep_tags(radar_array):
    if not radar_array or sum(radar_array) == 0: return []
    labels = ['전투', '성장', '시야', '생존', '오브젝트', '합류']
    scores = list(zip(labels, radar_array))
    scores.sort(key=lambda x: x[1], reverse=True)

    tags = []

    # 주요 지표: 상위 스탯으로 성향 태그 부여
    top_label = scores[0][0]
    if top_label in TAG_TOP:
        tags.append(TAG_TOP[top_label])

    # 경고 지표: 하위 스탯이 30 미만이면 취약점 태그 부여
    worst_label, worst_score = scores[-1]
    if worst_score < 30 and worst_label in TAG_WARN:
        tags.append(TAG_WARN[worst_label])

    # 이도저도 없으면 (또는 태그 1개인데 최저점이 40 이상이면) 밸런스 태그 부여
    if not tags or (len(tags) == 1 and worst_score >= 40):
        tags.append(TAG_BALANCE)

    return tags

def get_summoner_info(name, tag):
    return riot_get(f"https://asia.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{name}/{tag}")

def get_summoner_v4(puuid):
    res = riot_get(f"https://kr.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}")
    return res.json() if res.status_code == 200 else {}

def get_league_info_by_puuid(puuid):
    try:
        res = riot_get(f"https://kr.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}")
        if res.status_code == 200:
            data = res.json()
            for q in data:
                if q['queueType'] == 'RANKED_SOLO_5x5': return f"{q['tier']} {q['rank']} ({q['leaguePoints']} LP)", q['tier'].lower()
            if data: return f"{data[0]['tier']} {data[0]['rank']} ({data[0]['leaguePoints']} LP)", data[0]['tier'].lower()
    except Exception: pass
    return "Unranked", "unranked"

def get_live_game(puuid):
    res = riot_get(f"https://kr.api.riotgames.com/lol/spectator/v5/active-games/by-puuid/{puuid}")
    if res.status_code == 200: return {'isPlaying': True, 'minutes': max(res.json().get('gameLength', 0) // 60, 0)}
    return {'isPlaying': False}

def get_mastery(puuid):
    res = riot_get(f"https://kr.api.riotgames.com/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}/top?count=3")
    if res.status_code == 200:
        masteries = []
        for m in res.json():
            c_en = CHAMP_KEYS.get(str(m['championId']), "Unknown")
            pts = m['championPoints']
            pts_str = f"{round(pts/1000000, 1)}M" if pts >= 1000000 else f"{round(pts/1000)}k" if pts >= 1000 else str(pts)
            masteries.append({'champ_en': c_en, 'champ_kr': CHAMP_KR_MAP.get(c_en, c_en), 'level': m['championLevel'], 'points': pts_str})
        return masteries
    return []

def _strip_html(text):
    """DDragon 설명문의 HTML 태그 제거 (툴팁용)."""
    return re.sub(r'<[^>]+>', '', text or '').strip()

def get_champion_detail(champ_id):
    """DDragon에서 최신 패치 기준 챔피언 스킬(패시브 + QWER) 정보를 가져옴.
    패치 버전을 캐시 키에 포함 → 패치 변경 시 자동 갱신.
    v2: Riot 공식 아군팁/상대팁(allytips/enemytips) 포함."""
    cache_key = f"champdetail#v2#{champ_id}#{LATEST_VERSION}"
    cached_json, _ = db_read(cache_key)
    if cached_json:
        return json.loads(cached_json)
    try:
        url = f"https://ddragon.leagueoflegends.com/cdn/{LATEST_VERSION}/data/ko_KR/champion/{champ_id}.json"
        res = requests.get(url, timeout=5)
        if res.status_code != 200:
            return None
        d = res.json()['data'][champ_id]
        keys = ['Q', 'W', 'E', 'R']
        detail = {
            'title': d.get('title', ''),
            'lore': _strip_html(d.get('blurb', '')),
            'passive': {
                'name': d['passive']['name'],
                'img': f"https://ddragon.leagueoflegends.com/cdn/{LATEST_VERSION}/img/passive/{d['passive']['image']['full']}",
                'desc': _strip_html(d['passive'].get('description', '')),
            },
            'spells': [{
                'key': keys[i],
                'name': s['name'],
                'img': f"https://ddragon.leagueoflegends.com/cdn/{LATEST_VERSION}/img/spell/{s['image']['full']}",
                'desc': _strip_html(s.get('description', '')),
            } for i, s in enumerate(d['spells'][:4])],
            # Riot 공식 가이드 팁 (한글) — 빈 문자열 제거 후 최대 4개
            'allytips':  [t.strip() for t in d.get('allytips', [])  if t and t.strip()][:4],
            'enemytips': [t.strip() for t in d.get('enemytips', []) if t and t.strip()][:4],
        }
        db_write(cache_key, detail, int(time.time()))
        return detail
    except Exception as e:
        print(f"챔피언 상세 로드 에러 [{champ_id}]: {e}")
        return None

# 패치 변화 비교용 — 기본 스탯 한글명 + 버프 방향(값↑이 버프면 +1, 값↓이 버프면 -1)
STAT_KR = {
    "hp": "체력", "hpregen": "체력 재생", "mp": "마나", "mpregen": "마나 재생",
    "armor": "방어구", "spellblock": "마법 저항", "attackdamage": "공격력",
    "attackspeed": "공격 속도", "movespeed": "이동 속도", "attackrange": "사거리",
    "crit": "치명타", "hpperlevel": "레벨당 체력", "armorperlevel": "레벨당 방어구",
    "attackdamageperlevel": "레벨당 공격력",
}
STAT_BUFF_DIR = {k: 1 for k in STAT_KR}  # 대부분 값↑ = 버프

def get_champion_patch_changes(champ_en):
    """현재/직전 패치 DDragon 데이터를 비교해 버프/너프 자동 감지. 챔피언·패치 단위 캐시."""
    if not PREV_VERSION:
        return None
    cache_key = f"patchdiff#v3#{champ_en}#{LATEST_VERSION}"
    cached, _ = db_read(cache_key)
    if cached:
        return json.loads(cached)
    try:
        def fetch(ver):
            r = requests.get(f"https://ddragon.leagueoflegends.com/cdn/{ver}/data/ko_KR/champion/{champ_en}.json", timeout=5)
            return r.json()['data'][champ_en] if r.status_code == 200 else None
        cur_d, prev_d = fetch(LATEST_VERSION), fetch(PREV_VERSION)
        changes = []
        if cur_d and prev_d:
            res = _resource_label(cur_d.get('partype'))  # 마나/기력/분노/체력…
            # 기본 스탯 비교
            cs, ps = cur_d.get('stats', {}), prev_d.get('stats', {})
            for key, kr in STAT_KR.items():
                cv, pv = cs.get(key), ps.get(key)
                if cv is None or pv is None or abs(cv - pv) < 1e-6:
                    continue
                up = cv > pv
                is_buff = up if STAT_BUFF_DIR.get(key, 1) == 1 else not up
                changes.append({"type": "buff" if is_buff else "nerf",
                                "text": f"{kr} {round(pv,1)} → {round(cv,1)}"})
            # 스킬 비교 (쿨다운/마나코스트는 라벨 명확)
            cspells = {s['id']: s for s in cur_d.get('spells', [])}
            pspells = {s['id']: s for s in prev_d.get('spells', [])}
            keys = ['Q', 'W', 'E', 'R']
            for idx, sp in enumerate(cur_d.get('spells', [])):
                slot = keys[idx] if idx < 4 else '?'
                ps_sp = pspells.get(sp['id'])
                if not ps_sp:
                    continue
                # 쿨타임
                cc, pc = sp.get('cooldownBurn'), ps_sp.get('cooldownBurn')
                if cc and pc and cc != pc:
                    changes.append({"type": "buff" if _first_num(cc) < _first_num(pc) else "nerf",
                                    "text": f"{slot} 쿨타임 {pc} → {cc}초"})
                # 소모 자원 (마나/체력 등 명시)
                ck, pk = sp.get('costBurn'), ps_sp.get('costBurn')
                if ck and pk and ck != pk and ck not in ('0', 'No Cost'):
                    changes.append({"type": "buff" if _first_num(ck) < _first_num(pk) else "nerf",
                                    "text": f"{slot} {res} 소모량 {pk} → {ck}"})
                # 효과 수치(데미지 등) 변경 — 방향 단정 어려워 중립 표기
                if sp.get('effectBurn') != ps_sp.get('effectBurn'):
                    changes.append({"type": "adjust", "text": f"{slot} 스킬 효과 수치 변경"})
            # 패시브 설명 변경
            if cur_d.get('passive', {}).get('description') != prev_d.get('passive', {}).get('description'):
                changes.append({"type": "adjust", "text": "패시브 변경"})
        result = {"changes": changes, "prev_patch": official_patch(".".join(PREV_VERSION.split(".")[:2]))}
        db_write(cache_key, result, int(time.time()))
        return result
    except Exception as e:
        print(f"패치 비교 에러 [{champ_en}]: {e}")
        return None

def _first_num(burn):
    """'60/90/120' 또는 '12/11/10/9/8' 같은 burn 문자열의 첫 숫자."""
    try:
        return float(str(burn).split("/")[0])
    except (ValueError, IndexError):
        return 0.0

# 아이템 패치 비교용 — 주요(정수형) 스탯 한글명
ITEM_STAT_KR = {
    "FlatHPPoolMod": "체력", "FlatMPPoolMod": "마나", "FlatArmorMod": "방어력",
    "FlatSpellBlockMod": "마법저항", "FlatPhysicalDamageMod": "공격력",
    "FlatMagicDamageMod": "주문력", "FlatHPRegenMod": "체력재생",
}
def _resource_label(partype):
    """챔피언 자원(partype) → 소모 자원 표기(마나/기력/분노/체력…). 자원 없으면 체력으로 간주."""
    p = (partype or "").strip()
    return p if p and p != "없음" else "체력"
def _numfmt(v):
    return str(int(v)) if float(v).is_integer() else str(round(v, 1))

def get_patch_highlights(limit=8):
    """championFull.json + item.json 비교로 챔피언·아이템 패치 버프/너프 일괄 감지.
    DDragon 공식 데이터 기반 자동 감지(쿨타임/소모값/스탯/가격). 패치 단위 캐시."""
    if not PREV_VERSION:
        return None
    cache_key = f"patchhighlights#v4#{LATEST_VERSION}"
    cached, _ = db_read(cache_key)
    if cached:
        return json.loads(cached)
    try:
        def fetch_json(ver, file):
            r = requests.get(f"https://ddragon.leagueoflegends.com/cdn/{ver}/data/ko_KR/{file}", timeout=10)
            return r.json().get('data', {}) if r.status_code == 200 else {}
        cur_all, prev_all = fetch_json(LATEST_VERSION, "championFull.json"), fetch_json(PREV_VERSION, "championFull.json")
        if not cur_all or not prev_all:
            return None
        scored = []
        slot_keys = ['Q', 'W', 'E', 'R']
        for cid, cur_d in cur_all.items():
            prev_d = prev_all.get(cid)
            if not prev_d:
                continue
            res = _resource_label(cur_d.get('partype'))  # 마나/기력/분노/체력…
            buffs = nerfs = 0
            details = []
            # 기본 스탯
            cs, ps = cur_d.get('stats', {}), prev_d.get('stats', {})
            for key, kr in STAT_KR.items():
                cv, pv = cs.get(key), ps.get(key)
                if cv is None or pv is None or abs(cv - pv) < 1e-6:
                    continue
                is_buff = (cv > pv) if STAT_BUFF_DIR.get(key, 1) == 1 else (cv < pv)
                buffs, nerfs = (buffs + 1, nerfs) if is_buff else (buffs, nerfs + 1)
                details.append({"type": "buff" if is_buff else "nerf", "text": f"{kr} {round(pv,1)}→{round(cv,1)}"})
            # 스킬 쿨다운·소모값
            pspells = {s['id']: s for s in prev_d.get('spells', [])}
            for idx, sp in enumerate(cur_d.get('spells', [])):
                slot = slot_keys[idx] if idx < 4 else '?'
                ps_sp = pspells.get(sp['id'])
                if not ps_sp:
                    continue
                cc, pc = sp.get('cooldownBurn'), ps_sp.get('cooldownBurn')
                if cc and pc and cc != pc:
                    cd_buff = _first_num(cc) < _first_num(pc)
                    buffs, nerfs = (buffs + 1, nerfs) if cd_buff else (buffs, nerfs + 1)
                    details.append({"type": "buff" if cd_buff else "nerf", "text": f"{slot} 쿨타임 {pc}→{cc}초"})
                ck, pk = sp.get('costBurn'), ps_sp.get('costBurn')
                if ck and pk and ck != pk and ck not in ('0', 'No Cost'):
                    cost_buff = _first_num(ck) < _first_num(pk)
                    buffs, nerfs = (buffs + 1, nerfs) if cost_buff else (buffs, nerfs + 1)
                    details.append({"type": "buff" if cost_buff else "nerf", "text": f"{slot} {res} 소모량 {pk}→{ck}"})
            if buffs == 0 and nerfs == 0:
                continue
            net = buffs - nerfs
            kind = "buff" if net > 0 else ("nerf" if net < 0 else "adjust")
            scored.append({"id": cid, "kr": CHAMP_KR_MAP.get(cid, cid),
                           "kind": kind, "net": net, "details": details[:3]})
        buffed = sorted([s for s in scored if s["kind"] == "buff"], key=lambda x: -x["net"])[:limit]
        nerfed = sorted([s for s in scored if s["kind"] == "nerf"], key=lambda x:  x["net"])[:limit]

        # ── 아이템 변경 감지 (주요 스탯·가격) — 협곡 구매 가능 아이템만 ──
        # ⏸️ 보류: DDragon 아이템 stats는 모드 변형(동일 이름 중복 id)·레거시 필드로
        #    거짓 변경(예: 도란의 투구 미변경인데 변형 id가 바뀐 것처럼 표기)을 만들어 신빙성 낮음.
        item_scored, seen_names = [], set()
        cur_it = fetch_json(LATEST_VERSION, "item.json") if RELIABLE_ITEM_DIFF else {}
        prev_it = fetch_json(PREV_VERSION, "item.json") if RELIABLE_ITEM_DIFF else {}
        for iid, cit in cur_it.items():
            pit = prev_it.get(iid)
            if not pit or not cit.get('maps', {}).get('11') or not cit.get('gold', {}).get('purchasable'):
                continue
            name = cit.get('name', '')
            if not name or name in seen_names:
                continue
            ib = inn = 0
            idetails = []
            cstat, pstat = cit.get('stats', {}), pit.get('stats', {})
            for key, kr in ITEM_STAT_KR.items():
                cv, pv = cstat.get(key, 0), pstat.get(key, 0)
                if abs(cv - pv) < 1e-6:
                    continue
                up = cv > pv
                ib, inn = (ib + 1, inn) if up else (ib, inn + 1)
                idetails.append({"type": "buff" if up else "nerf", "text": f"{kr} {_numfmt(pv)}→{_numfmt(cv)}"})
            cg, pg = cit.get('gold', {}).get('total', 0), pit.get('gold', {}).get('total', 0)
            if cg != pg and pg > 0 and cg > 0:
                cheaper = cg < pg
                ib, inn = (ib + 1, inn) if cheaper else (ib, inn + 1)
                idetails.append({"type": "buff" if cheaper else "nerf", "text": f"가격 {pg}→{cg} G"})
            if ib == 0 and inn == 0:
                continue
            seen_names.add(name)
            inet = ib - inn
            ikind = "buff" if inet > 0 else ("nerf" if inet < 0 else "adjust")
            item_scored.append({"id": iid, "name": name,
                                "icon": f"https://ddragon.leagueoflegends.com/cdn/{LATEST_VERSION}/img/item/{iid}.png",
                                "kind": ikind, "net": inet, "details": idetails[:3]})
        items_buffed = sorted([s for s in item_scored if s["kind"] == "buff"], key=lambda x: -x["net"])[:limit]
        items_nerfed = sorted([s for s in item_scored if s["kind"] == "nerf"], key=lambda x:  x["net"])[:limit]

        result = {"buffed": buffed, "nerfed": nerfed,
                  "items_buffed": items_buffed, "items_nerfed": items_nerfed,
                  "cur_patch": official_patch(".".join(LATEST_VERSION.split(".")[:2])),
                  "prev_patch": official_patch(".".join(PREV_VERSION.split(".")[:2])),
                  "total_changed": len(scored), "item_changed": len(item_scored)}
        db_write(cache_key, result, int(time.time()))
        return result
    except Exception as e:
        print(f"패치 하이라이트 에러: {e}")
        return None

# ── 공식 패치노트 파서 (leagueoflegends.com 원문 → 구조화) ──
PATCH_UA = {'User-Agent': 'Mozilla/5.0'}

def _pn_strip(s):
    return html.unescape(re.sub(r'<[^>]+>', '', s)).strip()

def _pn_section(body, titles):
    """h2 제목이 titles 중 하나인 섹션 HTML(다음 h2 전까지) 반환."""
    h2s = list(re.finditer(r'<h2[^>]*>(.*?)</h2>', body, re.S))
    for i, m in enumerate(h2s):
        if _pn_strip(m.group(1)) in titles:
            return body[m.end():(h2s[i + 1].start() if i + 1 < len(h2s) else len(body))]
    return ''

def _pn_entries(section, kr_to_id):
    """h3 단위로 챔피언/아이템 항목 파싱 → [{name,id,kr,summary,groups:[{name,changes}]}]."""
    entries = []
    for b in re.split(r'(?=<h3)', section):
        h3m = re.search(r'<h3[^>]*>(.*?)</h3>', b, re.S)
        if not h3m:
            continue
        head = h3m.group(1)
        name = _pn_strip(head)
        if not name:
            continue
        cid = kr_to_id.get(name)  # 한글명 → DDragon id (챔피언일 때)
        bq = re.search(r'<blockquote[^>]*>(.*?)</blockquote>', b, re.S)
        summary = _pn_strip(bq.group(1)) if bq else ''
        groups = []
        first_h4 = b.find('<h4')
        head_part = b[:first_h4] if first_h4 != -1 else b
        gen = [_pn_strip(x) for x in re.findall(r'<li[^>]*>(.*?)</li>', head_part, re.S) if _pn_strip(x)]
        if gen:
            groups.append({"name": "", "changes": gen})
        for am in re.finditer(r'<h4[^>]*>(.*?)</h4>(.*?)(?=<h4|$)', b, re.S):
            aname = _pn_strip(am.group(1))
            lis = [_pn_strip(x) for x in re.findall(r'<li[^>]*>(.*?)</li>', am.group(2), re.S) if _pn_strip(x)]
            if lis:
                groups.append({"name": aname, "changes": lis})
        if groups or summary:
            entries.append({"name": name, "id": cid, "summary": summary, "groups": groups})
    return entries

def _discover_patch_url():
    """현재 패치의 공식 패치노트 기사 URL 탐색 (슬러그 형식 변화 대응)."""
    maj, minor = (CURRENT_PATCH_DISPLAY.split(".") + ["", ""])[:2]
    frag = f"patch-{maj}-{minor}-notes"
    try:
        r = requests.get("https://www.leagueoflegends.com/ko-kr/news/tags/patch-notes/", headers=PATCH_UA, timeout=10)
        if r.status_code == 200:
            m = re.search(r'href="(/ko-kr/news/game-updates/[a-z0-9\-]*' + re.escape(frag) + r'/?)"', r.text)
            if m:
                return "https://www.leagueoflegends.com" + m.group(1)
    except Exception:
        pass
    return f"https://www.leagueoflegends.com/ko-kr/news/game-updates/league-of-legends-{frag}"

def fetch_official_patch_notes():
    """공식 패치노트 원문을 파싱해 챔피언·아이템 변경 전체 반환. 패치 단위 캐시.
    실패 시 None → 호출부에서 DDragon 자동감지로 폴백."""
    cache_key = f"officialpatch#v2#{LATEST_VERSION}"
    cached, _ = db_read(cache_key)
    if cached:
        return json.loads(cached)
    try:
        url = _discover_patch_url()
        r = requests.get(url, headers=PATCH_UA, timeout=15)
        if r.status_code != 200:
            return None
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text, re.S)
        if not m:
            return None
        blades = json.loads(m.group(1)).get('props', {}).get('pageProps', {}).get('page', {}).get('blades', [])
        body = max((bl.get('richText', {}).get('body', '') for bl in blades if isinstance(bl.get('richText', {}).get('body', ''), str)),
                   key=len, default='')
        if not body:
            return None
        kr_to_id = {kr: cid for cid, kr in CHAMP_KR_MAP.items()}
        champions = _pn_entries(_pn_section(body, ['챔피언']), kr_to_id)
        items = _pn_entries(_pn_section(body, ['아이템']), {})
        if not champions and not items:
            return None  # 구조 변경 등 → 폴백
        result = {"patch": CURRENT_PATCH_DISPLAY, "champions": champions, "item_list": items, "source_url": url}
        db_write(cache_key, result, int(time.time()))
        return result
    except Exception as e:
        print(f"공식 패치노트 파싱 에러: {e}")
        return None

def get_match_details(puuid, start=0, count=20, queue=None, collect_stats=True, player_tier=None):
    url = f"https://asia.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?start={start}&count={count}"
    url += f"&queue={queue}" if queue and queue != 'all' else "&type=ranked"
        
    response = riot_get(url)
    if response.status_code != 200: return [], None, None, None, 0, [], {}, [], puuid, 0, [], 0, 0, "Teemo", []

    match_ids = response.json()
    matches, role_stats = [], {}
    overall_stats = {"combat": 0, "growth": 0, "vision": 0, "survival": 0, "objectives": 0, "join": 0}
    total_k, total_d, total_a, total_vision, total_kp, win_count, lose_count = 0, 0, 0, 0, 0, 0, 0
    recent_champ_stats = {}
    coplayer_tracker = {}  # ★ 과거 매칭 추적: 최근 N판 동반 플레이어 누적
    team_luck_scores = []  # ★ 팀운: 게임별 (내 팀원 평균 - 적팀 평균) op_score 차이

    for m_id in match_ids:
        try:  # ✅ 버그 수정 3: 매치 1개 실패해도 나머지 계속 처리
            raw = riot_get(f"https://asia.api.riotgames.com/lol/match/v5/matches/{m_id}")
            if raw.status_code != 200:
                print(f"매치 {m_id} 조회 실패: {raw.status_code}")
                continue
            m_res = raw.json()
            # ✅ 버그 수정 2: gameDuration은 항상 초 단위 → 단순하게 /60
            duration_m = max(1, m_res['info']['gameDuration'] / 60.0)
            queue_id = m_res['info'].get('queueId', 0)

            if queue_id not in [420, 440]:
                continue
            game_type = QUEUE_MAP.get(queue_id, "기타 랭크")

            # ★ 피기백 통계 수집: 이미 가져온 매치를 통계 DB에 누적 (추가 API 호출 없음)
            # 페이지네이션(더보기)에서는 생략 → 느린 무료 서버에서 타임아웃(502) 방지
            if collect_stats:
                record_match_stats(m_res, m_id)

            main_player_data, participants_details, max_damage, max_cs, max_cspm = None, [], 0, 0, 0

            for p in m_res['info']['participants']:
                dmg = p.get('totalDamageDealtToChampions', 0)
                cs = p.get('totalMinionsKilled', 0) + p.get('neutralMinionsKilled', 0)
                cspm = round(cs / duration_m, 1)

                if dmg > max_damage: max_damage = dmg
                if cs > max_cs: max_cs = cs
                if cspm > max_cspm: max_cspm = cspm

                dmg_str = f"{dmg/1000:.1f}K" if dmg >= 1000 else str(dmg)

                k, d, a = p['kills'], p['deaths'], p['assists']
                op_score = (k * 2) + (a * 1.5) - (d * 1.5) + (dmg / 400) + (cs * 0.2) + (p.get('visionScore', 0) * 0.5)

                champ_en = champ_ddragon_id(p['championName'])
                spell1 = SPELL_MAP.get(str(p.get('summoner1Id', '')), "SummonerFlash")
                spell2 = SPELL_MAP.get(str(p.get('summoner2Id', '')), "SummonerDot")

                try:
                    rune1 = RUNE_MAP.get(p['perks']['styles'][0]['selections'][0]['perk'], "")
                    rune2 = RUNE_MAP.get(p['perks']['styles'][1]['style'], "")
                except KeyError: rune1, rune2 = "", ""

                items = [p.get(f'item{i}', 0) for i in range(7)]

                participants_details.append({
                    'puuid': p['puuid'], 'name': p.get('riotIdGameName', 'Unknown'),
                    'tag': p.get('riotIdTagline', ''), 'champ_img': champ_en,
                    'champ_kr': CHAMP_KR_MAP.get(champ_en, champ_en),
                    'teamId': p['teamId'], 'win': p['win'], 'role_en': p.get('teamPosition', ''),
                    'k': k, 'd': d, 'a': a, 'dmg': dmg, 'dmg_str': dmg_str, 'cs': cs, 'cspm': cspm,
                    'kda': round((k + a) / max(1, d), 2),
                    'kp': round(p.get('challenges', {}).get('killParticipation', 0) * 100),
                    'score': op_score, 'spell1': spell1, 'spell2': spell2, 'rune1': rune1, 'rune2': rune2, 'item_list': items
                })

                if p['puuid'] == puuid:
                    main_player_data = participants_details[-1].copy()
                    main_player_data['championName_kr'] = CHAMP_KR_MAP.get(champ_en, champ_en)
                    main_player_data['game_type'] = game_type
                    main_player_data['cs_per_min'] = round(cs / duration_m, 1)
                    main_player_data['kda_ratio'] = round((k + a) / max(1, d), 2)
                    kp = p.get('challenges', {}).get('killParticipation', 0) * 100
                    main_player_data['kp'] = round(kp)
                    # ★ 개선 포인트용 상세 지표 + 티어 벤치마크 적립(피기백, 검색 유저)
                    _pm = _extract_metrics(p, duration_m)
                    main_player_data['metrics'] = _pm
                    if collect_stats and player_tier and p.get('teamPosition') in VALID_ROLES and duration_m >= 5:
                        record_tier_benchmark(_pm, player_tier, p['teamPosition'])
                    grade = calc_game_grade(main_player_data['kda_ratio'], round(kp), main_player_data['cs_per_min'], p['win'])
                    main_player_data['grade'] = grade
                    main_player_data['grade_style'] = GRADE_STYLE.get(grade, GRADE_STYLE['C'])

                    total_k += k; total_d += d; total_a += a; total_vision += p['visionScore']
                    total_kp += kp

                    if p['win']: win_count += 1
                    else: lose_count += 1

                    overall_stats['combat'] += (k + a); overall_stats['growth'] += p.get('goldEarned', 0)
                    overall_stats['vision'] += p.get('visionScore', 0)
                    # ✅ MEDIUM 개선: deaths가 20 초과 시 survival이 음수가 되는 버그 방지
                    overall_stats['survival'] += max(0, 20 - d)
                    overall_stats['objectives'] += p.get('damageDealtToObjectives', 0); overall_stats['join'] += kp

                    role = p.get('teamPosition', '')
                    if role not in role_stats: role_stats[role] = {'count':0, 'combat':0, 'growth':0, 'vision':0, 'survival':0, 'objectives':0, 'join':0}
                    rs = role_stats[role]
                    rs['count'] += 1; rs['combat'] += (k + a); rs['growth'] += p.get('goldEarned', 0)
                    rs['vision'] += p.get('visionScore', 0)
                    rs['survival'] += max(0, 20 - d)  # ✅ MEDIUM 개선: 음수 방지
                    rs['objectives'] += p.get('damageDealtToObjectives', 0); rs['join'] += kp

                    if champ_en not in recent_champ_stats:
                        recent_champ_stats[champ_en] = {'count': 0, 'win': 0, 'k': 0, 'd': 0, 'a': 0,
                            'name_kr': main_player_data['championName_kr'],
                            'cs': 0, 'vision': 0, 'kp': 0, 'duration_m': 0, 'objectives': 0, 'dmg': 0}
                    rcs = recent_champ_stats[champ_en]
                    rcs['count'] += 1; rcs['k'] += k; rcs['d'] += d; rcs['a'] += a
                    rcs['cs'] += cs; rcs['vision'] += p.get('visionScore', 0)
                    rcs['kp'] += kp; rcs['duration_m'] += duration_m
                    rcs['objectives'] += p.get('damageDealtToObjectives', 0)
                    rcs['dmg'] += dmg
                    if p['win']: rcs['win'] += 1

            if main_player_data is None:
                continue  # 이 매치에 검색한 소환사가 없으면 스킵

            participants_details.sort(key=lambda x: x['score'], reverse=True)
            win_mvp, lose_ace = False, False

            for i, p in enumerate(participants_details):
                p['dmg_percent'] = round((p['dmg'] / max_damage) * 100) if max_damage else 0
                p['cspm_percent'] = round((p['cspm'] / max_cspm) * 100) if max_cspm else 0
                p['analysis_tag'] = calc_player_tag(p)

                if p['win'] and not win_mvp: p['badge'], p['badge_class'] = "MVP", "badge-mvp"; win_mvp = True
                elif not p['win'] and not lose_ace: p['badge'], p['badge_class'] = "ACE", "badge-ace"; lose_ace = True
                else: p['badge'], p['badge_class'] = f"{i+1}등", "badge-normal"
                if p['puuid'] == puuid: main_player_data['main_badge'], main_player_data['main_badge_class'] = p['badge'], p['badge_class']

            main_player_data['blue_team'] = sorted([p for p in participants_details if p['teamId'] == 100], key=lambda x: ROLE_ORDER.get(x['role_en'], 6))
            main_player_data['red_team'] = sorted([p for p in participants_details if p['teamId'] == 200], key=lambda x: ROLE_ORDER.get(x['role_en'], 6))

            teammates = [t for t in participants_details if t['teamId'] == main_player_data['teamId'] and t['puuid'] != puuid]
            # ★ 팀운(이 판): 내 팀원(나 제외) 평균 vs 적팀 평균 op_score → 팀원이 잘했는지
            enemies = [t for t in participants_details if t['teamId'] != main_player_data['teamId']]
            if teammates and enemies:
                tl = (sum(t['score'] for t in teammates) / len(teammates)) - (sum(e['score'] for e in enemies) / len(enemies))
                main_player_data['team_luck_score'] = round(tl, 1)
                team_luck_scores.append(tl)
            if teammates:
                score_diff = main_player_data['score'] - (sum(t['score'] for t in teammates) / len(teammates))
                if score_diff > 12 and not main_player_data['win']: main_player_data['team_luck'], main_player_data['team_luck_class'] = "억까 😭", "luck-bad"
                elif score_diff < -8 and main_player_data['win']: main_player_data['team_luck'], main_player_data['team_luck_class'] = "버스 🚌", "luck-good"
                elif score_diff > 12 and main_player_data['win']: main_player_data['team_luck'], main_player_data['team_luck_class'] = "캐리 🔥", "luck-carry"
                else: main_player_data['team_luck'], main_player_data['team_luck_class'] = "1인분 ⚖️", "luck-normal"
            else: main_player_data['team_luck'], main_player_data['team_luck_class'] = "보통", "luck-normal"

            if main_player_data['d'] >= 10: main_player_data['feedback'] = "🚨 데스 억제 요망"
            elif main_player_data['kp'] < 30: main_player_data['feedback'] = "🎯 교전 합류 필요"
            elif main_player_data['kda_ratio'] >= 4: main_player_data['feedback'] = "🔥 폼 미쳤다!"
            else: main_player_data['feedback'] = "👍 무난한 플레이"

            # ★ 상황 대응형 아이템 빌드 분석 (상대 조합 기반)
            enemy_team_id = 200 if main_player_data['teamId'] == 100 else 100
            enemy_champs = [{'champ_en': p['champ_img'],
                             'kr': CHAMP_KR_MAP.get(p['champ_img'], p['champ_img'])}
                            for p in participants_details if p['teamId'] == enemy_team_id]
            comp = analyze_enemy_comp(enemy_champs)
            main_player_data['enemy_comp'] = comp
            main_player_data['situational_build'] = recommend_situational_build(
                main_player_data['champ_img'], main_player_data.get('role_en', ''), comp)

            # ★ 과거 매칭 추적: 이 판의 동반 플레이어를 누적 (본인 제외)
            my_team_id = main_player_data['teamId']
            for cp in participants_details:
                if cp['puuid'] == puuid:
                    continue
                key = cp['puuid']
                if key not in coplayer_tracker:
                    coplayer_tracker[key] = {'name': cp['name'], 'tag': cp['tag'],
                                             'champ_img': cp['champ_img'], 'champ_kr': cp['champ_kr'],
                                             'count': 0, 'as_ally': 0, 'as_enemy': 0, 'as_ally_win': 0}
                ct = coplayer_tracker[key]
                ct['count'] += 1
                ct['name'], ct['tag'] = cp['name'], cp['tag']  # 최신 닉네임 갱신
                ct['champ_img'], ct['champ_kr'] = cp['champ_img'], cp['champ_kr']
                if cp['teamId'] == my_team_id:
                    ct['as_ally'] += 1
                    if main_player_data.get('win'):
                        ct['as_ally_win'] += 1
                else:
                    ct['as_enemy'] += 1

            matches.append(main_player_data)

        except Exception as e:
            print(f"매치 {m_id} 처리 중 에러 (건너뜀): {e}")
            continue

            
    game_count = len(matches) if matches else 1
    overall_kda = {"k": round(total_k/game_count, 1), "d": round(total_d/game_count, 1), "a": round(total_a/game_count, 1), "ratio": round((total_k + total_a) / max(1, total_d), 2)}
    
    recent_champs_list = [{'name': s['name_kr'], 'img': c, 'count': s['count'],
                            'win_rate': round((s['win']/s['count'])*100),
                            'kda': round((s['k']+s['a'])/max(1, s['d']), 2),
                            'analysis': analyze_champion_playstyle(s)}
                           for c, s in recent_champ_stats.items()]
    recent_champs_list.sort(key=lambda x: (-x['count'], -x['win_rate']))
    top_recent_champs = recent_champs_list[:3]
    
    banner_champ = top_recent_champs[0]['img'] if top_recent_champs else "Teemo"

    overall_radar = calc_radar(overall_stats, game_count)
    deep_tags = generate_deep_tags(overall_radar)

    sorted_roles = sorted(role_stats.items(), key=lambda x: x[1]['count'], reverse=True)
    primary_role, secondary_role = None, None

    if len(sorted_roles) > 0: primary_role = {'name': ROLE_MAP.get(sorted_roles[0][0], "기타"), 'icon': ROLE_ICON.get(sorted_roles[0][0], "🎲"), 'radar': calc_radar(sorted_roles[0][1], sorted_roles[0][1]['count']), 'count': sorted_roles[0][1]['count']}
    if len(sorted_roles) > 1: secondary_role = {'name': ROLE_MAP.get(sorted_roles[1][0], "기타"), 'icon': ROLE_ICON.get(sorted_roles[1][0], "🎲"), 'radar': calc_radar(sorted_roles[1][1], sorted_roles[1][1]['count']), 'count': sorted_roles[1][1]['count']}

    win_rate = round((win_count/game_count)*100) if matches else 0
    most = Counter([m['champ_img'] for m in matches]).most_common(3)

    # ★ 과거 매칭 추적: 2회 이상 마주친 동반 플레이어 추출 (자주 만난 순)
    repeat_encounters = []
    for info in coplayer_tracker.values():
        if info['count'] >= 2:
            if info['as_ally'] >= info['as_enemy']:
                rel_label, rel_color = "주로 아군", "#60a5fa"
            else:
                rel_label, rel_color = "주로 상대", "#f87171"
            repeat_encounters.append({**info, 'rel_label': rel_label, 'rel_color': rel_color})
    repeat_encounters.sort(key=lambda x: -x['count'])
    repeat_encounters = repeat_encounters[:8]

    # ★ 최근 7게임 평균 팀운 지표 (팀원 vs 적팀 성과 기반 추정 — 재미용 지표)
    recent_team_luck = None
    if team_luck_scores:
        recent = team_luck_scores[:7]
        avg = sum(recent) / len(recent)
        idx = max(0, min(100, round(50 + avg * 2)))  # 50=중립, 차이 클수록 ±
        if idx >= 63:   label, cls, desc = "팀운 좋음 😊", "tl-good",   "최근 팀원들이 적팀보다 좋은 활약"
        elif idx >= 54: label, cls, desc = "팀운 약간 좋음 🙂", "tl-ok", "팀원 활약이 평균 이상"
        elif idx > 46:  label, cls, desc = "팀운 평범 😐", "tl-normal", "팀원·적팀 활약이 비슷"
        elif idx > 37:  label, cls, desc = "팀운 약간 아쉬움 😕", "tl-bad", "팀원 활약이 평균 이하"
        else:           label, cls, desc = "팀운 나쁨 😩", "tl-vbad",   "최근 팀원들이 적팀보다 부진"
        recent_team_luck = {"index": idx, "label": label, "cls": cls, "desc": desc,
                            "games": len(recent), "avg_diff": round(avg, 1)}

    return matches, overall_radar, primary_role, secondary_role, win_rate, most, overall_kda, deep_tags, puuid, len(matches), top_recent_champs, win_count, lose_count, banner_champ, repeat_encounters, recent_team_luck

def get_multi_search_summary(name, tag):
    try:
        acc_res = riot_get(f"https://asia.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{name}/{tag}")
        if acc_res.status_code != 200: return None
        puuid = acc_res.json()['puuid']

        v4 = riot_get(f"https://kr.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}").json()
        tier_text, tier_name = get_league_info_by_puuid(puuid)

        url = f"https://asia.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count=10&type=ranked"
        match_ids = riot_get(url).json()

        win_count, total_k, total_d, total_a = 0, 0, 0, 0
        recent_champs = {}

        for m_id in match_ids[:10]:
            m_res = riot_get(f"https://asia.api.riotgames.com/lol/match/v5/matches/{m_id}").json()
            for p in m_res['info']['participants']:
                if p['puuid'] == puuid:
                    if p['win']: win_count += 1
                    total_k += p['kills']; total_d += p['deaths']; total_a += p['assists']
                    c_en = champ_ddragon_id(p['championName'])
                    if c_en not in recent_champs: recent_champs[c_en] = 0
                    recent_champs[c_en] += 1
                    break
                    
        game_count = len(match_ids[:10]) if match_ids else 1
        win_rate = round((win_count/game_count)*100) if match_ids else 0
        kda = round((total_k + total_a) / max(1, total_d), 2)
        most_champ = sorted(recent_champs.items(), key=lambda x: x[1], reverse=True)[0][0] if recent_champs else "Teemo"
        
        return {
            "name": acc_res.json().get('gameName', name), "tag": acc_res.json().get('tagLine', tag),
            "icon": v4.get('profileIconId', 627), "level": v4.get('summonerLevel', 1),
            "tier_text": tier_text, "tier_name": tier_name,
            "win_rate": win_rate, "kda": kda, "most_champ": most_champ
        }
    except Exception as e:
        print(f"멀티 서치 에러: {e}")
        return None

def calc_game_grade(kda_ratio, kp, cs_per_min, win):
    score = min(35, kda_ratio * 7) + min(25, kp * 0.4) + min(20, cs_per_min * 4.5)
    if win: score += 20
    if score >= 88: return "S+"
    elif score >= 76: return "S"
    elif score >= 64: return "A+"
    elif score >= 52: return "A"
    elif score >= 42: return "B+"
    elif score >= 32: return "B"
    return "C"

def calc_player_tag(p):
    """스코어보드용 플레이어 1줄 분석 태그. 인게임 성과 기반 자동 판정."""
    kda = p.get('kda', 0)
    deaths = p.get('d', 0)
    dmg_pct = p.get('dmg_percent', 0)
    kp = p.get('kp', 0)
    cspm = p.get('cspm', 0)
    role = p.get('role_en', '')

    # 우선순위 순으로 가장 두드러진 특성 1개 부여
    if kda >= 6 and dmg_pct >= 70:
        return {"label": "하드 캐리", "color": "#fbbf24"}
    if dmg_pct >= 85:
        return {"label": "딜링 1위", "color": "#f472b6"}
    if deaths >= 10:
        return {"label": "휘청임", "color": "#f87171"}
    if kda >= 5:
        return {"label": "고효율", "color": "#4ade80"}
    if role == 'UTILITY' and kp >= 60:
        return {"label": "교전 설계", "color": "#a78bfa"}
    if role in ('BOTTOM', 'MIDDLE') and dmg_pct >= 60:
        return {"label": "주력 딜러", "color": "#f472b6"}
    if role == 'TOP' and deaths <= 4 and kda >= 2:
        return {"label": "단단함", "color": "#60a5fa"}
    if cspm >= 8.5:
        return {"label": "파밍왕", "color": "#60a5fa"}
    if kp >= 70:
        return {"label": "교전 집중", "color": "#4ade80"}
    if kda >= 3:
        return {"label": "안정적", "color": "#94a3b8"}
    if kp < 35:
        return {"label": "이탈 잦음", "color": "#f87171"}
    return {"label": "무난함", "color": "#94a3b8"}

GRADE_STYLE = {
    "S+": {"color": "#fbbf24", "bg": "rgba(251,191,36,0.2)", "border": "rgba(251,191,36,0.5)"},
    "S":  {"color": "#fbbf24", "bg": "rgba(251,191,36,0.15)", "border": "rgba(251,191,36,0.4)"},
    "A+": {"color": "#4ade80", "bg": "rgba(74,222,128,0.15)", "border": "rgba(74,222,128,0.4)"},
    "A":  {"color": "#4ade80", "bg": "rgba(74,222,128,0.1)", "border": "rgba(74,222,128,0.3)"},
    "B+": {"color": "#60a5fa", "bg": "rgba(96,165,250,0.15)", "border": "rgba(96,165,250,0.4)"},
    "B":  {"color": "#60a5fa", "bg": "rgba(96,165,250,0.1)", "border": "rgba(96,165,250,0.3)"},
    "C":  {"color": "#94a3b8", "bg": "rgba(148,163,184,0.1)", "border": "rgba(148,163,184,0.3)"},
}

def analyze_champion_playstyle(stats):
    count = stats['count']
    avg_k = stats['k'] / count
    avg_d = stats['d'] / count
    avg_a = stats['a'] / count
    avg_kda = round((avg_k + avg_a) / max(1, avg_d), 2)
    avg_kp = round(stats.get('kp', 0) / count)
    avg_cs = stats.get('cs', 0) / count
    avg_dur = max(1, stats.get('duration_m', count) / count)
    avg_cs_per_min = round(avg_cs / avg_dur, 1)
    avg_vision = round(stats.get('vision', 0) / count, 1)
    win_rate = round((stats['win'] / count) * 100)

    style_tags = []
    if avg_kda >= 4.0:   style_tags.append({"label": "칼날 KDA", "color": "#fbbf24"})
    elif avg_kda >= 2.5: style_tags.append({"label": "안정 KDA", "color": "#4ade80"})
    if avg_kp >= 70:     style_tags.append({"label": "교전 중심형", "color": "#4ade80"})
    elif avg_kp >= 55:   style_tags.append({"label": "합류형", "color": "#60a5fa"})
    if avg_cs_per_min >= 7.5: style_tags.append({"label": "파밍 머신", "color": "#60a5fa"})
    elif avg_cs_per_min >= 6: style_tags.append({"label": "성장 집중형", "color": "#a78bfa"})
    if avg_vision >= 25: style_tags.append({"label": "시야 장인", "color": "#a78bfa"})
    if avg_d >= 8:       style_tags.append({"label": "고위험 스타일", "color": "#f87171"})
    if win_rate >= 60:   style_tags.append({"label": "주력 챔피언", "color": "#fbbf24"})
    elif win_rate <= 35: style_tags.append({"label": "연습 필요", "color": "#f87171"})

    feedback = []
    if avg_d >= 7:
        feedback.append({"icon": "💀", "type": "danger", "title": f"평균 {round(avg_d,1)} 데스",
                         "desc": "생존력이 낮습니다. 포지셔닝을 개선하고 무리한 진입을 줄이세요."})
    if avg_kp < 45:
        feedback.append({"icon": "🎯", "type": "warning", "title": f"킬 관여 {avg_kp}%",
                         "desc": "팀 교전에 더 적극적으로 합류하세요."})
    elif avg_kp >= 70:
        feedback.append({"icon": "🤝", "type": "good", "title": f"킬 관여 {avg_kp}%",
                         "desc": "교전 기여도가 높습니다. 팀 중심 플레이어입니다."})
    if avg_cs_per_min < 5.5:
        feedback.append({"icon": "🌾", "type": "warning", "title": f"CS {avg_cs_per_min}/분",
                         "desc": "파밍 효율을 높이면 골드 격차를 벌릴 수 있습니다."})
    if win_rate >= 65:
        feedback.append({"icon": "🔥", "type": "good", "title": f"승률 {win_rate}%",
                         "desc": "이 챔피언이 현재 최고의 폼입니다. 계속 플레이하세요!"})
    elif win_rate <= 35:
        feedback.append({"icon": "📉", "type": "danger", "title": f"승률 {win_rate}%",
                         "desc": "이 챔피언 승률이 낮습니다. 메타 분석을 참고해보세요."})
    if not feedback:
        feedback.append({"icon": "✅", "type": "neutral", "title": "안정적인 플레이",
                         "desc": "큰 약점 없이 균형 잡힌 모습을 보이고 있습니다."})

    return {
        'avg_kda': avg_kda, 'avg_kp': avg_kp,
        'avg_cs_per_min': avg_cs_per_min, 'avg_vision': avg_vision,
        'win_rate': win_rate,
        'style_tags': style_tags[:3],
        'feedback': feedback[:2],
    }

# ═══════════════════════════════════════════════════════════════════════
#  ★ 상황 대응형 유동적 아이템 빌드 최적화 엔진
# ═══════════════════════════════════════════════════════════════════════

def analyze_enemy_comp(enemy_champs):
    """상대 팀 챔피언 영문명 리스트 → 조합 특성 분석.
    enemy_champs: [{'champ_en': 'Sion', 'kr': '사이온'}, ...] 형태"""
    profiles = []
    for c in enemy_champs:
        prof = get_champ_profile(c['champ_en'], CHAMP_TAGS.get(c['champ_en'], []))
        profiles.append({**prof, 'champ_en': c['champ_en'], 'kr': c['kr']})

    tanks  = [p for p in profiles if p['tank']]
    heals  = [p for p in profiles if p['heal']]
    cc     = [p for p in profiles if p['cc']]
    ad     = [p for p in profiles if p['dmg'] in ('AD', 'AD/AP')]
    ap     = [p for p in profiles if p['dmg'] in ('AP', 'AD/AP')]

    total = max(1, len(profiles))
    return {
        'profiles': profiles,
        'tank_names':  [p['kr'] for p in tanks],
        'heal_names':  [p['kr'] for p in heals],
        'cc_names':    [p['kr'] for p in cc],
        'tank_count':  len(tanks),
        'heal_count':  len(heals),
        'cc_count':    len(cc),
        'ad_count':    len(ad),
        'ap_count':    len(ap),
        # 위협 플래그
        'heavy_tank':  len(tanks) >= 2,
        'heavy_heal':  len(heals) >= 2,
        'heavy_cc':    len(cc) >= 4,
        'ad_dominant': len(ad) >= 4,
        'ap_dominant': len(ap) >= 4,
    }

def recommend_situational_build(my_champ_en, my_role, comp):
    """내 챔피언 딜 타입 + 상대 조합 → 상황별 우회 아이템 추천 + 데이터 근거.
    반환: 추천 카드 리스트 (우선순위 순, 최대 3개)"""
    my_prof = get_champ_profile(my_champ_en, CHAMP_TAGS.get(my_champ_en, []))
    my_dmg = my_prof['dmg']
    is_dealer = my_role in ('TOP', 'JUNGLE', 'MIDDLE', 'BOTTOM') and not my_prof['tank']
    is_frontline = my_prof['tank'] or my_role == 'UTILITY'

    recs = []

    # 1) 상대 하드 탱커 다수 → 비례 피해/방어 관통 (딜러 한정)
    if comp['heavy_tank'] and is_dealer:
        names = ", ".join(comp['tank_names'][:3])
        if my_dmg == 'AP':
            items = COUNTER_ITEMS['anti_tank_ap']
            reason = (f"상대 팀에 {names} 등 하드 탱커가 {comp['tank_count']}명 있습니다. "
                      f"일반 주문력 빌드로는 단단한 앞라인을 녹이기 어렵습니다. "
                      f"마법 관통과 현재 체력 비례 피해 아이템으로 우회해 탱커를 효율적으로 무력화하세요.")
        else:
            items = COUNTER_ITEMS['anti_tank_ad']
            reason = (f"상대 팀에 {names} 등 체력을 두껍게 확보한 하드 탱커가 {comp['tank_count']}명 배치되어 있습니다. "
                      f"치명타·고정 빌드를 고집하기보다 현재 체력 비례 피해와 방어 관통 아이템을 우회 코어로 채용하면 "
                      f"상대 앞라인을 녹이는 딜링 효율이 극대화됩니다.")
        recs.append({"priority": 1, "icon": "🛡️", "tag": "대(對) 탱커",
                     "tag_color": "#fbbf24", "items": items, "reason": reason})

    # 2) 상대 회복/지속력 다수 → 치유 감소
    if comp['heavy_heal']:
        names = ", ".join(comp['heal_names'][:3])
        items = COUNTER_ITEMS['anti_heal_ap'] if my_dmg == 'AP' else COUNTER_ITEMS['anti_heal_ad']
        reason = (f"상대 팀에 {names} 등 자체 회복·흡혈이 강한 챔피언이 {comp['heal_count']}명 있습니다. "
                  f"치유 감소(고통스러운 상처) 아이템을 한 코어 앞당겨 구매하면 상대의 지속 교전력을 크게 깎을 수 있습니다.")
        recs.append({"priority": 2, "icon": "🩸", "tag": "대(對) 회복",
                     "tag_color": "#f87171", "items": items, "reason": reason})

    # 3) 상대 AD/AP 편중 → 해당 저항 방어 아이템 (탱커/서폿/위협받는 모두)
    if comp['ad_dominant'] and not (my_dmg == 'AD' and is_dealer and not is_frontline):
        items = COUNTER_ITEMS['anti_ad']
        reason = (f"상대 딜 구성이 물리 피해(AD) {comp['ad_count']}명으로 크게 편중되어 있습니다. "
                  f"방어력 아이템을 한 개 이상 우선 채용하면 상대 주력 딜을 통째로 무력화할 수 있습니다. "
                  f"특히 치명타가 많다면 랜듀인의 예언이 효과적입니다.")
        recs.append({"priority": 3, "icon": "⚔️", "tag": "물리 방어",
                     "tag_color": "#60a5fa", "items": items, "reason": reason})
    elif comp['ap_dominant'] and not (my_dmg == 'AP' and is_dealer and not is_frontline):
        items = COUNTER_ITEMS['anti_ap']
        reason = (f"상대 딜 구성이 마법 피해(AP) {comp['ap_count']}명으로 크게 편중되어 있습니다. "
                  f"마법 저항 아이템을 한 개 우선 확보하면 상대 폭딜을 안정적으로 버틸 수 있습니다.")
        recs.append({"priority": 3, "icon": "🔮", "tag": "마법 방어",
                     "tag_color": "#a78bfa", "items": items, "reason": reason})

    # 4) 상대 강력한 CC 다수 → CC 해제/면역 (딜러에게 특히 치명적)
    if comp['heavy_cc'] and is_dealer:
        names = ", ".join(comp['cc_names'][:3])
        items = COUNTER_ITEMS['anti_cc']
        reason = (f"상대 팀에 {names} 등 강력한 군중제어기를 가진 챔피언이 {comp['cc_count']}명 있습니다. "
                  f"한 번의 CC 연계에 즉사할 수 있으므로, 군중제어 해제 장신구·아이템으로 생존 변수를 확보하세요.")
        recs.append({"priority": 4, "icon": "🌀", "tag": "대(對) CC",
                     "tag_color": "#34d399", "items": items, "reason": reason})

    recs.sort(key=lambda x: x['priority'])
    return recs[:3]

def generate_improvement_tips(matches, overall_kda, radar_array, primary_role=None, tier_name=None):
    """유저 지표를 '같은 티어·역할 평균(실측 벤치마크)'과 비교해 수치 기반 개선 포인트 생성.
    벤치마크 표본이 부족하면(가짜 숫자 금지) 라이다/폼 기반 폴백으로 안전 동작."""
    tips = []
    tier_key = (tier_name or "").strip().split(" ")[0].lower()
    bench = get_tier_benchmarks(tier_key, primary_role) if (tier_key and primary_role) else {}

    # ── 1) 데이터 기반: 내 지표 vs 티어 평균 ──
    if bench and matches:
        role_games = [m for m in matches if m.get('role_en') == primary_role and m.get('metrics')]
        if len(role_games) >= 3:
            uavg = {}
            for metric in BENCHMARK_METRICS:
                vals = [m['metrics'].get(metric) for m in role_games if m['metrics'].get(metric) is not None]
                if vals:
                    uavg[metric] = sum(vals) / len(vals)
            tkr = TIER_KR.get(tier_key, tier_key)
            weak = []  # (상대격차, tip)
            for metric, meta in BENCHMARK_METRICS.items():
                if metric not in bench or metric not in uavg or bench[metric] <= 0:
                    continue
                u, b = uavg[metric], bench[metric]
                deficit = (b - u) if meta['higher'] else (u - b)  # 양수 = 개선 필요
                rel = deficit / abs(b)
                if rel > 0.12:
                    unit = meta['unit']
                    word = "부족합니다" if meta['higher'] else "많습니다"
                    weak.append((rel, {"icon": "📊", "type": "danger" if rel > 0.28 else "warning",
                        "title": f"{meta['kr']} {round(u,1)}{unit} — {tkr} 평균({round(b,1)}{unit})보다 {round(abs(b-u),1)}{unit} {word}",
                        "desc": meta['advice']}))
            weak.sort(key=lambda x: -x[0])
            tips.extend(t for _, t in weak[:2])
            # 강점 1개 (티어 평균 대비 우수)
            for metric, meta in BENCHMARK_METRICS.items():
                if meta['higher'] and metric in bench and metric in uavg and bench[metric] > 0 and uavg[metric] >= bench[metric] * 1.15:
                    tips.append({"icon": "🔥", "type": "good",
                        "title": f"{meta['kr']} {round(uavg[metric],1)}{meta['unit']} — {tkr} 평균 이상 👍",
                        "desc": "이 강점을 적극 살려 게임을 주도하세요."})
                    break

    # ── 2) 폴백: 벤치마크 미확보 시 라이다/폼 기반 (가짜 수치 없이) ──
    if not tips:
        avg_d = overall_kda.get('d', 0)
        if avg_d >= 6:
            tips.append({"icon": "💀", "type": "danger", "title": f"평균 {avg_d}데스 — 생존력 개선 필요",
                "desc": "과감한 진입보다 포지셔닝을 우선시하세요."})
        if radar_array and len(radar_array) > 2 and radar_array[2] < 38:
            tips.append({"icon": "👁️", "type": "warning", "title": "시야 기여도 낮음",
                "desc": "귀환마다 제어 와드를 구매하고, 리콜 전 시야를 확인하세요."})
        kda_ratio = overall_kda.get('ratio', 0)
        if kda_ratio >= 4.0:
            tips.append({"icon": "🔥", "type": "good", "title": f"KDA 상위권 — {kda_ratio}:1",
                "desc": "효율적인 플레이입니다. 현재 패턴을 유지하세요!"})
        if not tips:
            tips.append({"icon": "⚖️", "type": "neutral", "title": "전반적으로 안정적인 플레이",
                "desc": "큰 약점 없이 균형 잡힌 플레이를 하고 있습니다."})
    return tips[:3]

# 라디아 축 → 추천 카테고리 + 어울리는 챔피언 클래스(DDragon tags)
AXIS_RECO = {
    '생존':     {"cat": "높은 '생존력'을 살릴 픽",   "icon": "🛡️", "tags": {"Tank", "Fighter"}},
    '시야':     {"cat": "'시야 장악'을 극대화할 픽", "icon": "👁️", "tags": {"Support"}},
    '전투':     {"cat": "'전투력' 기반 캐리 픽",     "icon": "⚔️", "tags": {"Assassin", "Fighter", "Mage"}},
    '성장':     {"cat": "'성장' 스케일링 픽",        "icon": "📈", "tags": {"Marksman", "Mage"}},
    '오브젝트': {"cat": "'오브젝트' 장악 픽",        "icon": "🐉", "tags": {"Tank", "Fighter"}},
    '합류':     {"cat": "'합류·로밍'에 강한 픽",     "icon": "📍", "tags": {"Mage", "Support", "Tank"}},
}
NUM_TIER_LABEL = {"OP": "OP", "1": "1티어", "2": "2티어", "3": "3티어", "4": "4티어", "5": "5티어"}

def recommend_champions(radar_array, primary_role=None):
    """플레이스타일(라디아) × 현재 패치 메타(측정 티어·승률)를 교차검증해
    카테고리별 3~4개 추천. 모든 추천은 현재 OP/1/2 티어 + 실측/예측 승률 근거."""
    if not radar_array or sum(radar_array) == 0:
        return []
    labels = ['전투', '성장', '시야', '생존', '오브젝트', '합류']
    order = sorted(range(len(radar_array)), key=lambda i: -radar_array[i])
    top_axes = [labels[i] for i in order[:3]]  # 강점 상위 3축

    try:
        tiers = build_champion_meta("emeraldplus")
    except Exception:
        tiers = {}
    strong = []
    for role, lst in tiers.items():
        for c in lst:
            if c.get('tier') in ('OP', '1', '2') and c.get('id'):
                strong.append({**c, "tags": set(CHAMP_TAGS.get(c['id'], []))})
    tier_rank = {"OP": 0, "1": 1, "2": 2}
    strong.sort(key=lambda x: (tier_rank.get(x['tier'], 3), -x.get('wr', 0)))

    used, recs = set(), []
    def add(c, cat, icon, axis=None):
        tlabel = NUM_TIER_LABEL.get(c['tier'], c['tier'])
        reason = (f"현재 {tlabel} · 승률 {c.get('wr',0)}% — 당신의 '{axis}' 강점과 시너지"
                  if axis else f"현재 {tlabel} · 승률 {c.get('wr',0)}% — 패치 {CURRENT_PATCH_DISPLAY} 상위 메타")
        recs.append({"id": c['id'], "kr": c['kr'],
                     "role": c.get('role_kr', ROLE_KR.get(c.get('role_en', ''), '')),
                     "cat": cat, "icon": icon, "tier": c['tier'],
                     "tier_style": c.get('tier_style'), "wr": c.get('wr', 0), "reason": reason})
        used.add(c['id'])

    # 강점 축별로 메타 상위 + 클래스 매칭 1픽씩
    for axis in top_axes:
        meta = AXIS_RECO.get(axis)
        if not meta:
            continue
        for c in strong:
            if c['id'] not in used and c['tags'] & meta['tags']:
                add(c, meta['cat'], meta['icon'], axis)
                break
    # 조커/메타 픽: 남은 강챔 최상위 1개로 다양성 보강
    for c in strong:
        if c['id'] not in used and len(recs) < 4:
            add(c, "지금 가장 강력한 메타 픽", "🔥")
            break
    return recs[:4]

def get_challenger_leaderboard():
    cache_key = "leaderboard_kr_solo"
    current_time = int(time.time())

    # ✅ MEDIUM 개선: db_read() 헬퍼 사용
    cached_json, updated_at = db_read(cache_key)
    if cached_json and current_time - updated_at < LEADERBOARD_CACHE_TIME:
        return json.loads(cached_json), True

    try:
        res = riot_get("https://kr.api.riotgames.com/lol/league/v4/challengerleagues/by-queue/RANKED_SOLO_5x5")
        if res.status_code != 200:
            return [], False
        entries = res.json().get('entries', [])
        entries.sort(key=lambda x: x['leaguePoints'], reverse=True)
        leaderboard = []
        for i, e in enumerate(entries[:50]):
            wins = e.get('wins', 0)
            losses = e.get('losses', 0)
            total = wins + losses
            winrate = round((wins / total) * 100) if total > 0 else 0
            leaderboard.append({
                "rank": i + 1,
                "name": e.get('summonerName', f'소환사{i+1}'),
                "tag": "KR1",
                "lp": f"{e['leaguePoints']:,} LP",
                "win": wins,
                "lose": losses,
                "winrate": winrate,
                "hotstreak": e.get('hotStreak', False),
                "veteran": e.get('veteran', False),
            })
        # ✅ MEDIUM 개선: db_write() 헬퍼 사용
        db_write(cache_key, leaderboard, current_time)
        return leaderboard, False
    except Exception as e:
        print(f"리더보드 API 에러: {e}")
        return [], False

def get_live_challenger_games():
    cache_key = "spectate_live_games"
    current_time = int(time.time())

    # ✅ MEDIUM 개선: db_read() 헬퍼 사용
    cached_json, updated_at = db_read(cache_key)
    if cached_json and current_time - updated_at < SPECTATE_CACHE_TIME:
        return json.loads(cached_json), True

    try:
        res = riot_get("https://kr.api.riotgames.com/lol/league/v4/challengerleagues/by-queue/RANKED_SOLO_5x5")
        if res.status_code != 200:
            return [], False
        entries = res.json().get('entries', [])
        entries.sort(key=lambda x: x['leaguePoints'], reverse=True)

        live_games, seen_ids = [], set()
        for entry in entries[:20]:
            if len(live_games) >= 6:
                break
            # 챌린저 엔트리는 puuid를 직접 제공 (summonerId 미제공)
            puuid = entry.get('puuid', '')
            if not puuid:
                continue
            g_res = riot_get(f"https://kr.api.riotgames.com/lol/spectator/v5/active-games/by-puuid/{puuid}")
            if g_res.status_code == 403:
                break  # 개인 키는 Spectator 미허용 → 호출 낭비 방지 (Production Key 승인 시 자동 작동)
            if g_res.status_code != 200:
                continue
            game = g_res.json()
            game_id = game.get('gameId')
            if game_id in seen_ids:
                continue
            seen_ids.add(game_id)
            if game.get('gameQueueConfigId', 0) not in [420, 440]:
                continue
            length = game.get('gameLength', 0)
            participants = game.get('participants', [])
            blue = [p for p in participants if p['teamId'] == 100]
            red  = [p for p in participants if p['teamId'] == 200]
            # puuid → 라이엇 ID 해석 (게임당 1회, 최대 6회)
            disp_name, disp_tag = "챌린저", "KR1"
            try:
                acc = riot_get(f"https://asia.api.riotgames.com/riot/account/v1/accounts/by-puuid/{puuid}")
                if acc.status_code == 200:
                    aj = acc.json()
                    disp_name = aj.get('gameName', '챌린저')
                    disp_tag = aj.get('tagLine', 'KR1')
            except Exception:
                pass
            live_games.append({
                "summoner_name": disp_name,
                "summoner_tag": disp_tag,
                "lp": f"{entry['leaguePoints']:,} LP",
                "tier": "CHALLENGER",
                "queue": QUEUE_MAP.get(game.get('gameQueueConfigId', 0), "랭크"),
                "time": f"{length // 60:02d}:{length % 60:02d}",
                "blue_champs": [{'id': CHAMP_KEYS.get(str(p['championId']), 'Teemo'),
                                 'kr': CHAMP_KR_MAP.get(CHAMP_KEYS.get(str(p['championId']), 'Teemo'), 'Teemo')} for p in blue[:5]],
                "red_champs":  [{'id': CHAMP_KEYS.get(str(p['championId']), 'Teemo'),
                                 'kr': CHAMP_KR_MAP.get(CHAMP_KEYS.get(str(p['championId']), 'Teemo'), 'Teemo')} for p in red[:5]],
            })

        # ✅ MEDIUM 개선: db_write() 헬퍼 사용
        db_write(cache_key, live_games, current_time)
        return live_games, False
    except Exception as e:
        print(f"관전 API 에러: {e}")
        return [], False

# ================= 고티어 운영법 (에디터 직접 작성) =================
GUIDE_PHASES = [
    ("early", "초반 라인전 및 동선"),
    ("mid",   "중반 사이드/오브젝트 합류"),
    ("late",  "후반 한타 포지셔닝"),
]
GUIDE_BRACKET_KR = {"emeraldplus": "에메랄드+"}

def get_champion_guide(champ_en, bracket="emeraldplus"):
    """챔피언 운영법(단계별) 반환. 작성된 단계만 포함. 없으면 has_guide=False."""
    rows = []
    try:
        conn = db_connect()
        rows = conn.execute("SELECT phase, title, body FROM champion_guide WHERE champ_en=? AND bracket=?",
                            (champ_en, bracket)).fetchall()
        conn.close()
    except Exception as e:
        print(f"운영법 조회 에러 [{champ_en}]: {e}")
    bymap = {r[0]: (r[1], r[2]) for r in rows if (r[2] or "").strip()}
    phases = []
    for key, default_title in GUIDE_PHASES:
        if key in bymap:
            title, body = bymap[key]
            phases.append({"key": key, "title": title or default_title, "body": body.strip()})
    return {"has_guide": bool(phases), "bracket": GUIDE_BRACKET_KR.get(bracket, bracket), "phases": phases}

def get_guide_raw(champ_en, bracket="emeraldplus"):
    """관리자 편집용 — 작성 여부 무관 전체 단계 원본 반환 {phase:{title,body}}."""
    try:
        conn = db_connect()
        rows = conn.execute("SELECT phase, title, body FROM champion_guide WHERE champ_en=? AND bracket=?",
                            (champ_en, bracket)).fetchall()
        conn.close()
        return {r[0]: {"title": r[1] or "", "body": r[2] or ""} for r in rows}
    except Exception as e:
        print(f"운영법 원본 조회 에러: {e}")
        return {}

def save_champion_guide(champ_en, form, bracket="emeraldplus"):
    """관리자 폼 → 단계별 운영법 upsert."""
    conn = db_connect()
    ts = int(time.time())
    for key, default_title in GUIDE_PHASES:
        body = (form.get(key) or "").strip()
        title = (form.get(key + "_title") or default_title).strip() or default_title
        conn.execute("""INSERT INTO champion_guide (champ_en, bracket, phase, title, body, updated_at)
                        VALUES (?,?,?,?,?,?)
                        ON CONFLICT(champ_en, bracket, phase) DO UPDATE SET
                          title=EXCLUDED.title, body=EXCLUDED.body, updated_at=EXCLUDED.updated_at""",
                     (champ_en, bracket, key, title, body, ts))
    conn.commit()
    conn.close()

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")
def is_admin():
    """관리자 여부 — 로그인 사용자명이 ADMIN_USERNAME(환경변수)과 일치."""
    return bool(ADMIN_USERNAME) and session.get('username') == ADMIN_USERNAME

# ================= 회원 인증 =================
@app.context_processor
def inject_user():
    """모든 템플릿에서 current_user / AI 코치 노출 여부 사용 가능하도록 주입."""
    base = {'ai_coach_enabled': AI_COACH_ENABLED}
    if session.get('user_id'):
        base['current_user'] = {'id': session['user_id'], 'username': session.get('username'),
                                'riot_name': session.get('riot_name'), 'riot_tag': session.get('riot_tag')}
    else:
        base['current_user'] = None
    return base

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'GET':
        return render_template('index.html', page='signup')
    username = (request.form.get('username') or '').strip()
    password = request.form.get('password') or ''
    password2 = request.form.get('password2') or ''
    riot_name = (request.form.get('riot_name') or '').strip()
    riot_tag = (request.form.get('riot_tag') or '').strip().lstrip('#')

    if len(username) < 3 or len(username) > 20:
        return render_template('index.html', page='signup', error="아이디는 3~20자로 입력해주세요.", form=request.form)
    if len(password) < 6:
        return render_template('index.html', page='signup', error="비밀번호는 6자 이상이어야 합니다.", form=request.form)
    if password != password2:
        return render_template('index.html', page='signup', error="비밀번호가 일치하지 않습니다.", form=request.form)

    try:
        conn = db_connect()
        exists = conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
        if exists:
            conn.close()
            return render_template('index.html', page='signup', error="이미 사용 중인 아이디입니다.", form=request.form)
        params = (username, generate_password_hash(password), riot_name, riot_tag, int(time.time()))
        if IS_PG:
            uid = conn.execute("""INSERT INTO users (username, password_hash, riot_name, riot_tag, created_at)
                                  VALUES (?,?,?,?,?) RETURNING id""", params).fetchone()[0]
        else:
            cur = conn.execute("""INSERT INTO users (username, password_hash, riot_name, riot_tag, created_at)
                                  VALUES (?,?,?,?,?)""", params)
            uid = cur.lastrowid
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"회원가입 에러: {e}")
        return render_template('index.html', page='signup', error="가입 처리 중 오류가 발생했습니다.", form=request.form)

    session['user_id'] = uid
    session['username'] = username
    session['riot_name'] = riot_name
    session['riot_tag'] = riot_tag
    return redirect('/')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        return render_template('index.html', page='login')
    username = (request.form.get('username') or '').strip()
    password = request.form.get('password') or ''
    try:
        conn = db_connect()
        row = conn.execute("SELECT id, password_hash, riot_name, riot_tag FROM users WHERE username=?", (username,)).fetchone()
        conn.close()
    except Exception as e:
        print(f"로그인 에러: {e}")
        return render_template('index.html', page='login', error="로그인 처리 중 오류가 발생했습니다.", form=request.form)

    if not row or not check_password_hash(row[1], password):
        return render_template('index.html', page='login', error="아이디 또는 비밀번호가 올바르지 않습니다.", form=request.form)

    session['user_id'] = row[0]
    session['username'] = username
    session['riot_name'] = row[2]
    session['riot_tag'] = row[3]
    return redirect('/')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

# ================= 라우팅 =================
@app.route('/live_games')
def live_games_api():
    """홈 '실시간 천상계 라이브' 위젯용 — 비동기 로드(JSON). 기존 관전 백엔드 재사용."""
    games, _ = get_live_challenger_games()
    return jsonify({"games": (games or [])[:3], "version": LATEST_VERSION})

@app.route('/')
def index():
    ensure_current_patch()  # 새 패치 자동 감지·갱신 (스로틀)
    # 라인별 최강 챔피언 — 실측 메타와 동일 산출(보정 승률·티어, /meta와 일치)
    tiers = build_champion_meta("emeraldplus")
    meta_top = []
    for role in ['TOP', 'JUNGLE', 'MIDDLE', 'BOTTOM', 'UTILITY']:
        lst = tiers.get(role, [])
        if lst:
            meta_top.append(lst[0])  # 이미 티어·승률순 정렬 + role_kr/tier_style 포함
    patch_highlights = get_patch_highlights()  # 이번 패치 버프/너프 (캐시)
    return render_template('index.html', page='home', roster_data=GLOBAL_ROSTER_DATA,
                           pro_gamers=PRO_GAMERS, latest_version=LATEST_VERSION,
                           meta_top=meta_top, current_patch=CURRENT_PATCH_DISPLAY,
                           patch_highlights=patch_highlights,
                           total_games=get_stats_total_games(), stats_basis=STATS_BASIS_LABEL)

def build_champion_meta(rank_tier="emeraldplus"):
    result = {"TOP": [], "JUNGLE": [], "MIDDLE": [], "BOTTOM": [], "UTILITY": []}
    processed = set()

    # ★ 실측 통계 로드 (피기백 수집 데이터)
    real_stats, real_bans = get_real_stats_map()
    total_games = get_stats_total_games()

    for champ_en, kr_name in CHAMP_KR_MAP.items():
        roles = CHAMPION_ROLE_MAP.get(champ_en, [])
        if not roles:
            tags = CHAMP_TAGS.get(champ_en, [])
            if "Marksman" in tags:
                roles = ["BOTTOM"]
            elif "Support" in tags and "Fighter" not in tags:
                roles = ["UTILITY"]
            elif "Assassin" in tags and "Fighter" not in tags:
                roles = ["MIDDLE"]
            elif "Mage" in tags and "Fighter" not in tags:
                roles = ["MIDDLE"]
            else:
                roles = ["TOP"]

        primary_role = roles[0]
        if champ_en in processed:
            continue
        processed.add(champ_en)

        # 정적(예측) 데이터 — 난이도/추세 등 메타 정보 출처
        champ_stats_entry = META_STATS.get(champ_en, {})
        role_stats_entry  = champ_stats_entry.get(primary_role, {})
        stats = role_stats_entry.get(rank_tier, None)
        if stats:
            difficulty = stats.get("difficulty", 2)
        else:
            difficulty = DEFAULT_ROLE_STATS.get(primary_role, DEFAULT_ROLE_STATS["TOP"])["difficulty"]

        # ★ 실측 우선: 표본이 충분하면 수집 데이터로, 아니면 정적 예측으로
        real_role = real_stats.get(champ_en, {}).get(primary_role)
        sample = real_role["games"] if real_role else 0
        if real_role and sample >= MIN_SAMPLE and total_games > 0:
            wr = smooth_wr(real_role["wins"], sample)
            pr = round(sample / total_games * 100, 1)
            br = round(real_bans.get(champ_en, 0) / total_games * 100, 1)
            source, trend = "실측", "stable"
        else:
            if stats:
                wr, pr, br = stats["wr"], stats["pr"], stats["br"]
                trend = stats.get("trend", "stable")
            else:
                d = DEFAULT_ROLE_STATS.get(primary_role, DEFAULT_ROLE_STATS["TOP"])
                wr, pr, br = d["wr"], d["pr"], d["br"]
                trend = d["trend"]
            source = "예측"

        num_tier   = calc_meta_tier(wr, pr, br)
        tier_style = NUMERIC_TIER_COLOR.get(num_tier, NUMERIC_TIER_COLOR["5"])

        existing = next((c for t in CHAMPION_TIERS.values()
                         for c in t if c["id"].lower() == champ_en.lower()), None)

        entry = {
            "id": champ_en, "kr": kr_name,
            "tier": num_tier, "tier_style": tier_style,
            "wr": wr, "pr": pr, "br": br,
            "trend": trend, "trend_icon": TREND_ICON.get(trend, "→"),
            "trend_color": TREND_COLOR.get(trend, "#94a3b8"),
            "difficulty": difficulty,
            "diff_label": DIFFICULTY_LABEL.get(difficulty, "보통"),
            "diff_color": DIFFICULTY_COLOR.get(difficulty, "#fbbf24"),
            "has_detail": existing is not None,
            "role_kr": ROLE_KR.get(primary_role, primary_role),
            "role_en": primary_role,
            "source": source, "sample": sample,
        }
        result[primary_role].append(entry)

    for role in result:
        tier_order = {"OP": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5}
        result[role].sort(key=lambda x: (tier_order.get(x["tier"], 5), -x["wr"]))

    return result

@app.route('/meta')
def meta():
    ensure_current_patch()  # 새 패치 자동 감지·갱신 (스로틀)
    rank_tier = request.args.get("rank", "emeraldplus")
    if rank_tier not in RANK_TIER_LABELS:
        rank_tier = "emeraldplus"
    champion_tiers = build_champion_meta(rank_tier)
    tier_counts = {"OP": 0, "1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
    real_count = 0
    for champs in champion_tiers.values():
        for c in champs:
            tier_counts[c["tier"]] = tier_counts.get(c["tier"], 0) + 1
            if c.get("source") == "실측":
                real_count += 1
    return render_template('index.html', page='meta', champion_tiers=champion_tiers,
                           latest_version=LATEST_VERSION, current_patch=CURRENT_PATCH_DISPLAY,
                           role_kr=ROLE_KR, rank_tier=rank_tier,
                           rank_tier_labels=RANK_TIER_LABELS,
                           numeric_tier_color=NUMERIC_TIER_COLOR,
                           tier_counts=tier_counts,
                           total_games=get_stats_total_games(), real_count=real_count)

@app.route('/patch')
def patch_notes():
    ensure_current_patch()  # 새 패치 자동 감지·갱신 (스로틀)
    official = fetch_official_patch_notes()        # 공식 원문 파싱 (전체)
    highlights = get_patch_highlights()            # DDragon 자동감지 (폴백/요약)
    return render_template('index.html', page='patch', official=official,
                           highlights=highlights, latest_version=LATEST_VERSION,
                           current_patch=CURRENT_PATCH_DISPLAY)

APEX_TIERS = {"challenger", "grandmaster", "master"}
SAMPLE_TIERS = ["challenger", "grandmaster", "master", "diamond", "emerald", "platinum"]

def _fetch_tier_pool(tier):
    """티어별 플레이어 풀(엔트리 리스트) 조회. 다이아 이하는 랜덤 디비전·페이지."""
    tier = tier.lower()
    if tier in APEX_TIERS:
        r = riot_get(f"https://kr.api.riotgames.com/lol/league/v4/{tier}leagues/by-queue/RANKED_SOLO_5x5")
        return r.json().get('entries', []) if r.status_code == 200 else []
    div = random.choice(["I", "II", "III", "IV"])
    page = random.randint(1, 4)
    r = riot_get(f"https://kr.api.riotgames.com/lol/league/v4/entries/RANKED_SOLO_5x5/{tier.upper()}/{div}?page={page}")
    return r.json() if r.status_code == 200 else []

@app.route('/collect')
def collect():
    """플래티넘+ 다중 티어 순회 수집. ?n=플레이어수(기본5, 최대12), ?tier=auto|티어명.
    크론으로 주기 호출 시 매번 다른 티어를 표본화해 전 티어 커버."""
    global _LAST_DB_ERROR
    _LAST_DB_ERROR = None  # 이번 호출의 에러만 진단
    n = max(1, min(int(request.args.get("n", 5)), 12))
    tier = request.args.get("tier", "auto").lower()
    if tier not in SAMPLE_TIERS:
        tier = random.choice(SAMPLE_TIERS)  # auto: 티어 랜덤 순회
    collected, scanned = 0, 0
    try:
        pool = _fetch_tier_pool(tier)
        if not pool:
            return jsonify({"error": f"{tier} 풀 조회 실패", "tier": tier}), 502
        random.shuffle(pool)  # 상위 편중 방지, 다양한 플레이어 표본
        for entry in pool[:n]:
            puuid = entry.get('puuid', '')
            if not puuid:
                continue
            ids_res = riot_get(f"https://asia.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count=10&type=ranked")
            if ids_res.status_code != 200:
                continue
            scanned += 1
            for m_id in ids_res.json():
                raw = riot_get(f"https://asia.api.riotgames.com/lol/match/v5/matches/{m_id}")
                if raw.status_code != 200:
                    continue
                m_json = raw.json()
                if record_match_stats(m_json, m_id):
                    collected += 1
                info = m_json.get('info', {})
                if info.get('queueId', 0) in (420, 440):
                    # ★ 티어 벤치마크 적립: 표본 플레이어 본인 지표를 sampled 티어로
                    dm = info.get('gameDuration', 0) / 60.0
                    me = next((pp for pp in info.get('participants', []) if pp.get('puuid') == puuid), None)
                    if me and me.get('teamPosition') in VALID_ROLES and dm >= 5:
                        record_tier_benchmark(_extract_metrics(me, dm), tier, me['teamPosition'])
                    tl = riot_get(f"https://asia.api.riotgames.com/lol/match/v5/matches/{m_id}/timeline")
                    if tl.status_code == 200:
                        record_timeline_stats(tl.json(), m_json, m_id)
    except Exception as e:
        return jsonify({"error": str(e), "collected": collected, "tier": tier}), 500
    return jsonify({"tier": tier, "collected_new_matches": collected,
                    "scanned_players": scanned, "total_games": get_stats_total_games()})

@app.route('/champion/<champ_id>')
def champion_page(champ_id):
    ensure_current_patch()  # 새 패치 자동 감지·갱신 (스로틀)
    # 1) DDragon 정식 챔피언 ID로 정규화 (대소문자 보정)
    canonical_id = champ_ddragon_id(champ_id)  # Wukong→MonkeyKing, 대소문자 보정 포함
    if canonical_id not in CHAMP_KR_MAP:
        return redirect('/')

    # 2) 상세 빌드 데이터(CHAMPION_TIERS) 보유 여부 확인
    champ_data, champ_role = None, None
    for role, champs in CHAMPION_TIERS.items():
        for c in champs:
            if c['id'].lower() == canonical_id.lower():
                champ_data, champ_role = c, role
                break
        if champ_data: break

    has_build = champ_data is not None

    if has_build:
        styled = {**champ_data,
                  'tier_style': TIER_COLOR.get(champ_data['tier'], TIER_COLOR['B']),
                  'trend_icon': TREND_ICON.get(champ_data['trend'], '→'),
                  'trend_color': TREND_COLOR.get(champ_data['trend'], '#94a3b8'),
                  'diff_label': DIFFICULTY_LABEL.get(champ_data['difficulty'], '보통'),
                  'diff_color': DIFFICULTY_COLOR.get(champ_data['difficulty'], '#fbbf24'),
                  'weak_vs_kr': [{'id': e, 'kr': CHAMP_KR_MAP.get(e, e)} for e in champ_data.get('weak_vs', [])],
                  'strong_vs_kr': [{'id': e, 'kr': CHAMP_KR_MAP.get(e, e)} for e in champ_data.get('strong_vs', [])]}
    else:
        # 3) 상세 빌드가 없는 챔피언 — 메타 통계 기반 기본 페이지 구성
        roles = CHAMPION_ROLE_MAP.get(canonical_id, [])
        if not roles:
            tags = CHAMP_TAGS.get(canonical_id, [])
            if "Marksman" in tags: roles = ["BOTTOM"]
            elif "Support" in tags and "Fighter" not in tags: roles = ["UTILITY"]
            elif "Mage" in tags and "Fighter" not in tags: roles = ["MIDDLE"]
            elif "Assassin" in tags and "Fighter" not in tags: roles = ["MIDDLE"]
            else: roles = ["TOP"]
        primary_role = roles[0]
        stats = META_STATS.get(canonical_id, {}).get(primary_role, {}).get("emeraldplus")
        if not stats:
            stats = DEFAULT_ROLE_STATS.get(primary_role, DEFAULT_ROLE_STATS["TOP"])
        wr, pr, br = stats["wr"], stats["pr"], stats["br"]
        trend, difficulty = stats.get("trend", "stable"), stats.get("difficulty", 2)
        num_tier = calc_meta_tier(wr, pr, br)
        tags = CHAMP_TAGS.get(canonical_id, [])
        style_kr = "/".join(tags[:2]) if tags else "—"
        champ_role = primary_role
        styled = {
            'id': canonical_id, 'kr': CHAMP_KR_MAP.get(canonical_id, canonical_id),
            'tier': ('OP' if num_tier == 'OP' else num_tier),
            'tier_style': NUMERIC_TIER_COLOR.get(num_tier, NUMERIC_TIER_COLOR["5"]),
            'wr': wr, 'pr': pr, 'br': br,
            'trend_icon': TREND_ICON.get(trend, '→'), 'trend_color': TREND_COLOR.get(trend, '#94a3b8'),
            'diff_label': DIFFICULTY_LABEL.get(difficulty, '보통'), 'diff_color': DIFFICULTY_COLOR.get(difficulty, '#fbbf24'),
            'style': style_kr,
            'rune_main': None, 'rune_sub': None, 'skill_order': None, 'items': [], 'tip': None,
            'weak_vs_kr': [], 'strong_vs_kr': [],
        }

    # 팁/카운터는 신뢰 데이터 확보 전까지 숨김 (하드코딩 부정확 데이터 제거)
    styled['weak_vs_kr'] = []
    styled['strong_vs_kr'] = []
    styled['tip'] = None

    # ★ 헤더 승률/티어 통일: 표본 충분하면 메타 페이지와 동일한 실측(보정) 값 사용
    #   (메타 페이지의 primary_role 산출 로직과 동일하게 맞춰 두 페이지 수치 일치)
    _roles = CHAMPION_ROLE_MAP.get(canonical_id, [])
    if not _roles:
        _tags = CHAMP_TAGS.get(canonical_id, [])
        if "Marksman" in _tags: _roles = ["BOTTOM"]
        elif "Support" in _tags and "Fighter" not in _tags: _roles = ["UTILITY"]
        elif "Assassin" in _tags and "Fighter" not in _tags: _roles = ["MIDDLE"]
        elif "Mage" in _tags and "Fighter" not in _tags: _roles = ["MIDDLE"]
        else: _roles = ["TOP"]
    _meta_role = _roles[0]
    _real_stats, _real_bans = get_real_stats_map()
    _total = get_stats_total_games()
    _rr = _real_stats.get(canonical_id, {}).get(_meta_role)
    if _rr and _rr["games"] >= MIN_SAMPLE and _total > 0:
        _wr = smooth_wr(_rr["wins"], _rr["games"])
        _pr = round(_rr["games"] / _total * 100, 1)
        _br = round(_real_bans.get(canonical_id, 0) / _total * 100, 1)
        _nt = calc_meta_tier(_wr, _pr, _br)
        styled['wr'], styled['pr'], styled['br'], styled['tier'] = _wr, _pr, _br, _nt
        styled['tier_style'] = NUMERIC_TIER_COLOR.get(_nt, NUMERIC_TIER_COLOR["5"])
        styled['sample'], styled['source'] = _rr["games"], '실측'
    else:
        styled.setdefault('source', '예측')

    # ★ 이미지 깨짐 방지: 챔피언 id를 항상 DDragon 정식 id로 고정
    #   (CHAMPION_TIERS의 id 케이싱이 달라도 스플래시/아이콘 URL이 유효하도록)
    styled['id'] = canonical_id

    # 4) DDragon 실시간 스킬 데이터 (패시브 + QWER) — 모든 챔피언 공통
    skills = get_champion_detail(canonical_id)
    # 5) 실측 추천 빌드 (룬/스펠/아이템/스킬순서) — 수집 데이터 기반
    champ_build = get_champion_build(canonical_id, champ_role)
    # 6) 카운터 라인 맞대결 (실측 승률 기반)
    build_role = champ_build['role'] if champ_build else champ_role
    champ_counters = get_champion_counters(canonical_id, build_role)
    # 7) 에메랄드+ 고티어 운영법 (에디터 작성 콘텐츠)
    champ_guide = get_champion_guide(canonical_id)

    return render_template('index.html', page='champion', champ=styled,
                           champ_role=ROLE_KR.get(champ_role, champ_role),
                           champ_role_en=champ_role, has_build=has_build, skills=skills,
                           champ_build=champ_build, champ_counters=champ_counters, role_kr=ROLE_KR,
                           champ_guide=champ_guide, is_admin=is_admin(), canonical_id=canonical_id,
                           latest_version=LATEST_VERSION, current_patch=CURRENT_PATCH_DISPLAY)

@app.route('/admin/guide', methods=['GET', 'POST'])
def admin_guide():
    """에메랄드+ 운영법 작성/수정 (관리자 전용)."""
    if not is_admin():
        return redirect('/')
    champ_in = request.values.get('champ', '').strip()
    canonical = champ_ddragon_id(champ_in) if champ_in else ''
    if canonical not in CHAMP_KR_MAP:
        canonical = ''
    if request.method == 'POST' and canonical:
        save_champion_guide(canonical, request.form)
        return redirect(f'/admin/guide?champ={canonical}&saved=1')
    cur = get_guide_raw(canonical) if canonical else {}
    champ_list = sorted(CHAMP_KR_MAP.items(), key=lambda x: x[1])  # (id, kr) 가나다순
    return render_template('index.html', page='admin_guide',
                           champ_list=champ_list, sel_champ=canonical,
                           sel_kr=CHAMP_KR_MAP.get(canonical, ''), guide_phases=GUIDE_PHASES,
                           cur_guide=cur, saved=request.args.get('saved'),
                           latest_version=LATEST_VERSION)

@app.route('/search')
def search():
    ensure_current_patch()  # 새 패치 자동 감지·갱신 (스로틀)
    raw_name = request.args.get('name', '').strip()
    tag = request.args.get('tag', 'KR1').strip()
    queue = request.args.get('queue', 'all')
    
    split_chars = [",", "\n", "\r\n"]
    for char in split_chars:
        if char in raw_name:
            raw_users = [u.strip() for u in raw_name.replace('\n', ',').split(',') if u.strip()]
            if len(raw_users) > 1:
                multi_data = []
                for user_str in raw_users[:5]: 
                    if '#' in user_str:
                        parts = user_str.split('#')
                        u_name, u_tag = parts[0].strip(), parts[1].strip()
                    else:
                        u_name, u_tag = user_str, 'KR1'
                    data = get_multi_search_summary(u_name, u_tag)
                    if data: multi_data.append(data)
                return render_template('index.html', page='multi', multi_data=multi_data, latest_version=LATEST_VERSION)

    # 🏆 챔피언 이름을 검색하면 챔피언 메타/상세 페이지로 이동 (한글/영문 정확 일치)
    champ_hit = match_champion_query(raw_name)
    if champ_hit:
        return redirect(f'/champion/{champ_hit}')

    # 💡 데이터베이스 캐싱 키 생성
    cache_key = f"{raw_name.lower().replace(' ', '')}#{tag.lower()}#{queue}"
    current_time = int(time.time())
    
    # 1. DB에서 캐시 데이터 조회
    # ✅ MEDIUM 개선: db_read() 헬퍼 사용
    cached_json, updated_at = db_read(cache_key)
    if cached_json and current_time - updated_at < CACHE_EXPIRE_TIME:
        cache_data = json.loads(cached_json)
        cache_data['from_cache'] = True  # 프론트엔드에 캐시 데이터 유무 인지용 플래그
        return render_template('index.html', **cache_data)

    # 2. 캐시가 없거나 5분이 지났다면 라이엇 API 호출 실행 (캐시 미스!)
    try:
        acc_res = get_summoner_info(raw_name, tag)
    except Exception:
        # ✅ 버그 수정 1: alert() 대신 에러 파라미터와 함께 홈으로 리다이렉트
        return redirect(f'/?error=network')
    if acc_res.status_code != 200:
        return redirect(f'/?error=not_found&name={raw_name}')
    
    acc = acc_res.json()
    searched_puuid = acc['puuid']
    v4 = get_summoner_v4(searched_puuid)
    tier_text, tier_name = get_league_info_by_puuid(searched_puuid)
    
    live_game = get_live_game(searched_puuid)
    masteries = get_mastery(searched_puuid)
    
    matches, overall_radar, primary_role, secondary_role, win, most, overall_kda, deep_tags, _, game_count, top_recent_champs, win_count, lose_count, banner_champ, repeat_encounters, recent_team_luck = get_match_details(searched_puuid, 0, 20, queue, player_tier=tier_name)
    improvement_tips = generate_improvement_tips(matches, overall_kda, overall_radar, primary_role, tier_name)
    champion_recs = recommend_champions(overall_radar, primary_role)

    # ★ 모스트 챔피언 맞춤 패치 변화 (DDragon 버전 비교)
    patch_changes = []
    for tc in top_recent_champs[:5]:
        pc = get_champion_patch_changes(tc['img'])
        patch_changes.append({
            "img": tc['img'], "name": tc['name'],
            "changes": (pc.get('changes', []) if pc else []),
            "prev_patch": (pc.get('prev_patch') if pc else None),
        })

    grade_order = ['S+', 'S', 'A+', 'A', 'B+', 'B', 'C']
    grade_dist = {g: sum(1 for m in matches if m.get('grade') == g) for g in grade_order}

    streak_count, streak_type = 0, None
    for m in matches:
        result = 'win' if m.get('win') else 'lose'
        if streak_type is None:
            streak_type = result; streak_count = 1
        elif result == streak_type:
            streak_count += 1
        else:
            break

    # ★ Req2-A: 최근 게임 포지션 분포 (본인 데이터)
    pos_counts = {}
    for m in matches:
        r = m.get('role_en')
        if r in ROLE_KR:
            pos_counts[r] = pos_counts.get(r, 0) + 1
    pos_total = sum(pos_counts.values()) or 1
    position_dist = [{"role": r, "role_kr": ROLE_KR[r], "count": pos_counts[r],
                      "pct": round(pos_counts[r] / pos_total * 100)}
                     for r in ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"] if pos_counts.get(r, 0) > 0]
    position_dist.sort(key=lambda x: -x['count'])

    # ★ Req2-B: 자주 함께한 듀오 승률 (아군으로 2판 이상 동행)
    duo_stats = []
    for e in repeat_encounters:
        a = e.get('as_ally', 0)
        if a >= 2:
            w = e.get('as_ally_win', 0)
            duo_stats.append({"name": e['name'], "tag": e['tag'], "champ_img": e['champ_img'],
                              "champ_kr": e['champ_kr'], "games": a, "wins": w, "wr": round(w / a * 100)})
    duo_stats.sort(key=lambda x: (-x['games'], -x['wr']))
    duo_stats = duo_stats[:5]

    # 템플릿에 보낼 데이터를 딕셔너리로 구조화
    render_payload = {
        "page": 'search',
        "name": acc.get('gameName', raw_name), "tag": acc.get('tagLine', tag), "queue": queue,
        "profile_icon_id": v4.get('profileIconId', 627), "level": v4.get('summonerLevel', 1),
        "tier_text": tier_text, "tier_name": tier_name, 
        "latest_version": LATEST_VERSION, "matches": matches, "game_count": game_count,
        "radar_data": overall_radar, "primary_role": primary_role, "secondary_role": secondary_role, 
        "win_rate": win, "win_count": win_count, "lose_count": lose_count, "banner_champ": banner_champ,
        "most_played": most, "live_game": live_game, "masteries": masteries,
        "overall_kda": overall_kda, "deep_tags": deep_tags, "searched_puuid": searched_puuid,
        "top_recent_champs": top_recent_champs, "from_cache": False,
        "improvement_tips": improvement_tips, "champion_recs": champion_recs,
        "grade_dist": grade_dist, "grade_style_map": GRADE_STYLE,
        "streak_count": streak_count, "streak_type": streak_type,
        "repeat_encounters": repeat_encounters,
        "recent_team_luck": recent_team_luck,
        "position_dist": position_dist, "duo_stats": duo_stats,
        "patch_changes": patch_changes, "current_patch": CURRENT_PATCH_DISPLAY,
    }

    # 3. 새로운 데이터를 DB에 JSON 문자열 형태로 저장/갱신
    # ✅ MEDIUM 개선: db_write() 헬퍼 사용
    db_write(cache_key, render_payload, current_time)

    return render_template('index.html', **render_payload)

@app.route('/leaderboard')
def leaderboard():
    leaderboard_data, from_cache = get_challenger_leaderboard()
    return render_template('index.html', page='leaderboard', leaderboard=leaderboard_data, from_cache=from_cache)

@app.route('/champions')
def champions():
    return render_template('index.html', page='champions', all_champs=CHAMP_KR_MAP, latest_version=LATEST_VERSION)

TIER_IMG_MAP = {
    "아이언": "iron", "브론즈": "bronze", "실버": "silver", "골드": "gold",
    "플래티넘": "platinum", "에메랄드": "emerald", "다이아몬드": "diamond",
    "마스터": "master", "그랜드마스터": "grandmaster", "챌린저": "challenger",
}
DUO_ROLES = ["전체", "탑", "정글", "미드", "원딜", "서포터"]
DUO_QUEUES = ["솔로랭크", "자유랭크", "일반", "칼바람"]

def _time_ago(ts):
    diff = int(time.time()) - ts
    if diff < 60: return "방금 전"
    if diff < 3600: return f"{diff // 60}분 전"
    if diff < 86400: return f"{diff // 3600}시간 전"
    return f"{diff // 86400}일 전"

@app.route('/duo')
def duo():
    posts = []
    try:
        conn = db_connect()
        rows = conn.execute("""SELECT name, tag, tier, my_role, find_role, queue_type, mic, message, created_at
                               FROM duo_posts ORDER BY created_at DESC LIMIT 50""").fetchall()
        conn.close()
        for r in rows:
            posts.append({
                "name": r[0], "tag": r[1], "tier": r[2], "img": TIER_IMG_MAP.get(r[2], "unranked"),
                "my_role": r[3], "find_role": r[4], "queue_type": r[5], "mic": r[6],
                "message": r[7], "time": _time_ago(r[8]),
            })
    except Exception as e:
        print(f"듀오 목록 조회 에러: {e}")
    return render_template('index.html', page='duo', duo_posts=posts,
                           tier_options=list(TIER_IMG_MAP.keys()),
                           role_options=DUO_ROLES, queue_options=DUO_QUEUES)

@app.route('/duo/create', methods=['POST'])
def duo_create():
    f = request.form
    name = (f.get('name') or '').strip()
    tag = (f.get('tag') or 'KR1').strip().lstrip('#')
    tier = (f.get('tier') or '').strip()
    my_role = (f.get('my_role') or '전체').strip()
    find_role = (f.get('find_role') or '전체').strip()
    queue_type = (f.get('queue_type') or '솔로랭크').strip()
    mic = (f.get('mic') or '상관없음').strip()
    message = (f.get('message') or '').strip()[:200]

    if not name or not tier or not message:
        return redirect('/duo?error=missing')
    if tier not in TIER_IMG_MAP:
        tier = "언랭"
    try:
        conn = db_connect()
        conn.execute("""INSERT INTO duo_posts (name, tag, tier, my_role, find_role, queue_type, mic, message, created_at)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                     (name, tag, tier, my_role, find_role, queue_type, mic, message, int(time.time())))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"듀오 글 작성 에러: {e}")
        return redirect('/duo?error=save')
    return redirect('/duo')

@app.route('/spectate')
def spectate():
    spectate_games, from_cache = get_live_challenger_games()
    return render_template('index.html', page='spectate', spectate_games=spectate_games, latest_version=LATEST_VERSION, from_cache=from_cache)

@app.route('/privacy')
def privacy():
    return render_template('index.html', page='privacy', current_patch=CURRENT_PATCH_DISPLAY)

@app.route('/riot.txt')
def riot_txt():
    # Riot Production API Key 사이트 소유권 검증 토큰
    return "c49dcec5-23e6-494c-b5d1-69f5e4d09a8a", 200, {'Content-Type': 'text/plain'}

@app.route('/more_matches')
def more_matches():
    puuid = request.args.get('puuid')
    start = int(request.args.get('start', 20))
    queue = request.args.get('queue', 'all')
    matches, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _ = get_match_details(puuid, start, 20, queue, collect_stats=False)
    return render_template('index.html', page='search', matches=matches, ajax=True, searched_puuid=puuid, latest_version=LATEST_VERSION)

@app.route('/growth')
def growth():
    """과거(21~40게임) vs 현재(최근 20게임) 레이더 비교 + 추세 피드백. AJAX 전용."""
    puuid = request.args.get('puuid', '')
    queue = request.args.get('queue', 'all')
    try:
        present = [float(x) for x in request.args.get('present', '').split(',') if x != '']
    except ValueError:
        present = []
    if not puuid or len(present) != 6:
        return jsonify({"ok": False, "msg": "잘못된 요청"}), 400

    res = get_match_details(puuid, 20, 20, queue)  # 21~40번째 게임
    past = res[1]  # overall_radar
    past_count = res[9]
    if not past or past_count < 3:
        return jsonify({"ok": False, "msg": "비교할 과거 전적이 부족합니다 (20게임 이상 필요)."})

    labels = ['전투', '성장', '시야', '생존', '오브젝트', '합류']
    trend = []
    deltas = []
    for i in range(6):
        pv, cv = past[i], present[i]
        delta = round(cv - pv)
        pct = round((cv - pv) / pv * 100) if pv > 0 else 0
        deltas.append(pct)
        direction = "up" if delta > 1 else ("down" if delta < -1 else "flat")
        trend.append({"axis": labels[i], "delta": delta, "pct": pct, "dir": direction})
    avg = round(sum(deltas) / 6)
    return jsonify({"ok": True, "past": past, "present": present, "trend": trend,
                    "summary_pct": avg, "past_count": past_count})

@app.route('/feedback', methods=['POST'])
def feedback():
    """고객의 소리 접수. JSON 또는 폼 데이터 모두 허용."""
    data = request.get_json(silent=True) or request.form
    content = (data.get('content') or '').strip()
    category = (data.get('category') or '기타').strip()
    contact = (data.get('contact') or '').strip()
    # 로그인 회원이면 자동 연동, 아니면 폼 값 사용
    user_ref = session.get('username') or (data.get('user_ref') or '').strip()

    if not content:
        return jsonify({"ok": False, "msg": "내용을 입력해주세요."}), 400
    if len(content) > 2000:
        content = content[:2000]

    try:
        conn = db_connect()
        conn.execute(
            "INSERT INTO feedback (category, content, contact, user_ref, created_at) VALUES (?, ?, ?, ?, ?)",
            (category, content, contact, user_ref, int(time.time()))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"피드백 저장 에러: {e}")
        return jsonify({"ok": False, "msg": "저장 중 오류가 발생했습니다."}), 500

    return jsonify({"ok": True, "msg": "소중한 의견 감사합니다!"})


# ═══════════════════════════════════════════════════════════════════════
#  🧠 AI 인게임 코치 — 초개인화 코칭 리포트 (킬러 기능)
#     현재: 실제 매치 요약값을 시드로 '가상 타임라인' 생성 → LLM(또는 데모) 코칭.
#     프로덕션 키/타임라인 연동 시 generate_mock_timeline() 만 실제 파서로 교체.
# ═══════════════════════════════════════════════════════════════════════

def generate_mock_timeline(summary):
    """실제 매치 요약(K/D/A·CS·킬관여·챔피언·승패)을 시드로 그럴듯한 '가상 타임라인'을 생성.
    ⚠️ death/kill 타임스탬프는 합성값(데모 시연용). 실측 판단(데모 리포트)은 집계 수치만 사용.
    프로덕션 키 발급 후 match-v5 timeline 파서로 이 함수만 교체하면 파이프라인이 그대로 실측 전환됨."""
    k = int(summary.get('k', 0) or 0); d = int(summary.get('d', 0) or 0); a = int(summary.get('a', 0) or 0)
    cs = int(summary.get('cs', 0) or 0); kp = int(summary.get('kp', 0) or 0)
    cspm = float(summary.get('cspm', 0) or 0)
    win = bool(summary.get('win', False))
    role = summary.get('role_en') or 'MIDDLE'
    dur = int(summary.get('duration_min', 28) or 28)
    rng = random.Random(f"{summary.get('champ')}|{k}|{d}|{a}|{cs}|{kp}|{win}")

    deaths = sorted(rng.randint(3 * 60, dur * 60) for _ in range(d))
    kills = sorted(rng.randint(2 * 60, dur * 60) for _ in range(k))
    cs10 = round(cspm * 10 * rng.uniform(0.82, 0.98)) if cspm else round(cs * (10 / max(dur, 1)))
    gd15 = int((k - d) * 250 + rng.randint(-600, 600) + (400 if win else -400))
    vspm = round(rng.uniform(0.6, 1.4) + (0.6 if role == 'UTILITY' else 0), 2)
    control_wards = rng.randint(0, 2) + (2 if role == 'UTILITY' else 0)
    early_deaths = sum(1 for t in deaths if t <= 14 * 60)

    return {
        "champion": summary.get('champ_kr') or summary.get('champ') or '알 수 없음',
        "role": role, "result": "승리" if win else "패배", "game_duration_min": dur,
        "kda": {"kills": k, "deaths": d, "assists": a, "kda_ratio": round((k + a) / max(1, d), 2)},
        "kill_participation_pct": kp,
        "cs_total": cs, "cs_per_min": round(cspm, 1), "cs_at_10min": cs10,
        "gold_diff_at_15min": gd15, "vision_per_min": vspm, "control_wards_placed": control_wards,
        "early_deaths_before_14min": early_deaths,
        "death_timestamps_sec": deaths, "kill_timestamps_sec": kills,
        "_source": "mock",  # 실측 타임라인 연동 시 'riot'
    }


COACH_SYSTEM_PROMPT = (
    "너는 리그 오브 레전드 챌린저 전문 코치다. 반드시 아래 규칙을 지켜라.\n"
    "1. 오직 <MATCH_DATA>에 주어진 수치에만 근거해 분석한다. 데이터에 없는 사실을 지어내지 마라.\n"
    "2. 존재하지 않는 아이템·룬·챔피언·패치 내용을 언급하지 마라. 특정 아이템명을 확신할 수 없으면 '코어 아이템/방어 아이템'처럼 범주로만 말하라.\n"
    "3. 핵심 문제점은 '정확히 1가지'만 짚는다. 데이터에서 가장 뚜렷한 약점을 골라라(예: 초반 데스 과다, 분당 CS 저조, 라인전 골드 열세, 시야 부족, 킬 관여 저조).\n"
    "4. 그 문제에 대한 '구체적이고 실행 가능한 개선책 1가지'를 제시한다. 다음 게임에서 바로 적용할 행동 지침으로.\n"
    "5. 근거 없는 칭찬·일반론('열심히 하세요')을 금지한다. 반드시 데이터의 수치를 인용하라.\n"
    "6. 한국어로, 친근하지만 전문적인 코치 말투로 작성한다.\n"
    "7. 반드시 아래 JSON 스키마로만 응답한다. JSON 외 텍스트·마크다운·코드펜스 금지.\n"
    '{"headline":"한 줄 총평(25자 내외)",'
    '"problem":{"title":"핵심 문제 제목","detail":"수치를 인용한 2~3문장"},'
    '"solution":{"title":"개선책 제목","detail":"다음 게임 실행 지침 2~3문장"},'
    '"stat_highlight":"가장 문제되는 핵심 수치 하나(예: 14분 내 데스 4회)"}'
)


def build_coach_prompt(timeline):
    """LLM 전달용 (system, user) 프롬프트 구성."""
    system = COACH_SYSTEM_PROMPT
    user = ("다음은 분석 대상 1게임의 데이터다.\n<MATCH_DATA>\n"
            + json.dumps(timeline, ensure_ascii=False, indent=2)
            + "\n</MATCH_DATA>\n위 데이터만 근거로, 규칙에 따라 JSON 코칭 리포트를 작성하라.")
    return system, user


def _demo_coach_report(tl):
    """LLM 없이도 UI/UX를 100% 검증할 수 있는 결정적 데모 리포트.
    ⚠️ 신뢰 원칙: 합성 타임스탬프가 아닌 '실제 집계 수치'(데스·분당CS·킬관여·KDA·승패)에만 근거."""
    kda = tl['kda']; d = kda['deaths']; ratio = kda['kda_ratio']
    cspm = tl['cs_per_min']; kp = tl['kill_participation_pct']; res = tl['result']
    if d >= 8 or (ratio < 1.5 and d >= 6):
        prob = {"title": "데스 관리 실패", "detail": f"이번 게임 {d}데스로 KDA {ratio}를 기록했습니다. 사망 1회는 골드·경험치 손실은 물론 오브젝트를 상대에게 내주는 빌미가 됩니다."}
        sol = {"title": "무리한 진입 대신 생존 우선", "detail": "상대 정글 위치가 미확인일 때는 라인을 강하게 밀지 말고, 갱 회피 동선을 먼저 확보하세요. 애매한 교전은 한 발 빼는 판단이 KDA를 지킵니다."}
        hi = f"{d}데스 · KDA {ratio}"
    elif cspm and cspm < 6:
        prob = {"title": "분당 CS 저조", "detail": f"분당 CS가 {cspm}개로 성장 자원이 부족했습니다. 후반 캐리력이 떨어지는 직접적 원인입니다."}
        sol = {"title": "귀환·로밍 후 CS 회수 습관화", "detail": "귀환 직전 마지막 웨이브를 밀어 넣고, 복귀 후 놓친 CS를 최우선 회수하세요. 로밍 후에도 사이드 웨이브를 잊지 말고, 목표는 분당 7개 이상입니다."}
        hi = f"분당 CS {cspm}"
    elif kp < 45:
        prob = {"title": "킬 관여 저조", "detail": f"킬 관여율이 {kp}%로 팀 교전 기여가 낮았습니다. 사이드에 고립되어 합류가 늦었을 가능성이 큽니다."}
        sol = {"title": "오브젝트 30초 전 합류", "detail": "드래곤·전령 생성 30초 전에는 사이드 정리를 마치고 합류 동선을 잡으세요. 미니맵을 자주 확인해 팀 교전 신호를 놓치지 마세요."}
        hi = f"킬 관여 {kp}%"
    elif ratio >= 4:
        prob = {"title": "강점 유지 — 다음 단계는 주도권 전환", "detail": f"KDA {ratio}, 킬 관여 {kp}%로 매우 안정적인 판이었습니다. 이제 잘 큰 이득을 '오브젝트/타워'로 환산하는 단계가 남았습니다."}
        sol = {"title": "이득을 스노우볼로", "detail": "킬·라인 우위를 얻은 직후 타워 압박이나 상대 정글 침투로 맵 주도권을 넓히세요. 개인 KDA를 넘어 팀 자원 격차를 만드는 것이 승률을 올립니다."}
        hi = f"KDA {ratio} · 킬관여 {kp}%"
    else:
        prob = {"title": "라인전 주도권 부족", "detail": f"{res}한 게임이지만 KDA {ratio}, 킬 관여 {kp}%로 결정적 주도권을 만들지는 못했습니다. 무난했지만 캐리로 이어지진 않았습니다."}
        sol = {"title": "정글 동선 읽고 능동적 플레이", "detail": "상대 정글이 반대 사이드에 보이면 라인 우선권을 살려 로밍하거나 상대 정글을 침범해 격차를 만드세요. 수동적으로 파밍만 하지 말고 이니시 각을 찾으세요."}
        hi = f"KDA {ratio} · 킬관여 {kp}%"
    return {
        "headline": f"{res} · {prob['title']}",
        "problem": prob, "solution": sol, "stat_highlight": hi, "_demo": True,
    }


def call_llm_coach(timeline):
    """실제 LLM 호출 또는 데모 폴백. (report_dict, is_live) 반환."""
    if not (AI_COACH_LIVE and ANTHROPIC_API_KEY):
        return _demo_coach_report(timeline), False
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        system, user = build_coach_prompt(timeline)
        msg = client.messages.create(
            model=AI_COACH_MODEL, max_tokens=700, temperature=0.4,
            system=system, messages=[{"role": "user", "content": user}],
        )
        raw = msg.content[0].text.strip()
        m = re.search(r'\{.*\}', raw, re.S)  # 코드펜스 등 방어적 JSON 추출
        if not m:
            return _demo_coach_report(timeline), False
        report = json.loads(m.group(0))
        report["_demo"] = False
        return report, True
    except Exception as e:
        print(f"AI 코치 LLM 에러: {e}")
        return _demo_coach_report(timeline), False


@app.route('/api/ai_coach', methods=['POST'])
def api_ai_coach():
    if not AI_COACH_ENABLED:
        return jsonify({"ok": False, "msg": "AI 코치 기능이 비활성화되어 있습니다."}), 403
    data = request.get_json(silent=True) or {}
    summary = {
        "champ": (data.get("champ") or "")[:40], "champ_kr": (data.get("champ_kr") or "")[:40],
        "k": data.get("k", 0), "d": data.get("d", 0), "a": data.get("a", 0),
        "kp": data.get("kp", 0), "cs": data.get("cs", 0), "cspm": data.get("cspm", 0),
        "win": bool(data.get("win", False)), "role_en": (data.get("role") or "")[:12],
        "duration_min": data.get("dur", 28),
    }
    sig = hashlib.md5(json.dumps(summary, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    live_tag = "live" if (AI_COACH_LIVE and ANTHROPIC_API_KEY) else "demo"
    cache_key = f"aicoach#{live_tag}#{sig}"
    now = int(time.time())
    cached, ts = db_read(cache_key)
    if cached and (now - ts) < AI_COACH_CACHE_TTL:
        try:
            return jsonify({"ok": True, "cached": True, **json.loads(cached)})
        except Exception:
            pass
    timeline = generate_mock_timeline(summary)
    report, is_live = call_llm_coach(timeline)
    payload = {"report": report, "is_live": is_live, "data_source": timeline.get("_source", "mock")}
    db_write(cache_key, payload, now)
    return jsonify({"ok": True, "cached": False, **payload})


# ================= 에러 페이지 =================
@app.errorhandler(404)
def page_not_found(e):
    return render_template(
        'index.html', page='error',
        error_code=404,
        error_title='페이지를 찾을 수 없습니다',
        error_desc='주소가 잘못되었거나 삭제된 페이지일 수 있습니다.'
    ), 404


@app.errorhandler(500)
def internal_error(e):
    return render_template(
        'index.html', page='error',
        error_code=500,
        error_title='일시적인 오류가 발생했습니다',
        error_desc='잠시 후 다시 시도해 주세요. 문제가 계속되면 피드백으로 알려주세요.'
    ), 500


if __name__ == '__main__':
    app.run(debug=True, port=5000)