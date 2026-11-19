"""Extract one target word from an assessed carrier sentence."""
import wave

from fif_oral.audio.assessment import evaluation_details, evaluation_node
from fif_oral.api.client import ApiError


def canonical(text):
    return text.strip(' \t\r\n.,!?;:"').casefold()


def carrier_sentence(word):
    template = "Please pronounce {word} clearly."
    if canonical(word) in {'please', 'pronounce', 'clearly'}:
        template = "They said {word} yesterday."
    return template.format(word=word)


def extract_word(source, target, xml, word):
    details = evaluation_details(xml)
    if details['rejected'] or details['except_info'] != '0':
        raise ApiError("上下文对齐的评测结果异常，未截取目标词。")
    result = evaluation_node(xml)
    matches = [node for node in result.iter('word') if canonical(node.get('content', '')) == canonical(word)
               and node.get('dp_message', '0') == '0']
    if len(matches) != 1:
        raise ApiError("上下文未得到唯一的目标词边界，未生成候选。")
    try:
        begin, end = int(matches[0].get('beg_pos', '-1')), int(matches[0].get('end_pos', '-1'))
    except ValueError as error:
        raise ApiError("上下文词边界格式异常。") from error
    with wave.open(str(source), 'rb') as original:
        rate = original.getframerate()
        if original.getnchannels() != 1 or original.getsampwidth() != 2 or not 0 <= begin < end:
            raise ApiError("上下文音频或词边界不符合要求。")
        # iFLYTEK boundaries are 10 ms frames. Keep a small consonant margin;
        # do not cut solely by energy, which could remove unvoiced stops.
        start = max(0, round((begin / 100 - 0.04) * rate))
        stop = min(original.getnframes(), round((end / 100 + 0.04) * rate))
        if start >= stop or end / 100 > original.getnframes() / rate + 0.1:
            raise ApiError("上下文词边界超出音频范围。")
        original.setpos(start)
        audio = original.readframes(stop - start)
        with wave.open(str(target), 'wb') as output:
            output.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
            output.writeframes(b'\0' * (round(rate * 0.15) * 2))
            output.writeframes(audio)
            output.writeframes(b'\0' * (round(rate * 0.25) * 2))
    return {'begin_frame': begin, 'end_frame': end, 'frame_seconds': 0.01,
            'margin_seconds': 0.04, 'word': word}
