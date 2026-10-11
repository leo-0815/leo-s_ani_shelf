import unittest
from contextlib import ExitStack
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch
import json
from app.server import Handler
from app.sources.chingwin import ChingWinSource


class ReconciliationHTTPTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack()
        self.stack.enter_context(patch("app.server.get_settings",return_value=SimpleNamespace(
            catalog_sync_configured=True,catalog_sync_token="test-only",public_url="",cloud_mode=False)))
        self.preview=self.stack.enter_context(patch("app.catalog_reconciliation.preview",return_value={"pairs":[],"total_pairs":0,"ambiguous":0}))
        self.apply=self.stack.enter_context(patch("app.catalog_reconciliation.apply_pairs",return_value={"merged":0,"blocked":[]}))
        class QuietHandler(Handler):
            def log_message(self,*args):pass
        self.server=ThreadingHTTPServer(("127.0.0.1",0),QuietHandler)
        self.thread=Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.stack.close()

    def request(self,raw,token=""):
        connection=HTTPConnection("127.0.0.1",self.server.server_port,timeout=3)
        connection.request("POST","/api/catalog-sync/reconcile",body=json.dumps(raw),
                           headers={"Authorization":"Bearer "+token,"Content-Type":"application/json"})
        response=connection.getresponse()
        result=(response.status,json.loads(response.read()))
        connection.close()
        return result

    def test_unauthorized_cannot_preview_or_apply(self):
        for action in ("preview","apply"):
            self.assertEqual(self.request({"action":action})[0],401)
        self.preview.assert_not_called();self.apply.assert_not_called()

    def test_authorized_preview_does_not_apply_and_has_protocol(self):
        status,body=self.request({"action":"preview","after_id":123},"test-only")
        self.assertEqual(status,200);self.assertEqual(body["protocol_version"],1)
        self.preview.assert_called_once_with(after_id=123);self.apply.assert_not_called()

    def test_unsupported_action_rejected(self):
        self.assertEqual(self.request({"action":"delete-all"},"test-only")[0],400)
        self.apply.assert_not_called()


class CoverURLTests(unittest.TestCase):
    def test_jsonld_images_are_urls_not_python_repr_or_unsafe_scheme(self):
        parse=ChingWinSource._cover_url
        page="https://www.ching-win.com.tw/products/books?page=1"
        self.assertEqual(parse(["",{"url":"/covers/1.jpg"}],page),"https://www.ching-win.com.tw/covers/1.jpg")
        self.assertEqual(parse({"contentUrl":"//images.example.test/a.jpg"},page),"https://images.example.test/a.jpg")
        for value in (None,{},"javascript:alert(1)",["data:image/png;base64,a"]):
            self.assertIsNone(parse(value,page))
