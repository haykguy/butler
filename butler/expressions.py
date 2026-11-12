"""Channel expressions (reactions, replies, effects) with strict targeting.

Butler never reads Hermes runtime/secret stores (e.g. the Photon sidecar
token) or talks to a platform sidecar directly. Reactions and replies stay
disabled until message membership can be verified, and screen/bubble effects
stay disabled until Hermes exposes a public ``ctx.platform_actions`` verb for
them. Every such request fails closed without contacting any backend.
"""
import json

EFFECTS_UNAVAILABLE = ('iMessage effects are unavailable: Hermes has no public plugin route for them yet '
                       '(pending a send_effect verb on ctx.platform_actions, NousResearch/hermes-agent#154299). Send the text as a normal reply.')


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
    because message membership cannot be verified; effects await a public Hermes route.
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
                          capabilities={'react': False, 'reply': False, 'effect': False},
                          note='Reactions and replies are disabled until message membership can be verified. '
                               'Effects are disabled until Hermes exposes a public send_effect platform action.')
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
        if action == 'effect':
            return result(ok=False, error=EFFECTS_UNAVAILABLE)
        return result(ok=False, error='message membership cannot be verified safely')

    description = ('Inspect channel expression capabilities for the active chat. Reactions, threaded replies '
                   'and iMessage effects currently fail closed (no verified message membership and no public '
                   'Hermes platform action for effects); no fallback send is performed.')
    schema = {'name': 'butler_expression', 'description': description,
              'parameters': {'type': 'object', 'properties': {
                  'action': {'type': 'string', 'enum': ['capabilities', 'react', 'reply', 'effect']},
                  'chat_id': {'type': 'string'}, 'message_id': {'type': 'string'},
                  'emoji': {'type': 'string'}, 'remove': {'type': 'boolean'},
                  'text': {'type': 'string'}, 'effect': {'type': 'string'}},
                  'required': ['action'], 'additionalProperties': False}}
    ctx.register_tool(name='butler_expression', toolset='butler', schema=schema,
                      handler=handle, description=description)
