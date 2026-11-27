
import sys
# Python 3.12+ 및 Streamlit Cloud 환경에서 pykrx의 pkg_resources 모듈 임포트 에러 방지용 shim
try:
    import pkg_resources
except Exception:
    try:
        import setuptools.command
        import pkg_resources
    except Exception:
        import types
        pkg_mock = types.ModuleType("pkg_resources")
        pkg_mock.resource_filename = lambda *args, **kwargs: ""
        pkg_mock.resource_string = lambda *args, **kwargs: b""
        pkg_mock.Requirement = type("Requirement", (), {"parse": lambda s: s})
        pkg_mock.get_distribution = lambda *args, **kwargs: type("Dist", (), {"version": "1.0.0"})()
        sys.modules["pkg_resources"] = pkg_mock

import os
import sys
import time
import json
import pickle
import sqlite3
import datetime
from pathlib import Path
import pandas as pd
from dotenv import load_dotenv

load_dotenv()
from pykrx import stock

# 캐시 디렉토리 설정
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
if not os.path.exists(CACHE_DIR):
    os.makedirs(CACHE_DIR)

# -----------------------------------------------------------------------------
# SQLite 캐시 DB 경로 및 상태 조회
# -----------------------------------------------------------------------------
def get_krx_db_path() -> Path:
    """오라클 서버 및 로컬 통합 SQLite 캐시 DB 경로 반환"""
    env_db = os.getenv("KRX_CACHE_DB")
    if env_db and os.path.exists(env_db):
        return Path(env_db)

    # 1) 오라클 서버 경로
    server_db = Path("/home/ubuntu/apps/data/krx_market_cache.db")
    if server_db.exists():
        return server_db

    # 2) 로컬 PC 경로 (00 Bookmarks/data)
    current_dir = Path(__file__).resolve().parent
    local_db = current_dir.parent / "00 Bookmarks" / "data" / "krx_market_cache.db"
    if local_db.exists():
        return local_db

    # 3) 앱 로컬 data 폴더
    app_db = current_dir / "data" / "krx_market_cache.db"
    if app_db.exists():
        return app_db

    return local_db

def get_db_status() -> dict:
    """캐시 DB 연결 상태 및 최신 수집 일자 확인"""
    db_path = get_krx_db_path()
    if not db_path.exists():
        return {"connected": False, "path": str(db_path), "latest_date": None, "stock_count": 0}
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        cur = conn.cursor()
        cur.execute("SELECT MAX(date), COUNT(DISTINCT ticker) FROM stock_daily_market")
        row = cur.fetchone()
        latest_date, count = row if row else (None, 0)
        conn.close()
        return {
            "connected": True,
            "path": str(db_path),
            "latest_date": latest_date,
            "stock_count": count or 0
        }
    except Exception as e:
        return {"connected": False, "path": str(db_path), "error": str(e), "latest_date": None, "stock_count": 0}

# -----------------------------------------------------------------------------
# 한투(KIS) 공용 토큰 확인 및 상태 반환
# -----------------------------------------------------------------------------
def get_kis_token_status() -> dict:
    """공용 KIS 토큰(kis_token.json) 유효성 및 만료 시간 확인"""
    current_dir = Path(__file__).resolve().parent
    candidates = [
        os.getenv("KIS_TOKEN_FILE"),
        "/home/ubuntu/apps/kis_token.json",
        str(current_dir.parent / "00 API Key" / "kis_token.json"),
        r"D:\AI Investing\00 API Key\kis_token.json",
        str(current_dir / "data" / "kis_token.json")
    ]
    now_ts = time.time()
    for p in candidates:
        if p and os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    tok = json.load(f)
                exp = tok.get("expires_at", 0)
                if tok.get("access_token") and exp > now_ts + 60:
                    exp_dt = datetime.datetime.fromtimestamp(exp)
                    hours_left = round((exp - now_ts) / 3600, 1)
                    return {
                        "available": True,
                        "path": p,
                        "expires_at": exp_dt.strftime("%Y-%m-%d %H:%M"),
                        "hours_left": hours_left
                    }
            except Exception:
                pass
    return {"available": False, "path": None, "expires_at": None, "hours_left": 0}

def get_kis_access_token() -> str:
    """공용 KIS 토큰 문자열 반환"""
    status = get_kis_token_status()
    if status["available"]:
        with open(status["path"], "r", encoding="utf-8") as f:
            return json.load(f).get("access_token")
    return None

def _load_kis_credentials():
    """KIS AppKey 및 AppSecret 자동 로드"""
    if not os.getenv("KIS_APP_KEY") or not os.getenv("KIS_APP_SECRET"):
        load_dotenv()
        current_dir = Path(__file__).resolve().parent
        candidates = [
            Path("/home/ubuntu/apps/.env"),
            current_dir.parent / "00 API Key" / "한국투자증권 API_kis.txt",
            Path(r"D:\AI Investing\00 API Key\한국투자증권 API_kis.txt"),
            current_dir / ".env"
        ]
        for p in candidates:
            if p.exists():
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if "appkey" in line.lower() or "app_key" in line.lower() or line.startswith("KIS_APP_KEY="):
                                val = line.split("=" if "=" in line else ":", 1)[1].strip().strip('"').strip("'")
                                os.environ["KIS_APP_KEY"] = val
                            elif "appsecret" in line.lower() or "app_secret" in line.lower() or line.startswith("KIS_APP_SECRET="):
                                val = line.split("=" if "=" in line else ":", 1)[1].strip().strip('"').strip("'")
                                os.environ["KIS_APP_SECRET"] = val
                except Exception:
                    pass

def fetch_market_cap_from_db(date_str, market="ALL"):
    """SQLite 캐시 DB에서 전종목 시가총액/거래대금/종목명 즉시 조회 (0.01초 소요)"""
    db_path = get_krx_db_path()
    if not db_path.exists():
        return None

    clean_date = date_str.replace("-", "")
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        if market in ["KOSPI", "KOSDAQ"]:
            query = """
            SELECT ticker, name as 종목명, close as 종가, market_cap as 시가총액, 
                   trading_volume as 거래량, trading_value as 거래대금, shares as 상장주식수, market
            FROM stock_daily_market 
            WHERE date = ? AND market = ?
            """
            params = (clean_date, market)
        else:
            query = """
            SELECT ticker, name as 종목명, close as 종가, market_cap as 시가총액, 
                   trading_volume as 거래량, trading_value as 거래대금, shares as 상장주식수, market
            FROM stock_daily_market 
            WHERE date = ?
            """
            params = (clean_date,)

        df = pd.read_sql_query(query, conn, params=params)
        conn.close()

        if not df.empty:
            df.set_index("ticker", inplace=True)
            return df
    except Exception as e:
        print(f"[DB Error] fetch_market_cap_from_db: {e}")
    return None

def setup_krx_auth(krx_id=None, krx_pw=None):
    """
    KRX 계정 정보를 환경변수에 주입하여 pykrx가 세션을 맺을 수 있도록 설정합니다.
    인자가 없을 경우 .env 또는 자격증명 파일에서 자동 로드합니다.
    """
    if not krx_id or not krx_pw:
        krx_id = os.getenv("KRX_ID")
        krx_pw = os.getenv("KRX_PW")

    # .env 파일 또는 로컬 텍스트 파일 폴백
    if not krx_id or not krx_pw:
        env_candidates = [
            Path(__file__).resolve().parent / ".env",
            Path(__file__).resolve().parent.parent / "01_MarketOverview2" / ".env",
            Path(__file__).resolve().parent.parent / "01 MarketOverview2" / ".env",
            Path(r"D:\AI Investing\00 API Key\KRX ID&PW.txt")
        ]
        for p in env_candidates:
            if p.exists():
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith("KRX_ID="):
                                krx_id = line.split("=", 1)[1].strip().strip('"').strip("'")
                            elif line.startswith("KRX_PW="):
                                krx_pw = line.split("=", 1)[1].strip().strip('"').strip("'")
                            elif line.startswith("ID :") or line.startswith("ID:"):
                                krx_id = line.split(":", 1)[1].strip()
                            elif line.startswith("PW :") or line.startswith("PW:"):
                                krx_pw = line.split(":", 1)[1].strip()
                    if krx_id and krx_pw:
                        break
                except Exception:
                    pass

    if krx_id and krx_pw:
        os.environ["KRX_ID"] = krx_id
        os.environ["KRX_PW"] = krx_pw
        try:
            from pykrx.website.comm.auth import get_auth_session
            session = get_auth_session()
            if session and session.is_authenticated:
                return True
        except Exception as e:
            print(f"KRX Auth Setup Error: {e}")
    return False

# 백그라운드 자동 인증 시도
setup_krx_auth()

import requests

# 한국거래소(KRX) 정규 휴장일 및 법정 공휴일 (2024~2027)
KRX_HOLIDAYS = {
    # 2024
    '20240101', '20240209', '20240212', '20240301', '20240410', '20240501', '20240506',
    '20240515', '20240606', '20240815', '20240916', '20240917', '20240918', '20241001',
    '20241003', '20241009', '20241225', '20241231',
    # 2025
    '20250101', '20250128', '20250129', '20250130', '20250303', '20250501', '20250505',
    '20250506', '20250606', '20250815', '20251003', '20251006', '20251007', '20251008',
    '20251009', '20251225', '20251231',
    # 2026
    '20260101', '20260216', '20260217', '20260218', '20260302', '20260501', '20260505',
    '20260525', '20260603', '20260606', '20260817', '20260924', '20260925', '20261005',
    '20261009', '20261225', '20261231',
    # 2027
    '20270101', '20270208', '20270209', '20270210', '20270301', '20270503', '20270505',
    '20270513', '20270607', '20270816', '20270914', '20270915', '20270916', '20271004',
    '20271011', '20271225', '20271231'
}

_CACHED_TRADING_DAYS = None

def get_krx_trading_days(count=120):
    """
    한국거래소(KRX)의 실제 거래일(개장일) 목록을 반환합니다.
    1. [가이드 05] Zero Network Startup & Non-blocking: 사전에 정의된 휴장일 캘린더 기반 즉시 반환(0.00초) 보장
    2. 네이버 증시 API를 통한 동적 검증 시 타임아웃(2초) 및 JSON 타입 안전성 검증 적용
    """
    global _CACHED_TRADING_DAYS
    if _CACHED_TRADING_DAYS is not None and len(_CACHED_TRADING_DAYS) >= count:
        return _CACHED_TRADING_DAYS
        
    # 기본 휴장일 및 주말 제외 알고리즘으로 즉시 유효 거래일 리스트 산출 (0.00초 보장)
    fallback_days = []
    now_kst = get_now_kst()
    d = now_kst
    for _ in range(count * 3):
        d_str = d.strftime('%Y%m%d')
        if d.weekday() < 5 and d_str not in KRX_HOLIDAYS:
            fallback_days.append(d_str)
            if len(fallback_days) >= count:
                break
        d -= datetime.timedelta(days=1)
    default_days = sorted(fallback_days)

    # 네이버 증시 API로 최신 일자 확인 (네트워크 지연 방지를 위해 2초 타임아웃 및 예외 방어)
    days = []
    headers = {'User-Agent': 'Mozilla/5.0'}
    pages_needed = (count + 59) // 60
    try:
        for page in range(1, pages_needed + 1):
            url = f'https://m.stock.naver.com/api/stock/005930/price?pageSize=60&page={page}'
            r = requests.get(url, headers=headers, timeout=2)
            if r.status_code == 200:
                items = r.json()
                if items and isinstance(items, list):
                    days.extend([
                        item['localTradedAt'].replace('-', '') 
                        for item in items 
                        if isinstance(item, dict) and 'localTradedAt' in item
                    ])
                else:
                    break
    except Exception:
        pass
        
    if days and len(days) >= count:
        _CACHED_TRADING_DAYS = sorted(list(set(days)))
    else:
        _CACHED_TRADING_DAYS = default_days
        
    return _CACHED_TRADING_DAYS

def is_krx_trading_day(date_str):
    """주어진 날짜(YYYYMMDD 또는 YYYY-MM-DD)가 실제 거래일인지 판별합니다."""
    clean_date = date_str.replace('-', '')
    trading_days = get_krx_trading_days(120)
    if clean_date in trading_days:
        return True
    # 거래일 목록 범위를 벗어난 과거/미래의 경우 규칙 기반 판별
    try:
        dt = datetime.datetime.strptime(clean_date, "%Y%m%d")
        return (dt.weekday() < 5) and (clean_date not in KRX_HOLIDAYS)
    except:
        return False

def get_now_kst():
    """한국 표준시(KST, UTC+9) datetime 객체 반환"""
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    return now_utc + datetime.timedelta(hours=9)

def get_nearest_business_day(date_str=None):
    """
    주어진 날짜 또는 현재 날짜 기준 가장 가까운 최근 '실제 거래일(개장일)'을 YYYYMMDD 형태로 반환합니다.
    - date_str이 없는 경우: KST 기준 16:00 이전이거나 오늘이 휴장일인 경우 '직전 실제 마감 거래일'을 반환
    - date_str이 있는 경우: 해당 날짜가 실제 거래일이면 그대로 반환, 주말/휴장일인 경우 '직전 실제 거래일'로 자동 보정
    """
    trading_days = get_krx_trading_days(120)
    
    if date_str:
        clean_date = str(date_str).replace('-', '')
        # 이미 실제 거래일 목록에 존재하는 경우
        if clean_date in trading_days:
            return clean_date
        # 휴장일이나 주말인 경우: 해당 날짜 이하(<=)의 가장 최근 실제 거래일 탐색
        earlier_days = [d for d in trading_days if d <= clean_date]
        if earlier_days:
            return earlier_days[-1]
            
        # fallback: 날짜를 하루씩 줄여가며 평일 및 공휴일 아닌 날짜 탐색
        try:
            dt = datetime.datetime.strptime(clean_date, "%Y%m%d")
            while True:
                d_str = dt.strftime("%Y%m%d")
                if dt.weekday() < 5 and d_str not in KRX_HOLIDAYS:
                    return d_str
                dt -= datetime.timedelta(days=1)
        except Exception:
            return clean_date
    
    # date_str이 전달되지 않은 경우 (최신 마감 거래일)
    now_kst = get_now_kst()
    today_str = now_kst.strftime("%Y%m%d")
    
    # 평일 16:00 이후이고 오늘이 실제 거래일 목록에 있으면 오늘 반환
    if now_kst.hour >= 16 and today_str in trading_days:
        return today_str
        
    # 16:00 이전이거나 오늘이 휴장일인 경우: 오늘보다 이전(<)의 가장 최근 거래일
    prior_days = [d for d in trading_days if d < today_str]
    if prior_days:
        return prior_days[-1]
        
    return trading_days[-1] if trading_days else (now_kst - datetime.timedelta(days=1)).strftime("%Y%m%d")

def fetch_market_cap_with_cache(date_str, market="ALL"):
    """
    특정 날짜의 시장 전체 종목 시가총액 정보를 가져오고 캐싱합니다.
    (종가, 시가총액, 거래량, 거래대금, 상장주식수, 종목명)
    - 1순위: SQLite 캐시 DB (krx_market_cache.db) 조회 (초고속 0.01초, KRX 트래픽 0)
    - 2순위: 로컬 pkl 파일 캐시
    - 3순위: pykrx 직접 호출 (백업)
    """
    date_str = get_nearest_business_day(date_str)

    # 1. SQLite 캐시 DB 우선 조회
    df_db = fetch_market_cap_from_db(date_str, market=market)
    if df_db is not None and not df_db.empty:
        return df_db

    cache_path = os.path.join(CACHE_DIR, f"mkt_cap_{market}_{date_str}.pkl")
    
    # 오늘 또는 미래 날짜는 캐시하지 않음
    today_str = get_now_kst().strftime("%Y%m%d")
    is_today = (date_str >= today_str)
    
    if not is_today and os.path.exists(cache_path):
        try:
            with open(cache_path, "rb") as f:
                return pickle.load(f)
        except Exception as e:
            print(f"Cache read error for market cap: {e}")
            
    # API 요청 전 딜레이 부여
    time.sleep(0.5)
    try:
        # pykrx를 통한 시가총액 정보 가져오기
        df = stock.get_market_cap_by_ticker(date_str, market=market)
        
        # 종목명 추가 매핑
        if not df.empty:
            names = []
            for ticker in df.index:
                try:
                    name = stock.get_market_net_purchases_of_equities_by_ticker
                    # 단순 종목명 얻기
                    name = stock.get_market_ticker_name(ticker)
                except:
                    name = ""
                names.append(name)
            df["종목명"] = names
            
        if not is_today and not df.empty:
            with open(cache_path, "wb") as f:
                pickle.dump(df, f)
        return df
    except Exception as e:
        print(f"Failed to fetch market cap for {date_str}: {e}")
        return pd.DataFrame()

def fetch_investor_net_purchases_with_cache(start_date, end_date, market="ALL", investor="기관합계"):
    """
    특정 기간 동안 특정 투자자의 종목별 순매수 데이터를 가져오고 캐싱합니다.
    - investor: '외국인', '기관합계', '연기금', '투신', '사모', '금융투자', '개인' 등
    """
    start_date = get_nearest_business_day(start_date)
    end_date = get_nearest_business_day(end_date)
    
    # 복합 주체 합산 처리 (외국인+연기금, 외국인+투신+연기금 등)
    if "+" in investor:
        sub_investors = [sub.strip() for sub in investor.split("+")]
        dfs = []
        for sub in sub_investors:
            df_sub = fetch_investor_net_purchases_with_cache(start_date, end_date, market=market, investor=sub)
            if not df_sub.empty:
                dfs.append(df_sub[["순매수거래량", "순매수거래대금"]])
        if not dfs:
            return pd.DataFrame()
            
        df_combined = pd.concat(dfs).groupby(level=0).sum()
        
        # 종목명 맵핑 복원
        for sub in sub_investors:
            df_sub = fetch_investor_net_purchases_with_cache(start_date, end_date, market=market, investor=sub)
            if not df_sub.empty and "종목명" in df_sub.columns:
                df_combined["종목명"] = df_sub["종목명"]
                break
                
        # 기존 스키마 호환성 필드 구성
        df_combined["매도거래량"] = 0
        df_combined["매수거래량"] = 0
        df_combined["매도거래대금"] = 0
        df_combined["매수거래대금"] = 0
        return df_combined

    # 투자자 한글명 -> pykrx 인자 매핑
    # pykrx의 get_market_net_purchases_of_equities_by_ticker는 한글 수급 주체명을 인자로 받음
    # (예: '외국인', '기관합계', '연기금', '투신', '사모', '금융투자', '보험', '개인' 등)
    
    cache_key = f"net_buy_{investor}_{market}_{start_date}_{end_date}.pkl"
    cache_path = os.path.join(CACHE_DIR, cache_key)
    
    today_str = get_now_kst().strftime("%Y%m%d")
    is_live = (end_date >= today_str)
    
    if not is_live and os.path.exists(cache_path):
        try:
            with open(cache_path, "rb") as f:
                return pickle.load(f)
        except Exception as e:
            print(f"Cache read error for net purchases: {e}")
            
    time.sleep(0.5)
    try:
        # pykrx 호출
        df = stock.get_market_net_purchases_of_equities_by_ticker(
            start_date, end_date, market=market, investor=investor
        )
        
        if not is_live and not df.empty:
            with open(cache_path, "wb") as f:
                pickle.dump(df, f)
        return df
    except Exception as e:
        print(f"Failed to fetch net purchases ({investor}) from {start_date} to {end_date}: {e}")
        return pd.DataFrame()

def fetch_daily_net_purchases_series(start_date, end_date, ticker):
    """
    특정 종목의 일자별 투자자별 순매수 거래대금 추이를 가져옵니다. (시각화용)
    1차: 로컬 디스크 pkl 캐시
    2차: 백그라운드 자동인증 후 PyKRX 호출 (원 단위)
    3차: KIS 공용 토큰 OpenAPI 폴백 (TR: FHKST01010900)
    """
    start_date = get_nearest_business_day(start_date)
    end_date = get_nearest_business_day(end_date)
    clean_ticker = str(ticker).zfill(6)
    
    cache_path = os.path.join(CACHE_DIR, f"daily_series_{clean_ticker}_{start_date}_{end_date}.pkl")
    today_str = get_now_kst().strftime("%Y%m%d")
    is_live = (end_date >= today_str)
    
    if not is_live and os.path.exists(cache_path):
        try:
            with open(cache_path, "rb") as f:
                return pickle.load(f)
        except Exception as e:
            print(f"Cache read error for daily series: {e}")
            
    time.sleep(0.3)
    # 1. PyKRX 호출
    try:
        df = stock.get_market_trading_value_by_date(start_date, end_date, clean_ticker, detail=True)
        if not is_live and not df.empty:
            with open(cache_path, "wb") as f:
                pickle.dump(df, f)
        if not df.empty:
            return df
    except Exception as e:
        print(f"Failed to fetch daily series for {clean_ticker}: {e}")

    # 2. KIS OpenAPI 폴백 (공용 토큰 FHKST01010900)
    token = get_kis_access_token()
    _load_kis_credentials()
    app_key = os.getenv("KIS_APP_KEY")
    app_secret = os.getenv("KIS_APP_SECRET")
    if token and app_key and app_secret:
        try:
            url = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/inquire-investor"
            headers = {
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {token}",
                "appkey": app_key,
                "appsecret": app_secret,
                "tr_id": "FHKST01010900",
                "custtype": "P"
            }
            params = {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": clean_ticker
            }
            resp = requests.get(url, headers=headers, params=params, timeout=5)
            if resp.status_code == 200:
                raw_out = resp.json().get("output", [])
                records = []
                for row in raw_out:
                    d_raw = str(row.get("stck_bsop_date", ""))
                    if start_date <= d_raw <= end_date:
                        prsn = float(row.get("prsn_ntby_tr_pbmn", 0)) * 1_000_000.0
                        frgn = float(row.get("frgn_ntby_tr_pbmn", 0)) * 1_000_000.0
                        orgn = float(row.get("orgn_ntby_tr_pbmn", 0)) * 1_000_000.0
                        records.append({
                            "날짜": f"{d_raw[:4]}-{d_raw[4:6]}-{d_raw[6:]}",
                            "금융투자": 0.0,
                            "보험": 0.0,
                            "투신": 0.0,
                            "사모": 0.0,
                            "은행": 0.0,
                            "기타금융": 0.0,
                            "연기금": 0.0,
                            "기타법인": 0.0,
                            "개인": prsn,
                            "외국인": frgn,
                            "기타외국인": 0.0,
                            "전체": prsn + frgn + orgn,
                            "기관합계": orgn
                        })
                if records:
                    kis_df = pd.DataFrame(records)
                    kis_df["날짜"] = pd.to_datetime(kis_df["날짜"])
                    kis_df = kis_df.set_index("날짜").sort_index()
                    return kis_df
        except Exception as e_kis:
            print(f"[Investor] KIS API fallback notice in 27 MajorsNetBuy: {e_kis}")

    return pd.DataFrame()

def get_latest_expected_trading_day(target_date: str = None) -> str:
    """
    가장 최근 거래 완료된 실제 영업일 YYYY-MM-DD 반환.
    - target_date가 전달된 경우: 해당 날짜 기준 (또는 직전 영업일)
    - target_date가 없는 경우: KST 기준 15:45 이전이거나 오늘이 주말/새벽이면 직전 마감 거래일 반환
    """
    from datetime import datetime, timezone, timedelta
    now_kst = datetime.now(timezone(timedelta(hours=9)))
    if target_date:
        try:
            clean_date = str(target_date).replace('-', '')
            dt = datetime.strptime(clean_date, "%Y%m%d").replace(tzinfo=timezone(timedelta(hours=9)))
        except Exception:
            dt = now_kst
    else:
        dt = now_kst

    # 평일 15:45 이후에만 당일 종가 확정
    if dt.weekday() < 5 and (dt.hour > 15 or (dt.hour == 15 and dt.minute >= 45)):
        return dt.strftime("%Y-%m-%d")

    # 장전, 새벽, 주말: 직전 마감 거래일 산출
    if dt.weekday() == 0:    # 월요일 장전 -> 지난주 금요일 (3일 전)
        days_back = 3
    elif dt.weekday() == 6:  # 일요일 -> 지난주 금요일 (2일 전)
        days_back = 2
    elif dt.weekday() == 5:  # 토요일 -> 지난주 금요일 (1일 전)
        days_back = 1
    else:                    # 화~금 장전/새벽 -> 전일 (1일 전)
        days_back = 1

    return (dt - timedelta(days=days_back)).strftime("%Y-%m-%d")
