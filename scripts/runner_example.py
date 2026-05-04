# =============================================================================
# Step 4 통합 셀 — ipynb에 추가
# 위치: 기존 ipynb의 마지막 (consensus_dict 셀 다음)
# =============================================================================

# 0) 모듈 import (scripts 폴더가 있다고 가정)
import sys
sys.path.insert(0, './scripts')   # 또는 절대경로
from us_movers import build_us_movers_dict
from serialize import (
    load_wics_slug_map,
    run_serialization,
)

# 1) WICS slug 매핑 로드 (Step 3-C 산출물)
wics_slug_map = load_wics_slug_map('./data/wics_slug_map.json')

# 2) US movers 처리 (Step 3-A)
us_movers_dict = build_us_movers_dict(
    last_trading_day=last_trading_day,
    kr_df=kr_df,
    us_df=us_df,
    kis_gr_df=kis_gr_df,
    coverage_sectors=coverage_sector_list,
    gemini_client=gemini_client,
    MODEL_ID=MODEL_ID,
    types=types,
    header_deepsearch=header_deepsearch,
    ds_auth=DS_AUTH,
)

# 3) previous_trading_date 산정
#    (이미 ipynb에서 past_date 변수 있으면 그걸 쓰거나, 직접 계산)
from datetime import datetime, timedelta
previous_trading_date = (
    datetime.strptime(last_trading_day, "%Y-%m-%d") - timedelta(days=1)
).strftime("%Y-%m-%d")
# ⚠️ 실제 영업일 캘린더 기반 산정이 더 정확함 (송이가 갖고 있는 stock_check 파일 등 활용)

# 4) 직렬화 실행
REPO_ROOT = r"C:\path\to\sector-issue-brief"   # ← 송이의 로컬 git clone 경로

run_serialization(
    repo_root=REPO_ROOT,
    trading_date=last_trading_day,
    previous_trading_date=previous_trading_date,
    coverage_sector_list=coverage_sector_list,
    wics_stock_df=wics_stock_df,
    main_combined_stock=main_combined_stock,
    top_news=top_news,
    us_movers_dict=us_movers_dict,
    disclosure_dict=disclosure_dict,
    research_dict=research_dict,
    consensus_dict=consensus_dict,
    wics_slug_map=wics_slug_map,
    gemini_client=gemini_client,
    MODEL_ID=MODEL_ID,
    types=types,
)

# 5) git push
import subprocess
subprocess.run(['git', '-C', REPO_ROOT, 'add', 'data/'], check=True)
subprocess.run(
    ['git', '-C', REPO_ROOT, 'commit', '-m', f'data: {last_trading_day} update'],
    check=True
)
subprocess.run(['git', '-C', REPO_ROOT, 'push'], check=True)
print(f"✓ {last_trading_day} GitHub push 완료")
