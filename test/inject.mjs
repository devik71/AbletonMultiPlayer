// Підключається до relay як окремий гравець і кидає одну подію.
// Потрібен, щоб перевірити застосування чужих подій у справжньому Live,
// не піднімаючи другу машину.
//
//   node test/inject.mjs TempoSet '{"bpm":135}'
//   node test/inject.mjs TransportSet '{"playing":true}'
//   node test/inject.mjs ClipLaunch '{"track":{"idx":0,"name":"1-MIDI"},"scene":{"idx":0,"name":"1"}}'

import WebSocket from '../daemon/node_modules/ws/index.js';

const [type, payloadRaw] = process.argv.slice(2);
if (!type) {
  console.error('usage: node test/inject.mjs <Type> [payloadJson]');
  process.exit(2);
}

const author = process.env.MP_AUTHOR || 'ghost';
const relay = process.env.MP_RELAY || 'ws://127.0.0.1:19870';
const session = process.env.MP_SESSION || 'default';
const payload = payloadRaw ? JSON.parse(payloadRaw) : {};

const ws = new WebSocket(relay);

ws.on('open', () => ws.send(JSON.stringify({ m: 'join', session, author, since: 1e9, proto: 1 })));

ws.on('message', (raw) => {
  const msg = JSON.parse(raw);
  if (msg.m === 'welcome') {
    console.log(`приєднався як ${author}, head=${msg.head.gseq}, у сесії: ${msg.peers.join(', ')}`);
    ws.send(JSON.stringify({
      m: 'submit',
      event: { type, payload, author, lseq: Date.now(), ts: Date.now() / 1000 },
    }));
  } else if (msg.m === 'commit' && msg.event.author === author) {
    console.log(`закомічено #${msg.event.gseq} ${msg.event.type} ${JSON.stringify(msg.event.payload)}`);
    ws.close();
  } else if (msg.m === 'error') {
    console.error(`relay error [${msg.code}]: ${msg.text}`);
    process.exit(1);
  }
});

ws.on('error', (e) => {
  console.error('relay:', e.message);
  process.exit(1);
});
