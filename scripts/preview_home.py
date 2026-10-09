"""Local-only visual fixture. No database, credentials or publisher requests."""
from __future__ import annotations
import json
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.server import Handler

USER = {"id":42,"display_name":"AniShelf 測試使用者","email":"demo@example.invalid",
        "is_admin":False,"general_audience":True,"csrf_token":"preview-only"}
BOOKS = [{"id":i,"title":title,"author":"示範作者","publisher_name":"示範出版社",
          "cover_url":"","release_date":f"2030-10-{day}","media_type":"novel",
          "release_precision":"day","release_status":"scheduled","edition_type":"standard"}
         for i,(title,day) in enumerate([("下一段故事 (2)",10),("書架上的冒險 (5) 特裝版",11),
                                         ("在城市邊緣讀一本書 (1)",12),("給明天的你 (3)",14)],1)]


class Preview(Handler):
    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/api/auth/me": self._json({"authenticated":True,"user":USER})
        elif route == "/api/home": self._json({"date":"2030-10-09","counts":{"wishlist":12,"collection":36,"followed_series":8},"items":BOOKS})
        elif route == "/api/publishers": self._json({"items":[]})
        elif route == "/api/stats": self._json(dict.fromkeys(["total","scheduled","available","unknown","scheduled_undated","wishlist","followed_series","purchased","purchased_paper","purchased_digital","purchased_series","purchased_spend"],0))
        elif route == "/api/books": self._json({"items":[],"total":0})
        elif route.startswith("/api/"): self._json({"error":"Preview fixture only"},404)
        else: self._static(route)

    def do_POST(self): self._json({"error":"Read-only preview"},405)
    def do_DELETE(self): self._json({"error":"Read-only preview"},405)


if __name__ == "__main__":
    print("Read-only home preview: http://127.0.0.1:8871", flush=True)
    ThreadingHTTPServer(("127.0.0.1",8871),Preview).serve_forever()
