"""
빌드 스크립트: sectors/_template.html을 읽어서 sectors/{slug}.html을 생성

사용법:
    python build_sector_pages.py

동작:
- data/wics_slug_map.json 을 읽어서 모든 slug 추출
- sectors/_template.html의 __SECTOR_NAME__ placeholder를 각 섹터의 소분류명(wics_3rd)으로
  치환해서 sectors/{slug}.html로 저장 (GA4 page_title이 섹터별로 구분되도록)
- 기존 파일 있으면 덮어쓰기

실행 시점:
- WICS slug 매핑이 변경되거나 _template.html이 변경될 때 실행
- 매일 데이터 업데이트와 무관
"""

import json
import html
import sys
from pathlib import Path

PLACEHOLDER = "__SECTOR_NAME__"


def main(repo_root: str = None):
    if repo_root is None:
        # 스크립트 기준 상위 폴더
        repo_root = str(Path(__file__).resolve().parent.parent)

    repo = Path(repo_root)
    template = repo / "sectors" / "_template.html"
    slug_map_path = repo / "data" / "wics_slug_map.json"

    if not template.exists():
        print(f"❌ 템플릿 없음: {template}")
        sys.exit(1)
    if not slug_map_path.exists():
        print(f"❌ slug map 없음: {slug_map_path}")
        sys.exit(1)

    template_html = template.read_text(encoding="utf-8")
    if PLACEHOLDER not in template_html:
        print(f"❌ 템플릿에 {PLACEHOLDER} placeholder가 없음 — title 치환 불가. _template.html 확인 필요.")
        sys.exit(1)

    with open(slug_map_path, "r", encoding="utf-8") as f:
        slug_map = json.load(f)

    # slug → 소분류명(wics_3rd) 매핑
    # 키 포맷: "대분류-중분류-소분류" → 마지막 토막이 wics_3rd
    slug_to_name = {}
    for full_key, slug in slug_map.items():
        name = full_key.rsplit("-", 1)[-1]
        if slug in slug_to_name and slug_to_name[slug] != name:
            print(f"⚠️  slug 중복: '{slug}' ('{slug_to_name[slug]}' vs '{name}') — 마지막 값 사용")
        slug_to_name[slug] = name

    print(f"slug 개수: {len(slug_to_name)}")

    sectors_dir = repo / "sectors"
    created = 0
    for slug in sorted(slug_to_name):
        name = slug_to_name[slug]
        page_html = template_html.replace(PLACEHOLDER, html.escape(name))
        (sectors_dir / f"{slug}.html").write_text(page_html, encoding="utf-8")
        created += 1

    print(f"✓ 생성 완료: {created}개")
    print(f"  위치: {sectors_dir}")


if __name__ == "__main__":
    main()
