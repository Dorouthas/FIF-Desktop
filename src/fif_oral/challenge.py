import hashlib
import json
import math
import os
import re
import tempfile
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import oss2
from fif_oral.audio.adaptive import NAMES

from fif_oral.audio.assessment import (
    Cancelled, EvaluationInterrupted, EvaluationRejected, check_cancel, convert_audio, evaluate,
    evaluation_details, rejected_evaluation, parse_result, challenge_items,
)
from fif_oral.api.client import ApiError, SessionExpired, SubmissionUncertain, records


class ChallengePending(EvaluationRejected):
    pass


class AttemptStore:
    def __init__(self, root, account_id, user_id, task_id, unit_id, level_id):
        self.context = {"account_id": account_id, "user_id": user_id, "task_id": task_id,
                        "unit_id": unit_id, "level_id": level_id}
        digest = hashlib.sha256(json.dumps(self.context, sort_keys=True).encode()).hexdigest()
        self.folder = root / digest
        self.path = None
        self.data = {}
        paths = sorted(self.folder.glob("*.json"), reverse=True)
        if paths:
            self.path = paths[0]
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(self.data, dict) or self.data.get("version") != 1 or self.data.get("context") != self.context:
                    raise ValueError()
            except (OSError, ValueError, TypeError) as error:
                raise ApiError("挑战进度文件无法读取，请检查该关卡的本地记录。") from error

    def start(self, items, files, hashes, source=None):
        now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        run_id = now + "-" + uuid4().hex[:10]
        self.path = self.folder / f"{run_id}.json"
        self.data = {"version": 1, "context": self.context, "run_id": run_id, "stage": "preparing",
                     "timestamp": int(time.time() * 1000), "items": [asdict(item) for item in items],
                     "files": [str(path.resolve()) for path in files], "completed": {}, "hashes": hashes,
                     "source": source or {"mode": "files"}}
        self.save()

    def save(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.folder,
                                         suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            try:
                json.dump(self.data, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            except BaseException:
                file.close()
                temporary.unlink(missing_ok=True)
                raise
        try:
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def save_evaluation(self, item, xml, details):
        folder = self.path.with_suffix("")
        folder.mkdir(parents=True, exist_ok=True)
        history = self.data.setdefault("evaluation_history", {}).setdefault(item.question_id, [])
        path = folder / f"{item.group}_{item.index}_{len(history) + 1}.xml"
        with tempfile.NamedTemporaryFile(dir=folder, suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            try:
                file.write(xml)
                file.flush()
                os.fsync(file.fileno())
            except BaseException:
                file.close()
                temporary.unlink(missing_ok=True)
                raise
        try:
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        entry = {**details, "xml": str(path.relative_to(self.folder))}
        history.append(entry)
        self.data.setdefault("evaluations", {})[item.question_id] = entry
        self.save()
        return entry


def file_hash(path):
    try:
        with path.open("rb") as file:
            return hashlib.file_digest(file, "sha256").hexdigest()
    except OSError as exc:
        raise ApiError(f"无法读取音频 {path.name}。") from exc


def upload(client, profile, item, wav, xml, timestamp, task_id):
    credentials = client.oss_credentials()
    endpoint = str(credentials.get("endpoint") or "")
    if "://" not in endpoint:
        endpoint = "https://" + endpoint
    parsed = urlsplit(endpoint)
    if (not parsed.hostname or not parsed.hostname.endswith(".aliyuncs.com")
            or parsed.username or parsed.password or parsed.port not in (None, 80, 443)):
        raise ApiError("平台返回的 OSS 地址无效。")
    endpoint = "https://" + parsed.hostname
    bucket = str(credentials.get("bucketName") or "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket):
        raise ApiError("平台返回的 OSS 存储桶无效。")
    required = ("accessKeyId", "accessKeySecret", "securityToken", "ossPath")
    if any(not credentials.get(key) for key in required):
        raise ApiError("平台返回的上传凭据不完整。")
    auth = oss2.StsAuth(*(credentials[key] for key in required[:3]))
    storage = oss2.Bucket(auth, endpoint, bucket, connect_timeout=20)
    parts = [str(profile["schoolId"]), client.user_id, item.question_id.split("#")[0],
             item.showtype, task_id, str(timestamp)]
    if any(not part or "/" in part or "\\" in part or part in {".", ".."} for part in parts):
        raise ApiError("题目或账号包含无效的上传路径。")
    key = "/".join(parts) + f"/{item.group}_{item.index}"
    prefix = urlsplit(str(credentials["ossPath"])).path.strip("/")
    audio_key = key + ".mp3"
    if prefix:
        if not audio_key.startswith(prefix + "/"):
            raise ApiError("录音存储路径与平台前缀不一致。")
        audio_key = audio_key[len(prefix) + 1:]
    # The assessment page stores WAV data under an .mp3 object key. Preserve that format.
    try:
        with wav.open("rb") as file:
            audio = storage.put_object(key + ".mp3", file, headers={"Content-Type": "audio/wav"})
        result = storage.put_object(key + ".xml", xml, headers={"Content-Type": "text/xml; charset=utf-8"})
        if audio.status != 200 or result.status != 200:
            raise ApiError("音频或评测结果上传未成功。")
    except (oss2.exceptions.OssError, OSError) as exc:
        raise ApiError("OSS 上传失败，已完成的进度可以继续。") from exc
    return audio_key


def normalized_path(value):
    return unquote(urlsplit(str(value)).path).lstrip("/")


def matches_record(report, results):
    expected = [normalized_path(result["recordPath"]) for result in results]
    actual = [normalized_path(row.get("audioPath")) for row in records(report.get("stuTaskList"))]
    if not actual:
        actual = [normalized_path(row.get("audio")) for row in records(report.get("audioAndDurations"))]
    if len(actual) != len(expected) or not all(expected):
        return False
    for path in expected:
        match = next((i for i, candidate in enumerate(actual)
                      if candidate == path or candidate.endswith("/" + path)), None)
        if match is None:
            return False
        actual.pop(match)
    return True


def reconcile(client, store, cancel, progress):
    data = store.data
    context = store.context
    results = [data["completed"][item["question_id"]]["result"] for item in data["items"]]
    for turn in range(4):
        check_cancel(cancel)
        progress("正在回查本次挑战成绩。")
        report_id = data.get("report_id")
        try:
            if report_id:
                report = client.challenge_report(context["task_id"], context["level_id"], report_id)
                if matches_record(report, results):
                    return confirm(store, report_id, report)
            else:
                history = client.challenge_history(context["task_id"], context["level_id"])
                baseline = set(data.get("baseline", []))
                for row in records(history.get("challengeRecs")):
                    record_id = str(row.get("kuChallengeRecUid") or "")
                    if record_id and record_id not in baseline:
                        report = client.challenge_record(context["task_id"], record_id)
                        if matches_record(report, results):
                            return confirm(store, record_id, report)
        except ApiError:
            if turn == 3:
                raise
        if turn < 3:
            cancel.wait(2)
    raise SubmissionUncertain("本次结果还未核实。点击「回查成绩」继续查询。")


def confirm(store, report_id, report):
    score = report.get("score")
    if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score):
        raise ApiError("报告缺少本次分数，请稍后回查。")
    store.data.update(stage="confirmed", report_id=report_id, score=score)
    store.save()
    return {"report_id": report_id, "score": score, "report": report}


def assess_question(client, store, profile, item, source, ffmpeg, cancel, progress, index, total, audio_policy,
                    *, defer_upload=False):
    best, last_details = None, {}
    tried = set()
    if hasattr(getattr(audio_policy, 'generator', None), 'client'):
        audio_policy.generator.client = client
    trials = audio_policy.trials if audio_policy else 1
    with tempfile.TemporaryDirectory(prefix="fif-assessment-") as folder:
        for attempt in range(trials + (1 if hasattr(audio_policy, "reserve") else 0)):
            check_cancel(cancel)
            recording = audio_policy.prepare(item, source, tried, cancel) if audio_policy else None
            if audio_policy and recording is None:
                break
            name = recording.profile if recording else "base"
            path = recording.path if recording else source
            tried.add(name)
            title = (recording.label if recording else "") or NAMES.get(name, name)
            progress(f"第 {index}/{total} 题 · {title}。")
            wav = Path(folder) / f"{attempt}.wav"
            pcm, duration = convert_audio(ffmpeg, path, wav)
            cached = recording.cached if recording else None
            if cached:
                if cached.get("normalized_sha256") and file_hash(wav) != cached["normalized_sha256"]:
                    audio_policy.invalidate_cache(recording)
                    raise ApiError("已验收候选的转换结果发生变化，需要重新评测。")
                xml = cached["xml_bytes"]
                progress(f"第 {index}/{total} 题复用已验收候选及原始评测 XML。")
            else:
                if hasattr(audio_policy, "reserve"):
                    audio_policy.reserve(recording)
                for retry in range(3):
                    check_cancel(cancel)
                    auth = client.assessment_auth(2)
                    try:
                        xml = evaluate(auth, pcm, item, cancel)
                        break
                    except EvaluationInterrupted:
                        if retry == 2:
                            raise
                        progress(f"第 {index}/{total} 题评测连接中断，正在重新连接（{retry + 2}/3）。")
                        cancel.wait(retry + 1)
            result = parse_result(xml, item)
            details = evaluation_details(xml)
            last_details = details
            if audio_policy and not hasattr(audio_policy, "observe_artifact"):
                audio_policy.observe(recording, details, float(details["raw_score"]) * 20)
            unusable = rejected_evaluation(item, details) or (audio_policy is not None and details["except_info"] != "0")
            entry = store.save_evaluation(item, xml, {**details, "profile": name, "label": title, "usable": not unusable,
                                          "score": result["score"], "audio": str(path.resolve()),
                                          "audio_sha256": file_hash(path), "normalized_sha256": file_hash(wav),
                                          "cached": bool(cached),
                                          "generation": audio_policy.describe(recording) if hasattr(audio_policy, 'describe') else {}})
            if hasattr(audio_policy, "observe_artifact"):
                audio_policy.observe_artifact(recording, details, result, entry, store)
            if unusable:
                progress(f"第 {index}/{total} 题 · {title} 评测异常（{details['except_info']}），正在选择其他候选。")
                continue
            candidate = (result, xml, duration, wav, entry)
            if best is None or result["score"] > best[0]["score"]:
                best = candidate
            if not audio_policy or result["score"] >= audio_policy.target:
                break
            progress(f"第 {index}/{total} 题得分 {result['score']}，低于目标 {audio_policy.target}，正在调整音频。")

        if best is None:
            reasons = {"28673": "没有有效语音或音量过小", "28676": "引擎判定朗读与参考文本不匹配",
                       "28680": "音频信噪比过低", "28709": "音频信噪比过低", "28690": "音频出现截幅"}
            code = last_details.get("except_info", "未知")
            reason = reasons.get(code, "音频未通过评测引擎检查")
            budget = audio_policy.failure_reason() if hasattr(audio_policy, "failure_reason") else f"已尝试 {len(tried)} 种方案。"
            raise EvaluationRejected(f"第 {index} 题「{item.prompt.text[:50]}」待复核：{reason}（{code}）。{budget}")
        result, xml, duration, wav, entry = best
        store.data["evaluations"][item.question_id] = entry
        store.save()
        check_cancel(cancel)
        result["learn_time"] = int(duration)
        if defer_upload:
            folder = store.path.with_suffix("") / "accepted"
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{item.group}_{item.index}.wav"
            with wav.open("rb") as original, tempfile.NamedTemporaryFile(dir=folder, suffix=".tmp", delete=False) as file:
                temporary = Path(file.name)
                try:
                    import shutil
                    shutil.copyfileobj(original, file)
                    file.flush()
                    os.fsync(file.fileno())
                except BaseException:
                    file.close()
                    temporary.unlink(missing_ok=True)
                    raise
            try:
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            store.data.setdefault("prepared", {})[item.question_id] = {"result": dict(result),
                "audio": str(target.resolve()), "audio_sha256": file_hash(target), "xml": entry["xml"],
                "xml_sha256": file_hash(store.folder / entry["xml"])}
            store.data.setdefault("pending", {}).pop(item.question_id, None)
            store.save()
            progress(f"第 {index}/{total} 题已验收，得分 {result['score']}。")
        else:
            progress(f"第 {index}/{total} 题得分 {result['score']}，正在上传。")
            result["recordPath"] = upload(client, profile, item, wav, xml, store.data["timestamp"], store.context["task_id"])
    return result


def run_challenge(client, store, items, files, ffmpeg, cancel, progress, *, source=None, audio_policy=None):
    context = store.context
    check_cancel(cancel)
    progress("正在建立评测会话。")
    client = client.assessment(context["task_id"], context["unit_id"], context["level_id"])
    if store.data.get("stage") in {"submitting", "submitted", "confirmed"}:
        return reconcile(client, store, cancel, progress)

    profile = client.profile()
    if profile.get("language") not in (2, "2") or not profile.get("schoolId"):
        raise ApiError("当前接入英文挑战，请选择英文关卡。")
    config = client.challenge_config(context["task_id"], context["level_id"])
    if int(config.get("answerCount") or 0) > 0 and int(config["answerCount"]) <= int(config.get("challengeTimes") or 0):
        raise ApiError("该关卡的挑战次数已用完。")
    fresh = challenge_items(client.level(context["level_id"]))
    if fresh != items:
        raise ApiError("平台题目已更新，请重新打开这个关卡。")
    if len(files) != len(items):
        raise ApiError("每道题需要选择一份音频。")
    origin = (store.data.get('source') or source or {})
    missing = origin.get('generation_failed', {})
    hashes = [None if not path.is_file() and item.question_id in missing else file_hash(path)
              for item, path in zip(items, files)]
    if store.data:
        if store.data.get("hashes") != hashes or store.data.get("items") != [asdict(item) for item in items]:
            raise ApiError("音频或题目发生变化，请开始新一轮挑战。")
    else:
        store.start(items, files, hashes, source)
    if hasattr(audio_policy, "bind"):
        audio_policy.bind(store, items, files)

    if any(rejected_evaluation(item, store.data.get("evaluations", {}).get(item.question_id, {}),
                               store.data["completed"].get(item.question_id, {}).get("result")) and
           item.question_id in store.data["completed"] for item in items):
        raise EvaluationRejected("上次进度包含被评测引擎拒识的音频。请点击「新一轮」，更换模型或音频后重新评测；单词题可尝试 XTTS v2。")

    for index, (item, source) in enumerate(zip(items, files), 1):
        check_cancel(cancel)
        if item.question_id in store.data["completed"]:
            progress(f"已恢复第 {index}/{len(items)} 题的结果。")
            continue
        if item.question_id in store.data.get("prepared", {}):
            progress(f"已恢复第 {index}/{len(items)} 题验收结果。")
            continue
        previous = store.data.get("evaluations", {}).get(item.question_id, {})
        if audio_policy is None and item.question_id in store.data.get("pending", {}) and \
                previous.get("usable") is False and previous.get("audio_sha256") == file_hash(source):
            progress(f"第 {index}/{len(items)} 题音频未改变，保留待复核状态。")
            continue
        try:
            assess_question(client, store, profile, item, source, ffmpeg, cancel, progress,
                            index, len(items), audio_policy, defer_upload=True)
        except (Cancelled, SessionExpired):
            raise
        except ApiError as error:
            store.data.setdefault("pending", {})[item.question_id] = {"reason": str(error), "time": time.time()}
            store.save()
            progress(f"第 {index}/{len(items)} 题待复核，继续处理后续题目。")

    if store.data.get("pending"):
        store.data["stage"] = "review"
        store.save()
        detail = next(iter(store.data["pending"].values()))["reason"]
        raise ChallengePending(f"已验收 {len(store.data.get('prepared', {})) + len(store.data['completed'])}/{len(items)} 题；"
                               f"{len(store.data['pending'])} 题待复核，尚未提交关卡。{detail}")

    # Freeze and verify the selected WAV/XML pairs before the first upload.
    for item in items:
        if item.question_id in store.data["completed"]:
            continue
        prepared = store.data["prepared"][item.question_id]
        audio = Path(prepared["audio"])
        xml = store.folder / prepared["xml"]
        if file_hash(audio) != prepared["audio_sha256"] or file_hash(xml) != prepared["xml_sha256"]:
            raise ApiError("已验收的音频或 XML 被修改，已停止上传。")
        actual_result = parse_result(xml.read_bytes(), item)
        if any(prepared["result"].get(key) != value for key, value in actual_result.items()):
            raise ApiError("已验收的分数与原始 XML 不一致，已停止上传。")
    store.data["stage"] = "uploading"
    store.save()
    for item in items:
        check_cancel(cancel)
        if item.question_id in store.data["completed"]:
            continue
        prepared = store.data["prepared"][item.question_id]
        result = dict(prepared["result"])
        result["recordPath"] = upload(client, profile, item, Path(prepared["audio"]),
            (store.folder / prepared["xml"]).read_bytes(), store.data["timestamp"], context["task_id"])
        store.data["completed"][item.question_id] = {"result": result}
        store.save()

    check_cancel(cancel)
    history = client.challenge_history(context["task_id"], context["level_id"])
    store.data["baseline"] = [str(record["kuChallengeRecUid"]) for record in records(history.get("challengeRecs"))
                              if record.get("kuChallengeRecUid")]
    results = [store.data["completed"][item.question_id]["result"] for item in sorted(items, key=lambda q: (q.group, q.index))]
    check_cancel(cancel)
    store.data["stage"] = "submitting"
    store.save()
    progress("正在提交本次挑战结果。")
    try:
        store.data["report_id"] = client.submit_challenge(context["task_id"], context["level_id"], results, profile)
    except SubmissionUncertain:
        progress("提交响应未确认，正在按录音路径回查。")
    except ApiError:
        store.data["stage"] = "rejected"
        store.save()
        raise
    else:
        store.data["stage"] = "submitted"
        store.save()
    return reconcile(client, store, cancel, progress)
