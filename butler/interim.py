"""Fail-closed validation of a judge's interim-send decision.

No caller is currently registered. It cannot intercept gateway commentary or
other plugins. Valid structure is not a guarantee of appropriate content.
"""

import json


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def interim_message(judge_output):
    """Return approved message text, or None for any invalid/negative decision.

    Input is a JSON string containing exactly {send: bool, message: string}.
    The caller must pass failed judge calls as None, not exception text.
    """
    if not isinstance(judge_output, str):
        return None
    try:
        decision = json.loads(judge_output, object_pairs_hook=_unique_object)
    except (ValueError, TypeError, RecursionError):
        return None
    if type(decision) is not dict or set(decision) != {"send", "message"}:
        return None
    if type(decision["send"]) is not bool or type(decision["message"]) is not str:
        return None
    if not decision["send"] or not decision["message"].strip():
        return None
    return decision["message"]
