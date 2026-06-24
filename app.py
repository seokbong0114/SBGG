import os
import random
import hashlib
import sqlite3
import json
import time
from flask import Flask, render_template, request, redirect
import requests
from dotenv import load_dotenv
from collections import Counter
from champion_meta import (
    CHAMPION_TIERS, CURRENT_PATCH,
    ROLE_KR, TIER_COLOR, TREND_ICON, TREND_COLOR,
    DIFFICULTY_LABEL, DIFFICULTY_COLOR,
    NUMERIC_TIER_COLOR, CHAMPION_ROLE_MAP, META_STATS,
    DEFAULT_ROLE_STATS, calc_meta_tier, RANK_TIER_LABELS
)

load_dotenv()
app = Flask(__name__)

API_KEY = os.getenv("RIOT_API_KEY")
HEADERS = {"X-Riot-Token": API_KEY}

# 💾 SQLite 데이터베이스 파일 경로 및 초기화 설정
DB_FILE = "sbgg.db"
CACHE_EXPIRE_TIME = 300      # 캐시 만료 시간: 5분
LEADERBOARD_CACHE_TIME = 600 # 래더 캐시: 10분
SPECTATE_CACHE_TIME = 60     # 관전 캐시: 1분 (라이브 데이터)

def riot_get(url):
    return requests.get(url, headers=HEADERS, timeout=5)

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # 소환사별 랭크 검색 데이터를 통째로 캐싱할 테이블 생성
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS search_cache (
            cache_key TEXT PRIMARY KEY,
            json_data TEXT,
            updated_at INTEGER
        )
    ''')
    conn.commit()
    conn.close()

# ✅ MEDIUM 개선: SQLite 연결/해제 보일러플레이트를 헬퍼 함수로 추상화
def db_read(cache_key):
    """캐시 키로 데이터를 조회. (json_data, updated_at) 또는 (None, 0) 반환."""
    try:
        conn = sqlite3.connect(DB_FILE)
        row = conn.execute("SELECT json_data, updated_at FROM search_cache WHERE cache_key=?", (cache_key,)).fetchone()
        conn.close()
        return (row[0], row[1]) if row else (None, 0)
    except Exception as e:
        print(f"DB 읽기 에러 [{cache_key}]: {e}")
        return (None, 0)

def db_write(cache_key, data, current_time):
    """데이터를 JSON으로 직렬화하여 캐시에 저장/갱신."""
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute("REPLACE INTO search_cache (cache_key, json_data, updated_at) VALUES (?, ?, ?)",
                     (cache_key, json.dumps(data), current_time))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"DB 쓰기 에러 [{cache_key}]: {e}")

# 앱 시작 시 DB 초기화 실행
init_db()

try:
    req_opts = {"timeout": 3}
    LATEST_VERSION = requests.get("https://ddragon.leagueoflegends.com/api/versions.json", **req_opts).json()[0]
    champ_data = requests.get(f"https://ddragon.leagueoflegends.com/cdn/{LATEST_VERSION}/data/ko_KR/champion.json", **req_opts).json()['data']
    CHAMP_KR_MAP = {val['id']: val['name'] for key, val in champ_data.items()}
    CHAMP_KEYS = {str(val['key']): val['id'] for key, val in champ_data.items()}
    CHAMP_TAGS = {val['id']: val.get('tags', []) for key, val in champ_data.items()} 
    
    spell_data = requests.get(f"https://ddragon.leagueoflegends.com/cdn/{LATEST_VERSION}/data/ko_KR/summoner.json", **req_opts).json()['data']
    SPELL_MAP = {str(val['key']): val['id'] for key, val in spell_data.items()}
    
    rune_data = requests.get(f"https://ddragon.leagueoflegends.com/cdn/{LATEST_VERSION}/data/ko_KR/runesReforged.json", **req_opts).json()
    RUNE_MAP = {}
    for tree in rune_data:
        RUNE_MAP[tree['id']] = tree['icon']
        for slot in tree['slots']:
            for rune in slot['runes']:
                RUNE_MAP[rune['id']] = rune['icon']
except Exception as e:
    print(f"라이엇 데이터 로드 실패 (안전 모드 실행): {e}")
    LATEST_VERSION = "14.12.1"
    CHAMP_KR_MAP, CHAMP_KEYS, SPELL_MAP, RUNE_MAP, CHAMP_TAGS = {}, {}, {}, {}, {}

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
            masteries.append({'champ_en': c_en, 'level': m['championLevel'], 'points': pts_str})
        return masteries
    return []

def get_match_details(puuid, start=0, count=20, queue=None):
    url = f"https://asia.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?start={start}&count={count}"
    url += f"&queue={queue}" if queue and queue != 'all' else "&type=ranked"
        
    response = riot_get(url)
    if response.status_code != 200: return [], None, None, None, 0, [], {}, [], puuid, 0, [], 0, 0, "Teemo"

    match_ids = response.json()
    matches, role_stats = [], {}
    overall_stats = {"combat": 0, "growth": 0, "vision": 0, "survival": 0, "objectives": 0, "join": 0}
    total_k, total_d, total_a, total_vision, total_kp, win_count, lose_count = 0, 0, 0, 0, 0, 0, 0
    recent_champ_stats = {}

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
                    'puuid': p['puuid'], 'name': p.get('riotIdGameName', 'Unknown'), 'champ_img': champ_en,
                    'teamId': p['teamId'], 'win': p['win'], 'role_en': p.get('teamPosition', ''),
                    'k': k, 'd': d, 'a': a, 'dmg': dmg, 'dmg_str': dmg_str, 'cs': cs, 'cspm': cspm,
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
    
    return matches, overall_radar, primary_role, secondary_role, win_rate, most, overall_kda, deep_tags, puuid, len(matches), top_recent_champs, win_count, lose_count, banner_champ

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
            summoner_id = entry.get('summonerId', '')
            if not summoner_id:
                continue
            s_res = riot_get(f"https://kr.api.riotgames.com/lol/summoner/v4/summoners/{summoner_id}")
            if s_res.status_code != 200:
                continue
            puuid = s_res.json().get('puuid', '')
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
            live_games.append({
                "summoner_name": entry.get('summonerName', '챌린저'),
                "lp": f"{entry['leaguePoints']:,} LP",
                "tier": "CHALLENGER",
                "queue": QUEUE_MAP.get(game.get('gameQueueConfigId', 0), "랭크"),
                "time": f"{length // 60:02d}:{length % 60:02d}",
                "blue_champs": [CHAMP_KEYS.get(str(p['championId']), 'Teemo') for p in blue[:5]],
                "red_champs":  [CHAMP_KEYS.get(str(p['championId']), 'Teemo') for p in red[:5]],
            })

        # ✅ MEDIUM 개선: db_write() 헬퍼 사용
        db_write(cache_key, live_games, current_time)
        return live_games, False
    except Exception as e:
        print(f"관전 API 에러: {e}")
        return [], False

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

        champ_stats_entry = META_STATS.get(champ_en, {})
        role_stats_entry  = champ_stats_entry.get(primary_role, {})
        stats = role_stats_entry.get(rank_tier, None)

        if stats:
            wr, pr, br = stats["wr"], stats["pr"], stats["br"]
            trend      = stats.get("trend", "stable")
            difficulty = stats.get("difficulty", 2)
        else:
            d = DEFAULT_ROLE_STATS.get(primary_role, DEFAULT_ROLE_STATS["TOP"])
            wr, pr, br = d["wr"], d["pr"], d["br"]
            trend, difficulty = d["trend"], d["difficulty"]

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
        }
        result[primary_role].append(entry)

    for role in result:
        tier_order = {"OP": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5}
        result[role].sort(key=lambda x: (tier_order.get(x["tier"], 5), -x["wr"]))

    return result

@app.route('/meta')
def meta():
    rank_tier = request.args.get("rank", "emeraldplus")
    if rank_tier not in RANK_TIER_LABELS:
        rank_tier = "emeraldplus"
    champion_tiers = build_champion_meta(rank_tier)
    tier_counts = {"OP": 0, "1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
    for champs in champion_tiers.values():
        for c in champs:
            tier_counts[c["tier"]] = tier_counts.get(c["tier"], 0) + 1
    return render_template('index.html', page='meta', champion_tiers=champion_tiers,
                           latest_version=LATEST_VERSION, current_patch=CURRENT_PATCH,
                           role_kr=ROLE_KR, rank_tier=rank_tier,
                           rank_tier_labels=RANK_TIER_LABELS,
                           numeric_tier_color=NUMERIC_TIER_COLOR,
                           tier_counts=tier_counts)

@app.route('/champion/<champ_id>')
def champion_page(champ_id):
    champ_data, champ_role = None, None
    for role, champs in CHAMPION_TIERS.items():
        for c in champs:
            if c['id'].lower() == champ_id.lower():
                champ_data, champ_role = c, role
                break
        if champ_data: break
    if not champ_data: return redirect('/')
    styled = {**champ_data,
              'tier_style': TIER_COLOR.get(champ_data['tier'], TIER_COLOR['B']),
              'trend_icon': TREND_ICON.get(champ_data['trend'], '→'),
              'trend_color': TREND_COLOR.get(champ_data['trend'], '#94a3b8'),
              'diff_label': DIFFICULTY_LABEL.get(champ_data['difficulty'], '보통'),
              'diff_color': DIFFICULTY_COLOR.get(champ_data['difficulty'], '#fbbf24')}
    return render_template('index.html', page='champion', champ=styled,
                           champ_role=ROLE_KR.get(champ_role, champ_role),
                           champ_role_en=champ_role,
                           latest_version=LATEST_VERSION, current_patch=CURRENT_PATCH)

@app.route('/search')
def search():
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
    
    matches, overall_radar, primary_role, secondary_role, win, most, overall_kda, deep_tags, _, game_count, top_recent_champs, win_count, lose_count, banner_champ = get_match_details(searched_puuid, 0, 20, queue)
    improvement_tips = generate_improvement_tips(matches, overall_kda, overall_radar)
    champion_recs = recommend_champions(overall_radar)

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

@app.route('/duo')
def duo():
    duo_posts = [
        {"tier": "다이아몬드", "img": "diamond", "role": "BOTTOM", "name": "석봉", "tag": "Bong", "title": "다이아 빡겜 서포터 구합니다. 마이크 필수", "time": "방금 전"},
        {"tier": "골드", "img": "gold", "role": "JUNGLE", "name": "배달해드림", "tag": "KR1", "title": "골드 즐겜러 구해요 아무나 오세요", "time": "15분 전"},
        {"tier": "플래티넘", "img": "platinum", "role": "MIDDLE", "name": "페이커", "tag": "T1", "title": "플레 탈출하실 정글러 모십니다", "time": "1시간 전"},
        {"tier": "에메랄드", "img": "emerald", "role": "TOP", "name": "탑신병자", "tag": "KR2", "title": "탑 위주 게임. 시팅 안 해줘도 됨", "time": "2시간 전"}
    ]
    return render_template('index.html', page='duo', duo_posts=duo_posts)

@app.route('/spectate')
def spectate():
    spectate_games, from_cache = get_live_challenger_games()
    return render_template('index.html', page='spectate', spectate_games=spectate_games, latest_version=LATEST_VERSION, from_cache=from_cache)

@app.route('/more_matches')
def more_matches():
    puuid = request.args.get('puuid')
    start = int(request.args.get('start', 20))
    queue = request.args.get('queue', 'all') 
    matches, _, _, _, _, _, _, _, _, _, _, _, _, _ = get_match_details(puuid, start, 20, queue)
    return render_template('index.html', page='search', matches=matches, ajax=True, searched_puuid=puuid, latest_version=LATEST_VERSION)

if __name__ == '__main__': 
    app.run(debug=True, port=5000)