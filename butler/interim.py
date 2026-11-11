"""Fail-closed validation of a judge's interim-send decision.

The optional post_judged_interim path is for plugin-owned status posts only;
no caller is currently registered. It cannot intercept gateway commentary or
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


def post_judged_interim(ctx, judge):
    """Post only a validated decision via the synchronous plugin SDK.

    Requires an active SDK context with post_message support. Judge exceptions
    are discarded without logging their potentially sensitive contents. Posting
    errors propagate, rather than being reported as successful delivery.
    """
    try:
        output = judge()
    except Exception:
        return None
    message = interim_message(output)
    if message is None:
        return None
    return ctx.post_message(message)
