"""Explicit local registrations only. This release intentionally registers no actions."""
import copy
import json
import math
import re
from provider import json_object


def validate(value, schema, path='arguments'):
    kind = schema.get('type')
    types = kind if isinstance(kind, list) else [kind]
    matches = {'object': isinstance(value, dict), 'array': isinstance(value, list),
               'string': isinstance(value, str), 'boolean': type(value) is bool,
               'integer': type(value) is int, 'number': type(value) in (int, float), 'null': value is None}
    if not any(matches.get(k, False) for k in types):
        raise ValueError(path + ' 类型不匹配。')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(path + ' 不在允许范围。')
    if isinstance(value, dict):
        properties = schema.get('properties', {})
        if set(value) - set(properties) or set(schema.get('required', [])) - set(value):
            raise ValueError(path + ' 字段不匹配。')
        for name, item in value.items():
            validate(item, properties[name], path + '.' + name)
    if isinstance(value, list):
        if len(value) > schema.get('maxItems', 32) or len(value) < schema.get('minItems', 0):
            raise ValueError(path + ' 长度超限。')
        for item in value:
            validate(item, schema['items'], path + '[]')
    if isinstance(value, str):
        if len(value) > schema.get('maxLength', 2000) or len(value) < schema.get('minLength', 0):
            raise ValueError(path + ' 长度超限。')
        if 'pattern' in schema and not re.fullmatch(schema['pattern'], value):
            raise ValueError(path + ' 格式不匹配。')
    if type(value) in (int, float):
        if not math.isfinite(value) or value < schema.get('minimum', -math.inf) or value > schema.get('maximum', math.inf):
            raise ValueError(path + ' 数值超限。')


def check_schema(schema):
    supported = {'type', 'description', 'enum', 'properties', 'required', 'additionalProperties',
                 'items', 'minItems', 'maxItems', 'minLength', 'maxLength', 'pattern', 'minimum', 'maximum'}
    if set(schema) - supported:
        raise ValueError('Unsupported schema keyword')
    kind = schema.get('type')
    kinds = kind if isinstance(kind, list) else [kind]
    if not kinds or set(kinds) - {'object', 'array', 'string', 'integer', 'number', 'boolean', 'null'}:
        raise ValueError('Schema type required')
    if 'object' in kinds:
        props = schema.get('properties', {})
        if schema.get('additionalProperties') is not False or set(schema.get('required', [])) != set(props):
            raise ValueError('Strict object schemas require all properties and no extras')
        for child in props.values():
            check_schema(child)
    if 'array' in kinds:
        check_schema(schema['items'])


class Registry:
    def __init__(self):
        self._entries = {}

    def register(self, name, description, schema, handler, *, mutating=False):
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', name) or name in self._entries or not callable(handler):
            raise ValueError('Invalid capability registration')
        check_schema(schema)
        self._entries[name] = (description, copy.deepcopy(schema), handler, mutating)

    def tools(self):
        return [dict(type='function', function=dict(name=name, description=entry[0],
                parameters=copy.deepcopy(entry[1]), strict=True)) for name, entry in self._entries.items()]

    def dispatch(self, call, guard):
        guard()
        function = call.get('function', {}) if isinstance(call, dict) else {}
        name = function.get('name', '') if isinstance(function, dict) else ''
        receipt = dict(name=name if isinstance(name, str) else '', status='rejected', hardware_executed=False)
        if name not in self._entries:
            return dict(receipt, reason='unsupported_capability', message='此动作尚未接入，没有执行。')
        try:
            arguments = json_object(function['arguments'])
            validate(arguments, self._entries[name][1])
        except (ValueError, KeyError, TypeError, RecursionError):
            return dict(receipt, reason='invalid_arguments', message='动作参数不符合约定，没有执行。')
        guard()
        if self._entries[name][3]:
            attempted = getattr(guard, 'attempted_actions', None)
            if attempted is None:
                return dict(receipt, reason='current_user_turn_required', message='需要当前用户明确请求。')
            # The provider may repeat an action with a new tool-call ID. No
            # mutation is retried in the same turn, even after a lost receipt.
            if name in attempted:
                return dict(receipt, reason='action_already_attempted', message='本轮已请求此操作，没有重复发送。')
            attempted.add(name)
        result = self._entries[name][2](arguments, guard)
        guard()
        if not isinstance(result, dict) or type(result.get('hardware_executed')) is not bool:
            raise RuntimeError('Capability handler must return an explicit hardware receipt')
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False)) > 4000:
            raise RuntimeError('Capability receipt too large')
        return dict(result, name=name)


def production_registry():
    # Add a reviewed schema AND its receipt-producing handler here after discussion.
    # Model text, URLs, shell snippets and arbitrary JSON are never dispatched.
    return Registry()
