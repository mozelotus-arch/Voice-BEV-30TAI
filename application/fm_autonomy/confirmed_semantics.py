"""Pure offline contract guard. Returns data only; contains no actuator or transport API."""
from decimal import Decimal, InvalidOperation
import re

WAKE='小雅小雅'
DIGITS='零一二三四五六七八九'


def chinese_integer(n):
    if n==0: return '零'
    result=''; zero=False
    for divisor,unit in ((1000,'千'),(100,'百'),(10,'十'),(1,'')):
        digit,n=divmod(n,divisor)
        if digit:
            if zero: result+='零'
            result+=DIGITS[digit]+unit; zero=False
        elif result and n: zero=True
    return result[1:] if result.startswith('一十') else result


CN={chinese_integer(i):i for i in range(10000)}


def number(text):
    if re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?',text):
        value=Decimal(text)
    else:
        parts=text.split('点')
        if len(parts)>2 or parts[0] not in CN: raise ValueError('Ambiguous numeral')
        value=Decimal(CN[parts[0]])
        if len(parts)==2:
            if not parts[1] or any(c not in DIGITS for c in parts[1]): raise ValueError('Invalid fraction')
            value+=Decimal('0.'+''.join(str(DIGITS.index(c)) for c in parts[1]))
    if not value.is_finite() or value<=0: raise ValueError('Nonpositive quantity')
    return value


def lexical(text):
    """Recognize only approved literal forms; do not infer synonyms or a default distance."""
    text=text.strip().rstrip('。！!')
    if text=='停车': return dict(intent='stop',clear_actions=True,exit_automatic=True)
    if text=='开始自动避障': return dict(intent='auto_start')
    m=re.fullmatch(r'(前进|后退|左转|右转)(.*)',text)
    if not m: return dict(intent='unknown',reason='outside_confirmed_forms')
    direction,tail=m.groups(); tail=tail.strip()
    intent='move' if direction in ('前进','后退') else 'turn'
    if not tail: return dict(intent=intent,direction=direction,needs_clarification=True,missing=['quantity','unit'])
    match=re.fullmatch(r'([0-9.零一二三四五六七八九十百千点]+)\s*(厘米|米|度)',tail)
    if not match:
        # A valid bare positive number is incomplete, never an executable default.
        try: number(tail)
        except (ValueError,InvalidOperation): return dict(intent='unknown',reason='invalid_quantity_or_unit')
        return dict(intent=intent,direction=direction,needs_clarification=True,missing=['unit'])
    quantity,unit=match.groups()
    if (intent=='turn') != (unit=='度'): return dict(intent='unknown',reason='unit_mismatch')
    try: value=number(quantity)
    except (ValueError,InvalidOperation): return dict(intent='unknown',reason='invalid_quantity')
    normalized=value/100 if unit=='厘米' else value
    return dict(intent=intent,direction=direction,quantity=quantity,unit=unit,
                normalized_value=str(normalized),normalized_unit='m' if intent=='move' else 'deg')


def guard(text,*,awake=False,motion_active=False,interaction_stopped=False):
    if not isinstance(text,str) or len(text)>120: return dict(kind='rejected',executable=False)
    if interaction_stopped: return dict(kind='interaction_stopped',executable=False)
    text=text.strip()
    woke=text.startswith(WAKE)
    if woke:
        text=text[len(WAKE):].lstrip(' ，,。')
        if not text: return dict(kind='wake',executable=False)
    parsed=lexical(text)
    bypass=motion_active and parsed['intent']=='stop'
    if not(awake or woke or bypass): return dict(kind='sleeping',executable=False)
    if parsed['intent']=='unknown': return dict(kind='rejected',executable=False,semantic=parsed)
    if parsed.get('needs_clarification'): return dict(kind='clarify',executable=False,semantic=parsed)
    return dict(kind='candidate',executable=False,semantic=parsed,
                execution_block='Offline contract only; controller limits and feedback not validated',stop_bypasses_wake=bypass)
