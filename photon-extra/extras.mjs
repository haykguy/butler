// No HTTP listener and no implicit authorization; the host must supply both.
export function createExtras({authorize, resolveSpace, sdk = {}, withImageSnapshot} = {}) {
  const ready = action => typeof authorize === 'function' && typeof resolveSpace === 'function' &&
    (action === 'contact_card' ? typeof sdk.nativeContactCard === 'function' :
      typeof sdk.background === 'function' && (action === 'background_clear' || typeof withImageSnapshot === 'function'));
  function capabilities() {
    return {version: 1, contact_card: ready('contact_card'), background_set: ready('background_set'), background_clear: ready('background_clear')};
  }
  const response = (status, error) => ({status, data: error ? {ok:false,error} : {ok:true}});
  async function handle(url, body, context) {
    if (url !== '/x/contact-card' && url !== '/x/background') return null;
    if (!body || typeof body !== 'object' || Array.isArray(body) || typeof body.spaceId !== 'string' || !body.spaceId || body.spaceId.trim() !== body.spaceId) return response(400,'invalid request');
    const isContact = url === '/x/contact-card';
    const keys = Object.keys(body);
    const clear = body.clear === true;
    if (keys.some(k => !(isContact ? ['spaceId'] : ['spaceId','clear','path']).includes(k)) ||
        (!isContact && ((body.clear !== undefined && typeof body.clear !== 'boolean') || (clear ? 'path' in body : typeof body.path !== 'string' || !body.path || body.path === 'clear')))) return response(400,'invalid request');
    const action = isContact ? 'contact_card' : clear ? 'background_clear' : 'background_set';
    if (!ready(action)) return response(503,'operation unavailable');
    try {
      if (await authorize({action, spaceId:body.spaceId, context}) !== true) return response(403,'not authorized');
      const send = async input => {
        const space = await resolveSpace(body.spaceId, {quiet:true});
        if (!space || typeof space.send !== 'function') throw new Error('space unavailable');
        await space.send(isContact ? sdk.nativeContactCard() : sdk.background(input));
      };
      if (action === 'background_set') {
        let invoked = false, completed = false;
        await withImageSnapshot(body.path, async path => {
          if (invoked || typeof path !== 'string' || !path || path === 'clear') throw new Error('invalid snapshot');
          invoked = true;
          await send(path);
          completed = true;
        }, context);
        if (!completed) throw new Error('snapshot did not complete send');
      } else await send(clear ? 'clear' : undefined);
      return response(200);
    } catch {
      return response(502,'operation failed; outcome may be unknown');
    }
  }
  return {capabilities, handle};
}
