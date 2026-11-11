"""Native iMessage operations using the gateway's authenticated Photon sidecar.

Adapted from Mecha's imessage-native plugin by Hayk and contributors.
No adapter monkeypatching, home-channel fallback, or server is used.
Message-targeted actions remain disabled until the backend proves membership.
"""
import json


from .expressions import _session, _sidecar, _EFFECTS

_ACTIONS = ('capabilities', 'react', 'reply', 'edit', 'unsend', 'poll', 'effect',
            'voice_note', 'send_image', 'card', 'contact_card', 'background',
            'thread', 'image_edit')


def register_imessage_tools(ctx, *, session_provider=None, sidecar=None):
    session_provider = session_provider or _session
    sidecar = sidecar or _sidecar

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
                          note='Media files are limited to Hermes image_cache and plugin-data/butler/media; voice accepts audio_path or text via configured Hermes TTS. Message targets, thread history, cards, backgrounds and contact cards are unavailable.')
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
                    body = {'spaceId': chat, 'path': str(staged), 'kind': 'attachment'}
                    if caption:
                        body['caption'] = caption
                    response = sidecar('/send-attachment', body)
                if not isinstance(response, dict) or not isinstance(response.get('ok'), bool):
                    return result(ok=False, error='invalid Photon sidecar response')
                return json.dumps(response, ensure_ascii=False)
            except ValueError as exc:
                return result(ok=False, error=str(exc))
            except Exception:
                return result(ok=False, error='Photon sidecar unavailable or operation unsupported')
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
                    response = sidecar('/send-attachment', {'spaceId': chat, 'path': str(output),
                                       'kind': 'voice', 'name': prepared['name'],
                                       'mimeType': prepared['mime_type']})
                finally:
                    output.unlink(missing_ok=True)
                    output.parent.rmdir()
                if not isinstance(response, dict) or not isinstance(response.get('ok'), bool):
                    return result(ok=False, error='invalid Photon sidecar response')
                return json.dumps(response, ensure_ascii=False)
            except ValueError as exc:
                return result(ok=False, error=str(exc))
            except Exception:
                return result(ok=False, error='voice conversion or Photon sidecar unavailable')
        if action == 'thread':
            return result(ok=False, error='authenticated thread event history unavailable')
        if action == 'card':
            return result(ok=False, error='interactive cards unavailable: external card service and authenticated callback are not configured or integrated')
        if action in ('background', 'contact_card'):
            return result(ok=False, error='operation unavailable: Photon sidecar does not implement this route')
        body = {'spaceId': chat}
        if action == 'poll':
            title, options = args.get('title'), args.get('options')
            if not isinstance(title, str) or not title.strip() or not isinstance(options, list) or len(options) < 2 or any(not isinstance(x, str) or not x.strip() for x in options):
                return result(ok=False, error='title and at least two nonempty options required')
            body.update(title=title, options=options)
            path = '/send-poll'
        elif action == 'effect':
            text, effect = args.get('text'), args.get('effect')
            if not isinstance(text, str) or not text.strip() or not isinstance(effect, str) or effect not in _EFFECTS:
                return result(ok=False, error='nonempty text and valid effect required')
            body.update(text=text, effect=effect)
            path = '/send-effect'
        else:
            return result(ok=False, error='unsupported action')
        try:
            response = sidecar(path, body)
            if not isinstance(response, dict) or not isinstance(response.get('ok'), bool):
                return result(ok=False, error='invalid Photon sidecar response')
            return json.dumps(response, ensure_ascii=False)
        except Exception:
            return result(ok=False, error='Photon sidecar unavailable or operation unsupported')

    description = ('Native iMessage operations in the active Photon chat. Media inputs must be regular, non-hardlinked files under HERMES_HOME/image_cache or HERMES_HOME/plugin-data/butler/media, with no symlinks and supported media extensions. These directories are a sharing boundary, not proof of user consent. Voice notes accept text through configured Hermes TTS or an authorized audio_path. Targeted actions and thread history fail closed; cards, backgrounds and contact cards are unavailable.')
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
