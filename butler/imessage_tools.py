"""iMessage media and expression helpers for the active Photon chat.

Butler never reads Hermes secret stores (such as the Photon sidecar token) and
never calls a platform sidecar directly. Images and voice notes are staged in
the Hermes media cache through the public ``gateway.platforms.base`` cache
helpers and returned as ``MEDIA:`` directives, so the model includes them in
its normal reply and Hermes' Photon adapter delivers them as attachments.
Effects and polls have no public plugin route yet and fail closed.
Message-targeted actions remain disabled until the backend proves membership.
"""
import json


from .expressions import _session, _EFFECTS, EFFECTS_UNAVAILABLE

POLLS_UNAVAILABLE = ('iMessage polls are unavailable: Hermes has no public plugin route for them yet '
                     '(pending a send_poll verb on ctx.platform_actions). Ask the question as a normal reply.')


def _cache_media(kind, data, ext):
    """Write media bytes into the Hermes media cache via the public helpers."""
    from gateway.platforms.base import cache_audio_from_bytes, cache_image_from_bytes
    if kind == 'image':
        return cache_image_from_bytes(data, ext)
    return cache_audio_from_bytes(data, ext)


def _media_reply(path, *, voice=False, caption=None):
    directive = f'MEDIA:{path}'
    lines = (['[[audio_as_voice]]'] if voice else []) + [directive]
    payload = {
        'ok': True,
        'delivery': 'reply_attachment',
        'media_path': str(path),
        'reply_directive': '\n'.join(lines),
        'instruction': ('Not sent yet. To deliver it, include reply_directive verbatim on its own line(s) '
                        'in your reply to this chat; Hermes attaches the file.'),
    }
    if caption:
        payload['caption'] = caption
    return payload

_ACTIONS = ('capabilities', 'react', 'reply', 'edit', 'unsend', 'poll', 'effect',
            'voice_note', 'send_image', 'card', 'contact_card', 'background',
            'thread', 'image_edit')


def register_imessage_tools(ctx, *, session_provider=None, media_cache=None):
    session_provider = session_provider or _session
    media_cache = media_cache or _cache_media

    def handle(args, **kwargs):
        def result(**values):
            return json.dumps(values, ensure_ascii=False)

        if not isinstance(args, dict) or args.get('action') not in _ACTIONS:
            return result(ok=False, error='unsupported action or invalid arguments')
        action = args['action']
        try:
            platform, chat = session_provider()
        except Exception:
            return result(ok=False, error='session context unavailable')
        if platform != 'photon' or not isinstance(chat, str) or not chat or chat != chat.strip():
            return result(ok=False, error='active Photon chat required')
        if 'chat_id' in args and args['chat_id'] != chat:
            return result(ok=False, error='chat_id must match the active chat')
        if action == 'capabilities':
            return result(ok=True, membership_verified=False,
                          delivery='reply_attachment',
                          note='send_image and voice_note stage the file and return a MEDIA: directive to include in your reply. Media inputs are limited to Hermes image_cache and plugin-data/butler/media; voice accepts audio_path or text via configured Hermes TTS. Effects, polls, message targets, thread history, cards, backgrounds and contact cards are unavailable.')
        if action in ('react', 'reply', 'edit', 'unsend') or (action == 'voice_note' and args.get('reply_to_message_id')):
            return result(ok=False, error='message membership cannot be verified safely')
        if action == 'image_edit':
            from contextlib import ExitStack
            from .imessage_media import roots, snapshot
            from .imessage_imaging import edit_image
            try:
                operations = args.get('operations')
                if not isinstance(operations, list):
                    raise ValueError('operations must be a list')
                cache = roots()[0]
                edited = cache / 'edited'
                if cache.is_symlink() or edited.is_symlink():
                    raise ValueError('image cache cannot be a symlink')
                with ExitStack() as stack:
                    input_path = stack.enter_context(snapshot(args.get('input_path'), 'image'))
                    safe_ops = []
                    for op in operations:
                        if not isinstance(op, dict):
                            raise ValueError('each operation must be an object')
                        op = dict(op)
                        if str(op.get('op', '')).lower() == 'overlay':
                            op['path'] = str(stack.enter_context(snapshot(op.get('path'), 'image')))
                        safe_ops.append(op)
                    edit_args = {'input_path': str(input_path), 'operations': safe_ops}
                    if 'output_path' in args:
                        edit_args['output_path'] = args['output_path']
                    return json.dumps(edit_image(edit_args), ensure_ascii=False)
            except ValueError as exc:
                return result(ok=False, error=str(exc))
        if action == 'send_image':
            from .imessage_media import snapshot
            caption = args.get('caption')
            if caption is not None and not isinstance(caption, str):
                return result(ok=False, error='caption must be text')
            try:
                with snapshot(args.get('path'), 'image') as staged:
                    cached = media_cache('image', staged.read_bytes(), staged.suffix.lower())
                return json.dumps(_media_reply(cached, caption=caption or None), ensure_ascii=False)
            except ValueError as exc:
                return result(ok=False, error=str(exc))
            except Exception:
                return result(ok=False, error='Hermes media cache unavailable')
        if action == 'voice_note':
            from pathlib import Path
            from .imessage_media import snapshot
            from .imessage_voice import prepare_voice
            if args.get('audio_path') and 'text' in args:
                return result(ok=False, error='provide either audio_path or text, not both')
            try:
                if args.get('audio_path'):
                    with snapshot(args['audio_path'], 'audio') as staged:
                        prepared = prepare_voice({'audio_path': str(staged)})
                else:
                    prepared = prepare_voice({'text': args.get('text')}, dispatch_tool=getattr(ctx, 'dispatch_tool', None))
                if not prepared.get('ok'):
                    return json.dumps(prepared, ensure_ascii=False)
                output = Path(prepared['path'])
                try:
                    cached = media_cache('audio', output.read_bytes(), output.suffix.lower())
                finally:
                    output.unlink(missing_ok=True)
                    output.parent.rmdir()
                return json.dumps(_media_reply(cached, voice=True), ensure_ascii=False)
            except ValueError as exc:
                return result(ok=False, error=str(exc))
            except Exception:
                return result(ok=False, error='voice conversion or Hermes media cache unavailable')
        if action == 'thread':
            return result(ok=False, error='authenticated thread event history unavailable')
        if action == 'card':
            return result(ok=False, error='interactive cards unavailable: external card service and authenticated callback are not configured or integrated')
        if action in ('background', 'contact_card'):
            return result(ok=False, error='operation unavailable: no public Hermes route exists for it')
        if action == 'poll':
            return result(ok=False, error=POLLS_UNAVAILABLE)
        if action == 'effect':
            return result(ok=False, error=EFFECTS_UNAVAILABLE)
        return result(ok=False, error='unsupported action')

    description = ('iMessage helpers for the active Photon chat. send_image and voice_note do not send by themselves: they stage the file in the Hermes media cache and return a MEDIA: reply_directive that you must include in your reply so Hermes delivers it as an attachment. Media inputs must be regular, non-hardlinked files under HERMES_HOME/image_cache or HERMES_HOME/plugin-data/butler/media, with no symlinks and supported media extensions. These directories are a sharing boundary, not proof of user consent. Voice notes accept text through configured Hermes TTS or an authorized audio_path. Effects and polls have no public Hermes route yet and fail closed; targeted actions and thread history fail closed; cards, backgrounds and contact cards are unavailable.')
    properties = {'action': {'type': 'string', 'enum': list(_ACTIONS)},
                  'chat_id': {'type': 'string'}, 'message_id': {'type': 'string'},
                  'reply_to_message_id': {'type': 'string'}, 'text': {'type': 'string'},
                  'emoji': {'type': 'string'}, 'remove': {'type': 'boolean'},
                  'effect': {'type': 'string'}, 'title': {'type': 'string'},
                  'subtitle': {'type': 'string'}, 'options': {'type': 'array', 'items': {'type': 'string'}},
                  'choices': {'type': 'array', 'items': {'type': 'string'}},
                  'expires_minutes': {'type': 'integer'}, 'audio_path': {'type': 'string'},
                  'path': {'type': 'string'}, 'caption': {'type': 'string'},
                  'clear': {'type': 'boolean'}, 'limit': {'type': 'integer'},
                  'input_path': {'type': 'string'}, 'output_path': {'type': 'string'},
                  'operations': {'type': 'array', 'items': {'type': 'object'}}}
    schema = {'name': 'butler_imessage', 'description': description,
              'parameters': {'type': 'object', 'properties': properties,
                             'required': ['action'], 'additionalProperties': False}}
    ctx.register_tool(name='butler_imessage', toolset='butler', schema=schema,
                      handler=handle, description=description)
