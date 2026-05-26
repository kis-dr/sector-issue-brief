"""
공시/타사리포트/컨센서스 fetch 함수 (7일 누적 + is_new 마킹 버전)

기존 ipynb의 fetch_disclosure_data / fetch_research_data / fetch_consensus_data 를
대체. 외부 의존(header_deepsearch, DS_AUTH, _retry_call, host_url, check_session,
check_params)은 ipynb 환경에서 이미 정의되어 있다고 가정.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pandas as pd
import requests


def _dedup_by_title_publisher(items: list[dict]) -> list[dict]:
    """
    (title, publisher) 키로 중복 제거. 가장 최신 published_at만 남김.
    """
    if not items:
        return items
    sorted_items = sorted(items, key=lambda x: str(x.get("published_at", "")), reverse=True)
    seen = set()
    out = []
    for item in sorted_items:
        title = (item.get("title") or "").strip()
        pub = (item.get("publisher") or "").strip()
        key = (title, pub)
        if key in seen or not title:
            continue
        seen.add(key)
        out.append(item)
    return out


# ============================================================
# 1. DISCLOSURE — 7일치 + is_new 마킹
# ============================================================
def fetch_disclosure_data(all_codes, last_trading_day,
                          header_deepsearch=None,
                          days_back=7, max_workers=4, verbose=True) -> dict:
    BASE_URL = "https://api-v2.deepsearch.com/v1/articles/documents/disclosure"
    date_from = (datetime.strptime(last_trading_day, "%Y-%m-%d")
                 - timedelta(days=days_back)).strftime("%Y-%m-%d")

    disclosure_dict = {}
    lock = threading.Lock()
    completed = [0]
    total = len(all_codes)

    def _fetch_one(code):
        url = (f"{BASE_URL}?symbols=KRX:{code}"
               f"&date_from={date_from}")

        def _call():
            resp = requests.get(url, headers={"Authorization": header_deepsearch},
                                auth=DS_AUTH, verify=False)  # noqa: F821
            resp.raise_for_status()
            return resp.json().get("data", [])

        try:
            data = _retry_call(_call, max_retry=3)  # noqa: F821
            data = _dedup_by_title_publisher(data)
            # is_new 마킹
            for item in data:
                pub_date = str(item.get("published_at", ""))[:10]
                item["is_new"] = (pub_date >= last_trading_day)

            with lock:
                completed[0] += 1
                if data:
                    disclosure_dict[code] = data
                if verbose:
                    new_cnt = sum(1 for d in data if d.get("is_new"))
                    print(f"[DSC {completed[0]}/{total}] {code} | "
                          f"{len(data)}건 (신규 {new_cnt})" if data
                          else f"[DSC {completed[0]}/{total}] {code} | 없음")
        except Exception as e:
            with lock:
                completed[0] += 1
                if verbose:
                    print(f"[DSC {completed[0]}/{total}] ERR {code}: {e}")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        list(executor.map(_fetch_one, all_codes))

    return disclosure_dict


# ============================================================
# 2. RESEARCH — 7일치 + is_new 마킹
# ============================================================
def fetch_research_data(all_codes, last_trading_day,
                        header_deepsearch=None,
                        days_back=7, page_size=100,
                        max_workers=4, verbose=True) -> dict:
    BASE_URL = "https://api-v2.deepsearch.com/v1/articles/documents/research"
    date_from = (datetime.strptime(last_trading_day, "%Y-%m-%d")
                 - timedelta(days=days_back)).strftime("%Y-%m-%d")

    research_dict = {}
    lock = threading.Lock()
    completed = [0]
    total = len(all_codes)

    def _fetch_one(code):
        url = (f"{BASE_URL}?symbols=KRX:{code}"
               f"&date_from={date_from}"
               f"&clustering=true&uniquify=true&research_insight=true"
               f"&page_size={page_size}")

        def _call():
            resp = requests.get(url, headers={"Authorization": header_deepsearch},
                                auth=DS_AUTH, verify=False)  # noqa: F821
            resp.raise_for_status()
            return resp.json().get("data", [])

        try:
            data = _retry_call(_call, max_retry=3)  # noqa: F821
            data = _dedup_by_title_publisher(data)
            # is_new 마킹
            for item in data:
                pub_date = str(item.get("published_at", ""))[:10]
                item["is_new"] = (pub_date >= last_trading_day)

            with lock:
                completed[0] += 1
                if data:
                    research_dict[code] = data
                if verbose:
                    new_cnt = sum(1 for d in data if d.get("is_new"))
                    print(f"[RSH {completed[0]}/{total}] {code} | "
                          f"{len(data)}건 (신규 {new_cnt})" if data
                          else f"[RSH {completed[0]}/{total}] {code} | 없음")
        except Exception as e:
            with lock:
                completed[0] += 1
                if verbose:
                    print(f"[RSH {completed[0]}/{total}] ERR {code}: {e}")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        list(executor.map(_fetch_one, all_codes))

    return research_dict


# ============================================================
# 3. CONSENSUS — 현행 top5 유지 + is_new 마킹 + last_trading_day 기준
# ============================================================
# ⚠️ CNS_DT: VAL 변동 발생일 (정수 형태, e.g. 20260429)
CONSENSUS_DATE_COL = "CNS_DT"


def fetch_consensus_data(all_codes, last_trading_day,
                         max_workers=4, verbose=True) -> dict:
    consensus_dict = {}
    lock = threading.Lock()
    completed = [0]
    total = len(all_codes)

    thread_local = threading.local()

    def get_session():
        if not hasattr(thread_local, "session"):
            thread_local.session = requests.Session()
            thread_local.session.cookies.update(check_session.cookies)  # noqa: F821
            thread_local.session.headers.update(check_session.headers)  # noqa: F821
        return thread_local.session

    # ▼ last_trading_day 기준으로 분기/연도 산정
    base_date = datetime.strptime(last_trading_day, "%Y-%m-%d")
    q_end = ((base_date.month - 1) // 3 + 1) * 3
    current_q = f"{base_date.year}{q_end:02d}"
    current_y = f"{base_date.year}12"

    def get_cons_df(jcode, term='Y', max_retry=3):
        term_map = {'Q': 32, 'Y': 31}
        yyyymm = current_q if term == 'Q' else current_y
        payload = {**check_params,                          # noqa: F821
                   'jcode': jcode, 'icode': '121500', 'yyyymm': yyyymm}

        # 최근 1개월 cutoff
        cutoff_int = int((base_date - timedelta(days=30)).strftime('%Y%m%d'))
        ltd_int = int(last_trading_day.replace("-", ""))

        session = get_session()
        for attempt in range(max_retry):
            try:
                res = session.post(f"{host_url}/etc/cons/comp_cons_item",  # noqa: F821
                                   data=payload)
                df = (pd.DataFrame(res.json()['results'])
                      .query(f"TERM_TYP == {term_map[term]}")
                      .assign(TERM_TYP=term))
                if df.empty:
                    return df.assign(is_new=False)

                # 최근 1개월 데이터만
                df['CNS_DT'] = df['CNS_DT'].astype(int)
                df = df[df['CNS_DT'] >= cutoff_int]
                if df.empty:
                    return df.assign(is_new=False)

                # 값 변동 시점만 (값 같으면 스킵)
                df = df.sort_values('CNS_DT', ascending=True).reset_index(drop=True)
                df = df[df['VAL'] != df['VAL'].shift(1)]

                # is_new 마킹
                df['is_new'] = df['CNS_DT'] >= ltd_int
                return df
            except Exception:
                if attempt < max_retry - 1:
                    time.sleep(2 ** attempt)
                else:
                    return None

    def _fetch_one(code):
        cons_q = get_cons_df(code, term='Q')
        cons_y = get_cons_df(code, term='Y')
        with lock:
            completed[0] += 1
            if cons_q is not None or cons_y is not None:
                consensus_dict[code] = {"Q": cons_q, "Y": cons_y}
                if verbose:
                    new_q = int(cons_q['is_new'].sum()) if cons_q is not None and not cons_q.empty else 0
                    new_y = int(cons_y['is_new'].sum()) if cons_y is not None and not cons_y.empty else 0
                    print(f"[CNS {completed[0]}/{total}] {code} | Q:{new_q}new Y:{new_y}new")
            else:
                if verbose:
                    print(f"[CNS {completed[0]}/{total}] {code} DATA IMPORT 오류")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        list(executor.map(_fetch_one, all_codes))

    return consensus_dict
