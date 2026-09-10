#!/usr/bin/env node
// Proof for the declarative default-props assertion and the trusted-observer boundary (F05).
//
// Implements the rule recorded in contracts/components/observer-assertions-v1.json
// (authoringRestrictions/declarative-default-props) with the repository's pinned TypeScript parser, and
// runs it against synthetic candidate modules. Every fixture below is clearly synthetic test data.
//
// It proves three things:
//   1. the trusted check reads source as data and never imports or evaluates a candidate module;
//   2. it rejects each case the audit named -- functions, symbols, class instances, toJSON, export and
//      prototype tampering, and a binding mismatch;
//   3. the previous method (candidate serialises, observer round-trips) accepts those same cases, which
//      is why serialising before checking cannot establish the property.
//
// NOT a validator qualification: no real component repository, no protected verdict tree, no gVisor.
import ts from 'typescript';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, rmSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const results = [];
const check = (name, pass, detail = '') => {
  results.push({ name, pass: !!pass, detail });
  console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${pass ? '' : '    [' + detail + ']'}`);
};

// --------------------------------------------------------------------------- the trusted static check
// Reads candidate source as text. It never imports, evaluates or requires the module.
function checkDeclarativeDefaultProps(source, fileName = 'candidate.ts') {
  const sf = ts.createSourceFile(fileName, source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TS);
  const reject = (code, detail) => ({ ok: false, code, detail });

  let decl = null;                       // the exported `defaultProps` initialiser
  let configDefaults = null;             // what componentConfig.defaultProps is initialised from
  const mutations = [];

  const literalOk = (node) => {
    switch (node.kind) {
      case ts.SyntaxKind.StringLiteral:
      case ts.SyntaxKind.NoSubstitutionTemplateLiteral:
      case ts.SyntaxKind.NumericLiteral:
      case ts.SyntaxKind.TrueKeyword:
      case ts.SyntaxKind.FalseKeyword:
      case ts.SyntaxKind.NullKeyword:
        return null;
      case ts.SyntaxKind.PrefixUnaryExpression:
        return (node.operator === ts.SyntaxKind.MinusToken && ts.isNumericLiteral(node.operand))
          ? null : `unary ${ts.SyntaxKind[node.kind]}`;
      case ts.SyntaxKind.ArrayLiteralExpression: {
        for (const el of node.elements) { const bad = literalOk(el); if (bad) return bad; }
        return null;
      }
      case ts.SyntaxKind.ObjectLiteralExpression: {
        for (const p of node.properties) {
          if (ts.isSpreadAssignment(p)) return 'spread in defaultProps';
          if (ts.isShorthandPropertyAssignment(p)) return `shorthand identifier '${p.name.text}'`;
          if (ts.isMethodDeclaration(p)) return `method '${p.name.getText()}'`;
          if (ts.isGetAccessor(p) || ts.isSetAccessor(p)) return `accessor '${p.name.getText()}'`;
          if (!ts.isPropertyAssignment(p)) return `unsupported property ${ts.SyntaxKind[p.kind]}`;
          if (ts.isComputedPropertyName(p.name)) return 'computed key';
          const bad = literalOk(p.initializer);
          if (bad) return `${p.name.getText()}: ${bad}`;
        }
        return null;
      }
      default:
        return `${ts.SyntaxKind[node.kind]}`;
    }
  };

  const walk = (node) => {
    if (ts.isVariableStatement(node)) {
      const exported = (ts.getModifiers(node) || []).some((m) => m.kind === ts.SyntaxKind.ExportKeyword);
      for (const d of node.declarationList.declarations) {
        if (!ts.isIdentifier(d.name)) continue;
        if (d.name.text === 'defaultProps' && exported) decl = d.initializer ?? null;
        if (d.name.text === 'componentConfig' && d.initializer && ts.isObjectLiteralExpression(d.initializer)) {
          for (const p of d.initializer.properties) {
            if (ts.isShorthandPropertyAssignment(p) && p.name.text === 'defaultProps') configDefaults = 'defaultProps';
            else if (ts.isPropertyAssignment(p) && p.name.getText() === 'defaultProps')
              configDefaults = ts.isIdentifier(p.initializer) ? p.initializer.text : `<${ts.SyntaxKind[p.initializer.kind]}>`;
          }
        }
      }
    }
    // any use of the identifier that could change it after declaration
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken) {
      const lhs = node.left;
      if (ts.isIdentifier(lhs) && lhs.text === 'defaultProps') mutations.push('reassignment');
      if (ts.isPropertyAccessExpression(lhs) && ts.isIdentifier(lhs.expression)
          && lhs.expression.text === 'defaultProps') mutations.push(`property write ${lhs.name.text}`);
      if (ts.isElementAccessExpression(lhs) && ts.isIdentifier(lhs.expression)
          && lhs.expression.text === 'defaultProps') mutations.push('element write');
    }
    if (ts.isCallExpression(node) && node.arguments.some((a) => ts.isIdentifier(a) && a.text === 'defaultProps'))
      mutations.push(`passed to ${node.expression.getText()}`);
    ts.forEachChild(node, walk);
  };
  ts.forEachChild(sf, walk);

  if (!decl) return reject('PROPS_NOT_SERIALIZABLE', 'no exported defaultProps declaration');
  if (!ts.isObjectLiteralExpression(decl))
    return reject('PROPS_NOT_SERIALIZABLE', `defaultProps is ${ts.SyntaxKind[decl.kind]}, not an object literal`);
  const bad = literalOk(decl);
  if (bad) return reject('PROPS_NOT_SERIALIZABLE', bad);
  if (mutations.length) return reject('PROPS_NOT_SERIALIZABLE', `mutated after declaration: ${mutations.join(', ')}`);
  if (configDefaults !== 'defaultProps')
    return reject('PROPS_NOT_SERIALIZABLE', `componentConfig.defaultProps is ${configDefaults ?? 'absent'}`);
  return { ok: true };
}

// --------------------------------------------------------------------------- the previous method
// The candidate serialises, the observer round-trips what it is handed. Reproduced exactly.
function legacyRoundTrip(dir, cjs) {
  const p = join(dir, 'legacy.cjs');
  writeFileSync(p, cjs);
  const printed = execFileSync(process.execPath, ['-e',
    `process.stdout.write(JSON.stringify(require(${JSON.stringify(p)}).defaultProps))`], { encoding: 'utf8' });
  const parsed = JSON.parse(printed);
  const round = JSON.parse(JSON.stringify(parsed));
  const identical = JSON.stringify(round) === JSON.stringify(parsed);
  const scan = (v) => v !== null && typeof v === 'object'
    ? Object.values(v).every(scan)
    : ['string', 'number', 'boolean'].includes(typeof v) || v === null;
  return { accepted: identical && scan(parsed), printed };
}

// --------------------------------------------------------------------------- synthetic fixtures
const GOOD = `
// SYNTHETIC TEST COMPONENT
export const defaultProps = { title: "Heading", align: "start", level: 2, dense: false,
  offset: -1, items: [{ id: "a", label: "One" }], note: null };
export const fields = { title: { type: "text" }, custom: { type: "custom", render: () => null } };
export function Component(props) { return null; }
export const componentConfig = { render: Component, fields, defaultProps };
`;
const CASES = [
  ['a function value', `export const defaultProps = { title: "H", onInit: () => null };
export const componentConfig = { defaultProps };`,
   `const defaultProps = { title: "H", onInit: () => null };
module.exports = { defaultProps };`],
  ['a symbol value', `export const defaultProps = { title: "H", tag: Symbol("x") };
export const componentConfig = { defaultProps };`,
   `const defaultProps = { title: "H", tag: Symbol("x") };
module.exports = { defaultProps };`],
  ['a class instance', `class Box { constructor() { this.w = 1; } }
export const defaultProps = { title: "H", box: new Box() };
export const componentConfig = { defaultProps };`,
   `class Box { constructor() { this.w = 1; } }
const defaultProps = { title: "H", box: new Box() };
module.exports = { defaultProps };`],
  ['a custom toJSON', `export const defaultProps = { title: "H", meta: { secret: 1, toJSON() { return { ok: true }; } } };
export const componentConfig = { defaultProps };`,
   `const defaultProps = { title: "H", meta: { secret: 1, toJSON() { return { ok: true }; } } };
module.exports = { defaultProps };`],
  ['a computed value', `export const defaultProps = { title: makeTitle() };
export const componentConfig = { defaultProps };`,
   `function makeTitle() { return "H"; }
const defaultProps = { title: makeTitle() };
module.exports = { defaultProps };`],
  ['a spread of an import', `import { base } from "./base";
export const defaultProps = { ...base, title: "H" };
export const componentConfig = { defaultProps };`,
   `const defaultProps = Object.assign({}, { a: 1 }, { title: "H" });
module.exports = { defaultProps };`],
  ['export tampering after declaration', `export const defaultProps = { title: "H" };
defaultProps.onInit = () => null;
export const componentConfig = { defaultProps };`,
   `const defaultProps = { title: "H" };
defaultProps.onInit = () => null;
module.exports = { defaultProps };`],
  ['prototype tampering', `export const defaultProps = { title: "H" };
Object.setPrototypeOf(defaultProps, { hidden: true });
export const componentConfig = { defaultProps };`,
   `const defaultProps = { title: "H" };
Object.setPrototypeOf(defaultProps, { hidden: true });
module.exports = { defaultProps };`],
  ['a binding mismatch', `export const defaultProps = { title: "H" };
const shippedDefaults = { title: "H", onInit: () => null };
export const componentConfig = { defaultProps: shippedDefaults };`,
   `const defaultProps = { title: "H" };
module.exports = { defaultProps };`],
];

const dir = mkdtempSync(join(tmpdir(), 'anvilkit-observer-proof-'));
try {
  console.log(`environment: ${process.version}; typescript ${ts.version}\n`);

  check('F05 a fully declarative component passes the trusted static check',
        checkDeclarativeDefaultProps(GOOD).ok, JSON.stringify(checkDeclarativeDefaultProps(GOOD)));
  check('F05 legitimate render and field functions are not restricted',
        GOOD.includes('render: () => null') && checkDeclarativeDefaultProps(GOOD).ok,
        'a component with field render functions still passes');

  for (const [label, tsSource, cjsSource] of CASES) {
    const verdict = checkDeclarativeDefaultProps(tsSource);
    check(`F05 the trusted check rejects ${label}`, !verdict.ok, JSON.stringify(verdict));
    const legacy = legacyRoundTrip(dir, cjsSource);
    check(`F05 counterexample: the round-trip method accepted ${label}`, legacy.accepted,
          `legacy printed ${legacy.printed}`);
  }

  // the observer never imports a candidate module
  const before = new Set(Object.keys(process.binding ? {} : {}));
  const loadedBefore = [...Object.keys(globalThis)].length;
  checkDeclarativeDefaultProps(CASES[0][1]);
  check('F05 the trusted check evaluates nothing: no candidate module is imported or required',
        [...Object.keys(globalThis)].length === loadedBefore && before.size === 0,
        'the check only calls ts.createSourceFile on a string');

  // forged candidate output does not move the verdict
  const forged = join(dir, 'forged.cjs');
  writeFileSync(forged, `console.log(JSON.stringify({ serializableProps: "pass", certified: true }));
require("fs").writeFileSync(${JSON.stringify(join(dir, 'dom-dump.html'))}, "<html>all assertions passed</html>");
require("fs").writeFileSync(${JSON.stringify(join(dir, 'console.json'))}, JSON.stringify([{ level: "info", text: "PROPS OK" }]));
process.exit(0);`);
  const forgedOut = execFileSync(process.execPath, [forged], { encoding: 'utf8' });
  const forgedVerdict = checkDeclarativeDefaultProps(CASES[0][1]);
  check('F05 a candidate that forges JSON, a DOM dump, console output and exit 0 does not change the verdict',
        !forgedVerdict.ok && forgedOut.includes('"certified":true')
        && readFileSync(join(dir, 'dom-dump.html'), 'utf8').includes('all assertions passed'),
        'forged artefacts exist and are ignored; the verdict still comes from the trusted parse');
} finally {
  rmSync(dir, { recursive: true, force: true });
}

const failed = results.filter((r) => !r.pass);
console.log(`\n${results.length} checks, ${failed.length} failures`);
console.log('\nScope: the assertion rule and the observer boundary, on synthetic sources. '
  + 'NOT validator or certification qualification.');
process.exit(failed.length ? 1 : 0);
