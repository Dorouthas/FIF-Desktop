import base64
import hashlib
import json
import os
import shutil
import tempfile
import uuid
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


FORMAT = "fif-accounts-v1"


@dataclass
class Voice:
    id: str
    name: str
    path: str
    sha256: str


@dataclass
class Account:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = ""
    username: str = ""
    password: str = field(default="", repr=False)
    user_id: str = ""
    default_voice: str = ""
    voices: list[Voice] = field(default_factory=list)


class VaultError(Exception):
    pass


class Vault:
    def __init__(self, path: Path, password: str):
        self.path = path
        self.accounts: list[Account] = []
        self.active = ""
        try:
            if path.exists():
                envelope = json.loads(path.read_text(encoding="utf-8"))
                if envelope["format"] != FORMAT:
                    raise VaultError("账号文件的格式不受支持。")
                self.salt = base64.b64decode(envelope["salt"], validate=True)
                nonce = base64.b64decode(envelope["nonce"], validate=True)
                ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
                if len(self.salt) != 16 or len(nonce) != 12:
                    raise ValueError("Invalid salt or nonce")
                self.cipher = self._cipher(password)
                data = json.loads(self.cipher.decrypt(nonce, ciphertext, FORMAT.encode()))
                self.active = data["active"]
                for record in data["accounts"]:
                    voices = [Voice(**voice) for voice in record.pop("voices")]
                    self.accounts.append(Account(**record, voices=voices))
            else:
                self.salt = os.urandom(16)
                self.cipher = self._cipher(password)
                self.save([], "")
        except InvalidTag as exc:
            raise VaultError("主密码不正确，或账号文件已损坏。") from exc
        except (ValueError, TypeError, KeyError) as exc:
            raise VaultError("账号文件已损坏，无法读取。") from exc

    def _cipher(self, password: str) -> AESGCM:
        key = Scrypt(salt=self.salt, length=32, n=2**16, r=8, p=1).derive(password.encode())
        return AESGCM(key)

    def save(self, accounts: list[Account], active: str):
        raw = json.dumps({"active": active, "accounts": [asdict(a) for a in accounts]},
                         ensure_ascii=False).encode()
        nonce = os.urandom(12)
        ciphertext = self.cipher.encrypt(nonce, raw, FORMAT.encode())
        envelope = {"format": FORMAT}
        for key, value in (("salt", self.salt), ("nonce", nonce), ("ciphertext", ciphertext)):
            envelope[key] = base64.b64encode(value).decode("ascii")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                         suffix=".tmp", delete=False) as temporary:
            temp_path = Path(temporary.name)
            try:
                json.dump(envelope, temporary, indent=2)
                temporary.flush()
                os.fsync(temporary.fileno())
            except BaseException:
                temporary.close()
                temp_path.unlink(missing_ok=True)
                raise
        try:
            temp_path.replace(self.path)
        finally:
            temp_path.unlink(missing_ok=True)
        self.accounts = accounts
        self.active = active

    def voice_path(self, voice: Voice) -> Path:
        library = (self.path.parent / "voices").resolve()
        path = (self.path.parent / voice.path).resolve()
        if path.parent != library or path.name != f"{voice.id}.wav":
            raise VaultError("音色文件路径无效。")
        return path

    def import_voice(self, source: Path) -> Voice:
        with wave.open(str(source), "rb") as audio:
            if not audio.getnframes():
                raise VaultError("WAV 文件没有音频数据。")
        voice_id = uuid.uuid4().hex
        voice = Voice(voice_id, source.stem, f"voices/{voice_id}.wav", "")
        path = self.voice_path(voice)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(source, path)
            with path.open("rb") as file:
                voice.sha256 = hashlib.file_digest(file, "sha256").hexdigest()
        except OSError:
            path.unlink(missing_ok=True)
            raise
        return voice
