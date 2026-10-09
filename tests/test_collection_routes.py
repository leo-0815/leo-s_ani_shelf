import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse
from app.server import Handler


class CollectionRouteTests(unittest.TestCase):
    def setUp(self):
        from app.abuse import SlidingWindow
        patcher = patch("app.server.LIMITER", SlidingWindow())
        patcher.start()
        self.addCleanup(patcher.stop)

    def handler(self,path):
        h=object.__new__(Handler)
        h.path=path
        h.headers={}
        h._json=MagicMock()
        h._require_user=lambda:{"id":42}
        h._require_csrf=lambda user:True
        return h

    @patch("app.server.save_custom",return_value=8)
    def test_custom_creation_uses_session_user_not_payload(self,save):
        h=self.handler("/api/collection/custom")
        payload={"title":"私有書","user_id":999}
        h._body=lambda:payload
        h.do_POST()
        save.assert_called_once_with(42,payload)

    @patch("app.server.get_custom",return_value={"id":8})
    def test_custom_detail_uses_session_user(self,get):
        h=self.handler("/api/collection/custom/8")
        h._authenticated_get(urlparse(h.path),{"id":42})
        get.assert_called_once_with(42,8)

    @patch("app.server.remove_owned")
    def test_removal_uses_session_user(self,remove):
        h=self.handler("/api/collection/8")
        h.do_DELETE()
        remove.assert_called_once_with(42,8)

    @patch("app.server.save_custom")
    def test_creation_requires_csrf(self,save):
        h=self.handler("/api/collection/custom")
        h._require_csrf=lambda user:False
        h.do_POST()
        save.assert_not_called()
