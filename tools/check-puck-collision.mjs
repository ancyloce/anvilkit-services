#!/usr/bin/env node
// Controlled Puck package proof. No Studio source import or production-host claim.
// node tools/check-puck-collision.mjs /absolute/path/to/installed/studio
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';

const studio = process.argv[2];
assert(studio, 'Pass the installed Studio root explicitly');
const require = createRequire(resolve(studio, 'package.json'));
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { Render } = require('@puckeditor/core');
const fixture = JSON.parse(readFileSync(new URL('../contracts/catalog/puck-collision-v1.fixtures.json', import.meta.url)));
const original = JSON.stringify(fixture.data);
let builtInHeroCalls = 0;
const builtins = {
  Hero: { render: () => { builtInHeroCalls++; return React.createElement('b', {}, 'WRONG BUILTIN'); } },
  Text: { render: ({ text }) => React.createElement('p', {}, text) },
};

function assemble(data, compatible) {
  const components = Object.create(null);
  const reserved = new Set();
  const excludedBuiltins = new Set();
  for (const entry of data.root.props.remoteComponentLock.entries) {
    assert(!reserved.has(entry.puckType), 'Duplicate lock rejects session');
    reserved.add(entry.puckType);
    components[entry.puckType] = compatible
      ? { render: ({ title }) => React.createElement('section', {}, title) }
      : { fields: {}, defaultProps: {}, permissions: { delete: false, duplicate: false }, render: () => React.createElement('div', { 'data-unavailable': entry.puckType }, 'Remote unavailable') };
  }
  for (const [type, config] of Object.entries(builtins)) {
    if (reserved.has(type)) excludedBuiltins.add(type);
    else components[type] = config;
  }
  return { config: { components }, excludedBuiltins };
}

for (const compatible of [false, true]) {
  const session = assemble(fixture.data, compatible);
  const html = renderToStaticMarkup(React.createElement(Render, { config: session.config, data: fixture.data }));
  assert(html.includes('Unrelated text'));
  assert(html.includes(compatible ? 'Saved remote content' : 'Remote unavailable'));
  assert.equal(builtInHeroCalls, 0);
  assert(session.excludedBuiltins.has('Hero'));
  assert.throws(() => {
    if (session.excludedBuiltins.has('Hero')) throw new Error('SESSION_TYPE_COLLISION');
  }, /SESSION_TYPE_COLLISION/);
  assert.equal(JSON.stringify(fixture.data), original);
  // The host save adapter must retain this full document; this is a JSON round-trip,
  // not a production page-persistence test.
  assert.deepEqual(JSON.parse(JSON.stringify(fixture.data)), fixture.data);
}
const conflicting = structuredClone(fixture.data);
conflicting.root.props.remoteComponentLock.entries.push({ ...conflicting.root.props.remoteComponentLock.entries[0], componentId: 'other' });
assert.throws(() => assemble(conflicting, true), /Duplicate lock/);
const newPage = structuredClone(fixture.data);
newPage.root.props.remoteComponentLock.entries = [];
assert.equal(assemble(newPage, false).config.components.Hero, builtins.Hero);
console.log(JSON.stringify({ node: process.version, puck: require('@puckeditor/core/package.json').version, react: React.version, scenarios: 4 }));
console.log('PASS: controlled Puck SSR and JSON round-trip; production editor, browser, save CAS and host ABI remain unqualified');
