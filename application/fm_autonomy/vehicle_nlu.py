"""Strict agreement between the confirmed grammar and the new 5/7 BERT heads."""
from .confirmed_semantics import WAKE, lexical
import re

SCHEMA = 'fm-vehicle-nlu-v1'
INTENTS = {'move': 0, 'turn': 1, 'auto_start': 2, 'stop': 3, 'unknown': 4}
BIO = {'O': 0, 'B-direction': 1, 'I-direction': 2, 'B-quantity': 3,
       'I-quantity': 4, 'B-unit': 5, 'I-unit': 6}


def command_body(text):
    if not isinstance(text, str) or len(text) > 120:
        raise ValueError('invalid_command_text')
    text = text.strip()
    if text.startswith(WAKE):
        text = text[len(WAKE):].lstrip(' ，,。')
    # Same terminal punctuation already accepted by lexical(); normalize BEFORE
    # tokenization, retaining source_text separately. Never repair model BIO tags.
    text = text.rstrip('。！!')
    # Exact surface-language adapter; the pinned BERT contract still receives
    # its original direction/quantity/unit vocabulary. Never repair ASR tokens.
    aliases = {'向前走':'前进', '向后走':'后退', '向左转':'左转', '向右转':'右转'}
    match = re.fullmatch(r'(向前走|向后走|向左转|向右转)([0-9.零一二三四五六七八九十百千点]+\s*(?:厘米|米|度))', text)
    if match:
        canonical = aliases[match[1]] + match[2]
        if lexical(canonical)['intent'] in ('move', 'turn'):
            return canonical
    return text


def agree(text, prediction):
    """BIO offsets must cover exactly the grammar's slots; confidence is not authority."""
    semantic = lexical(text)
    if not isinstance(prediction, dict) or prediction.get('text') != text:
        raise ValueError('nlu_text_mismatch')
    if prediction.get('intent') != semantic['intent']:
        raise ValueError('nlu_intent_mismatch')
    spans = {}
    active = None
    previous_end = 0
    for token in prediction.get('token_predictions', []):
        start, end, label = token.get('start'), token.get('end'), token.get('label')
        if (type(start) is not int or type(end) is not int or
                not 0 <= previous_end <= start < end <= len(text) or label not in BIO):
            raise ValueError('invalid_nlu_token')
        previous_end = end
        if label == 'O':
            active = None
            continue
        prefix, slot = label.split('-', 1)
        if prefix == 'B':
            if slot in spans:
                raise ValueError('duplicate_nlu_slot')
            spans[slot] = [start, end]
            active = slot
        elif active != slot or spans[slot][1] != start:
            raise ValueError('invalid_nlu_bio_continuation')
        else:
            spans[slot][1] = end
    slots = {key: text[a:b] for key, (a, b) in spans.items()}
    expected = {key: semantic[key] for key in ('direction', 'quantity', 'unit') if key in semantic}
    if slots != expected:
        raise ValueError('nlu_slot_mismatch')
    return semantic


class VehicleNLU:
    def __init__(self, adapter):
        if adapter.interface['intent_to_id'] != INTENTS or adapter.interface['bio_to_id'] != BIO:
            raise ValueError('vehicle_bert_5_intent_7_bio_contract_required')
        self.adapter = adapter

    def understand(self, text):
        body = command_body(text)
        semantic = lexical(body)
        # Safety stop and incomplete/invalid grammar never wait for neural inference.
        if not body or semantic['intent'] in ('stop', 'unknown') or semantic.get('needs_clarification'):
            return dict(schema=SCHEMA, source_text=text, body=body, prediction=None)
        prediction = self.adapter.interpret(body)
        # Disagreement is a rejected command, not a failed native runtime.
        # The parent rechecks this evidence before considering any control action.
        return dict(schema=SCHEMA, source_text=text, body=body, prediction=prediction,
                    normalization='exact_direction_aliases_v1')


def validate_envelope(text, envelope):
    body = command_body(text)
    if (not isinstance(envelope, dict) or envelope.get('schema') != SCHEMA or
            envelope.get('source_text') != text or envelope.get('body') != body):
        raise ValueError('vehicle_nlu_envelope_mismatch')
    semantic = lexical(body)
    if body and semantic['intent'] not in ('stop', 'unknown') and not semantic.get('needs_clarification'):
        agree(body, envelope.get('prediction'))
    return semantic
