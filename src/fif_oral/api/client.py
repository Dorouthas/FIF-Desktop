import base64
import json
import re
import time
from dataclasses import dataclass, field
from html import unescape
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


BASE = "https://moral.fifedu.com"


class ApiError(Exception):
    pass


class SessionExpired(ApiError):
    pass


class SubmissionUncertain(ApiError):
    pass


class SameOriginRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl)[:2] != urlsplit(BASE)[:2]:
            raise SessionExpired("平台要求重新登录。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def jwt_metadata(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return data if isinstance(data, dict) else {}
    except (ValueError, IndexError):
        return {}


@dataclass(frozen=True)
class FiFClient:
    token: str = field(repr=False)
    source: str
    user_id: str = ""
    name: str = ""
    expires_at: float | None = None

    @classmethod
    def connect(cls, token: str, source: str, expected_id: str, expected_login: str):
        token = token.strip().removeprefix("Bearer ")
        claims = jwt_metadata(token)
        expiry = claims.get("exp")
        expires_at = expiry if isinstance(expiry, (int, float)) else None
        client = cls(token, source, expires_at=expires_at)
        profile = client._read("/kyxl-app/account/getUserInfo", {}, method="GET")
        user_id = str(profile.get("userId") or profile.get("user_id") or "")
        login = str(profile.get("loginName") or claims.get("login_name") or "")
        if not user_id:
            raise ApiError("平台没有返回用户编号，无法绑定账号。")
        if (expected_id and expected_id != user_id) or (
            expected_login and login and login.casefold() != expected_login.casefold()
        ):
            raise ApiError("登录的身份与选定账号不一致，请选择正确的账号。")
        name = str(profile.get("realName") or login or expected_login or user_id)
        return cls(token, source, user_id, name, expires_at)

    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= time.time()

    def _request(self, path, fields, *, method="POST", empty_page=False, headers=None, submitting=False):
        if self.expired():
            raise SessionExpired("会话已过期，请重新登录。")
        encoded = urlencode(fields)
        endpoint = BASE + path
        if method == "GET" and encoded:
            endpoint += "?" + encoded
        request = Request(endpoint,
                          data=encoded.encode() if method == "POST" else None, method=method)
        request.add_unredirected_header("Authorization", "Bearer " + self.token)
        request.add_header("source", self.source)
        request.add_header("Accept", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, str(value))
        if method == "POST":
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with build_opener(SameOriginRedirects()).open(request, timeout=15) as response:
                payload = json.load(response)
        except HTTPError as exc:
            if submitting and exc.code >= 500:
                raise SubmissionUncertain("提交响应异常，需回查本次记录。") from exc
            if exc.code == 401:
                raise SessionExpired("平台会话已失效，请重新登录。") from exc
            raise ApiError(f"平台返回 HTTP {exc.code}。") from exc
        except (URLError, OSError, HTTPException) as exc:
            if submitting:
                raise SubmissionUncertain("提交响应未收到，需回查本次记录。") from exc
            raise ApiError("无法连接到 FIF 口语，请检查网络后重试。") from exc
        except ValueError as exc:
            if submitting:
                raise SubmissionUncertain("提交响应无法读取，需回查本次记录。") from exc
            raise ApiError("平台返回了无法读取的响应。") from exc
        if not isinstance(payload, dict):
            error = SubmissionUncertain if submitting else ApiError
            raise error("平台响应格式异常。")
        if payload.get("status") in (1, "1"):
            return payload.get("data")
        message = str(payload.get("msg") or payload.get("message") or "")
        if payload.get("code") in (401, "401") or any(
            word in message.lower() for word in ("未登录", "重新登录", "登录过期", "token")
        ):
            raise SessionExpired("平台会话已失效，请重新登录。")
        if empty_page and payload.get("status") in (-1, "-1") and not payload.get("data"):
            if not message or any(word in message for word in ("无数据", "暂无", "没有数据", "无更多")):
                return {}
        raise ApiError(message[:240] or "平台拒绝了该次查询。")

    def _read(self, path, fields, *, method="POST", empty_page=False) -> dict:
        data = self._request(path, fields, method=method, empty_page=empty_page)
        if data is None and empty_page:
            return {}
        if not isinstance(data, dict):
            raise ApiError("平台响应缺少数据。")
        return data

    def tasks(self, status: int) -> list[dict]:
        tasks = []
        seen = set()
        for page in range(1, 201):
            data = self._read("/kyxl-app/stu/task/teaTaskList",
                              {"userId": self.user_id, "status": status, "page": page},
                              empty_page=True)
            items = []
            for task in records(data.get("ttiList")):
                identity = task.get("taskId") or task.get("id")
                if identity and identity not in seen:
                    items.append(task)
                    seen.add(identity)
            if not items:
                return tasks
            tasks.extend(items)
        raise ApiError("任务列表分页异常，请刷新后重试。")

    def task(self, task_id: str) -> dict:
        return self._read("/kyxl-app/task/stu/teaTaskDetail", {"userId": self.user_id, "id": task_id})

    def unit(self, task_id: str, unit_id: str) -> dict:
        return self._read("/kyxl-app/stu/column/stuUnitInfo",
                          {"unitId": unit_id, "taskId": task_id, "bankType": ""}, method="GET")

    def level(self, level_id: str) -> dict:
        return self._read("/kyxl-app/column/getLevelInfo", {"levelId": level_id})

    def assessment(self, task_id, unit_id, level_id):
        url = self._request("/kyxl-app/h5/getH5Url", {
            "pageType": "DEKARON_MAIN", "taskId": task_id, "unitId": unit_id, "gId": level_id,
        }, method="GET")
        if not isinstance(url, str):
            raise ApiError("平台没有返回评测会话地址。")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "static.fifedu.com":
            raise ApiError("评测会话地址无效。")
        fields = parse_qs(parsed.query)
        fields.update(parse_qs(parsed.fragment.partition("?")[2]))
        token = fields.get("token", [""])[0]
        if not token:
            raise ApiError("评测会话缺少令牌。")
        return FiFClient.connect(token, fields.get("source", [self.source])[0], self.user_id, "")

    def profile(self):
        return self._read("/kyxl-app/account/getUserInfo", {}, method="GET")

    def challenge_config(self, task_id, level_id):
        return self._read("/kyxl-app/task/stu/getStudentTaskChallengeConfig", {
            "taskId": task_id, "levelId": level_id, "studentId": self.user_id,
        })

    def assessment_auth(self, language):
        return self._read("/kyxl-app/common/getAuthUrl", {"language": language})

    def oss_credentials(self):
        return self._read("/kyxl-app/oss/getOss", {})

    def challenge_history(self, task_id, level_id):
        return self._read("/kyxl-app/stu/levelHistory/getChallengeRecList", {
            "userid": self.user_id, "levelId": level_id, "taskId": task_id, "questionType": 0,
        })

    def challenge_record(self, task_id, record_id):
        return self._read("/kyxl-app/stu/levelHistory/getChallengeRec", {
            "challengeRecUid": record_id, "taskId": task_id, "questionType": 0,
        })

    def challenge_report(self, task_id, level_id, report_id):
        return self._read("/kyxl-app/stu/levelHistory/levelReport", {
            "userid": self.user_id, "levelId": level_id, "taskId": task_id,
            "questionType": 0, "bestRecUid": report_id,
        })

    def submit_challenge(self, task_id, level_id, results, profile):
        headers = {key: profile[key] for key in ("schoolId", "userId", "loginName", "userNo", "language")
                   if profile.get(key) is not None}
        name = str(profile.get("realName") or "").encode("utf-16-be")
        headers["realName"] = "".join(f"\\u{int.from_bytes(name[i:i+2], 'big'):x}" for i in range(0, len(name), 2))
        headers["userRole"] = profile.get("role", 0)
        headers["clientType"] = "5" if profile.get("role") == 1 else "6"
        data = self._request("/kyxl-app-challenge/evaluation/submitChallengeResults", {
            "taskId": task_id, "levelId": level_id, "studentId": self.user_id,
            "resultJson": json.dumps(results, ensure_ascii=False, separators=(",", ":")),
        }, headers=headers, submitting=True)
        if not isinstance(data, dict) or not data.get("questionAnswerId"):
            raise SubmissionUncertain("提交响应缺少报告编号，需回查本次记录。")
        return str(data["questionAnswerId"])


def records(value) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def plain(value) -> str:
    if not isinstance(value, str):
        return ""
    value = re.sub(r"<(?:br\s*/?|/p|/div)\s*>", "\n", value, flags=re.I)
    return unescape(re.sub(r"<[^>]+>", "", value)).strip()


def audio_url(prefix: str, path) -> str:
    if not isinstance(path, str) or not path.strip():
        return ""
    try:
        url = urljoin(prefix, path.strip())
        if url.startswith("//"):
            url = "https:" + url
        parts = urlsplit(url)
    except ValueError:
        return ""
    host = parts.hostname or ""
    if parts.scheme == "https" and (host == "fifedu.com" or host.endswith(".fifedu.com")):
        return url if not parts.username and not parts.password else ""
    return ""


@dataclass(frozen=True)
class Prompt:
    text: str
    translation: str
    audio: str


def question_groups(data: dict, *, for_practice=False) -> list[tuple[str, str, list[Prompt]]]:
    groups = []
    prefix = str(data.get("resourcePrefixPath") or "")
    if prefix:
        prefix = prefix.rstrip("/") + "/"
    content = data.get("content") or data.get("levelContent") or {}
    if not isinstance(content, dict):
        return groups
    for mode in records(content.get("moshi")):
        question = mode.get("question") or {}
        qcontent = question.get("qcontent") if isinstance(question, dict) else None
        if not isinstance(qcontent, dict):
            continue
        items = [q for item in records(qcontent.get("item")) for q in records(item.get("questions"))]
        items.extend(records(qcontent.get("contentArray")))
        entries = items if for_practice and items else [qcontent, *items]
        prompts = []
        for entry in entries:
            text = plain(entry.get("text")) or plain(entry.get("title"))
            translation = plain(entry.get("title_cn"))
            audio = audio_url(prefix, entry.get("res_path"))
            prompt = Prompt(text.replace("##", "\n"), translation, audio)
            if (text or translation or audio) and (for_practice or prompt not in prompts):
                prompts.append(prompt)
        if prompts:
            groups.append((str(mode.get("name") or "题目"), str(mode.get("showtype") or ""), prompts))
    return groups
