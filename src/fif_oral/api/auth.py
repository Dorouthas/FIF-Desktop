import base64
import json
import re
import time
from http.cookiejar import Cookie, CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

from fif_oral.api.client import ApiError, FiFClient


CAS = "https://cycore.fifedu.com/cas-server/login"
SSO = "https://www.fifedu.com/iplat/ssoservice"
STUDENT = "https://oral.fifedu.com/kyxl-web/student/index.do"
CAPTCHA = "https://cycore.fifedu.com/fif_captcha/"
STUDENT_HOSTS = {"oral.fifedu.com", "moral.fifedu.com", "cycore.fifedu.com", "www.fifedu.com", "static.fifedu.com"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class CaptchaRequired(ApiError):
    def __init__(self, image: bytes, message: str):
        super().__init__(message)
        self.image = image


class LoginFlow:
    def __init__(self):
        self.cookies = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies), NoRedirect())
        self.captcha_id = ""

    def _open(self, url, fields=None, *, post=False):
        data = urlencode(fields).encode() if fields is not None else None
        if data is not None and not post:
            url += "?" + data.decode("ascii")
        request = Request(url, data=data if post else None, headers={"Accept": "application/json, */*"})
        try:
            return self.opener.open(request, timeout=12)
        except HTTPError as exc:
            return exc
        except (URLError, TimeoutError) as exc:
            raise ApiError("无法连接到 FIF 口语，请检查网络后重试。") from exc

    def _data(self, url, fields, callback="") -> dict:
        with self._open(url, fields) as response:
            if response.code != 200:
                raise ApiError(f"登录服务返回 HTTP {response.code}。")
            body = response.read(2_000_000).decode("utf-8-sig", errors="replace").strip()
        if callback and not body.startswith("{"):
            wrapped = re.fullmatch(r"(?:/\*\*/\s*)?" + re.escape(callback) + r"\s*\((.*)\)\s*;?", body, re.S)
            body = wrapped.group(1) if wrapped else ""
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
        raise ApiError("登录服务未返回可识别的数据。")

    def _challenge(self, message):
        self.captcha_id = ""
        result = self._data(CAPTCHA + "getCaptcha", {"t_": int(time.time() * 1000)})
        data = result.get("data") or {}
        captcha_id = str(data.get("uuid") or "")
        image = str(data.get("image") or "")
        if not captcha_id or not image:
            raise ApiError("平台要求验证，当前没有返回可显示的图片验证码。")
        try:
            payload = image.split(",", 1)[-1] if image.startswith("data:image/") else image
            raw = base64.b64decode(payload, validate=True)
        except ValueError as exc:
            raise ApiError("平台返回了无法显示的验证码。") from exc
        self.captcha_id = captcha_id
        raise CaptchaRequired(raw, message)

    def _verify_captcha(self, answer):
        if self.captcha_id:
            result = self._data(CAPTCHA + "checkCaptcha", {"uuid": self.captcha_id, "captcha": answer})
            if (result.get("data") or {}).get("check") != "successed":
                self._challenge("验证码不匹配，请输入新的验证码。")

    def _portal_token(self, username, password, answer):
        self._verify_captcha(answer)
        ticket = self._data(CAS, {"service": SSO, "get-lt": "true", "n": int(time.time() * 1000),
                                  "callback": "jsonpcallback"}, "jsonpcallback")
        if not ticket.get("lt") or not ticket.get("execution"):
            raise ApiError("平台没有签发登录票据。")
        credentials = {
            "service": SSO, "lt": ticket["lt"], "execution": ticket["execution"],
            "username": username, "password": base64.b64encode(password.encode()).decode("ascii"),
            "isajax": "true", "isframe": "true", "_eventId": "submit", "serviceURL": "", "type": "pwd",
            "x_code": self.captcha_id, "callback": "logincallback",
        }
        result = self._data(CAS, credentials, "logincallback")
        self.captcha_id = ""
        message = str(result.get("msgCode") or result.get("msg") or "")
        if "验证码" in message:
            self._challenge("请输入 FIF 口语下发的图片验证码。")
        if result.get("status") in (-1, "-1") or "滑块" in message or "安全验证" in message:
            raise ApiError("平台要求更新密码或完成额外身份验证，请联系学校管理员。")
        token = result.get("token")
        if result.get("login") == "fails" or not isinstance(token, str) or not token.strip():
            raise ApiError("平台拒绝登录，请核对账号和密码。")
        with self._open(SSO, {"action": "login"}, post=True) as response:
            if response.code >= 400:
                raise ApiError(f"平台未建立门户会话（HTTP {response.code}）。")
        return token

    def _student_token(self, portal_token):
        self.cookies.set_cookie(Cookie(
            version=0, name="prod-token", value=portal_token, port=None, port_specified=False,
            domain=".fifedu.com", domain_specified=True, domain_initial_dot=True, path="/",
            path_specified=True, secure=True, expires=None, discard=True, comment=None,
            comment_url=None, rest={},
        ))
        for url in (SSO + "?" + urlencode({"nextPage": STUDENT}),
                    STUDENT + "?" + urlencode({"token": portal_token})):
            result = self._student_redirect(url)
            if result:
                return result
        raise ApiError("门户已认证，但平台未通过 HTTP 跳转签发口语会话。")

    def _student_redirect(self, url):
        for _ in range(12):
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or parsed.hostname not in STUDENT_HOSTS
                    or parsed.username or parsed.password or parsed.port not in (None, 443)):
                raise ApiError("平台登录跳转到了未识别的服务。")
            if parsed.hostname == "static.fifedu.com" and "/student-h5/" in parsed.path:
                params = parse_qs(parsed.query)
                params.update(parse_qs(parsed.fragment.partition("?")[2]))
                token = params.get("token", [""])[0]
                if token:
                    return token, params.get("source", ["10003"])[0]
                break
            with self._open(url) as response:
                if response.code not in {301, 302, 303, 307, 308}:
                    break
                target = response.headers.get("Location")
                if not target:
                    break
                url = urljoin(url, target)
        return None

    def sign_in(self, username, password, expected_id, answer=""):
        portal_token = self._portal_token(username, password, answer)
        token, source = self._student_token(portal_token)
        return FiFClient.connect(token, source, expected_id, username)
