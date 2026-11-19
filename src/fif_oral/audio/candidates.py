"""Persistent candidate generation, admission checks, and verified-result cache."""
import hashlib
import json
import time
import fif_oral.audio.speech as speech
from dataclasses import asdict, replace
from pathlib import Path

from fif_oral.audio.adaptive import AdaptiveAudio, Recording, digest
from fif_oral.audio.assessment import Cancelled, ChallengeItem, check_cancel, convert_audio, evaluate, evaluation_details
from fif_oral.audio.alignment import carrier_sentence, extract_word
from fif_oral.audio.quality import inspect_audio
from fif_oral.api.client import ApiError, Prompt
from fif_oral.audio.text import SINGLE_WORD
from fif_oral.audio.speech import MODEL_FILES, MODEL_NAMES, save_json, synthesis_text, uses_system_voice


VERSION = "candidates-v1"
CACHE_SECONDS = 86400


class CandidateGenerator:
    def __init__(self, settings, voice, data_dir, items, cancel):
        self.settings, self.voice, self.data_dir = settings, voice, data_dir
        self.client, self.ffmpeg = None, ''
        self.align_request = None
        self.engines, self.unavailable = {}, {}
        self.system = speech.SystemSpeech(cancel) if any(uses_system_voice(item, settings) for item in items) else None
        self.edge = None
        config = asdict(settings)
        for name in ("max_trials", "target_score", "adaptive", "context_mode"):
            config.pop(name, None)
        fingerprints = {}
        uses_backend = any(not uses_system_voice(item, settings) for item in items)
        backends = {settings.backend} if settings.backend in MODEL_FILES and uses_backend else set()
        if settings.backend != 'edge' or not uses_backend:
            config.pop('edge_voice', None)
            config.pop('edge_rate', None)
        if settings.fallback_model != "off":
            backends.add(settings.fallback_model)
        for backend in sorted(backends):
            replace(settings, backend=backend).validate()
            for name in MODEL_FILES[backend]:
                check_cancel(cancel)
                fingerprints[f"{backend}/{name}"] = digest(Path(getattr(settings, backend)) / name)
        if backends and (voice is None or not voice.is_file()):
            raise ApiError("候选模型需要账号参考 WAV，请选择音色或关闭备用模型。")
        if not backends:
            for name in ("python", "device", "your_tts", "xtts_v2"):
                config.pop(name, None)
            if settings.backend != 'edge' or not uses_backend:
                config.pop('backend', None)
        self.signature = hashlib.sha256(json.dumps({"config": config, "models": fingerprints,
            "voice": digest(voice) if backends and voice is not None else None,
            "system_voice": self.system.voice_id if self.system else None,
            "worker": digest(Path(__file__).with_name("speech_worker.py")) if backends else None,
            "system_code": digest(Path(__file__).with_name("system.py")), "version": VERSION,
            **({'edge_code': digest(Path(__file__).with_name('edge.py'))} if settings.backend == 'edge' and uses_backend else {})},
            sort_keys=True).encode()).hexdigest()

    def specs(self, item):
        if uses_system_voice(item, self.settings):
            specs = [{"id": f"system-rate-{rate}", "kind": "system", "rate": rate,
                      "label": f"重新合成：Windows 原生语速 {rate}"} for rate in (0, -2, 2, -4)]
        elif self.settings.backend == 'edge':
            base = int(self.settings.edge_rate[:-1])
            rates = list(dict.fromkeys(max(-50, min(100, base + delta)) for delta in (-10, 10, -20)))
            specs = [{'id': f'edge-rate-{rate}', 'kind': 'edge', 'rate': f'{rate:+d}%',
                      'label': f'重新合成：Edge TTS · {self.settings.edge_voice} · {rate:+d}%'} for rate in rates]
        else:
            specs = self._model_specs(self.settings.backend)
        if self.settings.context_mode == 'on' and SINGLE_WORD.fullmatch(item.prompt.text.strip()):
            context = {'id': 'context-word-v1', 'kind': 'context', 'cost': 2, 'label': '上下文合成并截取目标词'}
            specs.insert(2, context)
        if self.settings.fallback_model != "off" and (uses_system_voice(item, self.settings) or
                                                      self.settings.fallback_model != self.settings.backend):
            specs.extend(self._model_specs(self.settings.fallback_model))
        return specs

    @staticmethod
    def _model_specs(backend):
        return [{"id": f"{backend}-seed-{seed}-speed-{speed}", "kind": "model", "backend": backend,
                 "seed": seed, "speed": speed, "label": f"重新合成：{MODEL_NAMES[backend]} · 语速 {speed}"}
                for seed, speed in ((17, 1.0), (43, 0.85), (101, 1.15))]

    def generate(self, item, spec, target, cancel):
        if spec['kind'] == 'context':
            return self._context(item, target, cancel)
        if spec['kind'] == 'edge':
            if self.edge is None:
                self.edge = speech.EdgeSpeech(self.settings, self.ffmpeg)
            self.edge.synthesize(synthesis_text(item, self.settings), target, cancel, rate=spec['rate'])
        elif spec["kind"] == "system":
            self.system.synthesize(item.prompt.text.strip(), target, cancel, rate=spec["rate"])
        else:
            backend = spec["backend"]
            if backend in self.unavailable:
                raise ApiError(self.unavailable[backend])
            settings = replace(self.settings, backend=backend, word_mode="model")
            if backend not in self.engines:
                try:
                    self.engines[backend] = speech.SpeechEngine(settings, self.voice,
                        self.data_dir / f"speech-candidate-{backend}.log", cancel)
                except Cancelled:
                    raise
                except ApiError as error:
                    self.unavailable[backend] = str(error)
                    raise
            self.engines[backend].synthesize(synthesis_text(item, settings), target, cancel,
                                             speed=spec["speed"], seed=spec["seed"])

    def _context(self, item, target, cancel):
        if self.client is None or not self.ffmpeg:
            raise ApiError('上下文候选缺少已连接的评测会话。')
        sentence = carrier_sentence(item.prompt.text.strip())
        carrier = ChallengeItem(item.question_id + ':context', 't2', item.group, item.index,
                                'read_sentence', Prompt(sentence, '', ''))
        context_audio = target.with_suffix('.context.wav')
        if uses_system_voice(item, self.settings):
            self.system.synthesize(sentence, context_audio, cancel, rate=0)
        elif self.settings.backend == 'edge':
            self.generate(carrier, {'kind': 'edge', 'rate': self.settings.edge_rate}, context_audio, cancel)
        else:
            self.generate(carrier, {'kind': 'model', 'backend': self.settings.backend, 'speed': 1.0,
                                   'seed': 137}, context_audio, cancel)
        normalized = target.with_suffix('.context-16k.wav')
        pcm, duration = convert_audio(self.ffmpeg, context_audio, normalized)
        check_cancel(cancel)
        xml_path = target.with_suffix('.alignment.xml')
        if self.align_request is None:
            raise ApiError('上下文候选未连接预算账本。')
        xml = self.align_request(carrier, pcm, xml_path, cancel)
        boundaries = extract_word(normalized, target, xml, item.prompt.text)
        return {'context_text': sentence, 'context_audio': str(context_audio.resolve()),
                'alignment_xml': str(xml_path.resolve()), 'boundaries': boundaries}

    def close(self):
        for engine in self.engines.values():
            engine.close()


class CandidatePolicy(AdaptiveAudio):
    continue_on_failure = True

    def __init__(self, root, context, ffmpeg, generator, target=80, trials=3):
        super().__init__(root, context, ffmpeg, target, trials)
        self.generator = generator
        self.generator.ffmpeg = ffmpeg
        self.generator.align_request = self._align_request
        self.ledger_path = self.folder / "candidates-v1.json"
        try:
            self.ledger = json.loads(self.ledger_path.read_text(encoding="utf-8")) if self.ledger_path.exists() else {
                "version": VERSION, "questions": {}}
        except ValueError as error:
            raise ApiError("候选语音账本无法读取，请检查数据文件。") from error
        if self.ledger.get("version") != VERSION or not isinstance(self.ledger.get("questions"), dict):
            raise ApiError("候选语音账本格式异常。")
        self.store = None
        self.current_key = ""

    def _save(self):
        save_json(self.ledger_path, self.ledger)

    def _align_request(self, carrier, pcm, xml_path, cancel):
        key = hashlib.sha256(carrier.reference.encode() + b'\0' + pcm).hexdigest()
        cache = self.ledger.setdefault('alignments', {})
        previous = cache.get(key)
        if previous and time.time() - previous['time'] <= CACHE_SECONDS:
            if previous['status'] != 'accepted':
                raise ApiError('该上下文音频已拒识或结果未确认，未重复送评测。')
            original = Path(previous['xml'])
            if original.is_file() and digest(original) == previous['xml_hash']:
                xml = original.read_bytes()
                xml_path.write_bytes(xml)
                return xml
        cache[key] = {'status': 'evaluating', 'time': time.time()}
        self._save()
        xml = evaluate(self.generator.client.assessment_auth(2), pcm, carrier, cancel)
        xml_path.write_bytes(xml)
        details = evaluation_details(xml)
        cache[key].update(status='accepted' if not details['rejected'] and details['except_info'] == '0' else 'rejected',
                          xml=str(xml_path.resolve()), xml_hash=digest(xml_path))
        self._save()
        return xml

    def _key(self, item, source):
        value = [VERSION, item.question_id, item.category, item.reference,
                 digest(source) if source.is_file() else 'base-unavailable', self.generator.signature]
        return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()

    def _entry(self, item, source):
        key = self._key(item, source)
        if key not in self.ledger["questions"]:
            self.ledger["questions"][key] = {"reference": item.reference, "source": str(source.resolve()),
                "source_hash": digest(source) if source.is_file() else None, "evaluations_used": 0, "generations_used": 0,
                "specs": {}, "outcomes": {}}
        return key, self.ledger["questions"][key]

    def bind(self, store, items, files):
        self.store = store
        # Old rejected inputs remain excluded, but old DSP-only trials do not spend
        # the new generation policy's budget. Migration happens once per hash.
        for item, source in zip(items, files):
            key, entry = self._entry(item, source)
            rows = store.data.get("evaluation_history", {}).get(item.question_id, [])
            if not rows:
                row = store.data.get("evaluations", {}).get(item.question_id)
                rows = [row] if row else []
            for row in rows:
                fingerprint = row.get("audio_sha256")
                if fingerprint and (row.get("usable") is False or row.get("rejected")):
                    entry["outcomes"].setdefault(fingerprint, {"status": "rejected", "imported": True,
                        "path": row.get("audio", ""), "error": str(row.get("except_info", "28676")),
                        "time": time.time()})
        self._save()

    def _cached(self, entry, tried):
        candidates = sorted((row for row in entry["outcomes"].values() if row.get("status") == "accepted"),
                            key=lambda row: row["score"], reverse=True)
        for row in candidates:
            if row.get('profile') == 'context-word-v1' and getattr(self.generator.settings, 'context_mode', 'off') != 'on':
                continue
            name = "verified-" + row["hash"][:16]
            if name in tried or time.time() - row["time"] > CACHE_SECONDS:
                continue
            if row["score"] < self.target and entry["evaluations_used"] < self.trials:
                continue
            path, xml = Path(row["path"]), Path(row["xml"])
            if path.is_file() and xml.is_file() and digest(path) == row["hash"] and digest(xml) == row["xml_hash"]:
                return Recording(self.current_key, name, path, "已验收候选", {**row, "xml_bytes": xml.read_bytes()})
        return None

    def _admit(self, entry, spec, path):
        fingerprint = digest(path)
        previous = entry["outcomes"].get(fingerprint)
        if previous and previous.get("status") != "accepted":
            spec.update(status="duplicate", hash=fingerprint)
            self._save()
            return None
        try:
            metrics = inspect_audio(path)
        except ApiError as error:
            metrics = {"problem": "invalid_format", "reason": str(error)}
        spec.update(hash=fingerprint, quality=metrics)
        if metrics["problem"]:
            code = "28690" if metrics["problem"] == "clipping" else "28673"
            spec["status"] = "local_rejected"
            entry["outcomes"][fingerprint] = {"status": "local_rejected", "error": code,
                "path": str(path.resolve()), "time": time.time(), "quality": metrics}
            self._save()
            return None
        spec["status"] = "prepared"
        self._save()
        return Recording(self.current_key, spec["id"], path, spec["label"])

    def prepare(self, item, source, tried, cancel):
        check_cancel(cancel)
        key, entry = self._entry(item, source)
        self.current_key = key
        cached = self._cached(entry, tried)
        if cached:
            return cached
        if entry["evaluations_used"] >= self.trials:
            return None
        for spec in entry["specs"].values():
            if spec["status"] == "prepared" and spec["id"] not in tried:
                path = Path(spec["path"])
                if path.is_file() and digest(path) == spec.get("hash"):
                    return Recording(key, spec["id"], path, spec["label"])
        if "base" not in entry["specs"]:
            spec = {"id": "base", "label": "原音频", "status": "preparing", "path": str(source.resolve())}
            entry["specs"]["base"] = spec
            if source.is_file():
                recording = self._admit(entry, spec, source)
                if recording:
                    return recording
            else:
                spec['status'] = 'base_generation_failed'
                self._save()
        outcomes = list(entry["outcomes"].values())
        last = max(outcomes, key=lambda row: row.get("time", 0), default={})
        specs = []
        if last.get("error") in {"28673", "28689", "28690"}:
            profile = "quieter" if last["error"] == "28690" else "padding"
            specs.append({"id": "repair-" + profile, "kind": "repair", "profile": profile,
                          "label": "录音修正：" + ("降低音量" if profile == "quieter" else "补充静音")})
        specs.extend(self.generator.specs(item))
        for template in specs:
            check_cancel(cancel)
            if template.get('cost', 1) > self.trials - entry['evaluations_used']:
                continue
            if template["id"] in entry["specs"] or entry["generations_used"] >= self.trials * 2:
                continue
            path = self.folder / f"{key}-{template['id']}.wav"
            spec = {**template, "path": str(path.resolve()), "status": "generating"}
            entry["specs"][spec["id"]] = spec
            entry["generations_used"] += 1
            self._save()
            temporary = path.with_suffix(".pending.wav")
            try:
                if spec['kind'] == 'context':
                    # The carrier-sentence assessment costs one slot; reserve it
                    # before any request. The cropped-word assessment costs another.
                    entry['evaluations_used'] += 1
                    spec['alignment_reserved'] = time.time()
                    self._save()
                if spec["kind"] == "repair":
                    self._variant(source, path, spec["profile"], cancel)
                else:
                    metadata = self.generator.generate(item, spec, temporary, cancel)
                    if metadata:
                        spec.update(metadata)
                    check_cancel(cancel)
                    temporary.replace(path)
                recording = self._admit(entry, spec, path)
                if recording:
                    return recording
            except Cancelled:
                spec["status"] = "cancelled"
                self._save()
                raise
            except (ApiError, OSError) as error:
                spec.update(status="generation_error", error=str(error)[:240])
                self._save()
            finally:
                temporary.unlink(missing_ok=True)
        return None

    def reserve(self, recording):
        if recording.cached:
            return
        entry = self.ledger["questions"][recording.key]
        spec = entry["specs"][recording.profile]
        if entry["evaluations_used"] >= self.trials or spec["status"] != "prepared":
            raise ApiError("本题候选评测预算已用尽，或该候选已送评测。")
        entry["evaluations_used"] += 1
        spec.update(status="evaluating", started=time.time())
        self._save()

    def observe_artifact(self, recording, details, result, artifact, store):
        if recording.cached:
            return
        entry = self.ledger["questions"][recording.key]
        spec = entry["specs"][recording.profile]
        path = Path(recording.path)
        fingerprint = digest(path)
        xml = (store.folder / artifact["xml"]).resolve()
        status = "accepted" if artifact["usable"] else "rejected"
        spec["status"] = status
        entry["outcomes"][fingerprint] = {"status": status, "hash": fingerprint, "path": str(path.resolve()),
            "error": str(details["except_info"]), "score": result["score"], "time": time.time(),
            "xml": str(xml), "xml_hash": digest(xml), "profile": recording.profile, "result": dict(result),
            "normalized_sha256": artifact.get("normalized_sha256")}
        self._save()

    def describe(self, recording):
        profile = recording.cached['profile'] if recording.cached else recording.profile
        return self.ledger['questions'][recording.key]['specs'].get(profile, {})

    def invalidate_cache(self, recording):
        self.ledger["questions"][recording.key]["outcomes"][recording.cached["hash"]]["status"] = "stale"
        self._save()

    def failure_reason(self):
        entry = self.ledger["questions"].get(self.current_key, {})
        return (f"候选预算已用 {entry.get('evaluations_used', 0)}/{self.trials}；已拒识音频不会重复送评测。"
                "可在模型设置中增加预算或调整待复核题的生成策略。")
