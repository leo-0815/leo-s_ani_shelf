"""Small read-only official grading probes selected from the local public catalog."""
import argparse
import json
import re
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
vendor=Path(__file__).resolve().parents[1]/".vendor"
if vendor.exists():
    sys.path.insert(0,str(vendor))
from app.db import transaction
from app.sources.common import fetch_html, parse_page, polite_pause

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--source", choices=("chingwin","spp","tongli","kadokawa","tohan"), required=True)
    parser.add_argument("--limit",type=int,default=2)
    parser.add_argument("--key",default="")
    args=parser.parse_args()
    with transaction() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT b.title,b.source_key,b.source_url,b.isbn FROM books b JOIN publishers p ON p.id=b.publisher_id WHERE p.code=%s AND b.media_type IN ('novel','manga') AND (%s='' OR b.source_key=%s) ORDER BY RAND() LIMIT %s",(args.source,args.key,args.key,min(max(args.limit,1),3)))
            rows=cursor.fetchall()
    for row in rows:
        result=dict(row)
        try:
            html=fetch_html(row["source_url"],timeout=20,attempts=1)
            text=parse_page(html).flat_text
            result["labels"]=[text[max(0,m.start()-40):m.end()+80] for m in re.finditer("分級|級別|普遍級|限制級|R18",text)][:12]
            result["structured_flags"]=[html[max(0,m.start()-30):m.end()+80] for m in re.finditer("is_adult|isAdult|adult_product|is_restricted",html)][:6]
            from app.sources.product_rating import parse_rating
            result["parsed"]=parse_rating(args.source,row["source_url"],html,row["source_key"],row["isbn"])
        except Exception as exc:
            result["error"]=str(exc)
        print(json.dumps(result,ensure_ascii=False),flush=True)
        polite_pause(1.5)
if __name__=="__main__":
    main()

