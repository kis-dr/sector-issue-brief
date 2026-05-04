"""
종목별 뉴스 수집 모듈
- fetch_comment_data 옛 코드를 단순화 (텔레그램 제거)
- 어제자 1일치, 종목당 최대 3건 핵심 뉴스 선정
- 중복 제거 (TF-IDF 코사인 유사도 0.2)
- Gemini로 핵심 뉴스 선정 + sentiment 태깅 (response_schema 사용)
"""

import json, time, random
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


# ============================================================
# 1. 중복 제거 (옛 remove_similar_articles 그대로)
# ============================================================
def remove_similar_articles(df: pd.DataFrame,
                             title_threshold: float = 0.2,
                             summary_threshold: float = 0.2) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy().reset_index(drop=True)

    def get_sim_matrix(texts):
        vec = TfidfVectorizer(analyzer='char_wb', ngram_range=(2, 4), min_df=1)
        return cosine_similarity(vec.fit_transform(texts))

    title_sim   = get_sim_matrix(df["title"].fillna("").tolist())
    summary_sim = get_sim_matrix(df["summary"].fillna("").tolist())
    sim_matrix = (title_sim >= title_threshold) | (summary_sim >= summary_threshold)
    sorted_idx = df["published_at"].argsort()[::-1].tolist()
    to_drop = set()
    for i in sorted_idx:
        if i in to_drop:
            continue
        for j in sorted_idx:
            if i == j or j in to_drop:
                continue
            if sim_matrix[i, j]:
                to_drop.add(j)
    return df.drop(index=list(to_drop)).reset_index(drop=True)


# ============================================================
# 2. Gemini 선정 + sentiment 태깅 (response_schema 사용)
# ============================================================

NEWS_SELECTION_PROMPT = """너는 한국 증권사 시니어 에쿼티 애널리스트다.
주어진 종목의 뉴스 목록 중 주가에 가장 큰 영향을 미칠 핵심 뉴스 **최대 3개**를 선정하라.

선정 기준 (하나 이상 해당):
1. 실적: 매출/영업이익/순이익 서프라이즈/쇼크, 가이던스 변경
2. 수급: 대규모 지분 변동, 자사주 매입/소각, 블록딜, 공매도
3. 기업 이벤트: M&A, 분할, 유상증자, CB/BW 발행, 상장/상폐
4. 규제/정책: 업종에 직접 영향 주는 법안/규제/관세/보조금
5. 산업 수급: 핵심 원자재 가격, 공급망, 수주/계약
6. 매크로: 금리, 환율, 신용등급
7. 경영 리스크: CEO 교체, 횡령/분식, 소송/과징금
8. 섹터 밸류에이션 리레이팅

제외 기준: 스포츠/연예/사회/날씨 등 펀더멘털 무관 뉴스

중복 제거: 같은 사건 다루는 뉴스는 가장 정보량 많은 것 1개만.
각 뉴스마다 sentiment를 "positive"/"negative"/"neutral" 중 하나로 분류.

빈약하면 빈 배열도 OK."""


def select_news_with_sentiment(df: pd.DataFrame, stock_code: str,
                                gemini_client, MODEL_ID: str, types,
                                max_input: int = 15, max_retry: int = 3) -> dict:
    """
    Returns: {
        "selected_ids": [...],
        "comments": {id: {"title", "summary", "url", "sentiment"}}
    }
    """
    if df.empty:
        return {"selected_ids": [], "comments": {}}

    df = df.head(max_input).copy()

    SCHEMA = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "id":        {"type": "STRING"},
                "sentiment": {"type": "STRING", "enum": ["positive", "negative", "neutral"]},
            },
            "required": ["id", "sentiment"],
        },
        "maxItems": 3,
    }

    inputs = [
        {"id": str(r["id"]),
         "t":  str(r["title"]),
         "s":  str(r.get("summary", ""))[:200]}
        for _, r in df.iterrows()
    ]

    user_content = (f"종목: {stock_code}\n오늘: {datetime.now().strftime('%Y-%m-%d')}\n\n"
                    + json.dumps(inputs, ensure_ascii=False))

    for attempt in range(max_retry):
        try:
            resp = gemini_client.models.generate_content(
                model=MODEL_ID,
                contents=user_content,
                config=types.GenerateContentConfig(
                    system_instruction=NEWS_SELECTION_PROMPT,
                    response_mime_type="application/json",
                    response_schema=SCHEMA,
                    temperature=0.1,
                ),
            )
            parsed = json.loads(resp.text)[:3]   # 안전장치
            break
        except Exception as e:
            err = str(e)
            if "429" in err and attempt < max_retry - 1:
                wait = 2 ** (attempt + 1) + random.uniform(0, 2)
                time.sleep(wait)
                continue
            if attempt < max_retry - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            return {"selected_ids": [], "comments": {}}

    valid_ids  = set(df["id"].astype(str))
    id_to_meta = df.set_index(df["id"].astype(str))[
        ["title", "summary", "content_url", "published_at"]
    ].to_dict("index")

    selected_ids = []
    comments = {}
    for item in parsed:
        nid = str(item.get("id", ""))
        if nid in valid_ids and nid not in selected_ids:
            selected_ids.append(nid)
            meta = id_to_meta.get(nid, {})
            comments[nid] = {
                "title":        meta.get("title", ""),
                "summary":      str(meta.get("summary", ""))[:300],
                "url":          meta.get("content_url", ""),
                "published_at": str(meta.get("published_at", "")),
                "sentiment":    item.get("sentiment", "neutral"),
            }
    return {"selected_ids": selected_ids, "comments": comments}


# ============================================================
# 3. End-to-end: 종목별 뉴스 dict 빌드
# ============================================================

def fetch_stock_comments(all_codes: list[str], last_trading_day: str,
                         header_deepsearch: str, ds_auth,
                         gemini_client, MODEL_ID: str, types,
                         page_size: int = 100, max_workers: int = 4,
                         filter_empty: bool = True, verbose: bool = True) -> dict:
    """
    Returns: {code: {selected_ids: [...], comments: {id: {...}}}}
    """
    BASE_URL = ("https://api-v2.deepsearch.com/v1/articles/"
                "politics,economy,society,culture,world,tech,entertainment,opinion")
    COLUMNS = ["id", "title", "publisher", "summary", "published_at", "content_url"]

    comment_dict = {}
    import threading
    lock = threading.Lock()
    completed = [0]
    total = len(all_codes)

    def _work(code):
        try:
            url = (f"{BASE_URL}?symbols=KRX:{code}"
                   f"&date_from={last_trading_day}&date_to={last_trading_day}"
                   f"&clustering=false&uniquify=true&research_insight=true"
                   f"&page_size={page_size}")
            resp = requests.get(url, headers={"Authorization": header_deepsearch},
                                auth=ds_auth, verify=False)
            resp.raise_for_status()
            data = resp.json().get("data", [])

            with lock:
                completed[0] += 1
                progress = f"[CMT {completed[0]}/{total}] {code}"

            if not data:
                if verbose: print(f"{progress} | 0건")
                return

            news_df = (pd.DataFrame(data)[COLUMNS]
                       .sort_values("published_at", ascending=False)
                       .reset_index(drop=True))

            news_df_unique = remove_similar_articles(news_df)
            result = select_news_with_sentiment(
                news_df_unique, code, gemini_client, MODEL_ID, types
            )

            with lock:
                if result.get("selected_ids"):
                    comment_dict[code] = result
                if verbose:
                    print(f"{progress} | "
                          f"raw {len(news_df)} → dedup {len(news_df_unique)} → "
                          f"selected {len(result.get('selected_ids', []))}")
        except Exception as e:
            with lock:
                completed[0] += 1
                if verbose:
                    print(f"[CMT {completed[0]}/{total}] {code} ERR: {e}")

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(_work, all_codes))

    if filter_empty:
        comment_dict = {k: v for k, v in comment_dict.items() if v.get("selected_ids")}
    return comment_dict
