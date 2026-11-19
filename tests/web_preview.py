"""Isolated browser-QA fixture. All FIF Oral/TTS/evaluation/upload endpoints are simulated.

Run explicitly with python tests/web_preview.py. No production account data is opened.
"""
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch


import uvicorn
from fif_oral.web.app import create_app
from fif_oral.storage import Vault, Account
from test_speech import FakeEngine, fixture_settings, write_wav
from test_web_api import WebPlatform, asdict_settings
from test_challenge import level, REJECTED_WORD_XML, XML
from fif_oral.audio.speech import save_json


def main():
    with tempfile.TemporaryDirectory(prefix='fif-web-preview-') as folder:
        root = Path(folder)
        vault = Vault(root / 'accounts.json', 'preview-master')
        audio = root / 'source.wav'
        write_wav(audio)
        voice = vault.import_voice(audio)
        voice.name = '示例参考音色'
        account = Account(id='preview-account', name='界面演示账号', username='preview-student',
                          password='preview-password', default_voice=voice.id, voices=[voice])
        vault.save([account], account.id)
        fixture_settings(root).save(root)
        import shutil
        save_json(root / 'web.json', {'ffmpeg': shutil.which('ffmpeg') or ''})
        platform = WebPlatform()
        platform.children['a'].data = level(('Hello world.',))
        platform.children['c'].data = level(('Practice makes progress.',))
        old_tasks = platform.tasks
        platform.tasks = lambda status: [{**row, 'taskName': '口语任务 · 界面演示数据'} for row in old_tasks(status)]
        app = create_app(root)

        def evaluate(auth, pcm, item, cancel):
            time.sleep(.6)
            return REJECTED_WORD_XML if item.showtype == 't1' else XML

        with patch('fif_oral.web.app.LoginFlow.sign_in', return_value=platform), patch('fif_oral.audio.speech.SpeechEngine', FakeEngine), \
                patch('fif_oral.challenge.evaluate', side_effect=evaluate), \
                patch('fif_oral.challenge.upload', side_effect=lambda client, profile, item, wav, xml, stamp, task: f'{stamp}/{item.index}.mp3'):
            print('Isolated QA preview: http://127.0.0.1:8765 (all task data is simulated)', flush=True)
            uvicorn.run(app, host='127.0.0.1', port=8765, proxy_headers=False, access_log=False)


if __name__ == '__main__':
    main()
