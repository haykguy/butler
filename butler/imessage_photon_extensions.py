"""Controlled adapter for optional, nonstandard Photon /x routes.

These routes are not provided by the standard Hermes sidecar. The caller must
supply the current session and an authenticated transport and must not present
this adapter as available merely because this module exists. No fallback send
or retry is attempted: a timeout may mean the operation already happened.
"""
from .imessage_media import snapshot


def invoke_extension(action, args, *, session, sidecar):
    """Perform an explicitly confirmed operation in the current Photon chat."""
    if action not in ('contact_card', 'background') or not isinstance(args, dict):
        return {'ok': False, 'error': 'unsupported action or invalid arguments'}
    if not isinstance(session, (tuple, list)) or len(session) != 2:
        return {'ok': False, 'error': 'active Photon chat required'}
    platform, chat = session
    if platform != 'photon' or not isinstance(chat, str) or not chat or chat != chat.strip():
        return {'ok': False, 'error': 'active Photon chat required'}
    if args.get('confirm') is not True:
        return {'ok': False, 'error': 'explicit confirmation required'}
    if 'chat_id' in args and args['chat_id'] != chat:
        return {'ok': False, 'error': 'chat_id must match the active chat'}
    allowed = {'confirm', 'chat_id'} | ({'path', 'clear'} if action == 'background' else set())
    if set(args) - allowed:
        return {'ok': False, 'error': 'unexpected arguments'}
    def call(path, body):
        try:
            return sidecar(path, body)
        except Exception as exc:
            raise RuntimeError('sidecar call failed') from exc

    try:
        if action == 'contact_card':
            response = call('/x/contact-card', {'spaceId': chat})
        else:
            clear = args.get('clear', False)
            if not isinstance(clear, bool) or (clear and 'path' in args) or (not clear and not args.get('path')):
                return {'ok': False, 'error': 'provide either clear=true or an image path'}
            if clear:
                response = call('/x/background', {'spaceId': chat, 'clear': True})
            else:
                with snapshot(args['path'], 'image') as staged:
                    response = call('/x/background', {'spaceId': chat, 'path': str(staged)})
        if not isinstance(response, dict) or not isinstance(response.get('ok'), bool):
            return {'ok': False, 'error': 'invalid Photon sidecar response'}
        return {'ok': response['ok']} if response['ok'] else {'ok': False, 'error': 'Photon sidecar rejected operation'}
    except ValueError as exc:
        return {'ok': False, 'error': str(exc)}
    except Exception:
        return {'ok': False, 'error': 'Photon extension unavailable or operation failed; outcome may be unknown'}
