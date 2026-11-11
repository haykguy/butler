import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createExtras } from './extras.mjs';

test('authorized contact card invokes the actual SDK builder and space send', async () => {
  const sent = [];
  const extra = createExtras({ authorize: async () => true, resolveSpace: async id => ({ send: async x => sent.push([id,x]) }), sdk: { nativeContactCard: () => ({type:'contactCard'}) } });
  const result = await extra.handle('/x/contact-card', {spaceId:'chat'} , {});
  assert.deepEqual(result, {status:200,data:{ok:true}});
  assert.deepEqual(sent, [['chat',{type:'contactCard'}]]);
});


test('background set uses snapshot and clear uses SDK clear sentinel', async () => {
  const sent = [];
  const extra = createExtras({authorize: async () => true, resolveSpace: async () => ({send: async x => sent.push(x)}), sdk:{background: value => ({value})}, withImageSnapshot: async (path, use) => {assert.equal(path, '/requested'); await use('/private-snapshot');}});
  assert.equal((await extra.handle('/x/background',{spaceId:'s',path:'/requested'},{})).status,200);
  assert.equal((await extra.handle('/x/background',{spaceId:'s',clear:true},{})).status,200);
  assert.deepEqual(sent,[{value:'/private-snapshot'},{value:'clear'}]);
});

test('snapshot hook must invoke send before reporting success', async () => {
  const extra = createExtras({authorize:async()=>true,resolveSpace:async()=>({send:async()=>{}}),sdk:{background:x=>x},withImageSnapshot:async()=>{}});
  assert.equal((await extra.handle('/x/background',{spaceId:'s',path:'a'},{})).status,502);
});


test('availability reflects installed dependencies, not backend eligibility', () => {
  assert.deepEqual(createExtras().capabilities(),{version:1,contact_card:false,background_set:false,background_clear:false});
  const x=createExtras({authorize:async()=>true,resolveSpace:async()=>{},sdk:{background:()=>{},nativeContactCard:()=>{}}});
  assert.deepEqual(x.capabilities(),{version:1,contact_card:true,background_set:false,background_clear:true});
});

test('rejects malformed and unauthorized requests without resolving or sending', async () => {
  let calls=0;
  const x=createExtras({authorize:async()=>false,resolveSpace:async()=>{calls++},sdk:{background:()=>{},nativeContactCard:()=>{}},withImageSnapshot:async()=>{calls++}});
  for (const [route,body,status] of [['/x/contact-card',{spaceId:'s',other:1},400],['/x/background',{spaceId:'s',clear:true,path:'x'},400],['/x/background',{spaceId:'s',path:'clear'},400],['/x/contact-card',{spaceId:'s'},403],['/x/background',{spaceId:'s',path:'x'},403]]) assert.equal((await x.handle(route,body,{})).status,status);
  assert.equal(await x.handle('/x/reply',{spaceId:'s'},{}),null);
  assert.equal(calls,0);
});

test('SDK failure is unknown outcome and unavailable dependencies fail closed', async () => {
  const x=createExtras({authorize:async()=>true,resolveSpace:async()=>({send:async()=>{throw Error('secret')}}),sdk:{nativeContactCard:()=>({})}});
  assert.deepEqual(await x.handle('/x/contact-card',{spaceId:'s'},{}),{status:502,data:{ok:false,error:'operation failed; outcome may be unknown'}});
  assert.equal((await x.handle('/x/background',{spaceId:'s',clear:true},{})).status,503);
});
