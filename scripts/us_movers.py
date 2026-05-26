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
            print(f"  [Gemini] 모든 모델 실패 → {wait}초 대기 후 라운드 {round_idx+1}/{total_rounds} 재시도")
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
# 0-extra) 미국 종목 어닝 데이터 (yfinance + curl_cffi)
# =============================================================================

def fetch_earnings_for_tickers(tickers: list[str], max_workers: int = 4, max_rows: int = 5) -> dict[str, list[dict]]:
    """
    yfinance + curl_cffi로 종목별 earnings_dates 최근 max_rows건 수집.
    Returns: {ticker: [{date, eps_est, eps_actual, surprise_pct}, ...]}
    """
    
    from curl_cffi import requests

    session = requests.Session(verify=False)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    out = {}
    lock = threading.Lock()
    def _one(ticker):
        try:
            stk = yf.Ticker(ticker, session=session)
            ed = stk.earnings_dates
            if ed is None or ed.empty:
                return
            rows = []
            for dt, r in ed.head(max_rows).iterrows():
                rows.append({
                    "date":         dt.strftime("%Y-%m-%d") if hasattr(dt, 'strftime') else str(dt)[:10],
                    "eps_est":      round(float(r['EPS Estimate']), 2) if pd.notna(r.get('EPS Estimate')) else None,
                    "eps_actual":   round(float(r['Reported EPS']), 2) if pd.notna(r.get('Reported EPS')) else None,
                    "surprise_pct": round(float(r['Surprise(%)']), 2) if pd.notna(r.get('Surprise(%)')) else None,
                })
            if rows:
                with lock:
                    out[ticker] = rows
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(_one, tickers))
    print(f"[earnings] {len(out)}/{len(tickers)} 종목 어닝 데이터 수집")
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

US_FOLDER = r"\\197.197.26.121\계량분석\송준영\2026\디지털RA\US_데일리_특징_섹터\DATA"

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
        print(f"[US movers] 파일 없음: {folder}")
        return None

    dated = []
    for f in files:
        m = re.search(r'US_stk_top_1000\((\d{4}-\d{2}-\d{2})\)', f)
        if m:
            dated.append((m.group(1), f))

    # 오늘 이하 최신 파일 선택
    candidates = sorted([(d, f) for d, f in dated if d <= today_str], reverse=True)
    if not candidates:
        print(f"[US movers] {today_str} 이하 파일 없음 → 스킵")
        return None

    date_used, fpath = candidates[0]
    df = pd.read_excel(fpath)
    print(f"[US movers] 로드: {os.path.basename(fpath)} ({len(df):,}건) ← 기준일 {date_used}")
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
    print(f"[US movers] 필터링: {len(df):,}건 → {len(movers):,}건 "
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
                    print(f"    [DS] 429 rate limit → {wait}s 대기")
                    time.sleep(wait)
                    continue
                if resp.status_code != 200:
                    print(f"    [DS] HTTP {resp.status_code}: {resp.text[:200]}")
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
                    print(f"    [DS] timeout (page {page})")
                    return all_data
            except Exception as e:
                if attempt < max_retry - 1:
                    time.sleep(2 ** attempt)
                else:
                    print(f"    [DS] error (page {page}): {e}")
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
            print(f"  [DeepSearch] batch 실패: {e}")
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
    ('기술주', 'consumer-electronics'):              ['IT-기술하드웨어와장비-전자제품',
                                                       'IT-기술하드웨어와장비-전기제품'],
    ('기술주', 'electronic-components'):             ['IT-기술하드웨어와장비-전자제품',
                                                       'IT-기술하드웨어와장비-전기장비와부품'],
    ('기술주', 'communication-equipment'):           ['IT-기술하드웨어와장비-통신장비'],
    ('기술주', 'scientific-technical-instruments'):  ['IT-기술하드웨어와장비-전자제품'],
    ('기술주', 'software-application'):              ['IT-소프트웨어와서비스-소프트웨어'],
    ('기술주', 'software-infrastructure'):           ['IT-소프트웨어와서비스-소프트웨어'],
    ('기술주', 'information-technology-services'):   ['IT-소프트웨어와서비스-IT서비스'],
    ('기술주', 'solar'):                             ['IT-반도체와반도체장비-반도체와반도체장비'],
    # electronics-computer-distribution: 유통이라 제외

    # ── 통신 ──
    ('통신', 'internet-content-information'):        ['IT-소프트웨어와서비스-인터넷서비스'],
    ('통신', 'electronic-gaming-multimedia'):        ['경기관련소비재-내구소비재와의류-게임엔터테인먼트'],
    ('통신', 'entertainment'):                        ['경기관련소비재-미디어-방송과엔터테인먼트'],
    ('통신', 'telecom-services'):                    ['커뮤니케이션서비스-전기통신서비스-통신서비스'],
    ('통신', 'advertising-agencies'):                ['경기관련소비재-미디어-광고'],
    ('통신', 'publishing'):                           ['경기관련소비재-미디어-출판'],

    # ── 헬스케어 ──
    ('헬스케어', 'biotechnology'):                    ['건강관리-제약과생물공학-생물공학'],
    ('헬스케어', 'drug-manufacturers-general'):       ['건강관리-제약과생물공학-제약'],
    ('헬스케어', 'drug-manufacturers-specialty-generic'): ['건강관리-제약과생물공학-제약'],
    ('헬스케어', 'medical-devices'):                  ['건강관리-건강관리장비와서비스-건강관리장비와용품'],
    ('헬스케어', 'medical-instruments-supplies'):     ['건강관리-건강관리장비와서비스-건강관리장비와용품'],
    ('헬스케어', 'diagnostics-research'):             ['건강관리-건강관리장비와서비스-건강관리기술'],
    ('헬스케어', 'health-information-services'):      ['건강관리-건강관리장비와서비스-건강관리기술'],
    ('헬스케어', 'healthcare-plans'):                 ['건강관리-건강관리장비와서비스-건강관리서비스'],
    ('헬스케어', 'medical-care-facilities'):          ['건강관리-건강관리장비와서비스-건강관리서비스'],

    # ── 경기소비재 ──
    ('경기소비재', 'auto-manufacturers'):             ['경기관련소비재-자동차와부품-자동차'],
    ('경기소비재', 'auto-parts'):                      ['경기관련소비재-자동차와부품-자동차부품'],
    ('경기소비재', 'apparel-manufacturing'):          ['경기관련소비재-내구소비재와의류-섬유,의류,신발,호화품'],
    ('경기소비재', 'apparel-retail'):                 ['경기관련소비재-소매(유통)-전문소매'],
    ('경기소비재', 'footwear-accessories'):           ['경기관련소비재-내구소비재와의류-섬유,의류,신발,호화품'],
    ('경기소비재', 'luxury-goods'):                   ['경기관련소비재-내구소비재와의류-섬유,의류,신발,호화품'],
    ('경기소비재', 'internet-retail'):                ['경기관련소비재-소매(유통)-인터넷판매'],
    ('경기소비재', 'department-stores'):              ['경기관련소비재-소매(유통)-백화점과일반상점'],
    ('경기소비재', 'home-improvement-retail'):        ['경기관련소비재-소매(유통)-전문소매'],
    ('경기소비재', 'specialty-retail'):               ['경기관련소비재-소매(유통)-전문소매'],
    ('경기소비재', 'restaurants'):                    ['경기관련소비재-호텔,레스토랑,레저-호텔과레저서비스'],
    ('경기소비재', 'lodging'):                         ['경기관련소비재-호텔,레스토랑,레저-호텔과레저서비스'],
    ('경기소비재', 'leisure'):                         ['경기관련소비재-호텔,레스토랑,레저-호텔과레저서비스'],
    ('경기소비재', 'travel-services'):                ['경기관련소비재-호텔,레스토랑,레저-호텔과레저서비스'],
    ('경기소비재', 'resorts-casinos'):                ['경기관련소비재-호텔,레스토랑,레저-호텔과레저서비스'],
    ('경기소비재', 'gambling'):                        ['경기관련소비재-호텔,레스토랑,레저-호텔과레저서비스'],
    ('경기소비재', 'residential-construction'):       ['산업재-자본재-건설'],
    ('경기소비재', 'furnishings-fixtures-appliances'): ['산업재-자본재-가구',
                                                       'IT-기술하드웨어와장비-전기제품'],
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
    ('필수소비재', 'household-personal-products'):    ['필수소비재-가정용품과개인용품-가정용품',
                                                       '필수소비재-가정용품과개인용품-개인용품'],
    # education-training-services: 한국 시장 직접 연결 약함

    # ── 산업재 ──
    ('산업재', 'aerospace-defense'):                  ['산업재-자본재-우주항공과국방'],
    ('산업재', 'airlines'):                            ['산업재-운송-항공사'],
    ('산업재', 'airports-air-services'):              ['산업재-운송-운송인프라'],
    ('산업재', 'integrated-freight-logistics'):       ['산업재-운송-항공화물운송과물류'],
    ('산업재', 'trucking'):                            ['산업재-운송-육상운송'],
    ('산업재', 'railroads'):                           ['산업재-운송-육상운송'],
    ('산업재', 'farm-heavy-construction-machinery'):  ['산업재-자본재-기계'],
    ('산업재', 'specialty-industrial-machinery'):     ['산업재-자본재-기계'],
    ('산업재', 'tools-accessories'):                  ['산업재-자본재-기계'],
    ('산업재', 'metal-fabrication'):                  ['산업재-자본재-기계'],
    ('산업재', 'building-products-equipment'):        ['산업재-자본재-건축자재'],
    ('산업재', 'engineering-construction'):           ['산업재-자본재-건설'],
    ('산업재', 'electrical-equipment-parts'):         ['IT-기술하드웨어와장비-전기장비와부품',
                                                       'IT-기술하드웨어와장비-전기제품'],
    ('산업재', 'industrial-distribution'):            ['산업재-자본재-산업재유통'],
    ('산업재', 'rental-leasing-services'):            ['산업재-상업서비스와공급품-상업서비스와공급품'],
    ('산업재', 'specialty-business-services'):        ['산업재-상업서비스와공급품-상업서비스와공급품'],
    ('산업재', 'security-protection-services'):       ['산업재-상업서비스와공급품-상업서비스와공급품'],
    ('산업재', 'pollution-treatment-controls'):       ['산업재-상업서비스와공급품-상업서비스와공급품'],
    ('산업재', 'waste-management'):                   ['산업재-상업서비스와공급품-상업서비스와공급품'],
    ('산업재', 'consulting-services'):                ['산업재-전문서비스-전문서비스'],
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
    ('금융', 'credit-services'):                       ['금융-다각화된금융-다각화된금융서비스'],
    ('금융', 'mortgage-finance'):                      ['금융-다각화된금융-다각화된금융서비스'],
    ('금융', 'financial-conglomerates'):               ['금융-다각화된금융-다각화된금융서비스'],

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
    ('기초소재', 'steel'):                              ['소재-소재-금속과광업'],
    ('기초소재', 'aluminum'):                           ['소재-소재-금속과광업'],
    ('기초소재', 'copper'):                              ['소재-소재-금속과광업'],
    ('기초소재', 'gold'):                                ['소재-소재-금속과광업'],
    ('기초소재', 'silver'):                              ['소재-소재-금속과광업'],
    ('기초소재', 'other-precious-metals-mining'):       ['소재-소재-금속과광업'],
    ('기초소재', 'other-industrial-metals-mining'):     ['소재-소재-금속과광업'],
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
- 일간 변동률(change_pct)과 52주 최고/최저 발생 여부
- 어제자 관련 뉴스 (최대 5건, 최신순)

변동의 핵심 원인을 한 줄로 요약하라 (15~40자).
- 사족 금지, 당연한 말 금지, 결정적 사유만
- 좋은 예: "1Q26 실적 컨센 상회 + 데이터센터 가이던스 상향"
- 나쁜 예: "주가가 상승했다", "투자자 관심 증가"
- 뉴스가 부실해서 사유 추정 불가하면 "사유 미상"

JSON 배열로 반환. 입력 종목 순서와 동일하게."""


def analyze_movers_with_gemini(movers: pd.DataFrame,
                                news_by_ticker : dict[str, list[dict]],
                                coverage_sectors: list[str],
                                gemini_client, GEMINI_MODELS: list[str], types,
                                batch_size: int = 10,
                                max_news_per_stock: int = 5) -> pd.DataFrame:
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
                "reason": {"type": "STRING"},
            },
            "required": ["reason"]
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
            print(f"  [Gemini] batch {batch_idx} 최종 실패: {e}")
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
    movers['reason'] = [r['reason'] if r else "사유 미상" for r in results]
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
    print(f"[US movers] DeepSearch 뉴스: {sum(1 for v in matched.values() if v)}/{len(movers)} 종목 매칭")


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
                urls.append({"title": title, "url": url.strip()})
            if len(urls) >= 2:
                break
        return urls
    movers['_news_urls'] = movers['Symbol'].apply(_extract_news_urls)

    # 5.7) 어닝 + fwd P/E, P/B 데이터 수집
    print("[US movers] 어닝/PE/PB 데이터 수집 중...")
    earnings_dict = fetch_earnings_for_tickers(movers['Symbol'].tolist(), max_workers=4, max_rows=5)
    movers['_earnings'] = movers['Symbol'].apply(lambda t: earnings_dict.get(t, []))

    # US 종목 fwd PE/PB (yfinance + curl_cffi)
    us_pe = {}
    us_pb = {}
    def _fetch_us_pe(ticker):
        try:
            from curl_cffi import requests
            session = requests.Session(verify=False)
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            })
            stk = yf.Ticker(ticker, session=session)
            info = stk.info
            pe = info.get('forwardPE')
            pb = info.get('priceToBook')
            with lock_pe:
                if pe and pe > 0: us_pe[ticker] = float(pe)
                if pb and pb > 0: us_pb[ticker] = float(pb)
        except Exception:
            pass
    lock_pe = threading.Lock()
    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(_fetch_us_pe, movers['Symbol'].tolist()))
    print(f"[US PE/PB] {len(us_pe)} PE, {len(us_pb)} PB")
    movers['fwd_pe'] = movers['Symbol'].map(us_pe)
    movers['fwd_pb'] = movers['Symbol'].map(us_pb)

    # 6) WICS별 dict
    result = aggregate_movers_by_wics(movers)
    print(f"[US movers] 최종: {sum(len(v) for v in result.values())}개 매핑 "
          f"({len(result)}개 섹터에 분산)")
    return result
