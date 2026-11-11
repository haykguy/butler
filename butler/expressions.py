"""Opt-in expressions through the local Photon sidecar, with strict targeting."""
import json
import os
from pathlib import Path
import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _sidecar(path, body):
    """Call the local Photon sidecar using its runtime discovery record."""
    home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    record = json.loads((home / "runtime" / "photon-sidecar.json").read_text())
    port, token = record["port"], record["token"]
    if type(port) is not int or not 1 <= port <= 65535 or not isinstance(token, str) or not token:
        raise ValueError("invalid Photon sidecar record")
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json",
        "X-Hermes-Sidecar-Token": token}, method="POST")
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=60) as response:
        return json.loads(response.read())


_EFFECTS = frozenset(('slam', 'loud', 'gentle', 'invisible', 'balloons', 'confetti',
                      'lasers', 'fireworks', 'celebration', 'echo', 'spotlight',
                      'heart', 'sparkles'))


def _session():
    from gateway.session_context import get_session_env
    return (get_session_env('HERMES_SESSION_PLATFORM', ''),
            get_session_env('HERMES_SESSION_CHAT_ID', ''))


def register_expression_tools(ctx, session_provider=None):
    """Register one tool. session_provider is injectable for isolated tests.

    Only the active photon chat is eligible. Reactions and replies are disabled
    because the available sidecar does not reliably validate message membership.
    """
    session = session_provider or _session

    def handle(args, **kwargs):
        def result(**value):
            return json.dumps(value, ensure_ascii=False)

        if not isinstance(args, dict):
            return result(ok=False, error='arguments must be an object')
        action = args.get('action')
        if action not in ('capabilities', 'react', 'reply', 'effect'):
            return result(ok=False, error='unsupported action')
        try:
            platform, chat = session()
        except Exception:
            return result(ok=False, error='session context unavailable')
        if not isinstance(platform, str) or not isinstance(chat, str) or not platform or not chat or chat != chat.strip():
            return result(ok=False, error='active platform and chat required')
        if action == 'capabilities':
            return result(ok=True, platform=platform, chat_id=chat,
                          capabilities={'react': False, 'reply': False,
                                        'effect': platform == 'photon'},
                          note='Reactions and replies are disabled until message membership can be verified. Effects require a compatible running Photon sidecar.')
        if platform != 'photon':
            return result(ok=False, error='unsupported channel or missing adapter bridge', platform=platform, action=action)
        requested_chat = args.get('chat_id')
        if not isinstance(requested_chat, str) or not requested_chat or requested_chat != chat:
            return result(ok=False, error='chat_id must exactly match the active chat')
        payload = {}
        if action in ('react', 'reply'):
            mid = args.get('message_id')
            if not isinstance(mid, str) or not mid.strip() or mid != mid.strip():
                return result(ok=False, error='message_id must be a non-empty unpadded string')
            payload['message_id'] = mid
        if action == 'react':
            remove = args.get('remove', False)
            if not isinstance(remove, bool):
                return result(ok=False, error='remove must be boolean')
            emoji = args.get('emoji')
            if not remove and (not isinstance(emoji, str) or not emoji.strip()):
                return result(ok=False, error='emoji is required when adding a reaction')
            payload['remove'] = remove
            if not remove:
                payload['emoji'] = emoji
        else:
            text = args.get('text')
            if not isinstance(text, str) or not text.strip():
                return result(ok=False, error='text must be non-empty')
            payload['text'] = text
            effect = args.get('effect')
            if action == 'effect' or effect is not None:
                if not isinstance(effect, str) or effect not in _EFFECTS:
                    return result(ok=False, error='unknown or missing iMessage effect')
                payload['effect'] = effect
        try:
            if action != 'effect':
                return result(ok=False, error='message membership cannot be verified safely')
            raw = _sidecar('/send-effect', {'spaceId': chat, **payload})
            response = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return result(ok=False, error='Photon sidecar unavailable or failed')
        if not isinstance(response, dict) or not isinstance(response.get('ok'), bool):
            return result(ok=False, error='invalid response from Photon sidecar')
        return result(**response)

    description = ('Inspect channel capabilities or send effects through the local Photon sidecar. '
                   'Provide exact active chat_id and explicit message_id for reactions and replies. '
                   'Telegram operations currently require an adapter bridge; no fallback send is performed.')
    schema = {'name': 'butler_expression', 'description': description,
              'parameters': {'type': 'object', 'properties': {
                  'action': {'type': 'string', 'enum': ['capabilities', 'react', 'reply', 'effect']},
                  'chat_id': {'type': 'string'}, 'message_id': {'type': 'string'},
                  'emoji': {'type': 'string'}, 'remove': {'type': 'boolean'},
                  'text': {'type': 'string'}, 'effect': {'type': 'string'}},
                  'required': ['action'], 'additionalProperties': False}}
    ctx.register_tool(name='butler_expression', toolset='butler', schema=schema,
                      handler=handle, description=description)
