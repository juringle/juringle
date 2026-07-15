from flask import Flask, request, jsonify, render_template_string, redirect, url_for, session
from authlib.integrations.flask_client import OAuth
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
import threading
import time
from collections import defaultdict
from datetime import datetime

# IP별 사용량 추적
usage_tracker = defaultdict(lambda: {"count": 0, "date": ""})
DAILY_LIMIT = 5
import anthropic
import requests
from bs4 import BeautifulSoup
import os
from dotenv import load_dotenv
from sector_guide import SECTOR_GUIDE
import sqlite3
from datetime import datetime

def save_analysis(url, summary, stocks):
    try:
        conn = sqlite3.connect('juringle.db')
        c = conn.cursor()
        c.execute(
            "INSERT INTO analyses (article_url, article_summary) VALUES (?, ?)",
            (url, summary)
        )
        analysis_id = c.lastrowid
        for stock in stocks:
            price = stock.get('price', {}).get('price', '').replace(',', '') if stock.get('price') else None
            price_float = float(price) if price else None
            c.execute("""
                INSERT INTO recommendations 
                (analysis_id, ticker, name, type, point, price_at_analysis)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                analysis_id,
                stock.get('ticker'),
                stock.get('name'),
                stock.get('type', 'good'),
                stock.get('point'),
                price_float
            ))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"DB 저장 오류: {e}")

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "juringle-secret-2026")

# OAuth 설정
oauth = OAuth(app)
# HTTPS 강제 설정
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

google = oauth.register(
    name='google',
    client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

# 로그인 매니저
login_manager = LoginManager(app)

# 간단한 유저 저장소
users = {}

class User(UserMixin):
    def __init__(self, id, email, name, picture):
        self.id = id
        self.email = email
        self.name = name
        self.picture = picture

@login_manager.user_loader
def load_user(user_id):
    return users.get(user_id)

# gunicorn 시작시 자동 실행
import os
if os.environ.get('GUNICORN_CMD_ARGS') or not __name__ == '__main__':
    pass

# 뉴스 캐시
news_cache = []
news_cache_time = datetime.now()

def refresh_news_cache():
    global news_cache
    while True:
        news_cache = get_today_news()
        time.sleep(3600)  # 1시간마다 갱신

def start_news_refresh():
    t = threading.Thread(target=refresh_news_cache, daemon=True)
    t.start()
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

def get_title_from_url(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        res = requests.get(url, headers=headers, timeout=10)
        res.encoding = "utf-8"
        soup = BeautifulSoup(res.text, "html.parser")
        title = soup.find("title")
        if title:
            return title.get_text(strip=True)[:100]
        return None
    except:
        return None

def search_naver_news(query):
    try:
        headers = {
            "X-Naver-Client-Id": os.getenv("NAVER_CLIENT_ID"),
            "X-Naver-Client-Secret": os.getenv("NAVER_CLIENT_SECRET")
        }
        params = {"query": query, "display": 3, "sort": "date"}
        res = requests.get("https://openapi.naver.com/v1/search/news.json", headers=headers, params=params, timeout=10)
        data = res.json()
        if "items" in data and data["items"]:
            result = ""
            for item in data["items"]:
                title = item.get("title", "").replace("<b>", "").replace("</b>", "")
                desc = item.get("description", "").replace("<b>", "").replace("</b>", "")
                result += title + "\n" + desc + "\n\n"
            return result[:3000]
        return None
    except:
        return None

def crawl_article(url):
    start = time.perf_counter()
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        res = requests.get(url, headers=headers, timeout=10)
        res.encoding = "utf-8"
        soup = BeautifulSoup(res.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        for selector in ["article", ".article-body", ".news-body", "#articleBody", "#article-view-content-div", ".article_body", "#newsct_article"]:
            content = soup.select_one(selector)
            if content:
                text = content.get_text(separator="\n", strip=True)
                if len(text) > 200:
                    return text[:6000]
        body = soup.get_text(separator="\n", strip=True)[:6000]
        if len(body) > 200:
            return body
        return None
    except:
        return None
    finally:
        print(f"[PERF] crawl_article: {time.perf_counter() - start:.2f}s")

def is_valid_article(text):
    if not text or len(text) < 200:
        return False
    garbage = ['광고', '구독', '댓글', '좋아요', '슬퍼요', '화나요', '저작권자', '무단 전재', '카카오톡', '페이스북']
    count = sum(1 for g in garbage if g in text)
    return count < 4

def is_valid_url(url):
    try:
        if not url.startswith("http"):
            return False
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.head(url, headers=headers, timeout=5, allow_redirects=True)
        if res.status_code < 400:
            return True
        res = requests.get(url, headers=headers, timeout=5)
        return res.status_code < 400
    except:
        return False

def get_article(url):
    if not is_valid_url(url):
        return None, '유효하지 않은 URL이에요. 기사 링크를 다시 확인해주세요.'
    article = crawl_article(url)
    if article and is_valid_article(article):
        return article, None
    title = get_title_from_url(url)
    if not title:
        return None, '기사 제목을 읽어올 수 없어요. 다른 링크를 시도해보세요.'
    result = search_naver_news(title)
    if result:
        return result, None
    return None, '기사 내용을 읽어올 수 없어요. 다른 링크를 시도해보세요.'


def get_stock_price(ticker):
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker + ".KS")
        info = stock.fast_info
        price = info.last_price
        prev = info.previous_close
        if price and prev:
            change = price - prev
            change_pct = (change / prev) * 100
            return {
                "price": f"{int(price):,}",
                "change": f"{'+' if change >= 0 else ''}{int(change):,}",
                "change_pct": f"{'+' if change_pct >= 0 else ''}{change_pct:.2f}%",
                "is_up": change >= 0
            }
        return None
    except:
        return None

# 상장 종목 DB 로드
import json as _json
try:
    with open('stock_db.json', 'r', encoding='utf-8') as f:
        STOCK_DB = _json.load(f)
    print(f"종목 DB 로드: {len(STOCK_DB)}개")
except:
    STOCK_DB = {}
    print("종목 DB 없음")

try:
    with open('stock_candidate_db.json', 'r', encoding='utf-8') as f:
        STOCK_CANDIDATE_DB = _json.load(f)
    print(f"후보 종목 DB 로드: {len(STOCK_CANDIDATE_DB)}개")
except:
    STOCK_CANDIDATE_DB = {}
    print("후보 종목 DB 없음")

def verify_ticker(name, ticker):
    # DB에서 바로 확인 - 빠르고 정확!
    if ticker in STOCK_DB:
        return True, STOCK_DB[ticker]
    return False, None

def detect_article_modes(article_text, related_news):
    combined_text = (article_text + "\n" + related_news).lower()

    def find_signals(keywords):
        matches = []
        for keyword in keywords:
            if keyword.lower() in combined_text:
                matches.append(keyword)
        return matches

    matched_signals = {
        "market_index": find_signals([
            "코스피", "코스닥", "지수", "목표치", "랠리", "증시",
            "비중 확대", "최선호 시장", "밸류에이션", "외국인 순매수", "리레이팅"
        ]),
        "industry_outlook": find_signals([
            "업황", "사이클", "공급 부족", "수요 증가", "가격 상승", "CAPEX",
            "투자 확대", "슈퍼사이클", "국산화", "수출 증가"
        ]),
        "company_news": [],
        "policy": find_signals([
            "정부", "정책", "지원", "육성", "규제 완화", "세액공제",
            "보조금", "입찰", "공공 발주", "국책 사업"
        ]),
        "order_contract": find_signals([
            "수주", "계약", "공급 계약", "납품", "장기공급", "MOU",
            "LOI", "발주", "프로젝트"
        ]),
        "earnings": find_signals([
            "실적", "영업이익", "매출", "순이익", "어닝 서프라이즈",
            "마진", "흑자전환", "적자전환", "컨센서스"
        ])
    }

    for ticker, item in list(STOCK_DB.items())[:]:
        name = item.get("name", "") if isinstance(item, dict) else str(item)
        if name and name.lower() in combined_text:
            matched_signals["company_news"].append(f"{name}({ticker})")
            if len(matched_signals["company_news"]) >= 20:
                break

    sector_signal_keywords = {
        "반도체/AI": [
            "AI 반도체", "HBM", "DRAM", "NAND", "메모리", "반도체"
        ],
        "원전/전력": [
            "전력기기", "변압기", "전력망", "송전", "데이터센터 전력"
        ],
        "통신/데이터센터": [
            "데이터센터 전력", "IDC", "서버랙", "광통신", "클라우드 인프라"
        ],
        "조선/해양": [
            "조선", "LNG선", "선박", "해양플랜트"
        ],
        "방산": [
            "방산", "무기", "수출 계약", "항공우주"
        ],
        "배터리/소재": [
            "배터리", "양극재", "음극재", "전해액", "분리막"
        ]
    }
    market_reason_sectors = [
        sector for sector, keywords in sector_signal_keywords.items()
        if find_signals(keywords)
    ]

    policy_execution_signals = [
        signal for signal in matched_signals["policy"]
        if signal in ["규제 완화", "세액공제", "보조금", "입찰", "공공 발주", "국책 사업"]
    ]
    modes = []
    if len(matched_signals["market_index"]) >= 2:
        modes.append("MARKET_INDEX_OUTLOOK")
    if len(matched_signals["industry_outlook"]) >= 2:
        modes.append("INDUSTRY_OUTLOOK")
    if len(matched_signals["policy"]) >= 2 and policy_execution_signals:
        modes.append("POLICY_NEWS")
    if len(matched_signals["order_contract"]) >= 2:
        modes.append("ORDER_CONTRACT_NEWS")
    if len(matched_signals["earnings"]) >= 2:
        modes.append("EARNINGS_NEWS")

    if "MARKET_INDEX_OUTLOOK" in modes:
        primary_mode = "MARKET_INDEX_OUTLOOK"
    elif "INDUSTRY_OUTLOOK" in modes:
        primary_mode = "INDUSTRY_OUTLOOK"
    elif "POLICY_NEWS" in modes:
        primary_mode = "POLICY_NEWS"
    elif "ORDER_CONTRACT_NEWS" in modes:
        primary_mode = "ORDER_CONTRACT_NEWS"
    elif "EARNINGS_NEWS" in modes:
        primary_mode = "EARNINGS_NEWS"
    else:
        primary_mode = "UNKNOWN"

    return {
        "primary_mode": primary_mode,
        "modes": modes,
        "market_reason_sectors": market_reason_sectors,
        "matched_signals": matched_signals
    }

def build_candidate_prompt_section(article_text, related_news):
    if not STOCK_CANDIDATE_DB:
        return ""

    article_modes = detect_article_modes(article_text, related_news)
    print(f"기사 타입 판별: primary={article_modes['primary_mode']}, modes={article_modes['modes']}")
    print(f"시장전망 원인 섹터: {article_modes['market_reason_sectors']}")
    print(f"기사 타입 신호: {article_modes['matched_signals']}")
    market_reason_sectors = article_modes["market_reason_sectors"]

    combined_text = (article_text + "\n" + related_news).lower()
    finance_direct_keywords = [
        "거래대금", "브로커리지", "고객예탁금", "예탁금", "신용융자",
        "IPO", "상장 주관", "IB 수익", "운용자산", "AUM", "증권사 실적",
        "수수료 수익", "리테일 거래", "위탁매매"
    ]
    finance_direct_signals = [
        keyword for keyword in finance_direct_keywords
        if keyword.lower() in combined_text
    ]
    finance_gate_enabled = (
        article_modes["primary_mode"] == "MARKET_INDEX_OUTLOOK"
        and bool(market_reason_sectors)
        and "금융/증권/보험" not in market_reason_sectors
    )
    print(f"금융 sector gate: enabled={finance_gate_enabled}, direct_signals={finance_direct_signals}")

    def matched_terms(terms):
        matches = []
        for term in terms:
            if not term:
                continue
            normalized = str(term).strip()
            if normalized and normalized.lower() in combined_text:
                matches.append(normalized)
        return matches

    semiconductor_core_terms = [
        "AI 반도체", "HBM", "DRAM", "NAND", "메모리", "고대역폭메모리",
        "서버용 메모리", "반도체 장비", "패키징", "테스트", "증착"
    ]

    scored_candidates = []
    sector_scores = defaultdict(int)
    for ticker, item in STOCK_CANDIDATE_DB.items():
        related_matches = matched_terms(item.get("related_keywords", []))
        trigger_matches = matched_terms(item.get("benefit_triggers", []))
        theme_matches = matched_terms(item.get("themes", []))
        context_matches = matched_terms([
            item.get("subsector", ""),
            item.get("value_chain_role", "")
        ])

        score = (
            len(related_matches) * 3
            + len(trigger_matches) * 2
            + len(theme_matches) * 2
            + len(context_matches)
        )

        # "상"은 실제 기사 매칭이 있을 때만 약한 보너스입니다.
        if score > 0 and item.get("confidence_base") == "상":
            score += 1

        semiconductor_exception_matches = []
        if item.get("sector") == "반도체/AI" and not related_matches:
            theme_context_matches = theme_matches + context_matches
            semiconductor_exception_matches = [
                term for term in semiconductor_core_terms
                if any(term in match for match in theme_context_matches)
            ]

        has_related_match = bool(related_matches) or len(semiconductor_exception_matches) >= 2
        if not has_related_match or score < 3:
            continue
        if (
            finance_gate_enabled
            and item.get("sector") == "금융/증권/보험"
            and not finance_direct_signals
        ):
            continue

        matched = related_matches + trigger_matches + theme_matches + context_matches
        if semiconductor_exception_matches:
            matched.append(
                "semiconductor_core_exception:" + ",".join(semiconductor_exception_matches)
            )
        scored_candidates.append((score, len(matched), ticker, item, matched))
        sector_scores[item.get("sector", "")] += score

    if not scored_candidates:
        return ""

    ranked_sectors = sorted(
        sector_scores.items(),
        key=lambda row: row[1],
        reverse=True
    )
    top_sector_list = [
        sector for sector in market_reason_sectors
        if sector in sector_scores
    ][:4]
    for sector, sector_score in ranked_sectors:
        if len(top_sector_list) >= 4:
            break
        if sector in top_sector_list:
            continue
        if 0 < len(market_reason_sectors) <= 2 and sector_score < 5:
            continue
        top_sector_list.append(sector)
    top_sectors = set(top_sector_list)

    print(f"article_modes 우선 섹터: {market_reason_sectors}")
    print(f"최종 후보 top_sectors: {sorted(top_sectors)}")

    scored_candidates.sort(
        key=lambda row: (
            1 if row[3].get("sector", "") in market_reason_sectors else 0,
            row[0],
            row[1],
            1 if row[3].get("confidence_base") == "상" else 0
        ),
        reverse=True
    )

    max_total = 25
    max_per_sector = 6
    sector_counts = defaultdict(int)
    candidates = []
    for score, match_count, ticker, item, matched in scored_candidates:
        sector = item.get("sector", "")
        if sector not in top_sectors:
            continue
        sector_limit = (
            1
            if finance_gate_enabled and sector == "금융/증권/보험"
            else max_per_sector
        )
        if sector_counts[sector] >= sector_limit:
            continue
        candidates.append((score, match_count, ticker, item, matched))
        sector_counts[sector] += 1
        if len(candidates) >= max_total:
            break

    if not candidates:
        return ""

    lines = []
    detected_sectors = []
    for score, match_count, ticker, item, matched in candidates:
        sector = item.get("sector", "")
        if sector not in detected_sectors:
            detected_sectors.append(sector)
        triggers = ", ".join(item.get("benefit_triggers", [])[:3])
        keywords = ", ".join(item.get("related_keywords", [])[:5])
        matched_summary = ", ".join(matched[:5])
        lines.append(
            f"- {item.get('name')}({ticker}) | sector: {sector} | {item.get('subsector')} | "
            f"역할: {item.get('value_chain_role')} | 수혜 조건: {triggers} | "
            f"키워드: {keywords} | 기본 확신도: {item.get('confidence_base')} | "
            f"매칭: {matched_summary} | 점수: {score}"
        )

    print(f"후보군 섹터 감지: {', '.join(detected_sectors)}")
    print(f"후보군 라인 수: {len(lines)}")
    print(f"sector별 후보 수: {dict(sector_counts)}")

    return """

[추천 후보군 - """ + ", ".join(detected_sectors) + """]
아래 후보군은 stock_candidate_db.json에서 기사 본문과 관련 뉴스의 키워드 매칭 점수로 선별한 상장 종목입니다.
추천 후보군이 제공된 경우, 가능한 한 후보군 안에서만 good 종목을 고르세요.
후보군 밖 종목은 매우 명확한 이유가 있을 때만 추천하세요.
후보군에 있어도 뉴스와 연결이 약하면 추천하지 마세요.
후보군의 value_chain_role, benefit_triggers, related_keywords를 근거로 추천 이유를 작성하세요.
confidence_base가 "상"이어도 기사와 직접 매칭되지 않으면 추천하지 마세요.
""" + "\n".join(lines)


def get_related_news(article_text):
    """기사 본문에서 핵심 키워드 추출 후 최신 관련 뉴스 검색"""
    total_start = time.perf_counter()
    try:
        # Claude로 키워드 추출
        kw_prompt = f"""아래 기사에서 주식 분석에 중요한 핵심 키워드 3개만 추출하세요.
키워드만 쉼표로 구분해서 답하세요. 다른 말은 하지 마세요.
예시: 삼성전자, 반도체, 수출규제

기사:
{article_text[:1000]}"""
        
        keyword_start = time.perf_counter()
        kw_msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=50,
            messages=[{"role": "user", "content": kw_prompt}]
        )
        print(f"[PERF] keyword_extract: {time.perf_counter() - keyword_start:.2f}s")
        keywords = kw_msg.content[0].text.strip()
        print(f"추출된 키워드: {keywords}")
        
        # 네이버 뉴스 API로 최신 뉴스 검색
        headers = {
            "X-Naver-Client-Id": os.getenv("NAVER_CLIENT_ID"),
            "X-Naver-Client-Secret": os.getenv("NAVER_CLIENT_SECRET")
        }
        params = {"query": keywords, "display": 5, "sort": "date"}
        naver_start = time.perf_counter()
        res = requests.get(
            "https://openapi.naver.com/v1/search/news.json",
            headers=headers, params=params, timeout=10
        )
        print(f"[PERF] naver_news_search: {time.perf_counter() - naver_start:.2f}s")
        data = res.json()
        
        if "items" not in data or not data["items"]:
            return ""
        
        related = "\n\n[관련 최신 뉴스]\n"
        for item in data["items"]:
            title = item.get("title", "").replace("<b>", "").replace("</b>", "")
            desc = item.get("description", "").replace("<b>", "").replace("</b>", "")
            pub_date = item.get("pubDate", "")[:16]
            related += f"- ({pub_date}) {title}: {desc}\n"
        
        return related
    except Exception as e:
        print(f"관련 뉴스 검색 실패: {e}")
        return ""
    finally:
        print(f"[PERF] get_related_news_total: {time.perf_counter() - total_start:.2f}s")

def analyze_stocks_stream(article_text, analyze_start=None):
    # 관련 최신 뉴스 검색
    related_news = get_related_news(article_text)
    print(f"관련 뉴스 추가됨: {len(related_news)}자")
    if analyze_start:
        print(f"[PERF] claude_analysis_start: {time.perf_counter() - analyze_start:.2f}s")
    candidate_section = build_candidate_prompt_section(article_text, related_news)
    
    prompt = """당신은 월가와 여의도를 모두 경험한 최고 수준의 한국 주식 애널리스트 팀입니다.
아래 뉴스를 읽고 "시장이 아직 주목하지 못한" 숨겨진 수혜/피해 종목을 발굴하세요.

업종별 세부 분류 가이드 (종목 추천시 반드시 이 가이드를 우선 참고하세요):
""" + SECTOR_GUIDE + """

핵심 원칙:
1. 뉴스의 직접 당사자(예: 삼성 파업뉴스에서 삼성전자)는 너무 당연하므로 포함하지 마세요
2-0. 반사이익이 직접적이고 명확한 종목(예: 혼다 철수시 현대차/기아)을 반드시 최상위에 배치하세요
2-1. 3차 이상 간접 추론 종목은 절대 포함하지 마세요. 예) 개인정보 유출 → 게임사 악재 (X), 개인정보 유출 → 보안솔루션 호재 (O)
2-2. 악재 종목은 확실한 직접 피해가 예상될 때만 포함하세요. 애매하면 비워두세요.
2-4. 호재 종목도 억지 연관은 절대 금지. 확실한 연관 없으면 3개 이하로 줄여도 됩니다.
2-3. 정치인 관련 뉴스 패턴:
   - 선거/당선 뉴스 → 해당 정치인 출신 지역 개발주, 과거 공약 관련주, 지지 산업군
   - 예) 건설/도시개발 공약 → 관련 건설사, 부동산
   - 예) 에너지 공약 → 관련 에너지 기업
   - 단, 근거 없는 테마주는 포함하지 마세요
2. 2차, 3차 파급효과를 추론하세요
   - 예) 삼성 파업 → 반도체 공급 차질 → TSMC 반사수혜 → 국내 TSMC 장비 납품사 수혜
   - 예) 노조 임금 확대 → 자동화 투자 가속 → 산업용 로봇 기업 수혜
   - 예) 원자재 가격 상승 → 원가 부담 기업 악재 → 대체재 보유 기업 호재
3. 일반 투자자가 쉽게 떠올리지 못하는 종목을 우선 발굴하세요
4. 반드시 구체적 인과관계로 설명하세요 (추상적 표현 금지)
4-1. [사실 검증 필수] 종목 추천 전 스스로 검증하세요: "이 회사가 실제로 해당 사업/기술/거래관계를 보유하고 있는가?"
   - 확실히 아는 경우에만 포함하세요. "~할 것으로 추정", "~가능성 존재" 수준의 연결은 제외하세요
   - 회사의 주력 사업이 뉴스 내용과 무관한데 부수 사업으로 억지 연결하는 것 금지
   - 해당 기업과 뉴스 속 기업/산업의 실제 거래 이력이나 사업 연관성을 모르면 추천하지 마세요
4-2. reason에는 반드시 "이 회사의 실제 사업 근거"를 포함하세요
   - 좋은 예: "한미반도체는 HBM용 TC본더 세계 1위 기업으로 SK하이닉스에 납품 중"
   - 나쁜 예: "패키징 기술을 보유하고 있어 수혜 가능성 존재" (근거 모호)
5. 확신도 기준:
   - 상: 직접적 매출/이익 영향이 수치로 예측 가능한 경우
   - 중: 높은 개연성이 있으나 변수가 존재하는 경우
   - 하: 가능성은 있으나 다른 요인에 의해 상쇄될 수 있는 경우
6. good 종목은 5개를 억지로 채우지 말고, 명확한 수혜/관련 종목만 추천하세요
7. 확신도 '하'인 good 종목은 추천하지 마세요
8. bad 종목은 분석하거나 생성하지 말고 항상 빈 배열 []로 반환하세요

마크다운 기호는 절대 사용하지 마세요.

기사 내용:
""" + article_text + related_news + candidate_section + """

반드시 아래 JSON 형식으로만 답변하세요. 다른 텍스트는 절대 포함하지 마세요.
good은 최대 5개까지 배열로 작성하세요.
good의 각 객체 필드는 아래 JSON 구조와 동일하게 유지하세요.
good 추천 종목이 없으면 빈 배열 []로 작성하세요.
bad는 분석하거나 생성하지 말고 항상 빈 배열 []로 작성하세요.
{
  "summary": "투자자 관점 핵심 요약 2줄. 이 뉴스가 시장에 주는 진짜 의미를 담을 것",
  "good": [
    {"name": "종목명", "ticker": "티커", "point": "한줄 핵심 (구체적 수혜 메커니즘)", "reason": "2~3차 파급효과 기반 수혜 이유", "confidence": "상/중/하", "check": "이 종목에서 반드시 확인해야 할 리스크"}
  ],
  "bad": [],
  "tip": "시장 참여자 대부분이 놓치고 있는 이 뉴스의 핵심 인사이트 1줄"
}"""
    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text

def analyze_stocks_stream_gpt(article_text, analyze_start=None):
    gpt_start = time.perf_counter()
    # 관련 최신 뉴스 검색
    related_news = get_related_news(article_text)
    print(f"관련 뉴스 추가됨: {len(related_news)}자")
    if analyze_start:
        print(f"[PERF] gpt_analysis_start: {time.perf_counter() - analyze_start:.2f}s")
    
    prompt = """당신은 월가와 여의도를 모두 경험한 최고 수준의 한국 주식 애널리스트 팀입니다.
아래 뉴스를 읽고 "시장이 아직 주목하지 못한" 숨겨진 수혜/피해 종목을 발굴하세요.

업종별 세부 분류 가이드 (종목 추천시 반드시 이 가이드를 우선 참고하세요):
""" + SECTOR_GUIDE + """

핵심 원칙:
1. 뉴스의 직접 당사자(예: 삼성 파업뉴스에서 삼성전자)는 너무 당연하므로 포함하지 마세요
2-0. 반사이익이 직접적이고 명확한 종목(예: 혼다 철수시 현대차/기아)을 반드시 최상위에 배치하세요
2-1. 3차 이상 간접 추론 종목은 절대 포함하지 마세요. 예) 개인정보 유출 → 게임사 악재 (X), 개인정보 유출 → 보안솔루션 호재 (O)
2-2. 악재 종목은 확실한 직접 피해가 예상될 때만 포함하세요. 애매하면 비워두세요.
2-4. 호재 종목도 억지 연관은 절대 금지. 확실한 연관 없으면 3개 이하로 줄여도 됩니다.
2-3. 정치인 관련 뉴스 패턴:
   - 선거/당선 뉴스 → 해당 정치인 출신 지역 개발주, 과거 공약 관련주, 지지 산업군
   - 예) 건설/도시개발 공약 → 관련 건설사, 부동산
   - 예) 에너지 공약 → 관련 에너지 기업
   - 단, 근거 없는 테마주는 포함하지 마세요
2. 2차, 3차 파급효과를 추론하세요
   - 예) 삼성 파업 → 반도체 공급 차질 → TSMC 반사수혜 → 국내 TSMC 장비 납품사 수혜
   - 예) 노조 임금 확대 → 자동화 투자 가속 → 산업용 로봇 기업 수혜
   - 예) 원자재 가격 상승 → 원가 부담 기업 악재 → 대체재 보유 기업 호재
3. 일반 투자자가 쉽게 떠올리지 못하는 종목을 우선 발굴하세요
4. 반드시 구체적 인과관계로 설명하세요 (추상적 표현 금지)
4-1. [사실 검증 필수] 종목 추천 전 스스로 검증하세요: "이 회사가 실제로 해당 사업/기술/거래관계를 보유하고 있는가?"
   - 확실히 아는 경우에만 포함하세요. "~할 것으로 추정", "~가능성 존재" 수준의 연결은 제외하세요
   - 회사의 주력 사업이 뉴스 내용과 무관한데 부수 사업으로 억지 연결하는 것 금지
   - 해당 기업과 뉴스 속 기업/산업의 실제 거래 이력이나 사업 연관성을 모르면 추천하지 마세요
4-2. reason에는 반드시 "이 회사의 실제 사업 근거"를 포함하세요
   - 좋은 예: "한미반도체는 HBM용 TC본더 세계 1위 기업으로 SK하이닉스에 납품 중"
   - 나쁜 예: "패키징 기술을 보유하고 있어 수혜 가능성 존재" (근거 모호)
5. 확신도 기준:
   - 상: 직접적 매출/이익 영향이 수치로 예측 가능한 경우
   - 중: 높은 개연성이 있으나 변수가 존재하는 경우
   - 하: 가능성은 있으나 다른 요인에 의해 상쇄될 수 있는 경우

마크다운 기호는 절대 사용하지 마세요.

기사 내용:
""" + article_text + related_news + """

반드시 아래 JSON 형식으로만 답변하세요. 다른 텍스트는 절대 포함하지 마세요.
good은 최대 5개까지 배열로 작성하고, bad는 최대 5개까지 배열로 작성하세요.
각 객체의 필드는 아래 JSON 구조와 동일하게 유지하세요.
추천할 종목이 없으면 빈 배열 []로 작성하세요.
{
  "summary": "투자자 관점 핵심 요약 2줄. 이 뉴스가 시장에 주는 진짜 의미를 담을 것",
  "good": [
    {"name": "종목명", "ticker": "티커", "point": "한줄 핵심 (구체적 수혜 메커니즘)", "reason": "2~3차 파급효과 기반 수혜 이유", "confidence": "상/중/하", "check": "이 종목에서 반드시 확인해야 할 리스크"}
  ],
  "bad": [
    {"name": "종목명", "ticker": "티커", "point": "한줄 핵심 (구체적 피해 메커니즘)", "reason": "2~3차 파급효과 기반 악재 이유", "confidence": "상/중/하", "check": "악재 완화 가능성 체크포인트"}
  ],
  "tip": "시장 참여자 대부분이 놓치고 있는 이 뉴스의 핵심 인사이트 1줄"
}"""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print(f"[PERF] gpt_analyze_total: {time.perf_counter() - gpt_start:.2f}s")
        return '{"error":"OPENAI_API_KEY가 설정되어 있지 않습니다."}'

    res = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        },
        json={
            "model": "gpt-5-mini",
            "input": [{"role": "user", "content": prompt}],
            "max_output_tokens": 3000
        },
        timeout=60
    )
    res.raise_for_status()
    data = res.json()
    print(f"[PERF] gpt_analyze_total: {time.perf_counter() - gpt_start:.2f}s")
    if data.get("output_text"):
        return data["output_text"]
    output = ""
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in ("output_text", "text"):
                output += content.get("text", "")
    return output

HTML = open("template.html").read()

def get_today_news():
    try:
        headers = {
            "X-Naver-Client-Id": os.getenv("NAVER_CLIENT_ID"),
            "X-Naver-Client-Secret": os.getenv("NAVER_CLIENT_SECRET")
        }
        queries = ["주식 증시 코스피", "경제 금융 환율", "국제 외교 무역", "정치 정책 산업"]
        news_list = []
        seen = set()
        for query in queries:
            params = {"query": query, "display": 5, "sort": "date"}
            res = requests.get("https://openapi.naver.com/v1/search/news.json", headers=headers, params=params, timeout=5)
            data = res.json()
            for item in data.get("items", []):
                title = item["title"].replace("<b>", "").replace("</b>", "").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"')
                link = item.get("originallink") or item.get("link")
                if title not in seen:
                    seen.add(title)
                    news_list.append({"title": title, "link": link})
        return news_list[:10]
    except:
        return []




@app.route("/login")
def login():
    redirect_uri = url_for('callback', _external=True)
    return google.authorize_redirect(redirect_uri)

@app.route("/callback")
def callback():
    token = google.authorize_access_token()
    userinfo = token.get('userinfo')
    if userinfo:
        user_id = userinfo['sub']
        user = User(
            id=user_id,
            email=userinfo['email'],
            name=userinfo.get('name', ''),
            picture=userinfo.get('picture', '')
        )
        users[user_id] = user
        login_user(user)
    return redirect('/')

@app.route("/logout")
def logout():
    logout_user()
    return redirect('/')

@app.route("/me")
def me():
    if current_user.is_authenticated:
        return jsonify({
            "logged_in": True,
            "name": current_user.name,
            "email": current_user.email,
            "picture": current_user.picture
        })
    return jsonify({"logged_in": False})

@app.route("/about")
def about():
    return render_template_string("""
<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8"><title>서비스 소개 - Juringle</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
body{font-family:sans-serif;max-width:800px;margin:40px auto;padding:20px;line-height:1.8;color:#333;}
h1{font-size:2rem;} .j1{color:#4285F4;}.j2{color:#EA4335;}.j3{color:#FBBC05;}.j4{color:#34A853;}
.card{background:#f8f9fa;border-radius:12px;padding:20px;margin:20px 0;}
.tag{display:inline-block;background:#e8f0fe;color:#4285F4;padding:4px 12px;border-radius:20px;font-size:14px;margin:4px;}
</style>
</head>
<body>
<h1><span class="j1">J</span><span class="j2">u</span><span class="j3">r</span><span class="j4">i</span><span class="j1">n</span><span class="j2">g</span><span class="j3">l</span><span class="j4">e</span></h1>
<p style="font-size:1.2rem;color:#666;">뉴스 링크 하나로 관련 주식을 찾아드려요</p>

<div class="card">
<h2>🔍 이런 서비스예요</h2>
<p>뉴스 기사 URL을 붙여넣으면 AI가 관련 한국 주식 종목을 분석해드려요. 호재/악재 종목을 한눈에 파악하고, 시장이 아직 주목하지 못한 숨겨진 수혜주까지 발굴해드립니다.</p>
</div>

<div class="card">
<h2>✨ 주요 기능</h2>
<span class="tag">뉴스 URL 분석</span>
<span class="tag">호재/악재 분류</span>
<span class="tag">관련주 자동 발굴</span>
<span class="tag">오늘의 경제 뉴스</span>
<span class="tag">종목 주가 확인</span>
</div>

<div class="card">
<h2>⚠️ 유의사항</h2>
<p>본 서비스는 AI가 생성한 참고용 정보를 제공합니다. 투자 권유가 아니며, 모든 투자 결정과 책임은 이용자 본인에게 있습니다.</p>
</div>

<div class="card">
<h2>📩 문의</h2>
<p>juringle.official@gmail.com</p>
</div>

<p><a href="/">← 홈으로</a></p>
</body></html>
""")

@app.route("/privacy")
def privacy():
    return render_template_string("""
<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8"><title>개인정보처리방침 - Juringle</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>body{font-family:sans-serif;max-width:800px;margin:40px auto;padding:20px;line-height:1.8;color:#333;}h1{color:#4285F4;}h2{margin-top:30px;}</style>
</head>
<body>
<h1>개인정보처리방침</h1>
<p>Juringle(이하 "서비스")은 이용자의 개인정보를 중요시하며, 개인정보보호법을 준수합니다.</p>
<h2>1. 수집하는 개인정보</h2>
<p>서비스 이용 시 별도의 개인정보를 수집하지 않습니다. 향후 로그인 기능 추가 시 이메일 주소를 수집할 수 있습니다.</p>
<h2>2. 개인정보의 이용목적</h2>
<p>수집된 개인정보는 서비스 제공 및 개선 목적으로만 사용됩니다.</p>
<h2>3. 개인정보의 보유기간</h2>
<p>서비스 탈퇴 시 즉시 삭제됩니다.</p>
<h2>4. 개인정보의 제3자 제공</h2>
<p>이용자의 개인정보를 제3자에게 제공하지 않습니다.</p>
<h2>5. 문의</h2>
<p>개인정보 관련 문의: juringle.official@gmail.com</p>
<p><a href="/">← 홈으로</a></p>
</body></html>
""")

@app.route("/terms")
def terms():
    return render_template_string("""
<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8"><title>이용약관 - Juringle</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>body{font-family:sans-serif;max-width:800px;margin:40px auto;padding:20px;line-height:1.8;color:#333;}h1{color:#4285F4;}h2{margin-top:30px;}</style>
</head>
<body>
<h1>이용약관</h1>
<h2>제1조 (목적)</h2>
<p>이 약관은 Juringle(이하 "서비스")의 이용조건 및 절차에 관한 사항을 규정합니다.</p>
<h2>제2조 (서비스 내용)</h2>
<p>서비스는 뉴스 기사 URL을 입력받아 AI를 통해 관련 주식 종목을 분석하는 정보 제공 서비스입니다.</p>
<h2>제3조 (면책조항)</h2>
<p>본 서비스는 투자 참고용 정보만을 제공하며, 투자 권유가 아닙니다. 투자 결과에 대한 책임은 이용자 본인에게 있습니다.</p>
<h2>제4조 (서비스 변경 및 중단)</h2>
<p>서비스는 운영상 필요에 따라 사전 고지 없이 변경되거나 중단될 수 있습니다.</p>
<h2>제5조 (문의)</h2>
<p>서비스 관련 문의: juringle.official@gmail.com</p>
<p><a href="/">← 홈으로</a></p>
</body></html>
""")

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/today-news")
def today_news():
    global news_cache, news_cache_time
    now = datetime.now()
    if not news_cache or (now - news_cache_time).seconds > 3600:
        news_cache = get_today_news()
        news_cache_time = now
    return jsonify({"news": news_cache})

@app.route("/analyze", methods=["POST"])
def analyze():
    import json as jsonlib
    analyze_start = time.perf_counter()
    # IP 기반 사용량 체크
    ip = request.remote_addr
    today = datetime.now().strftime("%Y-%m-%d")
    if usage_tracker[ip]["date"] != today:
        usage_tracker[ip] = {"count": 0, "date": today}
    if usage_tracker[ip]["count"] >= DAILY_LIMIT:
        print(f"[PERF] analyze_total: {time.perf_counter() - analyze_start:.2f}s")
        return jsonify({"error": f"하루 {DAILY_LIMIT}회 무료 분석을 모두 사용했어요. 내일 다시 이용해주세요! 😊"})
    usage_tracker[ip]["count"] += 1
    data = request.json
    url = data.get("url")
    if not url:
        print(f"[PERF] analyze_total: {time.perf_counter() - analyze_start:.2f}s")
        return jsonify({"error": "URL을 입력해주세요."})
    article, err = get_article(url)
    if err:
        print(f"[PERF] analyze_total: {time.perf_counter() - analyze_start:.2f}s")
        return jsonify({"error": err})

    full_result = ""
    for chunk in analyze_stocks_stream(article, analyze_start):
        full_result += chunk
    result = full_result.strip()

    if "```" in result:
        parts = result.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                result = part
                break
    result = result.strip()

    try:
        parsed = jsonlib.loads(result)
        blocked_names = ["한국거래소", "거래소", "협회", "기관", "정부", "지수", "ETF", "비상장"]
        valid_good = []
        for stock in parsed.get("good", []):
            ticker = stock.get("ticker", "")
            name = stock.get("name", "")
            if not ticker or any(word in name for word in blocked_names):
                continue
            valid, real_name = verify_ticker(name, ticker)
            if not valid:
                continue
            stock["verified"] = True
            stock["real_name"] = real_name
            price_info = get_stock_price(ticker)
            if price_info:
                stock["price"] = price_info
            valid_good.append(stock)
        parsed["good"] = valid_good
        parsed["bad"] = []
        result = jsonlib.dumps(parsed, ensure_ascii=False)
    except:
        pass

    try:
        parsed = jsonlib.loads(result)
        all_stocks = []
        for s in parsed.get("good", []):
            s["type"] = "good"
            all_stocks.append(s)
        save_analysis(url, parsed.get("summary", ""), all_stocks)
    except:
        pass

    print(f"[PERF] analyze_total: {time.perf_counter() - analyze_start:.2f}s")
    return jsonify({"result": result, "format": "json"})

@app.route("/analyze_gpt", methods=["POST"])
def analyze_gpt():
    import json as jsonlib
    analyze_start = time.perf_counter()
    # IP 기반 사용량 체크
    ip = request.remote_addr
    today = datetime.now().strftime("%Y-%m-%d")
    if usage_tracker[ip]["date"] != today:
        usage_tracker[ip] = {"count": 0, "date": today}
    if usage_tracker[ip]["count"] >= DAILY_LIMIT:
        print(f"[PERF] analyze_gpt_total: {time.perf_counter() - analyze_start:.2f}s")
        return jsonify({"error": f"하루 {DAILY_LIMIT}회 무료 분석을 모두 사용했어요. 내일 다시 이용해주세요! 😊"})
    usage_tracker[ip]["count"] += 1
    data = request.json
    url = data.get("url")
    if not url:
        print(f"[PERF] analyze_gpt_total: {time.perf_counter() - analyze_start:.2f}s")
        return jsonify({"error": "URL을 입력해주세요."})
    article, err = get_article(url)
    if err:
        print(f"[PERF] analyze_gpt_total: {time.perf_counter() - analyze_start:.2f}s")
        return jsonify({"error": err})

    try:
        result = analyze_stocks_stream_gpt(article, analyze_start).strip()
    except Exception as e:
        print(f"[PERF] analyze_gpt_total: {time.perf_counter() - analyze_start:.2f}s")
        return jsonify({"error": f"GPT 분석 실패: {e}"})

    if "```" in result:
        parts = result.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                result = part
                break
    result = result.strip()

    try:
        parsed = jsonlib.loads(result)
        for stock in parsed.get("good", []) + parsed.get("bad", []):
            ticker = stock.get("ticker", "")
            name = stock.get("name", "")
            if ticker:
                valid, real_name = verify_ticker(name, ticker)
                if not valid:
                    stock["verified"] = False
                    continue
                stock["verified"] = True
                stock["real_name"] = real_name
                price_info = get_stock_price(ticker)
                if price_info:
                    stock["price"] = price_info
        result = jsonlib.dumps(parsed, ensure_ascii=False)
    except:
        pass

    try:
        parsed = jsonlib.loads(result)
        all_stocks = []
        for s in parsed.get("good", []):
            s["type"] = "good"
            all_stocks.append(s)
        save_analysis(url, parsed.get("summary", ""), all_stocks)
    except:
        pass

    print(f"[PERF] analyze_gpt_total: {time.perf_counter() - analyze_start:.2f}s")
    return jsonify({"result": result, "format": "json"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
