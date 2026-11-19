import json
import shutil
import tempfile
import unittest
import wave
from http.client import IncompleteRead
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import fif_oral.audio.assessment as assessment
import fif_oral.challenge as challenge
from fif_oral.api.client import ApiError, FiFClient, SubmissionUncertain


XML = b'''<xml_result><read_chapter><rec_paper>
<read_chapter total_score="3.6" phone_score="4" accuracy_score="3.5"
fluency_score="4.5" integrity_score="5"><sentence content="Hello world">
<word content="sil" total_score="0"/><word content="Hello," total_score="4"/>
<word content="world" total_score="3.5"/><word content="extra" dp_message="32"/>
</sentence></read_chapter></rec_paper></read_chapter></xml_result>'''

REJECTED_WORD_XML = b'''<xml_result><read_word><rec_paper>
<read_word content="thank" total_score="4.2" accuracy_score="4.2" is_rejected="true"
except_info="28676" reject_type="128"/>
</rec_paper></read_word></xml_result>'''


def word_level():
    data = level(("thank",))
    data["content"]["moshi"][0]["showtype"] = "t1"
    return data


def level(texts=("Hello world",)):
    return {"content": {"moshi": [{"name": "挑战", "showtype": "t11", "question": {
        "questionid": "question", "qcontent": {"item": [{"questions": [{"text": text} for text in texts]}]}
    }}]}}


class Platform:
    user_id = "student"
    name = "Test student"

    def expired(self):
        return False

    def __init__(self, data, lost=False):
        self.data = data
        self.lost = lost
        self.submissions = []
        self.reports = 0
        self.on_history = None

    def assessment(self, *args):
        return self

    def profile(self):
        return {"language": 2, "schoolId": "school"}

    def challenge_config(self, *args):
        return {"answerCount": 0, "challengeTimes": 2}

    def level(self, *args):
        return self.data

    def assessment_auth(self, *args):
        return {}

    def challenge_history(self, *args):
        if self.on_history:
            self.on_history()
        ids = ["old"] + (["unrelated", "ours"] if self.submissions else [])
        return {"challengeRecs": [{"kuChallengeRecUid": identity} for identity in ids]}

    def challenge_record(self, task, record):
        paths = [result["recordPath"] for result in self.submissions[0]]
        if record == "unrelated":
            paths = ["different.wav"]
        return {"score": 72 if record == "ours" else 100,
                "stuTaskList": [{"audioPath": "https://cdn.example/prefix/" + path} for path in paths]}

    def challenge_report(self, task, level, record):
        self.reports += 1
        return self.challenge_record(task, record)

    def submit_challenge(self, task, level, results, profile):
        self.submissions.append(results)
        if self.lost:
            raise SubmissionUncertain("Response lost after acceptance")
        return "ours"


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is needed for conversion coverage")
class ChallengeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.audio = self.root / "input.wav"
        with wave.open(str(self.audio), "wb") as file:
            file.setparams((2, 2, 44100, 0, "NONE", "not compressed"))
            file.writeframes(b"\0" * 4410 * 4)
        self.cancel = Event()
        self.uploaded = []

    def store(self):
        return challenge.AttemptStore(self.root, "account", "student", "task", "unit", "level")

    def upload(self, client, profile, item, wav, xml, timestamp, task):
        with wave.open(str(wav), "rb") as audio:
            self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()), (1, 2, 16000))
        self.assertEqual(xml, XML)
        self.uploaded.append(item.question_id)
        return f"{timestamp}/{item.group}_{item.index}.mp3"

    def run_flow(self, platform, store, items):
        with patch.object(challenge, "evaluate", return_value=XML) as evaluate, \
                patch.object(challenge, "upload", side_effect=self.upload):
            result = challenge.run_challenge(platform, store, items, [self.audio] * len(items),
                                             shutil.which("ffmpeg"), self.cancel, lambda message: None)
        return result, evaluate.call_count

    def test_conversion_submit_and_exact_report(self):
        data = level()
        platform, store = Platform(data), self.store()
        result, count = self.run_flow(platform, store, assessment.challenge_items(data))
        self.assertEqual((result["score"], result["report_id"], count), (72, "ours", 1))
        self.assertEqual(len(platform.submissions), 1)
        self.assertEqual(platform.submissions[0][0]["ansDetail"], "Hello,world#80,70")
        self.assertEqual(platform.submissions[0][0]["accuracy"], 70)
        self.assertEqual(store.data["stage"], "confirmed")
        self.assertNotIn("token", store.path.read_text(encoding="utf-8"))

    def test_lost_response_recovers_our_record_and_resume_never_resubmits(self):
        data = level()
        platform, store = Platform(data, lost=True), self.store()
        items = assessment.challenge_items(data)
        result, _ = self.run_flow(platform, store, items)
        self.assertEqual((result["report_id"], result["score"]), ("ours", 72))
        self.assertEqual(len(platform.submissions), 1)
        result, evaluations = self.run_flow(platform, self.store(), items)
        self.assertEqual(evaluations, 0)
        self.assertEqual(len(self.uploaded), 1)
        self.assertEqual(len(platform.submissions), 1)

    def test_partial_resume_and_changed_audio_rejected(self):
        data = level(("First sentence", "Second sentence"))
        platform, store = Platform(data), self.store()
        items = assessment.challenge_items(data)
        with patch.object(challenge, "evaluate", side_effect=[XML, assessment.Cancelled("stop")]), \
                patch.object(challenge, "upload", side_effect=self.upload):
            with self.assertRaises(assessment.Cancelled):
                challenge.run_challenge(platform, store, items, [self.audio] * 2,
                                         shutil.which("ffmpeg"), self.cancel, lambda message: None)
        self.assertEqual(len(store.data["prepared"]), 1)
        self.assertEqual(len(store.data["completed"]), 0)
        self.assertEqual(len(self.uploaded), 0)
        self.assertEqual(len(platform.submissions), 0)
        original = self.audio.read_bytes()
        self.audio.write_bytes(original + b"changed")
        with self.assertRaisesRegex(ApiError, "发生变化"):
            self.run_flow(platform, self.store(), items)
        self.audio.write_bytes(original)
        _, evaluations = self.run_flow(platform, self.store(), items)
        self.assertEqual(evaluations, 1)
        self.assertEqual(len(platform.submissions), 1)

    def test_interrupted_evaluation_retries_without_duplicate_upload_or_submit(self):
        data = level()
        platform, store = Platform(data), self.store()
        items = assessment.challenge_items(data)
        with patch.object(challenge, "evaluate", side_effect=[assessment.EvaluationInterrupted("reset"), XML]) as evaluate, \
                patch.object(challenge, "upload", side_effect=self.upload):
            result = challenge.run_challenge(platform, store, items, [self.audio],
                                             shutil.which("ffmpeg"), self.cancel, lambda message: None)
        self.assertEqual(result["score"], 72)
        self.assertEqual(evaluate.call_count, 2)
        self.assertEqual(len(self.uploaded), 1)
        self.assertEqual(len(platform.submissions), 1)

    def test_upload_failure_resumes_without_reassessment_or_duplicate_completed_upload(self):
        data = level(('First sentence', 'Second sentence'))
        platform, store = Platform(data), self.store()
        items = assessment.challenge_items(data)
        with patch.object(challenge, 'evaluate', return_value=XML), \
                patch.object(challenge, 'upload', side_effect=['first.mp3', ApiError('OSS failed')]):
            with self.assertRaisesRegex(ApiError, 'OSS failed'):
                challenge.run_challenge(platform, store, items, [self.audio] * 2, shutil.which('ffmpeg'),
                                         self.cancel, lambda m: None)
        self.assertEqual(len(store.data['completed']), 1)
        self.assertEqual(len(store.data['prepared']), 2)
        self.assertFalse(platform.submissions)
        with patch.object(challenge, 'evaluate', side_effect=AssertionError('must not re-evaluate')), \
                patch.object(challenge, 'upload', return_value='second.mp3') as upload:
            challenge.run_challenge(platform, store, items, [self.audio] * 2, shutil.which('ffmpeg'),
                                     self.cancel, lambda m: None)
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(len(platform.submissions), 1)

    def test_cancel_during_history_prevents_submit(self):
        data = level()
        platform, store = Platform(data), self.store()
        platform.on_history = self.cancel.set
        with self.assertRaises(assessment.Cancelled):
            self.run_flow(platform, store, assessment.challenge_items(data))
        self.assertEqual(len(platform.submissions), 0)
        self.assertEqual(store.data["stage"], "uploading")

    def test_rejected_word_preserves_xml_and_stops_before_upload_or_submit(self):
        data = word_level()
        platform, store = Platform(data), self.store()
        items = assessment.challenge_items(data)
        with patch.object(challenge, "evaluate", return_value=REJECTED_WORD_XML), \
                patch.object(challenge, "upload") as upload:
            with self.assertRaisesRegex(assessment.EvaluationRejected, "28676"):
                challenge.run_challenge(platform, store, items, [self.audio],
                                         shutil.which("ffmpeg"), self.cancel, lambda message: None)
        upload.assert_not_called()
        self.assertFalse(platform.submissions)
        self.assertFalse(store.data["completed"])
        details = store.data["evaluations"][items[0].question_id]
        self.assertTrue(details["rejected"])
        self.assertEqual((store.folder / details["xml"]).read_bytes(), REJECTED_WORD_XML)

    def test_legacy_rejected_zero_cannot_be_submitted_on_resume(self):
        data = word_level()
        platform, store = Platform(data), self.store()
        items = assessment.challenge_items(data)
        store.start(items, [self.audio], [challenge.file_hash(self.audio)])
        store.data["completed"][items[0].question_id] = {
            "result": {"score": 0, "ansDetail": "word#0", "recordPath": "old.mp3"}}
        store.save()
        with patch.object(challenge, "evaluate") as evaluate:
            with self.assertRaisesRegex(assessment.EvaluationRejected, "新一轮"):
                challenge.run_challenge(platform, store, items, [self.audio],
                                         shutil.which("ffmpeg"), self.cancel, lambda message: None)
        evaluate.assert_not_called()
        self.assertFalse(platform.submissions)


class ProtocolTests(unittest.TestCase):
    def test_server_evaluation_timeout_is_retryable(self):
        socket = SimpleNamespace(settimeout=lambda value: None, send=lambda data: None,
                                 recv=lambda: json.dumps({"code": 60114, "message": "session timeout"}),
                                 close=lambda: None)
        auth = {"authUrl": "wss://ise-api.xfyun.cn/v2/open-ise", "appId": "app"}
        with patch("fif_oral.audio.assessment.websocket.create_connection", return_value=socket):
            with self.assertRaisesRegex(assessment.EvaluationInterrupted, "60114"):
                assessment.evaluate(auth, b"", assessment.challenge_items(word_level())[0], Event())

    def test_rejected_raw_score_is_not_accepted_and_legitimate_zero_is_distinct(self):
        item = assessment.challenge_items(word_level())[0]
        self.assertEqual(assessment.parse_result(REJECTED_WORD_XML, item)["score"], 0)
        details = assessment.evaluation_details(REJECTED_WORD_XML)
        self.assertEqual((details["rejected"], details["raw_score"], details["except_info"]),
                         (True, "4.2", "28676"))
        zero_xml = REJECTED_WORD_XML.replace(b'is_rejected="true"', b'is_rejected="false"').replace(b'4.2', b'0')
        result = assessment.parse_result(zero_xml, item)
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["ansDetail"], "thank#0")
        self.assertFalse(assessment.evaluation_details(zero_xml)["rejected"])
        word = assessment.ChallengeItem("id", "t1", 0, 0, "read_word", assessment.Prompt("word", "", ""))
        self.assertFalse(assessment.rejected_evaluation(word, {}, {"ansDetail": "word#0"}))
        self.assertFalse(assessment.rejected_evaluation(item, {"rejected": False}, {"ansDetail": "word#0"}))

    def test_chapter_parser_and_reject_unsafe_xml(self):
        item = assessment.challenge_items(level())[0]
        self.assertEqual(item.category, "read_chapter")
        self.assertEqual(assessment.parse_result(XML, item)["score"], 72)
        rejected_chapter = XML.replace(b'total_score="3.6"', b'is_rejected="true" total_score="3.6"', 1)
        self.assertEqual(assessment.parse_result(rejected_chapter, item)["score"], 72)
        with self.assertRaises(ApiError):
            assessment.parse_result(b'<!DOCTYPE foo><foo/>', item)

    def test_dialogue_preserves_original_indices(self):
        data = level(("B's first sentence", "A's first sentence", "B's second sentence"))
        mode = data["content"]["moshi"][0]
        mode["showtype"] = "t4"
        content = mode["question"]["qcontent"]
        content["sample"] = "A#B"
        for row, role in zip(content["item"][0]["questions"], ["B", "A", "B"]):
            row["photo"] = role
        self.assertEqual([item.index for item in assessment.challenge_items(data)], [1, 0, 2])

    def test_record_matching_is_exact_multiset(self):
        results = [{"recordPath": "ours/0.mp3"}, {"recordPath": "ours/1.mp3"}]
        self.assertTrue(challenge.matches_record({"stuTaskList": [
            {"audioPath": "/prefix/ours/1.mp3"}, {"audioPath": "/prefix/ours/0.mp3"}]}, results))
        self.assertFalse(challenge.matches_record({"stuTaskList": [
            {"audioPath": "/prefix/ours/1.mp3"}, {"audioPath": "/prefix/ours/1.mp3"}]}, results))
        self.assertFalse(challenge.matches_record({"score": 100}, results))

    def test_submission_headers_and_form(self):
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return b'{"status":1,"data":{"questionAnswerId":"new-record"}}'

        def opened(request, **kwargs):
            requests.append(request)
            return Response()

        client = FiFClient("test-token", "10003", "student")
        profile = {"schoolId": "school", "userId": "student", "loginName": "test",
                   "userNo": "test", "language": 2, "role": 0, "realName": "张同学"}
        with patch("fif_oral.api.client.build_opener", return_value=SimpleNamespace(open=opened)):
            self.assertEqual(client.submit_challenge("task", "level", [], profile), "new-record")
        headers = {key.lower(): value for key, value in requests[0].header_items()}
        self.assertEqual(headers["userrole"], "0")
        self.assertEqual(headers["clienttype"], "6")
        self.assertEqual(headers["realname"], "\\u5f20\\u540c\\u5b66")
        self.assertIn(b"resultJson=%5B%5D", requests[0].data)

    def test_truncated_submission_response_requires_reconciliation(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                raise IncompleteRead(b'{"status":1,')

        client = FiFClient("test-token", "10003", "student")
        with patch("fif_oral.api.client.build_opener", return_value=SimpleNamespace(open=lambda *a, **k: Response())):
            with self.assertRaises(SubmissionUncertain):
                client.submit_challenge("task", "level", [], {})


if __name__ == "__main__":
    unittest.main()
