from flask import Flask, request, jsonify, render_template_string
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
                    return text[:3000]
        body = soup.get_text(separator="\n", strip=True)[:3000]
        if len(body) > 200:
            return body
        return None
    except:
        return None

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

def verify_ticker(name, ticker):
    # DB에서 바로 확인 - 빠르고 정확!
    if ticker in STOCK_DB:
        return True, STOCK_DB[ticker]
    return False, None

def analyze_stocks_stream(article_text):
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
5. 확신도 기준:
   - 상: 직접적 매출/이익 영향이 수치로 예측 가능한 경우
   - 중: 높은 개연성이 있으나 변수가 존재하는 경우
   - 하: 가능성은 있으나 다른 요인에 의해 상쇄될 수 있는 경우

마크다운 기호는 절대 사용하지 마세요.

기사 내용:
""" + article_text + """

반드시 아래 JSON 형식으로만 답변하세요. 다른 텍스트는 절대 포함하지 마세요:
{
  "summary": "투자자 관점 핵심 요약 2줄. 이 뉴스가 시장에 주는 진짜 의미를 담을 것",
  "good": [
    {"name": "종목명", "ticker": "티커", "point": "한줄 핵심 (구체적 수혜 메커니즘)", "reason": "2~3차 파급효과 기반 수혜 이유", "confidence": "상/중/하", "check": "이 종목에서 반드시 확인해야 할 리스크"},
    {"name": "종목명", "ticker": "티커", "point": "한줄 핵심", "reason": "수혜 이유", "confidence": "상/중/하", "check": "주목 포인트"},
    {"name": "종목명", "ticker": "티커", "point": "한줄 핵심", "reason": "수혜 이유", "confidence": "상/중/하", "check": "주목 포인트"},
    {"name": "종목명", "ticker": "티커", "point": "한줄 핵심", "reason": "수혜 이유", "confidence": "상/중/하", "check": "주목 포인트"},
    {"name": "종목명", "ticker": "티커", "point": "한줄 핵심", "reason": "수혜 이유", "confidence": "상/중/하", "check": "주목 포인트"}
  ],
  "bad": [
    {"name": "종목명", "ticker": "티커", "point": "한줄 핵심 (구체적 피해 메커니즘)", "reason": "2~3차 파급효과 기반 악재 이유", "confidence": "상/중/하", "check": "악재 완화 가능성 체크포인트"},
    {"name": "종목명", "ticker": "티커", "point": "한줄 핵심", "reason": "악재 이유", "confidence": "상/중/하", "check": "주목 포인트"},
    {"name": "종목명", "ticker": "티커", "point": "한줄 핵심", "reason": "악재 이유", "confidence": "상/중/하", "check": "주목 포인트"},
    {"name": "종목명", "ticker": "티커", "point": "한줄 핵심", "reason": "악재 이유", "confidence": "상/중/하", "check": "주목 포인트"},
    {"name": "종목명", "ticker": "티커", "point": "한줄 핵심", "reason": "악재 이유", "confidence": "상/중/하", "check": "주목 포인트"}
  ],
  "tip": "시장 참여자 대부분이 놓치고 있는 이 뉴스의 핵심 인사이트 1줄"
}"""
    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text

HTML = open("template.html").read()

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/analyze", methods=["POST"])
def analyze():
    import json as jsonlib
    data = request.json
    url = data.get("url")
    if not url:
        return jsonify({"error": "URL을 입력해주세요."})
    article, err = get_article(url)
    if err:
        return jsonify({"error": err})

    full_result = ""
    for chunk in analyze_stocks_stream(article):
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

    return jsonify({"result": result, "format": "json"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
