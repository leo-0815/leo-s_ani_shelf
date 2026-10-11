"""Local maintenance client. Preview by default; --apply is explicitly required."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def run(client, apply=False):
    totals={"merged":0,"blocked":{},"ambiguous":0}
    for attempt in range(3 if apply else 1):
        after=0; advanced=0
        while True:
            plan=client._request("POST","/api/catalog-sync/reconcile",{"action":"preview","after_id":after})
            totals["ambiguous"]=plan["ambiguous"]
            pairs=plan["pairs"]
            if not apply:
                return {"total_pairs":plan["total_pairs"],"ambiguous":plan["ambiguous"],"examples":pairs[:10]}
            if not pairs: break
            for offset in range(0,len(pairs),10):
                result=client._request("POST","/api/catalog-sync/reconcile",
                                       {"action":"apply","pairs":pairs[offset:offset+10]})
                totals["merged"]+=result["merged"];advanced+=result["merged"]
                for row in result["blocked"]:
                    totals["blocked"][row["old_id"]]=row["reason"]
                print(json.dumps({"merged":totals["merged"],"blocked":len(totals["blocked"])},ensure_ascii=False),flush=True)
            after=pairs[-1]["old_id"]
        if not advanced:break
    return totals


def main():
    from app.config import load_dotenv, ROOT
    load_dotenv(ROOT/".env")
    from app.catalog_sync import CatalogSyncClient
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply",action="store_true")
    args=parser.parse_args()
    print(json.dumps(run(CatalogSyncClient(),args.apply),ensure_ascii=False),flush=True)


if __name__=="__main__":main()
