import os
import random
import hashlib
import sqlite3
import json
import time
import re
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
            champ = p.get('championName', '')
            if not champ:
                continue
            win = 1 if p.get('win') else 0
            cur.execute("""INSERT INTO champion_stats (champ_en, role, games, wins) VALUES (?,?,1,?)
                           ON CONFLICT(champ_en, role) DO UPDATE SET games=games+1, wins=wins+?""",
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
                               ON CONFLICT(champ_en, role, page) DO UPDATE SET games=games+1, wins=wins+?""",
                            (champ, role, page, styles[0]['style'], styles[1]['style'], win, win))
            except (KeyError, IndexError, TypeError):
                pass

            # 소환사 주문 (정렬된 조합)
            s1, s2 = p.get('summoner1Id'), p.get('summoner2Id')
            if s1 and s2:
                combo = "-".join(sorted([str(s1), str(s2)]))
                cur.execute("""INSERT INTO build_spells (champ_en, role, spells, games, wins) VALUES (?,?,?,1,?)
                               ON CONFLICT(champ_en, role, spells) DO UPDATE SET games=games+1, wins=wins+?""",
                            (champ, role, combo, win, win))

            # 최종 아이템 → 코어/신발 분리 빈도 집계
            for i in range(6):
                iid = str(p.get(f'item{i}', 0))
                if iid == '0':
                    continue
                if iid in BOOTS_ITEMS:
                    cur.execute("""INSERT INTO build_boots (champ_en, role, item_id, games, wins) VALUES (?,?,?,1,?)
                                   ON CONFLICT(champ_en, role, item_id) DO UPDATE SET games=games+1, wins=wins+?""",
                                (champ, role, iid, win, win))
                elif iid in CORE_ITEMS:
                    cur.execute("""INSERT INTO build_items (champ_en, role, item_id, games, wins) VALUES (?,?,?,1,?)
                                   ON CONFLICT(champ_en, role, item_id) DO UPDATE SET games=games+1, wins=wins+?""",
                                (champ, role, iid, win, win))

        # 카운터 라인 맞대결 (같은 라인 양 팀 챔피언 대결)
        by_role = {}
        for p in info.get('participants', []):
            r = p.get('teamPosition', '')
            if r in VALID_ROLES and p.get('championName'):
                by_role.setdefault(r, []).append((p['championName'], p.get('teamId'), 1 if p.get('win') else 0))
        for r, plist in by_role.items():
            if len(plist) == 2 and plist[0][1] != plist[1][1]:
                (ca, _, wa), (cb, _, wb) = plist
                cur.execute("""INSERT INTO build_matchups (champ_en, role, opponent, games, wins) VALUES (?,?,?,1,?)
                               ON CONFLICT(champ_en, role, opponent) DO UPDATE SET games=games+1, wins=wins+?""",
                            (ca, r, cb, wa, wa))
                cur.execute("""INSERT INTO build_matchups (champ_en, role, opponent, games, wins) VALUES (?,?,?,1,?)
                               ON CONFLICT(champ_en, role, opponent) DO UPDATE SET games=games+1, wins=wins+?""",
                            (cb, r, ca, wb, wb))
        # 밴
        for team in info.get('teams', []):
            for ban in team.get('bans', []):
                cid = str(ban.get('championId', -1))
                champ = CHAMP_KEYS.get(cid)
                if not champ:
                    continue
                cur.execute("""INSERT INTO champion_bans (champ_en, bans) VALUES (?,1)
                               ON CONFLICT(champ_en) DO UPDATE SET bans=bans+1""", (champ,))
        # 총 경기 수 증가 + 처리 완료 기록
        cur.execute("""INSERT INTO stats_meta (key, value) VALUES ('total_games', '1')
                       ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT)""")
        cur.execute("INSERT INTO processed_matches (match_id, processed_at) VALUES (?,?) ON CONFLICT(match_id) DO NOTHING",
                    (match_id, int(time.time())))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
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
                pmeta[pid] = (p['championName'], role, 1 if p.get('win') else 0)

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
                               ON CONFLICT(champ_en, role, skill_order) DO UPDATE SET games=games+1, wins=wins+?""",
                            (champ, role, order_str, win, win))
            # 레벨별 스킬 (lol.ps 스타일 18레벨 트리)
            for lvl, slot in enumerate(full, start=1):
                cur.execute("""INSERT INTO build_skill_levels (champ_en, role, lvl, slot, games, wins) VALUES (?,?,?,?,1,?)
                               ON CONFLICT(champ_en, role, lvl, slot) DO UPDATE SET games=games+1, wins=wins+?""",
                            (champ, role, lvl, slot, win, win))
            # 코어 아이템 구매 순서: 앞 4개 (4코어 타임라인)
            if item_seq[pid]:
                seqstr = "-".join(item_seq[pid][:4])
                cur.execute("""INSERT INTO build_item_order (champ_en, role, seq, games, wins) VALUES (?,?,?,1,?)
                               ON CONFLICT(champ_en, role, seq) DO UPDATE SET games=games+1, wins=wins+?""",
                            (champ, role, seqstr, win, win))
            # 시작 아이템 세트
            if start_items[pid]:
                sset = "-".join(sorted(start_items[pid]))
                cur.execute("""INSERT INTO build_starts (champ_en, role, items, games, wins) VALUES (?,?,?,1,?)
                               ON CONFLICT(champ_en, role, items) DO UPDATE SET games=games+1, wins=wins+?""",
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
        ALT_MIN = 3  # 고승률 변형 최소 표본
        WR = "(CAST(wins AS REAL)/games) DESC, games DESC"

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
                "games": games, "wr": round(wins / games * 100) if games else 0,
            }
        def spell_view(row):
            if not row:
                return None
            spells = [{"icon": SPELL_MAP.get(s), "name": SPELL_NAME.get(SPELL_MAP.get(s), "")}
                      for s in row[0].split("-") if SPELL_MAP.get(s)]
            return {"list": spells, "games": row[1], "wr": round(row[2] / row[1] * 100) if row[1] else 0}
        def boots_view(row):
            if not row:
                return None
            return {"id": row[0], "name": ITEM_NAME.get(row[0], ""), "games": row[1],
                    "wr": round(row[2] / row[1] * 100) if row[1] else 0}
        def start_view(row):
            if not row:
                return None
            return {"list": [{"id": i, "name": ITEM_NAME.get(i, "")} for i in row[0].split("-")],
                    "games": row[1], "wr": round(row[2] / row[1] * 100) if row[1] else 0}

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
                           "wr": round(it[2] / it[1] * 100) if it[1] else 0, "games": it[1]} for it in items]
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
                "wr": round(grp["wins"] / grp["games"] * 100) if grp["games"] else 0})
        # 스킬 마스터 순서(우선순위)
        sk = cur.execute("""SELECT skill_order, games, wins FROM build_skills
                            WHERE champ_en=? AND role=? ORDER BY games DESC LIMIT 1""", (champ_en, role)).fetchone()
        if sk:
            build["skill_order"] = {"order": sk[0].split(">"), "games": sk[1], "wr": round(sk[2] / sk[1] * 100) if sk[1] else 0}
        # 레벨별 스킬트리 (lol.ps 스타일) — 각 레벨에서 가장 많이 찍은 슬롯
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

def get_champion_counters(champ_en, role, min_games=3):
    """라인 맞대결 승률 기반 카운터(취약)/유리 상대 산출."""
    try:
        conn = db_connect()
        rows = conn.execute("""SELECT opponent, games, wins FROM build_matchups
                               WHERE champ_en=? AND role=? AND games>=? ORDER BY games DESC""",
                            (champ_en, role, min_games)).fetchall()
        conn.close()
        matchups = [{"id": r[0], "kr": CHAMP_KR_MAP.get(r[0], r[0]),
                     "games": r[1], "wr": round(r[2] / r[1] * 100)} for r in rows]
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

def load_ddragon():
    """DDragon 최신 데이터를 전부 (재)로드. 새 패치 감지 시 ensure_current_patch에서 재호출."""
    global LATEST_VERSION, PREV_VERSION, CURRENT_PATCH, CHAMP_KR_MAP, CHAMP_KEYS, CHAMP_TAGS
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
    패치 버전을 캐시 키에 포함 → 패치 변경 시 자동 갱신."""
    cache_key = f"champdetail#{champ_id}#{LATEST_VERSION}"
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
    cache_key = f"patchdiff#{champ_en}#{LATEST_VERSION}"
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
                # 쿨다운
                cc, pc = sp.get('cooldownBurn'), ps_sp.get('cooldownBurn')
                if cc and pc and cc != pc:
                    changes.append({"type": "buff" if _first_num(cc) < _first_num(pc) else "nerf",
                                    "text": f"{slot} 쿨다운 {pc} → {cc}초"})
                # 코스트
                ck, pk = sp.get('costBurn'), ps_sp.get('costBurn')
                if ck and pk and ck != pk and ck not in ('0', 'No Cost'):
                    changes.append({"type": "buff" if _first_num(ck) < _first_num(pk) else "nerf",
                                    "text": f"{slot} 소모값 {pk} → {ck}"})
                # 효과 수치(데미지 등) 변경 — 방향 단정 어려워 중립 표기
                if sp.get('effectBurn') != ps_sp.get('effectBurn'):
                    changes.append({"type": "adjust", "text": f"{slot} 스킬 효과 수치 변경"})
            # 패시브 설명 변경
            if cur_d.get('passive', {}).get('description') != prev_d.get('passive', {}).get('description'):
                changes.append({"type": "adjust", "text": "패시브 변경"})
        result = {"changes": changes, "prev_patch": ".".join(PREV_VERSION.split(".")[:2])}
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

def get_match_details(puuid, start=0, count=20, queue=None, collect_stats=True):
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

                champ_en = p['championName']
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
                                             'count': 0, 'as_ally': 0, 'as_enemy': 0}
                ct = coplayer_tracker[key]
                ct['count'] += 1
                ct['name'], ct['tag'] = cp['name'], cp['tag']  # 최신 닉네임 갱신
                ct['champ_img'], ct['champ_kr'] = cp['champ_img'], cp['champ_kr']
                if cp['teamId'] == my_team_id:
                    ct['as_ally'] += 1
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

    return matches, overall_radar, primary_role, secondary_role, win_rate, most, overall_kda, deep_tags, puuid, len(matches), top_recent_champs, win_count, lose_count, banner_champ, repeat_encounters

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
                    c_en = p['championName']
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

def generate_improvement_tips(matches, overall_kda, radar_array):
    tips = []
    avg_d = overall_kda.get('d', 0)
    if avg_d >= 6:
        tips.append({"icon": "💀", "type": "danger",
            "title": f"평균 {avg_d}데스 — 생존력 개선 필요",
            "desc": "라인전과 교전 시 과감한 진입보다 포지셔닝을 우선시하세요."})
    if radar_array and len(radar_array) > 2 and radar_array[2] < 38:
        tips.append({"icon": "👁️", "type": "warning",
            "title": "시야 기여도 낮음",
            "desc": "귀환마다 제어 와드를 구매하고, 리콜 전 강·정글 시야를 꼭 확인하세요."})
    if radar_array and len(radar_array) > 5 and radar_array[5] < 38:
        tips.append({"icon": "📍", "type": "warning",
            "title": "팀파이트 합류율 낮음",
            "desc": "미니맵으로 팀원의 교전 신호를 확인하고 로밍 타이밍을 개선하세요."})
    kda_ratio = overall_kda.get('ratio', 0)
    if kda_ratio >= 4.0:
        tips.append({"icon": "🔥", "type": "good",
            "title": f"KDA 상위권 — {kda_ratio}:1",
            "desc": "현재 플레이 패턴이 효율적입니다. 이 챔피언/포지션 조합을 유지하세요!"})
    if matches:
        recent_5 = matches[:5]
        rwr = sum(1 for m in recent_5 if m.get('win')) / len(recent_5) * 100
        if rwr >= 60:
            tips.append({"icon": "📈", "type": "good",
                "title": f"최근 폼 상승 ({rwr:.0f}% 승률)",
                "desc": "최근 5게임 흐름이 좋습니다. 현재 챔피언 풀을 유지하세요!"})
        elif rwr <= 30:
            tips.append({"icon": "📉", "type": "danger",
                "title": f"최근 폼 하강 ({rwr:.0f}% 승률)",
                "desc": "최근 흐름이 좋지 않습니다. 주력 챔피언이나 라인을 변경해보세요."})
    if not tips:
        tips.append({"icon": "⚖️", "type": "neutral",
            "title": "전반적으로 안정적인 플레이",
            "desc": "큰 약점 없이 균형 잡힌 플레이를 하고 있습니다. 세부 스탯을 꾸준히 개선하세요."})
    return tips[:3]

def recommend_champions(radar_array):
    if not radar_array or sum(radar_array) == 0: return []
    labels = ['전투', '성장', '시야', '생존', '오브젝트', '합류']
    top_stat = labels[radar_array.index(max(radar_array))]
    # ✅ MEDIUM 개선: 하드코딩 pool 대신 CHAMPION_TIERS + META_STATS에서 동적으로 추천
    # 각 스탯 축에 어울리는 역할군 매핑
    stat_to_roles = {
        '전투':     ['JUNGLE', 'MIDDLE', 'TOP'],
        '성장':     ['BOTTOM', 'MIDDLE', 'JUNGLE'],
        '시야':     ['UTILITY', 'JUNGLE'],
        '생존':     ['TOP', 'UTILITY', 'JUNGLE'],
        '오브젝트': ['JUNGLE', 'TOP', 'UTILITY'],
        '합류':     ['MIDDLE', 'UTILITY', 'TOP'],
    }
    target_roles = stat_to_roles.get(top_stat, ['TOP', 'JUNGLE', 'MIDDLE'])

    candidates = []
    for role in target_roles:
        for c in CHAMPION_TIERS.get(role, []):
            if c.get('tier') in ('S', 'A') and c.get('id'):
                candidates.append({
                    "id": c['id'],
                    "kr": CHAMP_KR_MAP.get(c['id'], c['id']),
                    "role": ROLE_MAP.get(role, role),
                    "reason": f"'{top_stat}' 지향 플레이어에게 추천"
                })

    # CHAMPION_TIERS에 충분한 데이터가 없으면 기존 풀로 폴백
    if len(candidates) < 3:
        fallback_pool = {
            '전투':     [("Darius","다리우스","탑"), ("LeeSin","리 신","정글"), ("Zed","제드","미드")],
            '성장':     [("Viego","비에고","정글"), ("Viktor","빅토르","미드"), ("Vayne","베인","원딜")],
            '시야':     [("Thresh","쓰레쉬","서포터"), ("Karma","카르마","서포터"), ("LeeSin","리 신","정글")],
            '생존':     [("Malphite","말파이트","탑"), ("Warwick","워윅","정글"), ("Lulu","룰루","서포터")],
            '오브젝트': [("Amumu","아무무","정글"), ("Nautilus","노틸러스","서포터"), ("Aatrox","아트록스","탑")],
            '합류':     [("Malphite","말파이트","탑"), ("Ahri","아리","미드"), ("Thresh","쓰레쉬","서포터")],
        }
        candidates = [{"id": c[0], "kr": c[1], "role": c[2], "reason": f"'{top_stat}' 지향 플레이어에게 추천"}
                      for c in fallback_pool.get(top_stat, [])]

    return candidates[:3]

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

# ================= 회원 인증 =================
@app.context_processor
def inject_user():
    """모든 템플릿에서 current_user 사용 가능하도록 주입."""
    if session.get('user_id'):
        return {'current_user': {'id': session['user_id'], 'username': session.get('username'),
                                 'riot_name': session.get('riot_name'), 'riot_tag': session.get('riot_tag')}}
    return {'current_user': None}

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
@app.route('/')
def index():
    meta_top = []
    for role in ['TOP', 'JUNGLE', 'MIDDLE', 'BOTTOM', 'UTILITY']:
        s_champs = [c for c in CHAMPION_TIERS.get(role, []) if c['tier'] == 'S']
        if s_champs:
            c = s_champs[0]
            meta_top.append({**c, 'role_kr': ROLE_KR.get(role, role),
                              'tier_style': TIER_COLOR.get(c['tier'], TIER_COLOR['B'])})
    return render_template('index.html', page='home', roster_data=GLOBAL_ROSTER_DATA,
                           pro_gamers=PRO_GAMERS, latest_version=LATEST_VERSION,
                           meta_top=meta_top, current_patch=CURRENT_PATCH)

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
            wr = round(real_role["wins"] / sample * 100, 1)
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
                           latest_version=LATEST_VERSION, current_patch=CURRENT_PATCH,
                           role_kr=ROLE_KR, rank_tier=rank_tier,
                           rank_tier_labels=RANK_TIER_LABELS,
                           numeric_tier_color=NUMERIC_TIER_COLOR,
                           tier_counts=tier_counts,
                           total_games=get_stats_total_games(), real_count=real_count)

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
                if m_json.get('info', {}).get('queueId', 0) in (420, 440):
                    tl = riot_get(f"https://asia.api.riotgames.com/lol/match/v5/matches/{m_id}/timeline")
                    if tl.status_code == 200:
                        record_timeline_stats(tl.json(), m_json, m_id)
    except Exception:
        import traceback
        return "<pre>COLLECT ERROR:\n" + traceback.format_exc() + "</pre>", 500
    try:
        total = get_stats_total_games()
    except Exception:
        import traceback
        return "<pre>TOTAL ERROR:\n" + traceback.format_exc() + "</pre>", 500
    return jsonify({"tier": tier, "collected_new_matches": collected,
                    "scanned_players": scanned, "total_games": total})

@app.route('/champion/<champ_id>')
def champion_page(champ_id):
    ensure_current_patch()  # 새 패치 자동 감지·갱신 (스로틀)
    # 1) DDragon 정식 챔피언 ID로 정규화 (대소문자 보정)
    canonical_id = next((cid for cid in CHAMP_KR_MAP if cid.lower() == champ_id.lower()), None)
    if not canonical_id and champ_id in CHAMP_KR_MAP:
        canonical_id = champ_id
    if not canonical_id:
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

    # 4) DDragon 실시간 스킬 데이터 (패시브 + QWER) — 모든 챔피언 공통
    skills = get_champion_detail(canonical_id)
    # 5) 실측 추천 빌드 (룬/스펠/아이템/스킬순서) — 수집 데이터 기반
    champ_build = get_champion_build(canonical_id, champ_role)
    # 6) 카운터 라인 맞대결 (실측 승률 기반)
    build_role = champ_build['role'] if champ_build else champ_role
    champ_counters = get_champion_counters(canonical_id, build_role)

    return render_template('index.html', page='champion', champ=styled,
                           champ_role=ROLE_KR.get(champ_role, champ_role),
                           champ_role_en=champ_role, has_build=has_build, skills=skills,
                           champ_build=champ_build, champ_counters=champ_counters, role_kr=ROLE_KR,
                           latest_version=LATEST_VERSION, current_patch=CURRENT_PATCH)

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
    
    matches, overall_radar, primary_role, secondary_role, win, most, overall_kda, deep_tags, _, game_count, top_recent_champs, win_count, lose_count, banner_champ, repeat_encounters = get_match_details(searched_puuid, 0, 20, queue)
    improvement_tips = generate_improvement_tips(matches, overall_kda, overall_radar)
    champion_recs = recommend_champions(overall_radar)

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
        "patch_changes": patch_changes, "current_patch": CURRENT_PATCH,
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
    return render_template('index.html', page='privacy', current_patch=CURRENT_PATCH)

@app.route('/more_matches')
def more_matches():
    puuid = request.args.get('puuid')
    start = int(request.args.get('start', 20))
    queue = request.args.get('queue', 'all')
    matches, _, _, _, _, _, _, _, _, _, _, _, _, _, _ = get_match_details(puuid, start, 20, queue, collect_stats=False)
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

if __name__ == '__main__': 
    app.run(debug=True, port=5000)