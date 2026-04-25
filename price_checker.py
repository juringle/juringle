import sqlite3
import yfinance as yf
from datetime import datetime, timedelta
import schedule
import time

def update_stock_db():
    try:
        import requests
        import pandas as pd
        from io import BytesIO
        import json
        print(f"[{datetime.now()}] 종목 DB 업데이트 시작...")
        url = 'https://kind.krx.co.kr/corpgeneral/corpList.do'
        params = {'method': 'download', 'searchType': '13'}
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(url, params=params, headers=headers)
        df = pd.read_html(BytesIO(res.content), header=0)[0]
        df['종목코드'] = df['종목코드'].astype(str).str.zfill(6)
        stock_db = dict(zip(df['종목코드'], df['회사명']))
        with open('stock_db.json', 'w', encoding='utf-8') as f:
            json.dump(stock_db, f, ensure_ascii=False)
        print(f"종목 DB 업데이트 완료! {len(stock_db)}개")
    except Exception as e:
        print(f"DB 업데이트 오류: {e}")

def check_prices():
    print(f"[{datetime.now()}] 주가 체크 시작...")
    conn = sqlite3.connect('juringle.db')
    c = conn.cursor()
    week_ago = datetime.now() - timedelta(days=7)
    c.execute("""
        SELECT r.id, r.ticker, r.price_at_analysis, a.created_at
        FROM recommendations r
        JOIN analyses a ON r.analysis_id = a.id
        WHERE r.price_1w IS NULL
        AND r.price_at_analysis IS NOT NULL
        AND a.created_at <= ?
    """, (week_ago,))
    rows = c.fetchall()
    print(f"체크할 종목: {len(rows)}개")
    for row in rows:
        rec_id, ticker, price_at, created_at = row
        try:
            stock = yf.Ticker(ticker + ".KS")
            current = stock.fast_info.last_price
            if current and price_at:
                return_1w = ((current - price_at) / price_at) * 100
                c.execute("""
                    UPDATE recommendations 
                    SET price_1w=?, return_1w=?, checked_at=?
                    WHERE id=?
                """, (current, return_1w, datetime.now(), rec_id))
                print(f"{ticker}: {price_at:,.0f}원 → {current:,.0f}원 ({return_1w:+.2f}%)")
        except Exception as e:
            print(f"{ticker} 오류: {e}")
    conn.commit()
    conn.close()
    print("완료!")

schedule.every().day.at("09:00").do(check_prices)
schedule.every().day.at("08:00").do(update_stock_db)
print("주가 체크 스케줄러 시작!")
check_prices()
while True:
    schedule.run_pending()
    time.sleep(60)
