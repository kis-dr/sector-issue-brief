"""
US Movers 처리 로직
- xlsx 로드 → 변동 종목 필터 → 뉴스 매칭/수집 → Gemini 사유분석 + WICS 매핑
- 기존 ipynb의 _fetch_paginated, classify_wics 패턴을 따름
- ipynb에서 import해서 사용하거나, 셀에 그대로 붙여넣기 가능
"""

import glob, os, re, time, json, random, threading
from datetime import datetime, timedelta, date as _date
from concurrent.futures import ThreadPoolExecutor, as_completed
import yfinance as yf
import numpy as np
import pandas as pd
import requests

import logging
logger = logging.getLogger(__name__)


def _gemini_call_with_fallback(gemini_client, config, contents, models,
                                round_waits=(30, 60, 120)):
    """
    GEMINI_MODELS 리스트 순서대로 호출 시도. 429/RESOURCE_EXHAUSTED 시 다음 모델로.
    모든 모델이 한 라운드에서 다 실패하면 round_waits 만큼 대기 후 다시 라운드 재시도.
    최종 실패 시 마지막 에러 raise.
    """
    if not models:
        raise ValueError("GEMINI_MODELS 리스트가 비어있음")

    last_err = None
    total_rounds = 1 + len(round_waits)
    for round_idx in range(total_rounds):
        if round_idx > 0:
            wait = round_waits[round_idx - 1]
            logger.warning(f"  [Gemini] 모든 모델 실패 → {wait}초 대기 후 라운드 {round_idx+1}/{total_rounds} 재시도")
            time.sleep(wait)
        for m in models:
            try:
                return gemini_client.models.generate_content(
                    model=m, contents=contents, config=config
                )
            except Exception as e:
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                    last_err = e
                    continue
                raise
    raise last_err or RuntimeError("Gemini 모든 모델/라운드 실패")


# =============================================================================
# 0-extra) 미국 종목 yfinance 데이터 (fwd P/E·P/B + earnings + news, 세션 공유·종목당 Ticker 1회)
# =============================================================================

# ─── yfinance 뉴스 정규화 / 종목 관련성 필터 ─────────────────────────────────
_NAME_SUFFIX_RE = re.compile(
    r'\b(?:inc|incorporated|corp|corporation|co|company|ltd|limited|plc|'
    r'holdings?|group|the|sa|nv|ag|class\s+[a-c])\b\.?', re.IGNORECASE)


def _core_name(name: str) -> str:
    """회사명에서 법인 접미사 등 제거한 핵심 명칭. 예: 'Apple Inc' → 'apple'."""
    n = re.sub(r'[.,]', ' ', name or '')
    n = _NAME_SUFFIX_RE.sub(' ', n)
    n = re.sub(r'\s+', ' ', n).strip()
    return n


def _symbol_in_text(text: str, symbol: str) -> bool:
    """대문자 심볼이 토큰 경계 안에서 등장하는지 (원문 대소문자 유지로 'all'→'ALL' 오탐 방지)."""
    if not symbol or len(symbol) < 2:
        return False
    s = re.escape(symbol.upper())
    return re.search(rf'(?<![A-Za-z0-9]){s}(?![A-Za-z0-9])', text) is not None


# 회사명 첫 토큰이 너무 일반적이면 단독 매칭에서 제외 (오탐 방지)
_GENERIC_NAME_TOKENS = {
    'advanced', 'general', 'american', 'united', 'first', 'national', 'global',
    'international', 'new', 'western', 'eastern', 'northern', 'southern',
    'central', 'pacific', 'atlantic', 'standard', 'premier', 'allied',
    'consolidated', 'universal', 'continental',
}


def _name_in_text(text_lower: str, name: str) -> bool:
    core = _core_name(name).lower()
    if not core:
        return False
    # 1) 핵심명 전체 구절 매칭 (정밀)
    if len(core) >= 3 and re.search(rf'(?<![a-z0-9]){re.escape(core)}(?![a-z0-9])', text_lower):
        return True
    # 2) 첫 토큰이 충분히 distinctive하면 단독 매칭 (리콜 보강; 예: 'Micron Technology'→'Micron')
    parts = core.split()
    first = parts[0] if parts else ''
    if len(first) >= 4 and first not in _GENERIC_NAME_TOKENS:
        if re.search(rf'(?<![a-z0-9]){re.escape(first)}(?![a-z0-9])', text_lower):
            return True
    return False


def _news_relevant(title: str, summary: str, symbol: str, name: str) -> bool:
    """제목/요약에 심볼(대문자 토큰) 또는 회사 핵심명(단어 경계)이 있으면 관련 기사로 판정."""
    raw = f"{title} {summary}"
    return _symbol_in_text(raw, symbol) or _name_in_text(raw.lower(), name)


def _normalize_yf_news(item: dict) -> dict:
    """yfinance get_news() 항목 → 내부 뉴스 dict 스키마로 정규화.
    신규 스키마(content 래퍼) 우선, 구버전(flat) fallback."""
    c = item.get('content') if isinstance(item.get('content'), dict) else item
    title = c.get('title') or ''
    summary = c.get('summary') or c.get('description') or ''
    pub = c.get('pubDate') or c.get('displayTime') or c.get('providerPublishTime') or ''
    url = ''
    cu = c.get('canonicalUrl') or {}
    tu = c.get('clickThroughUrl') or {}
    if isinstance(cu, dict):
        url = cu.get('url') or ''
    if not url and isinstance(tu, dict):
        url = tu.get('url') or ''
    if not url:
        url = c.get('link') or ''
    prov = c.get('provider')
    source = prov.get('displayName', '') if isinstance(prov, dict) else (c.get('publisher') or '')
    return {
        'title':        title,
        'summary':      str(summary)[:1000],
        'published_at': str(pub),
        'source':       source,
        'content_url':  url,
        'symbols':      [],
    }


def _merge_news(ds_list, yf_list, max_per_stock: int = 10) -> list[dict]:
    """DeepSearch + yfinance 뉴스 병합 → 제목·URL 중복 제거 → 최신순 → max_per_stock cap."""
    combined = list(ds_list or []) + list(yf_list or [])
    combined.sort(key=lambda x: str(x.get('published_at') or ''), reverse=True)
    seen_title, seen_url = set(), set()
    out = []
    for n in combined:
        title = (n.get('title') or '').strip().lower()
        url = (n.get('content_url') or n.get('url') or '').strip()
        if title and title in seen_title:
            continue
        if url and url in seen_url:
            continue
        if title:
            seen_title.add(title)
        if url:
            seen_url.add(url)
        out.append(n)
        if len(out) >= max_per_stock:
            break
    return out


def fetch_yf_data_for_tickers(tickers: list[str], name_map: dict[str, str] | None = None,
                              max_workers: int = 4, max_rows: int = 5,
                              max_news: int = 10) -> dict[str, dict]:
    """
    종목당 yf.Ticker를 1회만 생성해 fwd P/E·P/B, earnings, news를 함께 수집.
    - 뉴스는 제목/요약에 심볼 또는 회사명이 들어간 '종목 관련' 기사만 필터링.
    Returns: {ticker: {"fwd_pe", "fwd_pb", "earnings": [...], "news": [...]}}
    """
    name_map = name_map or {}
    from curl_cffi import requests

    session = requests.Session(verify=False)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    out = {}
    lock = threading.Lock()

    def _one(ticker):
        rec = {"fwd_pe": None, "fwd_pb": None, "earnings": [], "news": []}
        try:
            stk = yf.Ticker(ticker, session=session)   # ← 종목당 Ticker 1회 생성
        except Exception:
            with lock:
                out[ticker] = rec
            return

        # 1) info → fwd P/E, P/B
        try:
            info = stk.info or {}
            pe = info.get('forwardPE')
            pb = info.get('priceToBook')
            if pe and pe > 0:
                rec["fwd_pe"] = float(pe)
            if pb and pb > 0:
                rec["fwd_pb"] = float(pb)
        except Exception:
            pass

        # 2) earnings_dates → 최근 max_rows건
        try:
            ed = stk.earnings_dates
            if ed is not None and not ed.empty:
                rows = []
                for dt, r in ed.head(max_rows).iterrows():
                    rows.append({
                        "date":         dt.strftime("%Y-%m-%d") if hasattr(dt, 'strftime') else str(dt)[:10],
                        "eps_est":      round(float(r['EPS Estimate']), 2) if pd.notna(r.get('EPS Estimate')) else None,
                        "eps_actual":   round(float(r['Reported EPS']), 2) if pd.notna(r.get('Reported EPS')) else None,
                        "surprise_pct": round(float(r['Surprise(%)']), 2) if pd.notna(r.get('Surprise(%)')) else None,
                    })
                rec["earnings"] = rows
        except Exception:
            pass

        # 3) get_news → 정규화 + 종목 관련만 필터
        try:
            raw_news = stk.get_news() or []
            name = name_map.get(ticker, '')
            picked = []
            for it in raw_news:
                ne = _normalize_yf_news(it)
                if not ne['title']:
                    continue
                if _news_relevant(ne['title'], ne['summary'], ticker, name):
                    picked.append(ne)
                if len(picked) >= max_news:
                    break
            rec["news"] = picked
        except Exception:
            pass

        with lock:
            out[ticker] = rec

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(_one, tickers))

    n_pe = sum(1 for v in out.values() if v["fwd_pe"] is not None)
    n_news = sum(1 for v in out.values() if v["news"])
    logger.info(f"[yfinance] {len(out)}/{len(tickers)} 종목 (PE {n_pe}, 뉴스보유 {n_news})")
    return out

# ─── 외부 의존 (ipynb 환경에서 이미 정의된 것들) ────────────────────────────
# from __main__ import (
#     gemini_client, GEMINI_MODELS, types,
#     header_deepsearch, DS_AUTH,
#     coverage_sector_list,    # 45개 커버 WICS 3rd-level
#     _fetch_paginated,        # ipynb cell 7
# )

# =============================================================================
# 1) xlsx 로드
# =============================================================================

US_FOLDER = r"\\197.197.26.121\계량분석\★ FY26 디지털리서치사업 ★\Digital_RA\issue_briefing_by_sector\DATA"

def load_us_movers_xlsx(target_date: str, folder: str = US_FOLDER) -> pd.DataFrame | None:
    """
    오늘(KST) 기준 가장 최신 US_stk_top_1000({date}).xlsx 로드.
    - KST 07:00 실행 시 미국 전일 종가 파일이 당일 날짜로 생성됨.
    - target_date는 참고용, 실제로는 '오늘 이하 가장 최신' 파일을 선택.
    """
    from datetime import date as _date
    today_str = _date.today().strftime('%Y-%m-%d')

    files = glob.glob(os.path.join(folder, "US_stk_top_1000(*).xlsx"))
    if not files:
        logger.info(f"[US movers] 파일 없음: {folder}")
        return None

    dated = []
    for f in files:
        m = re.search(r'US_stk_top_1000\((\d{4}-\d{2}-\d{2})\)', f)
        if m:
            dated.append((m.group(1), f))

    # 오늘 이하 최신 파일 선택
    candidates = sorted([(d, f) for d, f in dated if d <= today_str], reverse=True)
    if not candidates:
        logger.info(f"[US movers] {today_str} 이하 파일 없음 → 스킵")
        return None

    date_used, fpath = candidates[0]
    df = pd.read_excel(fpath)
    logger.info(f"[US movers] 로드: {os.path.basename(fpath)} ({len(df):,}건) ← 기준일 {date_used}")
    return df


# =============================================================================
# 2) 변동 종목 필터
# =============================================================================

def filter_us_movers(df: pd.DataFrame, change_threshold: float = 3.0) -> pd.DataFrame:
    """|change|≥3% OR 52주 최고/최저 발생 종목만 추출."""
    cond_change = df['change'].abs() >= change_threshold
    cond_high   = df['오늘52주최고가여부'].astype(str).str.upper() == 'Y'
    cond_low    = df['오늘52주최저가여부'].astype(str).str.upper() == 'Y'
    movers = df[cond_change | cond_high | cond_low].copy()

    movers['is_52w_high']  = cond_high.loc[movers.index]
    movers['is_52w_low']   = cond_low.loc[movers.index]
    logger.info(f"[US movers] 필터링: {len(df):,}건 → {len(movers):,}건 "
          f"(변동≥{change_threshold}%: {cond_change.sum()}, 52w_high: {cond_high.sum()}, 52w_low: {cond_low.sum()})")
    return movers.reset_index(drop=True)


# =============================================================================
# 4) 부족분 DeepSearch 호출 (어제자, 종목당 최대 10건)
# =============================================================================

def _ds_get_with_retry(url: str, header_deepsearch: str, ds_auth,
                       max_retry: int = 3, timeout: int = 30) -> list[dict]:
    """DeepSearch API GET 호출 + 재시도. 페이지네이션 포함."""
    all_data = []
    page = 1
    max_pages = 5
 
    while page <= max_pages:
        sep = "&" if "?" in url else "?"
        paged_url = f"{url}{sep}page={page}" if page > 1 else url
 
        for attempt in range(max_retry):
            try:
                resp = requests.get(
                    paged_url,
                    headers={"Authorization": header_deepsearch},
                    auth=ds_auth,
                    verify=False,
                    timeout=timeout,
                )
                if resp.status_code == 429:
                    wait = 2 ** (attempt + 1)
                    logger.info(f"    [DS] 429 rate limit → {wait}s 대기")
                    time.sleep(wait)
                    continue
                if resp.status_code != 200:
                    logger.info(f"    [DS] HTTP {resp.status_code}: {resp.text[:200]}")
                    break  # 이 페이지 포기
 
                j = resp.json()
                data = j.get("data", [])
                if not data:
                    return all_data  # 더 이상 데이터 없음
 
                all_data.extend(data)
 
                # 페이지네이션: total_pages 또는 data가 page_size 미만이면 종료
                total_pages = j.get("total_pages", 1)
                if page >= total_pages or len(data) < 100:
                    return all_data
                break  # 다음 페이지로
 
            except requests.exceptions.Timeout:
                if attempt < max_retry - 1:
                    time.sleep(2 ** attempt)
                else:
                    logger.info(f"    [DS] timeout (page {page})")
                    return all_data
            except Exception as e:
                if attempt < max_retry - 1:
                    time.sleep(2 ** attempt)
                else:
                    logger.warning(f"    [DS] error (page {page}): {e}")
                    return all_data
 
        page += 1
 
    return all_data

def fetch_movers_news_deepsearch(tickers: list[str], date_str: str,
                                  exchange_map: dict[str, str],
                                  max_per_stock: int = 10,
                                  batch_size: int = 20,
                                  header_deepsearch: str = None,
                                  ds_auth=None) -> dict[str, list[dict]]:
    """
    DeepSearch global-articles?symbols=... 호출.
    여러 ticker를 batch_size 단위로 한 번에 호출.
    Returns: {ticker: [...]}
    """
    if not tickers:
        return {}

    # symbols 파라미터: NYSE:AAPL,NASDAQ:MSFT,...
    def to_symbol(t):
        ex = exchange_map.get(t, '')
        return f"{ex}:{t}" if ex else t

    by_ticker = {t: [] for t in tickers}
    batches = [tickers[i:i+batch_size] for i in range(0, len(tickers), batch_size)]

    for batch in batches:
        symbols = ",".join(to_symbol(t) for t in batch)
        try:
            url = (f"https://api-v2.deepsearch.com/v1/global-articles"
                    f"?symbols={symbols}"
                    f"&date_from={date_str}"
                    f"&page_size=100")
            data = _ds_get_with_retry(url, header_deepsearch, ds_auth)
        except Exception as e:
            logger.warning(f"  [DeepSearch] batch 실패: {e}")
            continue

        # ticker별로 다시 분배 (한 기사가 여러 symbols 포함 가능)
        for art in data:
            # companies 필드에서 ticker 추출
            art_tickers = set()
            for comp in (art.get('companies') or []):
                sym = comp.get('symbol', '')
                if sym:
                    art_tickers.add(sym)
            # fallback: symbols 필드 (혹시 채워지는 경우 대비)
            for sym in (art.get('symbols') or []):
                art_tickers.add(sym.split(':')[-1])

            if not art_tickers:
                continue
            title = art.get('title_ko') or art.get('title') or ''
            summary = art.get('summary_ko') or art.get('summary') or ''
            entry = {
                'title':        title,
                'summary':      str(summary)[:1000],
                'published_at': str(art.get('published_at', '')),
                'source':       art.get('publisher', ''),
                'content_url':  art.get('content_url', '') or art.get('url', ''),
                'symbols':      art.get('companies', []),
            }
            for t in art_tickers:
                if t in by_ticker:
                    by_ticker[t].append(entry)

    # 최신순 정렬 + 중복 제거 + 종목당 max
    for t in by_ticker:
        by_ticker[t].sort(key=lambda x: x['published_at'], reverse=True)
        seen = set()
        deduped = []
        for art in by_ticker[t]:
            title = art.get('title', '').strip()
            if title and title in seen:
                continue
            seen.add(title)
            deduped.append(art)
        by_ticker[t] = deduped[:max_per_stock]
    return by_ticker


# =============================================================================
# 5) sector/industry → WICS 3rd-level 매핑 dict (Gemini 분류 대체)
# =============================================================================
# 보수적 매핑: 한국 종목과 직접 경쟁/공급망/사이클 동조화 관계만 매핑
# 1:다 가능, sector/industry 1조합이 여러 WICS에 매핑될 수 있음
# 매핑할 가치 없는 (한국 시장과 무관한) industry는 키에서 제외 = 빈 배열 효과

SECTOR_INDUSTRY_TO_WICS: dict[tuple[str, str], list[str]] = {
    # ── 기술주 ──
    ('기술주', 'semiconductors'):                    ['IT-반도체와반도체장비-반도체와반도체장비'],
    ('기술주', 'semiconductor-equipment-materials'): ['IT-반도체와반도체장비-반도체와반도체장비'],
    ('기술주', 'computer-hardware'):                 ['IT-기술하드웨어와장비-컴퓨터와주변기기'],
    ('기술주', 'consumer-electronics'):              ['IT-전자와 전기제품-전자제품',
                                                       'IT-전자와 전기제품-전기제품'],
    ('기술주', 'electronic-components'):             ['IT-전자와 전기제품-전자제품',
                                                       'IT-기술하드웨어와장비-전자장비와기기'],
    ('기술주', 'communication-equipment'):           ['IT-기술하드웨어와장비-통신장비'],
    ('기술주', 'scientific-technical-instruments'):  ['IT-전자와 전기제품-전자제품'],
    ('기술주', 'software-application'):              ['IT-소프트웨어와서비스-소프트웨어'],
    ('기술주', 'software-infrastructure'):           ['IT-소프트웨어와서비스-소프트웨어'],
    ('기술주', 'information-technology-services'):   ['IT-소프트웨어와서비스-IT서비스'],
    ('기술주', 'solar'):                             ['IT-반도체와반도체장비-반도체와반도체장비'],
    # electronics-computer-distribution: 유통이라 제외

    # ── 통신 ──
    ('통신', 'internet-content-information'):        ['커뮤니케이션서비스-미디어와엔터테인먼트-양방향미디어와서비스'],
    ('통신', 'electronic-gaming-multimedia'):        ['커뮤니케이션서비스-미디어와엔터테인먼트-게임엔터테인먼트'],
    ('통신', 'entertainment'):                        ['커뮤니케이션서비스-미디어와엔터테인먼트-방송과엔터테인먼트'],
    ('통신', 'telecom-services'):                    ['커뮤니케이션서비스-전기통신서비스-다각화된통신서비스'],
    ('통신', 'advertising-agencies'):                ['커뮤니케이션서비스-미디어와엔터테인먼트-광고'],
    ('통신', 'publishing'):                           ['커뮤니케이션서비스-미디어와엔터테인먼트-출판'],

    # ── 헬스케어 ──
    ('헬스케어', 'biotechnology'):                    ['건강관리-제약과생물공학-생물공학'],
    ('헬스케어', 'drug-manufacturers-general'):       ['건강관리-제약과생물공학-제약'],
    ('헬스케어', 'drug-manufacturers-specialty-generic'): ['건강관리-제약과생물공학-제약'],
    ('헬스케어', 'medical-devices'):                  ['건강관리-건강관리장비와서비스-건강관리장비와용품'],
    ('헬스케어', 'medical-instruments-supplies'):     ['건강관리-건강관리장비와서비스-건강관리장비와용품'],
    ('헬스케어', 'diagnostics-research'):             ['건강관리-건강관리장비와서비스-건강관리기술'],
    ('헬스케어', 'health-information-services'):      ['건강관리-건강관리장비와서비스-건강관리기술'],
    ('헬스케어', 'healthcare-plans'):                 ['건강관리-건강관리장비와서비스-건강관리업체및서비스'],
    ('헬스케어', 'medical-care-facilities'):          ['건강관리-건강관리장비와서비스-건강관리업체및서비스'],

    # ── 경기소비재 ──
    ('경기소비재', 'auto-manufacturers'):             ['경기관련소비재-자동차와부품-자동차'],
    ('경기소비재', 'auto-parts'):                      ['경기관련소비재-자동차와부품-자동차부품'],
    ('경기소비재', 'apparel-manufacturing'):          ['경기관련소비재-내구소비재와의류-섬유,의류,신발,호화품'],
    ('경기소비재', 'apparel-retail'):                 ['경기관련소비재-소매(유통)-전문소매'],
    ('경기소비재', 'footwear-accessories'):           ['경기관련소비재-내구소비재와의류-섬유,의류,신발,호화품'],
    ('경기소비재', 'luxury-goods'):                   ['경기관련소비재-내구소비재와의류-섬유,의류,신발,호화품'],
    ('경기소비재', 'internet-retail'):                ['경기관련소비재-소매(유통)-인터넷과카탈로그소매'],
    ('경기소비재', 'department-stores'):              ['경기관련소비재-소매(유통)-백화점과일반상점'],
    ('경기소비재', 'home-improvement-retail'):        ['경기관련소비재-소매(유통)-전문소매'],
    ('경기소비재', 'specialty-retail'):               ['경기관련소비재-소매(유통)-전문소매'],
    ('경기소비재', 'restaurants'):                    ['경기관련소비재-호텔,레스토랑,레저 등-호텔,레스토랑,레저'],
    ('경기소비재', 'lodging'):                         ['경기관련소비재-호텔,레스토랑,레저 등-호텔,레스토랑,레저'],
    ('경기소비재', 'leisure'):                         ['경기관련소비재-호텔,레스토랑,레저 등-호텔,레스토랑,레저'],
    ('경기소비재', 'travel-services'):                ['경기관련소비재-호텔,레스토랑,레저 등-호텔,레스토랑,레저'],
    ('경기소비재', 'resorts-casinos'):                ['경기관련소비재-호텔,레스토랑,레저 등-호텔,레스토랑,레저'],
    ('경기소비재', 'gambling'):                        ['경기관련소비재-호텔,레스토랑,레저 등-호텔,레스토랑,레저'],
    ('경기소비재', 'residential-construction'):       ['산업재-자본재-건설'],
    ('경기소비재', 'furnishings-fixtures-appliances'): ['산업재-자본재-가구',
                                                       'IT-전자와 전기제품-전기제품'],
    ('경기소비재', 'packaging-containers'):           ['소재-소재-포장재'],
    # auto-truck-dealerships, personal-services: 한국 종목과 직접 연결 약해서 제외

    # ── 필수소비재 ──
    ('필수소비재', 'beverages-brewers'):              ['필수소비재-식품,음료,담배-음료'],
    ('필수소비재', 'beverages-non-alcoholic'):        ['필수소비재-식품,음료,담배-음료'],
    ('필수소비재', 'beverages-wineries-distilleries'):['필수소비재-식품,음료,담배-음료'],
    ('필수소비재', 'tobacco'):                         ['필수소비재-식품,음료,담배-담배'],
    ('필수소비재', 'packaged-foods'):                 ['필수소비재-식품,음료,담배-식품'],
    ('필수소비재', 'farm-products'):                  ['필수소비재-식품,음료,담배-식품'],
    ('필수소비재', 'confectioners'):                  ['필수소비재-식품,음료,담배-식품'],
    ('필수소비재', 'food-distribution'):              ['필수소비재-식품과기본식료품소매-식품과기본식료품소매'],
    ('필수소비재', 'grocery-stores'):                 ['필수소비재-식품과기본식료품소매-식품과기본식료품소매'],
    ('필수소비재', 'discount-stores'):                ['필수소비재-식품과기본식료품소매-식품과기본식료품소매'],
    ('필수소비재', 'household-personal-products'):    ['필수소비재-가정용품과개인용품-가정용품'],
    # education-training-services: 한국 시장 직접 연결 약함

    # ── 산업재 ──
    ('산업재', 'aerospace-defense'):                  ['산업재-자본재-우주항공과국방'],
    ('산업재', 'airlines'):                            ['산업재-운송-항공사'],
    ('산업재', 'airports-air-services'):              ['산업재-운송-운송인프라'],
    ('산업재', 'integrated-freight-logistics'):       ['산업재-운송-항공화물운송과물류'],
    ('산업재', 'marine-shipping'):                    ['산업재-운송-해운사'],
    ('산업재', 'trucking'):                            ['산업재-운송-도로와철도운송'],
    ('산업재', 'railroads'):                           ['산업재-운송-도로와철도운송'],
    ('산업재', 'farm-heavy-construction-machinery'):  ['산업재-자본재-기계'],
    ('산업재', 'specialty-industrial-machinery'):     ['산업재-자본재-기계'],
    ('산업재', 'tools-accessories'):                  ['산업재-자본재-기계'],
    ('산업재', 'metal-fabrication'):                  ['산업재-자본재-기계'],
    ('산업재', 'building-products-equipment'):        ['산업재-자본재-건축자재'],
    ('산업재', 'engineering-construction'):           ['산업재-자본재-건설'],
    ('산업재', 'electrical-equipment-parts'):         ['IT-기술하드웨어와장비-전자장비와기기',
                                                       'IT-전자와 전기제품-전기제품'],
    ('산업재', 'industrial-distribution'):            ['산업재-자본재-무역회사와판매업체'],
    ('산업재', 'rental-leasing-services'):            ['산업재-상업서비스와공급품-상업서비스와공급품'],
    ('산업재', 'specialty-business-services'):        ['산업재-상업서비스와공급품-상업서비스와공급품'],
    ('산업재', 'security-protection-services'):       ['산업재-상업서비스와공급품-상업서비스와공급품'],
    ('산업재', 'pollution-treatment-controls'):       ['산업재-상업서비스와공급품-상업서비스와공급품'],
    ('산업재', 'waste-management'):                   ['산업재-상업서비스와공급품-상업서비스와공급품'],
    ('산업재', 'consulting-services'):                ['산업재-상업서비스와공급품-상업서비스와공급품'],
    # conglomerates: 너무 광범위해서 제외

    # ── 금융 ──
    ('금융', 'banks-diversified'):                    ['금융-은행-은행'],
    ('금융', 'banks-regional'):                       ['금융-은행-은행'],
    ('금융', 'asset-management'):                     ['금융-증권-증권'],
    ('금융', 'capital-markets'):                       ['금융-증권-증권'],
    ('금융', 'financial-data-stock-exchanges'):        ['금융-증권-증권'],
    ('금융', 'insurance-life'):                        ['금융-보험-생명보험'],
    ('금융', 'insurance-property-casualty'):           ['금융-보험-손해보험'],
    ('금융', 'insurance-reinsurance'):                 ['금융-보험-손해보험'],
    ('금융', 'insurance-diversified'):                 ['금융-보험-생명보험', '금융-보험-손해보험'],
    ('금융', 'insurance-brokers'):                     ['금융-보험-손해보험'],
    ('금융', 'insurance-specialty'):                   ['금융-보험-손해보험'],
    ('금융', 'credit-services'):                       ['금융-다각화된금융-기타금융'],
    ('금융', 'mortgage-finance'):                      ['금융-다각화된금융-기타금융'],
    ('금융', 'financial-conglomerates'):               ['금융-다각화된금융-기타금융'],

    # ── 부동산 ──
    ('부동산', 'real-estate-services'):                ['금융-부동산-부동산'],
    ('부동산', 'reit-diversified'):                    ['금융-부동산-부동산'],
    ('부동산', 'reit-residential'):                    ['금융-부동산-부동산'],
    ('부동산', 'reit-retail'):                         ['금융-부동산-부동산'],
    ('부동산', 'reit-office'):                          ['금융-부동산-부동산'],
    ('부동산', 'reit-industrial'):                     ['금융-부동산-부동산'],
    ('부동산', 'reit-healthcare-facilities'):          ['금융-부동산-부동산'],
    ('부동산', 'reit-hotel-motel'):                    ['금융-부동산-부동산'],
    ('부동산', 'reit-mortgage'):                        ['금융-부동산-부동산'],
    ('부동산', 'reit-specialty'):                       ['금융-부동산-부동산'],

    # ── 에너지 ──
    ('에너지', 'oil-gas-e-p'):                          ['에너지-에너지-석유와가스'],
    ('에너지', 'oil-gas-integrated'):                  ['에너지-에너지-석유와가스'],
    ('에너지', 'oil-gas-refining-marketing'):          ['에너지-에너지-석유와가스'],
    ('에너지', 'oil-gas-midstream'):                   ['에너지-에너지-석유와가스'],
    ('에너지', 'oil-gas-equipment-services'):          ['에너지-에너지-에너지장비및서비스'],
    ('에너지', 'uranium'):                              ['에너지-에너지-석유와가스'],

    # ── 기초소재 ──
    ('기초소재', 'chemicals'):                          ['소재-소재-화학'],
    ('기초소재', 'specialty-chemicals'):                ['소재-소재-화학'],
    ('기초소재', 'agricultural-inputs'):                ['소재-소재-화학'],
    ('기초소재', 'steel'):                              ['소재-소재-철강'],
    ('기초소재', 'aluminum'):                           ['소재-소재-비철금속'],
    ('기초소재', 'copper'):                              ['소재-소재-비철금속'],
    ('기초소재', 'gold'):                                ['소재-소재-비철금속'],
    ('기초소재', 'silver'):                              ['소재-소재-비철금속'],
    ('기초소재', 'other-precious-metals-mining'):       ['소재-소재-비철금속'],
    ('기초소재', 'other-industrial-metals-mining'):     ['소재-소재-비철금속'],
    ('기초소재', 'building-materials'):                 ['산업재-자본재-건축자재'],

    # ── 공공서비스 ──
    ('공공서비스', 'utilities-regulated-electric'):    ['유틸리티-유틸리티-전기유틸리티'],
    ('공공서비스', 'utilities-renewable'):              ['유틸리티-유틸리티-전기유틸리티'],
    ('공공서비스', 'utilities-independent-power-producers'): ['유틸리티-유틸리티-전기유틸리티'],
    ('공공서비스', 'utilities-regulated-gas'):          ['유틸리티-유틸리티-가스유틸리티'],
    ('공공서비스', 'utilities-regulated-water'):        ['유틸리티-유틸리티-복합유틸리티'],
    ('공공서비스', 'utilities-diversified'):            ['유틸리티-유틸리티-복합유틸리티'],

    # ('기타', '*'): 매핑 없음
}


def map_us_to_wics(sector: str, industry: str) -> list[str]:
    """sector/industry → WICS 3rd-level 리스트. 매핑 없으면 빈 배열."""
    return SECTOR_INDUSTRY_TO_WICS.get((str(sector).strip(), str(industry).strip()), [])


# =============================================================================
# 6) Gemini: 변동 사유만 추출 (WICS는 dict 룩업으로 대체)
# =============================================================================

ANALYSIS_PROMPT = """너는 한국 주식시장 셀사이드 애널리스트다.

미국 종목 리스트가 주어진다. 각 종목마다:
- 일간 변동률(change_pct)과 그 부호로 확정된 방향(direction: 상승/하락/보합)
- 52주 최고/최저 발생 여부(flag)
- 어제자 이후 최신 종목별 관련 뉴스 (최대 10건, 최신순)

변동의 핵심 원인을 한 줄로 요약하라 (50~60자).
- 애널리스트가 볼 요약문이기 때문에 러프하고 쉽게 표현하지말고, 전문적이고 구체적으로 작성하되 간결하게 작성할 것
- 사족 금지, 당연한 말 금지, 결정적 사유만
- 예시1) 퀄컴(QCOM) 주가 하락 코멘트
    - 좋은 예: "엔비디아가 AI PC용 신규 반도체를 공개하면서, 퀄컴의 스냅드래곤 칩 경쟁력 악화 가능성"
    - 나쁜 예: "반도체 경쟁 심화로 주가 하락", "투자자 관심 소외"
- 예시2) 일라이릴리(LLY) 주가 상승 코멘트
    - 좋은 예: "CVS Caremark가 경구용 GLP-1 약물 Foundayo에 대한 신규 출시 제한 해제 발표"
    - 나쁜 예: "당뇨병 치료제 수요 증가로 주가 상승"
- 뉴스가 부실해서 사유 추정 불가하면 "사유 미상"

[방향 규칙 — 반드시 준수]
- change_pct의 부호가 그날 방향(direction)을 확정하는 ground truth다. change_pct>0이면 상승, <0이면 하락, 0이면 보합이다.
- 사유(reason)는 반드시 이 방향과 일치해야 한다. 상승 종목에 "하락/실망 매물/약세" 같은 하락 서사를, 하락 종목에 "급등/호재로 상승" 같은 상승 서사를 절대 쓰지 마라.
- 뉴스가 부호와 반대 방향을 시사하더라도(예: 상승했는데 "실적 컨센 하회" 기사) change_pct 부호가 우선이다. 이때는 그 방향을 설명할 다른 요인(매출·가이던스 호조, 수급, 동종업계 모멘텀 등)을 뉴스에서 찾아 작성하고, 마땅한 근거가 없으면 "사유 미상"으로 하라.
- 각 종목마다 direction(상승/하락/보합)을 reason과 함께 출력하라. direction은 입력으로 준 change_pct 부호와 반드시 동일해야 한다.

각 항목은 {"direction": "...", "reason": "..."} 형태의 JSON으로, 배열로 반환. 입력 종목 순서와 동일하게."""


# change_pct 부호 → 방향 라벨 (ground truth)
def _dir_of(chg) -> str:
    if chg > 0:
        return "상승"
    if chg < 0:
        return "하락"
    return "보합"


def _resolve_reason(reason: str, model_dir: str, change_pct) -> str:
    """모델이 답한 방향(model_dir)이 change_pct 부호와 모순이면 사유 미상으로 강등.
    - 보합(change_pct==0) 또는 모델이 방향을 안 준 경우는 검증 생략(원문 유지)."""
    reason = reason or "사유 미상"
    actual_dir = _dir_of(change_pct)
    md = (model_dir or "").strip()
    if reason != "사유 미상" and actual_dir != "보합" and md and md != actual_dir:
        return "사유 미상"
    return reason


def analyze_movers_with_gemini(movers: pd.DataFrame,
                                news_by_ticker : dict[str, list[dict]],
                                coverage_sectors: list[str],
                                gemini_client, GEMINI_MODELS: list[str], types,
                                batch_size: int = 10,
                                max_news_per_stock: int = 10) -> pd.DataFrame:
    """
    movers DataFrame에 reason, wics_sectors 컬럼 추가.
    Gemini 한 번 호출로 두 작업을 동시에 처리.
    """
    if movers.empty:
        return movers.assign(reason=None, wics_sectors=[[] for _ in range(len(movers))])

    SCHEMA = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "direction": {"type": "STRING", "enum": ["상승", "하락", "보합"]},
                "reason": {"type": "STRING"},
            },
            "required": ["direction", "reason"]
        }
    }

    # 입력 데이터 구성
    inputs = []
    for _, mv in movers.iterrows():
        news_list = news_by_ticker.get(mv['Symbol'], [])[:max_news_per_stock]
        flag = []
        if mv.get('is_52w_high'): flag.append("52주최고")
        if mv.get('is_52w_low'):  flag.append("52주최저")
        inputs.append({
            "ticker":     mv['Symbol'],
            "name":       str(mv['name']),
            "change_pct": round(float(mv['change']), 2),
            "direction":  _dir_of(round(float(mv['change']), 2)),
            "flag":       flag,
            "news":       [{"t": n['title'], "s": n['summary'][:1000]} for n in news_list],
        })

    batches = [inputs[i:i+batch_size] for i in range(0, len(inputs), batch_size)]
    results: list[dict] = [None] * len(inputs)

    def _call(batch_idx: int) -> tuple[int, list[dict]]:
        batch_inputs = batches[batch_idx]
        config = types.GenerateContentConfig(
            system_instruction=ANALYSIS_PROMPT,
            response_mime_type="application/json",
            response_schema=SCHEMA,
            temperature=0.0,
        )
        try:
            resp = _gemini_call_with_fallback(
                gemini_client, config,
                json.dumps(batch_inputs, ensure_ascii=False),
                GEMINI_MODELS,
            )
            parsed = json.loads(resp.text)
            if len(parsed) != len(batch_inputs):
                parsed = parsed + [{"reason": "사유 미상"}] * (len(batch_inputs) - len(parsed))
            time.sleep(0.5)
            return batch_idx, parsed
        except Exception as e:
            # 최종 실패 (라운드 재시도까지 다 실패) → 사유 미상 fallback
            logger.error(f"  [Gemini] batch {batch_idx} 최종 실패: {e}")
            return batch_idx, [{"reason": "사유 미상"}] * len(batch_inputs)

    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = [ex.submit(_call, i) for i in range(len(batches))]
        for fut in as_completed(futures):
            bi, parsed = fut.result()
            start = bi * batch_size
            for j, item in enumerate(parsed):
                if start + j < len(inputs):
                    results[start + j] = item

    movers = movers.copy()
    # 방향 검증: 모델 direction이 change_pct 부호와 모순이면 사유 미상으로 강등 (오방향 서사 차단)
    reasons = []
    for r, chg in zip(results, movers['change'].tolist()):
        item = r or {}
        raw_reason = item.get('reason') or "사유 미상"
        model_dir = item.get('direction') or ""
        reason = _resolve_reason(raw_reason, model_dir, round(float(chg), 2))
        if reason != raw_reason:
            logger.info(f"  [방향검증] change_pct 부호와 모델 방향('{model_dir}') 불일치 → 사유 미상 강등 (원문: {raw_reason})")
        reasons.append(reason)
    movers['reason'] = reasons
    # WICS는 dict 룩업으로 매핑
    movers['wics_sectors'] = [
        map_us_to_wics(s, i)
        for s, i in zip(movers.get('sector', [''] * len(movers)),
                         movers.get('industry', [''] * len(movers)))
    ]
    return movers


# =============================================================================
# 6) WICS별 dict로 산출 (멀티 매핑 → 같은 mover가 여러 섹터에 들어감)
# =============================================================================

def aggregate_movers_by_wics(movers: pd.DataFrame) -> dict[str, list[dict]]:
    """
    {wics_sector: [mover_dict, ...]} 형태로 산출.
    한 mover가 여러 wics_sectors에 매핑되어 있으면 양쪽에 모두 포함.
    """
    out: dict[str, list[dict]] = {}
    for _, mv in movers.iterrows():
        sectors = mv.get('wics_sectors') or []
        if not sectors:
            continue
        entry = {
            "ticker":      mv['Symbol'],
            "exchange":    mv.get('거래소', ''),
            "name":        mv['name'],
            "name_ko":     mv.get('한글종목명', ''),
            "price":       round(float(mv['price']), 2) if pd.notna(mv.get('price')) else None,
            "market_cap":  float(mv['market_cap']) if pd.notna(mv.get('market_cap')) else None,
            "currency":    "USD",
            "change_pct":  round(float(mv['change']), 2),
            "is_52w_high": bool(mv.get('is_52w_high', False)),
            "is_52w_low":  bool(mv.get('is_52w_low', False)),
            "reason":      mv.get('reason') or "사유 미상",
            "description": str(mv.get('기업개요')) if pd.notna(mv.get('기업개요')) else "",
            "fwd_pe":      round(float(mv['fwd_pe']), 1) if pd.notna(mv.get('fwd_pe')) else None,
            "fwd_pb":      round(float(mv['fwd_pb']), 2) if pd.notna(mv.get('fwd_pb')) else None,
            "news_urls":   mv.get('_news_urls') or [],
            "earnings":    mv.get('_earnings') or [],
        }
        for sec in sectors:
            out.setdefault(sec, []).append(entry)

    # 섹터별로 |change_pct| 큰 순 정렬 + 최대 5개로 cap
    MAX_PER_SECTOR = 5
    for sec in out:
        out[sec].sort(key=lambda x: abs(x['change_pct']), reverse=True)
        out[sec] = out[sec][:MAX_PER_SECTOR]
    return out


# =============================================================================
# 7) End-to-end runner (ipynb 셀에서 호출)
# =============================================================================

def build_us_movers_dict(last_trading_day: str,
                          coverage_sectors: list[str],
                          gemini_client, GEMINI_MODELS: list[str], types,
                          header_deepsearch: str, ds_auth,
                          description_db: pd.DataFrame | None = None) -> dict[str, list[dict]]:
    """
    US movers 처리 end-to-end.
    description_db: ['Symbol', 'description_translate'] 컬럼 갖는 DataFrame (선택)
    Returns: {wics_3rd: [movers...]} — sectors/{slug}.json에 그대로 사용 가능
    """
    # 1) 로드
    raw = load_us_movers_xlsx(last_trading_day)
    if raw is None:
        return {}

    # 2) 필터
    movers = filter_us_movers(raw)
    if movers.empty:
        return {}


    # 3) DeepSearch로 전체 종목 뉴스 fetch
    exchange_map = dict(zip(movers['Symbol'], movers['거래소']))
    matched = fetch_movers_news_deepsearch(
        movers['Symbol'].tolist(), last_trading_day, exchange_map,
        max_per_stock=10,
        header_deepsearch=header_deepsearch, ds_auth=ds_auth,
    )
    logger.info(f"[US movers] DeepSearch 뉴스: {sum(1 for v in matched.values() if v)}/{len(movers)} 종목 매칭")

    # 3.5) yfinance 데이터 (종목당 Ticker 1회: fwd P/E·P/B + earnings + news)
    logger.info("[US movers] yfinance 데이터(PE/PB·earnings·news) 수집 중...")
    name_map = dict(zip(movers['Symbol'], movers['name']))
    yf_data = fetch_yf_data_for_tickers(
        movers['Symbol'].tolist(), name_map=name_map,
        max_workers=4, max_rows=5, max_news=10,
    )

    # 3.6) DeepSearch + yfinance 뉴스 병합 (제목·URL 중복 제거 → 최신순 → 종목당 10건)
    #      병합 결과가 Gemini 사유 입력과 관련뉴스 링크 양쪽에 쓰임
    for t in movers['Symbol']:
        matched[t] = _merge_news(matched.get(t), yf_data.get(t, {}).get('news'), max_per_stock=10)
    logger.info(f"[US movers] 뉴스 병합(DeepSearch+yfinance): {sum(1 for v in matched.values() if v)}/{len(movers)} 종목")

    # 5) Gemini 분석 (사유 + WICS 멀티)
    movers = analyze_movers_with_gemini(
        movers, matched, coverage_sectors,
        gemini_client, GEMINI_MODELS, types,
        batch_size=10, max_news_per_stock=5,
    )

    # 5.5) description 머지 (있을 때만)
    if description_db is not None and not description_db.empty:
        movers = movers.merge(
            description_db[['Symbol', 'description_translate']],
            on='Symbol', how='left',
        )
        movers.rename(columns={'description_translate': '기업개요'}, inplace=True)
    else:
        movers['기업개요'] = None

    # 5.6) 뉴스 URL 상위 5건 중 URL 있는 것만 (변동 원인 근거)

    def _extract_news_urls(ticker):
        items = matched.get(ticker) or []
        urls = []
        seen_titles = set()
        for n in items[:5]:
            title = (n.get("title") or "").strip()
            if title in seen_titles:
                continue
            seen_titles.add(title)
            url = (n.get("content_url") or n.get("url") or
                n.get("link") or n.get("source_url") or "")
            if url and url.strip() and url.startswith("http"):
                # 섹터 핵심뉴스 후보 풀에서도 쓰이도록 summary·published_at 동봉.
                # (serialize 등 기존 소비처는 title/url만 읽으므로 추가 키는 무해)
                urls.append({
                    "title":        title,
                    "url":          url.strip(),
                    "summary":      str(n.get("summary", "") or "")[:1000],
                    "published_at": str(n.get("published_at", "") or ""),
                })
            if len(urls) >= 2:
                break
        return urls
    movers['_news_urls'] = movers['Symbol'].apply(_extract_news_urls)

    # 5.7) 어닝 + fwd P/E, P/B — 위 3.5 yfinance 수집분 재사용 (Ticker 추가 호출 없음)
    movers['_earnings'] = movers['Symbol'].apply(lambda t: yf_data.get(t, {}).get('earnings', []))
    movers['fwd_pe']    = movers['Symbol'].apply(lambda t: yf_data.get(t, {}).get('fwd_pe'))
    movers['fwd_pb']    = movers['Symbol'].apply(lambda t: yf_data.get(t, {}).get('fwd_pb'))
    n_pe = int(movers['fwd_pe'].notna().sum())
    n_pb = int(movers['fwd_pb'].notna().sum())
    logger.info(f"[US PE/PB] {n_pe} PE, {n_pb} PB (yfinance 재사용)")

    # 6) WICS별 dict
    result = aggregate_movers_by_wics(movers)
    logger.info(f"[US movers] 최종: {sum(len(v) for v in result.values())}개 매핑 "
          f"({len(result)}개 섹터에 분산)")
    return result


# =============================================================================
# 8) US 어닝콜 처리 (SECTOR_INDUSTRY_TO_WICS 매핑 재활용)
# =============================================================================

def _calc_eps_surprise(eps_actual, eps_expected) -> float | None:
    """
    EPS surprise = (actual - expected) / |expected| * 100
    - expected가 0이거나 NaN이면 None
    - actual이 NaN이면 None
    """
    try:
        if eps_actual is None or eps_expected is None:
            return None
        if pd.isna(eps_actual) or pd.isna(eps_expected):
            return None
        a = float(eps_actual)
        e = float(eps_expected)
        if e == 0:
            return None
        return round((a - e) / abs(e) * 100, 2)
    except (ValueError, TypeError):
        return None


def _safe_float(v):
    """NaN/None/빈문자열 → None, 나머지는 float."""
    try:
        if v is None:
            return None
        if isinstance(v, str) and not v.strip():
            return None
        f = float(v)
        if pd.isna(f):
            return None
        return f
    except (ValueError, TypeError):
        return None


def build_us_earnings_dict(earnings_df: pd.DataFrame) -> dict[str, list[dict]]:
    """
    DeepSearch US 어닝콜 CSV → {wics_3rd: [earning_entry, ...]} dict.

    earnings_df 칼럼 (CSV 그대로):
        title, title_ko, description, description_ko, exchange, company_ticker,
        sector, industry, isin, transcript_date, period,
        revenue_actual, revenue_expected, revenue_change_rate,
        eps_actual, eps_expected, eps_change_rate,
        source, summary

    매핑: us_movers.SECTOR_INDUSTRY_TO_WICS 재활용
    한 어닝콜이 여러 WICS에 매핑되면 양쪽 모두에 entry 추가.

    Returns:
        {wics_3rd: [
            {ticker, title_ko, exchange, transcript_date, period,
             revenue_actual, revenue_expected,
             eps_actual, eps_expected, eps_surprise_pct,
             source, summary},
            ...
        ]}
        매핑 안 되는 industry는 스킵.
    """
    if earnings_df is None or earnings_df.empty:
        return {}

    out: dict[str, list[dict]] = {}
    unmapped = []

    for _, r in earnings_df.iterrows():
        sector   = str(r.get('sector', '') or '').strip()
        industry = str(r.get('industry', '') or '').strip()
        ticker   = str(r.get('company_ticker', '') or '').strip()
        if not ticker:
            continue

        # 사용자 확인: industry 데이터가 이미 lowercase-hyphen으로 정리되어 있음 → 그대로 룩업
        wics_sectors = map_us_to_wics(sector, industry)
        if not wics_sectors:
            unmapped.append((ticker, sector, industry))
            continue

        eps_actual   = _safe_float(r.get('eps_actual'))
        eps_expected = _safe_float(r.get('eps_expected'))
        rev_actual   = _safe_float(r.get('revenue_actual'))
        rev_expected = _safe_float(r.get('revenue_expected'))

        eps_surprise = _calc_eps_surprise(eps_actual, eps_expected)

        # transcript_date: '2026-05-26T00:00:00Z' → '2026-05-26'
        td_raw = str(r.get('transcript_date', '') or '').strip()
        td_clean = td_raw[:10] if td_raw else ''

        entry = {
            "ticker":           ticker,
            "title_ko":         str(r.get('title_ko', '') or '').strip(),
            "exchange":         str(r.get('exchange', '') or '').strip(),
            "transcript_date":  td_clean,
            "period":           str(r.get('period', '') or '').strip(),
            "revenue_actual":   rev_actual,
            "revenue_expected": rev_expected,
            "eps_actual":       eps_actual,
            "eps_expected":     eps_expected,
            "eps_surprise_pct": eps_surprise,
            "source":           str(r.get('source', '') or '').strip(),
            "summary":          str(r.get('summary', '') or '').strip(),
        }

        for sec in wics_sectors:
            out.setdefault(sec, []).append(entry)

    # 섹터별로 transcript_date 최신순 정렬
    for sec in out:
        out[sec].sort(key=lambda x: x.get('transcript_date') or '', reverse=True)

    if unmapped:
        # 매핑 실패 케이스 — 디버깅 용도로 상위 5개만 출력
        logger.warning(f"[US earnings] 매핑 실패 {len(unmapped)}건 (예: {unmapped[:5]})")
    logger.info(f"[US earnings] 최종: {sum(len(v) for v in out.values())}개 매핑 "
          f"({len(out)}개 섹터에 분산)")
    return out
