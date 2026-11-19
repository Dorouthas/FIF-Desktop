import base64
import json
import math
import re
import subprocess
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree

import websocket

from fif_oral.api.client import ApiError, Prompt, audio_url, plain, records


class Cancelled(ApiError):
    pass


class EvaluationInterrupted(ApiError):
    pass


class EvaluationRejected(ApiError):
    pass


class UnsupportedQuestion(ApiError):
    pass


def check_cancel(cancel):
    if cancel.is_set():
        raise Cancelled("已停止，已完成的进度保存在本地。")


@dataclass(frozen=True)
class ChallengeItem:
    question_id: str
    showtype: str
    group: int
    index: int
    category: str
    prompt: Prompt

    @property
    def reference(self):
        prefix = "[word]" if self.category == "read_word" else "[content]"
        return prefix + "\n" + self.prompt.text


def challenge_kind(data):
    modes = records((data.get('content') or {}).get('moshi'))
    mode = next((mode for mode in modes if mode.get('name') == '挑战'), None)
    if mode is None:
        return 'unsupported'
    content = (mode.get('question') or {}).get('qcontent') or {}
    for group in records(content.get('item')):
        for question in records(group.get('questions')):
            if any(question.get(key) for key in ('right_answer', 'rightAnswer', 'rightKeys')) or any(
                answer.get('choise_list') for answer in records(question.get('answer'))):
                return 'listening'
    return 'reading' if mode.get('showtype') in {'t1', 't2', 't3', 't4', 't10', 't11', 't13'} else 'unsupported'


def challenge_items(data):
    if challenge_kind(data) == 'listening':
        raise UnsupportedQuestion('此关卡为听力选择题，原生提交尚未接入，未按朗读处理。')
    modes = records((data.get("content") or {}).get("moshi"))
    mode = next((m for m in modes if m.get("name") == "挑战"), None)
    if mode is None:
        raise UnsupportedQuestion("关卡没有挑战题目。")
    kind = str(mode.get("showtype") or "")
    if kind not in {"t1", "t2", "t3", "t4", "t10", "t11", "t13"}:
        raise UnsupportedQuestion(f"题型 {kind or '未知'} 的线上提交尚未接入。")
    question = mode.get("question") or {}
    question_id = str(question.get("questionid") or "")
    content = question.get("qcontent") or {}
    if not question_id or not isinstance(content, dict):
        raise ApiError("题目缺少编号或内容。")
    entries = [(group, index, entry) for group, item in enumerate(records(content.get("item")))
               for index, entry in enumerate(records(item.get("questions")))]
    if kind == "t4":
        if entries:
            roles = str(content.get("sample") or "").split("#")
            selected = [entry for role in roles for entry in entries
                        if entry[0] == 0 and str(entry[2].get("photo") or "").strip() == role.strip() and role.strip()]
        else:
            roles = [role.strip() for role in str(content.get("roles") or "").split("#") if role.strip()]
            fragments = str(content.get("text") or "").split("##")
            entries = []
            for index, fragment in enumerate(fragments):
                match = re.match(r"^\s*([^:：]+)[:：]\s*(.+)$", fragment)
                if match and match[1].strip() in roles:
                    entries.append((0, index, {"text": match[2], "photo": match[1].strip()}))
            selected = [entry for role in roles for entry in entries if entry[2]["photo"] == role]
        if len(selected) != len(entries):
            raise ApiError("角色和题目对应关系不完整，无法生成挑战结果。")
        entries = selected
    output = []
    prefix = str(data.get("resourcePrefixPath") or "").rstrip("/") + "/"
    for group, index, entry in entries:
        text = plain(entry.get("text")) or plain(entry.get("title"))
        if not text:
            raise ApiError("存在缺少评测文本的题目。")
        category = "read_sentence"
        if kind in {"t1", "t10"} and " " not in text and "-" not in text:
            category = "read_word"
        if kind in {"t11", "t13"}:
            category = "read_chapter"
        output.append(ChallengeItem(f"{question_id}#{group}#{index}", kind, group, index, category,
                                    Prompt(text, plain(entry.get("title_cn")), audio_url(prefix, entry.get("res_path")))))
    if not output or len({item.question_id for item in output}) != len(output):
        raise ApiError("题目数量或编号异常。")
    return output


def convert_audio(ffmpeg, source: Path, target: Path):
    command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
               "-vn", "-ac", "1", "-ar", "16000", "-t", "301", "-c:a", "pcm_s16le", str(target)]
    try:
        result = subprocess.run(command, capture_output=True, timeout=120,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ApiError("无法运行 FFmpeg，请选择可用的 ffmpeg.exe。") from exc
    if result.returncode:
        raise ApiError(f"无法读取音频 {source.name}，请检查格式。")
    with wave.open(str(target), "rb") as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != 16000:
            raise ApiError("音频转换结果不符合评测格式。")
        duration = audio.getnframes() / 16000
        if not 0 < duration <= 300:
            raise ApiError("每题音频长度需在 0 到 300 秒之间。")
        return audio.readframes(audio.getnframes()), duration


def evaluation_node(xml):
    if len(xml) > 2_000_000 or b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
        raise ApiError("评测结果格式异常。")
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise ApiError("评测结果无法解析。") from exc
    result = next((node for paper in root.iter("rec_paper") for node in paper
                   if node.tag in {"read_word", "read_sentence", "read_chapter"}), None)
    if result is None or "total_score" not in result.attrib:
        raise ApiError("讯飞结果缺少评分。")
    return result


def evaluation_details(xml):
    result = evaluation_node(xml)
    return {"rejected": result.get("is_rejected", "").lower() == "true",
            "except_info": result.get("except_info", "0"), "reject_type": result.get("reject_type", "0"),
            "raw_score": result.get("total_score", "")}


def rejected_evaluation(item, details, result=None):
    if "usable" in details:
        return details["usable"] is False
    if item.category == "read_chapter":
        return False
    if "rejected" in details:
        return details["rejected"] is True
    # Older clients saved this marker without the XML. The literal word "word"
    # can legitimately have that detail at zero, so do not infer rejection for it.
    return item.prompt.text.casefold() != "word" and (result or {}).get("ansDetail") == "word#0"


def parse_result(xml, item):
    result = evaluation_node(xml)

    def score(node, key):
        try:
            value = float(node.get(key, "0"))
            if not math.isfinite(value) or not 0 <= value <= 5:
                raise ValueError()
            return int(value * 20)
        except ValueError as exc:
            raise ApiError("讯飞评分数值异常。") from exc

    fields = {"questionId": item.question_id, "semantic": score(result, "phone_score"),
              "accuracy": score(result, "accuracy_score"), "fluency": score(result, "fluency_score"),
              "complete": score(result, "integrity_score"), "score": score(result, "total_score")}
    if item.category != "read_chapter" and result.get("is_rejected", "").lower() == "true":
        return {**dict.fromkeys(fields, 0), "questionId": item.question_id, "ansDetail": "word#0"}
    if item.category == "read_word":
        detail = result.get("content", item.prompt.text) + "#" + str(fields["score"])
    else:
        words = [word for word in result.iter("word")
                 if word.get("content") not in {None, "sil", "fil"} and word.get("dp_message") != "32"]
        detail = ",".join(word.get("content").replace(",", "") for word in words)
        detail += "#" + ",".join(str(score(word, "total_score")) for word in words)
    fields["ansDetail"] = detail
    return fields


def evaluate(auth, pcm, item, cancel):
    url = str(auth.get("authUrl") or "")
    parsed = urlsplit(url)
    if parsed.scheme != "wss" or parsed.hostname != "ise-api.xfyun.cn" or not auth.get("appId"):
        raise ApiError("平台返回的讯飞评测授权无效。")
    check_cancel(cancel)
    try:
        socket = websocket.create_connection(url, timeout=20, origin="https://static.fifedu.com")
    except (OSError, websocket.WebSocketException) as exc:
        raise EvaluationInterrupted("无法建立讯飞评测连接，请重新获取授权后重试。") from exc
    socket.settimeout(1)
    deadline = time.monotonic() + len(pcm) / 32000 + 30

    def receive():
        while True:
            check_cancel(cancel)
            try:
                message = socket.recv()
            except websocket.WebSocketTimeoutException:
                if time.monotonic() >= deadline:
                    raise EvaluationInterrupted("讯飞评测结果等待超时，请重试。")
                continue
            if not message:
                raise EvaluationInterrupted("讯飞评测连接提前结束。")
            if len(message) > 3_000_000:
                raise ApiError("讯飞评测响应过大。")
            response = json.loads(message)
            if response.get("code") != 0:
                code = response.get("code")
                error = EvaluationInterrupted if code in {10114, 60114} else ApiError
                raise error(f"讯飞评测返回错误码 {code}：{str(response.get('message') or '')[:100]}")
            data = response.get("data") or {}
            if data.get("status") == 2:
                try:
                    return base64.b64decode(data["data"], validate=True)
                except (ValueError, KeyError) as exc:
                    raise ApiError("讯飞结果无法解码。") from exc

    worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ise-result")
    try:
        # Receive while transmitting, because the service can reject an audio stream early.
        pending = worker.submit(receive)
        socket.send(json.dumps({"common": {"app_id": auth["appId"]}, "business": {
            "category": item.category, "rstcd": "utf8", "group": "pupil", "sub": "ise", "ent": "en_vip",
            "tte": "utf-8", "cmd": "ssb", "auf": "audio/L16;rate=16000", "aus": 1,
            "aue": "raw", "text": item.reference,
        }, "data": {"status": 0, "encoding": "raw", "data_type": 1, "data": ""}}))
        start = time.monotonic()
        for offset in range(0, len(pcm), 1280):
            check_cancel(cancel)
            if pending.done():
                return pending.result()
            socket.send(json.dumps({"business": {"cmd": "auw", "aus": 1 if offset == 0 else 2, "aue": "raw"},
                                    "data": {"status": 1, "encoding": "raw", "data_type": 1,
                                             "data": base64.b64encode(pcm[offset:offset+1280]).decode()}}))
            delay = start + (offset + 1280) / 32000 - time.monotonic()
            if delay > 0:
                cancel.wait(delay)
        check_cancel(cancel)
        socket.send(json.dumps({"business": {"cmd": "auw", "aus": 4, "aue": "raw"},
                                "data": {"status": 2, "encoding": "raw", "data_type": 1, "data": ""}}))
        return pending.result(timeout=25)
    except (OSError, ValueError, TimeoutError, websocket.WebSocketException) as exc:
        raise EvaluationInterrupted("讯飞评测未完成，请检查网络后重试。") from exc
    finally:
        socket.close()
        worker.shutdown(wait=True, cancel_futures=True)
